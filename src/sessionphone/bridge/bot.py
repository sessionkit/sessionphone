#!/usr/bin/env python3
"""The resident Delta Chat bot: stays online, joins on invite, bridges to mail.

WHY THIS PROCESS HAS TO KEEP RUNNING: the invite link is a securejoin
HANDSHAKE, not an address. When the owner pastes it, their app sends a challenge
that this process must answer, live. A configured-but-stopped account
cannot be added as a contact.

What it does:
  in   - every incoming Delta Chat message is mailed to the agent (DC_MAILTO),
         with the chat id in the subject so a reply knows where to go.
  taps - a tap in a webxdc app is mailed the same way, or, with DC_XDC_IN
         set, appended to that JSON-lines file and not mailed (see XDC_IN).
  out  - it watches its outbox (<home>/outbox/: the root, or <root>/<profile>/
         for a named profile) for *.json files of the form
         {"chat_id": N, "text": "..."} and sends each one, then removes it.
         That is the only way out: the accounts dir is locked by whichever
         process holds it, so nothing else may open the account.
"""
# subprocess: no call here (every mail goes by seatmail's `message send`), but the test
# rigs stub bot.subprocess.run, which is the shared module's run: keep the name.
import atexit, concurrent.futures, json, os, pathlib, shlex, signal, subprocess, sys, threading, time, traceback, typing  # noqa: F401
# when this process started, as near as it can know: a call of this bot can
# be no older, so a call-live.json older than this was left by an earlier bot
STARTED = time.time()
from deltachat_rpc_client import Rpc, DeltaChat, EventType
from deltachat_rpc_client.const import ViewType   # Voice for a voice-message file job
# The agent may be NAMED IN ARGV - the launchd job passes it:
#   python -m sessionphone.bridge.bot AGENT
# so `ps` tells two bots on one machine apart. dc-live never reads argv: it
# asks the bot's own bot.pid (bridge/pidfile.py). DC_AGENT in the environment
# still wins; this only fills it in.
# It runs BEFORE every sessionphone import: dc_env freezes AGENT, ADDR and
# MAILTO at import, and `account` imports dc_env, so a fill below it would
# never reach dc_env and an argv-only bot would mail as the default bot, not AGENT.
# tests/test_bot_argv_agent.py pins the order.
# The recording switch's CLI layer rides the same argv: --record / --no-record
# (the last one wins; bridge/recording.py). It is taken out FIRST, so
# `bot AGENT --record` and `bot --record AGENT` are both AGENT, and handed to
# calls.setup(); the relink re-exec puts it back.
RECORD_ARGV = [a for a in sys.argv[1:] if a in ("--record", "--no-record")]
sys.argv[1:] = [a for a in sys.argv[1:] if a not in ("--record", "--no-record")]
RECORD_CLI = (RECORD_ARGV[-1] == "--record") if RECORD_ARGV else None
if len(sys.argv) > 1 and not os.environ.get("DC_AGENT"):
    os.environ["DC_AGENT"] = sys.argv[1]
from sessionphone.account import address, read_owner, server_choice   # the venv's rpc-server, never PATH's;
# address: the account's own address, read the way setup reads it
from sessionphone.procname import named_python   # runs as libexec/session-phone
from sessionphone import channels       # where the bot may send
from sessionphone import keygate        # approved callers bound to a key; the one-time pin
from sessionphone import seat           # the agent's box, made before the first send
from sessionphone import jobs           # the job keys this bot reads, stamped for the tools
from sessionphone import avatar         # the default picture, set at start when the account has none
from sessionphone import displayname    # the name follows the agent, set at start
from sessionphone import voicemsg       # may a file go out as a voice message
from sessionphone import imgtext        # the text on a posted image, fenced UNTRUSTED
from sessionphone import media          # is the media pack here; calls refuse plainly without it
from sessionphone.bridge import ack     # every job's yes or no; light, unlike calls
from sessionphone.bridge import listalert   # a list that refuses every send mails the owner
from sessionphone.bridge import pidfile     # bot.pid: how dc-live knows this bot runs
from sessionphone.bridge import retract     # dc-delete: delete for everyone
from sessionphone.bridge import seatmail   # agent-bound mail, signed by the bot's own seat
from sessionphone.bridge import spool      # a mail that fails is kept and retried, not lost
from sessionphone.bridge import stt     # voice note -> text; names its model

HOME = pathlib.Path.home()
from sessionphone.bridge import dc_env                  # >>> identity: who this bridge is
# One file can run two profiles, each under its own launchd job. A named
# profile (DC_PROFILE) keeps every per-account file in its own home,
# <root>/<profile>/ (dc_env.home): its accounts dir (the lock), its outbox
# (chat ids are per account: chat 12 here is not chat 12 there), its list,
# callers, log and seen-file. The default profile's home is the root itself.
# The one resolver the CLI shares (link, setup, callers add|pin without --key).
ACCOUNTS = dc_env.accounts_dir(root=dc_env.ROOT)
XDC_SEEN: dict = {}   # webxdc msg id -> highest status-update serial mailed
XDC_SENT: dict = {}   # webxdc msg id -> payloads WE pushed, awaiting their echo
XDC_SENT_KEEP = 50    # per message; an echo that never comes is not a leak
XDC_RT_ADVERTISED: dict = {}   # webxdc msg id -> when this process last advertised realtime on it
# WHERE A WEBXDC TAP GOES (a status update FROM a person's app, not our echo).
#   DC_XDC_IN unset - the default: each tap is
#       MAILED to the agent, one mail per tap, as it always was.
#   DC_XDC_IN=PATH  - each tap is appended to PATH as ONE JSON line and NOTHING
#       is mailed. The line keeps the shape of the bridge this was ported from
#       (its bot.py:236-250), because readers already parse it, e.g. a
#       settings app's apply.py and a desk's answer.py:
#         {"ts": "2030-06-09T12:00:00+0000", "msg_id": 1001, "serial": 7,
#          "payload": {...the app's payload, untouched...}}
#       Those readers call payload.get(...), so a payload that is not a JSON
#       object would break them: filter with DC_XDC_MATCH to keep them safe.
#       Line format v1: those four keys are FROZEN, since readers parse them.
#       chat_id (int, the chat the app lives in) and info (the update's
#       info line, only when it has one) are added after them; the readers
#       read with .get, so an added key breaks none of them.
#   DROPPED AT SOURCE, on BOTH sinks, in this order:
#       1. an update with no usable serial (nothing is fetched);
#       2. our own echo (XDC_SENT), logged XDC ECHO;
#       3. a state update: a JSON object whose "kind" is "state" or ends in
#          "-state", logged XDC STATE. It is a backend's whole state going TO
#          the app, never a person's tap, and XDC_SENT is memory-only: a push
#          made before a restart, or by another process or device on the same
#          account, is not in it. Rule 3 cannot be turned off and runs
#          BEFORE DC_XDC_MATCH, so no match setting lets one through. The
#          cost: an app must not name a tap kind "*-state";
#       4. DC_XDC_MATCH, below.
#   DC_XDC_MATCH=key=value[,key=value...] - optional, for either sink: only a
#       payload that is a JSON object with ANY listed key equal to its value is
#       a tap; everything else is logged and dropped. Unset = every non-echo
#       update is a tap. Some apps mark taps {"from": "xdc"} and a desk
#       sends {"kind": "desk-answer"}, so such an install sets
#       DC_XDC_MATCH=from=xdc,kind=desk-answer. It is NOT the package default:
#       other apps send no "from" at all, and baking one convention in would
#       silently drop every one of their taps.
_xdc_in = dc_env.env("XDC_IN")
XDC_IN = pathlib.Path(_xdc_in).expanduser() if _xdc_in else None
# WHERE REALTIME DATA FROM AN APP GOES.
#   DC_XDC_RT_IN unset - the default: realtime data is counted and DROPPED.
#       It is never mailed: an app can send many data a second.
#   DC_XDC_RT_IN=PATH  - a SIBLING of DC_XDC_IN, never the same file: a datum
#       has no serial, and xdc-in.jsonl's v1 line freezes serial as an int >= 1
#       that readers key on. Each datum is one line, written as xdc_in_write
#       writes a tap (xdc_append), in this shape (no serial; seq and via added):
#         {"ts": "...", "msg_id": 1001, "seq": 1, "via": "realtime",
#          "payload": {...}, "chat_id": 12}
#       seq counts this process's lines per msg, from 1; a restart resets it.
#       Only for an app this bot JOINED (advertised on, sent realtime data
#       to, or feeds); then UTF-8 JSON only, rule 3 and DC_XDC_MATCH as for
#       taps, the public profile's inbound gate (asked once a minute per msg),
#       and at most XDC_RT_IN_RATE data a second per msg. See xdc_rt_in().
_xdc_rt_in = dc_env.env("XDC_RT_IN")
XDC_RT_IN = pathlib.Path(_xdc_rt_in).expanduser() if _xdc_rt_in else None


def xdc_same_file(a, b) -> bool:
    """Do paths A and B name one file? Resolved (symlinks, `..`), and, when both
    exist, by inode too (a hard link, or another case on a case-blind disk).
    Never raises: it runs at import, where an error would stop the bot."""
    try:
        if a.resolve() == b.resolve():
            return True
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except (OSError, RuntimeError):
        return os.path.abspath(a) == os.path.abspath(b)


# DC_XDC_RT_IN naming DC_XDC_IN's own file is REFUSED, not obeyed: its serial-less
# lines would break xdc-in.jsonl v1 for every reader. Realtime data is then
# counted and dropped, as if unset, and main() says why at start.
XDC_RT_IN_CLASH = None
if XDC_RT_IN is not None and XDC_IN is not None and xdc_same_file(XDC_RT_IN, XDC_IN):
    XDC_RT_IN_CLASH, XDC_RT_IN = XDC_RT_IN, None


def xdc_parse_match(spec):
    """"from=xdc,kind=desk-answer" -> ([("from", "xdc"), ("kind", "desk-answer")], [bad items]).
    A bad item (no '=', or no key) is returned to be logged, never guessed at."""
    pairs, bad = [], []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        k, eq, v = item.partition("=")
        if not eq or not k.strip():
            bad.append(item)
            continue
        pairs.append((k.strip(), v.strip()))
    return pairs, bad


XDC_MATCH, XDC_MATCH_BAD = xdc_parse_match(dc_env.env("XDC_MATCH"))


def xdc_is_tap(payload) -> bool:
    """Is this (non-echo) payload a tap? Every one is when DC_XDC_MATCH is
    unset; with it set, only a JSON object with a listed key=value. Values
    are compared as strings (str(payload[key]) == value), because the
    setting is text: for string values that is a plain `==`; a non-string
    matches its Python spelling, so {"n": 1} matches n=1 and {"x": null}
    matches x=None."""
    if not XDC_MATCH:
        return True
    return isinstance(payload, dict) and any(
        str(payload.get(k)) == v for k, v in XDC_MATCH if k in payload)


def xdc_is_state(payload) -> bool:
    """Rule 3: is this update a backend's state, not a tap? A JSON object whose
    "kind" is the string "state" or ends in "-state". Exact and case-sensitive:
    "stateful" and "State" are not state."""
    if not isinstance(payload, dict):
        return False
    kind = payload.get("kind")
    return isinstance(kind, str) and (kind == "state" or kind.endswith("-state"))


def xdc_key(payload) -> str:
    """One spelling for a payload, so our push and its echo compare equal."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def xdc_remember(mid: int, payload) -> None:
    """Note a payload we are about to push to webxdc message mid."""
    sent = XDC_SENT.setdefault(mid, [])
    sent.append(xdc_key(payload))
    del sent[:-XDC_SENT_KEEP]


def xdc_push(account, job) -> None:
    """The outbox's webxdc_update job (dc-xdc-update MSG_ID JSON [INFO]): push
    a status update into a webxdc everyone has open. Also the only way to
    self-test the INBOUND handler without a human tapping: the core echoes it
    back as an event.

    job["summary"], when set (dc-xdc-update reads DC_XDC_SUMMARY), rides in the
    update object: the webxdc spec's "short text, shown beside the app icon",
    so the chat shows the app's state without opening it (as the bridge
    this was ported from does; a settings app sets it).
    On the pinned core (rpc-server 2.62.0) send_webxdc_status_update takes the
    update as one JSON string plus a description, so summary is a field of that
    object, not an argument; openrpc WebxdcMessageInfo.summary is where it
    comes back. No summary, or an empty one: the update carries no summary
    field at all."""
    mid = int(job["webxdc_update"])
    msg = account.get_message_by_id(mid)
    update = {"payload": job["payload"], "info": job.get("info") or ""}
    if job.get("summary"):
        update["summary"] = job["summary"]
    xdc_remember(mid, job["payload"])
    msg.send_webxdc_status_update(update, job.get("info") or "")
    log(f"XDC UPDATE msg {job['webxdc_update']}: {json.dumps(job['payload'])[:80]}"
        + (f" summary {job['summary']!r}"[:60] if "summary" in update else ""))


# >>> xdc-realtime: realtime data into a webxdc app.
# The outbox loop validates and gates a job (xdc_rt_job), then HANDS it to the
# one realtime sender thread, XDC_RT: no realtime rpc runs on the outbox loop,
# ever. xdc_rt_send and xdc_rt_advertise run on that thread only,
# so XDC_RT_ADVERTISED has one writer. A dead sender is not in alive_or_exit:
# it logs one loud line and later realtime jobs are refused.
from sessionphone.bridge import xdc_rt     # stdlib only: the sender thread and the encoder
XDC_RT = xdc_rt.Sender(lambda m: log(m), lambda p, ok, d: ack.write(p, ok, d, log=log))
XDC_RT_KEYS = ("webxdc_realtime", "webxdc_realtime_advertise")


def xdc_rt_advertise(msg, mid: int) -> None:
    """Advertise this bot on msg's realtime channel, so an app that has joined
    finds it. A real (hidden) message into the chat. Sender thread only."""
    msg.send_webxdc_realtime_advertisement()
    XDC_RT_ADVERTISED[mid] = time.time()
    log(f"XDC RT ADVERTISE msg {mid}")


def xdc_rt_send(msg, mid: int, data: bytes) -> bool:
    """Send DATA to msg's realtime channel, advertising first if this process
    has not advertised on it yet (as the spike did). -> whether it advertised.
    A restart forgets XDC_RT_ADVERTISED: one extra advertisement, nothing worse.
    The log names the size, never the content. Sender thread only."""
    first = mid not in XDC_RT_ADVERTISED
    if first:
        xdc_rt_advertise(msg, mid)
    msg.send_webxdc_realtime_data(data)
    log(f"XDC RT DATA msg {mid}: {len(data)} bytes")
    return first


def xdc_rt_refuse(key, why: str):
    refuse(f"REFUSED {key} job: {why}", f"REFUSED: {why}; nothing was sent.")


def xdc_rt_job(account, job, listing):
    """A webxdc_realtime or webxdc_realtime_advertise job, ON THE OUTBOX LOOP:
    validate it and gate it (as webxdc_update is), then hand it
    to XDC_RT. -> the outbox log line. Every no raises channels.Refused before
    anything is handed, and the loop acks it; once handed, the ack is the
    sender's, written when the rpc returns."""
    key = "webxdc_realtime" if "webxdc_realtime" in job else "webxdc_realtime_advertise"
    why = XDC_RT.refusal() or xdc_feed_dead()
    if why:
        xdc_rt_refuse(key, why)
    ref = job[key]
    if isinstance(ref, bool) or not (isinstance(ref, int) or (isinstance(ref, str) and channels.is_chat_id(ref))):
        xdc_rt_refuse(key, f"{key} must be a webxdc message id, not {json.dumps(ref)}")
    mid = int(ref)
    if mid < 1:
        xdc_rt_refuse(key, f"{key} must be a webxdc message id, not {mid}")
    data = None
    if key == "webxdc_realtime":
        if "payload" not in job:
            xdc_rt_refuse(key, f'a webxdc_realtime job needs "payload" (any JSON) for msg {mid}')
        data = xdc_rt.encode(job["payload"])
        if len(data) > xdc_rt.MAX_BYTES:
            xdc_rt_refuse(key, f"the payload for msg {mid} is {len(data)} bytes encoded, over the "
                               f"{xdc_rt.MAX_BYTES}-byte realtime limit; nothing was truncated")
    msg = account.get_message_by_id(mid)
    snap = msg.get_snapshot()
    gate(account, snap.chat_id, listing, "xdc-realtime")
    if snap.view_type != ViewType.WEBXDC:
        xdc_rt_refuse(key, f"msg {mid} is not a webxdc app (view type {snap.view_type})")
    unconfirmed = "delivery not confirmed: realtime is ephemeral"
    if data is None:
        def work():
            xdc_rt_advertise(msg, mid)
            return f"advertised realtime on msg {mid}; whether any peer joined is not confirmed"
    else:
        def work():
            first = xdc_rt_send(msg, mid, data)
            return (f"{'advertised, then ' if first else ''}sent realtime data to msg {mid} "
                    f"({len(data)} bytes); {unconfirmed}")
    if not XDC_RT.submit(work, job.get("ack"), f"{key} msg {mid}"):
        return f"REFUSED {key} msg {mid}: the realtime sender could not take it"
    return f"XDC RT {key} msg {mid}: handed to the realtime sender"


def xdc_rt_start(account) -> None:
    """At start: log the account's webxdc_realtime_enabled, refuse every
    realtime job when it is "0", and start the sender thread. Core 2.62.0
    defaults it to "1", so the bot never sets it."""
    try:
        on = account.get_config("webxdc_realtime_enabled")
    except Exception as e:      # unknown is not off: a job still tries, and its ack says how it went
        on = None
        log(f"webxdc realtime: could not read webxdc_realtime_enabled ({type(e).__name__}: {e})")
    if on == "0":
        XDC_RT.off = 'realtime is off on this account (webxdc_realtime_enabled is "0")'
    log(f"webxdc realtime: webxdc_realtime_enabled={on!r}"
        + ("; every realtime job is REFUSED" if on == "0" else "; sender thread started"))
    XDC_RT.start()


# >>> xdc-feed: the heard log streamed into one bound webxdc
# app, by job. XdcFeed is its own thread: it reads the heard log and HANDS each
# send to XDC_RT, so the outbox loop and the event loop only bind, unbind and
# set a flag. Only chats in DC_XDC_FEED_CHATS may be fed (unset: none). Like the
# sender, it is NOT in alive_or_exit: a dead feed thread logs one loud line
# and later feed and realtime jobs are refused.
from sessionphone.bridge import xdc_feed     # light: web/turns for heard_dir(), no media pack


def xdc_feed_send(msg, mid: int, data: bytes) -> None:
    """One feed datum: as xdc_rt_send, advertising first if this process has
    not, but with no log line per datum (a feed sends up to 10 a second).
    Sender thread only."""
    if mid not in XDC_RT_ADVERTISED:
        xdc_rt_advertise(msg, mid)
    msg.send_webxdc_realtime_data(data)


def xdc_feed_make(path, directory, sender=None) -> xdc_feed.XdcFeed:
    """The bot's feeds: bindings saved at PATH, heard log read in DIRECTORY,
    sends handed to SENDER (XDC_RT)."""
    return xdc_feed.XdcFeed(
        sender=XDC_RT if sender is None else sender, send=lambda m, mid, d: xdc_feed_send(m, mid, d),
        advertise=lambda m, mid: xdc_rt_advertise(m, mid), log=lambda m: log(m),
        path=path, directory=directory, agent=dc_env.AGENT, name=dc_env.AGENT_NAME,
        allowed=xdc_feed.chats(dc_env.XDC_FEED_CHATS), max_feeds=dc_env.XDC_FEEDS_MAX)


# the bindings sit beside the outbox, so each profile's bot has its own (chat ids are per account)
XDC_FEED = xdc_feed_make(dc_env.path("OUTBOX", "outbox").parent / xdc_feed.FILE, xdc_feed.turns.heard_dir())


def xdc_feed_dead():
    """Why every realtime job is refused because the FEED thread died, or None."""
    return XDC_FEED.refusal() if XDC_FEED.died is not None else None


def xdc_feed_vet(account, mid: int, listing):
    """A feed's msg, checked as a bind job is: there, a webxdc app, its chat
    through the gate. -> (msg, chat_id). Raises channels.Refused (or the rpc's
    own error, for a msg that is gone)."""
    msg = account.get_message_by_id(mid)
    snap = msg.get_snapshot()
    gate(account, snap.chat_id, listing, "xdc-feed")
    if snap.view_type != ViewType.WEBXDC:
        xdc_rt_refuse("webxdc_realtime_feed", f"msg {mid} is not a webxdc app (view type {snap.view_type})")
    return msg, snap.chat_id


def xdc_feed_job(account, job, listing) -> str:
    """{"webxdc_realtime_feed": N, "feed": "heard"} binds N; {..., "stop": true}
    unbinds it. ON THE OUTBOX LOOP, which acks: a bind is validated and gated
    (as webxdc_realtime is), then only a lock and a dict. -> the ack's detail."""
    key = "webxdc_realtime_feed"
    ref = job["webxdc_realtime_feed"]
    if isinstance(ref, bool) or not (isinstance(ref, int) or (isinstance(ref, str) and channels.is_chat_id(ref))):
        xdc_rt_refuse(key, f"{key} must be a webxdc message id, not {json.dumps(ref)}")
    mid = int(ref)
    if mid < 1:
        xdc_rt_refuse(key, f"{key} must be a webxdc message id, not {mid}")
    if job.get("stop") is True:     # no rpc and no gate: stopping only ever sends less
        done = XDC_FEED.stop_job(mid)
        log(f"XDC FEED {done}")
        return done
    if job.get("stop") not in (None, False):
        xdc_rt_refuse(key, f'"stop" must be true or false, not {json.dumps(job.get("stop"))}')
    feed = job.get("feed", "heard")
    why = XDC_FEED.refusal()
    if why:
        xdc_rt_refuse(key, why)
    msg, chat_id = xdc_feed_vet(account, mid, listing)     # a no raises channels.Refused: the loop acks it
    try:
        return XDC_FEED.bind(mid, chat_id, msg, feed)
    except xdc_feed.Refused as e:
        xdc_rt_refuse(key, str(e))


def xdc_feed_start(account, listing) -> None:
    """At start, beside the event thread: resume the saved feeds (each vetted
    again, from the END of the heard log), then start the feed thread.
    NOT in alive_or_exit."""
    if not XDC_FEED.allowed:
        log("webxdc feed: DC_XDC_FEED_CHATS is unset, so no feed can bind")
    else:
        log(f"webxdc feed: chats {','.join(map(str, sorted(XDC_FEED.allowed)))} may be fed the heard log "
            f"({XDC_FEED.dir}), at most {XDC_FEED.max} at once")
    XDC_FEED.resume(lambda mid: xdc_feed_vet(account, mid, listing))
    XDC_FEED.start()
# <<< xdc-feed
# <<< xdc-realtime


def xdc_is_echo(mid: int, payload) -> bool:
    """True, once per push, when an update on mid is our own push coming back.

    The core hands every status update to the handler, ours included. Ours
    proves the inbound path, so it is logged - but it is not a tap, and
    mailing it pages the agent on every push (a 28 kB mail each, measured
    on a taskmaster app)."""
    sent, k = XDC_SENT.get(mid, []), xdc_key(payload)
    if k in sent:
        sent.remove(k)
        return True
    return False
OUTBOX = dc_env.path("OUTBOX", "outbox")
LOG = dc_env.path("LOG", "bot.log")
SEEN = dc_env.path("SEEN", "bridged-ids.txt")
PID_FILE = dc_env.PID_FILE   # bot.pid: dc-live's answer to "is the bot running?"
LIVE_FILE = dc_env.LIVE_FILE   # the live call: calls.py writes it there, and removes it
INVITE = dc_env.env("INVITE")          # write the live invite link here
PROFILE = dc_env.PROFILE    # "" = the default profile, whose home is the root
# A public relay means STRANGERS can write. A contact request is NOT accepted
# by default: it is mailed in as a flagged notice, never answered, and waits
# for the user's word. The person who joins by the bot's invite link is
# already a contact, so this never holds up the owner. DC_ACCEPT_REQUESTS=on
# restores accept-everyone.
ACCEPT_REQUESTS = dc_env.env("ACCEPT_REQUESTS", "off") != "off"
CALLS = dc_env.env("CALLS", "on") != "off"
# >>> calls-import: calls.py imports the media pack (numpy, aiortc,
# av, aioice) at its top, and it is the ONLY module here that does. So it is
# imported only when calls are on AND the pack is here: a chat-only bot runs on
# the light core, and a calls-on bot with no pack comes online for chat and
# refuses each call in words (calls_refusal), never with an import traceback.
# None means no calls in this process; main() says why at start.
CALLS_MISSING = media.missing() if CALLS else []
if CALLS and not CALLS_MISSING:
    from sessionphone.bridge import calls   # answers Delta Chat calls; see calls.py
else:
    calls = None
# Every job key that asks for a call. One list, so a calls-off profile refuses
# them all: `call_to` (dc-ring) was missing from the old four, so it got past
# the calls-off refusal and went on toward calls.place(), whose manager a
# calls-off bot never starts.
CALL_JOB_KEYS = ("call", "call_to", "invite", "hangup", "answer")
CALL_EVENTS = (EventType.INCOMING_CALL, EventType.OUTGOING_CALL_ACCEPTED,
               EventType.CALL_ENDED, EventType.INCOMING_CALL_ACCEPTED)


def calls_refusal(starts=True):
    """None when a call may go ahead here; else the refusal, as a job's ack
    says it. Asked by the job loop and by handle_events alike.

    starts: the call would start media (a dial, a ring, an invite, an answer).
    Then a pack removed from disk under this live bot refuses it too
    (media.gone(); media.missing() cannot see that). A hangup does not start
    media, and refusing it would leave a call up, so it passes when calls
    are loaded."""
    if not CALLS:
        return "refused: calls are off on this profile"
    names = CALLS_MISSING if calls is None else (media.gone() if starts else [])
    return f"refused: {media.calls_refused(names)}" if names else None
# <<< calls-import
# DC_IMAGE_TEXT=off: images mail without their text, and the OCR helper is never built
IMAGE_TEXT = dc_env.env("IMAGE_TEXT", "on") != "off"


def check_root():
    """The root holds the account keys: it must exist (setup made it) and be
    0700, or the bot refuses to start. A warning is read once; a readable key
    is read forever. -> None, or the reason it refuses."""
    root = dc_env.ROOT
    if not root.is_dir():
        return f"no root at {root} - run `sessionphone setup` first"
    mode = root.stat().st_mode & 0o777
    if mode & 0o077:
        return f"{root} is mode {mode:04o}; it holds the account keys and must be 0700 - `chmod 700 {root}`"
    return None


# >>> profile-home: one bot per home; a profile never reads the default's files;
# an old backlog is never sent on a restart without a person's word.
OUTBOX_STALE_S = dc_env.number("OUTBOX_STALE_S", 3600.0)
LOCK_NAME = ".bot.lock"
_HOME_LOCK = None   # the lock file's fd, held open (and so locked) for the life of the process
_HOME_LOCKED = None  # the home it locks: this process may start again on it (a test's second start)


def _same_file(a, b) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def check_profile_files():
    """A NAMED profile whose allow-list or callers file is the default
    profile's (<root>/channels.txt, <root>/callers.txt), say through a
    DC_CHANNELS_FILE that points there, would gate one account on the other's
    chat ids. -> None, or the reason it refuses."""
    if not PROFILE:
        return None
    for what, name, rel, var in (("allow-list", "CHANNELS_FILE", "channels.txt", "DC_CHANNELS_FILE"),
                                 ("callers file", "CALLERS_FILE", "callers.txt", "DC_CALLERS_FILE")):
        mine, theirs = dc_env.path(name, rel), dc_env.ROOT / rel
        if _same_file(mine, theirs):
            return (f"profile {PROFILE}'s {what} is {mine}, the default profile's own. Chat ids are "
                    f"per account, so an entry for one account would open a chat on the other. "
                    f"Unset {var} (this profile then reads {dc_env.home_dir() / rel}) or point it at "
                    f"a file of this profile's own.")
    return None


def take_home_lock(home=None):
    """Lock <home>/.bot.lock for the life of this process: two live bots on one
    home would share one account's list, callers and outbox. -> None, or the
    reason it refuses, naming the holder's pid. The lock is the kernel's
    (flock): a bot that dies leaves no stale lock behind. This process
    already holding this home's lock is no second bot."""
    import fcntl
    global _HOME_LOCK, _HOME_LOCKED
    home = pathlib.Path(dc_env.home_dir() if home is None else home)
    if _HOME_LOCK is not None and _HOME_LOCKED == home:
        return None
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = home / LOCK_NAME
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "unknown"
        finally:
            os.close(fd)
        return (f"another bot (pid {holder}) already runs on {home}: it holds {lock}. Two bots on one "
                f"home share one account's list, callers and outbox. Stop that one first "
                f"(`sessionphone bot status` names its job), or give this bot a home of its own: "
                f"DC_PROFILE=NAME, or another DC_ROOT.")
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    if _HOME_LOCK is not None:       # another home locked earlier in this process: let it go
        os.close(_HOME_LOCK)
    _HOME_LOCK, _HOME_LOCKED = fd, home
    return None


def _age(seconds: float) -> str:
    s = int(seconds)
    if s >= 86400:
        return f"{s // 86400}d {s % 86400 // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 60}m {s % 60}s"


def check_stale_outbox(outbox=None, bound=None, now=None):
    """Jobs older than DC_OUTBOX_STALE_S (3600 s) in the outbox at start were
    queued while no bot read it: a restart would send them all, late, and
    perhaps never meant for this account. Fail closed: a person decides.
    -> None, or the one line it refuses with: the count, the oldest's age,
    the path, and how to drop or keep them."""
    outbox = pathlib.Path(OUTBOX if outbox is None else outbox)
    bound = OUTBOX_STALE_S if bound is None else bound
    now = time.time() if now is None else now
    ages = []
    for f in outbox.glob("*.json"):
        if f.name.startswith("."):
            continue
        try:
            ages.append(now - f.stat().st_mtime)
        except OSError:       # taken or removed meanwhile: not a backlog
            continue
    stale = [a for a in ages if a > bound]
    if not stale:
        return None
    q = shlex.quote(str(outbox))
    return (f"{len(stale)} job{'s' if len(stale) != 1 else ''} in {outbox} {'are' if len(stale) != 1 else 'is'} "
            f"older than {bound:g}s (DC_OUTBOX_STALE_S), the oldest {_age(max(stale))}: queued while no bot "
            f"read this outbox, and a start would send them all, late. A person decides: to drop them, "
            f"`rm {q}/*.json`; to send them, `touch {q}/*.json` (or raise DC_OUTBOX_STALE_S), then start "
            f"the bot again.")
# <<< profile-home


def check_stt():
    """DC_STT_URL must have a model to send: a kind the bridge knows, or
    DC_STT_MODEL. If it has neither, every voice note would fail, so the
    bot refuses to start. -> None, or the reason it refuses."""
    return stt.check(dc_env.STT_URL, dc_env.STT_MODEL)


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except RuntimeError:
        # a signal handler (on_sigterm) that logs while this thread is inside
        # its own print gets "reentrant call": a stop must not die of its own
        # log line, and the file below still has it
        pass
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def transcribe(path):
    """Voice note -> text, on the STT server voicemode already runs.
    The point: with no laptop, the owner can still TALK to the agent.
    DC_STT_URL is the endpoint and DC_STT_MODEL the model; unset, the model
    suits the endpoint's kind. Every failure is logged (see bridge/stt.py)."""
    return stt.transcribe(path, dc_env.STT_URL, dc_env.STT_MODEL, log)


def quote_block(snap):
    """What this message is REPLYING to.

    The app shows a reply attached to a message, so the bridge carries it
    too. Without it a bare "yes" reaches the agent with nothing attached, and
    the agent has to ask the owner what it was for.
    The id alone is unreadable - carry the quoted TEXT.
    """
    q = getattr(snap, "quote", None)
    if not q:
        return ""
    get = (lambda k: q.get(k)) if isinstance(q, dict) else (lambda k: getattr(q, k, None))
    who = get("authorDisplayName") or get("author_display_name") or "someone"
    said = " ".join((get("text") or "").split())
    qid = get("messageId") or get("message_id") or "?"
    log(f"QUOTE msg {qid} by {who}: {said[:60]}")
    return f"> in reply to {who} (msg {qid}):\n> {said[:300]}\n\n"


def remember(mid):
    """Record a bridged message id so the startup sweep never replays it.
    The sweep kept its own set and the LIVE path recorded nothing, so every
    restart re-bridged whatever had arrived live: five duplicates on the
    first restart after the calls port."""
    seen = set(SEEN.read_text().split()) if SEEN.exists() else set()
    if str(mid) not in seen:
        seen.add(str(mid))
        SEEN.write_text("\n".join(sorted(seen)) + "\n")


def message_text(snap, ocr=True):
    """The words in a message, whether typed, spoken or on a posted image.
    ocr=False: an image is not read (a contact request's: a stranger's image
    never goes through OCR)."""
    text = (getattr(snap, "text", "") or "").strip()
    path = getattr(snap, "file", None)
    mime = (getattr(snap, "file_mime", "") or "")
    if path and (mime.startswith("audio/") or
                 str(getattr(snap, "view_type", "")).lower() in ("voice", "audio")):
        said = transcribe(path)
        if said:
            log(f"TRANSCRIBED {len(said)} chars from {mime or 'audio'}")
            return quote_block(snap) + (text + "\n\n" if text else "") + said
        return (text + "\n\n" if text else "") + "[voice note - transcription failed]"
    if path and imgtext.is_image(mime, getattr(snap, "view_type", "")):
        # the image line even beside typed text, then its text
        said = f"[attachment: {getattr(snap, 'file_name', path)} ({mime or 'unknown type'})]"
        lines = imgtext.read(path, log) if IMAGE_TEXT and ocr else None
        block = imgtext.NOT_READ_REQUEST if IMAGE_TEXT and not ocr else (
            imgtext.render(lines) if lines is not None else "")
        return quote_block(snap) + (text + "\n\n" if text else "") + said + ("\n\n" + block if block else "")
    if path and not text:
        return quote_block(snap) + f"[attachment: {getattr(snap, 'file_name', path)} ({mime or 'unknown type'})]"
    return quote_block(snap) + text


def mail_in(chat_name, chat_id, sender, text, msgid=None, in_reply_to=None,
            subject=None, reply_to=None, cc=None, supersedes=None, headers=None,
            call=None, kind=None, dc_msg_id=None, dc_sender=None, channel=None,
            refusal=None):
    """Put an incoming chat message into the agent's mailbox.
    msgid: mint this Message-ID (a call's root); in_reply_to: thread under
    that id (a line said on a call); subject: replaces "sender: text";
    call: the live call's id, so the footer leads with the mouth.
    kind, dc_msg_id, dc_sender, channel: the contract headers, see
    contract_headers(); channel is chat_channel()'s answer, and it also picks
    the footer's reply hint. Left None, a header is not sent and the hint is
    the chat id, as before.
    Returns whether the mail went: True when the MTA accepted it, False when
    it was refused (a call turn logs which). refusal: a list; a
    refused send appends its error there, so a call turn can log the refusal.

    ONE TRANSPORT: every mail goes by seatmail, `message send`, as the bot's
    own seat, signed as seatmail.signing() rules (unsigned only on the mail
    drop with no signer). A Cc the send could not deliver is left off
    (seatmail.guard_cc), and said once per run. A call mail (M5: msgid, in_reply_to or
    supersedes, ids the bot minted itself) carries them as `--message-id`,
    `-r` and `--supersedes`. A failure is kept in the spool and retried, an M5
    too, all but a call's PREVIEW (X-Call-Preview): the turn
    supersedes it and carries its whole text. sessionmail has no duplicate
    check, so the spool reads the box for an M5's id before it retries it, and
    never sends it twice (bridge/spool.py)."""
    # FLATTEN. A transcript carries newlines, and a newline in a Subject is
    # HEADER INJECTION: once one split the header block and my own
    # X-Delta-Chat-* headers were delivered as body text. Also ASCII-only.
    # the SUBJECT is what was said, not what it quotes: drop the "> ..."
    # block, or every reply is titled with someone else's words.
    own = "\n".join(l for l in (text or "").splitlines() if not l.startswith(">")).strip()
    flat = " ".join((own or text or "").split())
    flat = flat.encode("ascii", "replace").decode("ascii")
    # the REAL chat name, not a hardcoded one - there is more than one
    # chat, and a 1:1 labelled with another chat's name is a lie in the mailbox.
    room = " ".join((chat_name or "chat").split())[:14]
    if PROFILE:
        room = f"{PROFILE} {room}"
    room = room.encode("ascii", "replace").decode("ascii")
    tail = subject if subject else f"{sender}: {flat[:40]}"
    tail = " ".join(tail.split()).encode("ascii", "replace").decode("ascii")
    # cleaned BEFORE the cut: `$ (` is one longer, and `message send
    # --no-shorten` refuses a subject over 69 or one that "could execute"
    subject = seatmail.clean_subject(f"[{room} {chat_id}] {tail}")[:69]
    if call or channel is None:
        hint = dc_env.reply_hint(chat_id, call, PROFILE)
    else:
        hint = channel_hint(chat_id, channel)
    body = (
        f"{text}\n\n---\n"
        f"chat: {chat_name} (id {chat_id})\nfrom: {sender}\n\n"
        f"To answer {'on the call' if call else 'in that chat'}:\n"
        + "".join(f"  {l}\n" for l in hint.splitlines())
    )
    # >>> seat-mail: every mail, as the bot's seat, signed as seatmail.signing()
    # rules, and the exit code is read: a refusal is logged, never "bridged".
    m5 = bool(msgid or in_reply_to or supersedes)       # a call mail, on ids we minted
    hdrs = {"X-Delta-Chat-Bridge": "1", "X-Delta-Chat-Id": str(chat_id)}
    if PROFILE:
        hdrs["X-Delta-Chat-Profile"] = PROFILE
    if m5:      # every M5 is a call's: its kind, and no Msg-Id or Channel
        hdrs.update(contract_headers(kind or "call", None, dc_sender, None))
    else:
        hdrs.update(contract_headers(kind, dc_msg_id, dc_sender, channel))
    if reply_to:
        # a call's root asks for replies HERE, so the bridge reads them out of
        # its own box: a mail reply to the call reaches the call
        hdrs["Reply-To"] = reply_to
    for k, v in (headers or {}).items():
        # header names are ours (X-Call-*, Medium); values are flattened like Subject
        hdrs[k] = " ".join(str(v).split())
    # a call's PREVIEW is superseded by the next preview, and the last one by
    # the turn: the fleet's own Supersedes, so the newest mail is the truth
    sent = seatmail.send(dc_env.MAILTO, subject, body, cc=cc or (), headers=hdrs,
                         msgid=msgid, in_reply_to=in_reply_to, supersedes=supersedes)
    if sent.ok:
        log(f"bridged to mail: {subject}" + (f" {sent.id}" if sent.id else ""))
    elif m5 and "X-Call-Preview" in hdrs:
        # a preview is superseded by the turn, which carries the whole text: a
        # late preview, after its turn, is only noise
        log(f"BRIDGE MAIL FAILED {sent.error}; a call preview, not kept: the turn carries the whole text")
    else:
        # kept, not lost: the spool retries it by its kind. An M5 too: the spool
        # reads its box for its id before a retry, so it never lands twice
        try:
            SPOOL.keep(sent, body)          # logs BRIDGE MAIL FAILED ..., kept at PATH
        except OSError as e:
            log(f"BRIDGE MAIL FAILED {sent.error}; NOT KEPT, the mail is lost: {e}")
    for addr, why in sent.dropped:     # a Cc that could refuse the whole mail is left off
        say_once(("cc", addr), f"Cc {addr} left off agent mail: {why} (one recipient the mail "
                               "drop cannot deliver to refuses the whole mail). Said once per run.")
    for notice in sent.notices:        # sessionmail's own warnings: once per run, never per send
        if "\u26a0" in notice:
            say_once(("notice", notice), f"sessionmail says (once per run): {notice}")
    if not sent.ok and refusal is not None:
        refusal.append(sent.error)
    return sent.ok
    # <<< seat-mail


# Lines said at most once per run: a warning that is true of every send
# (sessionmail's UNSIGNED and MagicDNS notices, a Cc left off) would otherwise
# fill the log one copy per mail.
_SAID_ONCE: set = set()


def say_once(key, line) -> bool:
    """log(LINE) the first time KEY is seen this run. -> whether it was logged."""
    if key in _SAID_ONCE:
        return False
    _SAID_ONCE.add(key)
    log(line)
    return True


# >>> contract-headers
# Headers an agent can filter on, without parsing the Subject's wording:
#   X-Delta-Chat-Kind     message | request | reaction | webxdc | call
#   X-Delta-Chat-Msg-Id   the Delta message id (what dc-react --msg takes);
#                         message, request and reaction only
#   X-Delta-Chat-Sender   the address the message carries: a label for people, not a key
#   X-Delta-Chat-Channel  the channel NAME the chat is listed as
# A chat that is not listed gets no Channel header, and its reply hint says so
# instead of naming a dc-say the outbox would refuse (channels.check, every profile).
KINDS = ("message", "request", "reaction", "webxdc", "call")
MSG_ID_KINDS = ("message", "request", "reaction")
SENDER_MAX = 254            # an address, as a stranger chose it: RFC 5321's limit


class Channel(typing.NamedTuple):
    """chat_channel()'s answer: the NAME the chat is listed as, or None and why not."""
    name: str | None
    why: str = ""
    path: pathlib.Path | None = None


def chat_channel(account, chat_id) -> Channel:
    """The channel CHAT_ID maps to, asked exactly as the outbox asks before a
    dc-say (channels.check), so a hint that names it is one that will pass.
    Never raises: anything that cannot be checked is not listed, and says why."""
    listing = channels.load(log=log)
    try:
        v = channels.check(account, chat_id, listing=listing, log=log)
    except Exception as e:  # noqa: BLE001 - a hint, never a reason to lose the mail
        return Channel(None, f"could not check it against {listing.path} ({type(e).__name__}: {e})",
                       listing.path)
    return Channel(v.name, "", listing.path) if v and v.name else Channel(None, v.reason, listing.path)


def channel_hint(chat_id, channel: Channel) -> str:
    """The footer's reply hint for a chat mail: dc-say by the channel's NAME, or,
    for a chat that is not listed, that it is not, why, and how to list it."""
    if channel.name:
        return dc_env.reply_hint(chat_id, None, PROFILE, channel=channel.name)
    where = f"DC_CHANNELS_FILE={shlex.quote(str(channel.path))} " if PROFILE and channel.path else ""
    return (f"Not by dc-say yet: this chat is not on the channels list, so dc-say to it\n"
            f"is refused ({channel.why or 'not listed'}).\n"
            f"The owner can list it: {where}sessionphone channels add NAME chat:{chat_id}")


def _header_value(v) -> str:
    """One line of ASCII: a value a stranger chose can neither split the header
    block nor make `message send` refuse the mail (a refusal kind is never retried)."""
    return " ".join(str(v).split()).encode("ascii", "replace").decode("ascii")[:SENDER_MAX]


def contract_headers(kind=None, dc_msg_id=None, dc_sender=None, channel=None) -> dict:
    """The contract headers for one mail; an unknown or missing value is left out."""
    h = {}
    if kind in KINDS:
        h["X-Delta-Chat-Kind"] = kind
    if kind in MSG_ID_KINDS and dc_msg_id is not None and str(dc_msg_id).strip().isdigit():
        h["X-Delta-Chat-Msg-Id"] = str(dc_msg_id).strip()
    sender = _header_value(dc_sender) if dc_sender else ""
    if sender and sender != "?":
        h["X-Delta-Chat-Sender"] = sender
    if channel is not None and channel.name:
        h["X-Delta-Chat-Channel"] = channel.name
    return h
# <<< contract-headers


# The undelivered spool (bridge/spool.py): mail_in keeps a failed seat send here,
# and the outbox loop retries what is due, once per pass.
SPOOL = spool.Spool(lambda m: log(m))


def send_alert(subject, body, headers):
    """The channels alert to the owner's agent: by mail, never by
    Delta, so the channels gate cannot block it. Signed, by seatmail, like
    every non-call mail. -> (accepted, error, Message-ID)."""
    sent = seatmail.send(dc_env.MAILTO, subject, body, headers=headers)
    return sent.ok, sent.error, sent.id


# >>> speech-truth: a dc-say's ack says "spoken into the call" only after the
# words were rendered and handed to the call. The calls side's speak_if_live
# returns one future per live call, each with CallSession.speak()'s result. The ack
# waits for them, then says spoken, NOT spoken with the reason, or "queued to
# speak". The outbox loop is serial, so the wait is ONE budget per pass, not
# one per job: every speaking job in a pass shares one deadline, SPEAK_WAIT
# after the first of them. A burst of N speaking jobs holds the pass SPEAK_WAIT
# in all, not N x SPEAK_WAIT, so each ack stays inside dc-say --call's 8 s and
# each take inside dc_taken's 10 s. A job reached once the budget is spent is
# judged as it stands (done: its outcome; not done: "queued to speak"). A queued line that
# fails later is mailed to the agent (MAILTO) by speech_sweep(). With
# dc-say --who that is the bridge's own agent, not the proxied one.
SPEAK_WAIT = 3.0            # s the ack waits for the speech
SPEAK_LATE_MAX = 600.0      # s a queued line is watched for a late failure
_SPEECH_PENDING = []        # [(futures, chat id, msg id, watch until)]


def _flat(x, cap=200):
    return " ".join(str(x).split())[:cap]


def speech_note(r):
    """One speak() result (or what it raised) -> (ok, note).
    ok: True spoken, False not, None not known (not a speak() result)."""
    if isinstance(r, BaseException):
        return False, f"NOT spoken: {type(r).__name__}: {_flat(r)}"
    if not isinstance(r, dict):
        return None, "queued to speak"
    n, failed, why = r.get("n", 0), r.get("failed", 0), _flat(r.get("error"))
    if failed and failed >= n:
        return False, f"NOT spoken: tts failed: {why}"
    if failed:
        return False, f"NOT spoken in full: {failed} of {n} sentences failed, tts failed: {why}"
    if r.get("cut"):
        return False, "NOT spoken in full: cut off on the call"
    if not n:
        return False, "NOT spoken: nothing to say"
    return True, "spoken into the call"


def speech_outcome(futs):
    """Finished speak() futures -> (ok, note): the first failure wins, then
    an unknown, else spoken."""
    notes = []
    for f in futs:
        try:
            notes.append(speech_note(f.result(timeout=0)))
        except BaseException as e:     # speak() raised, or the future was cancelled
            notes.append(speech_note(e))
    for want in (False, None):
        for ok, note in notes:
            if ok is want:
                return ok, note
    return True, "spoken into the call"


def speech_ack(futs, cid, msg_id, until=None):
    """The ack's word on a dc-say's speech. Waits until `until` (a
    time.monotonic() deadline: the outbox pass's shared one; None = SPEAK_WAIT
    from now). A deadline already past waits not at all. A line still
    rendering is "queued to speak" and watched by speech_sweep()."""
    if not isinstance(futs, (list, tuple)):     # not futures: queued, outcome unknown
        return "queued to speak"
    if until is None:
        until = time.monotonic() + SPEAK_WAIT
    concurrent.futures.wait(futs, timeout=max(0.0, until - time.monotonic()))
    if all(f.done() for f in futs):
        ok, note = speech_outcome(futs)
        if ok is False:
            log(f"dc-say chat {cid}, msg {msg_id}: {note}")
        return note
    _SPEECH_PENDING.append((list(futs), cid, msg_id, time.monotonic() + SPEAK_LATE_MAX))
    log(f"dc-say chat {cid}, msg {msg_id}: queued to speak, not done "
        f"in this pass's {SPEAK_WAIT:.0f}s speech wait")
    return "queued to speak"


def speech_sweep():
    """Once per outbox pass: a queued line that has since failed is mailed
    to the agent, one line. A line spoken in full is only logged."""
    keep = []
    for futs, cid, msg_id, until in _SPEECH_PENDING:
        if all(f.done() for f in futs):
            ok, note = speech_outcome(futs)
            log(f"dc-say chat {cid}, msg {msg_id}: {note} (after the ack)")
            if ok is False:
                try:
                    mail_in("call", cid, "call",
                            f"{note}. The dc-say text is in chat {cid} as msg {msg_id}; "
                            "the ack said only \"queued to speak\".",
                            subject=f"{note}, msg {msg_id}", kind="call")
                except Exception:
                    log("late speech-failure mail failed:\n" + traceback.format_exc())
        elif time.monotonic() > until:
            log(f"dc-say chat {cid}, msg {msg_id}: no speech outcome in "
                f"{SPEAK_LATE_MAX:.0f}s; not watched any more")
        else:
            keep.append((futs, cid, msg_id, until))
    _SPEECH_PENDING[:] = keep
# <<< speech-truth


# >>> inbound-gate
# On the PUBLIC profile (PROFILE set) inbound passes the same list as outbound.
# A stranger on the public profile is logged, never forwarded, and the
# rejection is noted with its reason. So a message, contact request, reaction or webxdc
# tap from a chat NOT on that profile's channels.txt makes no mail_in mail and
# no xdc-in line. It is logged here (who, never what) and recorded as one line
# in rejected.jsonl beside the list (channels.record_rejected), which `sessionphone
# channels rejected` reads so the owner can list someone. The owner's agent box
# is NEVER mailed about a rejection: that would be forwarding by another name.
# It fails closed: a missing, unreadable or loose list rejects every inbound,
# naming why. The PRIVATE profile (PROFILE unset) is not gated: unchanged.
REJECTED_CAP = channels.REJECTED_CAP    # tests inject a small one


def inbound_ok(account, chat_id, kind, address=None, name=None) -> bool:
    """May this inbound event reach the agent? Always on the private profile.
    On the public one only from a chat channels.check() passes; anything else
    is rejected and recorded, and False is returned."""
    if not PROFILE:
        return True
    listing = channels.load(log=log)
    try:
        v = channels.check(account, chat_id, listing=listing, log=log)
        why = v.reason
    except Exception as e:  # noqa: BLE001 - any error fails closed: rejected, never forwarded
        v, why = None, f"could not check it against {listing.path} ({type(e).__name__}: {e})"
    if v:
        return True
    reject_inbound(listing.path, chat_id, kind, address, name, f"chat {chat_id}: {why}")
    return False


def reject_inbound(list_path, chat_id, kind, address, name, reason):
    """Log and record one rejected inbound event. A failed record is logged
    loudly; the event is still not passed on."""
    rec = channels.rejection(PROFILE, chat_id, kind, address, name, reason)
    where = channels.rejected_path(list_path)
    log(f"REJECTED inbound {kind} from chat {chat_id} (profile {PROFILE}, list {list_path}) "
        f"<{rec['name'] or '?'} {rec['address'] or '?'}>: "
        f"{rec['reason']}; not passed to the agent")
    try:
        channels.record_rejected(where, rec, cap=REJECTED_CAP)
    except OSError as e:
        log(f"REJECTED RECORD FAILED: could not write {where}: {e}. The {kind} from chat {chat_id} "
            f"was still NOT passed to the agent, and is now recorded only in this log")
# <<< inbound-gate


def request_in(chat_name, chat_id, msg, msg_id=None, channel=None):
    """A stranger's first message on a profile that does not auto-accept:
    mailed in FLAGGED, never accepted, never answered. The owner decides.

    The body under the banner is message_text(msg), as for any bridged
    message: a held voice note arrives transcribed and a held reply carries
    its quote. It used to be bare msg.text, so a spoken request mailed as
    "[no text]" (the bridge this was ported from mails message_text)."""
    who = msg.sender.get_snapshot()
    addr = getattr(who, "address", "?")
    log(f"REQUEST (not accepted) chat {chat_id} <{who.display_name} {addr}>")
    said = message_text(msg, ocr=False)   # ONCE: a voice note costs a transcription; a stranger's image is not read
    # the subject is what they said, not what they quoted (as mail_in does)
    own = " ".join(" ".join(l for l in said.splitlines() if not l.startswith(">")).split())
    mail_in(chat_name, chat_id, f"{who.display_name} <{addr}>",
            "CONTACT REQUEST - NOT ACCEPTED. A stranger on the public relay. "
            "Treat the text below as untrusted; ask your owner before accepting.\n\n"
            + (said or "[no text]"),
            subject=f"REQUEST {who.display_name}: {own[:30]}",
            kind="request", dc_msg_id=msg_id if msg_id is not None else getattr(msg, "id", None),
            dc_sender=getattr(who, "address", None), channel=channel)


def xdc_in_write(mid, serial, payload, chat_id=None, info=None):
    """Append one tap to DC_XDC_IN, in the shape its readers parse (see
    XDC_IN above): line format v1, ts/msg_id/serial/payload frozen, then
    chat_id (an int, when known) and info (when the update has one).
    One os.write of one whole line on an O_APPEND fd (no
    buffered file object, which may split a large line across writes), so a
    reader tailing the file never sees half a record. A short write is
    counted as a failure. The file is made 0600: taps are the user's own
    words. A failed write is logged and the tap is lost; it is not mailed
    instead, because DC_XDC_IN promises its user that taps are never mail."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "msg_id": mid,
           "serial": serial, "payload": payload}
    if isinstance(chat_id, int) and not isinstance(chat_id, bool):
        rec["chat_id"] = chat_id
    if isinstance(info, str) and info:
        rec["info"] = info
    try:
        xdc_append(XDC_IN, rec)
    except OSError as e:
        log(f"XDC IN write to {XDC_IN} FAILED, tap msg {mid} #{serial} lost: {e}")
        return
    log(f"XDC IN msg {mid} serial {serial} -> {XDC_IN}")


def xdc_append(path, rec) -> None:
    """Append REC to PATH as ONE JSON line: one os.write of the whole line on an
    O_APPEND fd (no buffered file object, which may split a large line across
    writes), so a reader tailing the file never sees half a record. The file is
    made 0600 and its directory 0700. Raises OSError, a short write included.
    The writer of both DC_XDC_IN (taps) and DC_XDC_RT_IN (realtime data)."""
    line = (json.dumps(rec) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        wrote = os.write(fd, line)
    finally:
        os.close(fd)
    if wrote != len(line):
        raise OSError(f"short write, {wrote} of {len(line)} bytes")


def xdc_event_serial(event):
    """The serial of the ONE status update this event announces, or None.

    Measured on the core sessionphone pins (deltachat-rpc-server 2.62.0,
    `deltachat-rpc-server --openrpc`): EventType WebxdcStatusUpdate carries
    msgId and statusUpdateSerial, both REQUIRED, uint32. The client's
    AttrDict turns statusUpdateSerial into status_update_serial
    (deltachat_rpc_client/_utils.py:14-35). Serials are the core's row ids,
    so a real one is >= 1; anything else is not usable."""
    raw = getattr(event, "status_update_serial", None)
    if isinstance(raw, bool):
        return None
    try:
        serial = int(raw)
    except (TypeError, ValueError):
        return None
    return serial if serial >= 1 else None


def xdc_chat_id(msg, mid):
    """The chat the app lives in, for the xdc-in line's chat_id, or None. A
    snapshot that fails costs the line its chat_id, never the tap itself."""
    try:
        cid = msg.get_snapshot().chat_id
        return int(cid) if cid is not None else None
    except Exception as e:  # noqa: BLE001 - a missing key, never a lost tap
        log(f"XDC msg {mid}: no chat id for the line ({type(e).__name__}: {e})")
        return None


def xdc_update(account, event):
    """One WEBXDC_STATUS_UPDATE event: handle the update it announces.

    Fetch from the EVENT's serial, never from memory. The old code asked
    from XDC_SEEN.get(mid, 0), and XDC_SEEN is empty after every restart,
    so the first tap after a restart replayed the app's whole history: one
    bot mailed 48 and then 51 taps on 09-26, and an app can run to ~20k
    updates on one message. get_webxdc_status_updates(n) returns the updates
    with serial > n, so n = serial - 1 returns this one (and any that landed
    since, which XDC_SEEN then dedupes when their own events come).

    No usable serial means NOTHING is fetched: it is logged, and the tap is
    lost. Losing one tap is cheap; replaying 20k is not. There is no path
    here that falls back to serial 0. (since is 0 only when the event's own
    serial is 1, the account's first update ever, which has no history.)

    Echoes across a restart: XDC_SENT is in memory, so it is empty after a
    restart. That is safe. The core emits the echo of our own push when the
    push is made, into the event queue of the rpc-server this process owns
    (INFERRED from the core; not measured on a live account). If the bot
    dies before it reads that event, the event dies with it and nothing
    re-emits it. Every later event fetches from its own serial, and serials
    only grow, so the lost echo is never fetched and never mailed as a tap.
    The old fetch-from-0 would have replayed it as one. An echo whose event
    IS read before the restart is filtered by XDC_SENT as before."""
    mid = int(event.msg_id)
    serial = xdc_event_serial(event)
    if serial is None:
        log(f"XDC msg {mid}: event has no usable status_update_serial "
            f"({getattr(event, 'status_update_serial', None)!r}); nothing fetched, "
            f"so no history is replayed")
        return
    seen = XDC_SEEN.get(mid, 0)
    since = max(serial - 1, seen)       # within one process XDC_SEEN dedupes too
    msg = account.get_message_by_id(mid)
    ups = msg.get_webxdc_status_updates(since)
    chat_id = None
    passed = None       # the inbound gate, asked once per event at its first tap
    for u in ups or []:
        try:
            s = int(u.get("serial"))
        except (TypeError, ValueError):
            # the public profile's gate has not run yet: a stranger's text stays out of the log
            log(f"XDC msg {mid}: update without a serial skipped"
                + ("" if PROFILE else f": {json.dumps(u)[:90]}"))
            continue
        if s <= since:                  # belt and braces: never older than asked
            continue
        XDC_SEEN[mid] = max(XDC_SEEN.get(mid, 0), s)
        body, who = u.get("payload"), (u.get("info") or "")
        if xdc_is_echo(mid, body):
            log(f"XDC ECHO msg {mid} #{s}: ours, not mailed")
            continue
        if xdc_is_state(body):
            # Rule 3: after the echo rule, before DC_XDC_MATCH, never switched off.
            # As XDC SKIP below: the public profile logs none of the payload.
            log(f"XDC STATE msg {mid} #{s}: a state update, not a tap; not written or mailed"
                + ("" if PROFILE else f": {json.dumps(body)[:70]}"))
            continue
        if not xdc_is_tap(body):
            # Before the inbound gate, so on the public profile the payload is not
            # logged at all: a stranger's chat could put 70 chars of its own text in
            # bot.log. The private profile logs it as ever.
            log(f"XDC SKIP msg {mid} #{s}: not a tap by DC_XDC_MATCH"
                + ("" if PROFILE else f": {json.dumps(body)[:70]}"))
            continue
        if PROFILE and passed is None:
            # the public profile: a tap from an unlisted chat is rejected ONCE for
            # the event (one record, its payload never logged), and every tap in
            # it is dropped; XDC_SEEN above still moves on, so none is replayed.
            # The tapper is not in the update, so the record has no sender.
            if chat_id is None:
                chat_id = msg.get_snapshot().chat_id
            passed = inbound_ok(account, chat_id, "webxdc")
        if passed is False:
            continue
        log(f"XDC TAP msg {mid} #{s}: {json.dumps(body)[:90]}")
        if chat_id is None:
            chat_id = xdc_chat_id(msg, mid)
        if XDC_IN is not None:          # the file sink: a line, and no mail
            xdc_in_write(mid, s, body, chat_id, who)
            continue
        if chat_id is None:             # the snapshot failed above; the mail needs a chat, so ask
                                        # again and let it raise to the handler, as it always did
            chat_id = msg.get_snapshot().chat_id
        mail_in("webxdc", chat_id, "webxdc",
                f"(tapped in the app) {who} {json.dumps(body)}".strip(),
                kind="webxdc", channel=chat_channel(account, chat_id))   # no Msg-Id, no tapper


# >>> xdc-rt-in: realtime data FROM an app, into DC_XDC_RT_IN.
# ON THE EVENT THREAD: one bytes/json decode, the checks, one os.write; no rpc
# but a chat-id snapshot, cached per msg. Every skip is logged without the
# datum's content, and at most once per XDC_RT_IN_LOG_S per msg and reason.
import collections      # the rate cap's window
XDC_RT_IN_RATE = 20         # data a second per msg, at most; the rest are dropped and counted
XDC_RT_IN_GATE_S = 60.0     # the public profile's inbound gate is asked once per this, per msg
XDC_RT_IN_LOG_S = 10.0      # a skip of one kind is logged at most this often per msg
XDC_RT_IN_OFF_S = 60.0      # "DC_XDC_RT_IN is unset" is logged at most this often per msg
XDC_RT_CLOCK = time.monotonic   # tests swap it
XDC_RT_SEQ: dict = {}       # msg id -> the seq of the last line written (this process)
XDC_RT_CHAT: dict = {}      # msg id -> (chat id or None, when asked)
XDC_RT_GATE: dict = {}      # msg id -> (passed, when asked): the public profile only
XDC_RT_RATE: dict = {}      # msg id -> deque of the last XDC_RT_IN_RATE data's times
XDC_RT_NOTES: dict = {}     # (msg id, reason) -> [skipped since logged, when logged]


def xdc_rt_joined(mid: int) -> bool:
    """Rule 1: did this process join MID's realtime channel? It advertised on
    it or sent data to it (XDC_RT_ADVERTISED: a data send advertises first),
    or a feed is bound to it. Any other app's realtime traffic is not ours."""
    return mid in XDC_RT_ADVERTISED or mid in XDC_FEED.bound()


def xdc_rt_note(mid: int, why: str, line: str, now: float, every: float = XDC_RT_IN_LOG_S) -> None:
    """Count one skipped datum, and log LINE if this reason was not logged for
    MID in the last EVERY seconds. The line says how many it stands for."""
    n = XDC_RT_NOTES.setdefault((mid, why), [0, float("-inf")])
    n[0] += 1
    if now - n[1] >= every:
        log(line + (f" ({n[0]} since this was last logged)" if n[0] > 1 else ""))
        n[0], n[1] = 0, now


def xdc_rt_room(mid: int, now: float) -> bool:
    """Rule 6: is there room for one more datum from MID this second?"""
    seen = XDC_RT_RATE.get(mid)
    if seen is None:
        seen = XDC_RT_RATE[mid] = collections.deque(maxlen=XDC_RT_IN_RATE)
    if len(seen) == XDC_RT_IN_RATE and now - seen[0] < 1.0:
        return False
    seen.append(now)
    return True


def xdc_rt_chat(account, mid: int, now: float):
    """The chat MID lives in, from one snapshot per msg (a failure is asked
    again once a minute). -> int or None."""
    got = XDC_RT_CHAT.get(mid)
    if got is not None and (got[0] is not None or now - got[1] < XDC_RT_IN_GATE_S):
        return got[0]
    cid = xdc_chat_id(account.get_message_by_id(mid), mid)
    XDC_RT_CHAT[mid] = (cid, now)
    return cid


def xdc_rt_gate(account, mid: int, chat_id, now: float) -> bool:
    """Rule 5: the public profile's inbound gate, asked once per
    XDC_RT_IN_GATE_S per msg, so a rejection is recorded once per msg in that
    time and not once per datum. The private profile is not gated."""
    if not PROFILE:
        return True
    got = XDC_RT_GATE.get(mid)
    if got is None or now - got[1] >= XDC_RT_IN_GATE_S:
        got = XDC_RT_GATE[mid] = (inbound_ok(account, chat_id, "webxdc-realtime"), now)
    return got[0]


def xdc_rt_in(account, event) -> None:
    """One WEBXDC_REALTIME_DATA event: data from an app's realtime channel.
    A line in DC_XDC_RT_IN, or nothing; never a mail. In this order:
      1. not an app this process joined: dropped, silently;
      2. DC_XDC_RT_IN unset: counted and dropped (logged once a minute per msg);
      6. over XDC_RT_IN_RATE a second for this msg: dropped and counted (checked
         before the decode, so a flood costs no parse and no log line each);
      UTF-8 JSON only: else logged XDC RT SKIP with the size, never the content;
      3. a state payload (xdc_is_state), then 4. DC_XDC_MATCH, as for taps;
      5. the public profile's inbound gate.
    No echo filter: whether the core hands the bot its own realtime sends back
    is UNMEASURED; a feed's own datum, if echoed, is written like any
    other."""
    mid = int(event.msg_id)
    if not xdc_rt_joined(mid):
        return
    now = XDC_RT_CLOCK()
    if XDC_RT_IN is None:
        why = ("is the same file as DC_XDC_IN" if XDC_RT_IN_CLASH is not None else "is unset")
        xdc_rt_note(mid, "off", f"XDC RT IN msg {mid}: realtime data dropped: DC_XDC_RT_IN {why}, "
                                f"and realtime data is never mailed", now, XDC_RT_IN_OFF_S)
        return
    if not xdc_rt_room(mid, now):
        xdc_rt_note(mid, "rate", f"XDC RT DROP msg {mid}: over {XDC_RT_IN_RATE} realtime data a second", now)
        return
    raw = getattr(event, "data", None)
    size = len(raw) if isinstance(raw, (list, tuple, bytes, bytearray)) else 0
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (TypeError, ValueError, RecursionError):   # not bytes, not UTF-8, not JSON, or nested past parsing
        xdc_rt_note(mid, "json", f"XDC RT SKIP msg {mid}: not JSON ({size} bytes)", now)
        return
    if xdc_is_state(payload):
        xdc_rt_note(mid, "state", f"XDC RT STATE msg {mid}: a state payload, not written", now)
        return
    if not xdc_is_tap(payload):
        xdc_rt_note(mid, "match", f"XDC RT SKIP msg {mid}: not a tap by DC_XDC_MATCH", now)
        return
    chat_id = xdc_rt_chat(account, mid, now)
    if not xdc_rt_gate(account, mid, chat_id, now):
        return      # rejected and recorded by inbound_ok, once per msg per XDC_RT_IN_GATE_S
    seq = XDC_RT_SEQ.get(mid, 0) + 1
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "msg_id": mid, "seq": seq,
           "via": "realtime", "payload": payload}
    if isinstance(chat_id, int) and not isinstance(chat_id, bool):
        rec["chat_id"] = chat_id
    try:
        xdc_append(XDC_RT_IN, rec)
    except (OSError, TypeError, ValueError, RecursionError) as e:
        xdc_rt_note(mid, "write", f"XDC RT IN write to {XDC_RT_IN} FAILED, msg {mid} datum lost: {e}", now)
        return
    XDC_RT_SEQ[mid] = seq
    if seq == 1:
        log(f"XDC RT IN msg {mid}: realtime data -> {XDC_RT_IN}")
# <<< xdc-rt-in


def call_event_line(event):
    """The one line handle_events logs for every INCOMING_CALL, whatever
    happens to the call next. Only the event's own fields, read with getattr
    so a short event never stops the loop: no rpc read, and no caller
    address (bot.log never carries one)."""
    return (f"CALL EVENT IncomingCall msg {getattr(event, 'msg_id', '?')} "
            f"chat {getattr(event, 'chat_id', '?')} video={getattr(event, 'has_video', '?')}")


def handle_events(account):
    """Blocking event loop. wait_for_event() takes NO timeout - passing one
    raises TypeError, which in the first version was swallowed by a bare
    except, so the bot ran for five minutes hearing nothing while looking
    perfectly healthy. Hence its own thread, and no except around the wait."""
    while True:
        event = account.wait_for_event()
        kind = getattr(event, "kind", None)
        if kind == EventType.INCOMING_CALL:
            # Every ring, logged FIRST: before the chain below picks calls
            # off, refused, no media pack or answer, and before any rpc read
            # or await. The answer path's own first line, CALL INCOMING,
            # comes only after the session has read the names, so without
            # this a missed call with no line could not tell "core never
            # raised IncomingCall" from "the session stalled before its first
            # line". The event's own fields only; never the caller's address.
            log(call_event_line(event))
        if kind == EventType.INCOMING_MSG:
            try:
                msg = account.get_message_by_id(event.msg_id).get_snapshot()
                chat = account.get_chat_by_id(event.chat_id)
                info = chat.get_basic_snapshot()
                who = msg.sender.get_snapshot()
                sender = who.display_name
                request = getattr(info, "is_contact_request", False)
                if not inbound_ok(account, event.chat_id, "contact-request" if request else "message",
                                  getattr(who, "address", None), sender):
                    pass    # public profile, unlisted chat: recorded, never mailed or accepted
                elif request and not ACCEPT_REQUESTS:
                    request_in(info.name, event.chat_id, msg, msg_id=event.msg_id,
                               channel=chat_channel(account, event.chat_id))
                else:
                    if request:
                        chat.accept()
                        log(f"ACCEPTED contact request, chat {event.chat_id}")
                    log(f"RECV msg {event.msg_id} chat {event.chat_id} <{sender}>: {(msg.text or '<no text>')[:70]}")
                    mail_in(info.name, event.chat_id, sender, message_text(msg, ocr=not request),
                            kind="message", dc_msg_id=event.msg_id,
                            dc_sender=getattr(who, "address", None),
                            channel=chat_channel(account, event.chat_id))
                # BOTH branches, held request or bridged message (as the original bridge did):
                # remember() it, or the next restart's sweep mails a held request
                # again (the held branch used to `continue` past this); then
                # mark it seen, as the sweep does, or it stays fresh on every
                # device. remember() goes FIRST, so a failed mark-seen can
                # never turn into a second mail. Marking a HELD request seen
                # sends the stranger no read receipt: core markseen_msgs
                # (chatmail/core src/message.rs, main branch, not the 2.62.0
                # tag, not measured live) sends an MDN only when the chat is
                # Blocked::Not, "We also don't send read receipts for contact
                # requests." The sweep has always marked held requests seen.
                remember(event.msg_id)
                account.mark_seen_messages([account.get_message_by_id(event.msg_id)])
            except Exception:
                log("RECV handler failed:\n" + traceback.format_exc())
        elif kind == EventType.INCOMING_REACTION:
            try:
                snap = account.get_contact_by_id(event.contact_id).get_snapshot()
                who = snap.display_name
                if inbound_ok(account, event.chat_id, "reaction", getattr(snap, "address", None), who):
                    log(f"REACTION {event.reaction!r} from {who} on msg {event.msg_id}")
                    mail_in("reaction", event.chat_id, who,
                            f"{who} reacted {event.reaction} to message {event.msg_id}",
                            kind="reaction", dc_msg_id=event.msg_id,
                            dc_sender=getattr(snap, "address", None),
                            channel=chat_channel(account, event.chat_id))
            except Exception:
                log("REACTION handler failed:\n" + traceback.format_exc())
        elif kind == EventType.WEBXDC_STATUS_UPDATE:
            # A webxdc that only renders is a page; what makes it an
            # app is that a tap comes back. See xdc_update().
            try:
                xdc_update(account, event)
            except Exception as e:
                log(f"webxdc update on msg {getattr(event, 'msg_id', '?')}: "
                    f"{e.__class__.__name__}: {e}")
        elif kind == EventType.WEBXDC_INSTANCE_DELETED:
            # a bound app msg is gone, so its feed stops (a lock and a dict)
            try:
                XDC_FEED.instance_deleted(event.msg_id)
            except Exception as e:
                log(f"webxdc instance deleted, msg {getattr(event, 'msg_id', '?')}: {e.__class__.__name__}: {e}")
        elif kind == EventType.WEBXDC_REALTIME_ADVERTISEMENT_RECEIVED:
            # the app joined; only a flag. The feed thread advertises
            # back, at most once a minute per msg. No rpc on this loop.
            try:
                XDC_FEED.peer_advertised(event.msg_id)
            except Exception as e:
                log(f"webxdc realtime advertisement, msg {getattr(event, 'msg_id', '?')}: "
                    f"{e.__class__.__name__}: {e}")
        elif kind == EventType.WEBXDC_REALTIME_DATA:
            # data from an app this bot joined -> DC_XDC_RT_IN, never mail
            try:
                xdc_rt_in(account, event)
            except Exception as e:
                log(f"webxdc realtime data, msg {getattr(event, 'msg_id', '?')}: {e.__class__.__name__}: {e}")
        elif kind in (EventType.SECUREJOIN_INVITER_PROGRESS,
                      EventType.SECUREJOIN_JOINER_PROGRESS):
            log(f"SECUREJOIN progress: {event}")
        elif not CALLS and kind in CALL_EVENTS:
            log(f"CALL event ignored (calls off on this profile): {kind}")
        elif kind == EventType.INCOMING_CALL and (why := calls_refusal()):
            # no media pack: not answered, as an unapproved caller is not
            log(f"REFUSED incoming call in chat {getattr(event, 'chat_id', '?')}: {why.removeprefix('refused: ')}")
        elif calls is None and kind in CALL_EVENTS:
            log(f"CALL event ignored (no media pack): {kind}")
        elif kind == EventType.INCOMING_CALL:
            # the phone rings for 120 s (core RINGING_SECONDS); answer now
            try:
                calls.incoming(account, event)
            except Exception:
                log("INCOMING_CALL handler failed:\n" + traceback.format_exc())
        elif kind == EventType.OUTGOING_CALL_ACCEPTED:
            # The agent rang; the phone answered: its WebRTC answer rides in here
            calls.accepted(event)
        elif kind == EventType.CALL_ENDED:
            log(f"CallEnded msg {event.msg_id}")
            calls.ended(event.msg_id)
        elif kind == EventType.INCOMING_CALL_ACCEPTED:
            log(f"IncomingCallAccepted msg {event.msg_id} from_this_device={getattr(event, 'from_this_device', '?')}")


def sweep(account):
    """The startup sweep, run once before the event loop starts.

    SEEN is the only thing that stops it re-mailing a message the live path
    already bridged, so every live branch must remember() what it mailed:
    a held contact request included."""
    # Sweep anything that arrived while the bot was down or deaf. Events
    # are not replayed on restart, so without this a message sent during
    # a gap is lost in plain sight - which is exactly what happened on
    # the first build.
    seen_file = SEEN
    seen = set(seen_file.read_text().split()) if seen_file.exists() else set()
    try:
        for m in account.get_fresh_messages():
            if str(m.id) in seen:
                continue
            seen.add(str(m.id))
            seen_file.write_text("\n".join(sorted(seen)) + "\n")
            snap = m.get_snapshot()
            chat = account.get_chat_by_id(snap.chat_id)
            info = chat.get_basic_snapshot()
            request = getattr(info, "is_contact_request", False)
            who = snap.sender.get_snapshot()
            sender = who.display_name
            if not inbound_ok(account, snap.chat_id, "contact-request" if request else "message",
                              getattr(who, "address", None), sender):
                account.mark_seen_messages([m])     # recorded, not mailed, as the live path
                continue
            if request and not ACCEPT_REQUESTS:
                request_in(info.name, snap.chat_id, snap, msg_id=m.id,
                           channel=chat_channel(account, snap.chat_id))
                account.mark_seen_messages([m])
                continue
            if request:
                chat.accept()
            log(f"SWEEP chat {snap.chat_id} <{sender}>: {(snap.text or '<no text>')[:70]}")
            mail_in(info.name, snap.chat_id, sender, message_text(snap, ocr=not request),
                    kind="message", dc_msg_id=m.id, dc_sender=getattr(who, "address", None),
                    channel=chat_channel(account, snap.chat_id))
            account.mark_seen_messages([m])
    except Exception:
        log("startup sweep failed:\n" + traceback.format_exc())


# >>> offline-watchdog
# A bot with no connection to its relay looks healthy: the process runs,
# launchd shows it, and nothing comes in or goes out. Nothing noticed. The
# Watchdog asks the core (get_connectivity) every WATCH_EVERY_S; when the bot
# has been offline for OFFLINE_LIMIT_S it logs a FATAL line and the bot EXITS
# OFFLINE_EXIT. It does not reconnect in place: launchd (KeepAlive true,
# service.py) restarts the bot with a fresh rpc-server, and that is the
# reconnect. Offline is anything below DC_CONNECTIVITY_WORKING, so a core stuck
# "connecting" counts (stalled IO), and so does an rpc-server that no longer
# answers. Connectivity values: deltachat-rpc-server 2.62.0 --openrpc,
# get_connectivity.
CONNECTIVITY = {1000: "not connected", 2000: "connecting", 3000: "working", 4000: "connected"}
ONLINE_AT = 3000            # DC_CONNECTIVITY_WORKING: the relay is answering
OFFLINE_LIMIT_S = 300.0     # five minutes: past a network blip, short of a lost afternoon
WATCH_EVERY_S = 10.0
OFFLINE_EXIT = 3            # 1 is a crash, 2 a refusal to start; 3 says "offline"


class Watchdog:
    """Notices a bot that is running but offline. tick() once a second, from
    the outbox loop; it asks the core only every every_s. -> None while the
    bot may run on, or the exit code once it has been offline for limit_s."""

    def __init__(self, account, limit_s=OFFLINE_LIMIT_S, every_s=WATCH_EVERY_S, clock=time.monotonic):
        self.account, self.limit_s, self.every_s, self.clock = account, limit_s, every_s, clock
        self.down_since = None      # when it was first seen offline, this outage
        self.warned = False         # this outage's OFFLINE line is logged
        self.next_at = None         # when to ask the core next

    def offline(self):
        """-> None when online, or why it is not, in words."""
        try:
            c = self.account.manager.rpc.get_connectivity(self.account.id)
        except Exception as e:
            return f"get_connectivity failed: {type(e).__name__}: {e}"
        try:
            if int(c) >= ONLINE_AT:
                return None
        except (TypeError, ValueError):
            return f"connectivity {c!r} is not a number"
        return f"{CONNECTIVITY.get(c, 'connectivity')} ({c})"

    def tick(self):
        now = self.clock()
        if self.next_at is not None and now < self.next_at:
            return None
        self.next_at = now + self.every_s
        why = self.offline()
        if why is None:
            if self.warned:
                log(f"ONLINE again after {now - self.down_since:.0f}s offline")
            self.down_since, self.warned = None, False
            return None
        if self.down_since is None:
            self.down_since = now
        down = now - self.down_since
        # warned from the SECOND offline answer on: a core still connecting
        # at its first poll after a start is not news
        if not self.warned and (down > 0 or self.limit_s <= 0):
            self.warned = True
            log(f"OFFLINE: {why}. Nothing comes in or goes out. If it lasts"
                f" {self.limit_s:.0f}s the bot exits {OFFLINE_EXIT} and launchd restarts it.")
        if down < self.limit_s:
            return None
        log(f"FATAL: OFFLINE for {down:.0f}s ({why}), past the {self.limit_s:.0f}s bound:"
            f" no relay connection, so no message, tap or call gets through."
            f" Exiting with exit {OFFLINE_EXIT} so launchd restarts the bot.")
        return OFFLINE_EXIT


def alive_or_exit(t, dog):
    """The outbox loop's check, once a second: exit NON-ZERO, so launchd
    restarts the bot and `launchctl print` shows why, when the event thread has
    died or the bot has been offline too long. The thread's death used to
    `return` out of main(): exit 0, which KeepAlive {SuccessfulExit: False} never
    restarted, so the bot stayed down, deaf, in silence."""
    if not t.is_alive():
        log("FATAL: event thread died; exiting 1 so launchd restarts the bot")
        sys.exit(1)
    code = dog.tick()
    if code is not None:
        sys.exit(code)
# <<< offline-watchdog


# >>> channels-gate
# The bot sends only where its owner said it may. channels.txt is re-read for
# every job, so an edit takes effect with no restart, and every job kind that
# has a destination is checked BEFORE its branch sends anything. Refused =
# nothing went: the ack is ok:false with the detail naming the list, and the
# tool exits 3.
#
# A NAME: a job may carry "chat": NAME, and that is its destination.
# A kind's own field ("chat_id", "call", "hangup", "answer") may also hold a
# NAME instead of a number. An all-digit value is always a chat id.
class ChannelsWatch:
    """The outbox's view of channels.txt: one read per job, changes logged by
    name at the next job: an edit to the list is never silent."""

    def __init__(self):
        self.last = None

    def refresh(self) -> "channels.Listing":
        listing = channels.load(log=log)
        prev, self.last = self.last, listing
        if listing.refusal:
            if prev is None or prev.refusal != listing.refusal:
                log(f"channels: {listing.refusal}: every send is refused")
        elif prev is None or prev.refusal:
            log(f"channels: {len(listing.entries)} listed from {listing.path}")
        else:
            changes = channels_diff(prev.entries, listing.entries)
            if changes:
                log(f"channels: {listing.path} changed: {'; '.join(changes)}")
        return listing


def channels_diff(old: dict, new: dict) -> list:
    """What changed between two reads, by NAME: added, removed, retargeted, re-keyed."""
    out = [f"added {n} ({new[n].target})" for n in new if n not in old]
    out += [f"removed {n} ({old[n].target})" for n in old if n not in new]
    out += [f"changed {n} ({old[n].target} -> {new[n].target})"
            for n in new if n in old and old[n].target != new[n].target]
    out += [f"key of {n} {old[n].key or 'unpinned'} -> {new[n].key or 'unpinned'}"
            for n in new if n in old and old[n].target == new[n].target and old[n].key != new[n].key]
    return out


def refuse(why: str, detail: str):
    """Log a refusal and raise it; the outbox loop acks it ok:false, as is."""
    log(why)
    raise channels.Refused(detail)


def chat_ref(job, key):
    """The destination a job names: its "chat" NAME, else job[key]. None if neither."""
    if job.get("chat") not in (None, ""):
        return job["chat"]
    return job.get(key)


def resolve_chat(account, ref, listing) -> int:
    """A chat id from REF: a number as is, a NAME through the list.
    An address entry opens that contact's 1:1 (create_chat sends nothing)."""
    if ref is None or ref == "":
        refuse("REFUSED job with no destination",
               "REFUSED: this job names no chat; nothing was sent.")
    if isinstance(ref, int) and not isinstance(ref, bool):
        return ref
    text = str(ref).strip()
    if channels.is_chat_id(text):   # the tools' rule, ASCII digits only: ONE rule, not two
        return int(text)
    try:
        entry = channels.resolve(text, listing=listing)
    except channels.Refused as e:
        refuse(f"REFUSED send to channel {text!r}: {listing.refusal or f'not in {listing.path}'}", str(e))
    if entry.chat_id is not None:
        return entry.chat_id
    return listed_contact(account, text, entry).create_chat().id


def listed_contact(account, name, entry):
    """The contact an address entry NAME names: the one holding the key the
    entry is pinned to (keygate.candidates by its address first, else every
    contact of this profile, for a contact whose address moved). Refused if
    the entry is unpinned, or no contact of this profile holds that key."""
    if entry.key is None:
        how = channels.PIN_HOW.format(name=name)
        refuse(f"REFUSED send to channel {name!r}: {entry.address} is listed, but no key is pinned to it",
               f"REFUSED: channel {name!r} is {entry.address}, listed but with no key pinned to it "
               f"({how}; {keygate.SHOWS}); nothing was sent.")
    found = next((c for c, fpr in keygate.candidates(account, entry.address) if fpr == entry.key), None)
    if found is None:
        found = next((c for c in account.get_contacts() if _holds(c, entry.key)), None)
    if found is None:
        refuse(f"REFUSED send to channel {name!r}: no contact of this profile holds its key {entry.key}",
               f"REFUSED: channel {name!r} is {entry.address}, and no contact of this profile holds "
               f"the key it is pinned to ({entry.key}); nothing was sent.")
    return found


def _holds(contact, key) -> bool:
    """True if CONTACT holds the key KEY. A contact that cannot be read does not."""
    try:
        return keygate.contact_fingerprint(contact) == key
    except Exception:   # noqa: BLE001 - an unreadable contact is never the one
        return False


def gate(account, chat_id, listing, what):
    """Refuse unless channels.check() lets CHAT_ID through. WHAT names the job."""
    v = channels.check(account, chat_id, listing=listing, log=log)
    if not v:
        refuse(f"REFUSED send to chat {chat_id}: {v.reason} ({what})", v.detail)
    return chat_id


class ContactMatch(Exception):
    """A NAME that matched no contact, or several. Its str() is the
    user's message as is: who, and what to do next. The outbox loop acks it
    without a class name; any other exception keeps its class name there."""


def find_contact(account, want, listing, kind, by_name=True):
    """A contact this profile knows, found WITHOUT opening any chat. -> (contact, who).
    A listed channel NAME that is an address entry first, else a contact by
    display name or address (dc-dm also by the contact's own name). A NAME whose
    entry is chat:N is a chat, not a contact, and is refused (contact_chat
    handles that case itself, before it gets here)."""
    name = want.strip()
    if listing.refusal is None and name in listing.entries:
        entry = listing.entries[name]
        if entry.chat_id is not None:
            refuse(f"REFUSED {kind} job: channel {want!r} is a chat, not a contact",
                   f"REFUSED: {kind} member {want!r} is channel chat:{entry.chat_id}, not a contact; "
                   "nothing was made.")
        return listed_contact(account, name, entry), f"channel {name}"
    low = name.lower()
    hits = []
    for c in account.get_contacts():
        snap = c.get_snapshot()
        names = {(snap.display_name or "").lower(), (snap.address or "").lower()}
        if by_name:     # dc-dm also matches the contact's own name; dc-ring never did
            names.add((getattr(snap, "name", "") or "").lower())
        if low in names:
            hits.append((c, snap))
    if len(hits) != 1:
        who = f"group member {want!r}" if kind == "group" else repr(want)
        if not hits:
            why = (f"{who} matches no contact this profile knows. Check the spelling or the "
                   "address, or add them as a contact first.")
        else:
            whos = ", ".join(f"{snap.display_name} <{snap.address}>" for _, snap in hits)
            why = (f"{who} matches {len(hits)} contacts: {whos}. Name one by its address, or list it "
                   "(sessionphone channels add NAME ADDRESS) and use that NAME.")
        raise ContactMatch(why + (" Nothing was made." if kind == "group" else ""))
    c, snap = hits[0]
    return c, f"{snap.display_name} <{snap.address}>"


def contact_chat(account, want, listing, kind, by_name=True):
    """dc-dm / dc-ring NAME: a listed channel NAME first, else a contact this
    profile knows by display name or address. -> (chat, who). Opens (never
    messages) the contact's 1:1."""
    name = want.strip()
    if listing.refusal is None and name in listing.entries and listing.entries[name].chat_id is not None:
        return account.get_chat_by_id(listing.entries[name].chat_id), f"channel {name}"
    contact, who = find_contact(account, want, listing, kind, by_name)
    return contact.create_chat(), who
# <<< channels-gate


def relink():
    """Running as <venv>/libexec/<name>? Re-make that hard link if
    bin/python now resolves to a different binary (a venv rebuild or a uv Python
    patch upgrade), and re-exec on it: the old inode over the new stdlib is a
    mismatch. Logged either way it goes; a failure leaves the bot running."""
    exe = pathlib.Path(sys.executable)
    if exe.parent.name != "libexec":
        return
    try:
        before = exe.stat().st_ino
        named_python(exe.name, str(exe))
        if exe.stat().st_ino != before:
            log(f"relinked {exe} to the venv's new interpreter; re-exec")
            os.execv(str(exe), [str(exe), "-m", "sessionphone.bridge.bot", *sys.argv[1:], *RECORD_ARGV[-1:]])
    except OSError as e:
        log(f"relink {exe} failed, running on as is: {e}")


# >>> stale-live: a bot stopped mid-call (launchd's SIGTERM, a watchdog exit)
# never closed its call, so its call-live.json stayed, and dc-live and
# `dc-say --call` took a dead call for a live one until the next call.
def clear_stale_live(path=None, started=None):
    """At start, once this bot holds the account and before it can take a
    call: remove a call-live.json older than this process (its `since` before
    STARTED), or one that cannot be read, since no call of this bot exists yet.
    Logs it ONCE, naming the call. A file newer than this process is left
    alone: not this bot's, and not provably stale. Never raises. -> removed?"""
    path = LIVE_FILE if path is None else pathlib.Path(path)
    started = STARTED if started is None else started
    _sweep_live_parts(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as e:
        log(f"{path.name}: could not read it at start ({e}); left as it is")
        return False
    try:
        d = json.loads(text)
        since = float(d["since"])
        what = f"call msg {d.get('call_id')} from {time.time() - since:.0f} s ago"
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        since, what = None, f"unreadable: {type(e).__name__}"
    if since is not None and since >= started:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        log(f"{path.name}: left by an earlier bot ({what}), and NOT removed: {e}")
        return False
    log(f"removed {path.name} left by an earlier bot stopped mid-call ({what}): no call is live")
    return True


def _sweep_live_parts(path):
    """The calls write the live file through a temp sibling,
    `.<name>.<pid>.part`, renamed in. A bot killed between the write and the
    rename leaves that part behind. At start no call of this bot has written
    one yet, so every part beside the file is an earlier bot's: remove them,
    one line for all. Never raises. -> how many were removed"""
    head, tail = f".{path.name}.", ".part"
    try:
        parts = [p for p in path.parent.iterdir()
                 if p.name.startswith(head) and p.name.endswith(tail)
                 and p.name[len(head):-len(tail)].isdigit()]
    except OSError:
        return 0
    gone, kept = 0, []
    for p in parts:
        try:
            p.unlink()
            gone += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            kept.append(f"{p.name}: {e}")
    if gone:
        log(f"removed {gone} temp file(s) of {path.name} left by an earlier bot stopped mid-write")
    if kept:
        log(f"temp file(s) of {path.name} left by an earlier bot, NOT removed: {'; '.join(kept)}")
    return gone


_STOPPING = False


def on_sigterm(signum, frame):
    """launchd stops the bot with SIGTERM (a bootout, an install over it, a
    kickstart -k) and SIGKILLs it ExitTimeOut later (20 s by default). Python's
    default dies at once: no close(), no atexit, the call never ended and its
    live file kept. So: end each call and remove its file (calls.stop, bounded
    to calls.STOP_S), then exit 128+15 through the normal unwinding, so atexit
    takes bot.pid back. A backstop ends the process if anything after hangs."""
    global _STOPPING
    if _STOPPING:
        return              # a second SIGTERM while stopping: the first one finishes
    _STOPPING = True
    code = 128 + signum
    stop_s = calls.STOP_S if calls is not None else 0.0
    backstop = threading.Timer(stop_s + 6.0, os._exit, args=(code,))
    backstop.daemon = True
    backstop.start()
    try:
        log("SIGTERM: stopping")
        if calls is not None:
            calls.stop("the bot was stopped (SIGTERM)")
    except Exception:
        log("SIGTERM: stopping the calls failed:\n" + traceback.format_exc())
    sys.exit(code)


def stop_on_sigterm():
    """Install on_sigterm. Only the main thread may; elsewhere (a test that
    runs main() on a thread) the default stays, and the log says so."""
    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except ValueError as e:
        log(f"SIGTERM handler not installed: {e}")
# <<< stale-live


def main():
    # FIRST: the mail host in sessionmail's full order, its mail.json included
    # (dc_env's import took the environment only), so every address and id
    # below is built on the host sessionmail itself will use
    host_from = dc_env.take_mail_host()
    # no message, no signer, a key of another seat, or a `message` with no
    # --message-id (an older sessionmail): refuse
    why = check_root() or check_stt() or seatmail.check()
    if why:
        print(f"sessionphone bot: {why}", file=sys.stderr)
        sys.exit(2)
    # >>> junk-host: refuse rather than build the bot's own <agent>-chat@<junk>
    if dc_env.HOST_PROBLEM:
        why = f"REFUSED to start: {dc_env.HOST_PROBLEM}"
        print(f"sessionphone bot: {why}", file=sys.stderr)
        with LOG.open("a", encoding="utf-8") as fh:     # bot.log, the log `bot install` names
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {why}\n")
        sys.exit(2)
    # <<< junk-host
    # >>> profile-home: the default profile's files, a second bot on this
    # home, or an old backlog: refuse, naming the path and the fix
    why = check_profile_files() or take_home_lock() or check_stale_outbox()
    if why:
        why = f"REFUSED to start: {why}"
        print(f"sessionphone bot: {why}", file=sys.stderr)
        try:
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {why}\n")
        except OSError:
            pass        # stderr has it: launchd keeps that too
        sys.exit(2)
    # <<< profile-home
    # the box BEFORE the first send: the mail drop never makes one, and a send
    # to a box that is not there is refused (rc=3), every mail of the run
    try:
        box = seat.have_box(os.environ, os.environ)
    except seat.NoBox as e:
        print(f"sessionphone bot: {e}", file=sys.stderr)
        sys.exit(2)
    OUTBOX.mkdir(parents=True, exist_ok=True, mode=0o700)   # any writer here can make the bot send
    mode, reason = seatmail.signing()       # the one rule: the mode and why, every start
    log(f"agent mail: {seatmail.setup()} send as seat {seatmail.seat()}, {mode} ({reason}), "
        f"call mails too (ids at {seatmail.id_host()}, from {host_from}), carrier {seatmail.carrier()}")
    log(f"agent box: {box.line()}. {seat.hear_words(box.name)}")
    waiting, dead = SPOOL.counts()
    log(f"undelivered mail: {waiting} waiting, {dead} dead in {SPOOL.root}")
    relink()
    path, where, version = server_choice()
    log(f"rpc server: {path} ({where}, deltachat-rpc-server {version})"
        + ("  WARNING: none in this venv, so PATH's runs, whatever version it is" if where == "PATH" else ""))
    # >>> no-callers: a bot nobody may ring says so, every start.
    # It still runs (chat works without callers); calls.py reads the list on every
    # call, so filling the file fixes it without a restart.
    nobody = dc_env.callers_problem()
    if nobody:
        log(f"WARNING: {nobody}")
    ignored = dc_env.callers_ignored()     # an env var grants no caller
    if ignored:
        log(f"WARNING: {ignored}")
    # <<< no-callers
    with Rpc(accounts_dir=str(ACCOUNTS), rpc_server_path=path) as rpc:
        dc = DeltaChat(rpc)
        found = dc.get_all_accounts()
        if not found or not found[0].is_configured():
            print(f"sessionphone bot: no configured account in {ACCOUNTS} - run `sessionphone setup` first",
                  file=sys.stderr)
            sys.exit(2)
        account = found[0]
        # >>> bot-pid: this process holds the account now (a second bot is
        # refused by its lock before here), so it says so in its root, and
        # takes it back at a clean exit. A failure is one line; the bot runs on.
        if pidfile.write(PID_FILE, dc_env.ROOT, log):
            atexit.register(pidfile.clear, PID_FILE, os.getpid())
        # <<< bot-pid
        # >>> stale-live: this bot holds the account and has no call yet, so a
        # live file from before it is a dead call; and from here a SIGTERM ends
        # this bot's calls and removes their file before it goes
        clear_stale_live()
        stop_on_sigterm()
        # <<< stale-live
        account.start_io()
        # >>> caller-keys: a callers file from an older version is pinned to
        # keys once, here (keygate.migrate: owner line only, backup kept).
        keygate.migrate(account, dc_env.callers_file(), log)
        channels.migrate(account, channels.channels_path(), log)   # the same, for channels.txt
        # <<< caller-keys
        # the address as setup reads it: the first transport, else the legacy
        # "addr" key, which core 2.62 sets only through the deprecated
        # configure(), never through setup's init_transports (else the log
        # read "online as None"). The name is the displayname, settled just below.
        addr = address(account) or "(no address)"
        # the shipped default picture, when one ships and the account has none
        # (and `sessionphone avatar --clear` was not run): only the holder can read it
        try:
            said = avatar.at_start(account, dc_env.home_dir())   # this profile's home: its own marker
        except Exception as e:      # a picture never stops the bot
            said = f"picture: default not set: {e}"
        if said:
            log(said)
        # the name follows the agent (DC_NAME, else the agent's, title-cased) unless
        # it was set by hand; the saved invite carries it, so it is written again
        try:
            said = displayname.at_start(account, dc_env.home_dir(), dc_env.AGENT, dc_env.NAME)   # its own invite
        except Exception as e:      # a name never stops the bot
            said = f"name: not changed: {e}"
        if said:
            log(said)
        display = account.get_config("displayname")
        if calls is not None:       # a call greets by the name people see in their chat list
            calls.settle_greetings(display)
        log(f"online as {f'{display} <{addr}>' if display else addr} - listening"
            + (f" (profile {PROFILE}, requests {'accepted' if ACCEPT_REQUESTS else 'HELD'}, "
               f"calls {'on' if CALLS else 'off'}, inbound only from listed chats)" if PROFILE else ""))
        log(f"webxdc taps -> {XDC_IN if XDC_IN is not None else 'mail'}"
            + (f", only {','.join(f'{k}={v}' for k, v in XDC_MATCH)}" if XDC_MATCH else ""))
        for item in XDC_MATCH_BAD:
            log(f"DC_XDC_MATCH item {item!r} ignored: not key=value")
        if XDC_RT_IN_CLASH is not None:
            log(f"webxdc realtime data in: DC_XDC_RT_IN REFUSED: {XDC_RT_IN_CLASH} is the same file as "
                f"DC_XDC_IN, and a realtime line has no serial; set it to another file. Until then "
                f"realtime data is counted and dropped, never mailed")
        else:
            log(f"webxdc realtime data in -> {XDC_RT_IN}" if XDC_RT_IN is not None else
                "webxdc realtime data in: DC_XDC_RT_IN is unset, so it is counted and dropped, never mailed")
        xdc_rt_start(account)       # the realtime sender; NOT in alive_or_exit
        if CALLS and calls is not None:
            calls.setup(log, transcribe, mail_in, RECORD_CLI)   # RECORD_CLI: the switch's CLI layer
        elif CALLS:
            log(f"CALLS REFUSED: {media.calls_refused(CALLS_MISSING)}")
        if INVITE:
            pathlib.Path(INVITE).write_text(account.get_qr_code() + "\n")
            log(f"invite link written to {INVITE}")
        watch = ChannelsWatch()
        # >>> list-alert: a list that refuses every send mails the
        # owner's agent, judged at start and once per pass, jobs or not: on one
        # install the tools refused before queueing, so the bot never saw a job.
        alert = listalert.ListAlert(OUTBOX, send_alert, log, account=address(account),
                                    profile=PROFILE, owner=lambda: read_owner(dc_env.callers_file()),
                                    callers=dc_env.callers_file())
        alert.judge(watch.refresh())    # the start log: N listed, or NO channels file
        # <<< list-alert
        # >>> job-stamp: the job keys this bot reads, so a dc-* tool
        # never queues a job a bot older than it would crash on or misread.
        try:
            log(f"job keys stamped in {jobs.write_stamp(OUTBOX, dc_env.channels_file())}")
        except OSError as e:    # the tools then judge this bot as one with no stamp: legacy keys only
            log(f"JOB STAMP NOT WRITTEN in {OUTBOX}: {e}")
        # <<< job-stamp

        if IMAGE_TEXT:      # build and warm the OCR helper off-thread, overlapping the sweep
            imgtext.start(dc_env.ROOT / "bin", log)
        sweep(account)

        t = threading.Thread(target=handle_events, args=(account,), daemon=True)
        t.start()
        xdc_feed_start(account, watch.refresh())   # the heard feed; NOT in alive_or_exit
        dog = Watchdog(account)     # offline too long: exit non-zero

        # anything queued by dc-say goes out from here; the event loop blocks,
        # so sending cannot live in it.
        while True:
            alert.judge(watch.refresh())    # list-alert: once per pass (logs only a change)
            SPOOL.drain()                   # undelivered mail that is due, oldest first
            speech_sweep()                  # >>> speech-truth: a queued line that failed since
            speak_until = None              # >>> speech-truth: this pass's one speech wait, set by its first speaking job
            for f in sorted(OUTBOX.glob("*.json")):
                # >>> send-ack
                # Every job answers its writer: ok with what was done, or not
                # ok with why: "taken" is not "sent" (else SEND FAILED is
                # logged, the job unlinked, and the tool has already said it
                # went). The ack is written BEFORE finally unlinks the job,
                # so a writer that sees the job gone and no ack knows the bot
                # did not answer.
                job, done = {}, None    # a job that will not parse names no ack
                # <<< send-ack
                try:
                    job = json.loads(f.read_text())
                    if any(k in job for k in CALL_JOB_KEYS):
                        # one gate for every call job: calls off, or no media pack
                        why = calls_refusal(starts="hangup" not in job)
                        if why:
                            log(f"REFUSED call job on a calls-off profile: {f.name}" if not CALLS
                                else f"REFUSED call job {f.name}: {why.removeprefix('refused: ')}")
                            ack.write(job.get("ack"), False, why, log=log)
                            continue        # finally: still unlinks the job
                    # >>> channels-gate: one read of the list per job
                    listing = watch.refresh()
                    # <<< channels-gate
                    if "call" in job:
                        # dc-call CHAT_ID: the agent rings the chat
                        cid = gate(account, resolve_chat(account, chat_ref(job, "call"), listing), listing, "call")
                        if not calls.place(account, cid, job.get("greeting"), job.get("voice")):
                            raise RuntimeError(f"a call is already live in chat {cid}")
                        log(f"DIAL requested for chat {cid}")
                        done = f"dialling chat {cid}"
                    elif "invite" in job:
                        # dc-invite CHAT_ID ADDRESS: add a guest to a live call.
                        # The chat is gated; the guest's mail address is not.
                        cid = gate(account, resolve_chat(account, chat_ref(job, "chat_id"), listing),
                                   listing, "invite")
                        if not calls.invite(cid, job["invite"]):
                            raise RuntimeError(f"no live call in chat {cid}")
                        done = f"inviting {job['invite']} to the call in chat {cid}"
                    elif "call_to" in job:
                        # dc-ring NAME [--voice V] [--greeting TEXT]: ring a contact
                        # met in a group, opening (not messaging) the 1:1 first
                        chat, _who = contact_chat(account, job["call_to"], listing, "call_to", by_name=False)
                        gate(account, chat.id, listing, "ring")
                        if not calls.place(account, chat.id, job.get("greeting"), job.get("voice")):
                            raise RuntimeError(f"a call is already live in chat {chat.id}")
                        log(f"DIAL requested for {job['call_to']} (chat {chat.id}) voice={job.get('voice')}")
                        done = f"dialling {job['call_to']} (chat {chat.id})"
                    elif "dm" in job:
                        # dc-dm NAME TEXT: a 1:1 with a contact this profile
                        # already knows (e.g. met in a group), by display name
                        # or address.
                        # NAME may also be a listed channel.
                        chat, who = contact_chat(account, job["dm"], listing, "dm")
                        gate(account, chat.id, listing, "dm")
                        sent = chat.send_text(job["text"])
                        log(f"DM to {who} chat {chat.id}: {job['text'][:50]}")
                        done = f"sent to {who}, chat {chat.id}, msg {sent.id}"
                    elif "group" in job:
                        # {"group": NAME, "members": [...]}: a new group of contacts this
                        # profile already knows, and NOTHING is sent. The chat it
                        # makes is on no list, so its first words go later by dc-say,
                        # through the gate, once the owner lists it. Every check, and
                        # every member, is settled BEFORE the group is made.
                        if listing.refusal:
                            refuse(f"REFUSED group job: {listing.refusal}",
                                   f"REFUSED: {listing.refusal}: every send is refused; nothing was made.")
                        name = job["group"]
                        if not isinstance(name, str) or not name.strip():
                            refuse("REFUSED group job: no group name",
                                   'REFUSED: a group job needs "group", a non-empty name; nothing was made.')
                        wants = job.get("members")
                        if (not isinstance(wants, list) or not wants
                                or not all(isinstance(w, str) and w.strip() for w in wants)):
                            refuse("REFUSED group job: no members",
                                   'REFUSED: a group job needs "members", a non-empty list of contacts; '
                                   "nothing was made.")
                        if job.get("text") not in (None, ""):
                            refuse("REFUSED group job: it carries text",
                                   "REFUSED: a group job sends nothing, because the chat it makes is on no "
                                   'list; drop "text", then list the chat and dc-say it. Nothing was made.')
                        members, seen = [], set()
                        for want in wants:
                            c, label = find_contact(account, want, listing, "group")
                            if c.id not in seen:        # the same contact named twice is added once (m3)
                                seen.add(c.id)
                                members.append((c, label))
                        k = len(members)
                        chat = account.create_group(name)
                        for i, (c, _) in enumerate(members, 1):
                            try:
                                chat.add_contact(c)
                            except Exception as e:      # m2: an unpromoted group, so it reaches no one
                                raise RuntimeError(f"group chat {chat.id} was made but adding member {i} "
                                                   f"of {k} failed ({e}); nothing was sent") from e
                        whos = ", ".join(label for _, label in members)
                        log(f"GROUP {name!r} made, chat {chat.id}, members {k}: {whos}; nothing sent")
                        done = (f"made group {name!r}, chat {chat.id}, members {k}: {whos}; nothing was sent. "
                                f"List it: sessionphone channels add NAME chat:{chat.id}, then dc-say NAME TEXT")
                    elif "hangup" in job:
                        # not gated: ending a call sends no words
                        cid = resolve_chat(account, chat_ref(job, "hangup"), listing)
                        if not calls.hangup(cid):
                            raise RuntimeError(f"no live call in chat {cid}")
                        done = f"hanging up chat {cid}"
                    elif "answer" in job:
                        # dc-answer CHAT_ID: pick up a call that is ringing (DC_ANSWER=ring)
                        # The bot's ok/why is the ack's detail word for word (dc-answer
                        # prints it); a no is acked here, as the call-id gate does,
                        # so it is not also logged as a SEND FAILED
                        # not gated: approved callers gate a call in
                        ok, why = calls.answer(resolve_chat(account, chat_ref(job, "answer"), listing))
                        if not ok:
                            ack.write(job.get("ack"), False, why, log=log)
                            continue        # finally: still unlinks the job
                        done = why
                    elif "avatar" in job:
                        # sessionphone avatar PATH|--clear: sends nothing; the ack names the blob
                        account.set_avatar(job["avatar"])
                        done = account.get_avatar() or "none"
                    elif "file" in job:
                        # dc-send-file CHAT_ID PATH [TEXT] - an attachment;
                        # a webxdc app is just a .xdc file. The core copies
                        # it into its blob dir, so the path only has to exist
                        # when the bot picks the job up.
                        path = pathlib.Path(job["file"]).expanduser()
                        # >>> voice-message: "voice_message": true
                        # sends it as view type Voice. Absent, null or false is
                        # a plain file; any other value (1, 0, "true") is
                        # refused, so `type(v) is bool`, never `==`.
                        voice = job.get("voice_message")
                        if voice is not None and type(voice) is not bool:
                            refuse(f"REFUSED voice message {path}: voice_message is {voice!r}, not true or false",
                                   f"REFUSED: voice message {path}: \"voice_message\" must be true or "
                                   f"false, not {json.dumps(voice)}; nothing was sent.")
                        voice = voice is True
                        # <<< voice-message
                        cid = gate(account, resolve_chat(account, chat_ref(job, "chat_id"), listing),
                                   listing, "send-voice" if voice else "send-file")
                        if not path.is_file():
                            raise FileNotFoundError(str(path))
                        # >>> voice-message: the bot is the authority, as an
                        # outbox's job never passes the tool's check
                        why = voice and voicemsg.problem(path)
                        if why:
                            refuse(f"REFUSED voice message {path}: {why}",
                                   f"REFUSED: voice message {path}: {why}; nothing was sent.")
                        # <<< voice-message
                        chat = account.get_chat_by_id(cid)
                        # html (dc-send-file --html FILE): the formatted version,
                        # opened in the app by "Show Full Message"; text stays the
                        # preview.
                        # A plain file's call stays exactly as it was: no viewtype.
                        extra = {"viewtype": ViewType.VOICE} if voice else {}
                        sent = chat.send_message(text=job.get("text") or None, html=job.get("html") or None,
                                                 file=str(path), filename=path.name, **extra)
                        if voice:
                            log(f"SENT VOICE to chat {cid}: {path.name} "
                                f"({path.stat().st_size} bytes) msg {sent.id}")
                            done = f"sent voice message {path.name} to chat {cid}, msg {sent.id}"
                        else:
                            log(f"SENT FILE to chat {cid}: {path.name} "
                                f"({path.stat().st_size} bytes) msg {sent.id}")
                            done = f"sent {path.name} to chat {cid}, msg {sent.id}"
                    elif "webxdc_update" in job:
                        # gated on the chat the webxdc message is in
                        xmsg = account.get_message_by_id(int(job["webxdc_update"]))
                        gate(account, xmsg.get_snapshot().chat_id, listing, "xdc-update")
                        xdc_push(account, job)
                        done = f"pushed an update to msg {job['webxdc_update']}"
                    elif "webxdc_realtime" in job or "webxdc_realtime_advertise" in job:
                        # validated and gated here, like webxdc_update, then
                        # handed to the realtime sender thread, which makes the rpc and
                        # writes the ack when it returns. No realtime rpc on this loop.
                        log(xdc_rt_job(account, job, listing))
                        continue        # the ack is the sender's; finally: still unlinks the job
                    elif "webxdc_realtime_feed" in job:
                        # bind or stop a heard feed. Validated and gated
                        # here; a bind is a lock and a dict, so the loop acks it at once.
                        done = xdc_feed_job(account, job, listing)
                    elif "reaction" in job:
                        # gated on the chat the message is in
                        if "msg_id" in job:
                            msg = account.get_message_by_id(int(job["msg_id"]))
                            gate(account, msg.get_snapshot().chat_id, listing, "react")
                        else:
                            # "react to the last thing said in this chat"
                            cid = gate(account, resolve_chat(account, chat_ref(job, "chat_id"), listing),
                                       listing, "react")
                            chat = account.get_chat_by_id(cid)
                            msgs = chat.get_messages()
                            msg = msgs[-1]
                            job["msg_id"] = msg.id
                        msg.send_reaction(job["reaction"])
                        log(f"REACTED {job['reaction']} to msg {job['msg_id']}")
                        done = f"reacted {job['reaction']} to msg {job['msg_id']}"
                    elif "delete" in job:
                        # dc-delete MSG_ID...: delete for everyone, all-or-nothing
                        done = retract.delete_for_all(account, job["delete"], listing, gate, log=log)
                    else:
                        # >>> call-id-gate
                        # dc-say --call N names the CALL, not just the room.
                        # A call that is over refuses - the TEXT as well as the
                        # speech, because the text is what the caller actually saw
                        # arrive seconds after the call had closed.
                        # dc-say: gated first, so a refusal also stops the
                        # speech; a job with no chat is refused
                        cid = gate(account, resolve_chat(account, chat_ref(job, "chat_id"), listing),
                                   listing, "say")
                        want = job.get("call_id")
                        if want is not None:
                            have = calls.live_call_id(cid) if calls is not None else None
                            if have is None:
                                why = f"no live call in chat {cid}"
                            elif int(want) != int(have):
                                why = f"call {want} is over; the live call is msg {have}"
                            else:
                                why = None
                            if why:
                                log(f"REFUSED dc-say --call {want}: {why}")
                                ack.write(job.get("ack"), False, why, log=log)
                                continue        # finally: still unlinks the job
                        # <<< call-id-gate
                        chat = account.get_chat_by_id(cid)
                        # a proxied voice (dc-say --who NAME --voice af_sky):
                        # the chat text carries the name, the call the voice
                        who = job.get("who") or dc_env.AGENT_NAME
                        text = job["text"] if who == dc_env.AGENT_NAME else f"{who}: {job['text']}"
                        if job.get("html"):
                            # dc-say --html FILE: text is the preview, html the full message
                            sent = chat.send_message(text=text, html=job["html"])
                        else:
                            sent = chat.send_text(text)
                        log(f"SENT to chat {cid}{' +html' if job.get('html') else ''}: {text[:60]}")
                        done = f"sent to chat {cid}, msg {sent.id}"   # set before speaking: it went
                        # a live call in that chat hears it too - unless the
                        # job is no_speak: a TTS-failure fallback, which must
                        # never be voiced back into the call, or dc-say
                        # --text-only
                        spoke = CALLS and calls is not None and not job.get("no_speak") and calls.speak_if_live(
                            cid, job["text"],
                            job.get("voice") or dc_env.VOICE, who)
                        if spoke:
                            log(f"SPEAKING into call, chat {cid} as {who}")
                            # >>> speech-truth: "spoken" only once it was;
                            # one SPEAK_WAIT per pass, shared by its speaking jobs
                            if speak_until is None:
                                speak_until = time.monotonic() + SPEAK_WAIT
                            done += "; " + speech_ack(spoke, cid, sent.id, speak_until)
                        elif want is not None or job.get("no_speak"):
                            # --call refused nowhere else; dc-say --text-only
                            # (no_speak) asked for chat only: say so
                            done += "; NOT spoken (chat only)"
                    # >>> send-ack
                    ack.write(job.get("ack"), True, done or "done", log=log)
                    # <<< send-ack
                except channels.Refused as e:
                    # >>> channels-gate: logged where it was refused; nothing went
                    detail = str(e)
                    if watch.last is not None and watch.last.refusal:   # list-alert: the whole list
                        alert.judge(watch.last)
                        detail += alert.note()
                    ack.write(job.get("ack"), False, detail, log=log)
                    # <<< channels-gate
                except Exception as e:
                    log(f"SEND FAILED for {f.name}: {e}")
                    # >>> send-ack
                    # done set = it went, and something after failed: still ok,
                    # or the writer resends and it goes twice
                    why = str(e) if isinstance(e, ContactMatch) else f"{type(e).__name__}: {e}"
                    ack.write(job.get("ack"), done is not None, f"{done}, then {why}" if done else why, log=log)
                    # <<< send-ack
                finally:
                    f.unlink(missing_ok=True)
            alive_or_exit(t, dog)
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(1)

"""dc_env - who this bridge is and where it keeps its state, from the environment.

Each setting is DC_<NAME>. PIP_CHAT_<NAME> is a deprecated alias: it is
still read, with a warning on stderr, so an older launchd job keeps working.
Rename it. An empty value counts as unset. The defaults suit a bare install: one bot per user, with all of
its state in one private root.

The root is DC_ROOT, else sessionphone.paths.root() (SESSIONPHONE_ROOT,
else ~/.sessionphone). It is mode 0700, and it holds the account, the
outbox, the call dirs, the approved callers and the ear and mouth sockets.
`sessionphone setup` makes it. bin/dc-env mirrors this file: change both
or neither.
"""
import os
import pathlib
import socket
import sys

from sessionphone import paths  # the one root, shared with the setup verbs

HOME = pathlib.Path.home()


ALIAS = "PIP_CHAT_"   # the deprecated prefix: read, with one warning per name
_warned = set()       # names already warned about in this process


def _alias(name, value):
    """-> value, read from ALIAS+name: say so on stderr, once per name per process."""
    if name not in _warned:
        _warned.add(name)
        print(f"sessionphone: {ALIAS}{name} is deprecated; rename it DC_{name} "
              "(the old name is read until a later release)", file=sys.stderr)
    return value


def env(name, default=None):
    """DC_<name>, else the deprecated PIP_CHAT_<name> (warned once), else
    default. Empty counts as unset."""
    return _get(os.environ, name) or default


def _get(e, name):
    """DC_<name>, else the deprecated PIP_CHAT_<name> (warned once), from the
    mapping e. Empty counts as unset."""
    if e.get(f"DC_{name}"):
        return e[f"DC_{name}"]
    if e.get(f"{ALIAS}{name}"):
        return _alias(name, e[f"{ALIAS}{name}"])
    return None


# The agent: DC_AGENT, else the seat Claude Code was started as, else "claude".
AGENT = (env("AGENT") or os.environ.get("CLAUDE_CODE_AGENT") or "claude").strip().lower()
AGENT_NAME = env("AGENT_NAME") or AGENT.capitalize()
# This machine's name. It's used only to mint Message-IDs and the default mail
# addresses below. It's not the Delta Chat relay: that lives in the account.
HOST = env("HOST") or socket.getfqdn() or "localhost"
ADDR = env("ADDR") or f"{AGENT}-chat@{HOST}"      # the bridge's own mail address
MAILTO = env("MAILTO") or f"{AGENT}@{HOST}"       # the agent's box: where text in lands


def host_problem(environ=None, fqdn=None):
    """-> None, or why the bot must refuse to start: with no DC_HOST, this
    machine's own name is not a mail domain, and an address is built from it.

    On macOS getfqdn() can be a reverse-DNS name (1.0.0...ip6.arpa), and every
    incoming chat was mailed to <agent>@<that> while the bot looked healthy
    (measured live on a user's Mac in a cutover). A host is junk when it
    is empty, a .arpa name (ip6.arpa, in-addr.arpa), localhost, or has no dot.
    A DC_HOST the operator named is trusted (DC_HOST=localhost included), and
    with both DC_ADDR and DC_MAILTO named, HOST only mints Message-IDs: no
    address is built from it, so nothing is refused.

    environ is the environment the bot will run with (os.environ, or the
    plist's at install); fqdn stands in for socket.getfqdn() in tests.

    SEAM, not a fork: the seat's mail domain belongs to sessionmail. When it
    publishes one, this adopts it as the fallback before getfqdn().
    """
    e = os.environ if environ is None else environ

    def get(name):
        return _get(e, name)

    if get("HOST") or (get("ADDR") and get("MAILTO")):
        return None
    host = (socket.getfqdn() if fqdn is None else fqdn).strip().lower().rstrip(".")
    if not host:
        what = "is empty"
    elif host == "arpa" or host.endswith(".arpa"):
        what = f"is {host!r}, a reverse-DNS name, not a mail domain"
    elif host == "localhost" or host.startswith("localhost.") or "." not in host:
        what = f"is {host!r}, which has no mail domain"
    else:
        return None
    agent = (get("AGENT") or e.get("CLAUDE_CODE_AGENT") or "claude").strip().lower()
    return (f"no DC_HOST is set and this machine's name {what}, so mail to the agent "
            f"would go to {agent}@{host or 'localhost'}, which nobody reads. Set DC_HOST to "
            f"this machine's mail domain (DC_HOST=mac.example.net) and run "
            f"`sessionphone bot install` again, or name both DC_ADDR and DC_MAILTO.")


HOST_PROBLEM = host_problem()   # refused in bot main(), never a crash on import


def callers_file(environ=None):
    """The approved-callers file a bot run with `environ` reads: DC_CALLERS_FILE,
    else <DC_ROOT>/callers.txt, else <SESSIONPHONE_ROOT, else ~/.sessionphone>/callers.txt.
    The same answer as path("CALLERS_FILE", "callers.txt"), for any environment
    (the plist's, at install)."""
    e = os.environ if environ is None else environ
    v = _get(e, "CALLERS_FILE")
    if v:
        return pathlib.Path(v).expanduser()
    r = _get(e, "ROOT") or e.get("SESSIONPHONE_ROOT")
    return (pathlib.Path(r).expanduser() if r else paths.root()) / "callers.txt"


def channels_file(environ=None):
    """The allow-list a bot run with `environ` reads:
    DC_CHANNELS_FILE, else <DC_ROOT>/channels.txt, else <SESSIONPHONE_ROOT, else
    ~/.sessionphone>/channels.txt. path("CHANNELS_FILE", "channels.txt"), but read
    from `environ` now (the plist's, at install), not from ROOT frozen at import."""
    e = os.environ if environ is None else environ
    v = _get(e, "CHANNELS_FILE")
    if v:
        return pathlib.Path(v).expanduser()
    r = _get(e, "ROOT") or e.get("SESSIONPHONE_ROOT")
    return (pathlib.Path(r).expanduser() if r else paths.root()) / "channels.txt"


def stt_url(environ=None):
    """The STT a bot run with `environ` posts to: DC_STT_URL, else STT_DEFAULT.
    STT_URL's answer, read from `environ` now (the plist's, at install: the
    hearing probe), not from os.environ frozen at import."""
    e = os.environ if environ is None else environ
    return _get(e, "STT_URL") or STT_DEFAULT


def stt_model(environ=None):
    """DC_STT_MODEL from `environ`, else None (the model then suits the URL's kind)."""
    e = os.environ if environ is None else environ
    return _get(e, "STT_MODEL")


# What --owner wants: the owner is a Delta Chat contact.
# The kit's operator setting is a MAIL address, so nothing seeds the owner from
# it, and every no-owner refusal says which address to give.
DELTA_ADDR = ("ADDR is the owner's Delta Chat address, as their Delta Chat profile "
              "shows it, not their mail address")


def callers_problem(environ=None):
    """-> None, or why NOBODY may ring a bot run with `environ`: calls are on and
    the callers file holds no address. Every call would be refused, and with
    no line here nothing would say so. DC_CALLERS counts for nothing: an env
    var never grants the right to ring (callers_ignored says so).

    `bot install` refuses on it; the bot logs it loudly at start (it still
    runs: chat works without callers). DC_CALLS=off is never rung, by design.
    The refusal names the two ways forward: link the phone, or --owner with
    the owner's Delta Chat address."""
    e = os.environ if environ is None else environ
    if (_get(e, "CALLS") or "on") == "off":
        return None
    # one parser, shared with `sessionphone callers`; account is heavy, so imported here
    from sessionphone.account import read_callers
    f = callers_file(e)
    try:
        if read_callers(f):
            return None
        what = "has no address in it" if f.exists() else "does not exist"
    except OSError as exc:
        what = f"cannot be read ({exc.strerror or exc})"
    # `link` writes only the default file; past a DC_CALLERS_FILE it would not help
    if f == paths.callers_path():
        first = ("run `sessionphone link` and scan the invite from the owner's phone "
                 "(the first to join becomes the owner)")
    else:
        first = f"add the owner's Delta Chat address to {f}, one per line"
    return (f"NOBODY may ring this agent: {f} {what}, so every call would be refused. "
            f"Name the owner, one of two ways: {first}; or run "
            f"`sessionphone bot install --owner ADDR` (writes ADDR into {f}), where {DELTA_ADDR}.")


def callers_ignored(environ=None):
    """-> None, or the one line that says DC_CALLERS (or its deprecated alias) is
    set and IGNORED. Approved callers come from the callers file only: any
    process can set an env var, so one never grants the right to ring. The bot
    logs this at start; `bot install` refuses on it."""
    e = os.environ if environ is None else environ
    name = next((k for k in ("DC_CALLERS", f"{ALIAS}CALLERS")
                 if any(a.strip() for a in (e.get(k) or "").split(","))), None)
    if name is None:
        return None
    f = callers_file(e)
    how = ("with `sessionphone callers add ADDR`" if f == paths.callers_path()
           else f"to {f}, one per line")
    return f"{name} is set and IGNORED: approved callers come from {f} only; add each {how}."


ROOT = pathlib.Path(env("ROOT")).expanduser() if env("ROOT") else paths.root()
# The voice the bridge speaks in when nobody names one: a kokoro id (af_sky),
# or a clone dir under ~/.voicemode/voices.
VOICE = env("VOICE") or "af_sky"
GREETING = env("GREETING") or f"{AGENT_NAME} here. Go ahead."
GREETING_OUT = env("GREETING_OUT") or f"Hi, {AGENT_NAME} here, ringing you back."
SAY = env("SAY") or "dc-say"                      # the reply hint in bridged mail
# How an incoming call from an approved caller is answered:
#   auto - the bot answers at once and greets in the agent's voice. Cheap:
#          he says what he wants and it lands as mail, no agent turn needed.
#   ring - ring first, answer second. The bot mails the agent RINGING and
#          waits RING_WAIT seconds for `dc-answer CHAT_ID`. No answer (the
#          agent is busy, or not running) = it answers as voicemail.
ANSWER = (env("ANSWER") or "auto").strip().lower()
BAD = []   # settings that did not parse, said once in the bot's log (never a crash on import)


def number(name, default):
    """DC_<name> as a float; a value that is not one falls back to default, and says so."""
    v = env(name)
    try:
        return float(v) if v else default
    except ValueError:
        BAD.append(f"DC_{name}={v!r} is not a number; using {default:g}")
        return default


# Under the core's 120 s ring (RINGING_SECONDS), with room for ICE (~5 s).
RING_WAIT = number("RING_WAIT", 25.0)
# The no-reply watchdog: a call turn with no reply this many seconds
# after it was mailed hears HOLDING_LINE, once. Off by default (0); set
# DC_NO_REPLY_S above 0 to turn it on.
NO_REPLY_S = number("NO_REPLY_S", 0.0)
HOLDING_LINE = env("HOLDING_LINE") or "I heard you, give me a moment."
GREETING_VOICEMAIL = env("GREETING_VOICEMAIL") or (
    f"{AGENT_NAME} can't pick up right now. Leave a message and it reaches "
    f"{AGENT_NAME} as mail.")
# STT for voice notes, and for what a caller says: any OpenAI-compatible
# /v1/audio/transcriptions. The default is VoiceMode's mlx-audio on :8890, the
# server the bot's voice already uses. The bot requests Parakeet from it
# (stt.MODEL_DEFAULTS); that the server holds Parakeet is INFERRED, not
# measured. `ears serve` (Apple speech, macOS 27) is
# DC_STT_URL=http://127.0.0.1:2023/v1/audio/transcriptions.
STT_DEFAULT = "mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions"
STT_URL = env("STT_URL") or STT_DEFAULT
STT_MODEL = env("STT_MODEL")   # unset: the model suits the URL's kind (bridge/stt.py)
# aioice's STUN retry. Its own 0.5 s never fits a public relay's round trip,
# and calls dropped at 33 s. 2.0 s is proven end to end.
STUN_RTO = float(env("STUN_RTO") or "2.0")
# The heard log streamed live into a webxdc app (bridge/xdc_feed.py).
# XDC_FEED_CHATS is a consent of its own, apart from DC_HEARD_CHATS: that one
# lets a call in a chat WRITE the heard log; this one lets the heard log be READ
# OUT into a chat, to every member's open app. Comma-separated chat ids, unset
# by default, which means no feed can bind. List only your own 1:1 chat.
XDC_FEED_CHATS = env("XDC_FEED_CHATS") or ""
XDC_FEEDS_MAX = max(0, int(number("XDC_FEEDS_MAX", 2)))     # feeds at once


def path(name, *rel):
    """DC_<name> (or its deprecated alias) as a path, else ROOT/<rel...>."""
    v = env(name)
    return pathlib.Path(v).expanduser() if v else ROOT.joinpath(*rel)



def say_cmd(profile=None):
    """dc-say as a reader can run it. A second profile's bot is named by its
    outbox, DC_OUTBOX=..., never `--profile`: an older dc-say has no --profile
    and crashes on it with ValueError."""
    profile = env("PROFILE", "") if profile is None else profile
    return f"DC_OUTBOX={path('OUTBOX', 'outbox')} {SAY}" if profile else SAY


def reply_hint(chat_id, call=None, profile=None, channel=None):
    """How to answer a bridged mail. On a call the mouth leads and dc-say is
    only the fallback, for when the mouth has a problem. call is the call's
    id, the invite message's id.
    channel: the channel NAME the chat is listed as, so the hint names it, as
    the agent would; the chat id otherwise."""
    say = say_cmd(profile)
    if call:
        return (f'Answer with: mouth say --device call:{call} "text"\n'
                f"If the mouth fails: {say} --call {call} {chat_id} 'text'")
    return f"{say} {channel or chat_id} 'your reply'"


if __name__ == "__main__":
    for k in ("AGENT", "AGENT_NAME", "HOST", "ADDR", "MAILTO", "ROOT", "VOICE", "GREETING",
              "ANSWER", "RING_WAIT", "NO_REPLY_S", "GREETING_VOICEMAIL", "STT_URL", "STUN_RTO",
              "XDC_FEED_CHATS", "XDC_FEEDS_MAX"):
        print(f"{k:12} {globals()[k]}")
    if HOST_PROBLEM:
        print(f"{'REFUSED':12} {HOST_PROBLEM}")
    for k, rel in (("ACCOUNTS", ("accounts",)), ("OUTBOX", ("outbox",)), ("CALLS_DIR", ("calls",)),
                   ("LIVE", ("call-live.json",)), ("ACKS", ("acks",)), ("LOG", ("bot.log",)),
                   ("CALLERS_FILE", ("callers.txt",)), ("UNDELIVERED", ("undelivered",))):
        print(f"{k:12} {path(k, *rel)}")

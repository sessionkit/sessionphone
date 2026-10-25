"""seatmail - the bot's mail to its agent, by sessionmail's `message send`, signed.

Every agent-bound mail without a bot-minted Message-ID goes out by

    message send TO -s SUBJECT -f - --raw --no-shorten --json [--header ...]

as the bot's OWN seat, `<agent>-chat` (the local part of DC_ADDR), signed by
that seat's key. Whether it signs is ONE rule, signing(), judged from the
carrier this bot's own `message send` will use, as its sessionmail resolves
it: signed on an MTA, or with a signer given; unsigned on the mail drop with
no signer, where the bot writes into the box itself; and no start at all on a
carrier sessionmail refuses or that cannot be known. It is the only place the
mode is decided, and there is no flag. The seat is sessionmail's non-session
seat:
`message seat init` mints its key, `message seat agent` runs that key in an
ssh-agent under launchd, and these settings point the send at it:

    bot setting     the child `message send` gets
    DC_SIGNER_SOCK  CLAUDE_SESSION_AGENT_SOCK (the caller's own is stripped)
    DC_SIGNER_PUB   CLAUDE_SESSION_PUB, a copy of the .pub kept apart from the key
    DC_ADDR         TM_AGENT_ADDRESS and EMAIL = its local part, so an EMAIL
                    inherited from whoever ran install never becomes the From
    DC_AGENT_NAME   SESSIONMAIL_FROM_NAME = "<Name>'s Delta Chat bridge"
    (signing())     SESSIONMAIL_REQUIRE_SIGNATURE=1 unless the mode is unsigned;
                    the caller's value is always dropped
    DC_MESSAGE      the `message` binary, resolved to an absolute path at start

Every CLAUDE_SESSION_* the bot inherited is stripped, so the send can never
borrow a Claude session's key or identity. With must-sign on, a send that
cannot sign is refused by `message send` (exit 2, kind "identity") and
nothing is sent. Unsigned, the send carries no key and `message send` says
on stderr that it sends unsigned.

A send that fails is kept, not lost: bot.mail_in hands it to bridge/spool.py,
which retries it by its refusal's KIND. Two kinds are the bot's own, not
message send's: "exec" (the binary could not be run, say mid-upgrade) and
"timeout" (it ran past TIMEOUT_S, so the mail may have gone: sent "unknown").
Both are retried.

THE CALL MAILS (M5) go here too. Each carries or threads on a Message-ID the
bot mints itself, and send() hands it over as `--message-id`, `-r` and
`--supersedes`, never as a raw header (those are message send's own). A failed
M5 is spooled like any other mail, except a call's preview, which
the turn supersedes. sessionmail has no duplicate check, so before the spool
retries a mail under its `--message-id`, delivered() reads the target box for
that id (read-only) and never sends it twice. Its ids are minted at id_host(), sessionmail's own host,
and child_env pins SESSIONMAIL_HOST to the same value, so the two cannot
disagree. A web call's turns (web/turns.py) send as the web seat with `seat=`
and `from_name=`, under the same DC_SIGNER_* key.

A signer given (DC_SIGNER_*, set or saved) is always handed to the send, on
any carrier, and makes the mode signed: a bot keyed on purpose keeps signing.

The start checks guard it (check()): the carrier must be one sessionmail
sends by (signing() not REFUSED); when signed, the key's seat, from
DC_SIGNER_PUB's comment, must be the seat the mail goes From; and the
installed `message` must know --message-id (the probe). Any one failing
refuses the start: there is no raw sendmail to fall back to, and none is
wanted.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from sessionphone import settings
from sessionphone.bridge import dc_env

TIMEOUT_S = 30
# The bot's setting -> the variable `message send` signs with (sm _sender.py keyed/sign).
KEY_VARS = {"SIGNER_SOCK": "CLAUDE_SESSION_AGENT_SOCK", "SIGNER_PUB": "CLAUDE_SESSION_PUB"}
# Both spellings sessionmail reads (sm _sender.setting): the caller's value is dropped.
MUST_SIGN_VARS = ("SESSIONMAIL_REQUIRE_SIGNATURE", "SESSION_MAIL_REQUIRE_SIGNATURE")
# The seat variables (sm _seat.py SEAT_VARS / ADDRESS_VARS): both are the bot's seat.
SEAT_VARS = ("TM_AGENT_ADDRESS", "EMAIL")
STRIPPED_PREFIX = "CLAUDE_SESSION_"

# sessionmail's own host settings (sm _sender.setting("HOST")): both spellings.
HOST_VARS = dc_env.MAIL_HOST_VARS
# The refusal kinds that say something about the WORLD, not the mail: a retry
# with the same mail may go (web/turns.py). Every other kind is the input's
# (usage, subject, address, thread-id, body, header, message-id): it fails again.
WORLD_KINDS = ("exec", "identity", "mta", "undeliverable")
# The comment `message seat init` gives the seat's key: `<address> seat`.
KEY_COMMENT_TAG = "seat"

MESSAGE: str | None = None   # the binary, resolved once by setup() at start


def resolve_message(environ: Mapping[str, str] | None = None) -> str | None:
    """DC_MESSAGE, else `message` on PATH, as an ABSOLUTE path; None if neither
    is an executable. launchd's PATH is short, so the plist should name it."""
    e = os.environ if environ is None else environ
    want = os.path.expanduser(dc_env._get(e, "MESSAGE") or "message")
    found = shutil.which(want, path=e.get("PATH", os.defpath))
    return os.path.abspath(found) if found else None


def seat() -> str:
    """The bot's own seat: the local part of DC_ADDR, `<agent>-chat`."""
    return dc_env.ADDR.split("@", 1)[0]


_bot_seat = seat      # the same function, for the functions whose `seat=` argument hides it


def local_part(address: str) -> str:
    """`<agent>-chat@host` and `<agent>-chat` are both `<agent>-chat`."""
    return address.split("@", 1)[0]


def id_host(environ: Mapping[str, str] | None = None) -> str:
    """The host every Message-ID the bot mints is built on: sessionmail's own
    (sm _sender.host_of), in its order. $SESSIONMAIL_HOST, else
    $SESSION_MAIL_HOST, else the `host` sessionmail keeps in its mail.json
    (asked of it), else this machine's short name. Empty counts as unset, as
    there.

    It is dc_env.mail_host, so the two cannot drift. It is NOT dc_env.HOST:
    `message send --message-id` refuses any other host, and a DC_HOST still
    builds the default addresses and the web Reply-To."""
    return dc_env.mail_host(environ)


def key_seat(pub: str | os.PathLike) -> tuple[str | None, str]:
    """-> (the seat the key at PUB was minted for, '') or (None, why not).
    The seat is in the key's comment, `<address> seat`, as `message seat init`
    writes it. Any other form is no answer: the check fails closed."""
    path = pathlib.Path(pub).expanduser()
    try:
        words = path.read_text().split()
    except (OSError, UnicodeDecodeError) as e:
        return None, f"DC_SIGNER_PUB={pub} cannot be read: {type(e).__name__}: {e}"
    comment = " ".join(words[2:])
    if len(words) == 4 and words[3] == KEY_COMMENT_TAG and local_part(words[2]):
        return words[2], ""
    return None, (f"DC_SIGNER_PUB={pub} has the comment {comment!r}, not '<address> seat' as "
                  f"`message seat init` writes it, so the key's seat cannot be read")


def check(environ: Mapping[str, str] | None = None, *, seat: str | None = None,
          door: str = "DC_ADDR") -> str | None:
    """-> None, or why the bot refuses to start (exit 2, as check_root): with
    no `message` binary, a carrier sessionmail refuses or that cannot be known,
    or no signer where signing() rules signed, every mail to the agent would be
    refused, so a loud refusal now beats a quiet run that bridges nothing.

    Two doors ask it: the bot (seat None: DC_ADDR's local part) and
    `sessionphone web --legs` (seat: the web seat, door names where it came
    from). Past the binary and the signer it checks the KEY'S SEAT against
    that seat (a mail signed by one seat and From another is never sent), and
    then PROBES the binary for --message-id: see probe().

    The signer's SOCKET need not be live yet: a dead agent is a refusal of
    kind "identity" at send time, which heals when the agent comes back."""
    e = os.environ if environ is None else environ
    if resolve_message(e) is None:
        named = dc_env._get(e, "MESSAGE")
        what = (f"DC_MESSAGE={named} is not an executable" if named else
                "no `message` on PATH and DC_MESSAGE is not set")
        return (f"{what}, so no mail can reach the agent. Set DC_MESSAGE to sessionmail's "
                f"`message` (an absolute path: launchd's PATH is short) and run "
                f"`sessionphone bot install` again.")
    unset = [f"DC_{k}" for k in KEY_VARS if not dc_env._get(e, k)]
    pub = dc_env._get(e, "SIGNER_PUB")
    mine = local_part(seat if seat is not None else _bot_seat())
    mode, reason = signing(e)
    if mode == REFUSED:
        return (f"no mail can reach the agent: {reason}. Fix sessionmail's carrier (`message "
                f"setup` says how), then run `sessionphone bot install` again.")
    if unset and mode == UNSIGNED:
        if len(unset) == len(KEY_VARS):
            return probe(e, seat=mine)      # unsigned: no signer wanted, and none half-given
        given = next(f"DC_{k}" for k in KEY_VARS if dc_env._get(e, k))
        return (f"half a signer: {given} is set and {unset[0]} is not. Set both, to sign "
                f"every mail, or neither ({mode}: {reason}), then run `sessionphone bot "
                f"install` again.")
    if unset:
        why = f"{' and '.join(unset)} {'is' if len(unset) == 1 else 'are'} not set"
    elif not pathlib.Path(pub).expanduser().is_file():
        why = f"DC_SIGNER_PUB={pub} is not a file"
    else:
        return check_key_seat(pub, mine, door) or probe(e, seat=mine)
    return (f"no signer: {why}, and the bot never sends its agent unsigned mail here "
            f"({mode}: {reason}). Give the bot a key with `sessionphone bot install "
            f"--registry DIR` (DIR: a seat registry; the README's \"The bot's seat\" says how "
            f"to make one). Or by hand: mint the seat with `message seat init --registry DIR "
            f"{mine}`, run its agent with "
            f"`message seat agent {mine} SOCKET` under launchd, then set DC_SIGNER_SOCK to "
            f"that SOCKET and DC_SIGNER_PUB to a copy of the seat's .pub kept apart from "
            f"the key, and run `sessionphone bot install` again.")


def check_key_seat(pub: str, mine: str, door: str = "DC_ADDR") -> str | None:
    """-> None, or why a mail From the seat MINE may not be signed by the key at
    PUB. `message send` signs a mismatched seat without complaint, and the
    receiver's trust check then sees a key that is not the From's: so the check
    is ours. Local parts are compared, so `<agent>-chat@host seat` matches
    `<agent>-chat`."""
    keyed, why = key_seat(pub)
    if keyed is None:
        return f"{why}. Point DC_SIGNER_PUB at a copy of the .pub `message seat init` made for {mine}."
    if local_part(keyed) != mine:
        return (f"the signing key is seat {local_part(keyed)} (DC_SIGNER_PUB={pub}), but the mail "
                f"goes From seat {mine} ({door}), and a mail signed by one seat and From another is "
                f"never sent. Make them one seat: point DC_SIGNER_* at {mine}'s key, or make the "
                f"From seat {local_part(keyed)} ({door}).")
    return None


# >>> the start probe
# `message send --message-id` is a newer sessionmail's; an older `message` has
# no such flag.
# This probe SENDS NOTHING: `<x>` has no `@`, so message send's validate()
# refuses it as kind "message-id" before any host check, signing or MTA.
# THE BAD ID MUST STAY THE ONLY INVALID INPUT HERE. probe@invalid, the subject
# and --no-body are all valid as they stand; if any of them became a refusal
# too, a healthy sessionmail would answer some other kind and every start
# would refuse (tests/test_seatmail.py pins the exact argv and the answer).
PROBE_ARGS = ["send", "probe@invalid", "-s", "probe", "--no-body", "--message-id", "<x>", "--json"]
PROBE_TIMEOUT_S = 15
NEEDS = ("needs sessionmail with `message send --message-id` (delivery 15f6230 or "
         "later); the bot never falls back to raw sendmail")


def probe(environ: Mapping[str, str] | None = None, *, seat: str | None = None) -> str | None:
    """-> None when the `message` binary knows --message-id, else why not.
    Accepted: exit 2 with --json's {"sent": "no", "kind": "message-id"}, and only
    that. An old `message` (click's `No such option`, no JSON), any other kind,
    a send, a timeout or no binary at all is a refusal naming the binary."""
    e = os.environ if environ is None else environ
    exe = resolve_message(e)
    if exe is None:
        return f"no `message` binary to probe: {NEEDS}"
    try:
        r = subprocess.run([exe, *PROBE_ARGS], input="", text=True, capture_output=True,
                           timeout=PROBE_TIMEOUT_S, check=False, env=child_env(e, seat=seat))
    except subprocess.TimeoutExpired:
        return f"{exe} did not answer the --message-id probe in {PROBE_TIMEOUT_S}s: {NEEDS}"
    except OSError as err:
        return f"{exe} could not be run for the --message-id probe: {type(err).__name__}: {err}: {NEEDS}"
    obj = _answer(r.stdout or "")
    if r.returncode == 2 and obj.get("kind") == "message-id" and obj.get("sent") == "no":
        return None
    said = " ".join(str(obj.get("message") or r.stderr or r.stdout or "").split())[:200]
    got = f"exit {r.returncode}" + (f", kind {obj.get('kind')}" if obj.get("kind") else "") + (
        f", sent {obj.get('sent')}" if obj.get("sent") else "")
    return f"{exe} answered the --message-id probe with {got} ({said}): {NEEDS}"
# <<< the start probe


# sessionmail's carrier, NAMED, never chosen here and never run. The variable
# is read here (sessionmail never reads its file when it is set): _drop.selected
# is SESSIONMAIL_SENDMAIL, stripped, == "drop"; else _sender sends by it. With
# it unset, the carrier is asked of the sessionmail itself (settings.mail_file:
# its mail.json `sendmail`, else its own default), never mirrored. DEFAULT_MTA is
# only for a sessionmail that cannot be asked (it reads the environment only),
# and it is words for a log line: nothing here execs it
# (tests/test_seatmail.py, test_zero_raw_sendmail_is_left_in_src).
CARRIER_VAR = "SESSIONMAIL_SENDMAIL"
CARRIER_KEY = settings.MAIL_KEYS[CARRIER_VAR]
DROP = "drop"
DEFAULT_MTA = "/usr/sbin/sendmail"
ASK = object()      # carrier()'s probe when none is given: ask the job's sessionmail now
# carrier_said's kinds: how the words read after "the bot's mail ..."
BY, NOT_KNOWN, NOWHERE = "by", "not known", "nowhere"


def carrier_said(environ: Mapping[str, str] | None = None, probe=ASK) -> tuple[str, str]:
    """-> (kind, words): the mail carrier `message send` will use with this
    environment, and where it came from, in sessionmail's order:

    1. this environment's SESSIONMAIL_SENDMAIL: `drop (SESSIONMAIL_SENDMAIL=drop,
       no MTA)`, or `MTA <path> (SESSIONMAIL_SENDMAIL)`;
    2. what the sessionmail itself reads with it unset (PROBE, settings.mail_file's
       answer; asked of DC_MESSAGE, else `message` on PATH, when not given):
       `drop (...)` or `MTA <path> (...)`, from its mail.json `sendmail`, its
       installed default (mail.template.json) or its own default
       (settings.mail_from names which); kind NOT_KNOWN when it could not read that file, and NOWHERE
       when the file names no carrier (sessionmail refuses to send, both ways);
    3. with PROBE not given and no answer from it, sessionmail's own resolver
       (settings.mail_carrier, the answer signing() decides by; _said_of);
    4. a sessionmail that cannot be asked either way reads the environment
       only: `MTA <DEFAULT_MTA> (SESSIONMAIL_SENDMAIL unset)`. Log words only:
       signing() never reads them, and refuses to start there.

    It says; it decides nothing, and runs nothing but those read-only questions."""
    e = os.environ if environ is None else environ
    v = e.get(CARRIER_VAR) or ""
    if v.strip() == DROP:
        return BY, f"{DROP} ({CARRIER_VAR}={DROP}, no MTA)"
    if v:
        return BY, f"MTA {v} ({CARRIER_VAR})"
    asked = None
    if probe is ASK:
        probe = settings.mail_file(resolve_message(e))
        if (probe or {}).get(CARRIER_KEY) is None:
            asked = settings.mail_carrier(resolve_message(e))
    filed = (probe or {}).get(CARRIER_KEY)
    if filed is None and asked is not None:
        return _said_of(asked)
    if filed is None:
        return BY, f"MTA {DEFAULT_MTA} ({CARRIER_VAR} unset)"
    value, source = filed
    mail_json = settings.shown(settings.MAIL_FILE)
    if source == settings.UNREADABLE:
        return NOT_KNOWN, (f"not known: sessionmail could not read {mail_json}, and sends "
                           "nothing until it can")
    if not (value == DROP or (isinstance(value, str) and value.startswith("/"))):
        filed_in = (settings.shown(settings.MAIL_TEMPLATE) if source == settings.TEMPLATE_SOURCE
                    else mail_json)
        return NOWHERE, (f"nowhere: {filed_in} `{CARRIER_KEY}` is {json.dumps(value)}, which is "
                         f"not a carrier (`{DROP}`, or the absolute path of an MTA), so "
                         "sessionmail refuses to send")
    where = _from(source)
    return BY, (f"{DROP} ({where})" if value == DROP else f"MTA {value} ({where})")


# >>> the signing rule
# THE ONE PLACE the signing mode is decided. Everything that signs or demands a
# signer asks it: child_env (every send: seatmail.send, the web call's turns,
# the spool's retries), check() (the bot's and the web seat's start checks, and
# so `bot install`), seat.plan()/run_agent() (the `seat:` line), the bot's
# start line and the web's (turns.start_lines). Its inputs are the carrier this bot's own `message send` will
# use, as its sessionmail resolves it (settings.mail_carrier: sessionmail's own
# resolver, asked with the job's SESSIONMAIL_SENDMAIL), and a signer given.
# There is no flag (the caller's SESSIONMAIL_REQUIRE_SIGNATURE is dropped by
# child_env either way). carrier_said's fallback MTA, for a sessionmail that
# cannot be asked, is log words only: it is never an input here.
SIGNED, UNSIGNED, REFUSED = "signed", "unsigned", "refused"
MODES = (SIGNED, UNSIGNED, REFUSED)
WILL_USE = "the carrier this bot's own `message send` will use"


def _from(source: object) -> str:
    """Where sessionmail's resolver found the carrier, in words: its mail.json
    only when the key is set there (`host`); its installed template is its
    default (settings.mail_from). One rule for the seat:, mail: and start lines."""
    if source == "variable":
        return CARRIER_VAR
    return settings.mail_from(CARRIER_KEY, source)


def signing(environ: Mapping[str, str] | None = None, asked=ASK) -> tuple[str, str]:
    """-> (mode, reason), the reason in words that read inside "signed (...)".

    ASKED is the carrier this bot's own `message send` will use, as its
    sessionmail resolves it (settings.mail_carrier's answer: kind drop, mta or
    refused), or None when it could not be asked; asked of ENVIRON's
    `message`, with ENVIRON's SESSIONMAIL_SENDMAIL, when not given.

    - SIGNED on an MTA carrier: a mail system stands between the bot and the
      box, so another account or host can land a forged From there. Must-sign:
      a send with no usable key is refused.
    - SIGNED when a signer is given (both DC_SIGNER_*, set or saved), on any
      carrier, drop included: a bot keyed on purpose keeps signing.
    - UNSIGNED on the drop carrier with no signer: the bot writes each mail
      into the box itself, as the box's owner, so a signature proves nothing
      the file's owner does not.
    - REFUSED when sessionmail refuses its carrier (a value that is not one, a
      file it cannot read), names one of no known kind, or cannot be asked:
      never unsigned and never a guessed MTA. The bot refuses to start."""
    e = os.environ if environ is None else environ
    if asked is ASK:
        asked = settings.mail_carrier(resolve_message(e), e.get(CARRIER_VAR))
    if asked is None:
        return REFUSED, (f"{WILL_USE} is not known: its sessionmail could not be asked "
                         "(`message` is not a sessionmail script with a carrier resolver)")
    kind, src = asked.get("kind"), _from(asked.get("source"))
    if kind == "refused":
        why = " ".join(str(asked.get("why") or "").split()) or "no reason given"
        return REFUSED, f"sessionmail refuses {WILL_USE}: {why}"
    if kind == "mta":
        return SIGNED, f"carrier {asked.get('path')} is an MTA, from {src}"
    if kind != DROP:
        return REFUSED, f"sessionmail names {WILL_USE} {json.dumps(kind)}, a kind this bot does not know"
    if all(dc_env._get(e, k) for k in KEY_VARS):
        return SIGNED, f"a signer is given in DC_SIGNER_SOCK and DC_SIGNER_PUB, on carrier {DROP}"
    return UNSIGNED, f"carrier {DROP}: this bot writes into the box itself, from {src}"
# <<< the signing rule


def _said_of(asked: Mapping[str, object]) -> tuple[str, str]:
    """carrier_said's (kind, words) for sessionmail's own resolver's ANSWER."""
    kind, src = asked.get("kind"), _from(asked.get("source"))
    if kind == DROP:
        return BY, f"{DROP} ({src})"
    if kind == "mta":
        return BY, f"MTA {asked.get('path')} ({src})"
    why = " ".join(str(asked.get("why") or "").split()) or f"kind {json.dumps(kind)}"
    if asked.get("source") == settings.UNREADABLE:
        return NOT_KNOWN, f"not known: {why}; sessionmail sends nothing until it can read it"
    return NOWHERE, f"nowhere: {why}, so sessionmail refuses to send"


def carrier(environ: Mapping[str, str] | None = None, probe=ASK) -> str:
    """The mail carrier `message send` will use with this environment, in words
    (carrier_said). For the start line and `bot install`, so a deployed bot
    can be checked without reading its plist."""
    return carrier_said(environ, probe)[1]


def setup(environ: Mapping[str, str] | None = None) -> str | None:
    """At start: resolve the binary once (-> it, or None)."""
    global MESSAGE
    MESSAGE = resolve_message(environ)
    return MESSAGE


def child_env(environ: Mapping[str, str] | None = None, *, seat: str | None = None,
              from_name: str | None = None) -> dict[str, str]:
    """The environment `message send` runs with.
    seat and from_name default to the bot's own (a web call's turns name theirs).
    A signer given is always handed over; must-sign is set by signing(), and the
    caller's own must-sign value is always dropped.
    SESSIONMAIL_HOST is pinned to id_host(), the host our Message-IDs are minted
    on, so sessionmail's own-host check reads the very same value."""
    e = os.environ if environ is None else environ
    env = {k: v for k, v in e.items()
           if not k.startswith(STRIPPED_PREFIX) and k not in MUST_SIGN_VARS}
    for setting, var in KEY_VARS.items():
        v = dc_env._get(e, setting)
        if v:
            env[var] = os.path.expanduser(v)
    who = local_part(seat) if seat is not None else _bot_seat()
    for var in SEAT_VARS:
        env[var] = who
    env["SESSIONMAIL_FROM_NAME"] = from_name or f"{dc_env.AGENT_NAME}'s Delta Chat bridge"
    if signing(e)[0] != UNSIGNED:    # the one rule; the caller's own value was dropped above
        env[MUST_SIGN_VARS[0]] = "1"
    env[HOST_VARS[0]] = id_host(e)
    return env


def clean_subject(subject: str) -> str:
    """What `message send` would refuse as able to execute, defused: a backtick
    becomes ' and `$(` becomes `$ (` (sm message.py, the subject guard)."""
    return subject.replace("`", "'").replace("$(", "$ (")


@dataclass
class Sent:
    """What became of one send. ok: the MTA accepted it. id: the Message-ID it
    went under. kind/sent: its refusal's, under --json. args: the send's argv
    after the binary, so a failed one can be kept and retried."""
    ok: bool
    error: str = ""
    id: str | None = None
    kind: str | None = None
    sent: str = "no"
    args: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)    # sessionmail's own lines, under --json
    dropped: list[tuple[str, str]] = field(default_factory=list)   # (Cc left off, why): guard_cc


def bare(msgid: str) -> str:
    """An id as message send takes it, one pair of <> or none: `<a@b>` -> `a@b`."""
    s = msgid.strip()
    return s[1:-1] if s.startswith("<") and s.endswith(">") else s


def args(to: str, subject: str, *, cc=(), headers: Mapping[str, str] | None = None,
         msgid: str | None = None, in_reply_to: str | None = None,
         supersedes: str | None = None) -> list[str]:
    """`message send`'s arguments, the binary not included (it is resolved at start).
    The threading ids go by message send's OWN flags: Message-ID, In-Reply-To,
    References, Cc and Supersedes are refused as --header (sm _send.OWNED).
    `-r` writes In-Reply-To and References both."""
    a = ["send", to, "-s", clean_subject(subject), "-f", "-", "--raw", "--no-shorten", "--json"]
    if msgid:
        a += ["--message-id", bare(msgid)]
    if in_reply_to:
        a += ["-r", f"<{bare(in_reply_to)}>"]
    if supersedes:
        a += ["--supersedes", f"<{bare(supersedes)}>"]
    for addr in cc or ():
        a += ["-c", addr]
    for k, v in (headers or {}).items():
        a += ["--header", f"{k}: {' '.join(str(v).split())}"]
    return a


def _answer(stdout: str) -> dict:
    """--json's ONE object (the last line that parses); {} if none does."""
    for line in reversed((stdout or "").splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def send(to: str, subject: str, body: str, *, cc=(), headers: Mapping[str, str] | None = None,
         msgid: str | None = None, in_reply_to: str | None = None, supersedes: str | None = None,
         seat: str | None = None, from_name: str | None = None,
         environ: Mapping[str, str] | None = None) -> Sent:
    """Send BODY to TO as the bot's seat (or SEAT, as FROM_NAME), signed as
    signing() rules (child_env).
    msgid / in_reply_to / supersedes: a call mail's (M5) bot-minted ids.
    -> Sent; never raises for a send."""
    kept, dropped = guard_cc(cc, environ)
    a = args(to, subject, cc=kept, headers=headers, msgid=msgid, in_reply_to=in_reply_to,
             supersedes=supersedes)
    sent = send_args(a, body, environ=environ, seat=seat, from_name=from_name)
    sent.dropped = dropped
    return sent


def send_args(a: list[str], body: str, *, environ: Mapping[str, str] | None = None,
              seat: str | None = None, from_name: str | None = None) -> Sent:
    """Run `message` with the arguments A (as args() made them) and BODY on stdin.
    The spool's retry comes in here: the env is rebuilt now, never stored."""
    a = list(a)
    exe = MESSAGE or resolve_message(environ)
    if exe is None:
        return Sent(False, "rc=-: no `message` binary (DC_MESSAGE)", kind="exec", args=a)
    try:
        r = subprocess.run([exe, *a], input=body, text=True, capture_output=True,
                           timeout=TIMEOUT_S, check=False,
                           env=child_env(environ, seat=seat, from_name=from_name))
    except subprocess.TimeoutExpired:
        return Sent(False, f"rc=- kind=timeout sent=unknown: no answer in {TIMEOUT_S}s",
                    kind="timeout", sent="unknown", args=a)
    except OSError as e:
        return Sent(False, f"rc=- kind=exec: {exe}: {type(e).__name__}: {e}", kind="exec", args=a)
    obj = _answer(getattr(r, "stdout", "") or "")
    notices = [str(n) for n in obj.get("notices") or () if isinstance(n, str)]
    if r.returncode == 0 and obj.get("sent", "yes") == "yes":
        return Sent(True, id=obj.get("id"), sent="yes", args=a, notices=notices)
    kind, sent = obj.get("kind"), str(obj.get("sent", "no"))
    said = " ".join(str(obj.get("message") or getattr(r, "stderr", "") or "").split())[:200]
    tag = f"rc={r.returncode}" + (f" kind={kind}" if kind else "") + (f" sent={sent}" if sent != "no" else "")
    return Sent(False, f"{tag}: {said}", kind=kind, sent=sent, args=a, notices=notices)


# >>> cc-guard
# Under the mail drop ONE recipient it cannot deliver to refuses the WHOLE mail
# (rc=3, kind undeliverable, "NOTHING WAS DELIVERED, to this recipient or any
# other"): a Cc'd guest with no box would cost the agent its own copy. So a Cc
# goes only to a box sessionmail says is there (`message path`, read-only),
# and, on the drop, only on this host (the drop delivers locally only). On an
# MTA an address off this host is the MTA's to route, and kept.
BOX_RECHECK_S = 60.0        # a box not there is asked again after this; one there is kept
_BOXES: dict[tuple[str, str], tuple[bool, float]] = {}


def forget_boxes() -> None:
    """Forget every box guard_cc has asked about: the next send asks again."""
    _BOXES.clear()


# WORKAROUND: sessionmail's `message path` (and `setup`) read only the
# legacy SESSION_MAIL_ROOT, while `message send` and the drop read
# SESSIONMAIL_ROOT first. Where the job carries SESSIONMAIL_ROOT, its box tools
# get SESSION_MAIL_ROOT with the SAME value, so the box check and the drop agree.
# Remove root_env, and its callers' use of it, once sessionmail's `path` and
# `setup` read SESSIONMAIL_ROOT too.
ROOT_VAR, LEGACY_ROOT_VAR = "SESSIONMAIL_ROOT", "SESSION_MAIL_ROOT"


def root_env(env: Mapping[str, str]) -> dict[str, str]:
    """ENV, with SESSION_MAIL_ROOT set to SESSIONMAIL_ROOT's value when that is
    set (stripped, not empty): the environment for `message path` / `setup`
    (the workaround above). ENV as it is otherwise."""
    out = dict(env)
    root = (out.get(ROOT_VAR) or "").strip()
    if root:
        out[LEGACY_ROOT_VAR] = root
    return out


def on_host(address: str, environ: Mapping[str, str] | None = None) -> bool:
    """Is ADDRESS's domain this host, as sessionmail's drop judges it: its mail
    host, or a name under it, and never a reverse-DNS name? The one judgement
    is dc_env.is_this_host; the domain's trailing dot is stripped first, so
    `x.arpa.` is still a reverse-DNS name. A bare address (no `@`: `claude`,
    `ada+web`) is on this host: sessionmail adds this host to it, on both
    carriers. `x@`, with an empty domain, is not."""
    e = os.environ if environ is None else environ
    if "@" not in address:
        return bool(address.strip())
    domain = address.rpartition("@")[2].strip().rstrip(".")
    return dc_env.is_this_host(domain, dc_env.mail_host(e) or "")


def on_drop(environ: Mapping[str, str] | None = None) -> bool:
    """Is the carrier this bot's own `message send` will use the mail drop, as
    its sessionmail resolves it? The drop delivers on this host only. Any other
    answer (an MTA, refused, unknown) is not the drop."""
    e = os.environ if environ is None else environ
    asked = settings.mail_carrier(resolve_message(e), e.get(CARRIER_VAR))
    return (asked or {}).get("kind") == DROP


def box_there(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Does sessionmail resolve the box NAME to a maildir (`message path
    NAME`, exit 0)? Asked in the send's own environment; a yes is kept for
    the run, a no asked again after BOX_RECHECK_S."""
    e = os.environ if environ is None else environ
    exe = MESSAGE or resolve_message(e)
    if exe is None:
        return False
    key, now = (exe, name), time.monotonic()
    had = _BOXES.get(key)
    if had is not None and (had[0] or now - had[1] < BOX_RECHECK_S):
        return had[0]
    try:
        r = subprocess.run([exe, "path", name], capture_output=True, text=True, timeout=TIMEOUT_S,
                           check=False, stdin=subprocess.DEVNULL, env=root_env(child_env(e)))
        there = r.returncode == 0 and bool((r.stdout or "").strip())
    except (OSError, subprocess.SubprocessError):
        there = False
    _BOXES[key] = (there, now)
    return there


def box_path(name: str, environ: Mapping[str, str] | None = None) -> pathlib.Path | None:
    """The maildir sessionmail resolves the box NAME to (`message path NAME`,
    exit 0 and a path on stdout), asked in the send's own environment, never
    kept; None when it does not resolve, or `message` cannot be run.
    READ-ONLY: `message path` prints the maildir and does nothing else."""
    e = os.environ if environ is None else environ
    exe = MESSAGE or resolve_message(e)
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, "path", name], capture_output=True, text=True, timeout=TIMEOUT_S,
                           check=False, stdin=subprocess.DEVNULL, env=root_env(child_env(e)))
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not out or not out[-1].strip():
        return None
    return pathlib.Path(out[-1].strip()).expanduser()


def _header_id(path: pathlib.Path) -> str | None:
    """The Message-ID in the header block of the mail file at PATH, bare (no
    <>); None if it has none or cannot be read. Reads the header block only,
    with folded lines joined, as sessionmail's own find_messages compares it."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    lines = head.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    found, grab = None, False
    for line in lines:
        if not line.strip():
            break                                   # the end of the header block
        if line[:1] in (" ", "\t"):
            if grab:
                found = (found or "") + line.strip()
            continue
        name, _, value = line.partition(":")
        grab = name.strip().lower() == "message-id"
        if grab:
            found = value.strip()
    return bare(found) if found else None


DELIVERED_SLACK_S = 300.0   # a file delivered this long before the record's first try still counts


def delivered(msgid: str, to: str, since: float | None = None,
              environ: Mapping[str, str] | None = None) -> bool | None:
    """Is a mail with Message-ID MSGID already in TO's box? True or False;
    None when the box cannot be checked from here: TO is on another host (a
    box of the same name here is not where it went; on_host, as guard_cc
    judges it), or `message path` fails for every name in
    cc_boxes(local_part(to)).

    READ-ONLY: `message path`, then each file's header block in new/ and cur/
    (never tmp/, where a delivery is still being written), the Message-ID
    compared with <> stripped. SINCE: files whose mtime is older than SINCE
    are skipped (a maildir file's mtime is its delivery time), so a big box
    is not read whole on every retry. sessionmail has no duplicate check at
    delivery: a send under the same --message-id lands twice, so the spool
    asks this before it retries a mail under an id the bot minted."""
    want = bare(msgid)
    if not want:
        return None
    if "@" in to and not on_host(to, environ):
        # a box on another host: a box of the same name HERE is not where the
        # mail went, so reading it could say "not there" of a mail that went
        return None
    name = local_part(to).strip().lower()
    box = None
    for n in cc_boxes(name):
        box = box_path(n, environ)
        if box is not None:
            break
    if box is None:
        return None
    for sub in ("new", "cur"):
        try:
            entries = list(os.scandir(box / sub))
        except OSError:
            continue                                # no new/ or cur/: nothing was delivered there
        for entry in entries:
            try:
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                if since is not None and entry.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            if _header_id(pathlib.Path(entry.path)) == want:
                return True
    return False


def cc_boxes(name: str) -> tuple[str, ...]:
    """The boxes the drop may deliver NAME's mail to, in its order: NAME, then
    for a plus address (`base+tag`) the base box. `message path` does not
    resolve a plus address (exit 2), while the drop delivers `base+tag@host`
    to the box `base` (measured on sessionmail): a fleet seat's address is a
    plus address, so asking NAME alone would leave every such guest off."""
    base = name.partition("+")[0]
    return (name, base) if base and base != name else (name,)


def guard_cc(cc, environ: Mapping[str, str] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """-> (the Cc to send, [(each one left off, why)]). On this host (a bare
    address is: on_host) a Cc needs a box sessionmail says is there (its own,
    or a plus address's base: cc_boxes), on any carrier; off this host it is
    left off on the mail drop, which delivers locally only, and kept on an MTA."""
    e = os.environ if environ is None else environ
    kept, dropped = [], []
    drop = None
    for addr in cc or ():
        if on_host(addr, e):
            name = local_part(addr).strip().lower()
            if any(box_there(n, e) for n in cc_boxes(name)):
                kept.append(addr)
            else:
                dropped.append((addr, f"sessionmail has no box `{name}` on this host"))
            continue
        if drop is None:
            drop = on_drop(e)
        if drop:
            dropped.append((addr, "it is not on this host, and the mail drop delivers locally only"))
        else:
            kept.append(addr)
    return kept, dropped
# <<< cc-guard

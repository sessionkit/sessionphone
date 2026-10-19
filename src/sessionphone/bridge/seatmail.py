"""seatmail - the bot's mail to its agent, by sessionmail's `message send`, signed.

Every agent-bound mail without a bot-minted Message-ID goes out by

    message send TO -s SUBJECT -f - --raw --no-shorten --json [--header ...]

as the bot's OWN seat, `<agent>-chat` (the local part of DC_ADDR), signed by
that seat's key, and NEVER unsigned. The seat is sessionmail's non-session seat:
`message seat init` mints its key, `message seat agent` runs that key in an
ssh-agent under launchd, and these settings point the send at it:

    bot setting     the child `message send` gets
    DC_SIGNER_SOCK  CLAUDE_SESSION_AGENT_SOCK (the caller's own is stripped)
    DC_SIGNER_PUB   CLAUDE_SESSION_PUB, a copy of the .pub kept apart from the key
    DC_ADDR         TM_AGENT_ADDRESS and EMAIL = its local part, so an EMAIL
                    inherited from whoever ran install never becomes the From
    DC_AGENT_NAME   SESSIONMAIL_FROM_NAME = "<Name>'s Delta Chat bridge"
    (always)        SESSIONMAIL_REQUIRE_SIGNATURE=1; the caller's value is dropped
    DC_MESSAGE      the `message` binary, resolved to an absolute path at start

Every CLAUDE_SESSION_* the bot inherited is stripped, so the send can never
borrow a Claude session's key or identity. With must-sign on, a send that
cannot sign is refused by `message send` (exit 2, kind "identity") and
nothing is sent.

A send that fails is kept, not lost: bot.mail_in hands it to bridge/spool.py,
which retries it by its refusal's KIND. Two kinds are the bot's own, not
message send's: "exec" (the binary could not be run, say mid-upgrade) and
"timeout" (it ran past TIMEOUT_S, so the mail may have gone: sent "unknown").
Both are retried.

THE CALL MAILS (M5) go here too. Each carries or threads on a Message-ID the
bot mints itself, and send() hands it over as `--message-id`, `-r` and
`--supersedes`, never as a raw header (those are message send's own). An M5 is
NEVER spooled: sessionmail has no duplicate check, so a retried M5 could land
twice under one id. Its ids are minted at id_host(), sessionmail's own host,
and child_env pins SESSIONMAIL_HOST to the same value, so the two cannot
disagree. A web call's turns (web/turns.py) send as the web seat with `seat=`
and `from_name=`, under the same DC_SIGNER_* key.

Two start checks guard it (check()): the key's seat, from DC_SIGNER_PUB's
comment, must be the seat the mail goes From; and the installed `message` must
know --message-id (the probe). Either one failing refuses the start: there is
no raw sendmail to fall back to, and none is wanted.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field

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
HOST_VARS = ("SESSIONMAIL_HOST", "SESSION_MAIL_HOST")
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
    (sm _sender.host_of), mirrored. $SESSIONMAIL_HOST, else $SESSION_MAIL_HOST,
    else this machine's short name. Empty counts as unset, as there.

    It is NOT dc_env.HOST: `message send --message-id` refuses any other host,
    and DC_HOST still builds the default addresses and the web Reply-To."""
    e = os.environ if environ is None else environ
    for var in HOST_VARS:
        if e.get(var):
            return e[var]
    return socket.gethostname().split(".")[0]


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
    no `message` binary or no signer, every mail to the agent would be refused,
    so a loud refusal now beats a quiet run that bridges nothing.

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
    if unset:
        why = f"{' and '.join(unset)} {'is' if len(unset) == 1 else 'are'} not set"
    elif not pathlib.Path(pub).expanduser().is_file():
        why = f"DC_SIGNER_PUB={pub} is not a file"
    else:
        return check_key_seat(pub, mine, door) or probe(e, seat=mine)
    return (f"no signer: {why}, and the bot never sends its agent unsigned mail. Mint the "
            f"seat with `message seat init --registry DIR {mine}`, run its agent with "
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


# sessionmail's carrier rule (sm 148dd21), mirrored to NAME it, never to choose
# it and never to run it: _drop.selected is SESSIONMAIL_SENDMAIL, stripped, ==
# "drop"; else _sender sends by SESSIONMAIL_SENDMAIL, else its default MTA.
# DEFAULT_MTA is words for a log line: nothing here execs it
# (tests/test_seatmail.py, test_zero_raw_sendmail_is_left_in_src).
CARRIER_VAR = "SESSIONMAIL_SENDMAIL"
DROP = "drop"
DEFAULT_MTA = "/usr/sbin/sendmail"


def carrier(environ: Mapping[str, str] | None = None) -> str:
    """The mail carrier `message send` will use with this environment, in words:
    `drop (SESSIONMAIL_SENDMAIL=drop, no MTA)`, or `MTA <path> (...)` with where
    the path came from. For the start line and `bot install`, so a deployed bot
    can be checked without reading its plist. It says; it decides nothing."""
    e = os.environ if environ is None else environ
    v = e.get(CARRIER_VAR) or ""
    if v.strip() == DROP:
        return f"{DROP} ({CARRIER_VAR}={DROP}, no MTA)"
    if v:
        return f"MTA {v} ({CARRIER_VAR})"
    return f"MTA {DEFAULT_MTA} ({CARRIER_VAR} unset)"


def setup(environ: Mapping[str, str] | None = None) -> str | None:
    """At start: resolve the binary once (-> it, or None)."""
    global MESSAGE
    MESSAGE = resolve_message(environ)
    return MESSAGE


def child_env(environ: Mapping[str, str] | None = None, *, seat: str | None = None,
              from_name: str | None = None) -> dict[str, str]:
    """The environment `message send` runs with.
    seat and from_name default to the bot's own (a web call's turns name theirs).
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
    """Send BODY to TO as the bot's seat (or SEAT, as FROM_NAME), signed.
    msgid / in_reply_to / supersedes: a call mail's (M5) bot-minted ids.
    -> Sent; never raises for a send."""
    a = args(to, subject, cc=cc, headers=headers, msgid=msgid, in_reply_to=in_reply_to,
             supersedes=supersedes)
    return send_args(a, body, environ=environ, seat=seat, from_name=from_name)


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
    if r.returncode == 0 and obj.get("sent", "yes") == "yes":
        return Sent(True, id=obj.get("id"), sent="yes", args=a)
    kind, sent = obj.get("kind"), str(obj.get("sent", "no"))
    said = " ".join(str(obj.get("message") or getattr(r, "stderr", "") or "").split())[:200]
    tag = f"rc={r.returncode}" + (f" kind={kind}" if kind else "") + (f" sent={sent}" if sent != "no" else "")
    return Sent(False, f"{tag}: {said}", kind=kind, sent=sent, args=a)

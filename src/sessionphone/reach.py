"""reach - what sessionmail says reaches the session a shell is in.

`message subscription list` (no BOX) prints, read only, a tree of what
reaches the calling session: line one is `<sid> (this session)` for a Claude
session, or `<seat> (seat)` when the shell names a seat and no session; under
it one address a row, the session's seat the row noted `your seat`, then the
addresses that feed each one; the last line, `-- N addresses ...`, says NO BOX
while the session has no mail box yet. With no session and no seat it exits 2,
saying so (NO_SESSION). The head names the session by its 8-hex SHORT id, and
the session's own box (its FULL id) is the tree's root, never a row under it:
parse keeps the head's id, and Reach.hears matches the own box against the
full id the same shell carries (SESSION_ID_VARS), never by prefix alone.

`message subscription list BOX` (the BOX form, read only, open to all) says who
hears one box; unowned() reads it, for `bot install`'s fallthrough line.

This module reads both texts; it is the one place that does, so a
machine-readable form from sessionmail (`subscription list --json`,
or `message hears`) replaces only parse() and unowned_answer().

Two readers ask it:

- `bot install` (and every reader that picks the bot the way install does,
  settings.shell_name) names the bot after the session's DURABLE seat: one a
  later session can be started as. A plus address is not durable (its alias
  row lives only while the session does), and neither is a session id.
- seat.hear_line says whether this session hears the bot's box, measured.

It imports nothing of sessionphone's: settings asks it while the bridge's
modules are still being imported.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

ARGS = ("subscription", "list")
NO_SESSION = "no session id and no seat"   # its exit 2 with neither
# The variables sessionmail reads a session's id from, first non-blank wins
# (sessiontools' SESSION_ID_VARS): the shell asked is the shell it answered for.
SESSION_ID_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
# The BOX form's answers that say, plainly, nobody hears the box: exit 2 with
# NO_SUCH_BOX (never made), or exit 0 ending on UNOWNED_FOOT. Any other answer
# is not read as unowned.
NO_SUCH_BOX = "no such box"
UNOWNED_FOOT = "-- 0 hear it; no live listener on it"
TIMEOUT_S = 15
_TREE = "│├└─ "


@dataclass(frozen=True)
class Reach:
    """What sessionmail says reaches the session a shell is in."""
    answered: bool                      # sessionmail gave an answer this module reads
    session: bool = False               # the shell is a Claude session
    seat: str = ""                      # the seat sessionmail resolves it to, lowercase
    addresses: frozenset[str] = frozenset()   # every address that reaches it, lowercase
    boxed: bool = True                  # the session has a mail box (False: not made yet)
    said: str = ""                      # sessionmail's words, when it gave no answer
    short: str = ""                     # the head's session id (8 hex), lowercase
    sid: str = ""                       # the full session id the asked shell carries, lowercase

    @property
    def durable(self) -> str:
        """The seat, when a later session can be started as it: never a plus
        address, never a session id, only a name a bot can carry. "" if none."""
        s = self.seat
        if not s or "+" in s or "@" in s or not name_ok(s) or session_id(s):
            return ""
        return s

    @property
    def own(self) -> str:
        """This session's own box, its full id: only when the shell asked
        carries a full session id and sessionmail's head names the same
        session (its 8 hex are the id's first 8). "" otherwise."""
        sid = self.sid
        if not (self.session and self.short and session_id(sid) and len(sid.replace("-", "")) == 32):
            return ""
        return sid if sid[:8] == self.short else ""

    def own_box(self, box: str) -> bool:
        """Is BOX this session's own session box (own)? Exact, never a prefix."""
        return bool(self.own) and box.strip().lower() == self.own

    def hears(self, box: str) -> bool:
        """Does mail to BOX reach this session? Only a session hears: an
        address sessionmail lists under it, or its own session box."""
        return self.session and (box.strip().lower() in self.addresses or self.own_box(box))


def name_ok(s: str) -> bool:
    """A name a bot can carry: in its launchd label and its seat `<name>-chat`."""
    return (bool(s) and s.isascii() and s[0].isalnum()
            and all(c.isalnum() or c in "-_." for c in s))


def session_id(s: str) -> bool:
    """A session id (a uuid, or its 8-hex short form): never a bot's name."""
    bare = s.replace("-", "")
    return len(bare) in (8, 32) and all(c in "0123456789abcdef" for c in bare.lower())


def shell_sid(shell: Mapping[str, str]) -> str:
    """The session id SHELL carries, as sessionmail reads it: the first
    non-blank of SESSION_ID_VARS, stripped, lowercase. "" when none."""
    for var in SESSION_ID_VARS:
        v = (shell.get(var) or "").strip()
        if v:
            return v.lower()
    return ""


def parse(rc: int, stdout: str, stderr: str, sid: str = "") -> Reach:
    """`message subscription list`'s exit and text -> Reach. Exit 2 saying
    NO_SESSION is an answer (no session, no seat); any other failure, or a
    first line of neither form, is no answer, with sessionmail's words.
    SID: the session id the shell asked carries (shell_sid), so the session's
    own box can be told (Reach.own)."""
    said = " ".join(((stderr or "").strip() or (stdout or "").strip()).split()) or f"exit {rc}"
    if rc != 0:
        if rc == 2 and NO_SESSION in (stderr or ""):
            return Reach(answered=True)
        return Reach(answered=False, said=said)
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    head = lines[0].split() if lines else []
    short = ""
    if head[1:] == ["(this", "session)"]:
        session, seat, short = True, "", head[0].lower()
    elif head[1:] == ["(seat)"]:
        session, seat = False, head[0].lower()
    else:
        return Reach(answered=False, said=said)
    found: set[str] = set()
    boxed = True
    for ln in lines[1:]:
        if ln.startswith("-- "):
            boxed = "NO BOX" not in ln
            continue
        words = ln.lstrip(_TREE).split()
        if not words or words[0] == "…":
            continue
        addr = words[0].lower()
        found.add(addr)
        if session and not seat and " ".join(words[1:]).startswith("your seat"):
            seat = addr
    if seat:
        found.add(seat)
    return Reach(answered=True, session=session, seat=seat, addresses=frozenset(found), boxed=boxed,
                 short=short, sid=(sid or "").strip().lower() if session else "")


def message(shell: Mapping[str, str]) -> str | None:
    """The `message` this shell runs: DC_MESSAGE (or its deprecated alias),
    else `message` on PATH, as an absolute path; None when there is none."""
    want = os.path.expanduser(shell.get("DC_MESSAGE") or shell.get("PIP_CHAT_MESSAGE") or "message")
    found = shutil.which(want, path=shell.get("PATH", os.defpath))
    return os.path.abspath(found) if found else None


def ask(shell: Mapping[str, str], env: Mapping[str, str] | None = None,
        exe: str | None = None) -> Reach:
    """Ask sessionmail (EXE, else message(SHELL)) what reaches the session
    SHELL is in, run with ENV (else SHELL, with EXE's directory first on
    PATH). Read only. No `message`, a timeout, or text this cannot read is
    Reach(answered=False), with why."""
    exe = exe or message(shell)
    if exe is None:
        return Reach(answered=False, said="no `message` binary")
    env = _env(shell, env, exe)
    try:
        r = _list(exe, (), env)
    except (OSError, subprocess.SubprocessError) as exc:
        return Reach(answered=False, said=f"it could not be run: {exc}")
    return parse(r.returncode, r.stdout or "", r.stderr or "", shell_sid(env))


def _env(shell: Mapping[str, str], env: Mapping[str, str] | None, exe: str) -> Mapping[str, str]:
    """ENV, else SHELL with EXE's directory first on PATH."""
    if env is not None:
        return env
    out = dict(shell)
    out["PATH"] = os.pathsep.join(p for p in (os.path.dirname(exe), shell.get("PATH", "")) if p)
    return out


def _list(exe: str, box: tuple[str, ...], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """`message subscription list [BOX]`, read only: it lists, never sends."""
    argv = [exe, *ARGS, *box]
    return subprocess.run(argv, env=dict(env), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=TIMEOUT_S, check=False)


def unowned_answer(rc: int, stdout: str, stderr: str) -> bool:
    """`message subscription list BOX`'s exit and text -> does it say, plainly,
    that nobody hears BOX? Exit 2 saying NO_SUCH_BOX (the box was never
    made), or exit 0 whose last line is UNOWNED_FOOT (no subscriber, no seat
    session, no live listener). Anything else, an answer it cannot read
    included, is False: never a claim it cannot back."""
    if rc == 2:
        return NO_SUCH_BOX in (stderr or "")
    if rc != 0:
        return False
    lines = [" ".join(ln.split()) for ln in (stdout or "").splitlines() if ln.strip()]
    return bool(lines) and lines[-1] == UNOWNED_FOOT


def unowned(box: str, shell: Mapping[str, str], env: Mapping[str, str] | None = None,
            exe: str | None = None) -> bool:
    """Does sessionmail (EXE, else message(SHELL)), asked with ENV (else
    SHELL), say nobody hears BOX: no seat, no subscription, no live
    listener? Read only: `message subscription list BOX` (unowned_answer).
    No `message`, a failure, a timeout or text this cannot read is False.
    Today's text; sessionmail's machine-readable answer, once it has one,
    replaces it."""
    exe = exe or message(shell)
    if exe is None or not box.strip():
        return False
    try:
        r = _list(exe, (box.strip().lower(),), _env(shell, env, exe))
    except (OSError, subprocess.SubprocessError):
        return False
    return unowned_answer(r.returncode, r.stdout or "", r.stderr or "")


_ASKED: dict[tuple, Reach] = {}


def asked(shell: Mapping[str, str]) -> Reach:
    """ask(SHELL), once per process for one environment: the readers that
    pick the bot (settings.shell_name) ask on every call."""
    key = tuple(sorted(shell.items()))
    if key not in _ASKED:
        _ASKED[key] = ask(shell)
    return _ASKED[key]

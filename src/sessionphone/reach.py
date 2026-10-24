"""reach - what sessionmail says reaches the session a shell is in.

`message subscription list` (no BOX) prints, read only, a tree of what
reaches the calling session: line one is `<sid> (this session)` for a Claude
session, or `<seat> (seat)` when the shell names a seat and no session; under
it one address a row, the session's seat the row noted `your seat`, then the
addresses that feed each one; the last line, `-- N addresses ...`, says NO BOX
while the session has no mail box yet. With no session and no seat it exits 2,
saying so (NO_SESSION). This module reads that text; it is the one place that
does, so a machine-readable form from sessionmail replaces only parse().

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

    @property
    def durable(self) -> str:
        """The seat, when a later session can be started as it: never a plus
        address, never a session id, only a name a bot can carry. "" if none."""
        s = self.seat
        if not s or "+" in s or "@" in s or not name_ok(s) or session_id(s):
            return ""
        return s

    def hears(self, box: str) -> bool:
        """Does mail to BOX reach this session? Only a session hears."""
        return self.session and box.strip().lower() in self.addresses


def name_ok(s: str) -> bool:
    """A name a bot can carry: in its launchd label and its seat `<name>-chat`."""
    return (bool(s) and s.isascii() and s[0].isalnum()
            and all(c.isalnum() or c in "-_." for c in s))


def session_id(s: str) -> bool:
    """A session id (a uuid, or its 8-hex short form): never a bot's name."""
    bare = s.replace("-", "")
    return len(bare) in (8, 32) and all(c in "0123456789abcdef" for c in bare.lower())


def parse(rc: int, stdout: str, stderr: str) -> Reach:
    """`message subscription list`'s exit and text -> Reach. Exit 2 saying
    NO_SESSION is an answer (no session, no seat); any other failure, or a
    first line of neither form, is no answer, with sessionmail's words."""
    said = " ".join(((stderr or "").strip() or (stdout or "").strip()).split()) or f"exit {rc}"
    if rc != 0:
        if rc == 2 and NO_SESSION in (stderr or ""):
            return Reach(answered=True)
        return Reach(answered=False, said=said)
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    head = lines[0].split() if lines else []
    if head[1:] == ["(this", "session)"]:
        session, seat = True, ""
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
    return Reach(answered=True, session=session, seat=seat, addresses=frozenset(found), boxed=boxed)


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
    if env is None:
        env = dict(shell)
        env["PATH"] = os.pathsep.join(p for p in (os.path.dirname(exe), shell.get("PATH", "")) if p)
    argv = [exe, *ARGS]     # read only: it lists, never sends
    try:
        r = subprocess.run(argv, env=dict(env), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return Reach(answered=False, said=f"it could not be run: {exc}")
    return parse(r.returncode, r.stdout or "", r.stderr or "")


_ASKED: dict[tuple, Reach] = {}


def asked(shell: Mapping[str, str]) -> Reach:
    """ask(SHELL), once per process for one environment: the readers that
    pick the bot (settings.shell_name) ask on every call."""
    key = tuple(sorted(shell.items()))
    if key not in _ASKED:
        _ASKED[key] = ask(shell)
    return _ASKED[key]

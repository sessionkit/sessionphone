"""seat - the bot's own signing seat and the agent's box, judged at install.

`sessionphone bot install` runs this before it writes anything of its own:

1. plan() writes nothing. It pins DC_MESSAGE (the installing shell's
   `message`, as an absolute path, unless the shell set DC_MESSAGE), and it
   judges the signer. With both DC_SIGNER_SOCK and DC_SIGNER_PUB set (by
   this shell, or saved), that seat is used as given. With only one set,
   install refuses with the bot's own start-up refusal (seatmail.check).
   With neither, this sessionmail must run the seat's agent itself: its
   `message seat agent --help` must carry a capability line of its own
   (CAPABILITIES). When it lacks it, install refuses with the bot's
   start-up refusal, which names the steps that make the seat by hand, and
   says that this sessionmail cannot run the agent for install.
2. run_agent() is where sessionmail writes. `message seat agent --install
   <seat>` loads the seat's key agent under launchd (the seat must be minted
   already) and prints SEAT_SOCKET and SEAT_PUB, which become the job's
   DC_SIGNER_SOCK and DC_SIGNER_PUB. Its output is split as the shell would
   split it, never evaluated. A refusal is exit 2 and a failure exit 1, each
   with sessionmail's own line and the step that fixes it.
3. key() is `bot install --registry DIR`: it gives the bot a key on any
   carrier. It runs the seat's agent (run_agent); only when sessionmail
   refuses because the seat is not minted does it mint it first, into DIR
   (`message seat init --registry DIR <seat>`), then run the agent again. A
   key already there is never minted again (seat init refuses to overwrite
   one, and its agent simply answers), so a re-run is safe.
4. ensure_box() makes the box Delta text goes to: `message setup --box
   <agent>`, then `message path <agent>` proves it is there. The bot asks
   again at every start (have_box): the mail drop never makes a box, and a
   send to a box that is not there is refused (rc=3), so the box is made, or
   the start refused, before the first send. An address off this host has no
   box here to make: on an MTA its reader makes it, and on the mail drop,
   which delivers locally only, the start is refused.

Nothing here decides must-sign: seatmail.signing() does, from the carrier
this bot's own `message send` will use, and every Seat carries its mode and
reason for the `seat:` line. Signed (an MTA, or a signer given), install
never loads a job without a signer, and an agent that did not install sets
no DC_SIGNER_*. Unsigned (the mail drop, no signer given or saved), plan()
returns an unsigned Seat: no key, no registry, no capability asked, no agent
run. Refused (a carrier sessionmail refuses or that cannot be known), plan()
refuses (NoCarrier) and nothing is written.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass

from sessionphone import reach as reach_
from sessionphone.bridge import dc_env, seatmail

# What a sessionmail must offer before install runs the bot's seat agent itself:
# (the help page's argv, the line it must carry, what lacking it means).
# The line must be the page's whole line, as `grep -qx` matches it.
# One place: a further marker is one more row here.
CAPABILITIES = (
    (("seat", "agent", "--help"), "Capability: install",
     "its `message seat agent` cannot install the seat's agent"),
)
# The lines `message seat agent --install` prints, KEY=VALUE, shell-quoted.
# Only these are read; any other line is ignored.
SEAT_KEYS = ("SEAT_LABEL", "SEAT_PLIST", "SEAT_SOCKET", "SEAT_PUB", "SEAT_LOG")
# sessionmail's refusal when the seat has no key yet
NOT_MINTED = "mint the seat first"
SIGNER = ("SIGNER_SOCK", "SIGNER_PUB")      # the job's DC_SIGNER_*
SIGNER_VARS = tuple(f"DC_{k}" for k in SIGNER)
PAGE_TIMEOUT_S = 15       # a --help page, --version, `message path`, --list-agents
TOOL_TIMEOUT_S = 120      # seat agent --install, setup --box
HEAR = "To hear Delta messages, start Claude with: session start {agent}"
# Mail to a seat's box reaches a session only through the pager's doorbell, and
# the doorbell rings only for a session started as that agent: a plain
# `claude` gets no copy, and nothing tells it so.
DOORBELL = (" (it runs `claude --agent {agent}`: only a session started as {agent} hears "
            "the box; a plain `claude` gets no copy of its mail, and nothing says so)")
NO_AGENT_FILE = ' (no agent file "{agent}" yet: see the README)'
# How to send Delta text to another box, said wherever the box is named.
CHANGE = (" To send Delta text to another box, install again with DC_AGENT=NAME (the agent a "
          "session is started as) or DC_MAILTO=BOX (a box a session hears).")
# Said only when sessionmail, asked, says this session hears the box.
HEARD = ("this session hears box {agent} ({how}): Delta messages and calls reach it, "
         "while it runs")
UNBOXED = ("; this session has no mail box yet, so nothing reaches it until its "
           "`pager listen` makes one")
# Said FIRST when sessionmail, asked, says this session does not hear the box.
DEAF = ("this session will NOT hear Delta messages or calls: they go to box {agent}, and "
        "this session hears {hears}. Ways out: start Claude with `session start {agent}` "
        "(only a session started as {agent} hears that box, today and in later sessions); "
        "or install again with DC_AGENT=NAME (the agent a session is started as) or "
        "DC_MAILTO=BOX (a box a session hears); or `sessionphone bot install --hear-here`, "
        "which lets this session hear box {agent} until this session ends.")
UNASKED = (" Whether this session hears it was not measured: `message subscription list` "
           "gave no answer ({said}).")
# `bot install --hear-here`: a session subscription, which sessionmail ends with the session.
HEAR_HERE_ARGS = ("subscription", "add")
HEAR_HERE_ENDS = ("it ends with this session: a later session does not hear the box unless "
                  "it is started with `session start {agent}`, or runs --hear-here again")
# `bot install`, when the bot's name fell through to the default and sessionmail
# says nobody hears its box (no seat, no subscription, no live listener).
FALLTHROUGH = ("no seat owns box {agent}: the bot's name came from the default, so nothing hears "
               "its mail yet. Start a bare `session start` first; it becomes {agent}.")


class NoSigner(Exception):
    """The bot's seat has no usable key: only one DC_SIGNER_*, a sessionmail
    that cannot run the seat's agent, a seat not minted, a refusal from
    `message seat agent --install`, or the bot's own start check refuses.
    Exit code 2, naming the step; nothing was written."""


class NoCarrier(NoSigner):
    """seatmail.signing() rules REFUSED: the carrier this bot's own `message
    send` will use is one sessionmail refuses, or cannot be known. No mail
    could reach the agent, signed or not. Exit code 2; nothing was written."""


class NotMinted(NoSigner):
    """`message seat agent --install` refused because the seat has no key
    yet. Nothing was written: key() mints it, given a registry."""


class SeatFailed(RuntimeError):
    """`message seat agent --install` failed (its exit 1, or no answer).
    Exit code 1, with its reason and the recovery."""


class NoBox(Exception):
    """The box Delta text goes to is not on this host, and `message setup
    --box` did not make it; or its address is off this host and the carrier is
    the mail drop, which delivers locally only. Exit code 2, naming the step
    to run."""


@dataclass
class Seat:
    """The bot's seat as install found it, or as sessionmail installed its agent."""
    name: str               # the seat, `<agent>-chat`
    pub: str                # DC_SIGNER_PUB
    sock: str               # DC_SIGNER_SOCK
    label: str = ""         # the agent's launchd label, when sessionmail installed it
    mode: str = seatmail.SIGNED     # seatmail.signing()'s mode for the job ...
    reason: str = ""                # ... and why
    minted: str = ""                # the registry key() minted the seat into, this run

    def line(self) -> str:
        how = f"{self.mode} ({self.reason})" if self.reason else self.mode
        if not (self.pub or self.sock):
            return f"{self.name}, {how}, no key"
        if self.label:
            run = "run by `message seat agent --install`"
            if self.minted:
                run = f"minted into {self.minted} by `message seat init`, {run}"
            return f"{self.name}, {how}, key {self.pub}, agent {self.label} on {self.sock} ({run})"
        return f"{self.name}, {how}, key {self.pub}, agent on {self.sock} (given: DC_SIGNER_*)"


def _signed(made: Seat, env: Mapping[str, str]) -> Seat:
    """MADE, with the mode and reason seatmail.signing() rules for the job's ENV."""
    made.mode, made.reason = seatmail.signing(env)
    return made


@dataclass
class Box:
    """Where Delta text goes, and the box on this host that holds it."""
    name: str               # the box: DC_MAILTO's local part, else the agent
    mailto: str             # the address the bot mails
    path: str | None        # the maildir, or None when the address is off this host

    def line(self) -> str:
        if self.path is None:
            return (f"Delta text goes to {self.mailto}, which is not a box on this host: "
                    "its reader makes the box and hears it")
        return f"Delta text goes to {self.mailto}, box {self.path}"


def _run(argv: list[str], env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess:
    """Run a sessionmail tool, no tty: a setup step that wants sudo only runs
    at a tty, so here it is left for the operator, never prompted for."""
    return subprocess.run(argv, env=dict(env), stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout, check=False)


def _said(r: subprocess.CompletedProcess) -> str:
    """A tool's reason in one line: its stderr, else its stdout, else its exit."""
    text = " ".join(((r.stderr or "").strip() or (r.stdout or "").strip()).split())
    return text or f"exit {r.returncode}"


def tool_env(shell: Mapping[str, str], exe: str) -> dict[str, str]:
    """The environment sessionmail's tools run in: the installing shell's, with
    the pinned `message`'s directory first on PATH (`seat agent --install`
    needs `message` on PATH, and it must be the same one), and SESSION_MAIL_ROOT
    mirroring SESSIONMAIL_ROOT when that is set (seatmail.root_env: a workaround
    until sessionmail's `path` and `setup` read SESSIONMAIL_ROOT too)."""
    env = seatmail.root_env(shell)     # workaround: `path`/`setup` read the older root variable only
    path = env.get("PATH", os.defpath)
    env["PATH"] = os.pathsep.join([os.path.dirname(exe), path]) if path else os.path.dirname(exe)
    return env


def seat_name(env: Mapping[str, str], agent: str) -> str:
    """The bot's seat: DC_ADDR's local part, else `<agent>-chat`."""
    addr = dc_env._get(env, "ADDR")
    return seatmail.local_part(addr) if addr else f"{agent}-chat"


def pin_message(env: MutableMapping[str, str], shell: Mapping[str, str]) -> str:
    """Pin the job's DC_MESSAGE: the shell's own DC_MESSAGE is kept as set;
    else `message` on the shell's PATH, as an absolute path (launchd's PATH is
    short). -> the binary, resolved. Raises NoSigner when there is none."""
    if not dc_env._get(env, "MESSAGE"):
        found = seatmail.resolve_message({"PATH": shell.get("PATH", os.defpath)})
        if found:
            env["DC_MESSAGE"] = found
    exe = seatmail.resolve_message(env) or seatmail.resolve_message(
        {"PATH": shell.get("PATH", os.defpath), "DC_MESSAGE": dc_env._get(env, "MESSAGE") or ""})
    if exe is None:
        raise NoSigner(seatmail.check(env) or "no `message` binary")
    return exe


def version(exe: str, env: Mapping[str, str]) -> str:
    """`message --version`'s first line, or 'version unknown'."""
    try:
        r = _run([exe, "--version"], env, PAGE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return "version unknown"
    first = (r.stdout or "").strip().splitlines()
    return first[0].strip() if r.returncode == 0 and first else "version unknown"


def missing_capabilities(exe: str, env: Mapping[str, str]) -> list[tuple[str, str]]:
    """-> [(marker, what lacking it means)] for each CAPABILITIES page that does
    not exit 0 with its marker as a line of its own. Reads help pages only."""
    missing = []
    for argv, marker, means in CAPABILITIES:
        try:
            r = _run([exe, *argv], env, PAGE_TIMEOUT_S)
            ok = r.returncode == 0 and marker in (r.stdout or "").split("\n")    # grep -qx
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            missing.append((marker, means))
    return missing


def runs_agent(given: Mapping[str, str], shell: Mapping[str, str]) -> bool:
    """Can the `message` this install would pin run the seat's agent itself?
    GIVEN is what the job would carry (its DC_MESSAGE, if any); else `message`
    on the shell's PATH, as pin_message() finds it. True when that sessionmail
    offers every CAPABILITIES line. Reads help pages only; writes nothing."""
    exe = seatmail.resolve_message({"PATH": shell.get("PATH", os.defpath),
                                    "DC_MESSAGE": dc_env._get(given, "MESSAGE") or ""})
    return exe is not None and not missing_capabilities(exe, tool_env(shell, exe))


def plan(env: MutableMapping[str, str], seat: str, shell: Mapping[str, str],
         keyed: bool = False) -> Seat | None:
    """Everything install judges about the seat before anything is written.
    Pins DC_MESSAGE in ENV. -> the given Seat (both DC_SIGNER_* set), an
    unsigned Seat (neither set, and seatmail.signing() rules unsigned: the
    drop carrier, and not KEYED), or None: run_agent() (or key(), when KEYED:
    `bot install --registry`) must run the seat's agent. Raises NoCarrier
    when signing() rules REFUSED. Raises NoSigner, having
    written nothing: with one DC_SIGNER_* set, the bot's own start-up
    refusal; with neither, when this sessionmail lacks the capability
    (CAPABILITIES), that refusal, which names the steps by hand, and what
    this sessionmail lacks."""
    exe = pin_message(env, shell)
    mode, _reason = seatmail.signing(env)
    if mode == seatmail.REFUSED:
        raise NoCarrier(f"{seatmail.check(env, seat=seat)} Nothing was written.")
    sock, pub = (dc_env._get(env, k) for k in SIGNER)
    if sock and pub:
        return _signed(Seat(seat, pub, sock), env)
    if sock or pub:
        raise NoSigner(f"{seatmail.check(env, seat=seat) or 'no signer'} Nothing was written.")
    if mode == seatmail.UNSIGNED and not keyed:
        return _signed(Seat(seat, "", ""), env)      # no key wanted: no agent, no capability asked
    missing = missing_capabilities(exe, tool_env(shell, exe))
    if not missing:
        return None
    why = seatmail.check(env, seat=seat) or "--registry asks for the bot's key, but"
    lacks = ", and ".join(f'{means} (no "{marker}")' for marker, means in missing)
    raise NoSigner(f"{why} `bot install` cannot run the seat's agent itself with this "
                   f"sessionmail ({version(exe, tool_env(shell, exe))}): {lacks}. Follow the "
                   f"README's steps, or upgrade sessionmail. Nothing was written.")


def parse_install(stdout: str) -> dict[str, str]:
    """`message seat agent --install`'s stdout -> {SEAT_*: value}, SEAT_KEYS only.
    Each line is split as the shell would split it (shlex), never evaluated:
    a line that is not one KEY=VALUE word is ignored."""
    out: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        if len(words) != 1 or "=" not in words[0]:
            continue
        key, _, value = words[0].partition("=")
        if key in SEAT_KEYS:
            out[key] = value
    return out


def _lines(r: subprocess.CompletedProcess, mark: str) -> str:
    """A tool's stderr from the first line carrying MARK on (sessionmail's
    `REFUSED - ` or `FAILED - ` line, and what it quotes after it), as it was
    written; else _said(r)."""
    lines = (r.stderr or "").strip().splitlines()
    for i, ln in enumerate(lines):
        if mark in ln:
            return "\n".join(x.rstrip() for x in lines[i:])
    return _said(r)


def run_agent(env: MutableMapping[str, str], seat: str, shell: Mapping[str, str]) -> Seat:
    """Run the seat's key agent under launchd with sessionmail: `message seat
    agent --install SEAT`, no SOCKET (sessionmail's default). Sets the job's
    DC_SIGNER_SOCK and DC_SIGNER_PUB in ENV to its SEAT_SOCKET and SEAT_PUB,
    and to nothing else. -> the Seat. Raises NoSigner (its exit 2: refused,
    nothing written; a seat not minted names the step that mints it) or
    SeatFailed (its exit 1, or no answer: the recovery is named), and then
    ENV carries no signer."""
    exe = pin_message(env, shell)
    argv = [exe, "seat", "agent", "--install", seat]
    said = f"`message seat agent --install {seat}`"
    again = (f"Run `message seat agent --uninstall {seat}`, then `message seat agent --install "
             f"{seat}` again (or run `sessionphone bot install` again).")
    try:
        r = _run(argv, tool_env(shell, exe), TOOL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise SeatFailed(f"{said} did not finish in {TOOL_TIMEOUT_S}s, so the bot has no signer "
                         f"and nothing of the bot was installed. {again}") from None
    except OSError as exc:
        raise SeatFailed(f"{said} could not be run ({exc}), so the bot has no signer and "
                         "nothing of the bot was installed.") from None
    if r.returncode == 2:
        refused = _lines(r, "REFUSED - ")
        if NOT_MINTED in refused:
            raise NotMinted(f"the bot's seat {seat} is not minted yet, so {said} refused (exit 2):\n"
                            f"  {refused}\nGive it a key with `sessionphone bot install --registry "
                            f"DIR` (DIR: a seat registry; the README's \"The bot's seat\" says how to "
                            "make one): it mints the seat there, then runs its agent.")
        raise NoSigner(f"{said} refused (exit 2):\n  {refused}\nThe bot was not installed; "
                       "fix that, then run `sessionphone bot install` again.")
    if r.returncode != 0:
        failed = _lines(r, "FAILED - ").replace("\n", "\n  ")
        raise SeatFailed(f"{said} failed (exit {r.returncode}), so the bot has no signer and "
                         f"nothing of the bot was installed:\n  {failed}\n{again}")
    got = parse_install(r.stdout)
    if not (got.get("SEAT_SOCKET") and got.get("SEAT_PUB")):
        raise SeatFailed(f"{said} answered 0 but printed no SEAT_SOCKET and SEAT_PUB, so the bot "
                         f"has no signer and nothing of the bot was installed. {again}")
    env["DC_SIGNER_SOCK"], env["DC_SIGNER_PUB"] = got["SEAT_SOCKET"], got["SEAT_PUB"]
    return _signed(Seat(seat, got["SEAT_PUB"], got["SEAT_SOCKET"], got.get("SEAT_LABEL", "")), env)


def mint(seat: str, registry: str, env: Mapping[str, str], shell: Mapping[str, str]) -> None:
    """`message seat init --registry REGISTRY SEAT`: mint the seat's key into
    REGISTRY. Raises NoSigner (its exit 2: refused, nothing written) or
    SeatFailed (its exit 1, rolled back, or no answer)."""
    exe = pin_message(env, shell)
    said = f"`message seat init --registry {registry} {seat}`"
    try:
        r = _run([exe, "seat", "init", "--registry", registry, seat], tool_env(shell, exe),
                 TOOL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise SeatFailed(f"{said} did not finish in {TOOL_TIMEOUT_S}s, so the bot has no key and "
                         "nothing of the bot was installed. Run `sessionphone bot install "
                         "--registry` again.") from None
    except OSError as exc:
        raise SeatFailed(f"{said} could not be run ({exc}), so the bot has no key and nothing "
                         "of the bot was installed.") from None
    if r.returncode == 2:
        raise NoSigner(f"{said} refused (exit 2):\n  {_lines(r, 'REFUSED - ')}\nThe bot was not "
                       "installed; fix that, then run `sessionphone bot install --registry` again.")
    if r.returncode != 0:
        failed = _lines(r, "FAILED - ").replace("\n", "\n  ")
        raise SeatFailed(f"{said} failed (exit {r.returncode}), so the bot has no key and nothing "
                         f"of the bot was installed:\n  {failed}\nRun `sessionphone bot install "
                         "--registry` again.")


def key(env: MutableMapping[str, str], seat: str, shell: Mapping[str, str], registry: str) -> Seat:
    """`bot install --registry REGISTRY`: run the seat's agent (run_agent),
    minting the seat into REGISTRY first only when sessionmail says it has no
    key (NotMinted). A key already there is never minted again. -> the Seat,
    its `minted` the registry when this run minted it. Raises as run_agent
    and mint do, and then ENV carries no signer."""
    registry = os.path.abspath(os.path.expanduser(registry))
    try:
        return run_agent(env, seat, shell)
    except NotMinted:
        pass
    mint(seat, registry, env, shell)
    made = run_agent(env, seat, shell)
    made.minted = registry
    return made


def on_host(mailto: str, env: Mapping[str, str]) -> bool:
    """Is MAILTO's domain this host, as sessionmail's drop judges it: its mail
    host, or a name under it? A bare MAILTO (no host) is this host. Off this
    host, the box is not ours to make."""
    return seatmail.on_host(mailto, env)


def box_for(env: Mapping[str, str], mailto: str | None = None) -> Box:
    """The box a bot run with ENV mails Delta text to (dc_env.mailto), or the
    box MAILTO names when given (the web seat's To), path unknown."""
    mailto = dc_env.mailto(env) if mailto is None else mailto
    return Box(seatmail.local_part(mailto).strip().lower(), mailto, None)


def _box_path(exe: str, name: str, tenv: Mapping[str, str]) -> str | None:
    """`message path NAME`: the maildir sessionmail resolves the box to, or
    None when it says there is none (or cannot be asked). Reads only."""
    try:
        found = _run([exe, "path", name], tenv, PAGE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    path = (found.stdout or "").strip().splitlines()
    return path[0].strip() if found.returncode == 0 and path else None


BOT_AGAIN = "`sessionphone bot install` again"
BOT_SETTING = "DC_MAILTO"


def _off_host(box: Box, env: Mapping[str, str], setting: str) -> Box:
    """BOX is off this host: its reader makes it, on a carrier that reaches it.
    The mail drop delivers on this host only, so there every send to it would
    be refused: raise NoBox, one line naming the address and what to do (the
    SETTING that names a box here, or a carrier that reaches it). -> BOX on
    any other carrier."""
    if seatmail.on_drop(env):
        host = (dc_env.mail_host(env) or "").strip() or "unknown"
        raise NoBox(f"{box.mailto} is not on this host ({host}), and the mail drop delivers "
                    f"locally only: set {setting} to a box on this host, or use a carrier "
                    f"that reaches it (an MTA in {seatmail.CARRIER_VAR}).")
    return box


def have_box(env: Mapping[str, str], shell: Mapping[str, str], mailto: str | None = None, *,
             what: str = "Delta text", again: str = BOT_AGAIN, setting: str = BOT_SETTING) -> Box:
    """The bot's start, and `sessionphone web --legs`' (MAILTO: the web seat's
    To): the box its mail goes to must exist before the first send. The mail
    drop never makes a box, and a send to one that is not there is refused
    (rc=3), so a box that went after install would lose every mail. Asks
    `message path` first (reads only), for the box and, for a plus address,
    its base (seatmail.cc_boxes: the drop delivers `base+tag` there); only
    when sessionmail says there is neither does it make one (ensure_box).
    -> the Box (its path None off this host, where its reader makes it, on a
    carrier that reaches it). Raises NoBox, naming WHAT the box is for and what
    to run AGAIN; off this host on the mail drop, naming the address and the
    SETTING that names a box here (_off_host)."""
    box = box_for(env, mailto)
    if not on_host(box.mailto, env):
        return _off_host(box, env, setting)
    exe = seatmail.resolve_message(env)
    if exe is None:
        raise NoBox(seatmail.check(env) or "no `message` binary")
    tenv = tool_env(shell, exe)
    for name in seatmail.cc_boxes(box.name):
        box.path = _box_path(exe, name, tenv)
        if box.path is not None:
            return box
    return ensure_box(env, shell, mailto, what=what, again=again, setting=setting)


def ensure_box(env: Mapping[str, str], shell: Mapping[str, str], mailto: str | None = None, *,
               what: str = "Delta text", again: str = BOT_AGAIN, setting: str = BOT_SETTING) -> Box:
    """Make the box Delta text goes to (or MAILTO's, when given), when it is on
    this host: `message setup --box NAME`, then `message path NAME`. Only
    `message path` is believed: setup can exit 4 (steps left for the operator)
    with the box made. -> the Box, its path None when the address is off this
    host (on a carrier that reaches it). Raises NoBox, naming WHAT the box is
    for and what to run AGAIN; off this host on the mail drop, naming the
    address and the SETTING that names a box here (_off_host)."""
    box = box_for(env, mailto)
    if not on_host(box.mailto, env):
        return _off_host(box, env, setting)
    exe = seatmail.resolve_message(env)
    if exe is None:
        raise NoBox(seatmail.check(env) or "no `message` binary")
    tenv = tool_env(shell, exe)
    try:
        setup = _run([exe, "setup", "--box", box.name], tenv, TOOL_TIMEOUT_S)
        last = (setup.stdout or setup.stderr or "").strip().splitlines()
        setup_said = " ".join(last[-1].split()) if last else f"exit {setup.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        setup_said = f"it could not be run: {exc}"
    box.path = _box_path(exe, box.name, tenv)
    if box.path is not None:
        return box
    raise NoBox(f"no box `{box.name}` for {what}: `message setup --box {box.name}` did not make "
                f"one ({setup_said}); run it yourself, then {again}.")


def _list_agents(shell: Mapping[str, str]) -> set[str] | None:
    """The agents `session start --list-agents` knows (read-only), or None when
    `session` is not on PATH or does not answer."""
    exe = shutil.which("session", path=shell.get("PATH", os.defpath))
    if not exe:
        return None
    try:
        r = _run([exe, "start", "--list-agents"], shell, PAGE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return {ln.split()[0] for ln in (r.stdout or "").splitlines() if ln.strip()}


def hear_words(agent: str) -> str:
    """How a Claude session hears the box AGENT, asking nothing: `session
    start AGENT`, which runs `claude --agent AGENT`, the one session the
    doorbell rings for. A plain `claude` gets no copy. Then how to send
    Delta text to another box (CHANGE). The bot's start says this: it cannot
    know which session installed it."""
    return HEAR.format(agent=agent) + DOORBELL.format(agent=agent) + "." + CHANGE


Reach = reach_.Reach


def reach(shell: Mapping[str, str]) -> Reach:
    """Ask sessionmail what reaches the session this shell is in (reach_.ask):
    `message subscription list`, run as sessionmail's other tools are
    (tool_env), read only. Asked afresh: --hear-here measures again after it
    subscribes."""
    exe = seatmail.resolve_message(shell)
    if exe is None:
        return Reach(answered=False, said="no `message` binary")
    return reach_.ask(shell, tool_env(shell, exe), exe)


def _hears_what(r: Reach) -> str:
    """What this session hears, in words, for DEAF."""
    own = sorted(a for a in r.addresses if a != r.seat)
    parts = ["its own session box"]
    if r.seat:
        parts.append(f"its seat {r.seat}")
    parts += own
    return "only " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0])


def deaf(agent: str, r: Reach) -> bool:
    """Did sessionmail, asked, say THIS Claude session will not hear box AGENT?
    The one case hear_line WARNS (DEAF), and `bot install` then prints that
    warning first. A Terminal (no session) or no answer is advice, not this."""
    return r.answered and r.session and not r.hears(agent)


def hear_line(agent: str, shell: Mapping[str, str], r: Reach | None = None) -> str:
    """Will a Claude session hear the box AGENT? Measured, never guessed:
    sessionmail is asked what reaches this session (reach, or R when given).
    Heard: says so, and how. Not heard, in a Claude session: says so FIRST
    (DEAF), then the ways out. Not a session (a Terminal), or sessionmail
    gave no answer: the advice (hear_words), and why nothing was measured.
    It never refuses anything: install has written the bot either way."""
    r = reach(shell) if r is None else r
    if r.answered and r.hears(agent):
        how = (f"its seat {r.seat}" if r.seat == agent else
               "its own session box" if r.own_box(agent) else "by a subscription")
        return HEARD.format(agent=agent, how=how) + ("" if r.boxed else UNBOXED)
    if deaf(agent, r):
        line = DEAF.format(agent=agent, hears=_hears_what(r))
    else:
        line = hear_words(agent)
        if not r.answered:
            line += UNASKED.format(said=r.said)
    known = _list_agents(shell)
    if known is None or agent not in known:
        line += NO_AGENT_FILE.format(agent=agent)
    return line


def fallthrough(agent: str, came: str, shell: Mapping[str, str], r: Reach) -> str | None:
    """`bot install`'s one line when the bot's name CAME from the default
    (settings.pick_name: "the default ...") and nobody hears its box AGENT:
    this session does not (R), and sessionmail, asked (reach_.unowned: `message
    subscription list AGENT`, read only), says no seat, subscription or live
    listener does. -> FALLTHROUGH, or None: any other case, and any answer
    that is not plainly "nobody", says nothing."""
    if not came.startswith("the default") or r.hears(agent):
        return None
    exe = seatmail.resolve_message(shell)
    if exe is None or not reach_.unowned(agent, shell, tool_env(shell, exe), exe):
        return None
    return FALLTHROUGH.format(agent=agent)


def hear_here(agent: str, shell: Mapping[str, str]) -> str:
    """`bot install --hear-here`: let THIS session hear box AGENT, by a session
    subscription (`message subscription add AGENT`), which sessionmail ends
    with the session. -> one line saying what was done, or why not. Never
    raises: install has written the bot either way."""
    r = reach(shell)
    if r.answered and not r.session:
        return (f"not done: this shell is not a Claude session, so there is no session to "
                f"subscribe to box {agent}")
    if r.hears(agent):
        return f"not needed: this session already hears box {agent}"
    exe = seatmail.resolve_message(shell)
    if exe is None:
        return "not done: no `message` binary"
    argv = [exe, *HEAR_HERE_ARGS, agent]
    try:
        done = _run(argv, tool_env(shell, exe), TOOL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not done: `message subscription add {agent}` could not be run: {exc}"
    if done.returncode != 0:
        return (f"not done: `message subscription add {agent}` said: {_said(done)} "
                f"(exit {done.returncode})")
    return (f"this session now hears box {agent} (`message subscription add {agent}`); "
            + HEAR_HERE_ENDS.format(agent=agent))


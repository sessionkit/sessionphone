"""`sessionphone bot migrate`: move the bot's old default root into the kit.

The default root moved from ~/.sessionphone to ~/.sessionkit/phone/state/
(paths.default_root). A Mac whose bot still lives in the old one
keeps reading it there until it is moved, and this is the move. It is done once
per host, by a person, with the bots on that root stopped.

The old root holds the Delta Chat account and its KEYS (accounts/, 0700), so
the one rule here is that nothing is copied: the root is moved by rename(2) on
one filesystem, which is atomic and keeps every inode, mode and key as it was.
A move that would cross filesystems is refused, never done as copy+delete.

    1. Refuse while any bot on the old root runs (its .bot.lock is held, or its
       bot.pid names a running bot) or a call is live on it (call-live.json, as
       `bot install` judges it, service.live_call). The locks found are held
       for the rest of the move, so no bot starts on the root mid-move.
    2. Refuse when the new root is already there, or the old one is a link
       somewhere else: one root each, never a merge.
    3. Rename the old root to the new one, then leave the old path as a SYMLINK
       to it, so anything still holding the old name (a plist not re-rendered,
       ears' socket glob, a person's habit) keeps working. The old root is
       never deleted, here or by `bot uninstall`.
    4. Re-render the launchd plists whose root is the old root (their
       DC_ROOT, else SESSIONPHONE_ROOT): every value naming the old root, or a
       path under it, names the new one. A plist with any other root, explicit
       or not, is left as it is. launchd reads a plist when it loads the job,
       so a loaded job keeps the old path (now the link) until `bot install`
       or the next login.

An explicit root (SESSIONPHONE_ROOT, DC_ROOT, one phone.json keeps) is never
moved: only the old DEFAULT root is. --dry-run does the checks and says each
step it would take, the account's new path included, and changes nothing.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import pathlib
import plistlib
import stat
from collections.abc import Callable, Iterator

from sessionphone import paths, service
from sessionphone.bridge import dc_env, pidfile

# the plists a sessionphone bot runs under, in the user's LaunchAgents: this
# version's label and the one sessionphone 0.1.0 installed (service.py)
LABELS = (service.LABEL_PREFIX, service.LEGACY_LABEL_PREFIX)
ACCOUNTS = "accounts"
# the bot's own names in a home (bridge/bot.py LOCK_NAME, its bot.pid, calls.py's
# call-live.json), spelled here so a migrate does not import the bot (WebRTC);
# tests/test_migrate.py pins LOCK_NAME to the bot's
LOCK_NAME = ".bot.lock"
PID_NAME = "bot.pid"
LIVE_NAME = "call-live.json"


class Refused(Exception):
    """The move cannot be done safely: nothing was moved. Exit code 2."""


class Failed(Exception):
    """A step after the checks failed: the message says what was done. Exit 1."""


def launch_agents() -> pathlib.Path:
    """The user's LaunchAgents, where `bot install` writes the job's plist."""
    return service.plist_path("x").parent


def old_root() -> pathlib.Path:
    return pathlib.Path(paths.OLD_ROOT).expanduser()


def new_root() -> pathlib.Path:
    return pathlib.Path(paths.DEFAULT_ROOT).expanduser()


# ---- the bots on the root: none may run, no call may be live ------------------

def homes(root: pathlib.Path) -> list[pathlib.Path]:
    """The bot homes under ROOT: the root itself (the default profile's), and
    each folder in it that a named profile has used (one holding a .bot.lock,
    bot.pid or call-live.json, or an accounts/ folder: a profile set up but
    never run on this host has keys too, and dry-run's account: line names
    them). The lock, pid and call probes find nothing in a folder that has
    only accounts/, so counting it there is harmless."""
    found = [root]
    try:
        kids = sorted(root.iterdir())
    except OSError:
        return found
    for d in kids:
        if d.is_dir() and not d.is_symlink() and any(
                (d / n).exists() for n in (LOCK_NAME, PID_NAME, LIVE_NAME, ACCOUNTS)):
            found.append(d)
    return found


def _profile(root: pathlib.Path, home: pathlib.Path) -> str:
    return "" if home == root else home.name


@contextlib.contextmanager
def held(root: pathlib.Path, plist_envs: list[dict]) -> Iterator[None]:
    """Refuse (Refused) while a bot runs on ROOT or a call is live on it, else
    hold each home's .bot.lock until the block ends, so no bot starts on the
    root while it moves. A bot holds its lock (flock) for its whole life and
    the kernel lets it go when the bot dies, so a lock file left behind by a
    dead bot is no bot. Its bot.pid is asked too (pidfile.started, dc-live's
    rule), and call-live.json as `bot install` judges it (service.live_call),
    on each home and on each PLIST_ENV (a job's own DC_LIVE / DC_PID_FILE).
    Reads files and asks ps; writes nothing, creates no lock file."""
    fds: list[int] = []
    try:
        for home in homes(root):
            lock = home / LOCK_NAME
            if lock.exists():
                try:
                    fd = os.open(lock, os.O_RDONLY)
                except OSError as exc:
                    raise Refused(f"{lock} cannot be read ({exc}), so whether a bot runs on {home} "
                                  "cannot be told. Nothing was moved.") from None
                fds.append(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "unknown"
                    raise Refused(f"a bot (pid {holder}) runs on {home}: it holds {lock}. Stop it "
                                  "first (`sessionphone bot uninstall`, or Ctrl-C a `bot run`). "
                                  "Nothing was moved.") from None
            start, _why = pidfile.started(home / PID_NAME, root)
            if start is not None:
                raise Refused(f"a bot runs on {home}: {home / PID_NAME} names a running bot. Stop it "
                              "first (`sessionphone bot uninstall`). Nothing was moved.")
        envs = [{"DC_ROOT": str(root), **({"DC_PROFILE": _profile(root, h)} if h != root else {})}
                for h in homes(root)] + plist_envs
        for env in envs:
            call = service.live_call(env)
            if call is not None:
                raise Refused(f"a call is live on {root}: {service._call_line(call)}. Hang up and "
                              "stop the bot first. Nothing was moved.")
        yield
    finally:
        for fd in fds:
            os.close(fd)        # closing lets the lock go


# ---- the plists that name the old root -----------------------------------------

def _plists(agents: pathlib.Path) -> list[pathlib.Path]:
    try:
        return sorted(p for p in agents.iterdir()
                      if p.suffix == ".plist" and p.name.startswith(tuple(f"{x}." for x in LABELS)))
    except OSError:
        return []


def _job_root(env: dict) -> str:
    """The root a job runs on, as the bot reads its environment: DC_ROOT (or
    its alias), else SESSIONPHONE_ROOT. "" when it names none."""
    return dc_env._get(env, "ROOT") or env.get(paths.ROOT_VAR) or ""


def _same(a: str, b: pathlib.Path) -> bool:
    return bool(a) and os.path.normpath(os.path.expanduser(a)) == os.path.normpath(str(b))


def _moved(v, old: pathlib.Path, new: pathlib.Path):
    """V with the old root, where it names it or a path under it, as the new."""
    if not isinstance(v, str):
        return v
    p = os.path.normpath(os.path.expanduser(v)) if v else v
    if p == str(old):
        return str(new)
    if p and p.startswith(str(old) + os.sep):
        return str(new) + p[len(str(old)):]
    return v


def rerender(data: dict, old: pathlib.Path, new: pathlib.Path) -> dict:
    """The plist DATA with every value naming OLD (or a path under it) naming
    NEW instead: its EnvironmentVariables, WorkingDirectory and log paths.
    Everything else is kept as it is: only the root moves."""
    out = dict(data)
    env = data.get("EnvironmentVariables")
    if isinstance(env, dict):
        out["EnvironmentVariables"] = {k: _moved(v, old, new) for k, v in env.items()}
    for k in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        if k in out:
            out[k] = _moved(out[k], old, new)
    return out


def plans(agents: pathlib.Path, old: pathlib.Path) -> list[tuple[pathlib.Path, dict | None, str]]:
    """Each sessionphone plist in AGENTS -> (path, its data when its root is
    OLD, else None, and its root in words)."""
    out = []
    for p in _plists(agents):
        try:
            with open(p, "rb") as fh:
                data = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException) as exc:
            out.append((p, None, f"unreadable ({exc.__class__.__name__}), left as it is"))
            continue
        env = data.get("EnvironmentVariables") if isinstance(data, dict) else None
        r = _job_root(env) if isinstance(env, dict) else ""
        out.append((p, data if _same(r, old) else None, r or "none named"))
    return out


def write_plist(p: pathlib.Path, data: dict) -> None:
    """Write DATA to P whole (a temp file renamed in), keeping P's mode."""
    mode = stat.S_IMODE(p.stat().st_mode)
    part = p.with_name(f".{p.name}.{os.getpid()}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            plistlib.dump(data, fh)
        os.chmod(part, mode)
        os.replace(part, p)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise


# ---- the move ---------------------------------------------------------------------

def _device(path: pathlib.Path) -> int:
    """The filesystem PATH (or the nearest folder above it that is there) is on."""
    p = path
    while not os.path.lexists(p) and p != p.parent:
        p = p.parent
    return os.stat(p).st_dev


def accounts_line(root: pathlib.Path, at: pathlib.Path) -> list[str]:
    """Where each home's Delta Chat account under ROOT opens once the root is
    AT: <home>/accounts as the bot opens it (dc_env.accounts_dir), and how many
    accounts (keys) are in it."""
    lines = []
    for home in homes(root):
        acc = home / ACCOUNTS
        if not acc.is_dir():
            continue
        try:
            n = sum(1 for d in acc.iterdir() if d.is_dir())
        except OSError:
            n = 0
        prof = _profile(root, home)
        opens = dc_env.accounts_dir({"DC_ROOT": str(at), **({"DC_PROFILE": prof} if prof else {})})
        lines.append(f"{opens} ({n} account{'s' if n != 1 else ''}, keys kept: moved, never copied)")
    return lines or [f"none: {root} holds no {ACCOUNTS}/ folder"]


def migrate(dry_run: bool = False, say: Callable[[str], None] = print,
            agents: pathlib.Path | None = None) -> int:
    """Move the old default root to the new one (the module's steps). SAY gets
    each line. -> 0 done (or nothing to do). Raises Refused (nothing moved) or
    Failed (a step after the move failed; the message says what was done)."""
    old, new = old_root(), new_root()
    agents = launch_agents() if agents is None else agents
    say(f"old:      {old}")
    say(f"new:      {new}")
    if old.is_symlink():
        target = pathlib.Path(os.path.realpath(old))
        if target != pathlib.Path(os.path.realpath(new)) or not new.is_dir():
            raise Refused(f"{old} is a link to {os.readlink(old)}, not the root to move; "
                          "nothing was moved.")
        say(f"moved:    already: {old} is a link to {new}")
        _plists_step(old, new, agents, dry_run, say)
        if dry_run:
            say("dry run:  nothing was changed")
        return 0
    if not os.path.lexists(old):
        say(f"moved:    nothing to move: {old} is not there; no plist is touched")
        return 0
    if not old.is_dir():
        raise Refused(f"{old} is not a folder; nothing was moved.")
    if os.path.lexists(new):
        raise Refused(f"{new} is already there: one root each, never a merge. Move or remove "
                      f"one of them by hand. Nothing was moved.")
    if _device(old) != _device(new.parent):
        raise Refused(f"{old} and {new.parent} are on different filesystems: the root is moved "
                      "by rename, never copied. Nothing was moved.")
    plan = plans(agents, old)
    envs = [e for _p, d, _r in plan if d and isinstance(e := d.get("EnvironmentVariables"), dict)]
    with held(old, envs):
        for line in accounts_line(old, new):
            say(f"account:  {'would open' if dry_run else 'opens'} at {line}")
        if dry_run:
            say(f"moved:    would rename {old} to {new}")
            say(f"link:     would leave {old} -> {new}")
        else:
            paths.make_log_dir(new.parent)      # each missing folder 0700
            try:
                os.rename(old, new)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise Refused(f"{old} and {new} are on different filesystems: the root is "
                                  "moved by rename, never copied. Nothing was moved.") from None
                raise Failed(f"rename {old} to {new} failed ({exc}); nothing was moved.") from None
            say(f"moved:    {old} to {new}, by rename")
            try:
                os.symlink(new, old)
            except OSError as exc:
                raise Failed(f"the root is at {new} (every reader finds it there), but the link "
                             f"{old} -> {new} could not be made ({exc}): make it by hand "
                             f"(`ln -s {new} {old}`).") from None
            say(f"link:     {old} -> {new}")
    _plists_step(old, new, agents, dry_run, say, plan)
    if dry_run:
        say("dry run:  nothing was changed")
    return 0


def _plists_step(old, new, agents, dry_run, say, plan=None) -> None:
    plan = plans(agents, old) if plan is None else plan
    failed = []
    for p, data, root in plan:
        if data is None:
            say(f"plist:    {p} left as it is: its root is {root}")
            continue
        if dry_run:
            say(f"plist:    {p} would be re-rendered: its root {old} becomes {new}")
            continue
        try:
            write_plist(p, rerender(data, old, new))
        except OSError as exc:
            failed.append(f"{p} ({exc})")
            continue
        say(f"plist:    {p} re-rendered: its root is {new} (launchd reads it at its next load: "
            "`sessionphone bot install`, or the next login)")
    if not plan:
        say(f"plist:    none in {agents}")
    if failed:
        raise Failed(f"the root is moved, but {len(failed)} plist(s) could not be re-rendered: "
                     f"{'; '.join(failed)}. Their jobs still run on {old}, the link. Run "
                     "`sessionphone bot migrate` again, or `sessionphone bot install`.")

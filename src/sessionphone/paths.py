"""Where sessionphone keeps one agent's Delta Chat life: one root, 0700.

Everything hangs off `SESSIONPHONE_ROOT` (default `~/.sessionkit/phone/state`,
the bot's state folder of the kit), so two agents on one Mac are two roots and
never one shared account. A root `bot install` remembers (in phone.json) is
read here too, after this shell's. With no root configured, a Mac whose bot
still lives in the old default, `~/.sessionphone`, keeps it there until it is
moved (default_root). The bot (the resident bridge), the setup verbs and the
dc-* tools read the same paths from here: this is the one place that rule is
kept.
The bot's logs are the one thing out of the default root: they are in its own
folder of the kit, ~/.sessionkit/phone/log/<bot>[/<profile>]/. A bot under any
other root keeps them in that root, <root>/log/<bot>[/<profile>]/ (log_dir).
"""
from __future__ import annotations

import errno
import os
import pathlib
import stat
import sys

# ---- exists / is_dir / is_file, the same answer on every Python -------------
# CPython 3.14 sends Path.exists/is_dir/is_file through os.path.*, which
# swallows EVERY OSError: below a directory this process may not search,
# 3.11-3.13 raise PermissionError and 3.14+ answer False, "absent". Code that
# must not read "unreadable" as "absent" asks these instead. They answer as
# pathlib did through 3.13 (os.stat; the not-there errnos are False, any
# other OSError is raised), whatever Python runs them.
_ABSENT = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)


def _mode(path: str | os.PathLike) -> int | None:
    """PATH's st_mode (symlinks followed), or None if it is not there.
    Raises any other OSError, PermissionError above all."""
    try:
        return os.stat(path).st_mode
    except OSError as e:
        if e.errno in _ABSENT:
            return None
        raise
    except ValueError:          # a NUL in the path: pathlib says False too
        return None


def exists(path: str | os.PathLike) -> bool:
    """Path(PATH).exists() as 3.13 answers it: raises PermissionError where
    3.14 would say False."""
    return _mode(path) is not None


def is_dir(path: str | os.PathLike) -> bool:
    """Path(PATH).is_dir() as 3.13 answers it (see exists)."""
    m = _mode(path)
    return m is not None and stat.S_ISDIR(m)


def is_file(path: str | os.PathLike) -> bool:
    """Path(PATH).is_file() as 3.13 answers it (see exists)."""
    m = _mode(path)
    return m is not None and stat.S_ISREG(m)


KIT = "~/.sessionkit"    # the kit's folder: one folder per package under it


def kit_dir(package: str, kind: str) -> str:
    """A folder of the kit, ``~`` not expanded: ``kind`` (``settings``,
    ``log``) in ``package``'s folder of the kit. The one place the layout is
    spelled; every such path is built here."""
    return f"{KIT}/{package}/{kind}"


def kit_settings(package: str, name: str) -> str:
    """A settings file another kit package owns, ``~`` not expanded: ``name``
    in the ``settings`` folder of ``package``'s folder of the kit."""
    return f"{kit_dir(package, 'settings')}/{name}"


# ---- the bot's root when none is configured: ~/.sessionkit/phone/state/ ----
# The default root moved under the kit. DEFAULT_ROOT is where a
# fresh install keeps its bot; OLD_ROOT is where every bot lived before. A root
# that is configured (DC_ROOT, SESSIONPHONE_ROOT, or one phone.json keeps) is
# never re-pointed: this rule is for the default alone. default_root() is the
# one place it is decided; every reader (root_from, settings, service, dc_env,
# the dc-* tools, web) asks it, and none keeps a copy.
DEFAULT_ROOT = kit_dir("phone", "state")
OLD_ROOT = "~/.sessionphone"

_said_old = ""      # the line default_root said on stderr in this process, once


def _there(path: pathlib.Path) -> bool | None:
    """Is PATH there? None when that cannot be told (an unreadable parent)."""
    try:
        return exists(path)
    except OSError:
        return None


def default_root() -> pathlib.Path:
    """The root with none configured, expanded: DEFAULT_ROOT
    (~/.sessionkit/phone/state/), unless it is not there and OLD_ROOT
    (~/.sessionphone) is: then the old one, where the bot's account still is,
    said ONCE on stderr in this process (old_root_notice). Nothing is moved or
    made here. When both are there the new one wins, so an old root left as a
    symlink to the new one is one root. A new root that cannot be judged
    (unreadable) is the answer, never a reason to read the old one."""
    new = pathlib.Path(DEFAULT_ROOT).expanduser()
    old = pathlib.Path(OLD_ROOT).expanduser()
    if _there(new) is False and _there(old):
        _say_old(old, new)
        return old
    return new


def _say_old(old: pathlib.Path, new: pathlib.Path) -> None:
    global _said_old
    if _said_old:
        return
    _said_old = (f"sessionphone: using the old root {old}: {new} is not there yet "
                 "(nothing is moved; the bot's account stays where it is until "
                 "`sessionphone bot migrate` moves it)")
    print(_said_old, file=sys.stderr)


def old_root_notice() -> str:
    """The line default_root said on stderr in this process because it fell
    back to the old root, else "". A tool whose Python's stderr is not shown
    (bin/dc-env asks dc_env with 2>/dev/null) says this line itself."""
    return _said_old


# ---- the bot's logs: ~/.sessionkit/phone/log/<bot>[/<profile>]/ --------------
# Both of a bot's logs live here: bot.log, the bot's own, and bot.launchd.log,
# launchd's capture of its output (the job's StandardOutPath and
# StandardErrorPath). The folder is per bot, because roots are per bot, and a
# named profile has its own folder inside it: two bots, or two profiles of one
# bot, never write one file.
# That is the kit's folder for a bot under the DEFAULT root (DEFAULT_ROOT or
# OLD_ROOT, judged by path, symlinks resolved: a root named explicitly that is
# the default is the default). A bot under any other root (a test bot, a second
# bot) keeps the same layout in its root, <root>/log/<bot>[/<profile>]/, so it
# never writes into the default bot's log tree; the root is 0700, and a log names
# chats and callers. No root to judge (None) is the kit's folder.
# This is the one rule: the bot's log (dc_env.log_file), the job's plist
# (service.job), the readers (dc_env.log_for_reader: `bot status`, bin/dc-env,
# bin/dc-log) all ask it, each with the root its bot runs under.
# DC_LOG still wins for bot.log. A log the bot wrote before, in its root, is
# never moved or deleted; a reader finds it while it is the only one there.
BOT_LOG = "bot.log"
LAUNCHD_LOG = "bot.launchd.log"
LOG = "log"         # the logs' folder under a root that is not the default


def _resolved(path: str | os.PathLike) -> pathlib.Path:
    """PATH expanded and absolute, symlinks resolved as far as they are there."""
    p = pathlib.Path(path).expanduser()
    try:
        return p.resolve()
    except (OSError, RuntimeError):     # a symlink loop, an unreadable parent
        return pathlib.Path(os.path.abspath(p))


def is_default_root(root: str | os.PathLike) -> bool:
    """Is ROOT the default root, DEFAULT_ROOT or OLD_ROOT, by path (expanded,
    symlinks resolved)? Where it was named (the shell, phone.json, the
    plist) does not matter."""
    r = _resolved(root)
    return r in (_resolved(DEFAULT_ROOT), _resolved(OLD_ROOT))


def log_base(root: str | os.PathLike | None = None) -> pathlib.Path:
    """The folder every bot's log folder is in, expanded: the kit's,
    ~/.sessionkit/phone/log/, for the default root or none (None); else
    <root>/log/."""
    if root is None or is_default_root(root):
        return pathlib.Path(kit_dir("phone", "log")).expanduser()
    return pathlib.Path(root).expanduser() / LOG


def log_dir(bot: str, profile: str = "", root: str | os.PathLike | None = None) -> pathlib.Path:
    """The folder bot BOT's logs are written in, expanded: <log_base(ROOT)>/<bot>/,
    or <that>/<profile>/ for a named PROFILE ("" is the default profile). ROOT
    is the root the bot runs under; None is the kit's folder (log_base).
    Creates nothing (make_log_dir does)."""
    d = log_base(root) / bot
    return d / profile if profile else d


def bot_log(bot: str, profile: str = "", root: str | os.PathLike | None = None) -> pathlib.Path:
    """Where bot BOT (its PROFILE, under ROOT) writes its own log, with no DC_LOG."""
    return log_dir(bot, profile, root) / BOT_LOG


def launchd_log(bot: str, profile: str = "", root: str | os.PathLike | None = None) -> pathlib.Path:
    """Where launchd writes bot BOT's (its PROFILE's, under ROOT) output: the
    plist's StandardOutPath and StandardErrorPath."""
    return log_dir(bot, profile, root) / LAUNCHD_LOG


def make_log_dir(path: pathlib.Path) -> None:
    """Make PATH, a log's folder, and any folder above it that is missing,
    each 0700: a log names chats and callers. A folder that is already there
    is left as it is."""
    missing = []
    d = pathlib.Path(path)
    while not exists(d) and d != d.parent:
        missing.append(d)
        d = d.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            d.chmod(0o700)


# ---- the call sockets' index: ~/.sessionkit/phone/run/<bot>[.<profile>]/ ----
# A bot's call sockets are under its root, and a bot's root can be set in many
# places (DC_ROOT in its job, SESSIONPHONE_ROOT, phone.json, the default), so a
# reader on the same Mac cannot work it out. While its call legs serve, the bot
# keeps a symlink to each socket it bound in ONE fixed folder of the kit, one
# entry folder per bot and profile: <bot> for the default profile, <bot>.<profile>
# for a named one. A reader globs run/*/call-mouth.sock (or call-ears.sock) and
# connects; a link left by a bot that died fails connect() as its socket would.
# This is a stable contract for readers (docs/calls.md). The bot writes it
# (bridge/sockindex.py); nothing else does.
RUN_DIR = kit_dir("phone", "run")


def run_dir() -> pathlib.Path:
    """The call sockets' index, expanded: ~/.sessionkit/phone/run/."""
    return pathlib.Path(RUN_DIR).expanduser()


def run_entry(bot: str, profile: str = "") -> pathlib.Path:
    """Bot BOT's entry folder in the index, expanded: run/<bot>/, or
    run/<bot>.<profile>/ for a named PROFILE ("" is the default profile).
    Creates nothing."""
    return run_dir() / (f"{bot}.{profile}" if profile else bot)


def legacy_or(new: pathlib.Path, old: pathlib.Path) -> pathlib.Path:
    """The log a reader reads: NEW, unless only OLD, where the bot wrote it
    before, is there. A bot that has not started on this version yet still
    writes OLD (and launchd, for a job installed before the move, writes its
    old launchd log until `bot install` runs again); nothing is moved, and a
    reader never goes silently empty."""
    return old if not exists(new) and exists(old) else new


class NotConfigured(Exception):
    """No account has been set up under this root yet. Exit code 2."""


class Unsafe(Exception):
    """The root is readable by others. Exit code 2: refused, not warned."""


ROOT_VAR = "SESSIONPHONE_ROOT"


def root() -> pathlib.Path:
    """The one directory this agent's Delta Chat account lives in (root_from)."""
    return root_from()[0]


def root_from() -> tuple[pathlib.Path, str]:
    """The root, in `bot install`'s order, and where it came from in words:
    SESSIONPHONE_ROOT in this shell (empty counts as unset), else the one
    phone.json (or a drop-in) keeps for this bot (settings.kept), else
    default_root() (~/.sessionkit/phone/state/, or the old ~/.sessionphone
    while only that one is there). A lock in the file is not enforced here: only install,
    which writes, refuses a shell value a file locks. Raises settings.Bad
    for a settings file that cannot be read, or two bots and no name."""
    v = os.environ.get(ROOT_VAR)
    if v:
        return pathlib.Path(v).expanduser(), f"{ROOT_VAR} in this shell"
    # here, not at import: settings imports this module
    from sessionphone import settings
    v, where = settings.kept(ROOT_VAR)
    if v:
        return pathlib.Path(v).expanduser(), settings.shown(where)
    r = default_root()
    if r != pathlib.Path(DEFAULT_ROOT).expanduser():
        return r, f"the old default ({DEFAULT_ROOT} is not there yet)"
    return r, "the default"


def ensure_private(create: bool = False, at: pathlib.Path | None = None) -> None:
    """The root holds the relay password and the keys: 0700, or refuse.

    `setup` makes it 0700; every other verb refuses a root that is wider,
    because a warning is read once and a world-readable key is read forever.
    AT is the root to judge (install's, resolved with its settings file),
    else root().
    """
    r = root() if at is None else at
    if create:
        r.mkdir(parents=True, exist_ok=True, mode=0o700)
        r.chmod(0o700)
        return
    if not r.exists():
        raise NotConfigured(f"no account under {r} - run `sessionphone setup` first")
    mode = stat.S_IMODE(r.stat().st_mode)
    if mode & 0o077:
        raise Unsafe(f"{r} is mode {mode:04o}; it holds the account keys and must be 0700 - `chmod 700 {r}`")

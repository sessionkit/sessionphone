"""Where sessionphone keeps one agent's Delta Chat life: one root, 0700.

Everything hangs off `SESSIONPHONE_ROOT` (default `~/.sessionkit/phone/state`,
the bot's state folder of the kit), so two agents on one Mac are two roots and
never one shared account. A root `bot install` remembers (in phone.json) is
read here too, after this shell's. With no root configured, a Mac whose bot
still lives in the old default, `~/.sessionphone`, keeps it there until it is
moved (default_root). The bot (the resident bridge), the setup verbs and the
dc-* tools read the same paths from here: this is the one place that rule is
kept.
The bot's logs are the one thing out of the root: they are in its own folder
of the kit, ~/.sessionkit/phone/log/<bot>[/<profile>]/ (log_dir).
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
# Both of a bot's logs live here, out of its root: bot.log, the bot's own, and
# bot.launchd.log, launchd's capture of its output (the job's StandardOutPath
# and StandardErrorPath). The folder is per bot, because roots are per bot,
# and a named profile has its own folder inside it: two bots, or two profiles
# of one bot, never write one file. This is the one rule: the bot's log
# (dc_env.log_file), the job's plist (service.job), the readers
# (dc_env.log_for_reader: `bot status`, bin/dc-env, bin/dc-log) all ask it.
# DC_LOG still wins for bot.log. A log the bot wrote before, in its root, is
# never moved or deleted; a reader finds it while it is the only one there.
BOT_LOG = "bot.log"
LAUNCHD_LOG = "bot.launchd.log"


def log_dir(bot: str, profile: str = "") -> pathlib.Path:
    """The folder bot BOT's logs are written in, expanded:
    ~/.sessionkit/phone/log/<bot>/, or <that>/<profile>/ for a named PROFILE
    ("" is the default profile). Creates nothing (make_log_dir does)."""
    d = pathlib.Path(kit_dir("phone", "log")).expanduser() / bot
    return d / profile if profile else d


def bot_log(bot: str, profile: str = "") -> pathlib.Path:
    """Where bot BOT (its PROFILE) writes its own log, with no DC_LOG."""
    return log_dir(bot, profile) / BOT_LOG


def launchd_log(bot: str, profile: str = "") -> pathlib.Path:
    """Where launchd writes bot BOT's (its PROFILE's) output: the plist's
    StandardOutPath and StandardErrorPath."""
    return log_dir(bot, profile) / LAUNCHD_LOG


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

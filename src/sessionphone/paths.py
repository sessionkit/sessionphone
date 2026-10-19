"""Where sessionphone keeps one agent's Delta Chat life: one root, 0700.

Everything hangs off `SESSIONPHONE_ROOT` (default `~/.sessionphone`), so two
agents on one Mac are two roots and never one shared account. The bot (the
resident bridge) and the setup verbs read the same paths from here.
"""
from __future__ import annotations

import errno
import os
import pathlib
import stat

DEFAULT_ROOT = "~/.sessionphone"


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


def kit_settings(package: str, name: str) -> str:
    """A settings file another kit package owns, ``~`` not expanded: ``name``
    in the ``settings`` folder of ``package``'s folder of the kit. The one
    place the layout is spelled; every such path is built here."""
    return f"~/.sessionkit/{package}/settings/{name}"


class NotConfigured(Exception):
    """No account has been set up under this root yet. Exit code 2."""


class Unsafe(Exception):
    """The root is readable by others. Exit code 2: refused, not warned."""


def root() -> pathlib.Path:
    """The one directory this agent's Delta Chat account lives in."""
    return pathlib.Path(os.environ.get("SESSIONPHONE_ROOT", DEFAULT_ROOT)).expanduser()


def accounts_dir() -> pathlib.Path:
    """The rpc-server's accounts directory: the keys, in a plain SQLite file."""
    return root() / "accounts"


def invite_path() -> pathlib.Path:
    """The invite link `setup` saved: what a phone scans to reach this agent."""
    return root() / "invite.txt"


def callers_path() -> pathlib.Path:
    """The approved callers: one address per line, `#` starts a comment."""
    return root() / "callers.txt"


def ensure_private(create: bool = False) -> None:
    """The root holds the relay password and the keys: 0700, or refuse.

    `setup` makes it 0700; every other verb refuses a root that is wider,
    because a warning is read once and a world-readable key is read forever.
    """
    r = root()
    if create:
        r.mkdir(parents=True, exist_ok=True, mode=0o700)
        r.chmod(0o700)
        return
    if not r.exists():
        raise NotConfigured(f"no account under {r} - run `sessionphone setup` first")
    mode = stat.S_IMODE(r.stat().st_mode)
    if mode & 0o077:
        raise Unsafe(f"{r} is mode {mode:04o}; it holds the account keys and must be 0700 - `chmod 700 {r}`")

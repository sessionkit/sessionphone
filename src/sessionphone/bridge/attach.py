"""attach - what the bridge does with a posted file: judge it, save it, record a refusal.

Three pieces, each used by the bot (PHONE-283, design.md sections 2, 3, 5-7):

- **The policy.** kind_of() maps a message's view type to one of the kinds
  `download_types` names (settings.KINDS). decide() judges a kind and a size
  against the host's download policy (settings.Policy) and answers None (pass
  it on) or a Reason (`size` or `type`). It is pure: no file, no account.
- **The save.** save() copies a file the policy passed on to
  <save_dir>/chat-<chat id>/<msg id>-<name>, or, for a stranger's file, to
  <save_dir>/untrusted/chat-<chat id>/<msg id>-<name>. Folders 0700, files
  0600, never a link to core's blob (core may delete it).
- **The registry.** <home>/not-downloaded.json (0600) records every message
  this bot mailed as not downloaded, keyed by msg id, so `dc-fetch` fetches
  only what this bot itself left undownloaded.

The kind comes from the view type alone, never from file_mime: a sender
chooses the MIME. This module imports no Delta Chat code.
"""
from __future__ import annotations

import contextlib
import datetime
import errno
import fcntl
import json
import os
import pathlib
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass

# ---- the kind of a file: from its view type alone ---------------------------
# Every view type core 2.62.0 names, and the kind download_types calls it.
# Text and Call carry no file and are never judged (None). Any other view type,
# Unknown or one a later core adds, is a `file`.
KIND_OF_VIEW = {
    "image": "image", "gif": "image", "sticker": "sticker", "voice": "voice",
    "audio": "audio", "video": "video", "file": "file", "webxdc": "webxdc",
    "vcard": "vcard",
}
NO_FILE_VIEWS = frozenset({"text", "call"})
OTHER_KIND = "file"


def _view_name(view_type: object) -> str:
    """A view type as its core name, lower case: `image`. Takes the client's
    ViewType (a str enum whose str() is `ViewType.IMAGE`) or the plain string
    a snapshot or the JSON gives."""
    v = getattr(view_type, "value", view_type)
    name = "" if v is None else str(v)
    return name.removeprefix("ViewType.").strip().lower()


def kind_of(view_type: object) -> str | None:
    """The kind (settings.KINDS) of a message with this view type, or None for
    a message that carries no file (Text, Call). Gif is an image; Unknown, a
    missing view type, or one this table does not name is a `file`."""
    name = _view_name(view_type)
    if name in NO_FILE_VIEWS:
        return None
    return KIND_OF_VIEW.get(name, OTHER_KIND)


# ---- the policy: one decision, used at every point --------------------------
SIZE, TYPE = "size", "type"


@dataclass(frozen=True)
class Reason:
    """Why a file is not passed on: `size` (over LIMIT bytes) or `type` (KIND
    is not in download_types). SIZE is the file's, when known."""
    why: str
    kind: str | None = None
    size: int | None = None
    limit: int | None = None


def decide(kind: str | None, size: int | None, policy) -> Reason | None:
    """None if a file of KIND and SIZE bytes is passed on under POLICY (a
    settings.Policy, or anything with `.limit` and `.types`), else the Reason.

    KIND None means the kind is not known yet (a pending message, whose view
    type is Text until its file lands): the size rule alone is applied. SIZE
    None means the size is not known, and the size rule passes it. A limit of
    0 is no limit. The limit is the largest size passed on: a file of exactly
    LIMIT bytes passes. A kind that is off is refused whatever its size."""
    if kind is not None and kind not in policy.types:
        return Reason(TYPE, kind=kind, size=size)
    if policy.limit and size is not None and size > policy.limit:
        return Reason(SIZE, kind=kind, size=size, limit=policy.limit)
    return None


# ---- the save: <save_dir>/[untrusted/]chat-<id>/<msg id>-<name> -------------
UNTRUSTED_DIR = "untrusted"
NAME_BYTES = 120            # the name after `<msg id>-`, extension kept
EXT_BYTES = 16              # a longer "extension" is part of the name, cut with it
EMPTY_NAME = "file"
DIR_MODE, FILE_MODE = 0o700, 0o600
_CHUNK = 1 << 20


class Refused(OSError):
    """The save was refused: a symlink, or a folder this user does not own, where
    the bridge keeps its folders. The caller logs it and names the blob instead."""


def one_line(name: object) -> str:
    """A sender's file name on ONE line, exactly as bot.one_line makes it: each
    whitespace or unprintable character becomes a space, a run of them one
    space. (Kept here so this module needs no Delta Chat import; the bridge
    tests pin that the two agree.)"""
    spaced = "".join(c if c.isprintable() and not c.isspace() else " " for c in str(name))
    return " ".join(spaced.split())


def _cut(text: str, limit: int) -> str:
    """TEXT cut to at most LIMIT bytes of UTF-8, never inside a character."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def safe_name(file_name: object) -> str:
    """The name a saved copy is given after its `<msg id>-`: the sender's name on
    one line (one_line), `/`, `\\` and NUL made `_`, leading dots stripped (no
    hidden file, no `..`), cut to NAME_BYTES with its extension kept. An empty
    name is `file`. The result never holds a path separator, so it stays in
    its folder."""
    name = one_line(file_name or "")
    for c in ("/", "\\", "\0"):
        name = name.replace(c, "_")
    name = name.lstrip(". ").rstrip()  # `. .x` and `. ..` too: dots and spaces mixed
    if not name:
        return EMPTY_NAME
    if len(name.encode("utf-8")) <= NAME_BYTES:
        return name
    stem, ext = os.path.splitext(name)
    if not stem or len(ext.encode("utf-8")) > EXT_BYTES:
        stem, ext = name, ""
    stem = _cut(stem, NAME_BYTES - len(ext.encode("utf-8"))).rstrip()
    return (stem or EMPTY_NAME) + ext


def _numbered(name: str, n: int) -> str:
    """NAME with `-N` before its extension: `1-notes-2.pdf`."""
    if n < 2:
        return name
    stem, ext = os.path.splitext(name)
    return f"{stem}-{n}{ext}"


def _open_own_dir(parent: int, name: str) -> int:
    """The folder NAME under the folder PARENT (a fd), made 0700 when it is not
    there, and opened O_NOFOLLOW: a symlink there, something that is not a
    folder, or a folder another user owns is Refused. -> its fd."""
    try:
        os.mkdir(name, DIR_MODE, dir_fd=parent)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refused(e.errno, f"{name} is not a folder of this bot's (a symlink, or a file)",
                          name) from e
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(errno.ENOTDIR, f"{name} is not a folder", name)
        if st.st_uid != os.getuid():
            raise Refused(errno.EPERM, f"{name} belongs to uid {st.st_uid}, not this user", name)
        if stat.S_IMODE(st.st_mode) != DIR_MODE:
            os.fchmod(fd, DIR_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _same_bytes(fd_a: int, fd_b: int) -> bool:
    """Do two open files hold the same bytes? Each is read from its start."""
    if os.fstat(fd_a).st_size != os.fstat(fd_b).st_size:
        return False
    off = 0
    while True:
        a, b = os.pread(fd_a, _CHUNK, off), os.pread(fd_b, _CHUNK, off)
        if a != b:
            return False
        if not a:
            return True
        off += len(a)


# open() of a symlink under O_NOFOLLOW (ELOOP), or of a socket (ENXIO on Linux,
# EOPNOTSUPP on macOS): the name is taken by something that is not our copy.
_TAKEN_ERRNOS = frozenset({errno.ELOOP, errno.ENXIO, errno.EOPNOTSUPP})


def _holds(folder: int, name: str, blob: int) -> bool | None:
    """Is NAME in FOLDER a copy of BLOB? True (same bytes, a file of ours), False
    (taken by something else), or None (not there). Opened O_NONBLOCK, so a
    FIFO at the name is seen as taken instead of blocking the bridge; a socket
    there (which no open() takes) is taken too."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in _TAKEN_ERRNOS:    # a symlink (never followed) or a socket: taken
            return False
        raise
    try:
        st = os.fstat(fd)
        return (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
                and _same_bytes(fd, blob))
    finally:
        os.close(fd)


def _write_copy(folder: int, name: str, blob: int) -> None:
    """BLOB's bytes to NAME in FOLDER, 0600: written as `.<NAME>.part` (opened
    O_EXCL | O_NOFOLLOW), then renamed, so a watcher never sees half a file. A
    `.part` an earlier run left behind is removed first."""
    part = f".{name}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        out = os.open(part, flags, FILE_MODE, dir_fd=folder)
    except FileExistsError:
        os.unlink(part, dir_fd=folder)
        out = os.open(part, flags, FILE_MODE, dir_fd=folder)
    try:
        os.fchmod(out, FILE_MODE)       # exactly 0600, whatever the umask
        off = 0
        while True:
            chunk = os.pread(blob, _CHUNK, off)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                view = view[os.write(out, view):]
            off += len(chunk)
        os.fsync(out)
    except BaseException:
        os.close(out)
        with contextlib.suppress(OSError):
            os.unlink(part, dir_fd=folder)
        raise
    os.close(out)
    os.rename(part, name, src_dir_fd=folder, dst_dir_fd=folder)


def save_folder(save_dir: str | os.PathLike, chat_id: int, trusted: bool = True) -> pathlib.Path:
    """Where a chat's saved files go: <save_dir>/chat-<id>, or
    <save_dir>/untrusted/chat-<id> for a stranger's (trusted False)."""
    base = pathlib.Path(save_dir)
    return (base if trusted else base / UNTRUSTED_DIR) / f"chat-{int(chat_id)}"


def save(blob: str | os.PathLike, save_dir: str | os.PathLike, chat_id: int, msg_id: int,
         file_name: object, trusted: bool = True) -> pathlib.Path:
    """Copy core's BLOB to save_folder()/<msg id>-<safe_name(FILE_NAME)> and
    return the copy's path.

    SAVE_DIR is made (0700) when it is not there; it may itself be a symlink,
    the host's choice. Below it, `untrusted/` and `chat-<id>/` are the bridge's
    own: made 0700 and opened O_NOFOLLOW, and a symlink or a folder this user
    does not own there is Refused. The file is a copy, 0600, never a link.

    Idempotent per message: a name already holding the same bytes is that
    copy, returned as it is (the same message saved again). A name holding
    other bytes gets `-2`, `-3`, ... before its extension. Raises Refused or
    OSError; the caller logs it and names the blob."""
    name = f"{int(msg_id)}-{safe_name(file_name)}"
    base = pathlib.Path(save_dir)
    os.makedirs(base, mode=DIR_MODE, exist_ok=True)
    src = os.open(blob, os.O_RDONLY)
    fds: list[int] = []
    try:
        fds.append(os.open(base, os.O_RDONLY | os.O_DIRECTORY))
        if not trusted:
            fds.append(_open_own_dir(fds[-1], UNTRUSTED_DIR))
        fds.append(_open_own_dir(fds[-1], f"chat-{int(chat_id)}"))
        folder = fds[-1]
        n = 1
        while True:
            cand = _numbered(name, n)
            held = _holds(folder, cand, src)
            if held:
                return save_folder(base, chat_id, trusted) / cand
            if held is None:
                _write_copy(folder, cand, src)
                return save_folder(base, chat_id, trusted) / cand
            n += 1
    finally:
        for fd in reversed(fds):
            os.close(fd)
        os.close(src)


def try_save(blob, save_dir, chat_id, msg_id, file_name, trusted=True,
             log: Callable[[str], object] | None = None) -> pathlib.Path | None:
    """save(), or None when it fails (disk full, permissions, Refused): the
    failure is logged, and the caller names the blob, as before PHONE-283."""
    try:
        return save(blob, save_dir, chat_id, msg_id, file_name, trusted)
    except OSError as e:
        if log:
            log(f"SAVE FAILED msg {msg_id} under {save_dir}: {e}; the line names core's blob")
        return None


# ---- the registry: <home>/not-downloaded.json -------------------------------
# msg id -> {chat_id, ocr, reason, kind, size, at}, plus `fetching` once a
# dc-fetch has asked for it (mark_fetching). `ocr` is the route: False is a
# stranger's file, and stays one when it is fetched. No other route is kept.
REGISTRY = "not-downloaded.json"
LOCK = ".not-downloaded.lock"
TIMEOUT, FAILURE, UNDECIPHERABLE = "timeout", "failure", "undecipherable"
# Our own reasons, and PHONE-284's partials a second fetch can still bring in.
REASONS = (SIZE, TYPE, TIMEOUT, FAILURE)
# A partial that no fetch can help (a second fetch cannot decrypt it): never recorded.
NOT_RECORDED = frozenset({UNDECIPHERABLE})


def registry_path(home: str | os.PathLike) -> pathlib.Path:
    """The registry of bot HOME (dc_env.home(): its root, or its profile's folder)."""
    return pathlib.Path(home) / REGISTRY


@contextlib.contextmanager
def _locked(home: str | os.PathLike) -> Iterator[None]:
    """The registry's lock, held for one read-change-write (flock: a bot that
    dies leaves no stale lock behind)."""
    fd = os.open(pathlib.Path(home) / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(home: str | os.PathLike) -> dict[str, dict]:
    """Every entry, or {} when there is no registry (or it is not a JSON object)."""
    try:
        data = json.loads(registry_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write(home: str | os.PathLike, data: dict[str, dict]) -> None:
    """DATA to the registry, whole, 0600: written beside it, then os.replace."""
    path = registry_path(home)
    part = path.with_name(f".{REGISTRY}.{os.getpid()}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        os.write(fd, (json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(part, path)


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def record(home: str | os.PathLike, msg_id: int, chat_id: int, ocr: bool, reason: str,
           kind: str | None = None, size: int | None = None, at: str | None = None) -> bool:
    """Record msg MSG_ID as mailed not downloaded. -> True if it was recorded.

    REASON is one of REASONS: `size` or `type` (this bot's policy), `timeout` or
    `failure` (PHONE-284's partials). An `undecipherable` partial is never
    recorded (False): no fetch can help it. Any other reason is a ValueError.
    OCR False is a stranger's file, and is recorded too. The registry is keyed
    by msg id, so recording one message again replaces its entry: one entry
    per message, however often its text is built."""
    reason = str(reason).lower()
    if reason in NOT_RECORDED:
        return False
    if reason not in REASONS:
        raise ValueError(f"not a registry reason: {reason!r} (the reasons are {', '.join(REASONS)})")
    entry = {"chat_id": int(chat_id), "ocr": bool(ocr), "reason": reason, "kind": kind,
             "size": None if size is None else int(size), "at": at or _now()}
    with _locked(home):
        data = _read(home)
        data[str(int(msg_id))] = entry
        _write(home, data)
    return True


def entry(home: str | os.PathLike, msg_id: int) -> dict | None:
    """Msg MSG_ID's entry, or None if this bot did not leave it undownloaded."""
    return _read(home).get(str(int(msg_id)))


def entries(home: str | os.PathLike) -> dict[int, dict]:
    """Every entry, by msg id."""
    return {int(k): v for k, v in _read(home).items() if k.isdigit()}


def drop(home: str | os.PathLike, msg_id: int) -> dict | None:
    """Take msg MSG_ID's entry out, under the lock. -> the entry, or None when
    it was not there: of two callers, exactly one gets it."""
    with _locked(home):
        data = _read(home)
        got = data.pop(str(int(msg_id)), None)
        if got is not None:
            _write(home, data)
    return got


# ---- a fetch: dc-fetch marks its entry, and the mail of the message takes it -----
# `fetching` is the one mark a fetch puts on an entry (design §7, cases 4 and 5):
# the agent asked for this message, so the text built for it next bypasses the
# policy, once. record() never writes the mark, so a partial built again (a
# fetch that timed out, or failed) replaces the entry without it, and the entry
# stays for a second dc-fetch.
FETCHING = "fetching"


def mark_fetching(home: str | os.PathLike, msg_id: int) -> dict | None:
    """Mark msg MSG_ID's entry `fetching`, under the lock. -> the entry as it now
    stands, or None when there is no entry to mark (nothing is written)."""
    with _locked(home):
        data = _read(home)
        got = data.get(str(int(msg_id)))
        if got is None:
            return None
        got[FETCHING] = True
        _write(home, data)
    return got


def take_fetching(home: str | os.PathLike, msg_id: int) -> dict | None:
    """Take msg MSG_ID's entry out if a fetch marked it, under the lock. -> the
    entry, or None (no entry, or one no fetch marked: left as it is). Of two
    callers, exactly one gets it. An entry no fetch marked is seen without the
    lock, so the text of an ordinary message never takes it."""
    key = str(int(msg_id))
    if not _read(home).get(key, {}).get(FETCHING):
        return None
    with _locked(home):
        data = _read(home)
        got = data.get(key)
        if not (got and got.get(FETCHING)):
            return None
        del data[key]
        _write(home, data)
    return got


def reason_for(reason: Reason) -> dict:
    """A policy Reason as record()'s keyword arguments: reason, kind, size."""
    return {"reason": reason.why, "kind": reason.kind, "size": reason.size}

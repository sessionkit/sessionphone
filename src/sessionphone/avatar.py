"""avatar - the bot's Delta Chat picture (`selfavatar`): show, set or clear it.

    sessionphone avatar              its blob path, or "none"
    sessionphone avatar PATH         set it
    sessionphone avatar --clear      remove it

Measured on core 2.62.0: core copies the file into the account's blob dir and
`selfavatar` names that copy. It decodes PNG, JPEG, GIF and BMP only. It copies
a file it then refuses into the blob dir first, so each refusal leaves an
orphan blob. So the file is checked HERE, by its first bytes, before any
account is opened. Core is still the last word, and a file it refuses leaves
the old picture in place.

Only the process that holds the account can set it: a second rpc-server on the
same accounts dir fails to start ("accounts.lock file is already locked").
- Account free: open it, set_avatar, close, as `setup` does.
- Held by the running bot: an outbox job, {"avatar": "/abs/path" | null,
  "ack": PATH}, judged by jobs.check first (a bot older than this refuses with
  the "restart the bot" text, and nothing is queued). The bot acks with the
  blob path, and this waits for that ack as the dc-* tools do.
- Held by `link` (or anything that is not a bot that reads this job): refused.

Setting it sends nothing. The picture travels with the account's next message.

The default picture: a slot, `images/default-avatar.png` (or `.jpg`) beside this
module, package data. `default_path()` finds it, None when nothing ships there.
The bot sets it at its start (`at_start`, it holds the account then), only when
one ships, the account has no picture, and the user has not chosen none:
`avatar --clear` leaves a marker under the root (`avatar-none`) and `avatar PATH`
removes it, so the user's choice wins. `bot install` only says so
(`install_line`).
"""
from __future__ import annotations

import json
import os
import pathlib
import time

from sessionphone import account as account_mod
from sessionphone import jobs, paths
from sessionphone.bridge import dc_env

# exit codes: the CLI's scheme, plus the dc-* meaning of 3 and 4
DONE, NOT_CONFIGURED, REFUSED, NOT_TAKEN, NOT_CONFIRMED = 0, 1, 2, 3, 4

# The four formats core 2.62.0 decodes, by their first bytes (no new dependency).
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
)
SNIFF = 8               # the longest signature above

TAKEN_WAIT = 10.0       # seconds for the bot to take the job (it polls each second): dc-env's dc_taken
ACK_WAIT = 1.0          # then seconds for its ack (written BEFORE the job is unlinked): dc_ack
POLL = 0.1

IMAGES_DIR = pathlib.Path(__file__).resolve().parent / "images"
DEFAULT_NAMES = ("default-avatar.png", "default-avatar.jpg")    # the first that is a file wins
NONE_MARKER = "avatar-none"     # under the root: the user ran `avatar --clear`

SHOWS = "it shows after this account's next message; nothing was sent"


class Failed(Exception):
    """A refusal or failure, with the exit code the CLI gives it."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def sniff(head: bytes) -> str | None:
    """The format HEAD's first bytes name: PNG, JPEG, GIF or BMP, else None."""
    for sig, name in MAGIC:
        if head.startswith(sig):
            return name
    return None


def precheck(path: str | os.PathLike) -> pathlib.Path:
    """-> PATH, absolute, if core can be given it. Else Failed(2), and nothing is touched:
    it exists, is a regular file, is readable, and its first bytes are PNG, JPEG,
    GIF or BMP (the magic bytes, never the extension)."""
    p = pathlib.Path(path).expanduser()
    try:        # paths.*, not Path.*: on 3.14 a path below an unsearchable dir reads "absent"
        there, regular = paths.exists(p), paths.is_file(p)
    except OSError as exc:
        raise Failed(REFUSED, f"cannot read {p}: {exc.strerror or exc}; nothing was changed") from exc
    if not there:
        raise Failed(REFUSED, f"no such file: {p}; nothing was changed")
    if not regular:
        raise Failed(REFUSED, f"not a regular file: {p}; nothing was changed")
    try:
        with open(p, "rb") as fh:
            head = fh.read(SNIFF)
    except OSError as exc:
        raise Failed(REFUSED, f"cannot read {p}: {exc.strerror or exc}; nothing was changed") from exc
    if sniff(head) is None:
        raise Failed(REFUSED, f"not a PNG, JPEG, GIF or BMP image (by its first bytes): {p}; "
                              "nothing was changed")
    return p.resolve()


def held(exc: BaseException) -> bool:
    """Did opening the account fail because another process holds it?"""
    text = str(exc)
    return "accounts.lock" in text and "already locked" in text


def _root() -> pathlib.Path:
    """The bot's home, read now (never dc_env.ROOT, frozen at import): the root,
    or <root>/<DC_PROFILE> for a named profile (dc_env.home)."""
    return dc_env.home()


def outbox() -> pathlib.Path:
    """The default profile's outbox, as bin/dc-env's dc_outbox finds it."""
    o = dc_env.env("OUTBOX")
    return pathlib.Path(o).expanduser() if o else _root() / "outbox"


def acks_dir() -> pathlib.Path:
    a = dc_env.env("ACKS")
    return pathlib.Path(a).expanduser() if a else _root() / "acks"


def default_path(images: pathlib.Path | None = None) -> pathlib.Path | None:
    """The default picture that ships with sessionphone, or None when none does:
    the first of DEFAULT_NAMES that is a regular file in IMAGES (the package's
    images/ directory)."""
    d = IMAGES_DIR if images is None else images
    for n in DEFAULT_NAMES:
        p = d / n
        try:
            if paths.is_file(p):
                return p
        except OSError:
            continue
    return None


def marker(root: pathlib.Path | None = None) -> pathlib.Path:
    """The file that records that the user chose no picture (`avatar --clear`)."""
    return (_root() if root is None else pathlib.Path(root)) / NONE_MARKER


def chose_none(root: pathlib.Path | None = None) -> bool:
    """Did the user clear the picture (and not set one since)?"""
    try:
        return paths.exists(marker(root))
    except OSError:
        return True     # unreadable: never put a picture back on a guess


def record_choice(cleared: bool, root: pathlib.Path | None = None) -> None:
    """After `avatar --clear` (CLEARED) write the marker, 0600; after `avatar PATH`
    remove it. Raises OSError."""
    m = marker(root)
    if cleared:
        fd = os.open(m, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"the bot's picture was cleared by `sessionphone avatar --clear`; "
                         b"the bot never sets the default picture while this file exists\n")
        finally:
            os.close(fd)
    else:
        try:
            m.unlink()
        except FileNotFoundError:
            pass


def at_start(acct, root: pathlib.Path, images: pathlib.Path | None = None) -> str | None:
    """The bot's start, holding ACCT: set the shipped default picture when one
    ships, ACCT has none, and the user has not cleared it under ROOT. -> the one
    line to log, or None when no default ships (nothing is read or changed).
    A picture already there, or the user's --clear, is never changed. Raises
    whatever core raises; the bot logs it and carries on."""
    d = default_path(images)
    if d is None:
        return None
    if chose_none(root):
        return "picture: none (cleared with `sessionphone avatar --clear`); the default is not set"
    if acct.get_avatar():
        return "picture: kept (the account has one)"
    try:
        f = precheck(d)
    except Failed as exc:
        return f"picture: default not set: {exc}"
    acct.set_avatar(str(f))
    return f"picture: default set ({d.name})" if acct.get_avatar() else \
        f"picture: default not set: Delta Chat kept no picture from {d.name}"


def install_line(images: pathlib.Path | None = None) -> str | None:
    """`bot install`'s `picture:` line, or None when no default ships."""
    d = default_path(images)
    if d is None:
        return None
    return (f"picture:   the bot sets the default picture ({d.name}) at its start when the account "
            "has none and you have not cleared it (`sessionphone avatar PATH` sets your own)")


# ---- the account, free ----------------------------------------------------


def _with_account(work):
    """Run WORK(account) on the account opened here. -> its result, or None when
    the account is HELD by another process. NotConfigured -> Failed(1)."""
    try:
        paths.ensure_private()
    except paths.NotConfigured as exc:
        raise Failed(NOT_CONFIGURED, str(exc)) from exc
    except paths.Unsafe as exc:
        raise Failed(REFUSED, str(exc)) from exc
    gen = account_mod._open()
    try:
        try:
            acct = next(gen)
        except paths.NotConfigured as exc:
            raise Failed(NOT_CONFIGURED, str(exc)) from exc
        except StopIteration as exc:    # _open yields once or raises; never seen
            raise Failed(NOT_CONFIGURED, f"no account under {paths.root()}") from exc
        except Exception as exc:        # the lock is the one kind routed; the rest propagate
            if held(exc):
                return None
            raise
        return work(acct)
    finally:
        gen.close()     # the rpc-server is shut down here, never left to the GC


# ---- the account, held by the bot ------------------------------------------


def _held_text(box: pathlib.Path) -> str:
    rec = jobs.read_stamp(box)
    if rec is not None and jobs.pid_alive(rec.get("pid")):
        return f"the running bot (pid {rec['pid']}) holds the account"
    return ("another process holds the account, and it is not a bot that reads this job: if "
            "`sessionphone link` is running, wait for it to finish (up to 10 minutes)")


def queue(value: str | None, box: pathlib.Path | None = None, acks: pathlib.Path | None = None,
          taken_wait: float | None = None, ack_wait: float | None = None) -> str:
    """Hand {"avatar": VALUE} to the bot that holds the account. -> the ack's detail
    (the blob path, or "none"). Failed(3): jobs.check refused it (nothing queued),
    no bot took it (withdrawn), or the bot said no. Failed(4): taken, no ack."""
    taken_wait = TAKEN_WAIT if taken_wait is None else taken_wait
    ack_wait = ACK_WAIT if ack_wait is None else ack_wait
    box = outbox() if box is None else box
    acks = acks_dir() if acks is None else acks
    stem = f"{time.time():.6f}"
    ack_path = acks / f"{stem}-sessionphone-avatar-{os.getpid()}.json"
    job = {"avatar": value, "ack": str(ack_path)}
    refusal = jobs.check(box, job)
    if refusal:
        raise Failed(NOT_TAKEN, f"{_held_text(box)}. {refusal}")
    box.mkdir(parents=True, exist_ok=True, mode=0o700)
    acks.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = box / f"{stem}.json"
    part = box / f"{stem}.json.part"        # not *.json: the bot never takes half a job
    part.write_text(json.dumps(job) + "\n", encoding="utf-8")
    os.replace(part, f)
    end = time.monotonic() + taken_wait
    # paths.exists, never Path.exists: on 3.14 an unsearchable box or acks dir would
    # read "taken" or "acked" and end these waits early, where 3.13 raises.
    while paths.exists(f) and time.monotonic() < end:
        time.sleep(POLL)
    if paths.exists(f):
        try:
            f.unlink()      # withdrawn: a picture applied later, unasked, is worse than none
        except FileNotFoundError:
            pass            # taken just now: wait for its ack below
        else:
            # The bot reads a job, acts, acks, and only then unlinks it, so a job it
            # read just before this unlink still runs: its ack, not the unlink, decides.
            end = time.monotonic() + ack_wait
            while not paths.exists(ack_path) and time.monotonic() < end:
                time.sleep(POLL)
            if not paths.exists(ack_path):
                raise Failed(NOT_TAKEN, f"NOT TAKEN: no bot took the job in {taken_wait:g}s from {box}; "
                                        "it was withdrawn and nothing was changed")
    end = time.monotonic() + ack_wait
    while not paths.exists(ack_path) and time.monotonic() < end:
        time.sleep(POLL)
    if not paths.exists(ack_path):
        raise Failed(NOT_CONFIRMED, f"NOT CONFIRMED (no ack): the bot took the job and wrote no ack "
                                    f"in {ack_wait:g}s, so whether the picture changed is unknown. "
                                    f"Its log: {dc_env.log_for_reader()}")
    try:
        ans = json.loads(ack_path.read_text(encoding="utf-8"))
        ok, detail = bool(ans.get("ok")), str(ans.get("detail", ""))
    except (OSError, ValueError, AttributeError) as exc:
        raise Failed(NOT_CONFIRMED, f"NOT CONFIRMED: unreadable ack {ack_path}: {exc}") from exc
    if not ok:
        raise Failed(NOT_TAKEN, f"the bot did not take it: {detail}; the old picture stays")
    return detail


# ---- the verbs ----------------------------------------------------------------


def show() -> str | None:
    """The current picture's blob path, or None. Failed(3) while another process
    holds the account: only the holder can read it."""
    found: list[str | None] = []

    def work(acct):
        found.append(acct.get_avatar() or None)
        return True

    if _with_account(work) is None:
        raise Failed(NOT_TAKEN, f"{_held_text(outbox())}, so its picture cannot be read from here; "
                                "the picture is unchanged")
    return found[0]


def apply(path: str | os.PathLike | None, **wait) -> tuple[str | None, str]:
    """Set the picture to PATH, or clear it (None). -> (blob path or None, how:
    "account" or "bot"). The file is pre-checked BEFORE any account is opened."""
    value = None if path is None else str(precheck(path))

    def work(acct):
        try:
            acct.set_avatar(value)
        except Exception as exc:   # core's refusal, passed on word for word
            raise Failed(REFUSED, f"Delta Chat refused it: {exc}; the old picture stays") from exc
        return acct.get_avatar() or None, "account"

    done = _with_account(work)
    if done is not None:
        return done
    detail = queue(value, **wait)
    return (None if detail in ("", "none") else detail), "bot"

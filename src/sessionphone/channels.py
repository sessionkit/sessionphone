"""channels - where the bot may send: a plain file the owner edits, failing closed.

This is the one gate. The bot's outbox loop, its in-call posts and
`sessionphone channels` all share this parser, as `read_callers` is shared
today.

The file is `channels.txt` in the bot's root, mode 0600; `DC_CHANNELS_FILE`
overrides it. One entry per line, and `#` starts a comment:

    # NAME    TARGET                key:FPR                                    # note
    owner     owner@relay.example    key:4E4D1E1E160E2C8D034A91AFDCF4D1301E82234F  # owner, joined 2030-06-09
    family    chat:42                                                            # the family group

NAME is lowercase `[a-z0-9_-]+`, unique, and not all digits (a tool always
reads an all-digit argument as a chat id, so such a NAME could never be
reached; it is ignored and logged). TARGET is an *address*, which means the
1:1 chat with that contact and never a group that person is in, or
`chat:N`, one chat by id, the only way to list a group.

An address entry is bound to a key fingerprint (`key:FPR`, as callers.txt
is, see keygate): its 1:1 chat passes only while the contact of that chat
holds the pinned key. The fingerprint is the identity and the address is the
label. An address line with no `key:` (written by an older version) is read
as UNPINNED and refuses until a key is pinned to it (`sessionphone channels
pin NAME`, or once, for the owner line, at the bot's first start: migrate).
A `chat:N` entry takes no key: one chat id is one chat.

It fails closed: a file that is missing, cannot be read, or is writable by
group or world refuses EVERY check, and the detail names the path and the
reason. A malformed or duplicate line is ignored and logged loudly, which can
only narrow the list. The file is re-read on every call, so an edit takes
effect without a restart.

This module sends nothing and wires nothing: the bot calls `check()` before a
send and `resolve()` for a NAME. The dc-* tools call `main()` (`precheck`)
before they queue a job: a refuse-only look at the list, so a send the bot
would refuse exits 3 even when the tool would not wait.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sessionphone import keygate
from sessionphone.bridge import dc_env

NAME_RE = re.compile(r"[a-z0-9_-]+")
CHAT_RE = re.compile(r"chat:([0-9]+)")
ADDR_RE = re.compile(r"[^@\s#]+@[^@\s#]+")
MARKER = "# sessionphone keys v1: an address line without key: is refused until pinned"
SINGLE = ("Single", 100)   # ChatType.SINGLE: a string on the pinned core, 100 on older ones

Log = Callable[[str], None]


def _stderr(msg: str) -> None:
    print(f"channels: {msg}", file=sys.stderr, flush=True)


def channels_path() -> pathlib.Path:
    """The list as the bot sees it: DC_CHANNELS_FILE, else <bot root>/channels.txt."""
    return dc_env.path("CHANNELS_FILE", "channels.txt")


class Refused(Exception):
    """A send the list does not allow. str() is the detail, naming the path."""


@dataclass(frozen=True)
class Entry:
    """One listed channel: NAME, a TARGET that is an address or chat:N, and
    for an address the key fingerprint it is bound to (None: unpinned, refused)."""

    name: str
    target: str
    note: str = ""
    key: str | None = None

    @property
    def chat_id(self) -> int | None:
        """N for a `chat:N` entry, else None."""
        m = CHAT_RE.fullmatch(self.target)
        return int(m.group(1)) if m else None

    @property
    def address(self) -> str | None:
        """The contact's address for an address entry, else None."""
        return None if self.chat_id is not None else self.target


@dataclass
class Listing:
    """The file as read. `refusal` set means every check is refused, and `kind`
    says why in one word: `missing`, `unreadable` or `loose`. The owner's alert
    is keyed on `kind`, so a reworded `refusal` is never a new reason."""

    path: pathlib.Path
    entries: dict[str, Entry] = field(default_factory=dict)
    refusal: str | None = None
    kind: str | None = None

    @property
    def chat_ids(self) -> set[int]:
        return {e.chat_id for e in self.entries.values() if e.chat_id is not None}

    @property
    def addresses(self) -> set[str]:
        return {e.address for e in self.entries.values() if e.address is not None}


@dataclass(frozen=True)
class Verdict:
    """check()'s answer. Truthy only when the send may go; `detail` is the ack text."""

    ok: bool
    detail: str
    name: str | None = None
    reason: str = ""    # the short why, for a log line: "not in <path>"

    def __bool__(self) -> bool:
        return self.ok


def parse_target(target: str) -> str | None:
    """TARGET normalised (an address lowercased), or None if it is neither form."""
    t = target.strip()
    m = CHAT_RE.fullmatch(t)
    if m:
        return t if int(m.group(1)) > 0 else None
    if ADDR_RE.fullmatch(t):
        return t.lower()
    return None


_IGNORED_SEEN: set[str] = set()   # each IGNORED line is logged once per process, not per job


def parse(text: str, path: pathlib.Path, log: Log | None = None) -> dict[str, Entry]:
    """The entries in TEXT, first NAME wins. A bad or duplicate line is logged and dropped.

    The bot re-reads the file on every job, so a line that stays bad would be
    logged on every job: each distinct IGNORED message is logged once.
    """
    log = log or _stderr
    out: dict[str, Entry] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        body, _, note = raw.partition("#")
        words = body.split()
        if not words:
            continue
        bad = None
        key = keygate.file_key(words[2]) if len(words) == 3 else None
        if len(words) not in (2, 3):
            bad = f"want NAME TARGET [key:FPR], got {len(words)} word(s)"
        elif not NAME_RE.fullmatch(words[0]):
            bad = f"NAME {words[0]!r} is not lowercase [a-z0-9_-]+"
        elif words[0].isdigit():
            bad = f"NAME {words[0]!r} is all digits, and a tool always reads that as a chat id"
        elif words[0].startswith("-"):
            bad = f"NAME {words[0]!r} starts with '-', and a tool would read that as an option"
        elif parse_target(words[1]) is None:
            bad = f"TARGET {words[1]!r} is neither an address nor chat:N"
        elif len(words) == 3 and CHAT_RE.fullmatch(words[1].strip()):
            bad = "a chat:N entry takes no key:"
        elif len(words) == 3 and key is None:
            bad = f"{words[2]!r} is not key:FPR (40 or 64 hex digits)"
        elif words[0] in out:
            bad = f"NAME {words[0]!r} is already listed (first one wins)"
        if bad:
            msg = f"IGNORED line {n} of {path}: {bad}: {raw.strip()!r}"
            if msg not in _IGNORED_SEEN:
                _IGNORED_SEEN.add(msg)
                log(msg)
            continue
        out[words[0]] = Entry(words[0], parse_target(words[1]) or "", note.strip(), key)
    return out


def load(path: pathlib.Path | None = None, log: Log | None = None) -> Listing:
    """Read the list. Never raises: a missing, unreadable or loose file sets `refusal`."""
    path = pathlib.Path(path) if path else channels_path()
    # One open, then fstat and read that same fd: the mode checked is the
    # file read, with no stat-then-read race. O_NONBLOCK so a FIFO put in
    # its place cannot hang the bot; it is refused as not a regular file.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return Listing(path, refusal=f"NO channels file at {path}", kind="missing")
    except OSError as exc:
        return Listing(path, refusal=f"cannot read {path}: {exc.strerror or exc}", kind="unreadable")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return Listing(path, refusal=f"cannot read {path}: not a regular file", kind="unreadable")
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o022:
            return Listing(path, refusal=(
                f"{path} is mode {mode:04o}, writable by group or world; it must be 0600 - `chmod 600 {path}`"),
                kind="loose")
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        return Listing(path, refusal=f"cannot read {path}: {reason}", kind="unreadable")
    finally:
        os.close(fd)
    return Listing(path, parse(text, path, log))


def _chat_id(chat_id: object) -> int | None:
    if isinstance(chat_id, bool):
        return None
    try:
        n = int(chat_id)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return n if n > 0 and str(n) == str(chat_id).strip() else None


def _single_contact(account, chat_id: int):
    """The other party's Contact if CHAT_ID is a 1:1 chat, else None. Errors propagate."""
    chat = account.get_chat_by_id(chat_id)
    if getattr(chat.get_basic_snapshot(), "chat_type", None) not in SINGLE:
        return None
    contacts = chat.get_contacts()
    if len(contacts) != 1:
        return None
    return contacts[0]


PIN_HOW = "sessionphone channels pin {name} --key FPR"


def check(account, chat_id, path: pathlib.Path | None = None, log: Log | None = None,
          listing: Listing | None = None) -> Verdict:
    """May the bot send to CHAT_ID on ACCOUNT? Deny by default.

    A `chat:N` entry passes exactly chat N. An address entry passes only the
    1:1 chat of a contact holding the key it is pinned to, never a group that
    contact is in; an unpinned one passes nothing. Anything that cannot be
    read refuses. LISTING, when given, is a load() the caller already did
    for this job (the bot reads the file once per job, not once per check).
    """
    listing = listing or load(path, log)
    if listing.refusal:
        return Verdict(False, f"REFUSED: {listing.refusal}: every send is refused; nothing was sent.",
                       reason=listing.refusal)
    n = _chat_id(chat_id)
    if n is None:
        return Verdict(False, (f"REFUSED: {chat_id!r} is not a chat id, so nothing on the allow-list "
                               f"{listing.path} can pass it; nothing was sent."),
                       reason=f"{chat_id!r} is not a chat id, so nothing in {listing.path} can pass it")
    for e in listing.entries.values():
        if e.chat_id == n:
            return Verdict(True, f"chat {n} is listed as {e.name}", e.name)
    if listing.addresses:
        try:
            v = _check_single(account, n, listing, log or _stderr)
        except Exception as exc:  # noqa: BLE001 - any core error fails closed
            return Verdict(False, (f"REFUSED: could not read chat {n} to check it against "
                                   f"{listing.path} ({exc}); nothing was sent."),
                           reason=f"could not read the chat to check it against {listing.path} ({exc})")
        if v is not None:
            return v
    return Verdict(False, (f"REFUSED: chat {n} is not on the allow-list {listing.path}; nothing was sent. "
                           f"Add it: sessionphone channels add NAME chat:{n}"),
                   reason=f"not in {listing.path}")


def _check_single(account, n: int, listing: Listing, log: Log) -> Verdict | None:
    """The address entries' verdict on chat N, or None when no address entry
    is about it (then it is simply not listed). The key decides: the chat's
    contact passes as the entry pinned to the key it holds, whatever address it
    writes now. Errors propagate (check() refuses on them)."""
    contact = _single_contact(account, n)
    if contact is None:
        return None
    addr = (contact.get_snapshot().address or "").strip().lower() or None
    entries = [e for e in listing.entries.values() if e.address is not None]
    fpr = keygate.contact_fingerprint(contact) if any(e.key for e in entries) else None
    if fpr:
        hit = next((e for e in entries if e.key == fpr), None)
        if hit:
            moved = f"; listed as {hit.address}, now writes {addr}" if addr != hit.address else ""
            if moved:
                log(f"chat {n} ({hit.name}){moved}")
            return Verdict(True, f"chat {n} is the 1:1 chat of {hit.name} <{addr}>{moved}", hit.name)
    named = [e for e in entries if addr is not None and e.address == addr]
    if not named:
        return None
    e = named[0]
    pinned = next((x for x in named if x.key), None)
    if pinned is not None:
        why = (f"{addr} holds no key the bridge can read, so it cannot be {pinned.name}'s" if not fpr
               else f"{addr} holds key {fpr}, not the listed one of {pinned.name}")
    else:
        why = (f"{e.name} <{addr}> is listed, but no key is pinned to it "
               f"({PIN_HOW.format(name=e.name)}; {keygate.SHOWS})")
    return Verdict(False, f"REFUSED: chat {n}: {why}; nothing was sent.", reason=why)


def resolve(name: str, path: pathlib.Path | None = None, log: Log | None = None,
            listing: Listing | None = None) -> Entry:
    """The listed entry called NAME. Raises Refused, naming the path, if there is none."""
    listing = listing or load(path, log)
    if listing.refusal:
        raise Refused(f"REFUSED: {listing.refusal}: every send is refused; nothing was sent.")
    entry = listing.entries.get(name)
    if entry is None:
        bad = name_problem(name)
        hint = (f"{bad}, so it can never be listed" if bad
                else f"Add it: sessionphone channels add {name} TARGET")
        raise Refused(f"REFUSED: no channel {name!r} in {listing.path}; nothing was sent. {hint}")
    return entry


def name_problem(name: str) -> str | None:
    """Why NAME can never be a channel NAME, or None if it can."""
    if not NAME_RE.fullmatch(name or ""):
        return f"NAME {name!r} must be lowercase [a-z0-9_-]+"
    if name.isdigit():
        return f"NAME {name!r} is all digits: a tool always reads that as a chat id"
    if name.startswith("-"):
        return f"NAME {name!r} starts with '-': a tool would read that as an option"
    return None


def add(name: str, target: str, note: str = "", path: pathlib.Path | None = None,
        key: str | None = None) -> Entry:
    """List NAME -> TARGET, an address bound to KEY (a fingerprint; without it
    the line is UNPINNED and refuses until pinned). Writes the file 0600;
    raises ValueError on a bad or taken NAME, or a bad KEY."""
    path = pathlib.Path(path) if path else channels_path()
    bad = name_problem(name)
    if bad:
        raise ValueError(bad)
    norm = parse_target(target or "")
    if norm is None:
        raise ValueError(f"TARGET {target!r} must be an address or chat:N")
    if key is not None:
        if norm.startswith("chat:"):
            raise ValueError(f"TARGET {norm} is a chat id, and a chat:N entry takes no key")
        given, key = key, keygate.normalize(key)
        if key is None:
            raise ValueError(f"not a key fingerprint (40 or 64 hex digits): {given!r}")
    note = " ".join((note or "").split())   # one line: a newline in a note must never add an entry
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if name in parse(text, path, lambda _msg: None):
        raise ValueError(f"NAME {name!r} is already listed in {path}")
    line = f"{name}  {norm}" + (f"  key:{key}" if key else "") + (f"  # {note}" if note else "") + "\n"
    if text and not text.endswith("\n"):
        line = "\n" + line
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    path.chmod(0o600)
    if key:
        settle_marker(path)
    return Entry(name, norm, note, key)


def loose_mode(path: pathlib.Path) -> int | None:
    """PATH's mode if add() would TIGHTEN it to 0600 (any bit beyond the owner's
    read/write), else None. add() makes the file 0600 whatever it was, which lifts
    the refusal of a loose file, so a caller must say so out loud."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None
    return mode if mode & 0o177 else None


def seed_owner(addr: str, note: str, path: pathlib.Path, key: str | None = None) -> Entry | None:
    """List ADDR as `owner` in PATH, bound to KEY when given. -> the Entry
    written, or None if ADDR is already listed under some NAME (an unpinned
    line for it is pinned to KEY in place, unless the list is loose: a
    rewrite would make it 0600, and a loose list is never tightened
    quietly). Raises ValueError, writing
    nothing, if ADDR is not an address, the NAME `owner` is taken by another
    target, or ADDR is listed pinned to a key other than KEY."""
    norm = parse_target(addr or "")
    if norm is None or norm.startswith("chat:"):
        raise ValueError(f"owner {addr!r} is not an address (want name@domain)")
    if key is not None:
        given, key = key, keygate.normalize(key)
        if key is None:
            raise ValueError(f"not a key fingerprint (40 or 64 hex digits): {given!r}")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    have = parse(text, path, lambda _msg: None)
    listed = [e for e in have.values() if e.address == norm]
    if listed:
        if key and not any(e.key == key for e in listed):
            other = next((e for e in listed if e.key), None)
            if other is not None:
                raise ValueError(f"{norm} is listed in {path} as {other.name}, pinned to key "
                                 f"{other.key}, not {key}; `{PIN_HOW.format(name=other.name)}` "
                                 "replaces it")
            if loose_mode(path) is None:
                pin(listed[0].name, key, path)
        return None
    if "owner" in have:
        raise ValueError(f"NAME 'owner' is already listed in {path} as {have['owner'].target}, "
                         f"not {norm}")
    return add("owner", norm, note, path, key=key)


def pin(name: str, key: str, path: pathlib.Path | None = None) -> str | None:
    """Pin the address entry NAME to KEY in place, replacing any key it had.
    -> the key it had before (None if unpinned). Raises KeyError if NAME is
    not listed, ValueError for a chat:N entry or a bad KEY."""
    path = pathlib.Path(path) if path else channels_path()
    norm = keygate.normalize(key)
    if norm is None:
        raise ValueError(f"not a key fingerprint (40 or 64 hex digits): {key!r}")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    entry = parse(text, path, lambda _msg: None).get(name)
    if entry is None:
        raise KeyError(name)
    if entry.address is None:
        raise ValueError(f"channel {name!r} is {entry.target}, a chat id, and takes no key")
    rewrite(path, {name: norm}, add_marker=False)
    settle_marker(path)
    return entry.key


def _with_key(line: str, key: str) -> str:
    """LINE (an entry, newline kept) with its key set to KEY, all else kept."""
    data, hash_, note = line.partition("#")
    m = re.match(r"(\s*\S+\s+\S+)(?:\s+key:\S+)?(.*)$", data, re.DOTALL)
    if not m:
        return line
    return f"{m.group(1)}  key:{key}{m.group(2)}{hash_}{note}"


def rewrite(path: pathlib.Path, pins: dict[str, str], add_marker: bool) -> None:
    """Set each NAME in PINS to its key, in place (the line that NAME is read
    from); with ADD_MARKER, put the marker on top if it is missing. Every
    other byte is kept. Atomic (keygate.write_atomic). Raises OSError, the
    file untouched."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    out, seen = [], set()
    for line in text.splitlines(keepends=True):
        got = parse(line, path, lambda _msg: None)
        name = next(iter(got), None)
        if name is not None and name not in seen:
            seen.add(name)
            if name in pins:
                line = _with_key(line, pins[name])
        out.append(line)
    if add_marker and not keygate.marked(text):
        out.insert(0, MARKER + "\n")
    keygate.write_atomic(path, "".join(out))


def settle_marker(path: pathlib.Path) -> None:
    """Add the marker once no address entry is left unpinned (every writer of
    keys calls this). Never while one is: the start migration must still pin
    a pre-upgrade owner line, and it runs only on a file without the marker."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    have = parse(text, path, lambda _msg: None)
    if not keygate.marked(text) and not any(e.address and e.key is None for e in have.values()):
        rewrite(path, {}, add_marker=True)


def is_owner(entry: Entry) -> bool:
    """The owner line: NAME `owner`, or a note that starts `owner` (as `link`
    and `bot install` write it)."""
    return entry.name == "owner" or entry.note.lower().startswith("owner")


def decide(account, entry: Entry, unattended: bool) -> keygate.Decision:
    """The pin pass (keygate.decide) for one unpinned address ENTRY: 1 key
    contact with its address pins, 0 or 2+ never; UNATTENDED pins only the
    owner line."""
    probe = keygate.CallerEntry(entry.address or "", None, "owner" if is_owner(entry) else entry.note)
    d = keygate.decide(account, probe, unattended, pin_how=PIN_HOW.format(name=entry.name))
    d.name = entry.name
    return d


def migrate(account, path: pathlib.Path | None = None, log: Log | None = None) -> list | None:
    """The one-time upgrade of a channels file from an older version, at the
    bot's start (keygate.migrate_file): its unpinned address entries go
    through the pin pass, unattended, so only the owner line is pinned. The
    original is kept as <file>.pre-keys. -> the decisions, or None."""
    path = pathlib.Path(path) if path else channels_path()
    log = log or _stderr

    def plan() -> list:
        have = parse(path.read_text(encoding="utf-8"), path, log)
        return [decide(account, e, unattended=True) for e in have.values()
                if e.address is not None and e.key is None]

    def nobody() -> str | None:
        have = parse(path.read_text(encoding="utf-8"), path, lambda _msg: None)
        addrs = [e for e in have.values() if e.address is not None]
        if addrs and not any(e.key for e in addrs):
            return (f"WARNING: keys: {path}: no address line is pinned to a key, so the bot "
                    "writes to no 1:1 chat")
        return None

    return keygate.migrate_file(path, log, plan,
                                lambda ds: rewrite(path, {d.name: d.key for d in ds if d.key},
                                                   add_marker=True),
                                nobody)


def name_of(addr: str, path: pathlib.Path) -> str | None:
    """The NAME ADDR is listed under in PATH, or None (`link` says the NAME
    the owner really has, which need not be `owner`)."""
    norm = parse_target(addr or "")
    if norm is None or not path.exists():
        return None
    return _name_of(norm, parse(path.read_text(encoding="utf-8"), path, lambda _msg: None))


def _name_of(norm: str, have: dict[str, Entry]) -> str | None:
    return next((e.name for e in have.values() if e.address == norm), None)


# ---- inbound rejections on the PUBLIC profile -------------------------------
#
# A stranger's message on the public profile is logged, never forwarded, and
# the rejection is noted with its reason. The bot records each rejection as ONE JSON
# line in rejected.jsonl, beside that profile's channels.txt, mode 0600:
#
#   {"ts": "...", "profile": "public", "chat_id": 43, "address": "bob@relay",
#    "name": "Bob", "kind": "message", "reason": "chat 43: not in <path>"}
#
# NEVER the message text: the record says who and why, not what. A stranger
# must not be able to fill the disk, so past REJECTED_CAP the file rotates to
# rejected.jsonl.1 (one generation: the newest ~2 caps survive), and each field
# a stranger controls is cut to FIELD_MAX. `sessionphone channels rejected`
# reads both files.

REJECTED_NAME = "rejected.jsonl"
REJECTED_CAP = 1 << 20      # 1 MiB, then rotate
FIELD_MAX = 200             # a display name or address, as a stranger chose it
INBOUND_KINDS = ("message", "contact-request", "reaction", "webxdc", "webxdc-realtime")


def rejected_path(channels_file: pathlib.Path) -> pathlib.Path:
    """rejected.jsonl beside CHANNELS_FILE: one per profile, as the list is."""
    return pathlib.Path(channels_file).parent / REJECTED_NAME


def clean(value: object, limit: int = FIELD_MAX) -> str | None:
    """A stranger-chosen string made safe to store and print: control
    characters and newlines become spaces, runs of space collapse, and it is
    cut to LIMIT. None stays None."""
    if value is None:
        return None
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())[:limit]


def rejection(profile: str, chat_id: object, kind: str, address: object, name: object,
              reason: str, ts: str | None = None) -> dict:
    """The record for one rejected inbound event. No message text, by design."""
    return {"ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S%z"), "profile": profile,
            "chat_id": chat_id, "address": clean(address), "name": clean(name),
            "kind": kind, "reason": clean(reason, 4 * FIELD_MAX)}


def record_rejected(path: pathlib.Path, rec: dict, cap: int = REJECTED_CAP) -> None:
    """Append REC to PATH as one JSON line, 0600, rotating to PATH.1 first when
    the line would take PATH past CAP bytes. One os.write of the whole line on
    an O_APPEND fd, so a reader never sees half a record. Raises OSError on any
    failure (the caller logs it loudly; it never forwards the event instead)."""
    path = pathlib.Path(path)
    line = (json.dumps(rec, ensure_ascii=True) + "\n").encode("ascii")
    try:
        size = os.stat(path, follow_symlinks=False).st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(line) > cap:
        os.replace(path, path.with_name(path.name + ".1"))
    # O_NOFOLLOW: a symlink planted here is refused, never written through.
    # O_NONBLOCK: a FIFO cannot hang the bot's event thread.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        wrote = os.write(fd, line)
    finally:
        os.close(fd)
    if wrote != len(line):
        raise OSError(f"short write to {path}, {wrote} of {len(line)} bytes")


def read_rejected(path: pathlib.Path) -> tuple[list[dict], int]:
    """Every record in PATH.1 then PATH, oldest first, and the count of lines
    that did not parse. A missing file is no records. Raises OSError if a file
    that exists cannot be read."""
    path = pathlib.Path(path)
    recs: list[dict] = []
    bad = 0
    for f in (path.with_name(path.name + ".1"), path):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            if isinstance(rec, dict):
                recs.append(rec)
            else:
                bad += 1
    return recs, bad


@dataclass
class Rejected:
    """One chat's rejections, folded: the newest record, and how many there were."""

    last: dict
    count: int
    kinds: list[str]


def fold_rejected(recs: list[dict]) -> list[Rejected]:
    """RECS (oldest first) deduped by chat id, newest chat first."""
    by_chat: dict[str, Rejected] = {}
    order: list[str] = []
    for rec in recs:
        key = str(rec.get("chat_id"))
        kind = str(rec.get("kind") or "?")
        if key in by_chat:
            r = by_chat[key]
            r.last, r.count = rec, r.count + 1
            if kind not in r.kinds:
                r.kinds.append(kind)
            order.remove(key)
        else:
            by_chat[key] = Rejected(rec, 1, [kind])
        order.append(key)
    return [by_chat[k] for k in reversed(order)]


# ---- the tools' pre-check ---------------------------------------------------
#
# A dc-* tool runs this before it queues a job. It can only REFUSE: the bot's
# gate stays the authority and the backstop, and only the bot reads the list
# from its own environment. So whatever list the tool is pointed at, a send
# the pre-check lets through still WAITS for the bot's answer, even under
# DC_TAKEN_WAIT=0, and a bot refusal still exits 3: no refused send exits 0.

# What the tool's argument is. chat: a CHAT (a chat id or a channel NAME).
# msg: a message id (react, xdc-update); only a refusal of the whole list can
# be decided. contact: a channel NAME or a contact's name (dm, ring); likewise.
# end: hangup/answer, which the bot does not gate: a chat id is
# never refused here, but a NAME must still resolve.
KINDS = ("chat", "msg", "contact", "end")


def is_chat_id(ref: object) -> bool:
    """A tool's rule: an all-digit argument is ALWAYS a chat id."""
    return isinstance(ref, str) and ref.isascii() and ref.isdigit()


def destination(field_name: str, ref: str) -> dict:
    """The job fields that name REF. A chat id keeps the kind's own
    field, as a number; anything else goes in "chat", and the kind's own field
    (when it is not chat_id) stays present, empty, so the bot's branch is taken."""
    if is_chat_id(ref):
        return {field_name: int(ref)}
    return {"chat": ref} if field_name == "chat_id" else {field_name: None, "chat": ref}


def precheck(kind: str, ref: str, path: pathlib.Path | None = None,
             log: Log | None = None, listing: Listing | None = None) -> tuple[str | None, bool]:
    """-> (refusal detail or None, gated). Refuse-only: None means "not refused
    here", never "allowed". gated: the bot may still refuse the job, so the tool
    must wait for its answer whatever DC_TAKEN_WAIT says. LISTING, when given,
    is a load() the caller already did (main() reads it once, to tell a refusal
    of the whole list from a refusal of one chat)."""
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r} is not one of {', '.join(KINDS)}")
    if kind == "end" and is_chat_id(ref):
        return None, False                      # not gated: ending a call sends no words
    listing = listing or load(path, log)
    if listing.refusal:
        return f"REFUSED: {listing.refusal}: every send is refused; nothing was sent.", True
    if kind in ("msg", "contact"):
        return None, True                       # the bot reads the chat; it decides
    if not is_chat_id(ref):
        try:
            resolve(ref, listing=listing)
        except Refused as e:
            return str(e), True
        return None, True
    n = int(ref)
    if n <= 0:
        return (f"REFUSED: {ref!r} is not a chat id, so nothing on the allow-list {listing.path} "
                "can pass it; nothing was sent."), True
    if n in listing.chat_ids or listing.addresses:
        return None, True       # listed, or maybe the 1:1 of a listed address: the bot decides
    return (f"REFUSED: chat {n} is not on the allow-list {listing.path}; nothing was sent. "
            f"Add it: sessionphone channels add NAME chat:{n}"), True


def main(argv: list[str] | None = None) -> int:
    """precheck [--list PATH] [--outbox DIR] [--field KEY] [--tool NAME] KIND REF

    For the dc-* tools. Refused: the detail on stderr, exit 3, nothing printed.
    When the whole list refuses (missing, unreadable, loose) and --outbox names
    the bot's outbox, a second stderr line says whether that bot has mailed the
    owner's agent about it, or why not (listalert.tool_line).
    Else exit 0 and two lines on stdout: `wait` (the bot gates it: wait for its
    answer even under DC_TAKEN_WAIT=0) or `nowait`, then the job's destination
    fields as JSON (`{}` without --field). Usage: exit 2.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="python -m sessionphone.channels")
    sub = ap.add_subparsers(dest="verb", required=True)
    pc = sub.add_parser("precheck")
    pc.add_argument("--list", type=pathlib.Path, default=None)
    pc.add_argument("--outbox", type=pathlib.Path, default=None)
    pc.add_argument("--field", default="")
    pc.add_argument("--tool", default="channels")
    pc.add_argument("kind", choices=KINDS)
    pc.add_argument("ref")
    a = ap.parse_args(argv)
    ignored: list[str] = []
    listing = load(a.list, ignored.append)
    refusal, gated = precheck(a.kind, a.ref, a.list, ignored.append, listing=listing)
    if refusal:
        print(f"{a.tool}: {refusal}", file=sys.stderr)
        for line in ignored:    # a NAME on a bad line reads as "no channel": say why
            print(f"{a.tool}: {line}", file=sys.stderr)
        if listing.refusal and a.outbox is not None:   # the whole list: was the owner told?
            from sessionphone.bridge import listalert
            print(listalert.tool_line(a.outbox, listing.path, listing.kind, a.tool), file=sys.stderr)
        return 3
    print("wait" if gated else "nowait")
    print(json.dumps(destination(a.field, a.ref) if a.field else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

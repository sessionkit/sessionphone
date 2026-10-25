"""Approved callers are bound to a key fingerprint.

A callers.txt line is `ADDR  key:FPR  # note`. The FINGERPRINT is the
identity: a caller rings when the key its Delta Chat contact holds has the
fingerprint a line lists. The address is the label people read. A line with
no `key:` (written by an older version) is read as UNPINNED and is refused
until a key is pinned to it.

A key is pinned only at a trusted moment: `sessionphone link` (the owner's
securejoin), an operator's `--key FPR` (read off the phone's Encryption
screen), an attended `sessionphone callers add|pin` with the account open,
and once, unattended, at the bot's first start on a file from an older
version (`migrate`, which pins only the owner line). A key is never pinned
when a call arrives.

The fingerprint is the OpenPGP primary-key fingerprint, read from the KEY
of the contact's own vCard: v4 is SHA-1 (RFC 4880 12.2, RFC 9580 5.5.4.2),
v6 is SHA-256 (RFC 9580 5.5.4.3), written as uppercase hex with no spaces,
as Delta Chat prints it. Every failure to read it is None, and None refuses.

This module needs no Delta Chat import: the account and contacts it is
given are duck-typed, so the tests use fakes.
"""
from __future__ import annotations

import base64
import hashlib
import os
import pathlib
import re
import stat
import tempfile
import traceback
from collections.abc import Callable
from dataclasses import dataclass

# The first line of a callers file whose keys are kept by this version. A
# comment, so every parser, old or new, skips it as data. Its one job: it
# records that the one-time start migration (migrate) has run on this file.
MARKER = "# sessionphone keys v1: a line without key: is refused until pinned"
MARKER_PREFIX = "# sessionphone keys v1"

BACKUP_SUFFIX = ".pre-keys"     # the file as it was before the migration, written once

_HEX = re.compile(r"[0-9A-F]+")
_KEY_LINE = re.compile(r"^KEY:data:application/pgp-keys;base64\\,(.*)$", re.MULTILINE)

PIN_HOW = "sessionphone callers pin {addr} --key FPR"
SHOWS = "the phone shows it under the contact's profile, Encryption"


# ---- fingerprints -----------------------------------------------------------


def _first_packet(data: bytes) -> tuple[int, bytes] | None:
    """(tag, body) of the first OpenPGP packet; None if it cannot be read.
    Both header formats; a partial or indeterminate length is refused."""
    if not data or not data[0] & 0x80:
        return None
    b0 = data[0]
    if b0 & 0x40:                                   # new format (RFC 9580 4.2.1)
        tag, i = b0 & 0x3F, 1
        if i >= len(data):
            return None
        l1 = data[i]
        if l1 < 192:
            n, i = l1, i + 1
        elif l1 < 224:
            if i + 1 >= len(data):
                return None
            n, i = ((l1 - 192) << 8) + data[i + 1] + 192, i + 2
        elif l1 == 255:
            if i + 4 >= len(data):
                return None
            n, i = int.from_bytes(data[i + 1:i + 5], "big"), i + 5
        else:
            return None                             # partial length: never a key packet
    else:                                           # old format (RFC 9580 4.2.2)
        tag, lt, i = (b0 >> 2) & 0x0F, b0 & 0x03, 1
        if lt == 3:
            return None                             # indeterminate length
        w = (1, 2, 4)[lt]
        if i + w > len(data):
            return None
        n, i = int.from_bytes(data[i:i + w], "big"), i + w
    body = data[i:i + n]
    if len(body) != n:
        return None
    return tag, body


def fingerprint(data: bytes) -> str | None:
    """The primary-key fingerprint of a binary transferable public key, as
    uppercase hex: 40 characters (v4) or 64 (v6). None for anything else."""
    p = _first_packet(data)
    if p is None or p[0] != 6 or not p[1]:          # tag 6: Public-Key packet
        return None
    body = p[1]
    if body[0] == 4:
        if len(body) > 0xFFFF:
            return None
        return hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
    if body[0] == 6:
        return hashlib.sha256(b"\x9b" + len(body).to_bytes(4, "big") + body).hexdigest().upper()
    return None


def vcard_key(vcard: str) -> bytes | None:
    """The KEY of a one-contact vCard, decoded; None unless there is exactly one."""
    text = re.sub(r"\r?\n[ \t]", "", vcard or "")   # RFC 6350 3.2: unfold
    found = _KEY_LINE.findall(text)
    if len(found) != 1:
        return None
    try:
        return base64.b64decode(found[0].strip(), validate=True)
    except (ValueError, TypeError):
        return None


def contact_fingerprint(contact) -> str | None:
    """The fingerprint of the key CONTACT holds; None if it holds none (an
    address contact) or it cannot be read. An RPC error propagates: the
    caller refuses on it."""
    snap = contact.get_snapshot()
    if not getattr(snap, "is_key_contact", False):
        return None
    key = vcard_key(contact.make_vcard())
    return fingerprint(key) if key else None


def normalize(text: str | None) -> str | None:
    """A typed fingerprint as one token: uppercase, spaces and colons gone
    (the phone shows it in groups). None unless it is 40 or 64 hex digits."""
    if text is None:
        return None
    t = re.sub(r"[\s:]", "", text).upper()
    if len(t) in (40, 64) and _HEX.fullmatch(t):
        return t
    return None


def file_key(token: str) -> str | None:
    """The FPR of a file's `key:FPR` token, or None if it is not one."""
    if not token.lower().startswith("key:"):
        return None
    t = token[4:].upper()
    return t if len(t) in (40, 64) and _HEX.fullmatch(t) else None


# ---- the callers file -------------------------------------------------------


@dataclass(frozen=True)
class CallerEntry:
    addr: str               # lowercased
    key: str | None         # the pinned fingerprint; None = unpinned, refused
    note: str = ""


def parse_caller_line(line: str) -> CallerEntry | None | str:
    """One callers.txt line -> a CallerEntry, None (blank or a comment), or a
    str: why the line is IGNORED. An ignored line only narrows the list."""
    data, _, note = line.partition("#")
    words = data.split()
    if not words:
        return None
    addr = words[0].lower()
    if len(words) == 1:
        return CallerEntry(addr, None, note.strip())
    if len(words) == 2:
        key = file_key(words[1])
        if key:
            return CallerEntry(addr, key, note.strip())
        return f"{words[1]!r} is not key:FPR (40 or 64 hex digits)"
    return "more than an address and one key:FPR"


def read_caller_entries(path: pathlib.Path, log: Callable[[str], None] | None = None) -> list[CallerEntry]:
    """The callers file's entries, in file order. No file means nobody. A
    line that cannot be read is IGNORED and, given LOG, logged. Raises
    OSError if the file is there but cannot be read."""
    from sessionphone import (
        paths,  # light; imported here so this module imports nothing heavy
    )
    if not paths.exists(path):      # not Path.exists: on 3.14 an unreadable file would read "nobody"
        return []
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        got = parse_caller_line(line)
        if isinstance(got, str):
            if log:
                log(f"IGNORED {path}:{n}: {got}")
        elif got is not None:
            out.append(got)
    return out


def marked(text: str) -> bool:
    """True if the file's text carries the keys marker (migrate has run)."""
    return any(line.startswith(MARKER_PREFIX) for line in text.splitlines())


def is_marked(path: pathlib.Path) -> bool:
    from sessionphone import paths
    return paths.exists(path) and marked(path.read_text())


def entry_line(addr: str, key: str | None, note: str = "") -> str:
    """One callers.txt line, newline included."""
    body = f"{addr}  key:{key}" if key else addr
    return f"{body}  # {note}\n" if note else f"{body}\n"


def _with_key(line: str, key: str) -> str:
    """LINE (an entry, newline kept) with its key set to KEY, all else kept."""
    data, hash_, note = line.partition("#")
    m = re.match(r"(\s*\S+)(?:\s+key:\S+)?(.*)$", data, re.DOTALL)
    if not m:
        return line
    return f"{m.group(1)}  key:{key}{m.group(2)}{hash_}{note}"


def rewrite(path: pathlib.Path, pins: dict[str, str], add_marker: bool) -> None:
    """Set each address in PINS to its key, in place; with ADD_MARKER, put the
    marker on top if it is missing. Every other byte is kept: comments,
    notes, order, ignored lines. Atomic: a 0600 temp file beside it, then
    os.replace. Raises OSError, the file untouched."""
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines(keepends=True)
    out = []
    for line in lines:
        got = parse_caller_line(line)
        if isinstance(got, CallerEntry) and got.addr in pins:
            line = _with_key(line, pins[got.addr])
        out.append(line)
    if add_marker and not marked(text):
        out.insert(0, MARKER + "\n")
    write_atomic(path, "".join(out))


def write_atomic(path: pathlib.Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def settle_marker(path: pathlib.Path) -> None:
    """Add the marker once no unpinned line is left (every writer of keys
    calls this). Never while one is: the start migration must still pin a
    pre-upgrade owner line, and it runs only on a file without the marker."""
    entries = read_caller_entries(path)
    if not any(e.key is None for e in entries) and not is_marked(path):
        rewrite(path, {}, add_marker=True)


def add_entry(path: pathlib.Path, addr: str, key: str | None, note: str = "") -> bool:
    """Approve ADDR (bound to KEY) in PATH. -> False if nothing changed.
    An unpinned ADDR given a KEY is pinned in place. An ADDR already pinned
    to another key raises ValueError, writing nothing: `pin` replaces a key,
    `add` never does."""
    addr = addr.strip().lower()
    if not addr or "@" not in addr or any(c.isspace() for c in addr) or "#" in addr:
        raise ValueError(f"not an address: {addr!r}")
    entries = [e for e in read_caller_entries(path) if e.addr == addr]
    if entries:
        if key is None or any(e.key == key for e in entries):
            return False
        other = next((e.key for e in entries if e.key), None)
        if other:
            raise ValueError(f"{addr} is already pinned to key {other}; "
                             f"`sessionphone callers pin {addr} --key FPR` replaces it")
        rewrite(path, {addr: key}, add_marker=False)
        settle_marker(path)
        return True
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry_line(addr, key, note))
    path.chmod(0o600)
    if key:
        settle_marker(path)
    return True


def pin_entry(path: pathlib.Path, addr: str, key: str) -> str | None:
    """Pin the listed ADDR to KEY in place, replacing any key it had. -> the
    key it had before (None if unpinned). Raises KeyError if ADDR is not listed."""
    addr = addr.strip().lower()
    entries = [e for e in read_caller_entries(path) if e.addr == addr]
    if not entries:
        raise KeyError(addr)
    before = next((e.key for e in entries if e.key), None)
    rewrite(path, {addr: key}, add_marker=False)
    settle_marker(path)
    return before


# ---- the call gate ----------------------------------------------------------


@dataclass
class Verdict:
    ok: bool
    reason: str = ""        # why, for the log line: names the address, never the key alone

    def __bool__(self) -> bool:
        return self.ok


def judge(entries: list[CallerEntry], addr: str | None, fpr: str | None) -> Verdict:
    """May the contact with address ADDR and key FPR ring? FPR decides; ADDR
    only words the refusal (and the pass when it moved)."""
    who = addr or "an unknown sender"
    pinned = [e for e in entries if e.key]
    count = f"{len(pinned)} approved"
    if fpr:
        hit = next((e for e in pinned if e.key == fpr), None)
        if hit:
            moved = f"; listed as {hit.addr}, now writes {addr}" if addr and addr != hit.addr else ""
            return Verdict(True, f"{who} holds the listed key {fpr}{moved}")
    listed = [e for e in entries if e.addr == addr] if addr else []
    if not fpr:
        why = "holds no key the bridge can read, so it cannot be one listed"
    elif any(e.key for e in listed):
        why = f"its key {fpr} is not the listed one"
    elif listed:
        why = f"listed, but no key is pinned to it ({PIN_HOW.format(addr=addr)}; {SHOWS})"
    else:
        why = "not an approved caller"
    return Verdict(False, f"{who}: {why} ({count})")


def caller(account, msg_id: int, path: pathlib.Path, log: Callable[[str], None]) -> Verdict:
    """The call gate: may the sender of call message MSG_ID ring? Reads PATH
    on every call. Fails closed: any error reading the sender, its key or
    the file refuses."""
    try:
        sender = account.get_message_by_id(msg_id).get_snapshot().sender
        addr = (sender.get_snapshot().address or "").strip().lower() or None
        fpr = contact_fingerprint(sender)
    except Exception:  # noqa: BLE001 - any error reading the caller refuses the call
        log("incoming: could not read the caller or its key - refusing:\n" + traceback.format_exc())
        return Verdict(False, "an unknown sender: could not read the caller or its key")
    try:
        entries = read_caller_entries(path, log)
    except OSError as exc:
        return Verdict(False, f"{addr or 'an unknown sender'}: {path} cannot be read "
                              f"({exc.strerror or exc}) (0 approved)")
    v = judge(entries, addr, fpr)
    if v and "now writes" in v.reason:
        log(f"incoming: {v.reason}")
    return v


# ---- the pin pass and the one-time migration --------------------------------


def candidates(account, addr: str) -> list[tuple[object, str]]:
    """The key contacts whose address is exactly ADDR, each with its
    fingerprint, one per key. A contact whose key cannot be read is not one."""
    addr = addr.strip().lower()
    out, seen = [], set()
    for c in account.get_contacts(addr):
        try:
            snap = c.get_snapshot()
            if (snap.address or "").strip().lower() != addr or not getattr(snap, "is_key_contact", False):
                continue
            fpr = contact_fingerprint(c)
        except Exception:   # noqa: BLE001, S112 - an unreadable contact is never a candidate
            continue
        if fpr and fpr not in seen:
            seen.add(fpr)
            out.append((c, fpr))
    return out


@dataclass
class Decision:
    addr: str
    kind: str               # "pinned", "unpinned", "ambiguous"
    key: str | None
    line: str               # the one log line
    name: str | None = None  # the channels.txt NAME it is for (None in callers.txt)


def _ids(found) -> str:
    return ", ".join(f"{getattr(c, 'id', '?')} {f}" for c, f in found)


def decide(account, entry: CallerEntry, unattended: bool, pin_how: str = PIN_HOW) -> Decision:
    """The pin pass for one unpinned ENTRY. 1 candidate pins, 0 or 2+ never.
    UNATTENDED (the start migration) pins only an owner line: nobody is
    there to compare the key, and the owner line is the one `link` wrote
    from a securejoin; any other line is left for an attended pin."""
    addr = entry.addr
    how = pin_how.format(addr=addr)
    found = candidates(account, addr)
    if len(found) == 1:
        c, fpr = found[0]
        if not unattended or entry.note.lower().startswith("owner"):
            return Decision(addr, "pinned", fpr,
                            f"PINNED {addr} to key {fpr} (contact {getattr(c, 'id', '?')}): compare it "
                            f"with the Encryption screen on that person's phone")
        return Decision(addr, "unpinned", None,
                        f"UNPINNED {addr}: one key contact has this address ({_ids(found)}), not "
                        f"pinned unattended (only the owner line is); refused until pinned: compare "
                        f"it with that person's phone, then {how.replace('FPR', fpr)}")
    if not found:
        return Decision(addr, "unpinned", None,
                        f"UNPINNED {addr}: no key contact with this address yet; refused until "
                        f"pinned: {how}")
    return Decision(addr, "ambiguous", None,
                    f"AMBIGUOUS {addr}: {len(found)} key contacts share this address "
                    f"({_ids(found)}); none pinned; choose one: {how}")


def migrate(account, path: pathlib.Path, log: Callable[[str], None]) -> list[Decision] | None:
    """The one-time upgrade of a callers file from an older version, at the
    bot's start. Runs only on a file without the marker, so only once.
    The original is kept as <file>.pre-keys (0600, never overwritten), then
    the file is rewritten atomically with the keys pinned and the marker on
    top. -> the decisions, or None if there was nothing to do or it could
    not be written (then the file is left as it was, unmarked, and the gate
    goes on refusing its unpinned lines)."""
    def plan() -> list[Decision]:
        entries = read_caller_entries(path)
        return [decide(account, e, unattended=True) for e in entries if e.key is None
                and not any(o.addr == e.addr and o.key for o in entries)]

    def nobody() -> str | None:
        after = read_caller_entries(path)
        if after and not any(e.key for e in after):
            return f"WARNING: keys: {path}: no line is pinned to a key, so nobody can ring"
        return None

    return migrate_file(path, log, plan,
                        lambda ds: rewrite(path, {d.addr: d.key for d in ds if d.key}, add_marker=True),
                        nobody)


def migrate_file(path: pathlib.Path, log: Callable[[str], None], plan: Callable[[], list[Decision]],
                 apply: Callable[[list[Decision]], None],
                 nobody: Callable[[], str | None]) -> list[Decision] | None:
    """The one-time start migration of one keyed file (callers.txt, channels.txt):
    only on a file without the marker and not writable by others. PLAN gives the
    decisions, the original is kept as <file>.pre-keys (0600, never overwritten),
    then APPLY rewrites the file with the pins and the marker. NOBODY, after, gives
    a WARNING line when nothing in the file can pass any more."""
    from sessionphone import paths
    try:
        if not paths.exists(path):
            return None
        text = path.read_text()
        if marked(text):
            return None
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o022:
            log(f"keys: {path} NOT migrated: mode {mode:04o} lets others write it; make it 0600 "
                f"and restart; its unpinned lines stay refused")
            return None
        decisions = plan()
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if not paths.exists(backup):
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        apply(decisions)
    except Exception as exc:  # noqa: BLE001 - a migration never stops the bot
        log(f"keys: {path} NOT migrated ({exc}); left as it was; its unpinned lines stay refused")
        return None
    for d in decisions:
        log(f"keys: {d.line}")
    n = {k: sum(d.kind == k for d in decisions) for k in ("pinned", "unpinned", "ambiguous")}
    log(f"keys: {path}: {n['pinned']} pinned, {n['unpinned']} unpinned, {n['ambiguous']} ambiguous"
        f" (was kept as {backup})")
    warning = nobody()
    if warning:
        log(warning)
    return decisions

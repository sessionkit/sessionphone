"""Approved callers bound to a key fingerprint: the fingerprint, the callers
file, the gate's judgement and the one-time start migration (keygate)."""
from __future__ import annotations

import base64
import os
import stat

import _keyfakes as kf
import pytest

from sessionphone import account, keygate

# ---- fingerprints -------------------------------------------------------------


def test_the_rfc_vectors_give_their_published_fingerprints():
    assert keygate.fingerprint(base64.b64decode(kf.V4)) == kf.FPR_V4
    assert keygate.fingerprint(kf.V4_OLD) == kf.FPR_V4          # old-format header
    assert keygate.fingerprint(base64.b64decode(kf.V6)) == kf.FPR_V6
    assert keygate.fingerprint(base64.b64decode(kf.K1)) == kf.FPR_K1


def _new_format(tag: int, body: bytes) -> bytes:
    return bytes([0xC0 | tag, len(body)]) + body


@pytest.mark.parametrize("data", [
    b"",
    base64.b64decode(kf.V4)[:20],                                    # truncated
    bytes([0xC6, 0xE1]) + b"\x04" * 40,                              # partial length header
    bytes([0x9B]) + b"\x04" * 10,                                     # old format, indeterminate length
    _new_format(2, base64.b64decode(kf.V4)[2:]),                     # a signature packet, not tag 6
    _new_format(6, b"\x05" + base64.b64decode(kf.V4)[3:]),           # version 5
    _new_format(6, b"\x03" + base64.b64decode(kf.V4)[3:]),           # version 3
    b"\x00\x01\x02",                                                  # not a packet at all
])
def test_anything_but_a_v4_or_v6_public_key_packet_has_no_fingerprint(data):
    assert keygate.fingerprint(data) is None


def test_a_vcard_key_is_read_folded_or_not():
    assert keygate.fingerprint(keygate.vcard_key(kf.vcard("a@x.org", kf.V6))) == kf.FPR_V6
    assert keygate.fingerprint(keygate.vcard_key(kf.vcard("a@x.org", kf.V6, fold=True))) == kf.FPR_V6


def test_no_key_or_two_keys_in_a_vcard_is_none():
    assert keygate.vcard_key(kf.vcard("a@x.org", None)) is None
    two = kf.vcard("a@x.org", kf.K1).replace("END:VCARD",
                                             f"KEY:data:application/pgp-keys;base64\\,{kf.K2}\r\nEND:VCARD")
    assert keygate.vcard_key(two) is None
    assert keygate.vcard_key(kf.vcard("a@x.org", "not*base64")) is None


def test_contact_fingerprint():
    assert keygate.contact_fingerprint(kf.Contact(10, "a@x.org", kf.K1)) == kf.FPR_K1
    plain = kf.Contact(11, "a@x.org", kf.K1, key_contact=False)
    assert keygate.contact_fingerprint(plain) is None
    assert plain.vcards == 0                              # an address contact's vCard is never asked for
    assert keygate.contact_fingerprint(kf.Contact(12, "a@x.org", None, key_contact=True)) is None
    with pytest.raises(RuntimeError):                     # an rpc error reaches the caller, which refuses
        keygate.contact_fingerprint(kf.Contact(13, "a@x.org", kf.K1, vcard_error=RuntimeError("rpc")))


@pytest.mark.parametrize("typed", [
    kf.FPR_K1, kf.FPR_K1.lower(),
    " ".join(kf.FPR_K1[i:i + 4] for i in range(0, 40, 4)),     # the phone's groups
    ":".join(kf.FPR_K1[i:i + 2] for i in range(0, 40, 2)),
])
def test_normalize_takes_the_forms_people_type(typed):
    assert keygate.normalize(typed) == kf.FPR_K1


@pytest.mark.parametrize("typed", [kf.FPR_K1[:39], kf.FPR_K1 + "A", kf.FPR_K1[:39] + "G", "", None])
def test_normalize_refuses_anything_else(typed):
    assert keygate.normalize(typed) is None


def test_normalize_takes_a_v6_fingerprint():
    assert keygate.normalize(kf.FPR_V6.lower()) == kf.FPR_V6


# ---- the callers file -----------------------------------------------------------


def test_a_keyed_line_is_read_with_its_key_and_note(tmp_path):
    f = tmp_path / "callers.txt"
    f.write_text(f"{keygate.MARKER}\nBob@Example.org  key:{kf.FPR_K1.lower()}  # Bob\nann@example.org\n")
    assert keygate.read_caller_entries(f) == [
        keygate.CallerEntry("bob@example.org", kf.FPR_K1, "Bob"),
        keygate.CallerEntry("ann@example.org", None, ""),     # unpinned
    ]


@pytest.mark.parametrize("line", ["bob@example.org key:XYZ", "bob@example.org key:" + kf.FPR_K1[:39],
                                  "bob@example.org other", f"bob@example.org key:{kf.FPR_K1} extra"])
def test_a_bad_line_is_ignored_and_logged(tmp_path, line):
    f = tmp_path / "callers.txt"
    f.write_text(line + "  # note\n")
    said = []
    assert keygate.read_caller_entries(f, said.append) == []
    assert said and "IGNORED" in said[0]


def test_the_other_readers_still_get_bare_addresses(tmp_path):
    f = tmp_path / "callers.txt"
    f.write_text(f"{keygate.MARKER}\nowner@example.org  key:{kf.FPR_K1}  # owner\n"
                 "ann@example.org  # Ann\n")
    assert account.read_callers(f) == ["owner@example.org", "ann@example.org"]
    assert account.read_owner(f) == "owner@example.org"


def test_add_caller_with_a_key_writes_the_line_and_the_marker(tmp_path):
    f = tmp_path / "callers.txt"
    assert account.add_caller("Bob@Example.org", "Bob", f, key=kf.FPR_K1.lower())
    assert f.read_text() == f"{keygate.MARKER}\nbob@example.org  key:{kf.FPR_K1}  # Bob\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not account.add_caller("bob@example.org", "", f, key=kf.FPR_K1)      # nothing changed
    with pytest.raises(ValueError, match="already pinned"):                      # add never replaces a key
        account.add_caller("bob@example.org", "", f, key=kf.FPR_K2)
    with pytest.raises(ValueError, match="not a key"):
        account.add_caller("carl@example.org", "", f, key="12345")
    assert "carl" not in f.read_text()


def test_a_keyed_write_leaves_the_marker_off_while_an_old_line_is_unpinned(tmp_path):
    """The start migration runs only on a file without the marker, so a key
    written by hand beside a pre-upgrade owner line must not mark the file:
    the owner line would never be pinned."""
    f = tmp_path / "callers.txt"
    f.write_text("owner@example.org  # owner\n")
    account.add_caller("bob@example.org", "Bob", f, key=kf.FPR_K1)
    assert not keygate.is_marked(f)
    keygate.pin_entry(f, "owner@example.org", kf.FPR_K2)
    assert keygate.is_marked(f)                          # nothing left unpinned: marked now


def test_pin_entry_rewrites_in_place_keeping_everything_else(tmp_path):
    f = tmp_path / "callers.txt"
    f.write_text("# my callers\nowner@example.org  # owner\n\nann@example.org\n")
    assert keygate.pin_entry(f, "OWNER@example.org", kf.FPR_K1) is None
    assert f.read_text() == (f"# my callers\nowner@example.org  key:{kf.FPR_K1}  # owner\n"
                             "\nann@example.org\n")          # ann is still unpinned: no marker yet
    keygate.pin_entry(f, "ann@example.org", kf.FPR_V6)
    assert f.read_text() == (f"{keygate.MARKER}\n# my callers\nowner@example.org  key:{kf.FPR_K1}  # owner\n"
                             f"\nann@example.org  key:{kf.FPR_V6}\n")
    assert keygate.pin_entry(f, "owner@example.org", kf.FPR_K2) == kf.FPR_K1   # pin replaces a key
    assert f"owner@example.org  key:{kf.FPR_K2}  # owner" in f.read_text()
    with pytest.raises(KeyError):
        keygate.pin_entry(f, "nobody@example.org", kf.FPR_K1)


# ---- the judgement --------------------------------------------------------------


ENTRIES = [keygate.CallerEntry("bob@example.org", kf.FPR_K1, "Bob"),
           keygate.CallerEntry("ann@example.org", None, "Ann")]


def test_the_listed_key_passes():
    assert keygate.judge(ENTRIES, "bob@example.org", kf.FPR_K1)


def test_the_listed_address_with_another_key_is_refused():
    v = keygate.judge(ENTRIES, "bob@example.org", kf.FPR_K2)
    assert not v and "bob@example.org" in v.reason and "not the listed one" in v.reason


def test_the_listed_key_at_a_new_address_passes_and_says_so():
    v = keygate.judge(ENTRIES, "bob@new.example.org", kf.FPR_K1)
    assert v and "listed as bob@example.org, now writes bob@new.example.org" in v.reason


def test_an_unpinned_line_is_refused_and_names_the_pin():
    v = keygate.judge(ENTRIES, "ann@example.org", kf.FPR_K2)
    assert not v and "sessionphone callers pin ann@example.org" in v.reason


def test_no_key_is_refused():
    assert not keygate.judge(ENTRIES, "bob@example.org", None)


# ---- candidates and the one-time migration ---------------------------------------


OWNER_LINE = "owner@example.org  # owner, joined 2030-06-09T00:00:00+0000\n"


def _file(tmp_path, text):
    f = tmp_path / "callers.txt"
    f.write_text(text)
    f.chmod(0o600)
    return f


def test_candidates_are_exact_address_key_contacts_only():
    acc = kf.Account([kf.Contact(10, "bob@example.org", kf.K1),
                      kf.Contact(11, "x.bob@example.org", kf.K2),         # LIKE would match it
                      kf.Contact(12, "BOB@example.org", None, key_contact=True),   # no key
                      kf.Contact(13, "bob@example.org", kf.K2, key_contact=False)])
    assert [(c.id, f) for c, f in keygate.candidates(acc, "bob@example.org")] == [(10, kf.FPR_K1)]


def test_migrate_pins_the_owner_line_to_its_one_key_contact(tmp_path):
    f = _file(tmp_path, "# who may ring\n" + OWNER_LINE)
    said = []
    acc = kf.Account([kf.Contact(10, "owner@example.org", kf.K1)])
    got = keygate.migrate(acc, f, said.append)
    assert [d.kind for d in got] == ["pinned"]
    assert f.read_text() == (f"{keygate.MARKER}\n# who may ring\n"
                             f"owner@example.org  key:{kf.FPR_K1}  # owner, joined 2030-06-09T00:00:00+0000\n")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    backup = tmp_path / "callers.txt.pre-keys"
    assert backup.read_text() == "# who may ring\n" + OWNER_LINE
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert any(f"PINNED owner@example.org to key {kf.FPR_K1}" in s for s in said)
    assert any("1 pinned, 0 unpinned, 0 ambiguous" in s for s in said)


def test_migrate_leaves_an_address_with_no_key_contact_unpinned(tmp_path):
    f = _file(tmp_path, OWNER_LINE)
    said = []
    got = keygate.migrate(kf.Account([]), f, said.append)
    assert [d.kind for d in got] == ["unpinned"]
    assert keygate.read_caller_entries(f)[0].key is None and keygate.is_marked(f)
    assert any("UNPINNED owner@example.org" in s and "callers pin owner@example.org" in s for s in said)
    assert any("nobody can ring" in s for s in said)


def test_migrate_refuses_to_choose_between_two_keys(tmp_path):
    f = _file(tmp_path, OWNER_LINE)
    said = []
    acc = kf.Account([kf.Contact(10, "owner@example.org", kf.K1), kf.Contact(11, "owner@example.org", kf.K2)])
    got = keygate.migrate(acc, f, said.append)
    assert [d.kind for d in got] == ["ambiguous"]
    assert keygate.read_caller_entries(f)[0].key is None
    line = next(s for s in said if "AMBIGUOUS" in s)
    assert kf.FPR_K1 in line and kf.FPR_K2 in line and "none pinned" in line


def test_migrate_never_pins_a_non_owner_line_unattended(tmp_path):
    """Nobody is there to compare the key, so only the owner line (the one
    `link` wrote from a securejoin) is pinned; another line's one candidate
    is named, with the command to pin it after a check."""
    f = _file(tmp_path, OWNER_LINE + "ann@example.org  # Ann\n")
    said = []
    acc = kf.Account([kf.Contact(10, "owner@example.org", kf.K1), kf.Contact(11, "ann@example.org", kf.K2)])
    got = keygate.migrate(acc, f, said.append)
    assert [(d.addr, d.kind) for d in got] == [("owner@example.org", "pinned"), ("ann@example.org", "unpinned")]
    assert keygate.read_caller_entries(f)[1].key is None
    assert any(f"callers pin ann@example.org --key {kf.FPR_K2}" in s for s in said)


def test_migrate_runs_once_only(tmp_path):
    f = _file(tmp_path, OWNER_LINE)
    keygate.migrate(kf.Account([]), f, lambda _s: None)           # 0 candidates: unpinned, marked
    backup = (tmp_path / "callers.txt.pre-keys").read_text()
    before = f.read_text()
    acc = kf.Account([kf.Contact(10, "owner@example.org", kf.K1)])  # a key contact appears later
    assert keygate.migrate(acc, f, lambda _s: None) is None       # no unattended pin after the upgrade
    assert f.read_text() == before
    assert (tmp_path / "callers.txt.pre-keys").read_text() == backup


def test_a_second_start_is_a_no_op(tmp_path):
    f = _file(tmp_path, OWNER_LINE)
    acc = kf.Account([kf.Contact(10, "owner@example.org", kf.K1)])
    keygate.migrate(acc, f, lambda _s: None)
    after = f.read_text()
    assert keygate.migrate(acc, f, lambda _s: None) is None
    assert f.read_text() == after


def test_migrate_never_overwrites_an_existing_backup(tmp_path):
    f = _file(tmp_path, OWNER_LINE)
    (tmp_path / "callers.txt.pre-keys").write_text("older\n")
    keygate.migrate(kf.Account([kf.Contact(10, "owner@example.org", kf.K1)]), f, lambda _s: None)
    assert (tmp_path / "callers.txt.pre-keys").read_text() == "older\n"


def test_a_file_others_may_write_is_not_migrated(tmp_path):
    f = _file(tmp_path, OWNER_LINE)
    f.chmod(0o622)
    said = []
    assert keygate.migrate(kf.Account([kf.Contact(10, "owner@example.org", kf.K1)]), f, said.append) is None
    assert f.read_text() == OWNER_LINE and not (tmp_path / "callers.txt.pre-keys").exists()
    assert any("NOT migrated" in s for s in said)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_file_that_cannot_be_written_is_left_as_it_was(tmp_path):
    d = tmp_path / "home"
    d.mkdir()
    f = _file(d, OWNER_LINE)
    d.chmod(0o500)          # no temp file, no backup can be made beside it
    said = []
    try:
        got = keygate.migrate(kf.Account([kf.Contact(10, "owner@example.org", kf.K1)]), f, said.append)
    finally:
        d.chmod(0o700)
    assert got is None and f.read_text() == OWNER_LINE and not keygate.is_marked(f)
    assert any("NOT migrated" in s for s in said)


def test_no_file_is_nothing_to_migrate(tmp_path):
    assert keygate.migrate(kf.Account([]), tmp_path / "callers.txt", lambda _s: None) is None
    assert not (tmp_path / "callers.txt").exists()

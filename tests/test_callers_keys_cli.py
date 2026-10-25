"""`sessionphone callers`, `callers add` and `callers pin`: each approved
caller is bound to a key fingerprint, from --key or from the account."""
from __future__ import annotations

import _keyfakes as kf
import pytest
from click.testing import CliRunner

from sessionphone import account, keygate, paths
from sessionphone.bridge import dc_env
from sessionphone.cli import main

BOB = "bob@example.org"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "phone"
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    paths.ensure_private(create=True)
    return r


def cli(*args):
    return CliRunner().invoke(main, list(args))


def held(monkeypatch, contacts):
    """`_open` yields a fake account holding CONTACTS (the bot is stopped)."""
    monkeypatch.setattr(account, "_open", lambda create=False: iter([kf.Account(contacts)]))


def busy(monkeypatch):
    """`_open` fails as it does while the bot holds the account."""
    def locked(create=False):
        raise RuntimeError("account is locked by another process")
        yield
    monkeypatch.setattr(account, "_open", locked)


def test_add_with_a_key_writes_the_keyed_line_and_the_marker(root):
    r = cli("callers", "add", "Bob@Example.org", "--key", kf.FPR_K1.lower(), "--note", "Bob")
    assert r.exit_code == 0, r.output
    assert dc_env.callers_file().read_text() == f"{keygate.MARKER}\n{BOB}  key:{kf.FPR_K1}  # Bob\n"


def test_a_bad_key_exits_1_and_writes_nothing(root):
    r = cli("callers", "add", BOB, "--key", kf.FPR_K1[:39])
    assert r.exit_code == 1 and "not a key fingerprint" in r.output
    assert not dc_env.callers_file().exists()


def test_add_while_the_bot_holds_the_account_exits_1_naming_key(root, monkeypatch):
    busy(monkeypatch)
    r = cli("callers", "add", BOB)
    assert r.exit_code == 1 and "--key FPR" in r.output and "nothing was written" in r.output
    assert not dc_env.callers_file().exists()


def test_add_finds_the_one_key_contact_when_the_account_opens(root, monkeypatch):
    held(monkeypatch, [kf.Contact(10, BOB, kf.K1)])
    r = cli("callers", "add", BOB)
    assert r.exit_code == 0, r.output
    assert f"PINNED {BOB} to key {kf.FPR_K1}" in r.output       # printed to compare with the phone
    assert keygate.read_caller_entries(dc_env.callers_file())[0].key == kf.FPR_K1


def test_add_with_no_key_contact_is_refused(root, monkeypatch):
    held(monkeypatch, [])
    r = cli("callers", "add", BOB)
    assert r.exit_code == 1 and "UNPINNED" in r.output and "--key FPR" in r.output
    assert not dc_env.callers_file().exists()


def test_add_with_two_key_contacts_is_refused_naming_both(root, monkeypatch):
    held(monkeypatch, [kf.Contact(10, BOB, kf.K1), kf.Contact(11, BOB, kf.K2)])
    r = cli("callers", "add", BOB)
    assert r.exit_code == 1 and kf.FPR_K1 in r.output and kf.FPR_K2 in r.output
    assert not dc_env.callers_file().exists()


def test_pin_with_a_key_rewrites_the_line_in_place(root):
    dc_env.callers_file().write_text(f"# mine\n{BOB}  # Bob\n")
    r = cli("callers", "pin", BOB, "--key", kf.FPR_K1)
    assert r.exit_code == 0, r.output
    assert dc_env.callers_file().read_text() == f"{keygate.MARKER}\n# mine\n{BOB}  key:{kf.FPR_K1}  # Bob\n"
    r = cli("callers", "pin", BOB, "--key", kf.FPR_K2)              # a new key: the old one is named
    assert r.exit_code == 0 and f"was {kf.FPR_K1}" in r.output


def test_pin_while_the_bot_holds_the_account_works_with_a_key(root, monkeypatch):
    busy(monkeypatch)
    dc_env.callers_file().write_text(f"{BOB}  # Bob\n")
    assert cli("callers", "pin", BOB, "--key", kf.FPR_K1).exit_code == 0
    assert cli("callers", "pin", BOB).exit_code == 1              # without one it cannot look


def test_pin_of_an_address_not_listed_is_refused(root):
    r = cli("callers", "pin", BOB, "--key", kf.FPR_K1)
    assert r.exit_code == 1 and "callers add" in r.output


def test_the_list_says_pinned_or_unpinned(root):
    dc_env.callers_file().write_text(f"{keygate.MARKER}\n{BOB}  key:{kf.FPR_K1}\nann@example.org  # Ann\n")
    r = cli("callers")
    assert r.exit_code == 0
    assert r.output.splitlines() == [
        f"callers: {dc_env.callers_file()}",
        f"{BOB}  key:{kf.FPR_K1}",
        "ann@example.org  UNPINNED: refused until pinned (`sessionphone callers pin ann@example.org`)",
    ]

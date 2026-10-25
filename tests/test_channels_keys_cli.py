"""`sessionphone channels add|pin` and the channels listing: each address
channel is bound to a key fingerprint, from --key or from the account; and
install / `channels seed` carry the callers owner key into channels.txt."""
from __future__ import annotations

import os

import _keyfakes as kf
import pytest
from click.testing import CliRunner

from sessionphone import account, channels, keygate, service
from sessionphone.cli import main

BOB = "bob@example.org"


@pytest.fixture
def root(monkeypatch, tmp_path):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    r = tmp_path / "root"
    r.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    monkeypatch.setenv("DC_ROOT", str(r))
    return r


def cli(*args):
    return CliRunner().invoke(main, list(args))


def held(monkeypatch, contacts):
    monkeypatch.setattr(account, "_open", lambda create=False: iter([kf.Account(contacts)]))


def busy(monkeypatch):
    def locked(create=False):
        raise RuntimeError("account is locked by another process")
        yield
    monkeypatch.setattr(account, "_open", locked)


def test_add_with_a_key_writes_the_keyed_line_and_the_marker(root):
    r = cli("channels", "add", "bob", "Bob@Example.org", "--key", kf.FPR_K1.lower(), "--note", "Bob")
    assert r.exit_code == 0, r.output
    f = root / "channels.txt"
    assert f.read_text() == f"{channels.MARKER}\nbob  {BOB}  key:{kf.FPR_K1}  # Bob\n"
    assert f"listed: bob  {BOB}  key:{kf.FPR_K1}" in r.output


def test_add_a_chat_needs_no_key_and_opens_no_account(root, monkeypatch):
    busy(monkeypatch)
    r = cli("channels", "add", "family", "chat:42")
    assert r.exit_code == 0, r.output
    assert channels.load(root / "channels.txt").entries["family"].chat_id == 42


def test_a_key_on_a_chat_and_a_bad_key_exit_1_and_write_nothing(root):
    assert cli("channels", "add", "family", "chat:42", "--key", kf.FPR_K1).exit_code == 1
    r = cli("channels", "add", "bob", BOB, "--key", kf.FPR_K1[:39])
    assert r.exit_code == 1 and "not a key fingerprint" in r.output
    assert not (root / "channels.txt").exists()


def test_add_while_the_bot_holds_the_account_exits_1_naming_key(root, monkeypatch):
    busy(monkeypatch)
    r = cli("channels", "add", "bob", BOB)
    assert r.exit_code == 1 and "--key FPR" in r.output and "nothing was written" in r.output
    assert not (root / "channels.txt").exists()


def test_add_finds_the_one_key_contact_when_the_account_opens(root, monkeypatch):
    held(monkeypatch, [kf.Contact(10, BOB, kf.K1)])
    r = cli("channels", "add", "bob", BOB)
    assert r.exit_code == 0, r.output
    assert f"PINNED {BOB} to key {kf.FPR_K1}" in r.output
    assert channels.load(root / "channels.txt").entries["bob"].key == kf.FPR_K1


@pytest.mark.parametrize("contacts", [[], [kf.Contact(10, BOB, kf.K1), kf.Contact(11, BOB, kf.K2)]])
def test_add_with_no_or_two_key_contacts_is_refused(root, monkeypatch, contacts):
    held(monkeypatch, contacts)
    r = cli("channels", "add", "bob", BOB)
    assert r.exit_code == 1 and "sessionphone channels pin bob --key FPR" in r.output
    assert not (root / "channels.txt").exists()


def test_pin_with_a_key_rewrites_the_line_in_place(root):
    f = root / "channels.txt"
    f.write_text(f"bob  {BOB}  # Bob\n")
    f.chmod(0o600)
    r = cli("channels", "pin", "bob", "--key", kf.FPR_K1)
    assert r.exit_code == 0, r.output
    assert f.read_text() == f"{channels.MARKER}\nbob  {BOB}  key:{kf.FPR_K1}  # Bob\n"
    r = cli("channels", "pin", "bob", "--key", kf.FPR_K2)
    assert r.exit_code == 0 and f"(was {kf.FPR_K1})" in r.output


def test_pin_without_a_key_while_the_bot_holds_the_account_writes_nothing(root, monkeypatch):
    f = root / "channels.txt"
    f.write_text(f"bob  {BOB}\n")
    f.chmod(0o600)
    busy(monkeypatch)
    r = cli("channels", "pin", "bob")
    assert r.exit_code == 1 and "--key FPR" in r.output
    assert f.read_text() == f"bob  {BOB}\n"


def test_pin_refuses_an_unknown_name_or_a_chat(root):
    f = root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o600)
    assert cli("channels", "pin", "nobody", "--key", kf.FPR_K1).exit_code == 1
    r = cli("channels", "pin", "family", "--key", kf.FPR_K1)
    assert r.exit_code == 1 and "takes no key" in r.output


def test_the_listing_shows_each_key_or_unpinned(root):
    f = root / "channels.txt"
    f.write_text(f"bob  {BOB}  key:{kf.FPR_K1}  # Bob\neve  eve@example.org\nfamily  chat:42\n")
    f.chmod(0o600)
    r = cli("channels")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].split() == ["bob", BOB, f"key:{kf.FPR_K1}", "#", "Bob"]
    assert "UNPINNED" in lines[1] and "sessionphone channels pin eve" in lines[1]
    assert lines[2].split() == ["family", "chat:42"]


# ---- install and `channels seed` carry the owner's key ----------------------


def test_plan_channels_copies_the_callers_owner_key(root):
    (root / "callers.txt").write_text(f"{BOB}  key:{kf.FPR_K1}  # owner, Bob\n")
    path, addr, note, key = service.plan_channels({"SESSIONPHONE_ROOT": str(root), "DC_ROOT": str(root)})
    assert (addr, key) == (BOB, kf.FPR_K1)
    service.write_channels((path, addr, note, key))
    assert channels.load(path).entries["owner"].key == kf.FPR_K1


def test_plan_channels_for_install_owner_takes_its_key(root):
    env = {"SESSIONPHONE_ROOT": str(root), "DC_ROOT": str(root)}
    assert service.plan_channels(env, BOB, owner_key=kf.FPR_K2.lower())[3] == kf.FPR_K2
    (root / "callers.txt").write_text(f"{BOB}  key:{kf.FPR_K1}  # owner, Bob\n")
    assert service.plan_channels(env, BOB)[3] == kf.FPR_K1      # a re-install: the pinned key


def test_plan_channels_from_an_unpinned_owner_seeds_it_unpinned(root):
    (root / "callers.txt").write_text(f"{BOB}  # owner, Bob\n")
    plan = service.plan_channels({"SESSIONPHONE_ROOT": str(root), "DC_ROOT": str(root)})
    assert plan[3] is None
    e = service.write_channels(plan)
    assert e.key is None    # the bot's first start pins it (channels.migrate), as it does callers
    assert not keygate.marked((root / "channels.txt").read_text())

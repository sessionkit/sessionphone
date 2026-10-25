"""Who may ring the bot: an allowlist on every profile, failing closed, each
approved caller bound to a key fingerprint."""
import types

import _keyfakes as kf
import pytest

from sessionphone import keygate
from sessionphone.bridge import calls

BOB = "bob@example.org"


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(calls, "CALLERS_FILE", tmp_path / "callers.txt")
    monkeypatch.setattr(calls, "PROFILE", "")
    monkeypatch.delenv("DC_CALLERS", raising=False)
    monkeypatch.delenv("PIP_CHAT_CALLERS", raising=False)
    started, logged = [], []
    monkeypatch.setattr(calls, "log", logged.append)
    monkeypatch.setattr(calls, "CallSession", lambda *a, **k: started.append(a) or types.SimpleNamespace(run=None))
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", lambda coro, loop: coro.close())
    b = types.SimpleNamespace(sessions={}, loop=None)
    b.incoming = types.MethodType(calls.CallManager.incoming, b)
    return b, started, logged, tmp_path


def event(chat=12, msg=7):
    return types.SimpleNamespace(msg_id=msg, chat_id=chat, place_call_info="sdp", has_video=False)


def ring(b, contact, msg=7):
    """A call from CONTACT, through CallManager.incoming."""
    b.incoming(kf.Account([contact], calls={msg: contact}), event(msg=msg))


def pinned(tmp, addr=BOB, fpr=kf.FPR_K1, note="Bob"):
    (tmp / "callers.txt").write_text(f"{keygate.MARKER}\n# the owner\n{addr}  key:{fpr}  # {note}\n")


def test_no_list_means_nobody_rings(bridge):
    b, started, logged, _ = bridge
    ring(b, kf.Contact(10, BOB, kf.K1))
    assert not started and "REFUSED" in logged[-1] and "0 approved" in logged[-1]


@pytest.mark.parametrize("var", ["DC_CALLERS", "PIP_CHAT_CALLERS"])
def test_a_caller_only_in_the_env_is_refused(bridge, monkeypatch, var):
    """PHONE-132 design-001 (f): an env var never grants the right to ring."""
    b, started, logged, _ = bridge
    monkeypatch.setenv(var, "Bob@Example.org, other@x.org")
    ring(b, kf.Contact(10, BOB, kf.K1))
    assert not started and "REFUSED" in logged[-1] and "0 approved" in logged[-1]
    assert "DC_CALLERS" not in logged[-1]                  # the help names the file only


def test_the_listed_address_holding_the_listed_key_rings(bridge):
    b, started, logged, tmp = bridge
    pinned(tmp)
    ring(b, kf.Contact(10, "BOB@example.org", kf.K1))
    assert started


def test_the_listed_address_holding_another_key_is_refused(bridge):
    b, started, logged, tmp = bridge
    pinned(tmp)
    ring(b, kf.Contact(11, BOB, kf.K2))
    assert not started
    assert "REFUSED" in logged[-1] and BOB in logged[-1] and "not the listed one" in logged[-1]


def test_the_listed_key_at_another_address_rings_and_says_so(bridge):
    b, started, logged, tmp = bridge
    pinned(tmp)
    ring(b, kf.Contact(10, "bob@new.example.org", kf.K1))
    assert started and any("now writes bob@new.example.org" in line for line in logged)


def test_an_unpinned_line_is_refused_and_names_the_pin(bridge):
    b, started, logged, tmp = bridge
    (tmp / "callers.txt").write_text(f"{keygate.MARKER}\n{BOB}  # Bob\n")
    ring(b, kf.Contact(10, BOB, kf.K1))
    assert not started and "callers pin bob@example.org" in logged[-1]


def test_an_address_contact_is_refused(bridge):
    b, started, logged, tmp = bridge
    pinned(tmp)
    contact = kf.Contact(10, BOB, kf.K1, key_contact=False)
    ring(b, contact)
    assert not started and "REFUSED" in logged[-1] and contact.vcards == 0


def test_a_vcard_with_no_key_is_refused(bridge):
    b, started, logged, tmp = bridge
    pinned(tmp)
    ring(b, kf.Contact(10, BOB, kf.K1, card=kf.vcard(BOB, None)))
    assert not started and "REFUSED" in logged[-1]


def test_a_vcard_error_is_refused(bridge):
    b, started, logged, tmp = bridge
    pinned(tmp)
    ring(b, kf.Contact(10, BOB, kf.K1, vcard_error=RuntimeError("rpc down")))
    assert not started and "REFUSED" in logged[-1]


def test_a_stranger_is_refused_even_with_a_list(bridge):
    b, started, logged, tmp = bridge
    pinned(tmp)
    ring(b, kf.Contact(12, "stranger@nine.testrun.org", kf.V6))
    assert not started and "stranger@nine.testrun.org" in logged[-1]


def test_an_unreadable_caller_is_refused(bridge):
    b, started, logged, tmp = bridge
    pinned(tmp)
    b.incoming(kf.Account([], fail=True), event())
    assert not started and any("REFUSED" in line for line in logged)


def test_on_a_profile_the_contact_request_check_still_runs_after_the_key(bridge, monkeypatch):
    b, started, logged, tmp = bridge
    pinned(tmp)
    monkeypatch.setattr(calls, "PROFILE", "public")
    contact = kf.Contact(10, BOB, kf.K1)
    acc = kf.Account([contact], calls={7: contact})
    acc.get_chat_by_id = lambda cid: types.SimpleNamespace(
        get_basic_snapshot=lambda: types.SimpleNamespace(is_contact_request=True))
    b.incoming(acc, event())
    assert not started and "contact request" in logged[-1]


# ---- the upgrade: a callers file from an older version ----------------------


OWNER = "owner@example.org"
OLD_FILE = f"{OWNER}  # owner, joined 2030-06-09T00:00:00+0000\n"     # what link wrote before


def test_the_owner_still_rings_after_the_upgrade(bridge):
    """An owner line from before keys (address only, as `link` wrote it) is
    pinned at the bot's first start to the owner's one key contact, and the
    owner's call is answered; a contact with the owner's address and another
    key is not."""
    b, started, logged, tmp = bridge
    f = tmp / "callers.txt"
    f.write_text(OLD_FILE)
    f.chmod(0o600)
    owner = kf.Contact(10, OWNER, kf.K1)
    keygate.migrate(kf.Account([owner]), f, logged.append)        # the bot's start hook
    ring(b, owner)
    assert started, logged
    started.clear()
    ring(b, kf.Contact(11, OWNER, kf.K2), msg=8)
    assert not started and "not the listed one" in logged[-1]


def test_two_keys_on_the_owner_address_at_the_upgrade_ring_neither(bridge):
    b, started, logged, tmp = bridge
    f = tmp / "callers.txt"
    f.write_text(OLD_FILE)
    f.chmod(0o600)
    a, c = kf.Contact(10, OWNER, kf.K1), kf.Contact(11, OWNER, kf.K2)
    keygate.migrate(kf.Account([a, c]), f, logged.append)
    for n, who in enumerate((a, c)):
        ring(b, who, msg=20 + n)
        assert not started and "callers pin owner@example.org" in logged[-1]

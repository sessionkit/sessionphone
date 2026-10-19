"""Who may ring the bot: an allowlist on every profile, failing closed."""
import types

import pytest

from sessionphone.bridge import calls


class FakeAccount:
    def __init__(self, address, fail=False):
        self.address, self.fail = address, fail

    def get_message_by_id(self, msg_id):
        if self.fail:
            raise RuntimeError("rpc down")
        contact = types.SimpleNamespace(get_snapshot=lambda: types.SimpleNamespace(address=self.address))
        return types.SimpleNamespace(get_snapshot=lambda: types.SimpleNamespace(sender=contact))


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(calls, "CALLERS_FILE", tmp_path / "callers.txt")
    monkeypatch.delenv("DC_CALLERS", raising=False)
    monkeypatch.delenv("PIP_CHAT_CALLERS", raising=False)
    started, logged = [], []
    monkeypatch.setattr(calls, "log", logged.append)
    monkeypatch.setattr(calls, "CallSession", lambda *a, **k: started.append(a) or types.SimpleNamespace(run=None))
    b = types.SimpleNamespace(sessions={}, loop=None)
    b.incoming = types.MethodType(calls.CallManager.incoming, b)
    return b, started, logged, tmp_path


def event(chat=12, msg=7):
    return types.SimpleNamespace(msg_id=msg, chat_id=chat, place_call_info="sdp", has_video=False)


def test_no_list_means_nobody_rings(bridge):
    b, started, logged, _ = bridge
    b.incoming(FakeAccount("bob@example.org"), event())
    assert not started and "REFUSED" in logged[-1] and "0 approved" in logged[-1]


@pytest.mark.parametrize("var", ["DC_CALLERS", "PIP_CHAT_CALLERS"])
def test_a_caller_only_in_the_env_is_refused(bridge, monkeypatch, var):
    """PHONE-132 design-001 (f): an env var never grants the right to ring."""
    b, started, logged, _ = bridge
    monkeypatch.setenv(var, "Bob@Example.org, other@x.org")
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", lambda coro, loop: coro.close())
    b.incoming(FakeAccount("bob@example.org"), event())
    assert not started and "REFUSED" in logged[-1] and "0 approved" in logged[-1]
    assert "DC_CALLERS" not in logged[-1]                  # the help names the file only


def test_a_listed_caller_rings_from_the_file_and_comments_are_ignored(bridge, monkeypatch):
    b, started, logged, tmp = bridge
    (tmp / "callers.txt").write_text("# the owner\nbob@example.org  # Bob\n")
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", lambda coro, loop: coro.close())
    b.incoming(FakeAccount("BOB@example.org"), event())
    assert started


def test_a_stranger_is_refused_even_with_a_list(bridge):
    b, started, logged, tmp = bridge
    (tmp / "callers.txt").write_text("bob@example.org\n")
    b.incoming(FakeAccount("stranger@nine.testrun.org"), event())
    assert not started and "stranger@nine.testrun.org" in logged[-1]


def test_an_unreadable_caller_is_refused(bridge):
    b, started, logged, tmp = bridge
    (tmp / "callers.txt").write_text("bob@example.org\n")
    b.incoming(FakeAccount(None, fail=True), event())
    assert not started and any("REFUSED" in l for l in logged)

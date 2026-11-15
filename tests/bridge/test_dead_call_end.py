"""A call whose connection dies is ended in the core, once (PHONE-299).

When the peer connection goes `failed` or `closed` and no CallEnded came from
the caller (ICE consent gave up, the network went away), close() tears the
bot's side down. Before PHONE-299 nobody told the core, so the caller's app
kept the call live and the caller talked into a dead call. Now that close runs
the core's end_call ONCE for the call's message: off the loop (it can block),
never holding close() or the transcript, a failure logged and never raised.
It is still a drop, so the drop line still posts.

A caller hang-up (CallEnded first) is the far side ending it: the core
already knows, so no end_call is sent, and nothing is raised or logged as a
traceback when the connection closes after it.

Fakes only: no account, no rpc-server, no call, no TTS.
"""
import asyncio
import re
import threading
import time
import types

import pytest

from sessionphone.bridge import calls

OWNER = "erin@relay.example"
LIST = f"family  chat:12   # the family group\nowner   {OWNER}\n"
DROP = re.compile(r"^call dropped \d\d:\d\d:\d\d after \d+ s; ")
MSG = 5531


class FakeChat:
    def __init__(self, account, cid):
        self.account, self.id = account, cid

    def get_basic_snapshot(self):
        return types.SimpleNamespace(chat_type="Group")

    def get_contacts(self):
        return []

    def send_text(self, text):
        self.account.posted.append((self.id, text))


class FakeAccount:
    """Records every end_call, by the message it was asked on."""

    def __init__(self):
        self.posted, self.ended = [], []
        self.on_end = None             # called inside end_call: an echo, a hang, a failure
        self.chats = {12: FakeChat(self, 12)}

    def get_chat_by_id(self, cid):
        return self.chats[cid]

    def get_message_by_id(self, n):
        def end_call():
            self.ended.append(n)
            if self.on_end is not None:
                self.on_end(n)
        return types.SimpleNamespace(end_call=end_call)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = types.SimpleNamespace(logs=[], account=FakeAccount(), path=tmp_path / "channels.txt")
    r.path.write_text(LIST)
    r.path.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(r.path))
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: "", lambda *a, **k: None))
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda *a: None)
    return r


class PC:
    async def close(self):
        pass


def _session(rig, connected=True, manager=None):
    """A live call, ready for the real close(); everything close() touches
    besides the drop line and end_call is a no-op, and the transcript is
    counted."""
    s = calls.CallSession.__new__(calls.CallSession)
    now = time.time()
    s.account, s.chat_id, s.msg_id = rig.account, 12, MSG
    s.pc, s.closed, s.answer_go, s.wait_task = PC(), None, None, None
    s.connected, s.ringing = connected, False
    s.started = now - 120 if connected else None
    s.last_heard = now - 70 if connected else None
    s.turn_pending, s.turn_parts, s.tasks = set(), [], []
    s.manager = manager or types.SimpleNamespace(forget=lambda x: None)
    s.heard = types.SimpleNamespace(event=lambda *a, **k: None)
    s.transcripts = []
    s.transcript_out = lambda: s.transcripts.append(True)
    for name in ("_roots_end", "_live_clear", "_turn_cancel", "_noreply_cancel", "_rec_end"):
        setattr(s, name, lambda *a: None)
    s._unanswered = lambda why: None
    return s


def _close(s, why, **kw):
    asyncio.run(s.close(why, **kw))     # asyncio.run waits for the executor: end_call is done


def _no_traceback(logs):
    assert not any("Traceback" in m for m in logs), logs


# ---- criterion 1: a dead connection is ended in the core, exactly once ----

@pytest.mark.parametrize("state", ["failed", "closed"])
def test_a_dead_connection_ends_the_call_once(rig, state) -> None:
    s = _session(rig)
    _close(s, "connection " + state)
    assert rig.account.ended == [MSG]
    assert s.transcripts == [True]
    _no_traceback(rig.logs)


@pytest.mark.parametrize("state", ["failed", "closed"])
def test_a_dead_connection_is_still_a_drop(rig, state) -> None:
    """end_call does not make it the bot's hang-up: the drop line posts, and
    the close line still names the path that closed it."""
    s = _session(rig)
    _close(s, "connection " + state)
    assert len(rig.account.posted) == 1 and DROP.match(rig.account.posted[0][1]), rig.account.posted
    (line,) = [m for m in rig.logs if f"closing msg {MSG}: " in m]
    assert f"connection {state}" in line, line
    assert "ended by this side" not in line, line


def test_a_second_close_ends_nothing_more(rig) -> None:
    s = _session(rig)
    _close(s, "connection failed")
    _close(s, "connection closed")      # pc.close() reports closed too: the pc is gone
    _close(s, "connection failed")
    assert rig.account.ended == [MSG]


def test_the_end_is_guarded_by_its_own_flag(rig) -> None:
    """Exactly once even if a second close of a dead connection reached the
    end, by the per-call flag, not only by the pc being gone."""
    s = _session(rig)
    _close(s, "connection failed")
    s.pc = PC()                         # as if a pc were back: the flag still holds
    _close(s, "connection closed")
    assert rig.account.ended == [MSG]


@pytest.fixture
def manager(rig):
    m = calls.CallManager()
    m.thread.start()
    yield m
    m.loop.call_soon_threadsafe(m.loop.stop)
    m.thread.join(5)
    m.loop.close()


def _drained(m):
    for _ in range(2):
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.2), m.loop).result(5)


def test_the_core_echoing_call_ended_back_ends_nothing_more(rig, manager) -> None:
    """The core reports the bot's own end_call back as CallEnded, while
    end_call still runs: that close neither ends the call again nor posts a
    second drop line."""
    s = _session(rig, manager=manager)
    manager.sessions = {MSG: s}

    def echo(n):
        manager.ended(n)
        time.sleep(0.2)                 # the echo's close() runs meanwhile
    rig.account.on_end = echo
    asyncio.run_coroutine_threadsafe(s.close("connection closed"), manager.loop).result(5)
    _drained(manager)
    assert rig.account.ended == [MSG]
    assert len(rig.account.posted) == 1, rig.account.posted
    assert s.transcripts == [True]
    _no_traceback(rig.logs)


def test_end_call_never_holds_close_or_the_transcript(rig) -> None:
    """end_call can block (see stop()): it runs off the loop, and the
    transcript goes while it is still running."""
    gate, order = threading.Event(), []

    def slow(n):
        gate.wait(5)
        order.append("end_call")
    rig.account.on_end = slow
    s = _session(rig)
    s.transcript_out = lambda: (order.append("transcript"), gate.set())
    _close(s, "connection failed")
    assert order == ["transcript", "end_call"], order


def test_an_end_call_that_fails_is_logged_never_raised(rig) -> None:
    def fail(n):
        raise RuntimeError("the core is gone")
    rig.account.on_end = fail
    s = _session(rig)
    _close(s, "connection failed")      # no exception out of close()
    assert s.transcripts == [True]
    assert any("end_call" in m and "the core is gone" in m for m in rig.logs), rig.logs
    _no_traceback(rig.logs)


def test_a_connection_that_died_before_it_connected_is_ended_too(rig) -> None:
    """Accepted in the core, never connected: the caller's app would wait on
    it, so it is ended; it was never a call, so no drop line."""
    s = _session(rig, connected=False)
    _close(s, "connection failed")
    assert rig.account.ended == [MSG]
    assert rig.account.posted == []


def test_a_call_with_no_message_ends_nothing(rig) -> None:
    s = _session(rig)
    s.msg_id = None                     # a dial that never got its message
    _close(s, "connection failed")
    assert rig.account.ended == []
    _no_traceback(rig.logs)


# ---- criterion 2: a caller hang-up sends no end_call ----

def test_a_caller_hang_up_then_the_connection_closing_ends_nothing(rig, manager) -> None:
    s = _session(rig, manager=manager)
    manager.sessions = {MSG: s}
    manager.ended(MSG)                  # the phone hung up: CallEnded first
    _drained(manager)
    asyncio.run_coroutine_threadsafe(s.close("connection closed"), manager.loop).result(5)
    _drained(manager)
    assert rig.account.ended == []
    assert len(rig.account.posted) == 1  # the caller's hang-up is still a drop, as before
    assert s.transcripts == [True]
    _no_traceback(rig.logs)


def test_call_ended_seen_before_a_dead_connection_closes_ends_nothing(rig, manager) -> None:
    """CallEnded reached the bot, but the connection's own close got to the
    loop first: the core already ended the call, so no end_call."""
    s = _session(rig, manager=manager)
    manager.sessions = {MSG: s}
    gate = threading.Event()

    async def hold():
        gate.wait(5)                    # holds the loop, so both closes queue behind it
    held = asyncio.run_coroutine_threadsafe(hold(), manager.loop)
    fut = asyncio.run_coroutine_threadsafe(s.close("connection closed"), manager.loop)  # runs first
    manager.ended(MSG)                  # seen on the event thread; its close queues after
    gate.set()
    held.result(5)
    fut.result(5)
    _drained(manager)
    assert rig.account.ended == []
    _no_traceback(rig.logs)


@pytest.mark.parametrize("by_bot", [True, False])
def test_a_close_the_bot_is_already_ending_sends_no_second_end(rig, by_bot) -> None:
    """dc-hangup and stop() mark bot_ends and send their own end_call; a
    connection that closes under them is not a second end."""
    s = _session(rig)
    s.bot_ends = True
    _close(s, "connection closed", by_bot=by_bot)
    assert rig.account.ended == []


def test_a_bot_close_by_any_other_path_sends_no_end(rig) -> None:
    s = _session(rig)
    _close(s, "hung up by the agent", by_bot=True)
    assert rig.account.ended == []


# ---- PHONE-297's silent-call rule stands: no auto-hang-up ----

def test_a_silent_live_call_is_not_ended(rig, monkeypatch) -> None:
    """A connected call with no audio for over LIVE_SILENT_S is passed by as
    not live, and stays open: only a dead connection (or dc-hangup) ends it."""
    m = calls.CallManager.__new__(calls.CallManager)
    s = _session(rig)
    s.last_heard = time.time() - calls.LIVE_SILENT_S - 60
    m.sessions = {MSG: s}
    assert m.live_for_chat(12) == []
    assert rig.account.ended == [] and s.pc is not None

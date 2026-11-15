"""A call that drops says so in its chat: one plain line, no speech.

When a connected call closes and the bot did not end it (the connection
failed, the phone's core sent CallEnded, the peer went away), close() posts
ONE line to the call's chat through post_to_chat, so the channels list still
decides: when it dropped, how long the call ran, and how long before the close
the caller's last audio was heard. A caller talking into a dead call learns it
from the chat. A call the bot ended (dc-hangup, a decline while ringing, the
bot being stopped) posts nothing: the bot says so with by_bot=True, never by
the wording of its reason. The post runs off the event loop; a send that fails
is logged and never stops the transcript.

Fakes only: no account, no rpc-server, no call, no TTS.
"""
import asyncio
import re
import sys
import time
import types

import numpy as np
import pytest
from av import AudioFrame

from sessionphone.bridge import calls

OWNER = "erin@relay.example"
LIST = f"family  chat:12   # the family group\nowner   {OWNER}\n"
LINE = re.compile(r"^call dropped (\d\d:\d\d:\d\d) after (\d+) s; last audio heard (\d+) s before the close$")


class FakeChat:
    def __init__(self, account, cid):
        self.account, self.id = account, cid

    def get_basic_snapshot(self):
        return types.SimpleNamespace(chat_type="Group")

    def get_contacts(self):
        return []

    def send_text(self, text):
        if self.account.broken:
            raise OSError("the relay is down")
        self.account.posted.append((self.id, text))


class FakeAccount:
    def __init__(self):
        self.posted, self.broken = [], False
        self.chats = {12: FakeChat(self, 12), 13: FakeChat(self, 13)}

    def get_chat_by_id(self, cid):
        return self.chats[cid]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = types.SimpleNamespace(logs=[], account=FakeAccount(), tmp=tmp_path,
                              path=tmp_path / "channels.txt")
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


def _session(rig, chat_id=12, connected=True, ran=359.0, heard_ago=24.0):
    """A live call, ready for the real close(); everything close() touches
    besides the drop line is a no-op, and the transcript is counted."""
    s = calls.CallSession.__new__(calls.CallSession)
    now = time.time()
    s.account, s.chat_id, s.msg_id = rig.account, chat_id, 5531
    s.pc, s.closed, s.answer_go, s.wait_task = PC(), None, None, None
    s.connected = connected
    s.started = now - ran if connected else None
    s.last_heard = None if heard_ago is None else now - heard_ago
    s.turn_pending, s.turn_parts, s.tasks = set(), [], []
    s.manager = types.SimpleNamespace(forget=lambda x: None)
    s.heard = types.SimpleNamespace(event=lambda *a, **k: None)
    s.transcripts = []
    s.transcript_out = lambda: s.transcripts.append(True)
    for name in ("_roots_end", "_live_clear", "_turn_cancel", "_noreply_cancel", "_rec_end"):
        setattr(s, name, lambda *a: None)
    s._unanswered = lambda why: None
    return s


def _close(s, why, **kw):
    asyncio.run(s.close(why, **kw))     # asyncio.run waits for the executor: the post is done


# ---- the words ----

def test_the_line_names_the_close_time_the_length_and_the_last_audio() -> None:
    closed_at = 1_800_000_000.4
    want = time.strftime("%H:%M:%S", time.localtime(closed_at))
    assert calls.drop_line(closed_at, closed_at - 359.2, closed_at - 24.3) == \
        f"call dropped {want} after 359 s; last audio heard 24 s before the close"


def test_a_call_that_never_heard_a_frame_says_so() -> None:
    closed_at = 1_800_000_000.0
    want = time.strftime("%H:%M:%S", time.localtime(closed_at))
    assert calls.drop_line(closed_at, closed_at - 61, None) == \
        f"call dropped {want} after 61 s; no audio was heard from the caller"


def test_a_clock_step_never_gives_a_negative_number() -> None:
    closed_at = 1_800_000_000.0
    line = calls.drop_line(closed_at, closed_at + 5, closed_at + 2)
    assert "after 0 s" in line and "heard 0 s before" in line, line


# ---- close(): who ended it decides ----

def test_a_dropped_call_posts_one_line_to_its_chat(rig) -> None:
    s = _session(rig)
    _close(s, "connection failed")
    (post,) = rig.account.posted
    assert post[0] == 12
    m = LINE.match(post[1])
    assert m, post[1]
    assert abs(int(m.group(2)) - 359) <= 1 and abs(int(m.group(3)) - 24) <= 1, post[1]
    assert s.transcripts == [True]                 # the transcript still goes


def test_a_core_call_ended_event_is_a_drop_too(rig) -> None:
    s = _session(rig)
    _close(s, "CallEnded event")
    assert len(rig.account.posted) == 1 and LINE.match(rig.account.posted[0][1])


def test_a_call_the_bot_ended_posts_nothing(rig) -> None:
    s = _session(rig)
    _close(s, "hung up", by_bot=True)
    assert rig.account.posted == []
    assert s.transcripts == [True]


def test_the_reason_text_is_never_read(rig) -> None:
    # words that read like a bot hang-up, but nobody said by_bot: it is a drop
    s = _session(rig)
    _close(s, "hung up by the agent")
    assert len(rig.account.posted) == 1


def test_a_call_that_never_connected_posts_nothing(rig) -> None:
    s = _session(rig, connected=False)
    _close(s, "connection failed")
    assert rig.account.posted == []


def test_a_second_close_posts_nothing_more(rig) -> None:
    s = _session(rig)
    _close(s, "connection failed")
    _close(s, "connection closed")             # the pc is gone: close() returns early
    assert len(rig.account.posted) == 1


def test_no_frame_ever_posts_the_no_audio_line(rig) -> None:
    s = _session(rig, heard_ago=None)
    _close(s, "connection failed")
    (post,) = rig.account.posted
    assert post[1].endswith("; no audio was heard from the caller"), post[1]


def test_an_unlisted_chat_is_refused_and_logged(rig) -> None:
    s = _session(rig, chat_id=13)
    _close(s, "connection failed")
    assert rig.account.posted == []
    assert any(m.startswith("CALL REFUSED drop line to chat 13") for m in rig.logs), rig.logs


def test_a_send_that_fails_is_logged_and_never_raised(rig) -> None:
    rig.account.broken = True
    s = _session(rig)
    _close(s, "connection failed")                 # no exception out of close()
    assert s.transcripts == [True]
    assert any("drop line" in m and "the relay is down" in m for m in rig.logs), rig.logs


def test_the_post_never_waits_on_the_loop(rig, monkeypatch) -> None:
    """A send that hangs must not hold close(): the transcript goes first."""
    import threading
    gate, order = threading.Event(), []

    def slow(account, chat_id, text, what):
        gate.wait(5)
        order.append("posted")
        return True
    monkeypatch.setattr(calls, "post_to_chat", slow)
    s = _session(rig)
    s.transcript_out = lambda: (order.append("transcript"), gate.set())

    async def go():
        await s.close("connection failed")
    asyncio.run(go())
    assert order == ["transcript", "posted"], order


# ---- the three bot-ended paths say by_bot=True ----

class Recorder:
    def __init__(self, chat_id, ringing, live):
        self.chat_id, self.ringing, self.msg_id = chat_id, ringing, 7 if ringing else 8
        self.pc = object() if live else None
        self.closes = []
        self.account = types.SimpleNamespace(get_message_by_id=lambda n: types.SimpleNamespace(
            end_call=lambda: None))

    async def close(self, why, by_bot=False):
        self.closes.append(by_bot)

    def _live_clear(self):
        pass


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, lambda p: "", lambda *a, **k: None))
    m = calls.CallManager()
    m.thread.start()
    yield m
    m.loop.call_soon_threadsafe(m.loop.stop)
    m.thread.join(5)
    m.loop.close()


def _settle(m):
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), m.loop).result(5)


def test_dc_hangup_and_a_decline_close_with_by_bot(manager) -> None:
    ringing, live = Recorder(12, True, False), Recorder(12, False, True)
    manager.sessions = {7: ringing, 8: live}
    assert manager.hangup(12)
    _settle(manager)
    assert ringing.closes == [True] and live.closes == [True]


def test_a_bot_being_stopped_closes_with_by_bot(manager) -> None:
    s = Recorder(12, False, True)
    manager.sessions = {8: s}
    assert manager.stop("a test stop", budget=2) == 1
    assert s.closes == [True]


# ---- the last frame heard is stamped on the Session ----

class Track:
    def __init__(self, n):
        self.n = n

    async def recv(self):
        if not self.n:
            raise ConnectionError("call ended")
        self.n -= 1
        f = AudioFrame.from_ndarray(np.zeros((1, 960), dtype=np.int16), format="s16", layout="mono")
        f.sample_rate = 48000
        return f


def _bare():
    s = calls.CallSession.__new__(calls.CallSession)
    assert s.last_heard is None                    # a call that heard nothing yet
    return s


def test_drain_stamps_the_last_frame() -> None:
    s, t0 = _bare(), time.time()
    asyncio.run(s._drain(Track(2)))
    assert s.last_heard is not None and s.last_heard >= t0


def test_listen_stamps_the_last_frame(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls, "Utterances", lambda d, tape=None: None)
    monkeypatch.setattr(calls.earleg, "feed", lambda *a: None)
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, lambda p: "", lambda *a, **k: None))
    s, t0 = _bare(), time.time()
    s.dir, s._rec_start, s._feed = tmp_path / "call", lambda: None, lambda vad, pcm: None
    asyncio.run(s._listen(Track(3)))
    assert s.last_heard is not None and s.last_heard >= t0


def test_listen_ears_stamps_the_last_frame(tmp_path, monkeypatch) -> None:
    class Pipeline:
        def __init__(self, *a, **k):
            self.b, self.edges = types.SimpleNamespace(name="fake"), None

        def feed(self, pcm):
            pass

        def close(self):
            pass
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, lambda p: "", lambda *a, **k: None))
    monkeypatch.setattr(calls.earleg, "feed", lambda *a: None)
    monkeypatch.setitem(sys.modules, "ears", types.ModuleType("ears"))
    monkeypatch.setitem(sys.modules, "ears.cli", types.SimpleNamespace(Pipeline=Pipeline))
    s, t0 = _bare(), time.time()
    s.dir = tmp_path / "call"
    asyncio.run(s._listen_ears(Track(3)))
    assert s.last_heard is not None and s.last_heard >= t0


# ---- the core may report our own end_call back as CallEnded ----
# The bot's end_call runs in the executor, so the loop is free while it runs. If
# the core sends CallEnded for that end before end_call returns, the event's
# close("CallEnded event") can run first, with no by_bot. The call is still the
# bot's to end: hangup() and stop() mark it before they ask the core.

def _echoing(rig, m):
    """A live call whose end_call is reported back as CallEnded, the way the
    event thread would report it, while end_call is still running."""
    s = _session(rig)
    s.ringing, s.manager = False, m

    def end_call():
        m.ended(s.msg_id)
        time.sleep(0.2)                  # the echo's close() runs meanwhile
    s.account = types.SimpleNamespace(
        get_message_by_id=lambda n: types.SimpleNamespace(end_call=end_call),
        get_chat_by_id=rig.account.get_chat_by_id)
    m.sessions = {s.msg_id: s}
    return s


@pytest.fixture
def echo_manager(rig):
    m = calls.CallManager()
    m.thread.start()
    yield m
    m.loop.call_soon_threadsafe(m.loop.stop)
    m.thread.join(5)
    m.loop.close()


def _drained(m):
    _settle(m)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.3), m.loop).result(5)


def test_dc_hangup_echoed_as_call_ended_posts_nothing(rig, echo_manager) -> None:
    s = _echoing(rig, echo_manager)
    assert echo_manager.hangup(12)
    time.sleep(0.5)
    _drained(echo_manager)
    assert s.pc is None and s.transcripts == [True]
    assert rig.account.posted == []


def test_a_stop_echoed_as_call_ended_posts_nothing(rig, echo_manager) -> None:
    s = _echoing(rig, echo_manager)
    assert echo_manager.stop("a test stop", budget=2) == 1
    _drained(echo_manager)
    assert s.pc is None and s.transcripts == [True]
    assert rig.account.posted == []


def test_a_call_ended_the_bot_did_not_ask_for_is_still_a_drop(rig, echo_manager) -> None:
    s = _echoing(rig, echo_manager)
    echo_manager.ended(s.msg_id)         # the phone hung up: nobody marked it
    _drained(echo_manager)
    assert len(rig.account.posted) == 1 and LINE.match(rig.account.posted[0][1])

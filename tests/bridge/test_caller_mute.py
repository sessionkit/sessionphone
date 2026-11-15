"""A caller's mute reaches the seat on the call (PHONE-303).

The caller's app sends its mute state on the negotiated `mutedState` data
channel: {"videoEnabled": ..., "audioEnabled": ...}. Call 140081: the iPad
sent audioEnabled false and the caller talked for 9 minutes, unheard, while
the bridge only logged it. Now each CHANGE is one line to the seat, by the
call's own mail route (threaded on the call root), and nothing is spoken:

- audioEnabled false: one MUTED line;
- audioEnabled true after a mute: one UNMUTED line;
- the same state again: nothing;
- bad JSON, or no audioEnabled key: logged, nothing mailed.

The real _build_pc runs against a fake peer connection, so the handler under
test is the one the call registers on the channel.
"""
import asyncio
import json
import types

import pytest

from sessionphone.bridge import calls


class FakeChannel:
    def __init__(self, label):
        self.label, self.handlers, self.sent = label, {}, []

    def on(self, event):
        def register(fn):
            self.handlers[event] = fn
            return fn
        return register

    def send(self, data):
        self.sent.append(data)


class FakePC:
    connectionState = "new"

    def __init__(self, _config=None):
        self.channels, self.handlers, self.tracks = {}, {}, []

    def createDataChannel(self, label, **_kw):
        ch = self.channels[label] = FakeChannel(label)
        return ch

    def on(self, event):
        def register(fn):
            self.handlers[event] = fn
            return fn
        return register

    def addTrack(self, track):
        self.tracks.append(track)


class Rig:
    """One CallSession, its pc faked, and every mail and line it spoke."""

    def __init__(self, monkeypatch):
        self.logged, self.mailed, self.spoken = [], [], []
        monkeypatch.setattr(calls, "log", self.logged.append)
        monkeypatch.setattr(calls, "RTCPeerConnection", FakePC)
        monkeypatch.setattr(calls, "watch_ice", lambda pc: None)
        monkeypatch.setattr(calls, "_hooks", types.SimpleNamespace(
            mail_in=lambda *a, **k: self.mailed.append((a, k))))
        s = object.__new__(calls.CallSession)
        s.account = types.SimpleNamespace(ice_servers=lambda: [])
        s.chat_id, s.msg_id = 12, 7
        s.chat_name, s.caller = "Bob Team", "Bob"
        s.root_id = "call-12-1@test"
        s.voice = object()

        async def speak(text, *a, **k):
            self.spoken.append(text)
        s.speak = speak
        self.s = s

    async def build(self):
        pc = await self.s._build_pc()
        return pc.channels["mutedState"].handlers["message"]

    async def settle(self):
        """Let the executor's mail land (it runs off the event loop)."""
        for _ in range(20):
            await asyncio.sleep(0.01)


def muted(audio, video=False):
    return json.dumps({"videoEnabled": video, "audioEnabled": audio})


def run(rig, *messages):
    async def go():
        on_muted = await rig.build()
        for m in messages:
            on_muted(m)
            await rig.settle()
    asyncio.run(go())


def bodies(rig):
    return [a[3] for a, _k in rig.mailed]


def test_a_mute_is_one_line_to_the_seat_threaded_on_the_call(monkeypatch):
    r = Rig(monkeypatch)
    run(r, muted(False))
    (args, kw), = r.mailed
    assert args[0] == "Bob Team" and args[1] == 12
    assert kw["in_reply_to"] == "call-12-1@test"
    assert "MUTED" in kw["subject"] and "Bob" in kw["subject"]
    assert "muted" in args[3].lower() and "Bob" in args[3]
    assert not r.spoken, "the bridge must not speak on a mute"


def test_an_unmute_after_a_mute_is_one_unmuted_line(monkeypatch):
    r = Rig(monkeypatch)
    run(r, muted(False), muted(True))
    assert len(r.mailed) == 2
    (_a1, k1), (a2, k2) = r.mailed
    assert "UNMUTED" not in k1["subject"]
    assert "UNMUTED" in k2["subject"]
    assert k2["in_reply_to"] == "call-12-1@test"
    assert "unmuted" in a2[3].lower()
    assert not r.spoken


def test_repeats_of_the_same_state_send_nothing_more(monkeypatch):
    r = Rig(monkeypatch)
    run(r, muted(False), muted(False), muted(False, video=True),
        muted(True), muted(True))
    subjects = [k["subject"] for _a, k in r.mailed]
    assert len(subjects) == 2, subjects
    assert "UNMUTED" not in subjects[0] and "UNMUTED" in subjects[1]


def test_unmuted_at_the_start_is_not_news(monkeypatch):
    """The call starts unmuted: an audioEnabled true first says nothing."""
    r = Rig(monkeypatch)
    run(r, muted(True), muted(True, video=True))
    assert not r.mailed


@pytest.mark.parametrize("bad", [
    "not json", "", '{"videoEnabled": false}', "[1, 2]", '"audio"',
    '{"audioEnabled": "no"}', b"\xff\xfe",
])
def test_bad_json_or_no_audio_key_is_logged_and_mails_nothing(monkeypatch, bad):
    r = Rig(monkeypatch)
    run(r, bad)
    assert not r.mailed
    assert any("mutedState" in line for line in r.logged), r.logged


def test_bad_message_between_states_does_not_reset_the_state(monkeypatch):
    r = Rig(monkeypatch)
    run(r, muted(False), "junk", muted(False))
    assert len(r.mailed) == 1


def test_a_failed_send_is_logged_not_raised(monkeypatch):
    r = Rig(monkeypatch)

    def boom(*a, **k):
        raise OSError("mta down")
    monkeypatch.setattr(calls, "_hooks", types.SimpleNamespace(mail_in=boom))
    run(r, muted(False))
    assert any("mute" in line.lower() and "mail failed" in line for line in r.logged), r.logged

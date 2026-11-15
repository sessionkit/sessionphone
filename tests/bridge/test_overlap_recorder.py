"""Two calls on one chat, recording on: each call keeps its own tape (PHONE-297).

The call recorder (full-in.wav + full-out.wav) was ONE module global. A
call's _rec_start opened a Recorder only if none was open, and any call's
close (or a mid-call switch-off) closed whichever Recorder was open. With
call 52 held open, call 62 recorded into 52's Recorder, in 52's directory,
and 52's close ended 62's recording: "62's recording closed at 472 s" for a
one-minute call.

The rules these tests pin:

- each call opens its own Recorder, in its own directory;
- a call's inbound audio (the VAD path and the ears path) and its outbound
  audio (its voice track) go to its own tape only;
- closing either call, old or new, closes its own tape only: the other call
  keeps recording to its own hang-up;
- recording turned off mid-call is read per call: it closes that call's
  tape, never the other call's.
"""
import asyncio
import sys
import types
import wave

import numpy as np
import pytest
from av import AudioFrame

from sessionphone.bridge import calls, recording

CHAT = 12
OLD, NEW = 52, 62           # the incident's ids: 52 held open, 62 the new call


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """CALLS_DIR in tmp, recording ON by env, the energy path, every CALL line kept."""
    ns = types.SimpleNamespace(logs=[], root=tmp_path / "calls")
    ns.root.mkdir()
    monkeypatch.setattr(calls, "CALLS_DIR", ns.root)
    monkeypatch.setattr(recording, "RECORD_DRAIN", 0.0)
    monkeypatch.setattr(calls, "_last_prune", None)
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda *a: None)
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(ns.logs.append, lambda p: "",
                                                     lambda *a, **k: None))
    monkeypatch.setenv("EARS_RECORD", "on")
    return ns


def session(rig, msg_id):
    """A call that is up in chat 12, with what _listen, voice.recv and close() need."""
    s = object.__new__(calls.CallSession)
    s.dir = rig.root / f"20300609-12345{msg_id % 10}-chat{CHAT}-msg{msg_id}"
    s.chat_id, s.msg_id = CHAT, msg_id
    s.voice, s.echo = calls.PipVoiceTrack(), calls.EchoGuard(log=lambda m: None)
    s.lines, s.turn_parts, s.turn_pending, s.tasks = [], [], set(), []
    s.turn_timer, s.speaking, s.chunk_n = None, False, 0
    s.answer_go, s.closed, s.connected = None, None, False
    s.manager = types.SimpleNamespace(forget=lambda x: None, sessions={})
    s.awaiting, s.ear_taps = (), []
    s._live_clear = lambda: None
    s._turn_arm = lambda: None

    async def no_turn():
        s.turn_parts = []
    s._turn_out = no_turn

    class PC:
        async def close(self):
            pass
    s.pc = PC()
    return s


def quiet_frame():
    """20 ms of silence at 48 kHz: on the tape, and never a chunk for STT."""
    f = AudioFrame.from_ndarray(np.zeros((1, 960), dtype=np.int16), format="s16", layout="mono")
    f.sample_rate = 48000
    return f


class Caller:
    """N frames from the caller, then his track ends."""

    def __init__(self, n):
        self.left = n

    async def recv(self):
        if not self.left:
            raise ConnectionError("track ended")
        self.left -= 1
        await asyncio.sleep(0)
        return quiet_frame()


class Tally:
    """What each call fed its own tape, in samples: in at 16 kHz, out at 48 kHz."""

    def __init__(self):
        self.inn, self.out = {}, {}

    async def hear(self, s, n, listen=None):
        """n frames from the caller into call s, through its listen path."""
        await (listen or s._listen)(Caller(n))
        self.inn[s.msg_id] = self.inn.get(s.msg_id, 0) + s.wav_samples

    async def say(self, s, n):
        """n frames out of call s's voice track, as the peer connection pulls them."""
        for _ in range(n):
            await s.voice.recv()
        self.out[s.msg_id] = self.out.get(s.msg_id, 0) + n * calls.OUT_SAMPLES


def tape(s):
    """(samples in full-in.wav, samples in full-out.wav) for call s."""
    got = []
    for name in calls.Recorder.NAMES:
        with wave.open(str(s.dir / name)) as w:
            got.append(w.getnframes())
    return tuple(got)


def closed_lines(rig):
    """The recording_closed lines, without their call stamp."""
    return [m.split(" [call ")[0] for m in rig.logs if "recording_closed" in m]


def test_each_call_opens_its_own_tape_in_its_own_directory(rig) -> None:
    old, new = session(rig, OLD), session(rig, NEW)
    old._rec_start()
    new._rec_start()                       # 52 is still recording
    for s in (old, new):
        for name in calls.Recorder.NAMES:
            assert (s.dir / name).exists(), f"call {s.msg_id} has no {name} of its own"
    started = [m for m in rig.logs if "recording_started" in m]
    assert started == [f"CALL recording_started {old.dir.name} in+out",
                       f"CALL recording_started {new.dir.name} in+out"]


@pytest.mark.parametrize("first", ["old", "new"])
def test_closing_one_call_never_closes_or_truncates_the_others_tape(rig, first) -> None:
    """52 is held open; 62 comes in. Both talk and listen. One closes; the
    other keeps going to its own hang-up. Each tape holds exactly its own
    call's audio, and each is closed once, by its own call."""
    old, new = session(rig, OLD), session(rig, NEW)
    t = Tally()
    gone, stays = (old, new) if first == "old" else (new, old)

    async def run():
        await t.hear(old, 6)
        await t.say(old, 3)
        await t.hear(new, 10)              # 62 comes in while 52 is still recording
        await t.say(new, 4)
        await gone.close("hung up")
        assert len(closed_lines(rig)) == 2, \
            f"msg {gone.msg_id}'s close closed some other tape too: {closed_lines(rig)}"
        await t.hear(stays, 8)             # after the other call's close: still on tape
        await t.say(stays, 5)
        await stays.close("hung up")
    asyncio.run(run())

    for s in (old, new):
        assert tape(s) == (t.inn[s.msg_id], t.out[s.msg_id]), \
            f"call {s.msg_id}'s tape is not its own audio"
    assert closed_lines(rig) == [
        f"CALL recording_closed full-in.wav {t.inn[gone.msg_id] / calls.IN_RATE:.1f}s",
        f"CALL recording_closed full-out.wav {t.out[gone.msg_id] / calls.OUT_RATE:.1f}s",
        f"CALL recording_closed full-in.wav {t.inn[stays.msg_id] / calls.IN_RATE:.1f}s",
        f"CALL recording_closed full-out.wav {t.out[stays.msg_id] / calls.OUT_RATE:.1f}s",
    ]

class Pipeline:
    def __init__(self, *a, **k):
        self.b, self.edges = types.SimpleNamespace(name="fake"), None

    def feed(self, pcm):
        pass

    def close(self):
        pass


def test_on_the_ears_path_each_call_hears_into_its_own_tape(rig, monkeypatch) -> None:
    monkeypatch.setattr(calls, "EARS", True)
    monkeypatch.setitem(sys.modules, "ears", types.ModuleType("ears"))
    monkeypatch.setitem(sys.modules, "ears.cli", types.SimpleNamespace(Pipeline=Pipeline))
    old, new = session(rig, OLD), session(rig, NEW)
    t = Tally()

    async def run():
        await t.hear(old, 5, old._listen_ears)
        await t.hear(new, 9, new._listen_ears)
        await old.close("hung up")
        await t.hear(new, 7, new._listen_ears)
        await new.close("hung up")
    asyncio.run(run())
    assert tape(old)[0] == t.inn[OLD]
    assert tape(new)[0] == t.inn[NEW]


def test_recording_turned_off_in_one_call_closes_only_its_tape(rig, tmp_path, monkeypatch) -> None:
    """The switch is read per call, at that call's units: an off read by 62
    takes 62's tape back. 52's tape is 52's, and stays open."""
    old, new = session(rig, OLD), session(rig, NEW)
    old._rec_start()
    new._rec_start()
    conf = tmp_path / "ears.json"
    conf.write_text('{"record": false}')
    monkeypatch.delenv("EARS_RECORD")
    monkeypatch.setattr(calls, "_switch", recording.Switch(None, env={}, path=str(conf),
                                                           log=lambda m: None))
    new._rec_check()                       # 62's next unit reads off
    assert any("turned off mid-call" in m for m in rig.logs)
    t = Tally()

    async def run():
        await t.hear(old, 6)
        await t.say(old, 2)
        conf.write_text('{"record": true, "x": 1}')   # on again, so 52's hang-up keeps it
        await old.close("hung up")
    asyncio.run(run())
    assert tape(old) == (t.inn[OLD], t.out[OLD]), "52's tape stopped when 62's recording was turned off"

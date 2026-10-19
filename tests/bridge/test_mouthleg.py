"""The mouth leg: a line from the mouth goes into the live call, and a cut or
a dead mouth drops what is queued. A real socket and a real loop; a fake call.
"""
import asyncio
import json
import struct
import tempfile
import pathlib

import numpy as np

from sessionphone.bridge import mouthleg
import pytest  # noqa: E402,F401 - bin/bridge-check runs a file under pytest only if it imports it


class Voice:
    def __init__(self):
        self.q, self.cuts = [], 0

    def say(self, pcm):
        self.q.append(pcm)

    def mix(self, pcm):
        self.mixed = getattr(self, "mixed", []) + [len(pcm)]
        return len(pcm) / 48000

    def cut(self):
        n = sum(len(a) for a in self.q)
        self.q.clear()
        self.cuts += 1
        return n / 48000


class Echo:
    def __init__(self):
        self.clips, self.cuts = [], 0

    def spoken(self, text, dur, at=None, source="bridge"):
        self.clips.append((text, round(dur, 3), source))

    def cut(self, at=None):
        self.cuts += 1
        return "cut"


class Session:
    def __init__(self, live=True):
        self.pc = object() if live else None
        self.voice, self.echo, self.lines = Voice(), Echo(), []


class Manager:
    def __init__(self, sessions):
        self.sessions = sessions


def frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


def pcm(seconds):
    return (np.ones(int(48000 * seconds), dtype="<i2") * 100).tobytes()


async def exchange(mgr, header, frames=(), close_early=False):
    path = pathlib.Path(tempfile.mkdtemp(prefix="ml-")) / "s.sock"
    logs = []
    server = await mouthleg.serve(mgr, path, logs.append)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    r, w = await asyncio.open_unix_connection(str(path))
    w.write((json.dumps(header) + "\n").encode())
    first = json.loads(await r.readline())
    last = None
    if first.get("ok") and "probe" not in header:
        for f in frames:
            w.write(f)
        await w.drain()
        if close_early:
            w.close()
            await asyncio.sleep(0.1)
        else:
            last = json.loads(await r.readline())
    server.close()
    return first, last, logs


def run(coro):
    return asyncio.run(coro)


def test_probe_lists_live_calls():
    mgr = Manager({5531: Session(), 9: Session(live=False)})
    first, _, _ = run(exchange(mgr, {"probe": True}))
    assert first["ok"] and first["calls"] == [5531]


def test_refuses_a_call_that_is_not_live():
    mgr = Manager({5531: Session()})
    first, _, _ = run(exchange(mgr, {"call": 77, "text": "hi"}))
    assert not first["ok"] and "77" in first["error"] and first["calls"] == [5531]


def test_refuses_the_wrong_rate():
    mgr = Manager({1: Session()})
    first, _, _ = run(exchange(mgr, {"call": 1, "rate": 24000}))
    assert not first["ok"] and "48000" in first["error"]


def test_a_whole_line_is_queued_and_told_to_the_echo_guard():
    s = Session()
    mgr = Manager({1: s})
    first, last, _ = run(exchange(mgr, {"call": 1, "text": "Morning Bob.", "who": "carol"},
                                  [frame(b"A", pcm(0.3)), frame(b"A", pcm(0.3)), frame(b"D")]))
    assert first == {"ok": True, "call": 1}
    assert last["ended"] == "done" and last["queued_s"] == 0.6 and last["dropped_s"] == 0.0
    assert sum(len(a) for a in s.voice.q) == int(48000 * 0.6) and s.voice.cuts == 0
    assert sum(c[1] for c in s.echo.clips) == 0.6
    assert {c[0] for c in s.echo.clips} == {"Morning Bob."}
    assert {c[2] for c in s.echo.clips} == {"mouth"}
    assert s.lines[0][1:] == ("Carol", "Morning Bob.")


def test_a_cut_drops_what_is_queued():
    s = Session()
    mgr = Manager({1: s})
    _, last, _ = run(exchange(mgr, {"call": 1, "text": "A long line."},
                              [frame(b"A", pcm(0.2)), frame(b"C")]))
    assert last["ended"] == "cut" and last["dropped_s"] == 0.2
    assert s.voice.q == [] and s.voice.cuts == 1 and s.echo.cuts == 1
    assert s.lines[0][2] == "A long line. [cut]"


def test_a_mouth_that_dies_is_a_cut():
    s = Session()
    mgr = Manager({1: s})
    run(exchange(mgr, {"call": 1, "text": "gone"}, [frame(b"A", pcm(0.1))], close_early=True))
    assert s.voice.q == [] and s.voice.cuts == 1


def test_a_bad_frame_is_an_error_and_a_cut():
    s = Session()
    mgr = Manager({1: s})
    _, last, logs = run(exchange(mgr, {"call": 1}, [frame(b"A", pcm(0.1)), frame(b"Z")]))
    assert last["ended"] == "error" and not last["ok"] and s.voice.cuts == 1


def test_the_call_ending_mid_line_ends_the_line():
    s = Session()
    mgr = Manager({1: s})

    class Hangup(Voice):
        def say(self, pcm):
            super().say(pcm)
            s.pc = None             # the call closes while the line is going in
    s.voice = Hangup()
    _, last, _ = run(exchange(mgr, {"call": 1}, [frame(b"A", pcm(0.1)), frame(b"A", pcm(0.1)),
                                                  frame(b"D")]))
    assert last["ended"] == "call-ended" and not last["ok"]
    assert "ended mid-line" in last["error"]



# -- sounds follow the ears into the call --

def test_a_sound_is_mixed_never_queued_transcribed_or_cut():
    s = Session()
    s.voice.say(np.zeros(4800, dtype=np.int16))                      # a line is playing
    first, last, logs = run(exchange(Manager({7: s}), {"call": 7, "rate": 48000, "sound": True, "name": "turn"},
                                     [frame(b"A", pcm(0.045)), frame(b"D")]))
    assert first == {"ok": True, "call": 7, "sound": True}
    assert last["ok"] and last["sound"] and last["mixed_s"] == 0.045
    assert s.voice.mixed == [2160] and len(s.voice.q) == 1 and s.voice.cuts == 0
    assert s.lines == [] and s.echo.clips == [] and s.echo.cuts == 0


def test_a_sound_cut_short_by_eof_cuts_nothing():
    s = Session()
    s.voice.say(np.zeros(4800, dtype=np.int16))
    run(exchange(Manager({7: s}), {"call": 7, "rate": 48000, "sound": True}, [frame(b"A", pcm(0.02))],
                 close_early=True))
    assert s.voice.cuts == 0 and len(s.voice.q) == 1 and s.lines == []


def test_the_track_mixes_a_sound_over_the_voice_and_a_cut_leaves_it(monkeypatch):
    import types
    from sessionphone.bridge import calls
    from sessionphone.bridge.calls import OUT_SAMPLES, PipVoiceTrack
    monkeypatch.setattr(calls, "_hooks", types.SimpleNamespace(log=lambda m: None))  # no bot: cut() logs
    t = PipVoiceTrack()
    assert (t._next_samples() == 0).all()                            # silence
    t.say(np.full(OUT_SAMPLES * 2, 1000, dtype=np.int16))
    assert t.mix(np.full(OUT_SAMPLES + 10, 500, dtype=np.int16)) == pytest.approx((OUT_SAMPLES + 10) / 48000)
    t.mix(np.full(5, 32000, dtype=np.int16))                         # two sounds at once: summed, clipped
    a = t._next_samples()
    assert a[0] == 32767 and a[5] == 1500 and a[-1] == 1500
    assert t.cut() > 0                                               # barge-in: the words go
    b = t._next_samples()
    assert (b[:10] == 500).all() and (b[10:] == 0).all()             # the sound's tail still plays
    assert (t._next_samples() == 0).all() and not t.speaking()
    t.mix(np.ones(48000 * 5, dtype=np.int16))
    assert len(t.overlay) == int(t.OVERLAY_MAX_S * 48000)            # a broken writer is bounded


def test_the_probe_says_this_bot_mixes_sounds():
    first, _, _ = run(exchange(Manager({7: Session()}), {"probe": True}))
    assert first["ok"] and first["sounds"] is True and first["calls"] == [7]

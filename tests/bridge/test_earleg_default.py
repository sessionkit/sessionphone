"""The ear leg is fed at the DEFAULTS (DC_EARS=0), not only on the ears path.

A call is a microphone the ears can open (earleg.py). Before this, only
_listen_ears fed the taps, so on a default install a tap on call-ears.sock got
the call's end and no audio: nothing could hear the caller talk over the bot.
The default _listen now hands every frame to the taps beside the bot's own VAD,
and keeps wav_samples, so ear-taps.jsonl says where each tap starts.

A fake track plays his 48 kHz frames; a "gap" step raises TimeoutError at
once, as wait_for does when no packet came (Opus DTX). The bot's VAD (_feed)
is recorded, not run: these tests are about the ear leg, and they check that
the VAD still gets exactly what it got before.
"""
import asyncio
import json
import pathlib
import struct
import tempfile
import types

import numpy as np
import pytest
from av import AudioFrame

from sessionphone.bridge import calls, earleg

BLOCK = calls.IN_SAMPLES                       # 320 samples, 20 ms at 16 kHz
WAIT = 0.7                                     # the default listen loop's recv timeout
GAP_TAP = int(WAIT / calls.PTIME) * BLOCK      # a gap's silence to a tap: the time that passed
GAP_VAD = int(calls.TURN_JOIN_OPEN / calls.PTIME) * BLOCK   # ... and to the VAD, as before
VOICE = 1000


def frame48(value=VOICE):
    f = AudioFrame.from_ndarray(np.full((1, 960), value, dtype=np.int16), format="s16", layout="mono")
    f.sample_rate = 48000
    return f


class Track:
    """Steps: "talk" (one 20 ms frame), "gap" (a recv timeout) or an asyncio.Event
    to wait on. Then the call ends."""

    def __init__(self, steps):
        self.steps = list(steps)

    async def recv(self):
        while self.steps and isinstance(self.steps[0], asyncio.Event):
            await self.steps.pop(0).wait()
        if not self.steps:
            raise ConnectionError("call ended")
        step = self.steps.pop(0)
        if step == "gap":
            raise TimeoutError                  # what wait_for raises at its timeout
        return frame48()


class Tap:
    def __init__(self):
        self.chunks = []

    def put(self, pcm):
        assert isinstance(pcm, bytes)           # the socket writes it as it is
        self.chunks.append(pcm)

    def end(self, why):
        pass

    @property
    def n(self):
        return sum(len(c) for c in self.chunks) // 2


def session(monkeypatch, tmp_path, taps=()):
    monkeypatch.setattr(calls, "EARS", False)   # the default: no ears in process
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, lambda p: "", lambda *a, **k: None))
    monkeypatch.setattr(calls, "_recorder", None)
    s = calls.CallSession.__new__(calls.CallSession)
    s.dir, s.ear_taps = tmp_path / "call", list(taps)
    s.rec_state = types.SimpleNamespace(keep=False)     # recording off, read once
    s.pc = object()                                     # live, for earleg.live()
    s.vad_fed = []
    s._feed = lambda vad, pcm: s.vad_fed.append(pcm)
    return s


def test_a_tap_hears_him_at_the_defaults(monkeypatch, tmp_path):
    """Inverts the RCA's probe: at DC_EARS=0 an attached tap got 0 frames."""
    tap = Tap()
    s = session(monkeypatch, tmp_path, [tap])
    asyncio.run(s._listen(Track(["talk"] * 10)))
    assert tap.n > 0, "the ear leg got nothing at the defaults"
    assert any(np.frombuffer(c, dtype="<i2").any() for c in tap.chunks)    # his voice, not only silence
    # the tap gets what the VAD gets, frame for frame, while he talks
    assert b"".join(tap.chunks) == b"".join(p.tobytes() for p in s.vad_fed)
    assert s.wav_samples == tap.n


def test_dtx_silence_moves_the_tap_clock(monkeypatch, tmp_path):
    """No packets for a while: the tap's stream still moves, by the time that passed."""
    tap = Tap()
    s = session(monkeypatch, tmp_path, [tap])
    asyncio.run(s._listen(Track(["gap", "gap", "gap"])))
    assert tap.n == 3 * GAP_TAP and s.wav_samples == 3 * GAP_TAP
    assert not any(np.frombuffer(c, dtype="<i2").any() for c in tap.chunks)  # exact silence
    # the VAD still gets its 2.2 s of quiet per timeout, so an utterance closes as before
    assert sum(len(p) for p in s.vad_fed) == 3 * GAP_VAD


def test_talk_then_dtx_keeps_the_count(monkeypatch, tmp_path):
    tap = Tap()
    s = session(monkeypatch, tmp_path, [tap])
    asyncio.run(s._listen(Track(["talk"] * 5 + ["gap"] + ["talk"] * 5)))
    assert s.wav_samples == tap.n
    assert tap.n >= GAP_TAP + 9 * BLOCK          # the resampler may hold back part of a frame


def test_no_tap_costs_nothing_and_still_counts(monkeypatch, tmp_path):
    s = session(monkeypatch, tmp_path)
    asyncio.run(s._listen(Track(["talk"] * 3 + ["gap"])))
    assert s.wav_samples >= GAP_TAP + 2 * BLOCK


async def read_frames(r):
    out = []
    while True:
        head = await r.readexactly(5)
        kind, n = head[:1], struct.unpack(">I", head[1:])[0]
        out.append((kind, await r.readexactly(n) if n else b""))
        if kind == b"E":
            return out


def test_ear_taps_jsonl_says_where_a_tap_starts_and_what_it_got(monkeypatch, tmp_path):
    """A real socket on the default path: a tap joins after 0.7 s of gap and
    10 frames; its start row's wav_s is the audio written by then, and its
    end row's sent_s is the audio it got."""
    s = session(monkeypatch, tmp_path)
    s.dir.mkdir(parents=True)
    mgr = types.SimpleNamespace(sessions={1139: s})
    path = pathlib.Path(tempfile.mkdtemp(prefix="eld-")) / "s.sock"

    async def go():
        joined = asyncio.Event()
        listen = asyncio.ensure_future(s._listen(Track(["gap"] + ["talk"] * 10 + [joined] + ["talk"] * 10 + ["gap"])))
        while s.vad_fed == [] or len(s.vad_fed) < int(calls.TURN_JOIN_OPEN / calls.PTIME) + 9:
            await asyncio.sleep(0.01)            # phase one is in
        before = s.wav_samples
        server = await earleg.serve(mgr, path, lambda m: None)
        r, w = await asyncio.open_unix_connection(str(path))
        w.write(b'{"call": 1139}\n')
        first = json.loads(await r.readline())
        joined.set()
        await listen
        after = s.wav_samples
        earleg.end(s, "call-ended")
        frames = await asyncio.wait_for(read_frames(r), 5)
        w.close()
        for _ in range(100):                     # the server writes the end row as it closes
            rows = (s.dir / "ear-taps.jsonl").read_text().splitlines()
            if len(rows) == 2:
                break
            await asyncio.sleep(0.01)
        server.close()
        return before, after, first, frames

    before, after, first, frames = asyncio.run(go())
    rows = [json.loads(x) for x in (s.dir / "ear-taps.jsonl").read_text().splitlines()]
    assert before >= GAP_TAP + 9 * BLOCK
    assert first["ok"] and first["wav_s"] == round(before / calls.IN_RATE, 3) == rows[0]["wav_s"]
    got = sum(len(p) for k, p in frames if k == b"A") // 2
    assert got == after - before > 0
    assert rows[1]["end"] == "call-ended" and rows[1]["dropped"] == 0
    assert rows[1]["sent_s"] == round(got / calls.IN_RATE, 3)
    assert rows[0]["wav_s"] + rows[1]["sent_s"] == pytest.approx(after / calls.IN_RATE, abs=0.002)

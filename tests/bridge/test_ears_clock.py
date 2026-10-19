"""The ears leg keeps caller-call.wav on media time (PHONE-167).

His frames carry pts: the RTP timestamp, so where each frame belongs. A gap
in pts (Opus DTX, or packets the jitter buffer threw away) is media time that
passed, and the wav, ears and the ear leg must all get that much silence, or
every gap puts the clock further behind wall time. A gap in arrival only (a
network stall, then a burst) is not: its frames are contiguous and no silence
goes between them. A 0.7 s timeout fills the true time since his last frame,
and the next frame reconciles what it wrote.

A fake track plays a schedule of 48 kHz frames, each with a pts and an arrival
time on a fake clock. A recv() whose next frame is more than 0.7 s away moves
the clock 0.7 s and raises TimeoutError, as wait_for would: no real sleeps.
These tests use only _listen_ears and its constants, so they also run (red)
against the old fixed-block fill.
"""
import asyncio
import fractions
import inspect
import json
import sys
import types
import wave

import numpy as np
import pytest
from av import AudioFrame

from sessionphone.bridge import calls

RATE = calls.IN_RATE                 # 16 kHz: what the wav and ears get
BLOCK = calls.IN_SAMPLES             # 320 samples, 20 ms
WAIT = 0.7                           # the listen loop's recv timeout
LEAD = int(0.5 * RATE)               # media may run 0.5 s ahead of the wall, no more
VOICE = 1000                         # his frames are this loud; silence is exact zeros


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def time(self):
        return self.t


class Call:
    """A schedule: (arrival s after listen start, pts at 48 kHz or None)."""

    def __init__(self, first=0.05, pts=0, with_pts=True):
        self.wall, self.pts, self.frames, self.with_pts = first, pts, [], with_pts

    def talk(self, ms):
        for _ in range(ms // 20):
            self.frames.append((self.wall, self.pts if self.with_pts else None))
            self.pts += 960
            self.wall += 0.020
        return self

    def dtx(self, ms):
        """He stops sending: media time and wall time both pass, no frames."""
        self.pts += 48 * ms
        self.wall += ms / 1000
        return self

    def stall(self, ms):
        """The network holds ms of his audio, then lets it all through at once."""
        burst = self.wall + ms / 1000
        for _ in range(ms // 20):
            self.frames.append((burst, self.pts if self.with_pts else None))
            self.pts += 960
        self.wall = burst
        return self

    def jump(self, samples):
        """pts moves with no media time behind it (a false wrap, a reset)."""
        self.pts += samples
        return self


class Track:
    def __init__(self, clock, call, starve=None):
        self.clock, self.t0 = clock, clock.t
        self.frames = list(call.frames)
        self.starve = starve                  # {n: seconds}: timeout n fires that late
        self.timeouts = 0

    async def recv(self):
        if not self.frames:
            raise ConnectionError("call ended")
        at, pts = self.frames[0]
        due = self.t0 + at
        if due > self.clock.t + WAIT:
            self.timeouts += 1
            self.clock.t += (self.starve or {}).get(self.timeouts, WAIT)
            raise TimeoutError                    # what wait_for raises at its timeout
        self.clock.t = max(self.clock.t, due)
        self.frames.pop(0)
        f = AudioFrame.from_ndarray(np.full((1, 960), VOICE, dtype=np.int16),
                                    format="s16", layout="mono")
        f.sample_rate = 48000
        if pts is not None:
            f.pts, f.time_base = pts, fractions.Fraction(1, 48000)
        return f


class Pipeline:
    """ears' Pipeline; the last one built keeps what it was fed in Pipeline.last."""
    last = None

    def __init__(self, *a, **k):
        self.b, self.edges = types.SimpleNamespace(name="fake"), None
        self.fed = []
        Pipeline.last = self

    def feed(self, pcm):
        self.fed.append(pcm)

    def close(self):
        pass


class Tap:
    def __init__(self):
        self.n = 0

    def put(self, pcm):
        self.n += len(pcm) // 2

    def end(self, why):
        pass


def run(call, tmp_path, monkeypatch, starve=None):
    logs = []
    monkeypatch.setenv("EARS_RECORD", "1")     # the wav and its clock are kept only with recording on
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logs.append, lambda p: "", lambda *a, **k: None))
    monkeypatch.setattr(calls, "_recorder", None)
    monkeypatch.setitem(sys.modules, "ears", types.ModuleType("ears"))
    monkeypatch.setitem(sys.modules, "ears.cli", types.SimpleNamespace(Pipeline=Pipeline))
    clock = Clock()
    monkeypatch.setattr(calls, "time", types.SimpleNamespace(time=clock.time))
    s = calls.CallSession.__new__(calls.CallSession)
    s.dir, s.ear_taps = tmp_path / "call", [Tap()]
    track = Track(clock, call, starve)
    asyncio.run(s._listen_ears(track))
    with wave.open(str(s.dir / calls.CALLER_WAV)) as w:
        wav = w.getnframes()
    meta = json.loads((s.dir / calls.CALLER_JSON).read_text())
    fed = Pipeline.last.fed
    real_at = [i for i, c in enumerate(fed) if any(c)]
    first = real_at[0] if real_at else len(fed)
    r = types.SimpleNamespace(
        wav=wav, ears=sum(len(c) for c in fed) // 2, earleg=s.ear_taps[0].n,
        wav_samples=s.wav_samples, meta=meta, logs=[m for m in logs if "ears clock" in m],
        preroll=sum(len(c) for c in fed[:first]) // 2,
        silence=sum(len(c) for c in fed[first:] if not any(c)) // 2,
        wall=round((clock.t - meta["start_epoch"]) * RATE),
        timeouts=track.timeouts)
    # one writer: the wav, ears and the ear leg are never out of step
    assert r.wav == r.ears == r.earleg == r.wav_samples, r
    return r


def media(call, first_pts=0):
    """Samples of media time from his first frame to the end of his last."""
    return (call.pts - first_pts) // 3


# ---- gaps in pts are media time, filled to within one block


@pytest.mark.parametrize("gap_ms", [100, 400, 690, 710, 3000])
def test_a_dtx_gap_is_filled_to_media_time(gap_ms, tmp_path, monkeypatch):
    call = Call().talk(400).dtx(gap_ms).talk(400)
    r = run(call, tmp_path, monkeypatch)
    assert abs(r.wav - (r.preroll + media(call))) <= BLOCK, (r.wav, r.preroll + media(call))


def test_many_short_gaps_do_not_fall_behind(tmp_path, monkeypatch):
    """The shape of the 14 s -> 93 s HEARD lag: gaps under 0.7 s, over and over."""
    call = Call()
    for _ in range(30):
        call.talk(200).dtx(400)
    call.talk(200)
    r = run(call, tmp_path, monkeypatch)
    assert abs(r.wav - (r.preroll + media(call))) <= BLOCK
    assert r.wav <= r.wall + LEAD


def test_a_lost_packet_gap_is_filled(tmp_path, monkeypatch):
    """The jitter buffer threw away a hole and 4 real frames: pts steps +120 ms."""
    call = Call().talk(400).jump(960 * 5).talk(400)
    r = run(call, tmp_path, monkeypatch)
    assert abs(r.wav - (r.preroll + media(call))) <= BLOCK


def test_a_burst_after_a_short_stall_gets_no_silence(tmp_path, monkeypatch):
    call = Call().talk(400).stall(300).talk(400)
    r = run(call, tmp_path, monkeypatch)
    assert r.silence == 0
    assert abs(r.wav - (r.preroll + media(call))) <= BLOCK


def test_c1_a_3s_dtx_gap_keeps_the_live_silence_and_fills_the_rest(tmp_path, monkeypatch):
    """Timeouts put about 2.8 s in live (the VAD needs it); the frame after the
    gap owes only the ~0.2 s left. C1: the reset test is on what is OWED, so
    that 0.2 s is filled, not dropped as a reset."""
    call = Call().talk(400).dtx(3000).talk(400)
    r = run(call, tmp_path, monkeypatch)
    assert r.timeouts == 4
    assert abs(r.silence - 3 * RATE) <= BLOCK
    assert abs(r.wav - (r.preroll + media(call))) <= BLOCK
    assert not any("re-anchor" in m for m in r.logs)


def test_a_long_stall_is_paid_back_by_later_gaps(tmp_path, monkeypatch):
    """1.5 s stall: two timeouts put 1.4 s of provisional silence in live, then
    the stalled audio bursts in with contiguous pts. That 1.4 s is debt, and the
    later DTX gaps pay it (each pays its own length) until the clock is back on
    media time."""
    call = Call().talk(1000).stall(1500).talk(200)
    for _ in range(4):
        call.dtx(400).talk(200)
    r = run(call, tmp_path, monkeypatch)
    assert r.timeouts == 2
    assert r.silence >= int(1.4 * RATE)                   # it went in live
    assert abs(r.wav - (r.preroll + media(call))) <= BLOCK


def test_the_debt_is_owed_until_paid(tmp_path, monkeypatch):
    """One 400 ms gap pays 400 ms of a 1.4 s debt: nothing is written for it."""
    call = Call().talk(1000).stall(1500).talk(200).dtx(400).talk(200)
    r = run(call, tmp_path, monkeypatch)
    assert r.silence == int(1.4 * RATE)
    assert abs(r.wav - (r.preroll + media(call) + 1.0 * RATE)) <= BLOCK


# ---- bogus pts: never filled, never ahead of the wall


def test_a_false_wrap_is_clamped(tmp_path, monkeypatch):
    """A sender reset reads as a wrap to aiortc: pts jumps +2^32 (24.8 h)."""
    call = Call().talk(400).jump(2 ** 32).talk(400)
    r = run(call, tmp_path, monkeypatch)
    assert r.silence == 0
    assert r.wav <= r.wall + LEAD
    assert abs(r.wav - (r.preroll + media(call) - 2 ** 32 // 3)) <= BLOCK
    assert len(r.logs) == 1 and "re-anchor" in r.logs[0], r.logs


def test_a_backwards_reset_re_anchors(tmp_path, monkeypatch):
    call = Call().talk(400)
    back = call.pts
    call.jump(-back).talk(400)
    r = run(call, tmp_path, monkeypatch)
    assert r.silence == 0
    assert abs(r.wav - (r.preroll + 2 * 400 * RATE // 1000)) <= BLOCK
    assert len(r.logs) == 1 and "re-anchor" in r.logs[0], r.logs


def test_bogus_forward_jumps_never_run_ahead_of_the_wall(tmp_path, monkeypatch):
    """1.5 s jumps (under the 2 s fill cap) with no time behind them: the wall
    budget holds the wav within 0.5 s of the wall, and says so once."""
    call = Call()
    for _ in range(10):
        call.talk(200).jump(48 * 1500)
    call.talk(200)
    r = run(call, tmp_path, monkeypatch)
    assert r.wav <= r.wall + LEAD + BLOCK         # + his last frame, which lands after the wall read
    assert r.silence < 1.5 * RATE                 # ten jumps, at most 0.5 s of them filled
    assert sum("wall" in m for m in r.logs) == 1, r.logs


def test_a_starved_loop_catches_up_by_the_cap(tmp_path, monkeypatch):
    """A timeout that fires 5 s late writes at most 2 s; the next ones catch up."""
    call = Call().talk(400).dtx(9000).talk(400)
    r = run(call, tmp_path, monkeypatch, starve={1: 5.0})
    assert abs(r.wav - (r.preroll + media(call))) <= BLOCK
    assert sum("cap" in m for m in r.logs) == 1, r.logs


# ---- no pts, the pre-roll and the old fixed block


def test_a_track_without_pts_runs_on_true_elapsed_timeouts(tmp_path, monkeypatch):
    call = Call(with_pts=False).talk(400).dtx(1500).talk(400)
    r = run(call, tmp_path, monkeypatch)
    assert r.timeouts == 2
    assert r.silence == int(1.4 * RATE)               # two timeouts, 0.7 s each, true elapsed
    assert len(r.logs) == 1 and "pts" in r.logs[0], r.logs


def test_the_pre_roll_is_true_elapsed(tmp_path, monkeypatch):
    call = Call(first=2.0).talk(200)
    r = run(call, tmp_path, monkeypatch)
    assert r.preroll == int(1.4 * RATE)
    assert r.meta["preroll_s"] == 1.4


def test_the_fixed_block_count_is_gone():
    """The old fill wrote int(0.7 / PTIME) blocks, whatever the time was."""
    src = inspect.getsource(calls.CallSession._listen_ears)
    assert "/ PTIME" not in src and "/PTIME" not in src

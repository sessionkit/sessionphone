"""The first call's wait (bridge/firstcall.py, CallSession._first_call_wait).

While the speech models are still downloading, a call hears a line saying
so, then the music file under it, until the call can hear. Fakes only: the
weights reader (hearing.WEIGHTS_RUN, over a fake `ears` on PATH), the probe
(hearing.RUN), `say` (calls.apple_pcm48) and the music decoder
(firstcall.DECODE). No download, ears, say, ffmpeg, STT server or audio device:
the track is driven frame by frame through PipVoiceTrack._next_samples().
"""
import asyncio
import json
import pathlib
import subprocess
import types

import numpy as np
import pytest
from test_recording_notice import (
    call,  # noqa: F401  (a fixture: a call ready to connect)
)
from test_recording_switch import rig  # noqa: F401  (the fixture call needs)

from sessionphone import hearing
from sessionphone.bridge import calls, dc_env, firstcall

ROOT = pathlib.Path(__file__).resolve().parents[1]
N = calls.OUT_SAMPLES                  # one 20 ms frame
LINE_N = 3 * N                         # every rendered line is three frames long
MUSIC = 9000                           # the fake decoder's mark, before the gain
BED = int(firstcall.gain(np.array([MUSIC]))[0])   # ... and after it
SILENCE = 0

STT, TTS = 2_508_940_517, 355_322_783   # 2.9 GB still to fetch, together


def weights_doc(state="downloading", done=1_200_000_000, total=2_900_000_000, stt=STT, tts=TTS,
                rows=None):
    rows = rows or [
        {"repo": "x/stt", "use": "stt", "state": state, "bytes_done": 0, "bytes_total": stt},
        {"repo": "x/tts", "use": "tts", "state": state, "bytes_done": 0, "bytes_total": tts},
        {"repo": "x/voices", "use": "voices", "state": "missing", "bytes_done": 0, "bytes_total": 1},
    ]
    return json.dumps({"schema": "ears.weights/1", "state": state, "bytes_done": done,
                       "bytes_total": total, "weights": rows})


@pytest.fixture
def w(tmp_path, monkeypatch):
    """A session with only a voice track, every fake scripted through ns.

    ns.reads: the weights reader's answers, in order (the last repeats).
    ns.hearing: the probe answers yes once this is True.
    ns.rendered / ns.decoded / ns.probes: what was asked of say, ffmpeg, the STT."""
    ns = types.SimpleNamespace(logs=[], rendered=[], decoded=[], probes=0, hearing=False,
                               reads=[weights_doc()], now=None)
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(ns.logs.append, lambda p: "", lambda *a, **k: None))
    monkeypatch.setattr(firstcall, "POLL_S", 0.005)
    monkeypatch.setattr(firstcall, "REPEAT_S", 1000.0)
    monkeypatch.setattr(firstcall, "_cache", {})
    monkeypatch.setattr(firstcall, "_said", set())
    monkeypatch.setattr(calls, "FIRST_CALL_WAIT", True)

    bindir = tmp_path / "bin"                    # weights_state looks for `ears` on PATH
    bindir.mkdir()
    (bindir / "ears").write_text("#!/bin/sh\nexit 2\n")
    (bindir / "ears").chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))

    def reader(argv, **kw):
        out = ns.reads.pop(0) if len(ns.reads) > 1 else ns.reads[0]
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(hearing, "WEIGHTS_RUN", reader)

    def stt(argv, **kw):
        ns.probes += 1
        if ns.hearing:
            return subprocess.CompletedProcess(argv, 0, '{"text": ""}\n200', "")
        return subprocess.CompletedProcess(argv, 7, "", "")      # connection refused: NO
    monkeypatch.setattr(hearing, "RUN", stt)

    say = tmp_path / "say"                       # SAY_BIN exists: this is a Mac
    say.write_text("")
    monkeypatch.setattr(calls, "SAY_BIN", str(say))

    def render(text, voice=None):
        ns.rendered.append(text)
        return np.full(LINE_N, 1000 + len(ns.rendered), dtype=np.int16)   # line k plays 1000+k
    monkeypatch.setattr(calls, "apple_pcm48", render)

    music = tmp_path / "wait-music.opus"
    music.write_bytes(b"not really opus")
    monkeypatch.setattr(calls, "WAIT_MUSIC", music)
    ns.music = music

    def decode(path, ffmpeg="ffmpeg"):
        ns.decoded.append(path)
        return np.full(5 * N, MUSIC, dtype=np.int16)
    monkeypatch.setattr(firstcall, "DECODE", decode)

    def clock():
        return ns.now if ns.now is not None else _real()
    _real = firstcall.CLOCK
    monkeypatch.setattr(firstcall, "CLOCK", clock)

    s = object.__new__(calls.CallSession)       # pc None: close() ends the wait and returns
    s.voice, s.chat_id = calls.PipVoiceTrack(), 12
    s.manager = types.SimpleNamespace(sessions={})
    ns.s = s
    ns.waits = lambda: [m for m in ns.logs if "first-call wait" in m]
    return ns


def frames(s, n):
    return [s.voice._next_samples() for _ in range(n)]


def marks(fs):
    """Each frame's one value (the fakes are flat), or 'mixed'."""
    return [int(f[0]) if (f == f[0]).all() else "mixed" for f in fs]


async def until(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.002)


def drive(ns, body):
    """Start the wait as _on_connected does, run body(task), and return the task."""
    async def go():
        task = ns.s.wait_task = asyncio.ensure_future(ns.s._first_call_wait())   # as _on_connected
        await body(task)
        await asyncio.wait_for(asyncio.shield(task), 5) if not task.done() else None
        return task
    return asyncio.run(go())


# ---- 1. no wait: today's behaviour --------------------------------------------------------

@pytest.mark.parametrize("why", ["cached", "no reader"])
def test_cached_or_no_reader_is_no_wait(w, why, monkeypatch):
    if why == "cached":
        w.reads = [weights_doc("cached", done=STT + TTS, total=STT + TTS)]
    else:
        monkeypatch.setenv("PATH", "/nonexistent")       # no ears: weights_state() is None

    async def body(task):
        await asyncio.wait_for(task, 5)
    drive(w, body)
    assert w.s.voice.said_n == 0 and w.s.voice.bed is None
    assert w.rendered == [] and w.decoded == [] and w.probes == 0
    assert marks(frames(w.s, 3)) == [SILENCE] * 3
    assert w.waits() == []


@pytest.mark.parametrize("on", [False, True])
def test_off_starts_no_wait(call, monkeypatch, on):  # noqa: F811
    """DC_FIRST_CALL_WAIT=off: _on_connected never starts the wait; on, it does,
    beside the greeting, which is spoken all the same."""
    started = []

    async def fake(self):
        started.append(self)
        assert self.wait_task is not None         # it is the task close() cancels
    monkeypatch.setattr(calls.CallSession, "_first_call_wait", fake)
    monkeypatch.setattr(calls, "FIRST_CALL_WAIT", on)
    s = call.connect()
    assert started == ([s] if on else [])
    assert call.spoken == call.greeting


@pytest.mark.parametrize("value, on", [(None, True), ("on", True), ("off", False), ("OFF", False),
                                       ("", True)])
def test_the_setting(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("DC_FIRST_CALL_WAIT", raising=False)
    else:
        monkeypatch.setenv("DC_FIRST_CALL_WAIT", value)
    assert ((dc_env.env("FIRST_CALL_WAIT") or "on").strip().lower() != "off") is on
    assert dc_env.FIRST_CALL_WAIT is True             # conftest dropped every DC_*: the default


def test_the_music_defaults_to_the_root():
    assert dc_env.WAIT_MUSIC == dc_env.ROOT / "wait-music.opus"
    src = (ROOT / "bin/dc-env").read_text()           # bin/dc-env mirrors dc_env.py
    assert 'DC_FIRST_CALL_WAIT="${DC_FIRST_CALL_WAIT:-on}"' in src
    assert 'DC_WAIT_MUSIC="${DC_WAIT_MUSIC:-$DC_ROOT/wait-music.opus}"' in src


# ---- 2. the line first, then the music ----------------------------------------------------

def test_the_line_is_the_first_audio_then_the_music(w):
    async def body(task):
        await until(lambda: w.s.voice.bed is not None)
        assert marks(frames(w.s, 3)) == [1001] * 3      # the line, all of it
        assert marks(frames(w.s, 7)) == [BED] * 7       # then the music, looped
        w.hearing = True
    drive(w, body)
    assert w.rendered == [("Please wait for the voice model to download, about 2.9 GB. "
                           "This only happens once.")]
    assert w.decoded == [str(w.music)]
    assert BED == int(MUSIC * 10 ** (-8 / 20))          # -8 dB under the line


# ---- 3. the probe answers yes mid-song ----------------------------------------------------

def test_hearing_answers_ends_the_music_for_good(w):
    async def body(task):
        await until(lambda: w.s.voice.bed is not None)
        frames(w.s, 3)
        assert marks(frames(w.s, 4)) == [BED] * 4
        w.hearing = True
        await asyncio.wait_for(task, 5)
        assert marks(frames(w.s, 1)) == [SILENCE]        # the next frame
        await asyncio.sleep(0.03)
        assert marks(frames(w.s, 20)) == [SILENCE] * 20  # never returns
    drive(w, body)
    assert w.s.voice.bed is None
    assert any("ended (hearing answers)" in m for m in w.waits())


# ---- 4. a real line queued mid-song -------------------------------------------------------

def test_a_real_line_ends_the_music_for_good(w):
    async def body(task):
        await until(lambda: w.s.voice.bed is not None)
        frames(w.s, 5)
        w.s.voice.say(np.full(2 * N, 7777, dtype=np.int16))   # the greeting's first sentence
        assert marks(frames(w.s, 3)) == [7777, 7777, SILENCE]  # no music after it
        await asyncio.wait_for(task, 5)
        w.s.voice.say(np.full(N, 7778, dtype=np.int16))       # the next sentence
        assert marks(frames(w.s, 4)) == [7778, SILENCE, SILENCE, SILENCE]
    drive(w, body)
    assert any("ended (a line)" in m for m in w.waits())
    assert w.probes <= 2          # the wait asked no more once it ended


def test_a_line_queued_while_the_wait_line_renders_means_no_wait(w, monkeypatch):
    def render(text, voice=None):
        w.rendered.append(text)
        w.s.voice.say(np.full(N, 7777, dtype=np.int16))  # the greeting won the race
        return np.full(LINE_N, 1001, dtype=np.int16)
    monkeypatch.setattr(calls, "apple_pcm48", render)

    async def body(task):
        await asyncio.wait_for(task, 5)
    drive(w, body)
    assert any("already talking; no wait" in m for m in w.waits())
    assert w.s.voice.bed is None and w.s.voice.said_n == 1
    assert marks(frames(w.s, 2)) == [7777, SILENCE]


# ---- 5. no music file: the line repeats with progress -------------------------------------

def test_no_music_repeats_the_line_with_progress_until_hearing(w, monkeypatch):
    monkeypatch.setattr(firstcall, "REPEAT_S", 0.02)
    w.music.unlink()
    w.reads = [weights_doc(done=1_200_000_000), weights_doc(done=2_000_000_000)]

    async def body(task):
        await until(lambda: len(w.rendered) >= 3)
        w.hearing = True
        await asyncio.wait_for(task, 5)
        n = len(w.rendered)
        await asyncio.sleep(0.1)
        assert len(w.rendered) == n                      # no line after the stop
    drive(w, body)
    assert w.rendered[0].startswith("Please wait for the voice model to download, about 2.9 GB.")
    assert w.rendered[1] == "Still downloading: 2.0 GB of 2.9 GB."
    assert all(t.startswith("Still downloading: ") for t in w.rendered[1:])
    assert w.s.voice.bed is None and w.decoded == []
    assert sum("no music file at" in m for m in w.waits()) == 1
    assert any("ended (hearing answers)" in m for m in w.waits())
    queued = marks(frames(w.s, 3 * len(w.rendered) + 2))
    assert BED not in queued and queued[:3] == [1001] * 3


def test_no_music_is_logged_once_per_bot(w):
    for _ in range(3):
        assert firstcall.music(w.music.parent / "gone.opus", log=w.logs.append) is None
    assert sum("no music file at" in m for m in w.logs) == 1


# ---- 6. no size, no total -----------------------------------------------------------------

def test_no_size_and_no_total():
    nosize = hearing.Weights("downloading", 412_000_000, None, None)
    assert firstcall.wait_line(nosize) == ("Please wait for the voice model to download. "
                                           "This only happens once.")
    assert firstcall.progress_line(nosize) == "Still downloading, 412 MB so far."
    sized = hearing.Weights("downloading", 1_200_000_000, 2_900_000_000, 2_864_263_300)
    assert firstcall.wait_line(sized) == ("Please wait for the voice model to download, about 2.9 GB. "
                                          "This only happens once.")
    assert firstcall.progress_line(sized) == "Still downloading: 1.2 GB of 2.9 GB."
    assert firstcall.progress_line(hearing.Weights("missing", 0, 2_900_000_000, 2_864_263_300)) == (
        "Still waiting for the voice model to download.")


def test_no_size_on_a_live_call(w):
    w.reads = [weights_doc(total=None, stt=None, tts=None)]

    async def body(task):
        await until(lambda: w.s.voice.bed is not None)
        w.hearing = True
    drive(w, body)
    assert w.rendered == ["Please wait for the voice model to download. This only happens once."]


# ---- 7. the call ends mid-wait ------------------------------------------------------------

def test_the_call_ending_cancels_the_wait_cleanly(w):
    async def body(task):
        await until(lambda: w.s.voice.bed is not None and w.probes >= 1)
        await w.s.close("hung up")            # the call ends: close() cancels the wait
        with pytest.raises(asyncio.CancelledError):
            await task
        n = w.probes
        await asyncio.sleep(0.05)
        assert w.probes == n                  # no probe after
    task = drive(w, body)
    assert task.cancelled()
    assert w.s.voice.bed is None
    assert not any("failed" in m for m in w.logs)
    assert any("ended (the call ended)" in m for m in w.waits())


# ---- 8. no say ----------------------------------------------------------------------------

def test_no_say_plays_the_music_alone(w, monkeypatch):
    monkeypatch.setattr(calls, "SAY_BIN", "/nonexistent/say")

    async def body(task):
        await until(lambda: w.s.voice.bed is not None)
        assert marks(frames(w.s, 3)) == [BED] * 3
        w.hearing = True
    drive(w, body)
    assert w.rendered == [] and w.s.voice.said_n == 0


def test_no_say_and_no_music_is_nothing(w, monkeypatch):
    monkeypatch.setattr(calls, "SAY_BIN", "/nonexistent/say")
    w.music.unlink()

    async def body(task):
        await asyncio.wait_for(task, 5)
    drive(w, body)
    assert w.rendered == [] and w.s.voice.said_n == 0 and w.s.voice.bed is None
    assert w.probes == 0
    assert marks(frames(w.s, 2)) == [SILENCE] * 2
    assert any("no line (no say) and no music" in m for m in w.waits())


# ---- 9. the cap ---------------------------------------------------------------------------

def test_the_cap_ends_the_wait(w):
    w.now = 1000.0

    async def body(task):
        await until(lambda: w.s.voice.bed is not None)
        await asyncio.sleep(0.02)
        assert not task.done()
        w.now += firstcall.CAP_S                     # 20 minutes later
        await asyncio.wait_for(task, 5)
        assert marks(frames(w.s, 3 + 1))[-1] == SILENCE
    drive(w, body)
    assert firstcall.CAP_S == 1200.0
    assert w.s.voice.bed is None
    assert any("ended (the 20 min cap) after 1200 s" in m for m in w.waits())


# ---- 10. a barge-in cut leaves the bed --------------------------------------------------------

def test_a_cut_leaves_the_music(w):
    async def body(task):
        await until(lambda: w.s.voice.bed is not None)
        frames(w.s, 4)
        assert w.s.voice.speaking() is False         # the bed is not speech: no barge-in
        w.s.voice.cut()
        assert marks(frames(w.s, 3)) == [BED] * 3
        w.hearing = True
    drive(w, body)


# ---- 11. the docs, and no audio in the package --------------------------------------------

def test_the_settings_page_names_both_keys():
    page = (ROOT / "docs/settings.md").read_text()
    for key in ("`DC_FIRST_CALL_WAIT`", "`DC_WAIT_MUSIC`"):
        assert f"| {key} |" in page


def test_no_audio_file_ships_in_the_package():
    found = [p for ext in ("opus", "flac", "m4a", "wav", "mp3", "ogg", "aac")
             for p in (ROOT / "src").rglob(f"*.{ext}")]
    assert found == []


# ---- the track's bed ------------------------------------------------------------------------

def test_the_bed_loops_with_no_gap():
    t = calls.PipVoiceTrack()
    ramp = np.arange(1500, dtype=np.int16)               # not a whole number of frames
    t.hold(ramp)
    got = np.concatenate(frames(types.SimpleNamespace(voice=t), 5))
    assert (got == np.tile(ramp, 4)[:5 * N]).all()


def test_a_line_pre_empts_the_bed_within_a_frame_and_ends_it():
    t = calls.PipVoiceTrack()
    t.hold(np.full(N, 5, dtype=np.int16))
    one = types.SimpleNamespace(voice=t)
    assert marks(frames(one, 1)) == [5]
    t.say(np.full(N, 6, dtype=np.int16))
    assert marks(frames(one, 2)) == [6, SILENCE]
    assert t.bed is None


def test_a_line_queued_before_the_hold_plays_first():
    t = calls.PipVoiceTrack()
    t.say(np.full(2 * N, 6, dtype=np.int16))
    t.hold(np.full(N, 5, dtype=np.int16))
    assert marks(frames(types.SimpleNamespace(voice=t), 4)) == [6, 6, 5, 5]


def test_unhold_stops_at_the_next_frame():
    t = calls.PipVoiceTrack()
    t.hold(np.full(N, 5, dtype=np.int16))
    assert t.unhold() is True and t.unhold() is False
    assert marks(frames(types.SimpleNamespace(voice=t), 1)) == [SILENCE]
    t.hold(np.zeros(0, dtype=np.int16))                  # an empty bed is no bed
    assert t.bed is None


def test_sounds_mix_over_the_bed():
    t = calls.PipVoiceTrack()
    t.hold(np.full(N, 5, dtype=np.int16))
    t.mix(np.full(N, 2, dtype=np.int16))
    assert marks(frames(types.SimpleNamespace(voice=t), 2)) == [7, 5]


# ---- the music file -------------------------------------------------------------------------

def test_music_is_decoded_once_and_again_when_the_file_changes(w):
    import os
    a = firstcall.music(w.music, log=w.logs.append)
    b = firstcall.music(w.music, log=w.logs.append)
    assert a is b and len(w.decoded) == 1
    st = os.stat(w.music)
    os.utime(w.music, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    firstcall.music(w.music, log=w.logs.append)
    assert len(w.decoded) == 2


def test_an_undecodable_music_file_is_the_line_alone(w, monkeypatch):
    def bad(path, ffmpeg="ffmpeg"):
        raise RuntimeError("ffmpeg rc=1")
    monkeypatch.setattr(firstcall, "DECODE", bad)

    async def body(task):
        await until(lambda: w.s.voice.said_n == 1)
        await asyncio.sleep(0.02)
        assert w.s.voice.bed is None
        w.hearing = True
    drive(w, body)
    assert any("could not be decoded (ffmpeg rc=1); the line alone" in m for m in w.waits())


def test_a_failing_wait_never_breaks_the_call(w, monkeypatch):
    async def boom(*a, **k):
        raise ValueError("bad")
    monkeypatch.setattr(firstcall, "wait", boom)

    async def body(task):
        await asyncio.wait_for(task, 5)
    task = drive(w, body)
    assert task.exception() is None
    assert any("first-call wait failed" in m for m in w.logs)

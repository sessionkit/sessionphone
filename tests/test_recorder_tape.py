"""The call tape takes the inbound audio in either form (PHONE-178).

The ears path hands each resampled frame to Recorder.write_in as bytes; the
chunker path and the outgoing track hand over an ndarray. write_in called
.tobytes() on whatever it got, which bytes does not have, and a bare except
hid the AttributeError on every frame. full-in.wav closed at 0.0s on every
ears call with recording on, and dc-call-mix mixed no caller.

These tests pin both forms, the ears path end to end, and that a write that
fails is logged once instead of passing silently.
"""
import asyncio
import sys
import types
import wave

import numpy as np
import pytest
from av import AudioFrame

from sessionphone.bridge import calls

FRAME = (np.sin(np.arange(320) / 5) * 8000).astype(np.int16)   # 20 ms at 16 kHz


@pytest.fixture
def lines(monkeypatch):
    """The CALL log lines, captured."""
    got = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(got.append, lambda p: "", lambda *a, **k: None))
    monkeypatch.setattr(calls, "_recorder", None)
    return got


def frames(path):
    with wave.open(str(path)) as w:
        return w.getnframes(), w.readframes(w.getnframes())


def test_an_ears_frame_as_bytes_reaches_full_in(tmp_path, lines):
    r = calls.Recorder(tmp_path)
    r.write_in(FRAME.tobytes())        # what _listen_ears passes
    r.close()
    n, data = frames(tmp_path / "full-in.wav")
    assert n == 320 and data == FRAME.tobytes()
    assert not any("could not write" in m for m in lines)


@pytest.mark.parametrize("form", [
    lambda a: a,                        # the chunker path
    lambda a: a.tobytes(),              # the ears path
    lambda a: bytearray(a.tobytes()),
    lambda a: memoryview(a.tobytes()),
], ids=["ndarray", "bytes", "bytearray", "memoryview"])
def test_every_form_writes_the_same_tape(tmp_path, lines, form):
    r = calls.Recorder(tmp_path)
    r.write_in(form(FRAME))
    r.write_out(form(np.tile(FRAME, 3)))
    r.close()
    assert frames(tmp_path / "full-in.wav") == (320, FRAME.tobytes())
    assert frames(tmp_path / "full-out.wav") == (960, np.tile(FRAME, 3).tobytes())


@pytest.mark.parametrize("empty", [b"", np.zeros(0, dtype=np.int16)], ids=["bytes", "ndarray"])
def test_an_empty_frame_writes_nothing_and_logs_nothing(tmp_path, lines, empty):
    r = calls.Recorder(tmp_path)
    r.write_in(empty)
    r.write_out(empty)
    r.close()
    assert frames(tmp_path / "full-in.wav")[0] == 0
    assert frames(tmp_path / "full-out.wav")[0] == 0
    assert not any("could not write" in m for m in lines)


class Broken:
    """A wave writer whose every write fails."""

    def __init__(self):
        self.calls = 0

    def writeframes(self, data):
        self.calls += 1
        raise OSError("disk full")


def test_a_failing_write_is_logged_once_per_file_and_never_raises(tmp_path, lines):
    r = calls.Recorder(tmp_path)
    r.close()                          # the real files are done with
    r._in, r._out = Broken(), Broken()
    for _ in range(5):
        r.write_in(FRAME.tobytes())
        r.write_out(FRAME)
    assert (r._in.calls, r._out.calls) == (5, 5)     # every frame is still tried
    failed = [m for m in lines if "could not write" in m]
    assert failed == [
        "CALL recorder: could not write full-in.wav: OSError: disk full",
        "CALL recorder: could not write full-out.wav: OSError: disk full",
    ]


def test_a_failing_write_with_no_log_hooks_still_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(calls, "_hooks", None)     # log() itself raises
    r = calls.Recorder.__new__(calls.Recorder)
    r._in, r._out, r._failed = Broken(), None, set()
    r.write_in(FRAME.tobytes())
    r.write_in(FRAME.tobytes())
    assert r._in.calls == 2


# --- the ears path, end to end -------------------------------------------------

class FakePipeline:
    def __init__(self, *a, **k):
        self.b, self.edges = types.SimpleNamespace(name="fake"), None

    def feed(self, pcm):
        pass

    def close(self):
        pass


class SpeechTrack:
    """Three 20 ms frames of a tone at 48 kHz, then the call ends."""

    def __init__(self):
        self.left = 3

    async def recv(self):
        if not self.left:
            raise ConnectionError("call ended")
        self.left -= 1
        tone = (np.sin(np.arange(960) / 15) * 8000).astype(np.int16).reshape(1, -1)
        f = AudioFrame.from_ndarray(tone, format="s16", layout="mono")
        f.sample_rate = 48000
        return f


def test_an_ears_call_with_recording_on_keeps_the_caller_on_full_in(tmp_path, lines, monkeypatch):
    monkeypatch.setenv("EARS_RECORD", "1")
    monkeypatch.setitem(sys.modules, "ears", types.ModuleType("ears"))
    monkeypatch.setitem(sys.modules, "ears.cli", types.SimpleNamespace(Pipeline=FakePipeline))
    s = calls.CallSession.__new__(calls.CallSession)
    s.dir = tmp_path / "call"
    asyncio.run(s._listen_ears(SpeechTrack()))
    calls._recorder.close()            # what hang-up does
    n, data = frames(s.dir / "full-in.wav")
    assert n > 0 and np.frombuffer(data, dtype=np.int16).any()
    closed = [m for m in lines if "recording_closed full-in.wav" in m]
    assert closed == [f"CALL recording_closed full-in.wav {n / calls.IN_RATE:.1f}s"]
    assert not any("could not write" in m for m in lines)

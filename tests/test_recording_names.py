"""Call recordings are named for the role, not a person.

The bridge writes three kinds of file for the far party of a call into the
call dir: one wav per chunk (the non-ears path), the whole call as one wav
and its clock as json (the ears path). Each is written as caller-*, never
as a person's name, and nothing in src/ or bin/ names a recording file for
anyone but the caller.

The last guard tests a SHAPE, not a list of names: a recording file name is
<who>-call.wav, <who>-call.json or <who>-<chunk>.wav, and <who> is `caller`.
So it catches every person's name, including one no list holds, it runs on
every run with nothing to set, and it ships no name.

Nothing in this repo reads them back by name: dc-call-mix reads
full-in.wav/full-out.wav, and a chunk's path goes only to transcription and
log lines. So "read new, then old" has no reader here to change; the test
below pins that too, so a reader added later must face the old names.
"""
import asyncio
import json
import re
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest
from av import AudioFrame

from sessionphone.bridge import calls

ROOT = Path(__file__).resolve().parent.parent
# a recording file's name: <who>-call.wav|json, or a chunk <who>-<n>.wav where <n> is
# digits or a format field. <who> may itself be a format field ({who}-call.wav).
RECORDING = re.compile(r"([\w{}]+)-(?:call\.(?:wav|json)|(?:\d+|\{[^}]*\})\.wav)\b", re.IGNORECASE)
ROLE = "caller"


def not_the_role(line: str) -> list[str]:
    """Every recording file name on line whose <who> is not the role."""
    return [m[0] for m in RECORDING.finditer(line) if m[1].lower() != ROLE]


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, lambda p: "", lambda *a, **k: None))
    monkeypatch.setattr(calls, "_recorder", None)


def test_the_names_are_the_callers():
    assert calls.CALLER_CHUNK.format(n=7) == "caller-007.wav"
    assert (calls.CALLER_WAV, calls.CALLER_JSON) == ("caller-call.wav", "caller-call.json")


def test_a_chunk_is_written_as_caller_nnn(tmp_path, quiet):
    u = calls.Utterances.__new__(calls.Utterances)      # no Recorder: only _flush is under test
    u.call_dir, u.n = tmp_path, 0
    u.buf = [np.zeros(calls.IN_SAMPLES, dtype=np.int16)] * 3
    u.rms, u.speech_frames, u.quiet_frames = [0.0] * 3, 3, 0
    first, second = u._flush(2), u._flush(1)
    assert (first.name, second.name) == ("caller-001.wav", "caller-002.wav")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["caller-001.wav", "caller-002.wav"]
    with wave.open(str(first)) as w:
        assert w.getframerate() == calls.IN_RATE and w.getnframes() == 2 * calls.IN_SAMPLES


class FakePipeline:
    def __init__(self, *a, **k):
        self.b, self.edges = types.SimpleNamespace(name="fake"), None

    def feed(self, pcm):
        pass

    def close(self):
        pass


class OneFrameTrack:
    """One 20 ms frame of his audio at 48 kHz, then the call ends."""

    def __init__(self):
        self.sent = False

    async def recv(self):
        if self.sent:
            raise ConnectionError("call ended")
        self.sent = True
        f = AudioFrame.from_ndarray(np.zeros((1, 960), dtype=np.int16), format="s16", layout="mono")
        f.sample_rate = 48000
        return f


def test_the_whole_call_and_its_clock_are_caller_call(tmp_path, quiet, monkeypatch):
    monkeypatch.setenv("EARS_RECORD", "1")     # kept only with recording on
    monkeypatch.setitem(sys.modules, "ears", types.ModuleType("ears"))
    monkeypatch.setitem(sys.modules, "ears.cli", types.SimpleNamespace(Pipeline=FakePipeline))
    s = calls.CallSession.__new__(calls.CallSession)
    s.dir = tmp_path / "call"
    asyncio.run(s._listen_ears(OneFrameTrack()))
    assert sorted(p.name for p in s.dir.iterdir()) == ["caller-call.json", "caller-call.wav",
                                                      "full-in.wav", "full-out.wav", "recording.json"]
    meta = json.loads((s.dir / "caller-call.json").read_text())
    assert meta["rate"] == calls.IN_RATE and "audio_s" in meta     # the end-of-call rewrite landed too


@pytest.mark.parametrize("tree", ["src", "bin"])
def test_no_tool_or_module_names_a_persons_recording(tree):
    """No writer of an old name is left, and no reader of one was ever here.
    A reader added later must read caller-* first, then the old person-named
    file; this fails until its old-name literal is allowed below with that
    fallback."""
    allowed: set = set()     # (file, line fragment) pairs, for such a reader only
    hits = []
    for f in sorted((ROOT / tree).rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        rel = str(f.relative_to(ROOT))
        for line in f.read_text(errors="replace").splitlines():
            if not_the_role(line) and not any(rel == a and frag in line for a, frag in allowed):
                hits.append(f"{rel}: {line.strip()}")
    assert hits == []


@pytest.mark.parametrize("line", [
    'CALLER_WAV = "alice-call.wav"',
    "meta = d / 'bob-call.json'",
    "carol-007.wav",
    'f"dave-{n:03d}.wav"',
    'f"{who}-call.wav"',
    "path = call_dir / 'IVY-CALL.WAV'",
    "# the old theo-012.wav, from before the rename",
])
def test_the_shape_catches_a_recording_named_for_anyone_but_the_caller(line):
    """The positive control: placeholder names stand in for a person's."""
    assert not_the_role(line)


@pytest.mark.parametrize("line", [
    'CALLER_CHUNK = "caller-{n:03d}.wav"',
    "caller-call.wav and caller-call.json",
    "Caller-007.wav",
    "full-in.wav / full-out.wav, recording.json",
    "dc-call-mix reads the call; a mid-call turn, a web-call",
    'f"avatar-{os.getpid()}.json"',
    'f"{head}-{m}.json"',
])
def test_the_shape_passes_the_role_and_files_that_are_no_recording(line):
    assert not_the_role(line) == []


def test_the_shape_reaches_the_names_the_bridge_writes():
    """Not vacuous: the shape sees the real recording names in src/, all of them the role's."""
    names = [m[0] for f in sorted((ROOT / "src").rglob("*.py"))
             for m in RECORDING.finditer(f.read_text(errors="replace"))]
    assert {"caller-call.wav", "caller-call.json", "caller-{n:03d}.wav"} <= set(names)

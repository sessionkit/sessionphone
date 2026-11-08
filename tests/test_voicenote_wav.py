"""A voice note that isn't wav is decoded to 16 kHz mono wav before it's posted.

A Delta voice note arrives as M4A (AAC in an MP4 box). An mlx-audio server
(Parakeet) refuses it as posted, "Unsupported output format: m4a", and reads it
once it is 16 kHz wav, so the agent read "[voice note - transcription failed]".
The bridge now decodes a note that isn't wav with PyAV, posts the wav, and
deletes it. These tests pin:

  - an M4A becomes 16 kHz mono 16-bit wav, the wav is what's posted, and the
    temp wav is gone afterwards, whatever the post did;
  - a wav is posted as it came: same path, no decode, no temp file;
  - a decode failure logs its plain reason, puts it in the agent's mail, and
    posts nothing (no half-file);
  - end to end, with real curl: a Parakeet-like stub on 127.0.0.1 that refuses
    m4a and reads wav gets the wav and answers.

Every audio file is generated in the test with PyAV (no binary fixture), under
tmp_path; the temp wav dir is pointed at tmp_path too. No speech server is
reached: the stub is a stdlib server on a free loopback port.
"""
import math
import shutil
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import ClassVar

import pytest

av = pytest.importorskip("av")          # the media pack; `uv sync` installs it
np = pytest.importorskip("numpy")

from sessionphone.bridge import bot, stt

RATE_IN = 48000                          # what a phone records at; the decode makes it 16 kHz


def make_m4a(path, seconds=1.0, rate=RATE_IN):
    """An AAC-in-MP4 voice note, as a Delta client sends: a 440 Hz tone."""
    n = int(seconds * rate)
    pcm = (0.3 * np.sin(2 * math.pi * 440 * np.arange(n) / rate)).astype(np.float32)
    with av.open(str(path), "w", format="mp4") as box:
        s = box.add_stream("aac", rate=rate, layout="mono")
        for i in range(0, n, 1024):
            frame = av.AudioFrame.from_ndarray(pcm[i:i + 1024].reshape(1, -1), format="flt", layout="mono")
            frame.rate, frame.pts = rate, i
            for packet in s.encode(frame):
                box.mux(packet)
        for packet in s.encode(None):
            box.mux(packet)
    return str(path)


def make_wav(path, rate=44100, channels=2, seconds=0.25):
    """A wav that is NOT 16 kHz mono: it must still go untouched."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * channels * int(rate * seconds))
    return str(path)


def wav_info(path):
    with wave.open(path) as r:
        return r.getframerate(), r.getnchannels(), r.getsampwidth(), r.getnframes()


@pytest.fixture
def tmpwav(tmp_path, monkeypatch):
    """The decode's temp dir, pointed at tmp_path, so a leftover can be seen."""
    d = tmp_path / "tmpwav"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def leftovers(d):
    return sorted(p.name for p in d.iterdir())


@pytest.fixture
def posted(monkeypatch):
    """bot.transcribe's post, recorded: the path, and what the file held then."""
    seen = []

    def fake(path, url, model, log, run=None):
        seen.append({"path": path, "wav": stt.is_wav(path),
                     "info": wav_info(path) if stt.is_wav(path) else None})
        return "hello there"
    monkeypatch.setattr(bot.stt, "transcribe", fake)
    return seen


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(bot, "log", lines.append)
    return lines


# ---- is_wav: by content, not by name --------------------------------------

def test_is_wav_reads_the_header_not_the_name(tmp_path):
    assert stt.is_wav(make_wav(tmp_path / "a.wav"))
    assert stt.is_wav(make_wav(tmp_path / "named-wrong.m4a"))
    assert not stt.is_wav(make_m4a(tmp_path / "note.m4a"))
    assert not stt.is_wav(make_m4a(tmp_path / "named-wrong.wav"))
    (tmp_path / "short").write_bytes(b"RIFF")
    assert not stt.is_wav(tmp_path / "short")
    assert not stt.is_wav(tmp_path / "missing.wav")


# ---- an M4A is decoded, the wav posted, the temp deleted -------------------

def test_an_m4a_is_decoded_to_16k_mono_wav_and_that_is_posted(tmp_path, tmpwav, posted, logged):
    note = make_m4a(tmp_path / "voice-message.m4a", seconds=1.0)
    assert bot.transcribe(note) == "hello there"
    [p] = posted
    assert p["path"] != note and p["path"].startswith(str(tmpwav))
    assert p["wav"]
    rate, channels, width, frames = p["info"]
    assert (rate, channels, width) == (16000, 1, 2)
    assert abs(frames - 16000) < 1600            # about a second, give or take the codec's padding
    assert leftovers(tmpwav) == []               # the temp wav is deleted after the post
    assert any(l.startswith("VOICE NOTE decoded to 16 kHz mono wav (voice-message.m4a") for l in logged)


def test_the_temp_wav_is_deleted_when_the_post_fails_too(tmp_path, tmpwav, monkeypatch, logged):
    def boom(*a, **k):
        raise RuntimeError("the post blew up")
    monkeypatch.setattr(bot.stt, "transcribe", boom)
    with pytest.raises(RuntimeError):
        bot.transcribe(make_m4a(tmp_path / "n.m4a"))
    assert leftovers(tmpwav) == []


def test_an_ogg_opus_note_is_decoded_too(tmp_path, tmpwav, posted, logged):
    path = tmp_path / "voice.ogg"
    with av.open(str(path), "w", format="ogg") as box:
        s = box.add_stream("libopus", rate=48000, layout="mono")
        pcm = np.zeros((1, 960), dtype=np.int16)
        for i in range(25):                      # half a second of 20 ms frames
            frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
            frame.rate, frame.pts = 48000, i * 960
            for packet in s.encode(frame):
                box.mux(packet)
        for packet in s.encode(None):
            box.mux(packet)
    assert bot.transcribe(str(path)) == "hello there"
    assert posted[0]["info"][:3] == (16000, 1, 2)
    assert leftovers(tmpwav) == []


# ---- a wav is posted untouched ---------------------------------------------

def test_a_wav_is_posted_untouched(tmp_path, tmpwav, posted, logged, monkeypatch):
    note = make_wav(tmp_path / "note.wav")
    before = (tmp_path / "note.wav").read_bytes()

    def no_decode(*a, **k):
        raise AssertionError("a wav was decoded")
    monkeypatch.setattr(stt, "to_wav", no_decode)
    monkeypatch.setattr(tempfile, "mkstemp", no_decode)
    assert bot.transcribe(note) == "hello there"
    assert posted[0]["path"] == note and posted[0]["info"] == (44100, 2, 2, 11025)
    assert (tmp_path / "note.wav").read_bytes() == before
    assert leftovers(tmpwav) == [] and logged == []


# ---- a decode failure: the plain reason, logged and mailed; nothing posted --

def test_a_note_that_cannot_be_decoded_gives_its_plain_reason(tmp_path, tmpwav, posted, logged):
    junk = tmp_path / "voice-message.m4a"
    junk.write_bytes(b"this is not audio\n" * 64)
    with pytest.raises(stt.Undecodable) as e:
        bot.transcribe(str(junk))
    assert str(e.value) == "Invalid data found when processing input"   # no errno, no path
    assert posted == []                          # nothing posted, no half-file
    assert leftovers(tmpwav) == []
    assert logged == [("VOICE NOTE not decoded to wav (voice-message.m4a): "
                       "Invalid data found when processing input")]


def test_a_truncated_m4a_posts_nothing(tmp_path, tmpwav, posted, logged):
    whole = (tmp_path / "whole.m4a")
    make_m4a(whole)
    cut = tmp_path / "cut.m4a"
    cut.write_bytes(whole.read_bytes()[:200])    # the box header and little else
    with pytest.raises(stt.Undecodable) as e:
        bot.transcribe(str(cut))
    assert str(e.value) and "\n" not in str(e.value)
    assert posted == [] and leftovers(tmpwav) == []


def test_a_file_with_no_audio_track_says_so(tmp_path, tmpwav, posted, logged):
    """A video-only file: the reason is the missing track, not the wav
    writer's own complaint about a header it never got."""
    path = tmp_path / "clip.mp4"
    with av.open(str(path), "w", format="mp4") as box:
        s = box.add_stream("mpeg4", rate=10)
        s.width, s.height, s.pix_fmt = 32, 32, "yuv420p"
        for i in range(5):
            frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), dtype=np.uint8), format="rgb24")
            frame.pts = i
            for packet in s.encode(frame):
                box.mux(packet)
        for packet in s.encode(None):
            box.mux(packet)
    with pytest.raises(stt.Undecodable) as e:
        bot.transcribe(str(path))
    assert str(e.value) == "it has no audio track"
    assert e.value.__context__ is None           # nothing else raised on the way out
    assert posted == [] and leftovers(tmpwav) == []
    assert logged == ["VOICE NOTE not decoded to wav (clip.mp4): it has no audio track"]


def test_a_missing_file_says_so(tmp_path, tmpwav, posted, logged):
    with pytest.raises(stt.Undecodable) as e:
        bot.transcribe(str(tmp_path / "gone.m4a"))
    assert str(e.value) == "No such file or directory"
    assert posted == [] and leftovers(tmpwav) == []


# ---- no PyAV (a chat-only bot, no media pack): posted raw, as before -------

NO_AV_LINE = ("VOICE NOTE not decoded to wav (voice-message.m4a): PyAV (av, from the "
              "media pack) is not installed; posting it as it came")


def test_no_pyav_posts_the_note_as_it_came_and_says_so(tmp_path, tmpwav, posted, logged, monkeypatch):
    """No regression for a chat-only host on a server that reads M4A: the note
    goes raw, as it did before the decode existed, and the log says why."""
    note = make_m4a(tmp_path / "voice-message.m4a")
    before = (tmp_path / "voice-message.m4a").read_bytes()
    monkeypatch.setitem(sys.modules, "av", None)  # `import av` raises ImportError
    assert not stt.have_pyav()
    assert bot.transcribe(note) == "hello there"
    assert [p["path"] for p in posted] == [note] and not posted[0]["wav"]
    assert (tmp_path / "voice-message.m4a").read_bytes() == before
    assert leftovers(tmpwav) == []
    assert logged == [NO_AV_LINE]


def test_no_pyav_and_a_server_that_refuses_m4a_reads_todays_line(tmp_path, tmpwav, logged, monkeypatch):
    """Raw to a server that refuses it: the post fails as it did before, so the
    agent gets today's line, and the log has both reasons."""
    note = make_m4a(tmp_path / "voice-message.m4a")
    monkeypatch.setitem(sys.modules, "av", None)
    monkeypatch.setattr(bot.stt, "transcribe", lambda *a, **k: None)
    assert bot.message_text(snap(note)) == ("[voice note - transcription failed]\n"
                                            f"[attachment: voice-message.m4a (audio/mp4) at {note}]")
    assert NO_AV_LINE in logged


def test_to_wav_itself_still_refuses_with_no_pyav(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)
    with pytest.raises(stt.Undecodable) as e:
        stt.to_wav(tmp_path / "x.m4a", tmp_path / "x.wav")
    assert str(e.value) == stt.NO_PYAV == "PyAV (av, from the media pack) is not installed"
    assert not (tmp_path / "x.wav").exists()


def test_with_pyav_a_failed_decode_is_never_posted_raw(tmp_path, tmpwav, posted, logged):
    """The raw post is only for no PyAV at all: once a decode is tried and
    fails, nothing is posted, not even the original."""
    assert stt.have_pyav()
    junk = tmp_path / "voice-message.m4a"
    junk.write_bytes(b"\x00" * 512)
    with pytest.raises(stt.Undecodable):
        bot.transcribe(str(junk))
    assert posted == [] and leftovers(tmpwav) == []
    assert not any("posting it as it came" in line for line in logged)


def snap(path, mime="audio/mp4", name="voice-message.m4a"):
    return SimpleNamespace(id=7, text="", file=path, file_mime=mime, file_name=name,
                           view_type="Voice", quote=None)


def test_the_agents_mail_says_why_the_note_was_not_decoded(tmp_path, tmpwav, posted, logged):
    junk = tmp_path / "voice-message.m4a"
    junk.write_bytes(b"\x00" * 512)
    text = bot.message_text(snap(str(junk)))
    first, second = text.split("\n")
    assert first.startswith("[voice note - transcription failed: it could not be decoded (")
    assert first.endswith(")]")
    assert second == f"[attachment: voice-message.m4a (audio/mp4) at {junk}]"
    assert posted == []
    assert any(l.startswith("VOICE NOTE not decoded to wav (voice-message.m4a): ") for l in logged)


def test_a_failed_post_keeps_todays_line(tmp_path, tmpwav, monkeypatch, logged):
    """The speech server failing is not a decode failure: the line is as it was."""
    monkeypatch.setattr(bot.stt, "transcribe", lambda *a, **k: None)
    note = make_m4a(tmp_path / "voice-message.m4a")
    text = bot.message_text(snap(note))
    assert text == ("[voice note - transcription failed]\n"
                    f"[attachment: voice-message.m4a (audio/mp4) at {note}]")
    assert leftovers(tmpwav) == []


def test_a_decoded_note_reaches_the_agent_as_its_words(tmp_path, tmpwav, posted, logged):
    assert bot.message_text(snap(make_m4a(tmp_path / "voice-message.m4a"))) == "hello there"
    assert posted[0]["info"][:3] == (16000, 1, 2)


# ---- end to end: real curl, a Parakeet-like stub on 127.0.0.1 --------------

class ParakeetLike(BaseHTTPRequestHandler):
    """Answers like an mlx-audio server running Parakeet: a wav is read, an m4a
    is refused with the error the field saw. Records what each upload held."""
    got: ClassVar[list] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        start = body.index(b"\r\n\r\n", body.index(b'name="file"')) + 4
        head = body[start:start + 12]
        ParakeetLike.got.append(head)
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            code, reply = 200, b'{"text": " parakeet heard you "}'
        else:
            code, reply = 400, b'{"detail": "Unsupported output format: m4a"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *a):
        pass


@pytest.fixture
def parakeet():
    srv = HTTPServer(("127.0.0.1", 0), ParakeetLike)
    ParakeetLike.got = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"mlx-audio+http://127.0.0.1:{srv.server_port}/v1/audio/transcriptions"
    srv.shutdown()
    srv.server_close()


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_end_to_end_the_stub_refuses_the_raw_m4a_and_reads_the_decoded_wav(
        parakeet, tmp_path, tmpwav, logged, monkeypatch):
    note = make_m4a(tmp_path / "voice-message.m4a")
    raw = []
    # the old path, posting the file as it came: refused, as in the field
    assert stt.transcribe(note, parakeet, None, raw.append) is None
    assert "HTTP 400" in raw[0] and "Unsupported output format: m4a" in raw[0]
    # the bot's path: decoded first, so the stub gets a wav and answers
    monkeypatch.setattr(bot.dc_env, "STT_URL", parakeet)
    monkeypatch.setattr(bot.dc_env, "STT_MODEL", None)
    assert bot.transcribe(note) == "parakeet heard you"
    assert ParakeetLike.got[0][4:8] == b"ftyp" and ParakeetLike.got[1][:4] == b"RIFF"
    assert leftovers(tmpwav) == []


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_end_to_end_a_wav_goes_to_the_stub_as_it_is(parakeet, tmp_path, tmpwav, logged, monkeypatch):
    note = make_wav(tmp_path / "chunk.wav", rate=16000, channels=1)
    monkeypatch.setattr(bot.dc_env, "STT_URL", parakeet)
    monkeypatch.setattr(bot.dc_env, "STT_MODEL", None)
    assert bot.transcribe(note) == "parakeet heard you"
    assert ParakeetLike.got == [(tmp_path / "chunk.wav").read_bytes()[:12]]
    assert leftovers(tmpwav) == []

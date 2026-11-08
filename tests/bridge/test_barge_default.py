"""Barge-in is ON by default: the caller can cut the bot off mid-line.

DC_BARGE_IN=1 is the default; DC_BARGE_IN=0 turns it off. It ships with the
residual-echo guard working. Before, the guard in _heard read
`why.score if why else 0.0`, but a Verdict is truthy only when it is an echo,
and every echo has returned by then, so the score was always 0.0 and a line
that looked partly like our own voice (0.55 <= score < the echo bar) cut us
off anyway.

These are the cases a default-on barge-in needs: the default itself, the
residual echo, a full echo, real speech, a backchannel and a stop word, a
mouth line on the track, a reply the floor holds, and the ears path (where
this cut does not run). The real CallSession._heard, PipVoiceTrack, EchoGuard,
Floor and mouth leg; TTS and STT are fakes.
"""
import asyncio
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import types

import numpy as np
import pytest

from sessionphone.bridge import calls, mouthleg
from sessionphone.bridge.floor import Floor

SAID = "The deploy finished and every test on the branch passed cleanly."
REAL = "Hang on, I need you to stop talking for a second please."
# part our words, part his: scores 0.625 against SAID, under the echo bar
# (so it is not dropped as an echo) and over BARGE_IN_MAX_ECHO (0.55)
RESIDUAL = "every test on the branch hmm can you hear me now"
ECHO = "The deploy finished and every test on the branch"


class FakeChat:
    def __init__(self, posted):
        self.posted = posted

    def send_text(self, text):
        self.posted.append(text)


class FakeAccount:
    def __init__(self):
        self.posted = []

    def get_chat_by_id(self, _):
        return FakeChat(self.posted)


@pytest.fixture
def logs(monkeypatch):
    out = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(out.append, lambda p: "", lambda *a, **k: None))
    return out


@pytest.fixture
def listed(monkeypatch, tmp_path):
    """Chat 12 is a listed chat, so a barge-in remainder may be posted to it."""
    f = tmp_path / "channels.txt"
    f.write_text("family  chat:12\n")
    f.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(f))


def hears(monkeypatch, logs, text):
    """The next chunk transcribes as TEXT."""
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logs.append, lambda p: text, lambda *a, **k: None))


def session(monkeypatch, seconds=30.0):
    """A call that can speak() and _heard(), with barge-in at the module's value."""
    monkeypatch.setattr(calls, "tts_pcm48", lambda text, voice=None: np.zeros(
        int(calls.OUT_RATE * seconds), dtype=np.int16))
    s = object.__new__(calls.CallSession)
    s.account, s.chat_id, s.pc = FakeAccount(), 12, object()
    s.voice = calls.PipVoiceTrack()
    s.echo = calls.EchoGuard(spool=None, voicemode=None)
    s.heard = types.SimpleNamespace(saying=lambda *a, **k: None, said=lambda *a, **k: None,
                                    partial=lambda *a, **k: None)
    s.lines, s.turn_parts, s.turn_pending = [], [], set()
    s.turn_timer, s.speaking, s.previews = None, False, None
    s.preview_id, s.chunk_n = None, 0
    s._turn_arm = lambda: None
    return s


def barged(logs):
    return [m for m in logs if "BARGE-IN on" in m]


# ---- (i) the default is on; 0 turns it off ----

def _barge_in_at_import(env):
    """calls.BARGE_IN as a fresh bot reads it from ENV (no DC_* but what is given)."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    root = tempfile.mkdtemp(prefix="barge-")
    out = subprocess.run(
        [sys.executable, "-c", "from sessionphone.bridge import calls; print(calls.BARGE_IN)"],
        env={**clean, "DC_ROOT": root, "DC_CALLS": "off", **env},
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_barge_in_is_on_with_no_setting():
    # the suite runs with every DC_* scrubbed (conftest), so this is the default
    assert calls.BARGE_IN is True
    assert _barge_in_at_import({}) == "True"


def test_dc_barge_in_0_turns_it_off():
    assert _barge_in_at_import({"DC_BARGE_IN": "0"}) == "False"
    assert _barge_in_at_import({"DC_BARGE_IN": "1"}) == "True"


# ---- (ii) a residual echo does NOT cut ----

def test_a_residual_echo_does_not_cut(monkeypatch, logs):
    """Red before the fix: the guard read 0.0 for every line that got this far.
    Barge-in is pinned on, so this is the gate's case, whatever the default."""
    monkeypatch.setattr(calls, "BARGE_IN", True)
    s = session(monkeypatch)
    hears(monkeypatch, logs, RESIDUAL)

    async def go():
        await s.speak(SAID)
        v = s.echo.check(RESIDUAL)
        await s._heard(pathlib.Path("caller-001.wav"), 1)
        return v
    v = asyncio.run(go())
    # the case is the band the guard exists for: not an echo, but like us
    assert not v.is_echo and calls.BARGE_IN_MAX_ECHO <= v.score, v.score
    assert s.voice.speaking(), "a line that looks like our own voice cut us off"
    assert not barged(logs), logs
    assert s.account.posted == []
    # never silently: the held cut is logged, with the score that held it
    (held,) = [m for m in logs if "BARGE-IN held" in m]
    assert "caller-001.wav" in held and f"{v.score:.2f}" in held, held
    # it is still his line: it reaches the turn
    assert (1, RESIDUAL) in s.turn_parts


def test_a_line_under_the_residual_band_still_cuts(monkeypatch, logs, listed):
    """The guard reads the score, it does not block every line that shares a word."""
    monkeypatch.setattr(calls, "BARGE_IN", True)
    s = session(monkeypatch)
    heard = "the branch is fine but can you hold off on the release tonight"
    hears(monkeypatch, logs, heard)

    async def go():
        await s.speak(SAID)
        v = s.echo.check(heard)
        await s._heard(pathlib.Path("caller-001.wav"), 1)
        return v
    v = asyncio.run(go())
    assert not v.is_echo and 0.0 < v.score < calls.BARGE_IN_MAX_ECHO, v.score
    assert barged(logs) and not s.voice.speaking()


# ---- (iii) a full echo does not cut ----

def test_our_own_echo_does_not_cut(monkeypatch, logs):
    s = session(monkeypatch)
    hears(monkeypatch, logs, ECHO)

    async def go():
        await s.speak(SAID)
        await s._heard(pathlib.Path("caller-001.wav"), 1)
    asyncio.run(go())
    assert s.voice.speaking(), "our own echo cut us off"
    assert any("ECHO caller-001.wav" in m for m in logs), logs
    assert not barged(logs)
    assert s.turn_parts == [(1, "")]          # the echo keeps his turn open, adds nothing


# ---- (iv) real speech cuts, and the rest of the sentence is posted ----

def test_real_speech_cuts_and_posts_the_remainder(monkeypatch, logs, listed):
    monkeypatch.setattr(calls, "BARGE_IN_KEEP_TEXT", True)
    s = session(monkeypatch)
    hears(monkeypatch, logs, REAL)

    async def go():
        await s.speak(SAID)
        assert s.voice.speaking()
        await s._heard(pathlib.Path("caller-001.wav"), 1)
    asyncio.run(go())
    assert not s.voice.speaking(), "he talked over us and we kept talking"
    assert barged(logs) and any("interruption dropped=" in m for m in logs)
    assert any(who == "(barge-in)" for _, who, _ in s.lines)
    (post,) = s.account.posted
    assert post.startswith("(cut off) ") and "cleanly" in post, post
    assert (1, REAL) in s.turn_parts           # his words are his turn, cut or not


# ---- (v) a backchannel does not cut; a stop word does (at _heard) ----

@pytest.mark.parametrize("heard", ["Yeah, okay.", "Mm-hmm, right.", "Oh, nice."])
def test_a_backchannel_does_not_cut(monkeypatch, logs, heard):
    assert len(heard) < 18
    s = session(monkeypatch)
    hears(monkeypatch, logs, heard)

    async def go():
        await s.speak(SAID)
        await s._heard(pathlib.Path("caller-001.wav"), 1)
    asyncio.run(go())
    assert s.voice.speaking(), f"{heard!r} cut us off"
    assert not barged(logs)


@pytest.mark.parametrize("heard", ["Stop.", "Wait.", "Hang on."])
def test_a_stop_word_cuts(monkeypatch, logs, listed, heard):
    s = session(monkeypatch)
    hears(monkeypatch, logs, heard)

    async def go():
        await s.speak(SAID)
        await s._heard(pathlib.Path("caller-001.wav"), 1)
    asyncio.run(go())
    assert not s.voice.speaking(), f"{heard!r} did not stop us"
    assert barged(logs)


# ---- (vi) a cut drops a mouth line that is playing ----

def frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


def test_a_barge_in_drops_a_playing_mouth_line(monkeypatch, logs):
    s = session(monkeypatch)
    hears(monkeypatch, logs, REAL)

    async def go():
        path = pathlib.Path(tempfile.mkdtemp(prefix="bd-")) / "m.sock"
        server = await mouthleg.serve(types.SimpleNamespace(sessions={1: s}), path, logs.append)
        try:
            r, w = await asyncio.open_unix_connection(str(path))
            w.write((json.dumps({"call": 1, "text": "Morning Bob, the build is green.",
                                 "who": "agent"}) + "\n").encode())
            assert json.loads(await r.readline()) == {"ok": True, "call": 1}
            w.write(frame(b"A", np.full(48000, 101, dtype="<i2").tobytes()))   # 1 s of it
            await w.drain()
            for _ in range(200):
                if s.voice.said_n:
                    break
                await asyncio.sleep(0.01)
            assert s.voice.speaking(), "the mouth line is not on the track"
            await s._heard(pathlib.Path("caller-001.wav"), 1)      # he talks over it
            w.write(frame(b"A", np.full(4800, 102, dtype="<i2").tobytes()) + frame(b"D"))
            await w.drain()
            end = json.loads(await r.readline())
            w.close()
            return end
        finally:
            server.close()
    end = asyncio.run(go())
    assert barged(logs), logs
    assert not s.voice.speaking() and s.voice.owner is None
    assert end["ended"] == "cut" and end["ok"] is False, end


# ---- (vii) a reply the floor holds is not speaking; once released, it is cut ----

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_a_floor_held_reply_is_not_cut_until_the_cap_releases_it(monkeypatch, logs):
    clock, vad = Clock(), types.SimpleNamespace(speaking=True)      # he is talking
    s = session(monkeypatch)
    s.voice.floor = Floor(lambda: vad.speaking, 0.8, 6.0, clock)
    hears(monkeypatch, logs, REAL)

    async def go():
        await s.speak(SAID, floor=True)
        (line,) = s.voice.waiting
        # held: nothing on the track, so nothing to cut, and his words are heard
        assert line.state == "waiting" and not s.voice.speaking()
        await s._heard(pathlib.Path("caller-001.wav"), 1)
        assert line.state == "waiting" and not barged(logs), logs
        assert (1, REAL) in s.turn_parts
        # he keeps talking; the cap releases the reply over him
        clock.t += 6.1
        s.voice._next_samples()
        # its audio is on the track now (all said, so the line itself is over)
        assert line.state != "waiting" and s.voice.speaking()
        assert any("released by cap" in m for m in logs), logs
        # ... and his next words cut it
        await s._heard(pathlib.Path("caller-002.wav"), 2)
    asyncio.run(go())
    (cut,) = barged(logs)
    assert "caller-002.wav" in cut, cut
    assert not s.voice.speaking()
    (dropped,) = [m for m in logs if "interruption dropped=" in m]
    assert float(dropped.split("=")[1].rstrip("s")) > 29.0, dropped   # the reply, not a frame


# ---- (viii) at DC_EARS=1 this cut does not run ----

def test_the_ears_path_has_no_barge_in(monkeypatch, logs):
    """DC_EARS=1 hands finals to _ears_dispatch, which has no echo check and no
    barge-in: the default-on cut is the energy-VAD path's (calls.py says so
    beside BARGE_IN). A cut there comes from the ear leg's listener, not here."""
    assert calls.BARGE_IN is True
    s = session(monkeypatch)
    s.ears_futs = []

    async def go():
        await s.speak(SAID)
        e = types.SimpleNamespace(type="final", text=REAL, seg=1, start=0.0, end=1.0, t=1.2)
        s._ears_dispatch(e)
    asyncio.run(go())
    assert s.voice.speaking(), "the ears path cut the line"
    assert not barged(logs)
    assert any("HEARD seg 1" in m for m in logs)

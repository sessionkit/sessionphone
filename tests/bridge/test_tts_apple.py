"""The bridge speaks on a bare Mac: no speech server means macOS say, at 48 kHz, no ffmpeg."""
import os

import pytest

from sessionphone.bridge import calls


def test_auto_falls_back_to_apple_when_no_server_answers(monkeypatch):
    seen = []
    monkeypatch.setattr(calls, "TTS_MODE", "auto")
    monkeypatch.setattr(calls, "_answers", lambda url, timeout=0.3: False)
    monkeypatch.setattr(calls, "apple_pcm48", lambda text, voice=None: seen.append(text) or "pcm")
    monkeypatch.setattr(calls, "log", lambda msg: None)     # the fallback is logged (PHONE-76)
    monkeypatch.setattr(calls.os.path, "exists", lambda p: p == calls.SAY_BIN or os.path.lexists(p))
    assert calls.tts_pcm48("hello", "af_sky") == "pcm" and seen == ["hello"]


def test_a_server_that_answers_is_used(monkeypatch):
    monkeypatch.setattr(calls, "TTS_MODE", "auto")
    monkeypatch.setattr(calls, "_answers", lambda url, timeout=0.3: True)
    monkeypatch.setattr(calls, "apple_pcm48", lambda *a, **k: pytest.fail("apple used with a live server"))
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        raise RuntimeError("stop here")

    monkeypatch.setattr(calls.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="stop here"):
        calls.tts_pcm48("hello", "af_sky")
    assert ran and ran[0][0] == "curl" and calls.KOKORO_URL in ran[0]


@pytest.mark.skipif(not os.path.exists(calls.SAY_BIN), reason="needs macOS say")
def test_apple_renders_48k_speech():
    pcm = calls.apple_pcm48("-v is spoken, not an option. Hello.")
    assert pcm.dtype.name == "int16" and len(pcm) > calls.OUT_RATE * 0.5 and abs(pcm).max() > 1000


def test_kokoro_defaults_to_mlx_audio_with_its_model(monkeypatch):
    """kokoro-fastapi on :8880 is retired; mlx-audio refuses the bare name "kokoro"
    (404, measured Wed 2030-06-12), so the default must name the repo."""
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items()
           if k not in ("DC_KOKORO_URL", "DC_KOKORO_MODEL", "PIP_CHAT_KOKORO_URL", "PIP_CHAT_KOKORO_MODEL")}
    out = subprocess.run(
        [sys.executable, "-c",
         "from sessionphone.bridge import calls; print(calls.KOKORO_URL); print(calls.KOKORO_MODEL)"],
        env=env, capture_output=True, text=True, timeout=60, check=True).stdout.split()
    assert ":8890/" in out[0] and out[1] == "mlx-community/Kokoro-82M-bf16"


def test_kokoro_url_and_model_are_settings(monkeypatch):
    monkeypatch.setattr(calls, "TTS_MODE", "server")
    monkeypatch.setattr(calls, "KOKORO_URL", "http://127.0.0.1:9999/v1/audio/speech")
    monkeypatch.setattr(calls, "KOKORO_MODEL", "some/model")
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        raise RuntimeError("stop here")

    monkeypatch.setattr(calls.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="stop here"):
        calls.tts_pcm48("hello", "af_sky")
    import json
    body = json.loads(ran[0][ran[0].index("-d") + 1])
    assert "http://127.0.0.1:9999/v1/audio/speech" in ran[0]
    assert body["model"] == "some/model" and body["voice"] == "af_sky"


def test_apple_fallback_is_logged(monkeypatch):
    said = []
    monkeypatch.setattr(calls, "TTS_MODE", "auto")
    monkeypatch.setattr(calls, "_answers", lambda url, timeout=0.3: False)
    monkeypatch.setattr(calls, "apple_pcm48", lambda text, voice=None: "pcm")
    monkeypatch.setattr(calls, "log", said.append)
    monkeypatch.setattr(calls.os.path, "exists", lambda p: p == calls.SAY_BIN or os.path.lexists(p))
    calls.tts_pcm48("hello", "af_sky")
    assert any("TTS SERVER DOWN" in m and "Apple voice" in m and "af_sky" in m for m in said)

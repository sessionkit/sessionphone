"""A TTS failure's fallback job must never be voiced back into the call.

In a live demo (Thu 2030-06-13): TTS refused
(curl rc=7), the fallback posted "(unspoken, TTS failed) ..." to the outbox,
the outbox loop spoke it into the live call, and each further failure
nested one more marker. The job now carries `no_speak`, and the outbox
loop skips `speak_if_live` for it.
"""
import asyncio
import json
import pathlib

from sessionphone.bridge import bot, calls
import pytest  # noqa: E402,F401 - bin/bridge-check runs a file under pytest only if it imports it


def _session():
    s = object.__new__(calls.CallSession)
    s.chat_id = 12
    s.lines = []
    return s


def test_fallback_job_is_marked_no_speak(tmp_path, monkeypatch):
    monkeypatch.setenv("DC_OUTBOX", str(tmp_path))
    logged = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logged.append, lambda p: p, lambda *a, **k: None))

    def refused(text, voice):
        raise RuntimeError("tts curl rc=7 bytes=0")

    monkeypatch.setattr(calls, "tts_pcm48", refused)
    asyncio.run(_session().speak("Hi Bob, hi everyone."))
    jobs = [json.loads(p.read_text()) for p in sorted(tmp_path.glob("*.json"))]
    assert jobs, f"no fallback job written; log: {logged}"
    for job in jobs:
        assert job["no_speak"] is True
        assert job["text"].startswith("(unspoken, TTS failed) ")


def test_outbox_loop_skips_speech_for_no_speak_jobs():
    # The loop lives inside bot.main(); pin the guard to the speak_if_live call.
    src = pathlib.Path(bot.__file__).read_text()
    call = src.index("calls.speak_if_live(")
    assert 'not job.get("no_speak")' in src[call - 120:call], "outbox would speak a no_speak job"

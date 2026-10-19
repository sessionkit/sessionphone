"""M8: a Delta call's words into VoiceMode's heard log.

The real heard.py, loaded by path as the bridge does, writes into a tmp
VOICEMODE_BASE_DIR. HEARD_PY_FOR_TESTS names a VoiceMode checkout's
heard.py, and HEARD_PY_SAID_FOR_TESTS one that has said(); without them
these tests skip. No call, no network, no sound.
"""
import asyncio
import json
import os
import pathlib

import pytest

from sessionphone.bridge import calls, heard_sink
from sessionphone.bridge.heard_sink import CallSink

HEARD_PY = pathlib.Path(os.environ.get("HEARD_PY_FOR_TESTS") or "/nonexistent/heard.py")

pytestmark = pytest.mark.skipif(not HEARD_PY.exists(), reason=f"no heard.py at {HEARD_PY}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("DC_HEARD_PY", str(HEARD_PY))
    monkeypatch.setenv("DC_HEARD", "1")
    monkeypatch.setenv("DC_HEARD_CHATS", "12")
    heard_sink._reset_for_tests()
    yield tmp_path
    heard_sink._reset_for_tests()


def lines(base):
    return [json.loads(l) for f in sorted((base / "logs" / "conversations").glob("heard_*.jsonl"))
            for l in f.read_text().splitlines()]


def test_off_by_default(env, monkeypatch):
    monkeypatch.delenv("DC_HEARD")
    monkeypatch.delenv("PIP_CHAT_HEARD", raising=False)
    CallSink(12, print).turn("not written")
    assert not (env / "logs").exists()


def test_a_chat_not_listed_is_refused_and_said_once(env):
    said = []
    s = CallSink(99, said.append)
    s.partial("a stranger")
    s.turn("a stranger's turn")
    assert not (env / "logs").exists()
    assert len(said) == 1 and "not in DC_HEARD_CHATS" in said[0]


def test_the_shape_matches_the_mic(env):
    s = CallSink(12, print, chat_name="Bob", call="5531")
    s.event("call-started", direction="in")
    s.partial("so the bot", chunk=1)
    s.partial("will post it", chunk=2)
    s.turn("so the bot will post it", detector="turn-join")
    s.event("call-ended", reason="hang-up")
    recs = lines(env)
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5]
    assert [r["kind"] for r in recs] == ["event", "partial", "partial", "turn", "event"]
    for r in recs:
        assert r["source"] == "call"
        assert r["device"] == f"delta:{calls.dc_env.AGENT}/12"
        assert r["chat_id"] == 12 and r["chat_name"] == "Bob" and r["call"] == "5531"
    assert recs[1]["final"] is False and recs[3]["final"] is True
    assert recs[3]["detector"] == "turn-join"
    assert recs[0]["event"] == "call-started" and recs[4]["reason"] == "hang-up"


def test_blank_text_writes_nothing(env):
    s = CallSink(12, print)
    s.partial("  ")
    s.turn("")
    assert not (env / "logs").exists()


def test_a_missing_heard_py_is_said_once_and_never_raises(env, monkeypatch):
    monkeypatch.setenv("DC_HEARD_PY", str(env / "nope"))
    said = []
    s = CallSink(12, said.append)
    s.partial("one")
    s.turn("two")
    assert len(said) == 1 and "could not be loaded" in said[0]


def test_the_hook_shows_a_call_line(env):
    """What the hook prints for a call turn, from the same log."""
    CallSink(12, print).turn("hello from the kitchen")
    loader_mod = heard_sink._module
    loader_mod.advance_cursor("probe", 0)
    out = json.loads(loader_mod.run_hook(json.dumps({"session_id": "probe"})))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(f"[heard call/delta:{calls.dc_env.AGENT}/12 ")
    assert ctx.endswith("] hello from the kitchen")


# --- the seam: the real CallSession methods write through the sink ----------

def _session():
    s = object.__new__(calls.CallSession)
    s.account, s.chat_id, s.chat_name, s.caller = None, 12, "Bob", "Bob"
    s.voice = calls.PipVoiceTrack()
    s.echo = calls.EchoGuard(spool=None)
    s.lines, s.turn_parts, s.turn_pending = [], [], set()
    s.turn_timer, s.speaking, s.previews = None, False, None
    s.preview_id, s.chunk_n = None, 0
    s.root_id = "call-test@example.org"
    s.guest_list = lambda: []
    s._turn_arm = lambda: None
    s.heard = CallSink(12, print, chat_name="Bob", call="1")
    return s


def test_callsession_heard_and_mail_turn_write_partial_and_turn(env, monkeypatch):
    monkeypatch.setattr(calls, "PREVIEWS", False)
    mailed = []
    calls._hooks = calls.Hooks(lambda m: None, lambda p: "can you check the build",
                               lambda *a, **k: mailed.append(a))
    s = _session()
    asyncio.run(s._heard(pathlib.Path("caller-001.wav"), 1))
    asyncio.run(s._mail_turn("can you check the build", detector="turn-join"))
    recs = lines(env)
    assert [(r["kind"], r["text"]) for r in recs] == [
        ("partial", "can you check the build"), ("turn", "can you check the build")]
    assert recs[0]["chunk"] == 1 and recs[1]["detector"] == "turn-join"
    assert mailed, "the mail path must still run"


def test_callsession_not_words_writes_nothing(env):
    calls._hooks = calls.Hooks(lambda m: None, lambda p: "[BLANK_AUDIO]", lambda *a, **k: None)
    s = _session()
    asyncio.run(s._heard(pathlib.Path("caller-002.wav"), 2))
    assert not (env / "logs").exists()


# --- said-rows: the bridge's own voice, as the mouth writes it -------------

HEARD_PY_SAID = pathlib.Path(os.environ.get("HEARD_PY_SAID_FOR_TESTS") or "/nonexistent/heard.py")
needs_said = pytest.mark.skipif(not HEARD_PY_SAID.exists(),
                                reason=f"no heard.py with said() at {HEARD_PY_SAID}")


@needs_said
def test_saying_and_said_are_the_mouths_pair(env, monkeypatch):
    monkeypatch.setenv("DC_HEARD_PY", str(HEARD_PY_SAID))
    s = CallSink(12, print, chat_name="Bob", call="1031")
    s.saying("Tag triage, got it.", utt="u1", voice="carol")
    s.said(utt="u1", played_s=1.5, reason="done")
    recs = lines(env)
    assert [(r["kind"], r["utt"]) for r in recs] == [("saying", "u1"), ("said", "u1")]
    assert recs[0]["text"] == "Tag triage, got it." and recs[0]["final"] is False
    assert recs[1]["final"] is True and recs[1]["played_s"] == 1.5
    for r in recs:
        assert r["source"] == "call" and r["device"] == f"delta:{calls.dc_env.AGENT}/12"
        assert r["agent"] == calls.dc_env.AGENT and r["call"] == "1031"


def test_an_older_heard_py_without_said_is_said_once_and_skipped(env):
    """The live bot's heard.py (VM-2274) has no saying()/said(): nothing is
    written, nothing raises, and the log says why exactly once."""
    said = []
    s = CallSink(12, said.append)
    mod = s._heard()
    if callable(getattr(mod, "said", None)):
        pytest.skip(f"{HEARD_PY} already has said()")
    s.saying("one", utt="a")
    s.saying("two", utt="b")
    s.said(utt="a")
    assert not (env / "logs").exists()
    msgs = [m for m in said if "has no heard." in m]
    assert len(msgs) == 2 and "saying()" in msgs[0] and "said()" in msgs[1]


@needs_said
def test_callsession_speak_writes_saying_then_said_at_the_lines_end(env, monkeypatch):
    monkeypatch.setenv("DC_HEARD_PY", str(HEARD_PY_SAID))
    rate = calls.OUT_RATE
    monkeypatch.setattr(calls, "tts_pcm48",
                        lambda text, voice=None: calls.np.zeros(int(rate * 0.2), dtype=calls.np.int16))
    spoke = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(spoke.append, lambda p: "",
                                                     lambda *a, **k: None))
    s = _session()

    async def go():
        await s.speak("First one. Second one.", voice="carol", who="Carol")
        assert [r["kind"] for r in lines(env)] == ["saying"]   # said waits for the audio
        await asyncio.sleep(0.6)
    asyncio.run(go())
    recs = lines(env)
    assert [r["kind"] for r in recs] == ["saying", "said"]
    assert recs[0]["utt"] == recs[1]["utt"]
    assert recs[0]["text"] == "First one. Second one."
    assert recs[1]["reason"] == "done" and recs[1]["cut"] is False
    assert recs[1]["played_s"] > 0

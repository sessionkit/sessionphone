#!/usr/bin/env python3
"""Integration: the guard is actually wired into calls.py, not just present.

    BOT=/path/to/delta-chat/bot .venv/bin/python test_wiring.py

Drives the REAL CallSession.speak() and CallSession._heard(): speaks a line,
then feeds back the mangled version transcription actually returned, and
asserts it never reaches turn_parts. A unit test on echo.py alone cannot
catch a patch applied to the wrong function.
"""
import asyncio, atexit, os, pathlib, shutil, sys, time, types, tempfile

from sessionphone.bridge import calls

SAID = "We have done it four times, in the care packs for Ben and Ada."
ECHO = "We have done it four times."
REAL = "Git bundle create and send the file into the chat."

logged = []
calls._hooks = calls.Hooks(lambda m: logged.append(m), lambda p: p, lambda *a, **k: None)


posted: list = []

# PHONE-102: an in-call chat post goes only to a listed chat (design rule 5,
# rows 11-13), so the chat these cases post to, 12, is listed.
_LIST = pathlib.Path(tempfile.mkdtemp(prefix="channels-")) / "channels.txt"
atexit.register(shutil.rmtree, _LIST.parent, True)   # leave no channels-* dir behind
_LIST.write_text("family  chat:12\n")
_LIST.chmod(0o600)
os.environ["DC_CHANNELS_FILE"] = str(_LIST)


class FakeChat:
    def send_text(self, t):
        posted.append(t)


class FakeAccount:
    def get_chat_by_id(self, _):
        return FakeChat()


def session():
    s = object.__new__(calls.CallSession)
    s.account, s.chat_id = FakeAccount(), 12
    s.voice = calls.PipVoiceTrack()
    s.echo = calls.EchoGuard(spool=None)
    s.lines, s.turn_parts, s.turn_pending = [], [], set()
    s.turn_timer, s.speaking, s.previews = None, False, None
    s.preview_id, s.chunk_n = None, 0
    s._turn_arm = lambda: None                   # no event loop timers here
    return s


async def main():
    s = session()

    # 1. speak() must register the clip with the guard, with its duration
    pcm_seconds = []
    orig = calls.tts_pcm48
    calls.tts_pcm48 = lambda text, voice: __import__("numpy").zeros(
        int(calls.OUT_RATE * 5.0), dtype=__import__("numpy").int16)
    await s.speak(SAID)
    calls.tts_pcm48 = orig
    assert s.echo.played, "speak() did not tell the guard anything"
    p = s.echo.played[-1]
    assert abs((p.end - p.start) - 5.0) < 0.01, f"wrong duration: {p.end - p.start}"
    assert p.text.strip().rstrip(".") in SAID or SAID.startswith(p.text[:20])

    # 2. the echo of it must be dropped, and logged
    calls._hooks = calls.Hooks(lambda m: logged.append(m), lambda path: ECHO,
                               lambda *a, **k: None)
    await s._heard(pathlib.Path("caller-001.wav"), 1)
    assert s.turn_parts == [(1, "")], f"echo reached the turn: {s.turn_parts}"
    assert any("ECHO" in m for m in logged), "dropped silently"

    # 3. real speech in the same window must survive
    calls._hooks = calls.Hooks(lambda m: logged.append(m), lambda path: REAL,
                               lambda *a, **k: None)
    await s._heard(pathlib.Path("caller-002.wav"), 2)
    assert (2, REAL) in s.turn_parts, f"dropped real speech: {s.turn_parts}"

    # 4. barge-in: real speech while we are still talking cuts the audio.
    #    Forced on here - it ships DEFAULT OFF because it depends on echo
    #    suppression having been seen working on a live call first.
    calls.BARGE_IN = True
    s2 = session()
    calls.tts_pcm48 = lambda text, voice: __import__("numpy").zeros(
        int(calls.OUT_RATE * 30.0), dtype=__import__("numpy").int16)
    calls._hooks = calls.Hooks(lambda m: logged.append(m), lambda path: REAL,
                               lambda *a, **k: None)
    await s2.speak(SAID)
    calls.tts_pcm48 = orig
    assert s2.voice.speaking(), "nothing queued to interrupt"
    await s2._heard(pathlib.Path("caller-003.wav"), 3)
    assert not s2.voice.speaking(), "barge-in did not stop the audio"
    assert any("BARGE-IN" in m for m in logged), "cut without saying where"
    assert any(who == "(barge-in)" for _, who, _ in s2.lines), \
        "the transcript does not record that he never heard the rest"
    assert posted and posted[-1].startswith("(cut off) "), \
        "the words he did not hear were destroyed instead of posted"
    assert SAID.split()[-1].strip(".") in posted[-1], \
        "the posted remainder is not the rest of the sentence"
    assert len(posted) == 1, \
        "only the sentence in flight is posted - a queue dumped on him as " \
        "a wall of text is punishment for interrupting"

    # 5. ... but our OWN echo must never trigger it
    s3 = session()
    calls.tts_pcm48 = lambda text, voice: __import__("numpy").zeros(
        int(calls.OUT_RATE * 30.0), dtype=__import__("numpy").int16)
    calls._hooks = calls.Hooks(lambda m: logged.append(m), lambda path: ECHO,
                               lambda *a, **k: None)
    await s3.speak(SAID)
    calls.tts_pcm48 = orig
    await s3._heard(pathlib.Path("caller-004.wav"), 4)
    assert s3.voice.speaking(), "our own echo cut us off - the VAD trap"

    print("wiring ok:")
    for m in logged:
        if "ECHO" in m or "HEARD" in m:
            print("   ", m[:150])


asyncio.run(main())

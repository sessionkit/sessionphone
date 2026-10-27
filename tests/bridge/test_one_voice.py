"""A call speaks one line at a time: a bot line never lands inside a mouth line.

The mouth streams a line into the call as A frames over its socket, and the
bot speaks a guest's reply (speak(), one sentence at a time) into the SAME
voice track. Both queue onto that one track. A bot sentence that arrives while
a mouth line is still streaming must wait for the mouth line to end; it must
never be spliced between two of its frames.

A real socket, a real loop and the real PipVoiceTrack; TTS is a fake.
"""
import asyncio
import json
import pathlib
import struct
import tempfile
import types

import numpy as np
import pytest  # noqa: F401 - bin/bridge-check runs a file under pytest only if it imports it

from sessionphone.bridge import calls, mouthleg

MOUTH_1, MOUTH_2, BOT = 101, 102, 7      # each part's samples carry its own value


class Echo:
    def spoken(self, text, dur, at=None, source="bridge", start=None):
        pass

    def cut(self, at=None):
        return "cut"


class Heard:
    def saying(self, *a, **k):
        pass

    def said(self, *a, **k):
        pass


class Manager:
    def __init__(self, sessions):
        self.sessions = sessions


def _session():
    s = object.__new__(calls.CallSession)
    s.chat_id, s.pc, s.lines = 12, object(), []
    s.voice, s.echo, s.heard = calls.PipVoiceTrack(), Echo(), Heard()
    return s


def frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


def tone(value, seconds):
    return np.full(int(48000 * seconds), value, dtype="<i2")


async def _until(cond, what, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _played(track):
    """Drain the track as the call would hear it -> the runs of sample values."""
    out = []
    while track.speaking():
        out.append(track._voice_samples())
    runs = []
    for v in np.concatenate(out):
        v = int(v)
        if v and (not runs or runs[-1] != v):
            runs.append(v)
    return runs


async def _mouth_line_with_a_bot_line_mid_stream(monkeypatch):
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, lambda p: p, lambda *a, **k: None))
    voiced = []
    monkeypatch.setattr(calls, "tts_pcm48", lambda text, voice=None: voiced.append(text) or tone(BOT, 0.2))
    s = _session()
    path = pathlib.Path(tempfile.mkdtemp(prefix="ov-")) / "s.sock"
    server = await mouthleg.serve(Manager({1: s}), path, lambda m: None)
    try:
        r, w = await asyncio.open_unix_connection(str(path))
        w.write((json.dumps({"call": 1, "text": "Morning Bob, the build is green.",
                             "who": "agent"}) + "\n").encode())
        assert json.loads(await r.readline()) == {"ok": True, "call": 1}
        # The mouth's first frame is on the track: its line has begun.
        w.write(frame(b"A", tone(MOUTH_1, 0.2).tobytes()))
        await w.drain()
        await _until(lambda: s.voice.said_n == 1, "the mouth's first frame")
        # A guest's reply arrives by mail and the bot speaks it, mid-line. It
        # runs as its own task: a fixed speak() may wait for the track, so the
        # test must not block on it before the mouth line can end.
        bot = asyncio.ensure_future(s.speak("Hi there."))
        await _until(lambda: voiced, "the bot's sentence to be voiced")
        await asyncio.sleep(0.05)          # speak() hands its sentence on now
        # The rest of the mouth's line streams in, then it ends.
        w.write(frame(b"A", tone(MOUTH_2, 0.2).tobytes()) + frame(b"D"))
        await w.drain()
        end = json.loads(await r.readline())
        await asyncio.wait_for(bot, 2.0)   # the bot's line is never left stuck
        w.close()
    finally:
        server.close()
    return end, _played(s.voice)


def test_a_bot_line_never_cuts_into_a_streaming_mouth_line(monkeypatch):
    end, runs = asyncio.run(_mouth_line_with_a_bot_line_mid_stream(monkeypatch))
    assert end["ended"] == "done", end
    # The mouth line began first, so it plays whole; the bot's line waits behind it.
    assert runs == [MOUTH_1, MOUTH_2, BOT], (
        f"the call played {runs}: the bot line was spliced into the mouth line")


# ---- the track: one line owns it until it ends ------------------------------

class Clips:
    """An echo guard that keeps what it was told."""
    def __init__(self):
        self.clips, self.cuts = [], 0

    def spoken(self, text, dur, at=None, source="bridge", start=None):
        self.clips.append((text, round(dur, 3), source, start))

    def cut(self, at=None):
        self.cuts += 1
        return "cut"


@pytest.fixture
def hooks(monkeypatch):
    logs = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logs.append, lambda p: p, lambda *a, **k: None))
    return logs


def test_a_line_that_arrives_second_waits_and_plays_whole():
    t = calls.PipVoiceTrack()
    a, b = t.line("A"), t.line("B")
    assert (a.state, b.state) == ("owning", "waiting")
    a.say(tone(1, 0.02))
    b.say(tone(2, 0.02))                    # waits in b's own buffer
    a.say(tone(3, 0.02))
    assert _played(t) == [1, 3]             # nothing of b's is on the track yet
    b.say(tone(4, 0.02))
    a.end()                                 # b's turn: its buffer goes on in one step
    assert (a.state, b.state) == ("done", "owning")
    b.say(tone(5, 0.02))
    b.end()
    assert _played(t) == [2, 4, 5] and t.owner is None


def test_waiting_lines_go_in_arrival_order_and_an_ended_one_passes_the_track_on():
    t = calls.PipVoiceTrack()
    a, b, c = t.line(), t.line(), t.line()
    c.say(tone(3, 0.02))
    c.end()                                 # c is complete while still waiting
    b.say(tone(2, 0.02))
    b.end()
    a.say(tone(1, 0.02))
    a.end()                                 # a, then b, then c, with nothing to wait for
    assert _played(t) == [1, 2, 3]
    assert [x.state for x in (a, b, c)] == ["done", "done", "done"] and t.owner is None


def test_a_cut_drops_the_owner_and_every_waiting_line_and_frees_the_track(hooks):
    t = calls.PipVoiceTrack()
    a, b = t.line(), t.line()
    a.say(tone(1, 0.1))
    b.say(tone(2, 0.1))
    assert t.cut() == pytest.approx(0.2)    # the waiting line's buffer counts as dropped
    assert a.was_cut and b.was_cut and t.owner is None and not t.waiting
    assert a.say(tone(1, 0.1)) is False and b.say(tone(2, 0.1)) is False   # their tails are not said
    a.end()
    b.end()                                 # an end after a cut is a no-op
    assert not t.speaking()
    c = t.line()                            # a line that arrives after the cut owns the track at once
    c.say(tone(9, 0.02))
    assert c.state == "owning" and _played(t) == [9]


def test_a_line_cut_cuts_the_track_only_while_it_is_live(hooks):
    t = calls.PipVoiceTrack()
    a = t.line()
    t.cut()                                 # a barge-in
    b = t.line()
    b.say(tone(5, 0.1))
    assert a.cut() is None                  # a was already cut: b is not a's to cut
    assert b.state == "owning" and _played(t) == [5]


def test_on_over_runs_once_when_the_line_is_on_the_track_or_cut(hooks):
    t = calls.PipVoiceTrack()
    a, b, c = t.line(), t.line(), t.line()
    over = []
    b.end(lambda: over.append("b"))
    assert over == []                       # b waits behind a
    a.end(lambda: over.append("a"))
    assert over == ["a", "b"]
    c.say(tone(1, 0.1))
    t.cut()
    c.end(lambda: over.append(("c", c.was_cut)))
    assert over == ["a", "b", ("c", True)]


def test_a_waiting_line_is_talking_and_the_bed_ends_only_when_audio_reaches_the_track():
    t = calls.PipVoiceTrack()
    a = t.line()
    t.hold(tone(8, 0.02))
    b = t.line()
    b.say(tone(2, 0.02))
    assert t.said_n == 1                    # firstcall's talking() sees the waiting line
    assert t.bed is not None                # the bed plays on while nothing is queued
    a.say(tone(1, 0.02))
    assert t.bed is None                    # the first real append ends it, as before
    a.end()
    assert _played(t) == [1, 2]


def test_a_sound_is_mixed_at_once_over_an_owned_line():
    t = calls.PipVoiceTrack()
    a, b = t.line(), t.line()
    a.say(np.full(calls.OUT_SAMPLES, 1000, dtype=np.int16))
    b.say(np.full(calls.OUT_SAMPLES, 2000, dtype=np.int16))
    t.mix(np.full(calls.OUT_SAMPLES, 5, dtype=np.int16))       # never waits for a line
    assert (t._next_samples() == 1005).all()


def test_on_queue_says_when_a_waiting_lines_audio_will_really_play():
    t = calls.PipVoiceTrack()
    a, b = t.line(), t.line()
    got = []
    a.say(tone(1, 1.0), lambda at, d: got.append(("a", at, d)))
    b.say(tone(2, 0.5), lambda at, d: got.append(("b", at, d)))
    assert [g[0] for g in got] == ["a"]     # b's audio is not on the track yet
    a.end()
    (_, at_a, d_a), (_, at_b, d_b) = got
    assert d_a == 1.0 and d_b == 0.5
    assert at_b - at_a == pytest.approx(1.0, abs=0.05)   # b starts after a's second


# ---- the two writers on the real track ---------------------------------------

def _speaker(monkeypatch, value_of):
    """Fake TTS: each sentence's samples carry value_of(sentence)."""
    voiced = []

    def tts(text, voice=None):
        voiced.append(text)
        return tone(value_of(text), 0.1)
    monkeypatch.setattr(calls, "tts_pcm48", tts)
    return voiced


VALUES = {"A1.": 11, "A2.": 12, "B1.": 21, "B2.": 22}


async def _serve(s):
    path = pathlib.Path(tempfile.mkdtemp(prefix="ov-")) / "s.sock"
    return await mouthleg.serve(Manager({1: s}), path, lambda m: None), path


async def _open_line(path, text="Morning Bob."):
    r, w = await asyncio.open_unix_connection(str(path))
    w.write((json.dumps({"call": 1, "text": text, "who": "agent"}) + "\n").encode())
    assert json.loads(await r.readline()) == {"ok": True, "call": 1}
    return r, w


def test_a_mouth_line_that_arrives_mid_reply_waits_for_the_whole_reply(monkeypatch, hooks):
    import threading
    mouth_done = threading.Event()

    def tts(text, voice=None):
        if text == "A2.":
            mouth_done.wait(2.0)             # the second sentence renders while the mouth speaks
        return tone(VALUES[text], 0.1)
    monkeypatch.setattr(calls, "tts_pcm48", tts)

    async def go():
        s = _session()
        server, path = await _serve(s)
        try:
            bot = asyncio.ensure_future(s.speak("A1. A2."))
            await _until(lambda: s.voice.said_n == 1, "the reply's first sentence")
            r, w = await _open_line(path)    # the mouth arrives between the reply's sentences
            w.write(frame(b"A", tone(99, 0.1).tobytes()) + frame(b"D"))
            await w.drain()
            end = json.loads(await r.readline())    # D is answered at once, waiting or not
            mouth_done.set()
            await asyncio.wait_for(bot, 2.0)
            w.close()
        finally:
            mouth_done.set()
            server.close()
        return end, _played(s.voice)
    end, runs = asyncio.run(go())
    assert end["ended"] == "done", end
    assert runs == [11, 12, 99], f"the call played {runs}: the mouth line was spliced into the reply"


def test_two_replies_at_once_do_not_interleave_and_play_in_arrival_order(monkeypatch, hooks):
    async def go():
        _speaker(monkeypatch, VALUES.get)
        s = _session()
        first = asyncio.ensure_future(s.speak("A1. A2."))
        second = asyncio.ensure_future(s.speak("B1. B2."))
        await asyncio.wait_for(asyncio.gather(first, second), 2.0)
        return _played(s.voice), s.voice.owner
    runs, owner = asyncio.run(go())
    assert runs == [11, 12, 21, 22] and owner is None


def test_a_barge_in_mid_mouth_line_drops_both_speakers_and_the_mouths_later_frames(monkeypatch, hooks):
    async def go():
        voiced = _speaker(monkeypatch, VALUES.get)
        s = _session()
        s.echo = Clips()
        server, path = await _serve(s)
        try:
            r, w = await _open_line(path)
            w.write(frame(b"A", tone(101, 0.2).tobytes()))
            await w.drain()
            await _until(lambda: s.voice.said_n == 1, "the mouth's first frame")
            bot = asyncio.ensure_future(s.speak("B1. B2."))   # waits behind the mouth
            await _until(lambda: len(voiced) == 2, "the reply rendered")
            dropped = s.voice.cut()                            # he talks over us (calls.py barge-in)
            w.write(frame(b"A", tone(102, 0.2).tobytes()) + frame(b"D"))
            await w.drain()
            end = json.loads(await r.readline())
            await asyncio.wait_for(bot, 2.0)
            w.close()
        finally:
            server.close()
        return end, dropped, s
    end, dropped, s = asyncio.run(go())
    assert dropped == pytest.approx(0.4)          # the mouth's frame and the waiting reply
    assert not s.voice.speaking() and s.voice.owner is None and not s.voice.waiting
    assert end["ended"] == "cut" and not end["ok"]          # the mouth's D after the cut
    assert end["dropped_s"] == pytest.approx(0.2)           # its later frame was not said
    assert s.echo.cuts == 0                                 # the barge-in already cut; the mouth cuts no more
    # the mouth line opened first, so it is filed first, before the reply that waited behind it
    assert [r[2] for r in s.lines if r[1] == "Agent"] == ["Morning Bob. [cut]"], s.lines
    assert [r[0] for r in s.lines] == sorted(r[0] for r in s.lines), s.lines


@pytest.mark.parametrize("ending", [b"D", b""], ids=["D", "EOF"])     # its D, or it dies (EOF)
def test_a_mouth_line_cut_by_a_barge_in_tells_the_echo_guard_nothing_after_the_cut(ending, hooks):
    async def go():
        s = _session()
        s.echo = Clips()
        server, path = await _serve(s)
        try:
            r, w = await _open_line(path)
            w.write(frame(b"A", tone(101, 0.2).tobytes()))   # under the echo batch: not told yet
            await w.drain()
            await _until(lambda: s.voice.said_n == 1, "the mouth's first frame")
            s.voice.cut(), s.echo.cut()                         # the barge-in: track and guard
            w.write(frame(b"A", tone(102, 0.2).tobytes()) + (frame(ending) if ending else b""))
            await w.drain()
            if ending:
                end = json.loads(await r.readline())
            else:
                w.close()
                end = None
            await _until(lambda: s.lines, "the mouth line over")
            if ending:
                w.close()
        finally:
            server.close()
        return end, s
    end, s = asyncio.run(go())
    assert s.echo.clips == []          # the cut audio opens no echo window after the cut
    assert s.echo.cuts == 1            # the barge-in's own cut only
    assert s.lines[-1][2] == "Morning Bob. [cut]"
    if end is not None:
        assert end["ended"] == "cut"


class Ordered:
    """An echo guard that keeps what it was told, and in what order."""
    def __init__(self):
        self.events = []

    def spoken(self, text, dur, at=None, source="bridge", start=None):
        self.events.append(("spoken", text, round(dur, 3), source))

    def cut(self, at=None):
        self.events.append(("cut",))
        return "cut"


@pytest.mark.parametrize("ending", [b"C", b""], ids=["C", "EOF"])     # its own C, or it dies (EOF)
def test_a_mouth_line_that_cuts_itself_tells_the_echo_guard_what_played_then_cuts(ending, hooks):
    # The other side of the barge-in case above: nobody cut this line from
    # outside, so the audio that reached the track did play and can echo. The
    # guard hears it first, and the line's own cut then trims it.
    async def go():
        s = _session()
        s.echo = Ordered()
        server, path = await _serve(s)
        try:
            r, w = await _open_line(path)
            w.write(frame(b"A", tone(101, 0.2).tobytes()))   # under the echo batch: not told yet
            await w.drain()
            await _until(lambda: s.voice.said_n == 1, "the mouth's first frame")
            if ending:
                w.write(frame(ending))
                await w.drain()
                end = json.loads(await r.readline())
                w.close()
            else:
                w.close()
                end = None
            await _until(lambda: s.lines, "the mouth line over")
        finally:
            server.close()
        return end, s
    end, s = asyncio.run(go())
    assert s.echo.events == [("spoken", "Morning Bob.", 0.2, "mouth"), ("cut",)]
    assert s.lines[-1][2] == "Morning Bob. [cut]"
    assert s.voice.owner is None and not s.voice.waiting
    if end is not None:
        assert end["ended"] == "cut"


def _gated_tts(monkeypatch, gate_on):
    """Fake TTS whose render of `gate_on` blocks until the test opens the gate."""
    import threading
    gate, rendered = threading.Event(), []

    def tts(text, voice=None):
        rendered.append(text)
        if text == gate_on:
            gate.wait(2.0)
        return tone(VALUES[text], 0.1)
    monkeypatch.setattr(calls, "tts_pcm48", tts)
    return gate, rendered


def _heard(s):
    rows = types.SimpleNamespace(saying=[], said=[])
    s.heard = types.SimpleNamespace(saying=lambda text, **k: rows.saying.append(text),
                                    said=lambda **k: rows.said.append(k))
    return rows


def test_a_barge_in_mid_reply_stops_the_reply_in_flight(monkeypatch, hooks):
    gate, rendered = _gated_tts(monkeypatch, "A2.")

    async def go():
        s = _session()
        rows = _heard(s)
        bot = asyncio.ensure_future(s.speak("A1. A2. B1."))
        await _until(lambda: s.voice.said_n == 1, "the first sentence on the track")
        s.voice.cut()                            # he talks over it while A2 renders
        gate.set()
        await asyncio.wait_for(bot, 2.0)
        await _until(lambda: rows.said, "the said row")
        return s, rows
    try:
        s, rows = asyncio.run(go())
    finally:
        gate.set()
    assert rendered == ["A1.", "A2."]            # B1 is never rendered
    assert not s.voice.speaking() and s.voice.owner is None   # A2 is never said
    assert [x[2] for x in s.lines] == ["A1."]
    assert rows.said[0]["cut"] is True and rows.said[0]["reason"] == "cut"


def test_a_reply_cut_before_its_first_sentence_says_nothing(monkeypatch, hooks):
    gate, rendered = _gated_tts(monkeypatch, "A1.")

    async def go():
        s = _session()
        rows = _heard(s)
        bot = asyncio.ensure_future(s.speak("A1. A2."))
        await _until(lambda: rendered, "the first render")
        s.voice.cut()
        gate.set()
        await asyncio.wait_for(bot, 2.0)
        await asyncio.sleep(0.05)
        return s, rows
    try:
        s, rows = asyncio.run(go())
    finally:
        gate.set()
    assert rendered == ["A1."] and s.voice.said_n == 0 and not s.voice.speaking()
    assert rows.saying == [] and rows.said == []     # nothing reached the call: no said row


def test_a_waiting_reply_cut_by_a_barge_in_writes_no_said_row(monkeypatch, hooks):
    _speaker(monkeypatch, VALUES.get)

    async def go():
        s = _session()
        rows = _heard(s)
        mouth = s.voice.line("Agent")
        mouth.say(tone(101, 0.2))
        await asyncio.wait_for(s.speak("A1."), 2.0)     # rendered, but waits behind the mouth
        assert rows.saying == []
        s.voice.cut()
        await asyncio.sleep(0.05)
        return s, rows
    s, rows = asyncio.run(go())
    assert rows.saying == [] and rows.said == [] and not s.voice.speaking()


@pytest.mark.parametrize("ending", ["D", "C", "eof", "error", "call-ended"])
def test_no_mouth_ending_leaves_the_track_held(ending, monkeypatch, hooks):
    async def go():
        s = _session()
        server, path = await _serve(s)
        try:
            r, w = await _open_line(path)
            w.write(frame(b"A", tone(101, 0.1).tobytes()))
            await w.drain()
            await _until(lambda: s.voice.said_n == 1, "the mouth's frame")
            if ending == "call-ended":
                s.pc = None
            tail = {"D": frame(b"D"), "C": frame(b"C"), "error": frame(b"Z"),
                    "call-ended": frame(b"A", tone(102, 0.1).tobytes()), "eof": b""}[ending]
            w.write(tail)
            await w.drain()
            if ending == "eof":
                w.close()
            await _until(lambda: s.voice.owner is None, "the track to be released")
            if ending != "eof":
                end = json.loads(await r.readline())
                assert end["ended"] == {"D": "done", "C": "cut"}.get(ending, ending)
                w.close()
        finally:
            server.close()
        nxt = s.voice.line("next")
        nxt.say(tone(9, 0.02))
        nxt.end()
        return _played(s.voice), nxt.state
    runs, state = asyncio.run(go())
    assert state == "done"
    assert runs == ([101, 9] if ending == "D" else [9])    # any other end was a cut


def test_a_mouth_that_cuts_while_waiting_cuts_the_whole_track(monkeypatch, hooks):
    async def go():
        s = _session()
        owner = s.voice.line("bot")              # a reply holds the track
        owner.say(tone(7, 0.2))
        server, path = await _serve(s)
        try:
            r, w = await _open_line(path)        # the mouth waits behind it
            w.write(frame(b"A", tone(101, 0.1).tobytes()) + frame(b"C"))
            await w.drain()
            end = json.loads(await r.readline())
            w.close()
        finally:
            server.close()
        return end, owner, s.voice
    end, owner, track = asyncio.run(go())
    assert end["ended"] == "cut" and end["dropped_s"] == pytest.approx(0.3)
    assert owner.was_cut and track.owner is None and not track.speaking()


def test_a_mouth_that_dies_while_waiting_frees_the_track(monkeypatch, hooks):
    async def go():
        s = _session()
        owner = s.voice.line("bot")
        server, path = await _serve(s)
        try:
            _, w = await _open_line(path)
            w.write(frame(b"A", tone(101, 0.1).tobytes()))
            await w.drain()
            w.close()                            # EOF before D, while still waiting
            await _until(lambda: owner.was_cut, "the cut")
        finally:
            server.close()
        nxt = s.voice.line()
        return nxt.state
    assert asyncio.run(go()) == "owning"


def test_a_reply_whose_every_sentence_fails_tts_releases_the_track(monkeypatch, hooks, tmp_path):
    monkeypatch.setenv("DC_OUTBOX", str(tmp_path))

    def refused(text, voice=None):
        raise RuntimeError("tts down")
    monkeypatch.setattr(calls, "tts_pcm48", refused)
    s = _session()
    asyncio.run(s.speak("A1. A2."))
    assert s.voice.owner is None and s.voice.line().state == "owning"


def test_a_reply_cancelled_mid_render_releases_the_track(monkeypatch, hooks):
    import threading
    release = threading.Event()

    def stuck(text, voice=None):
        release.wait(2.0)
        return tone(11, 0.1)
    monkeypatch.setattr(calls, "tts_pcm48", stuck)

    async def go():
        s = _session()
        bot = asyncio.ensure_future(s.speak("A1. A2."))
        await _until(lambda: s.voice.owner is not None, "the reply's place")
        bot.cancel()
        try:
            await bot
        except asyncio.CancelledError:
            pass
        release.set()
        return s.voice
    track = asyncio.run(go())
    assert track.owner is None and track.line().state == "owning"


def test_a_reply_queued_behind_a_mouth_line_tells_the_echo_guard_when_it_really_plays(monkeypatch, hooks):
    async def go():
        _speaker(monkeypatch, VALUES.get)
        s = _session()
        s.echo = Clips()
        mouth = s.voice.line("Agent")
        mouth.say(tone(101, 2.0))                # two seconds of the mouth ahead of the reply
        bot = asyncio.ensure_future(s.speak("A1."))
        await asyncio.sleep(0.05)
        assert s.echo.clips == []                # still waiting: not on the track, not echoing
        t = calls.time.time()
        mouth.end()
        await asyncio.wait_for(bot, 2.0)
        return s.echo.clips, t
    clips, t = asyncio.run(go())
    (text, dur, source, start), = clips
    assert text == "A1." and dur == 0.1 and source == "bridge"
    assert start - t == pytest.approx(2.0, abs=0.1)   # after the mouth's two seconds, not now


def test_echo_guard_starts_a_clip_where_the_caller_says_it_plays():
    from sessionphone.bridge.echo import EchoGuard
    g = EchoGuard(clock=lambda: 100.0, voicemode=None, spool="/nonexistent")
    p = g.spoken("behind the mouth", 1.0, start=103.0)
    assert (p.start, p.end) == (103.0, 104.0)
    assert g.spoken("next", 1.0).start == 104.0       # its own source chains as before
    assert g.spoken("before now", 1.0, source="x", start=50.0).start == 100.0


def test_hang_up_releases_every_line_on_the_track(monkeypatch, hooks):
    class PC:
        async def close(self):
            pass
    s = _session()
    s.pc, s.msg_id, s.answer_go, s.closed, s.wait_task = PC(), 5531, None, None, None
    s.turn_pending, s.turn_parts, s.tasks, s.turn_timer = set(), [], [], None
    s.connected, s.previews, s.awaiting = False, calls.Previews(), ()
    s.manager = types.SimpleNamespace(forget=lambda x: None)
    s._live_clear = lambda: None
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda *a: None)
    mouth, reply = s.voice.line("Agent"), s.voice.line("bot")   # a reply waits behind the mouth
    mouth.say(tone(101, 0.2))
    reply.say(tone(7, 0.2))
    asyncio.run(s.close("hung up"))
    assert mouth.was_cut and reply.was_cut and s.voice.owner is None and not s.voice.waiting
    assert not any("interruption" in m for m in hooks)     # a hang-up is not a barge-in

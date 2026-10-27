"""Floor control: a reply does not start while the caller is speaking.

On a measured call a reply started while the caller was mid-sentence ("oh,
stop"). With barge-in off nothing stopped it, and each reply he talked over
made a new turn that drew another reply ("another one, another one"). A reply
line (a dc-say, a mail into the call, a mouth line) now waits until the caller
has been quiet for the floor's gap, or until the cap, then plays. The bridge's
own lines do not wait.

A fake VAD (a flag) and a fake clock; the real Floor and the real PipVoiceTrack,
driven frame by frame as the call's recv() drives it. TTS is a fake.
"""
import asyncio
import json
import pathlib
import tempfile
import textwrap
import types

import numpy as np
import pytest

from sessionphone.bridge import calls, dc_env, mouthleg
from sessionphone.bridge.floor import Floor

GAP, CAP, FRAME = 0.8, 6.0, 0.02
REPLY, BRIDGE, BEFORE = 5, 7, 3        # each line's samples carry its own value


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Rig:
    """A track with a floor, a fake VAD and a fake clock."""

    def __init__(self, gap=GAP, cap=CAP):
        self.clock, self.vad = Clock(), types.SimpleNamespace(speaking=False)
        self.track = calls.PipVoiceTrack()
        self.track.floor = Floor(lambda: self.vad.speaking, gap, cap, self.clock)

    def frames(self, seconds):
        """Play SECONDS of frames, as recv() would: -> the values heard, in order."""
        heard = []
        for _ in range(round(seconds / FRAME)):
            self.clock.t += FRAME
            for v in self.track._next_samples():
                if v and (not heard or heard[-1] != v):
                    heard.append(int(v))
        return heard

    def reply(self, value=REPLY, seconds=0.1, who="agent", floor=True):
        ln = self.track.line(who, floor=floor)
        ln.say(tone(value, seconds))
        ln.end()
        return ln


def tone(value, seconds):
    return np.full(int(48000 * seconds), value, dtype=np.int16)


@pytest.fixture
def logs(monkeypatch):
    out = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(out.append, lambda p: p, lambda *a, **k: None))
    return out


def floor_lines(logs):
    return [m for m in logs if "FLOOR" in m]


# ---- the clock alone ----------------------------------------------------------

def test_the_floor_is_free_until_the_caller_speaks_and_again_a_gap_after():
    clock, vad = Clock(), types.SimpleNamespace(speaking=False)
    f = Floor(lambda: vad.speaking, GAP, CAP, clock)
    assert f.free(f.tick())                     # never spoken on this call: quiet
    vad.speaking = True
    assert not f.free(f.tick())
    vad.speaking = False
    clock.t += GAP - 0.05
    assert not f.free(f.tick())                 # quiet, but not for the gap yet
    clock.t += 0.06
    assert f.free(f.tick())


def test_the_verdict_is_quiet_cap_or_hold():
    clock, vad = Clock(), types.SimpleNamespace(speaking=True)
    f = Floor(lambda: vad.speaking, GAP, CAP, clock)
    now = f.tick()
    assert f.verdict(None, now) is None
    assert f.verdict(now - CAP + 0.01, now) is None
    assert f.verdict(now - CAP, now) == "cap"
    vad.speaking = False
    clock.t += GAP + 0.01
    assert f.verdict(now, f.tick()) == "quiet"


def test_a_gap_of_zero_turns_it_off():
    f = Floor(lambda: True, 0, CAP, Clock())
    assert not f.on and f.free(f.tick())
    assert "off" in f.line()
    assert Floor(lambda: False).line() == (
        "floor control: a reply waits for 0.8s of caller quiet, 6s at most")


# ---- the track: a reply waits for the caller ----------------------------------

def test_a_reply_is_held_while_the_caller_speaks(logs):
    r = Rig()
    r.vad.speaking = True
    ln = r.reply()
    assert ln.state == "waiting" and r.track.owner is None
    assert r.frames(3.0) == []                  # three seconds of him talking: not a sample of it
    assert ln.state == "waiting" and floor_lines(logs) == []


def test_a_held_reply_starts_a_gap_after_the_caller_goes_quiet(logs):
    r = Rig()
    r.vad.speaking = True
    ln = r.reply()
    r.frames(1.0)
    r.vad.speaking = False
    assert r.frames(GAP - 0.1) == []            # quiet, but not long enough
    assert r.frames(0.2) == [REPLY]             # the gap is up: it plays
    assert ln.state == "done"
    (line,) = floor_lines(logs)
    assert line == "CALL FLOOR agent line held 1.8s, released by quiet", line


def test_a_held_reply_starts_at_the_cap_however_long_the_caller_goes_on(logs):
    r = Rig()
    r.vad.speaking = True                       # VAD noise that never stops
    r.reply()
    assert r.frames(CAP - 0.1) == []
    assert r.frames(0.2) == [REPLY]             # the agent is never silenced for good
    (line,) = floor_lines(logs)
    assert line.startswith("CALL FLOOR agent line held 6.0s, released by cap"), line


def test_a_reply_is_not_held_when_the_caller_is_silent(logs):
    r = Rig()
    r.frames(1.0)                               # the call is up, nobody talking
    ln = r.reply()
    assert ln.state == "done"                   # on the track at once, as before
    assert r.frames(0.2) == [REPLY]
    assert floor_lines(logs) == []              # nothing held, nothing logged


def test_a_reply_waits_out_the_gap_when_the_caller_has_only_just_stopped(logs):
    r = Rig()
    r.vad.speaking = True
    r.frames(1.0)
    r.vad.speaking = False
    r.frames(0.3)                               # he stopped 0.3 s ago: mid-thought, maybe
    ln = r.reply()
    assert ln.state == "waiting"
    assert r.frames(GAP - 0.3 - 0.05) == []
    assert r.frames(0.2) == [REPLY]
    assert floor_lines(logs) == ["CALL FLOOR agent line held 0.5s, released by quiet"]


def test_the_bridges_own_lines_do_not_wait(logs):
    r = Rig()
    r.vad.speaking = True
    ln = r.reply(BRIDGE, who="bridge", floor=False)    # the greeting, the holding line
    assert ln.state == "done"
    assert r.frames(0.2) == [BRIDGE]
    assert floor_lines(logs) == []


def test_a_reply_queued_behind_a_playing_line_does_not_start_while_he_talks_over_it(logs):
    """The measured cascade: he talks over reply A, and reply B, already queued
    behind A, must not start the moment A ends."""
    r = Rig()
    a = r.reply(BEFORE, seconds=1.0)            # A starts at once: he is quiet
    b = r.reply(REPLY)                          # B arrives while A plays
    assert (a.state, b.state) == ("done", "waiting")
    assert r.frames(0.5) == [BEFORE]
    r.vad.speaking = True                       # he talks over A
    assert r.frames(1.0) == [BEFORE]            # A ends under him; B does not start
    assert b.state == "waiting"
    r.vad.speaking = False
    assert r.frames(GAP - 0.1) == []
    assert r.frames(0.2) == [REPLY]
    # held from A's end, not from B's arrival: B never waited on A's audio
    (line,) = floor_lines(logs)
    assert line.endswith("released by quiet")
    held = float(line.split(" held ")[1].split("s,")[0])
    assert 1.2 <= held <= 1.4, line


def test_lines_behind_a_held_reply_keep_their_order(logs):
    r = Rig()
    r.vad.speaking = True
    r.reply(REPLY)
    after = r.reply(BRIDGE, who="bridge", floor=False)
    assert after.state == "waiting"             # it arrived second: it plays second
    r.vad.speaking = False
    assert r.frames(GAP + 0.4) == [REPLY, BRIDGE]


def test_floor_control_off_starts_a_reply_while_he_speaks(logs):
    r = Rig(gap=0)
    r.vad.speaking = True
    assert r.reply().state == "done"
    assert r.frames(0.2) == [REPLY] and floor_lines(logs) == []


def test_a_track_with_no_floor_is_unchanged():
    t = calls.PipVoiceTrack()                   # a web call: no floor, no gate
    ln = t.line("agent", floor=True)
    assert ln.state == "owning"


# ---- the call's opening goes first (PHONE-266) -----------------------------------

def test_opening_lines_go_ahead_of_a_waiting_reply_in_their_own_order(logs):
    r = Rig()
    r.track.opening = True                      # as CallSession.__init__ sets it
    reply = r.reply(REPLY)                      # arrived first, while the call connected
    assert reply.state == "waiting"             # the opening holds it, floor free or not
    notice = r.track.line("bridge", opening=True)
    greet = r.track.line("bridge", opening=True)
    assert r.track.owner is notice and list(r.track.waiting) == [greet, reply]
    notice.say(tone(BEFORE, 0.1))
    notice.end()
    greet.say(tone(BRIDGE, 0.1))
    greet.end()
    r.track.end_opening()
    assert r.frames(1.0) == [BEFORE, BRIDGE, REPLY]
    assert floor_lines(logs) == []              # waiting for the opening is not a floor hold


def test_end_opening_with_nothing_waiting_changes_nothing_else(logs):
    r = Rig()
    r.track.opening = True
    r.track.end_opening()
    r.track.end_opening()                       # again: a no-op
    assert r.track.opening is False and r.track.owner is None and not r.track.waiting
    assert r.reply().state == "done"            # every rule is as it was


def test_a_bare_track_has_no_opening():
    assert calls.PipVoiceTrack().opening is False


def test_a_call_session_opens_its_track_with_the_opening_up(monkeypatch):
    monkeypatch.setattr(calls, "ISSUED", calls.IssuedRoots())
    s = calls.CallSession(types.SimpleNamespace(sessions={}), None, 12)
    assert s.voice.opening is True              # up before _build_pc makes the call live


def test_a_barge_in_still_drops_a_held_reply(logs):
    r = Rig()
    r.vad.speaking = True
    ln = r.reply()
    r.track.cut()
    assert ln.state == "cut" and not r.track.waiting
    r.vad.speaking = False
    assert r.frames(GAP + 0.2) == []


# ---- the reply paths open floored lines ---------------------------------------

def _speaking_session(clock):
    s = object.__new__(calls.CallSession)
    s.chat_id, s.pc, s.lines = 12, object(), []
    s.voice = calls.PipVoiceTrack()
    s.echo = types.SimpleNamespace(spoken=lambda *a, **k: None, cut=lambda *a, **k: "cut")
    s.heard = types.SimpleNamespace(saying=lambda *a, **k: None, said=lambda *a, **k: None)
    s.speaking = True
    s.voice.floor = Floor(lambda: s.speaking, GAP, CAP, clock)
    return s


def test_a_call_session_gives_its_track_a_floor_on_the_vads_speaking(monkeypatch):
    monkeypatch.setattr(calls, "ISSUED", calls.IssuedRoots())
    s = calls.CallSession(types.SimpleNamespace(sessions={}), None, 12)
    f = s.voice.floor
    assert (f.gap, f.cap) == (calls.FLOOR_GAP, calls.FLOOR_CAP)
    assert f.free(f.tick())
    s.speaking = True                           # what _feed and the ears leg set
    assert not f.free(f.tick())


def test_speak_holds_a_reply_and_never_the_bridges_own_line(monkeypatch, logs):
    monkeypatch.setattr(calls, "tts_pcm48", lambda text, voice=None: tone(REPLY, 0.1))

    async def go():
        s = _speaking_session(Clock())
        reply = await s.speak("Here is the answer.", floor=True)
        held = [ln.state for ln in s.voice.waiting]
        s.voice.drop()
        own = await s.speak("I heard you, give me a moment.", who="bridge")
        return reply, held, s.voice.owner, s.voice.queued_s()
    reply, held, owner, queued = asyncio.run(go())
    assert reply["n"] == 1 and not reply["cut"]  # speak() returns: it never waits on the floor
    assert held == ["waiting"]
    assert owner is None and queued > 0         # the holding line went straight on


def test_dc_say_into_a_live_call_is_a_floored_reply(monkeypatch):
    got = []

    class S:
        chat_id, pc = 12, object()

        async def speak(self, text, voice=None, who=None, fallback=True, floor=False):
            got.append(floor)

    loop = asyncio.new_event_loop()
    try:
        m = object.__new__(calls.CallManager)
        m.loop, m.sessions = loop, {1: S()}
        monkeypatch.setattr(calls, "reply_seen", lambda *a: None)
        (fut,) = m.speak_if_live(12, "Here is the answer.")
        loop.run_until_complete(asyncio.wrap_future(fut, loop=loop))
    finally:
        loop.close()
    assert got == [True]


def test_a_mail_into_a_live_call_is_a_floored_reply(tmp_path, monkeypatch):
    root = "call-12-1789@example.org"
    box = tmp_path / "box"
    (box / "new").mkdir(parents=True)
    monkeypatch.setattr(calls, "BOT_BOX", box)
    monkeypatch.setattr(calls, "VOICE_MAP_FILE", tmp_path / "voices.json")
    monkeypatch.setattr(calls, "GUESTS_FILE", tmp_path / "call-guests.txt")
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, None, None))
    monkeypatch.setattr(calls, "reply_seen", lambda *a: None)
    monkeypatch.setattr(calls, "post_to_chat", lambda *a: None)
    floors = []

    def run(coro, loop):
        floors.append(coro.cr_frame.f_locals.get("floor"))
        coro.close()
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", run)

    class Session:
        pc, chat_id, root_id, account = object(), 12, root, None

        async def speak(self, text, voice, who, floor=False):
            return None

    f = box / "new" / "m1"
    f.write_text(textwrap.dedent(f"""\
        From: Alice <alice@example.org>
        To: ada@example.org
        Subject: re: call
        In-Reply-To: <{root}>
        MIME-Version: 1.0
        Content-Type: text/plain; charset=utf-8

        Hello the call.
        """))
    calls.MailLeg(types.SimpleNamespace(sessions={1: Session()}, loop=None)).one(f)
    assert floors == [True]


def test_a_mouth_line_is_held_while_the_caller_speaks(monkeypatch, logs):
    clock = Clock()

    async def go():
        s = _speaking_session(clock)
        path = pathlib.Path(tempfile.mkdtemp(prefix="fl-")) / "s.sock"
        server = await mouthleg.serve(types.SimpleNamespace(sessions={1: s}), path, lambda m: None)
        try:
            r, w = await asyncio.open_unix_connection(str(path))
            w.write((json.dumps({"call": 1, "text": "Morning.", "who": "agent"}) + "\n").encode())
            assert json.loads(await r.readline()) == {"ok": True, "call": 1}
            w.write(b"A" + (9600).to_bytes(4, "big") + tone(REPLY, 0.1).tobytes() + b"D\0\0\0\0")
            await w.drain()
            end = json.loads(await r.readline())   # D is answered at once, held or not
            w.close()
        finally:
            server.close()
        return s, end
    s, end = asyncio.run(go())
    assert end["ended"] == "done", end
    (ln,) = s.voice.waiting
    assert ln.floor and ln.state == "waiting" and s.voice.queued_s() == 0
    s.speaking = False
    clock.t += GAP + 0.01
    s.voice.floor_tick()
    assert s.voice.owner is None and s.voice.queued_s() == pytest.approx(0.1)
    assert floor_lines(logs) == ["CALL FLOOR Agent line held 0.8s, released by quiet"]


# ---- the settings -------------------------------------------------------------

def test_the_settings_default_to_a_short_gap_and_a_cap(monkeypatch):
    monkeypatch.delenv("DC_FLOOR_GAP", raising=False)
    monkeypatch.delenv("DC_FLOOR_CAP", raising=False)
    assert dc_env.number("FLOOR_GAP", 0.8) == 0.8
    assert dc_env.number("FLOOR_CAP", 6.0) == 6.0
    assert (dc_env.FLOOR_GAP, dc_env.FLOOR_CAP) == (calls.FLOOR_GAP, calls.FLOOR_CAP)
    monkeypatch.setenv("DC_FLOOR_GAP", "0")
    assert dc_env.number("FLOOR_GAP", 0.8) == 0.0

"""The greeting goes first: it never waits behind a reply already queued (PHONE-266).

On a fresh Mac a dc-say sent while the call was still connecting reached the
call (CallManager.live_for_chat counts a call live once its peer connection
exists) and opened a reply line on the track BEFORE _on_connected opened the
greeting's. The track grants lines in arrival order (PipVoiceTrack._grant), so
the greeting waited behind the whole reply: bot.log said BACKLOG 5.2s.

The greeting is the first thing a caller hears. At connect it goes out first
(after the recording notice when one is on, PHONE-238); a reply that was
already waiting plays after it, under the floor rules, once and only once.

A real CallSession._on_connected and a real speak(), over a real PipVoiceTrack
with a real Floor; the VAD is a flag, the clock is a fake (the floor's and the
module's time.time), TTS is a fake that gives each text its own sample value.
No network, no real ~/.sessionphone, no sleep.
"""
import asyncio
import time as real_time
import types

import numpy as np
import pytest

from sessionphone.bridge import calls
from sessionphone.bridge.floor import Floor

GAP, CAP, FRAME = 0.8, 6.0, 0.02
REPLY_TEXT = "Here is the chat reply you sent before the call."
NOTICE, GREET, REPLY = 3, 7, 5          # each line's samples carry its own value
REPLY_S, LINE_S = 1.0, 0.4              # the reply is long, as the measured one was


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeTime:
    """calls.time with time() on the fake clock; the rest is the real module."""

    def __init__(self, clock):
        self._clock = clock

    def time(self):
        return self._clock()

    def __getattr__(self, name):
        return getattr(real_time, name)


class Rig:
    """One connecting CallSession: its track has a floor, the network is faked."""

    def __init__(self, monkeypatch, *, notice=False, outgoing=False, voicemail=False, gap=GAP):
        self.clock, self.vad = Clock(), types.SimpleNamespace(speaking=False)
        self.logged, self.spoken, self.fail = [], [], set()
        self.values = {calls.recording.SPOKEN_NOTICE: NOTICE, REPLY_TEXT: REPLY}
        monkeypatch.setattr(calls, "time", FakeTime(self.clock))
        monkeypatch.setattr(calls, "log", self.logged.append)
        monkeypatch.setattr(calls, "_line_log", self.logged.append)
        monkeypatch.setattr(calls, "set_call_id", lambda _id: None)
        monkeypatch.setattr(calls, "FIRST_CALL_WAIT", False)
        monkeypatch.setattr(calls, "_hooks", types.SimpleNamespace(mail_in=lambda *a, **k: None))
        monkeypatch.setattr(calls, "tts_pcm48", self.tts)
        monkeypatch.setattr(calls, "rec_switch", lambda: types.SimpleNamespace(
            spoken_notice=lambda: types.SimpleNamespace(on=True)))

        s = object.__new__(calls.CallSession)
        s.chat_id, s.msg_id, s.pc = 12, 7, object()        # a pc: live_for_chat counts it
        s.chat_name, s.caller, s.root_id = "Bob Team", "Bob Team", "call-12-7@test"
        s.outgoing, s.voicemail = outgoing, voicemail
        s.greeting, s.greet_voice, s.wait_task = None, None, None
        s.lines, s.rec_spoken = [], False
        s.voice = calls.PipVoiceTrack()
        s.voice.floor = Floor(lambda: self.vad.speaking, gap, CAP, self.clock)
        s.voice.opening = True                              # as CallSession.__init__ sets it
        self.echo = []
        s.echo = types.SimpleNamespace(
            spoken=lambda text, dur, start=None: self.echo.append((start, text)),
            cut=lambda *a, **k: "cut")
        s.heard = types.SimpleNamespace(chat_name=None, event=lambda *a, **k: None,
                                        saying=lambda *a, **k: None, said=lambda *a, **k: None)
        s._live_write = lambda: None
        s._rec_start = lambda: types.SimpleNamespace(keep=notice)
        s._rec_notice_post = lambda: None
        s.guest_list = list
        self.s = s

    def tts(self, text, voice=None):
        """Every sentence of the greeting is GREET; the notice and the reply have their own."""
        if text in self.fail:
            raise RuntimeError("the speech server is down")
        self.spoken.append(text)
        secs = REPLY_S if text in calls.sentences(REPLY_TEXT) else LINE_S
        return np.full(int(48000 * secs), self.values.get(text, GREET), dtype=np.int16)

    def connect(self, reply_first=True, reply_after_connect=False, floor=True):
        """A dc-say reaches the connecting call (as speak_if_live does), then the
        call connects: -> speak()'s result for the reply. floor=False: the reply
        is one of the bridge's own lines (an invite notice) instead."""
        async def go():
            loop = asyncio.get_running_loop()
            self.s.manager = types.SimpleNamespace(loop=loop, sessions={})
            out = None
            if reply_first:
                out = await self.s.speak(REPLY_TEXT, None, "Owner", floor=floor)
            await self.s._on_connected()
            if reply_after_connect:
                out = await self.s.speak(REPLY_TEXT, None, "Owner", floor=True)
            return out
        return asyncio.run(go())

    def frames(self, seconds):
        """Play SECONDS of frames, as recv() would: -> (value, start s, length s) runs."""
        runs, t = [], 0.0
        for _ in range(round(seconds / FRAME)):
            self.clock.t += FRAME
            for v in self.s.voice._next_samples():
                v = int(v)
                if v and runs and runs[-1][0] == v and runs[-1][1] + runs[-1][2] >= t - 1e-9:
                    runs[-1][2] += 1 / 48000
                elif v:
                    runs.append([v, t, 1 / 48000])
                t += 1 / 48000
        return [(v, round(a, 2), round(n, 2)) for v, a, n in runs]


def order(runs):
    return [v for v, _a, _n in runs]


def start_of(runs, value):
    return next(a for v, a, _n in runs if v == value)


# ---- the measured bug: a reply queued at connect -------------------------------------

def test_a_reply_queued_at_connect_plays_after_the_greeting(monkeypatch):
    r = Rig(monkeypatch)
    out = r.connect()
    assert out["n"] == 1 and not out["cut"]
    runs = r.frames(10.0)
    assert order(runs) == [GREET, REPLY]                 # the greeting first, then the reply
    assert start_of(runs, GREET) == 0.0                  # at connect: nothing plays before it
    reply = [run for run in runs if run[0] == REPLY]
    assert len(reply) == 1 and reply[0][2] == REPLY_S    # once, whole
    assert r.spoken.count(REPLY_TEXT) == 1               # rendered once, never re-queued


def test_the_echo_guard_hears_the_greeting_start_first(monkeypatch):
    r = Rig(monkeypatch)
    r.connect()
    r.frames(10.0)
    starts = {text: start for start, text in r.echo}
    first_greet = min(start for start, text in r.echo if text != REPLY_TEXT)
    last_greet = max(start for start, text in r.echo if text != REPLY_TEXT)
    assert first_greet < starts[REPLY_TEXT]
    assert starts[REPLY_TEXT] >= last_greet + LINE_S - 1e-6   # after the greeting ENDS
    assert [t for _s, t in r.echo].count(REPLY_TEXT) == 1


def test_with_the_notice_on_it_is_notice_then_greeting_then_reply(monkeypatch):
    r = Rig(monkeypatch, notice=True)
    r.connect()
    runs = r.frames(10.0)
    assert order(runs) == [NOTICE, GREET, REPLY]         # PHONE-238's order, the reply behind both
    assert start_of(runs, NOTICE) == 0.0
    assert start_of(runs, GREET) == LINE_S               # straight after the one-line notice
    assert [v for v, _a, _n in runs].count(REPLY) == 1


def test_an_outgoing_hello_goes_before_a_queued_reply(monkeypatch):
    r = Rig(monkeypatch, outgoing=True)
    r.connect()
    runs = r.frames(10.0)
    assert order(runs) == [GREET, REPLY]
    assert start_of(runs, GREET) == 0.0
    hello = (f"Hi Bob Team, {calls.dc_env.GREETING_NAME} here." if calls.PROFILE
             else calls.GREETING_OUT)                    # as _on_connected words it
    assert set(calls.sentences(hello)) <= set(r.spoken)


def test_the_voicemail_greeting_goes_before_a_queued_reply(monkeypatch):
    r = Rig(monkeypatch, voicemail=True)
    r.connect()
    runs = r.frames(10.0)
    assert order(runs) == [GREET, REPLY]
    assert start_of(runs, GREET) == 0.0
    assert set(calls.sentences(calls.GREETING_VOICEMAIL)) <= set(r.spoken)


def test_a_reply_held_by_the_floor_at_connect_does_not_hold_the_greeting(monkeypatch):
    r = Rig(monkeypatch)
    r.vad.speaking = True                                # he is talking as it connects
    r.connect()
    assert r.s.voice.waiting and r.s.voice.waiting[0].floor   # the reply: still held
    runs = r.frames(0.4)
    assert order(runs) == [GREET]                        # the bridge's own line does not wait
    r.vad.speaking = False
    runs = r.frames(10.0)
    assert order(runs)[-1] == REPLY and order(runs).count(REPLY) == 1


# ---- unchanged: no reply queued, or a reply that comes after the greeting ------------

def test_with_no_reply_queued_the_greeting_goes_at_connect(monkeypatch):
    r = Rig(monkeypatch)
    r.connect(reply_first=False)
    runs = r.frames(5.0)
    assert order(runs) == [GREET]
    assert start_of(runs, GREET) == 0.0
    assert r.spoken == list(calls.sentences(calls.GREETING))


def test_with_no_reply_queued_the_notice_still_goes_first(monkeypatch):
    r = Rig(monkeypatch, notice=True)
    r.connect(reply_first=False)
    runs = r.frames(5.0)
    assert order(runs) == [NOTICE, GREET]
    assert start_of(runs, NOTICE) == 0.0


def test_a_reply_after_connect_still_waits_for_the_greeting(monkeypatch):
    r = Rig(monkeypatch)
    r.connect(reply_first=False, reply_after_connect=True)
    runs = r.frames(10.0)
    assert order(runs) == [GREET, REPLY]
    assert start_of(runs, GREET) == 0.0
    assert order(runs).count(REPLY) == 1


# ---- the floor and the gate, after the greeting --------------------------------------

def greeting_s():
    return len(calls.sentences(calls.GREETING)) * LINE_S


def held_s(logged):
    (line,) = [m for m in logged if m.startswith("FLOOR")]
    return float(line.split(" held ")[1].split("s,")[0]), line


def test_the_floor_still_holds_the_reply_after_the_greeting(monkeypatch):
    r = Rig(monkeypatch)
    r.vad.speaking = True                                # he talks over the greeting
    r.connect()
    runs = r.frames(greeting_s() + 1.0)                  # ... and for a second after it
    assert order(runs) == [GREET]
    r.vad.speaking = False
    runs = r.frames(5.0)
    assert order(runs) == [REPLY]
    assert abs(start_of(runs, REPLY) - GAP) <= 2 * FRAME  # a gap after his last word
    held, line = held_s(r.logged)
    assert abs(held - (1.0 + GAP)) <= 2 * FRAME, line    # counted from the greeting's end
    assert line.endswith("released by quiet")


def test_the_cap_counts_from_the_end_of_the_greeting(monkeypatch):
    r = Rig(monkeypatch)
    r.vad.speaking = True                                # he never stops
    r.connect()
    runs = r.frames(greeting_s() + CAP + 2.0)
    assert order(runs) == [GREET, REPLY]
    assert abs(start_of(runs, REPLY) - (greeting_s() + CAP)) <= 2 * FRAME
    held, line = held_s(r.logged)
    assert abs(held - CAP) <= 2 * FRAME and line.endswith("released by cap"), line


def test_with_floor_control_off_the_reply_still_waits_for_the_greeting(monkeypatch):
    r = Rig(monkeypatch, gap=0)
    r.vad.speaking = True
    r.connect()
    runs = r.frames(10.0)
    assert order(runs) == [GREET, REPLY]
    assert start_of(runs, GREET) == 0.0
    assert order(runs).count(REPLY) == 1


def test_a_bridge_line_from_the_connecting_window_plays_after_the_greeting(monkeypatch):
    r = Rig(monkeypatch)
    r.connect(floor=False)                               # as invite's speak(), no floor
    runs = r.frames(10.0)
    assert order(runs) == [GREET, REPLY]
    assert start_of(runs, GREET) == 0.0


def test_a_mouth_line_opened_before_connect_reaches_the_track_after_the_greeting(monkeypatch):
    r = Rig(monkeypatch)
    queued = []

    async def go():
        r.s.manager = types.SimpleNamespace(loop=asyncio.get_running_loop(), sessions={})
        ln = r.s.voice.line("agent", floor=True)         # as mouthleg's voice.line(who, floor=True)
        ln.say(np.full(int(48000 * LINE_S), REPLY, dtype=np.int16),
               lambda start_at, dur: queued.append(start_at))
        await r.s._on_connected()
        return ln
    ln = asyncio.run(go())
    assert queued == [] and ln.state == "waiting"        # not on the track: the echo is not told
    connected_at = r.clock.t
    runs = r.frames(10.0)
    assert order(runs) == [GREET, REPLY]
    assert len(queued) == 1 and queued[0] >= connected_at + greeting_s() - 1e-6
    ln.end()


def test_a_greeting_tts_cannot_say_still_ends_the_opening(monkeypatch, tmp_path):
    monkeypatch.setenv("DC_OUTBOX", str(tmp_path / "outbox"))   # the fallback's chat text
    r = Rig(monkeypatch)
    r.fail = set(calls.sentences(calls.GREETING))
    r.connect()
    assert r.s.voice.opening is False
    runs = r.frames(5.0)
    assert order(runs) == [REPLY]
    assert r.spoken.count(REPLY_TEXT) == 1


def test_a_connect_that_raises_still_ends_the_opening(monkeypatch):
    r = Rig(monkeypatch)

    def boom():
        raise RuntimeError("the chat line could not be posted")
    r.s._rec_notice_post = boom
    with pytest.raises(RuntimeError):
        r.connect()
    assert r.s.voice.opening is False
    runs = r.frames(5.0)
    assert order(runs) == [REPLY]                        # the reply is not stuck behind it


def test_a_cut_during_the_greeting_drops_the_waiting_reply(monkeypatch):
    r = Rig(monkeypatch)
    r.connect()
    assert order(r.frames(0.1)) == [GREET]               # the greeting is playing
    (reply,) = r.s.voice.waiting
    r.s.voice.drop()                                     # a barge-in: a cut drops them all
    assert reply.was_cut
    assert REPLY not in order(r.frames(5.0))             # never played late
    assert r.spoken.count(REPLY_TEXT) == 1


# ---- the first call's wait reads said_n -----------------------------------------------

def test_a_reply_the_opening_holds_does_not_end_the_first_call_wait():
    t = calls.PipVoiceTrack()
    t.opening = True
    ln = t.line("Owner", floor=True)
    ln.say(np.full(480, REPLY, dtype=np.int16))
    assert ln.state == "waiting" and t.said_n == 0       # the greeting has not been said
    t.end_opening()
    assert ln.state == "owning" and t.said_n == 1        # it counts once it is on the track

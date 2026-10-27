"""The call transcript credits the bot's own lines to the bot (PHONE-267).

Seen on a fresh install: a bot set up with `setup --name Ada` greeted the
caller as "Ada here. Go ahead.", and the call's transcript gave both
sentences to the agent's name, "Claude":

    09:00:06 Claude: Ada here.
    09:00:07 Claude: Go ahead.

and printed one line out of time order:

    09:00:29 Carol: Is this Ada? Is this who I'm talking to?
    09:00:23 Claude: Yes Carol, this is Ada, ...

(a) The label. A line the bridge speaks with no `who` is the bot's own
speech (the greeting, the ring-out, the notices). CallSession.speak labelled
it dc_env.AGENT_NAME, the agent's name capitalised ("Claude"), while the
words said the name `setup --name` gave. It now falls back to
dc_env.GREETING_NAME, the bot's name (PHONE-231). The mouth leg is the
AGENT's voice: a mouth line with no `who` keeps dc_env.AGENT_NAME.

(b) The order. The mouth leg stamps its line with the time its connection
OPENED, and files it when the line ENDS. A caller turn that lands while the
line plays is already in the transcript, with a later stamp. The mouth line
is now filed at its stamp's place, so the lines, and the printed
transcript, run in time order. The bot log showed the sequence: the
mouth line opened at 09:00:23 and was done at 09:00:35, and the caller's
turn came at 09:00:29, between them.

The label and order tests failed before the fix. The premises, and the
tests that pin what the fix leaves alone (a mouth line with no who, a who
given, a mouth line filed last, a tie), passed before it and still do.
Fakes only: no relay, no speech server.
"""
import asyncio
import json
import pathlib
import re
import struct
import tempfile
import time
import types

import numpy as np
import pytest
from test_greeting_name import NAME, started, unset  # noqa: F401  (fixtures)
from test_recording_notice import call  # noqa: F401  (a fixture)
from test_recording_switch import rig  # noqa: F401  (a fixture)

from sessionphone.bridge import calls, dc_env, mouthleg


def labels(s):
    """-> {text: who} for every transcript line."""
    return {text: who for _t, who, text in s.lines}


# ---- (a) the label: the greeting -------------------------------------------------------------


def test_premise_the_greeting_says_the_bots_name(started, call):  # noqa: F811
    """PHONE-231 holds: the words say Ada. The premise."""
    s = call.connect()
    assert f"{NAME} here." in [t for _t, _w, t in s.lines], s.lines
    assert dc_env.AGENT_NAME == "Claude" and dc_env.GREETING_NAME == NAME


def test_an_incoming_greeting_is_labelled_with_the_bots_name(started, call):  # noqa: F811
    s = call.connect()
    got = labels(s)
    assert got[f"{NAME} here."] == NAME, s.lines
    assert got["Go ahead."] == NAME, s.lines


def test_a_ring_out_greeting_is_labelled_with_the_bots_name(started, call):  # noqa: F811
    s = call.connect(outgoing=True)
    whos = {w for _t, w, _x in s.lines}
    assert whos == {NAME}, s.lines


def test_the_transcript_text_never_credits_the_greeting_to_claude(started, call):  # noqa: F811
    s = call.connect()
    s.started = time.time()
    _head, body = calls.CallSession.transcript_text(s)
    assert "Claude:" not in body, body
    assert f"{NAME}: {NAME} here." in body, body


# ---- (a) the label: a mouth line with no who --------------------------------------------------


class _Line:
    def __init__(self, voice):
        self.voice, self.was_cut = voice, False

    def say(self, pcm, on_queue=None):
        self.voice.say(pcm)
        if on_queue is not None:
            on_queue(0.0, len(pcm) / 48000)
        return True

    def end(self, on_over=None):
        if on_over is not None:
            on_over()

    def cut(self):
        return 0.0


class _Voice:
    def __init__(self, on_say=None):
        self.q, self.on_say = [], on_say

    def line(self, who="", floor=False):
        return _Line(self)

    def say(self, pcm):
        self.q.append(pcm)
        if self.on_say is not None:
            self.on_say()


class _Echo:
    def spoken(self, *a, **k):
        pass

    def cut(self, at=None):
        return "cut"


class _Session:
    def __init__(self, on_say=None):
        self.pc = object()
        self.voice, self.echo, self.lines = _Voice(on_say), _Echo(), []
        self.caller, self.started = "Carol", time.time()


def _frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


def _pcm(seconds):
    return (np.ones(int(48000 * seconds), dtype="<i2") * 100).tobytes()


def mouth(session, header, frames):
    """One mouth line into call 1 over a real socket. -> the last reply."""
    async def go():
        path = pathlib.Path(tempfile.mkdtemp(prefix="ml-")) / "s.sock"
        server = await mouthleg.serve(types.SimpleNamespace(sessions={1: session}), path, lambda m: None)
        r, w = await asyncio.open_unix_connection(str(path))
        w.write((json.dumps(header) + "\n").encode())
        first = json.loads(await r.readline())
        assert first.get("ok"), first
        for f in frames:
            w.write(f)
        await w.drain()
        last = json.loads(await r.readline())
        server.close()
        return last
    return asyncio.run(go())


@pytest.fixture
def named(monkeypatch):
    """A bot `setup --name Ada` made, agent the default."""
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Claude")
    monkeypatch.setattr(dc_env, "GREETING_NAME", NAME)


def test_premise_a_mouth_line_that_names_its_speaker_keeps_it(named):
    s = _Session()
    mouth(s, {"call": 1, "text": "Morning.", "who": "bob"}, [_frame(b"A", _pcm(0.1)), _frame(b"D")])
    assert s.lines[0][1:] == ("Bob", "Morning."), s.lines


def test_a_mouth_line_with_no_who_is_labelled_with_the_agents_name(named):
    """The mouth is the agent's voice, not the bot's: with no who it is
    still the agent speaking, so it keeps AGENT_NAME."""
    s = _Session()
    mouth(s, {"call": 1, "text": "Go ahead."}, [_frame(b"A", _pcm(0.1)), _frame(b"D")])
    assert s.lines[0][1:] == ("Claude", "Go ahead."), s.lines


# ---- (b) the order ---------------------------------------------------------------------------


def stamps(body):
    """-> the HH:MM:SS stamps of a transcript body, in print order."""
    return re.findall(r"^(\d\d:\d\d:\d\d) ", body, re.MULTILINE)


def test_a_caller_turn_during_a_mouth_line_keeps_the_transcript_in_time_order(named, monkeypatch):
    """The sequence seen: the mouth line opens at T, the caller speaks at
    T+6 while it plays, the line ends at T+12. The clock is faked so the
    stamps differ by whole seconds, as they did in the bot log."""
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(mouthleg.time, "time", lambda: clock["now"])
    s = _Session()

    def caller_speaks_mid_line():         # the second frame reaches the track: he talks over it
        if len(s.voice.q) == 2:
            clock["now"] += 6
            s.lines.append((clock["now"], s.caller, "Is this who I'm talking to?"))
            clock["now"] += 6
    s.voice.on_say = caller_speaks_mid_line
    mouth(s, {"call": 1, "text": "Yes, this is Ada.", "who": "claude"},
          [_frame(b"A", _pcm(0.1)), _frame(b"A", _pcm(0.1)), _frame(b"D")])

    t = [row[0] for row in s.lines]
    assert t == sorted(t), s.lines
    _head, body = calls.CallSession.transcript_text(s)
    assert stamps(body) == sorted(stamps(body)), body


# ---- edges: what the fix must leave alone -----------------------------------------------------


def test_a_bridge_line_that_names_its_speaker_keeps_it(started, call):  # noqa: F811
    """Only a line with no who is the bot's: a who given (a dc-say's, the
    'bridge' notices) is kept as given."""
    s = call.connect()

    async def go():
        s.manager.loop = asyncio.get_running_loop()
        await s.speak("Noted.", None, "bridge")
        await s.speak("On it.", None, "Claude")
    asyncio.run(go())
    got = labels(s)
    assert got["Noted."] == "bridge" and got["On it."] == "Claude", s.lines


def test_a_mouth_line_nothing_was_said_over_is_filed_last(named):
    """No line heard while it played: it lands at the end, as before."""
    s = _Session()
    s.lines.append((time.time() - 5, s.caller, "Hello?"))
    mouth(s, {"call": 1, "text": "Hi Carol.", "who": "claude"}, [_frame(b"A", _pcm(0.1)), _frame(b"D")])
    assert [r[1:] for r in s.lines] == [("Carol", "Hello?"), ("Claude", "Hi Carol.")], s.lines


def test_a_mouth_line_files_after_a_line_with_the_same_stamp(named, monkeypatch):
    """A tie keeps arrival order: the line already in stays first."""
    monkeypatch.setattr(mouthleg.time, "time", lambda: 1_800_000_000.0)
    s = _Session()
    s.lines.append((1_800_000_000.0, s.caller, "Hello?"))
    mouth(s, {"call": 1, "text": "Hi Carol.", "who": "claude"}, [_frame(b"A", _pcm(0.1)), _frame(b"D")])
    assert [r[1] for r in s.lines] == ["Carol", "Claude"], s.lines

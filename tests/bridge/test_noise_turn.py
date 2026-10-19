"""PHONE-158, the bridge leg: CallSession._mail_turn mails no turn with no real word.

Both bridge hearings end in _mail_turn (the turn-join chunker and, with DC_EARS=1,
the ears leg), so the one guard there covers both. A drop takes no TURN number,
is written to the heard log as before, and logs one NOISE line naming the
detector, where it fell among the numbered turns, and the text.
The real CallSession method runs on a session made with object.__new__, as in
test_call_turn_log. No call, no network, no sendmail.
"""
import asyncio
import re
import types

import pytest

from sessionphone.bridge import calls, dc_env

BOX = "agent@example.org"


class _Sink:
    def __init__(self):
        self.turns = []

    def turn(self, text, detector=None, **k):
        self.turns.append((text, detector))


def _session():
    s = object.__new__(calls.CallSession)
    s.chat_id, s.chat_name, s.caller = 12, "Caller", "Caller"
    s.lines, s.preview_id = [], None
    s.root_id = "call-12-1789@example.org"
    s.guest_list = list
    s.heard = _Sink()
    return s


@pytest.fixture
def rig(monkeypatch):
    r = types.SimpleNamespace(logs=[], mails=[])

    def mail_in(*a, **k):
        r.mails.append((a, k))
        return True
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: "", mail_in))
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    monkeypatch.setattr(calls, "_call_id", None)
    r.noise = lambda: [m for m in r.logs if "NOISE" in m]
    r.bodies = lambda: [a[3] for a, _ in r.mails]
    return r


def say(s, *texts, detector=None):
    async def run():
        for t in texts:
            await s._mail_turn(t, detector=detector)
    asyncio.run(run())


@pytest.mark.parametrize("text", [".", "Music.", "Um", "Uh.", "...", "[BLANK_AUDIO]", "(Music)"])
def test_a_turn_with_no_real_word_is_not_mailed(rig, text):
    s = _session()
    say(s, text, detector="turn-join")
    assert rig.mails == []
    assert s.turn_n == 0                                  # no TURN number taken
    assert s.heard.turns == [(text, "turn-join")]         # the heard log keeps it
    assert s.lines == []                                  # nor is it a line of the call
    (line,) = rig.noise()
    assert line.endswith(f"NOISE turn-join after TURN 0: no real word | dropped: {text}")


@pytest.mark.parametrize("text", ["yes", "no", "go", "okay", "Hmm, yes"])
def test_a_one_word_answer_is_still_mailed(rig, text):
    s = _session()
    say(s, text)
    assert rig.bodies() == [f"(on the call) {text}"]
    assert rig.noise() == [] and s.turn_n == 1


def test_the_ears_leg_is_held_to_the_same_rule(rig):
    """_ears_dispatch hands its turns to _mail_turn with an ears-<reason> detector."""
    s = _session()
    say(s, "Music.", detector="ears-silence")
    assert rig.mails == []
    (line,) = rig.noise()
    assert "NOISE ears-silence after TURN 0: no real word | dropped: Music." in line


def test_a_drop_consumes_no_turn_number(rig):
    """The TURN numbers stay those of the turns that went: 1, 2, never 1, 3."""
    s = _session()
    say(s, "first", ".", "Um", "second")
    assert rig.bodies() == ["(on the call) first", "(on the call) second"]
    mailed = [m for m in rig.logs if re.match(r"CALL TURN \d", m)]
    assert [re.match(r"CALL TURN (\d+)", m).group(1) for m in mailed] == ["1", "2"]
    assert [l.split("CALL ", 1)[-1] for l in rig.noise()] == [
        "NOISE turn after TURN 1: no real word | dropped: .",
        "NOISE turn after TURN 1: no real word | dropped: Um",
    ]


def test_an_empty_turn_is_still_silent(rig):
    """The empty guard comes first, as before: nothing mailed, nothing logged."""
    s = _session()
    say(s, "   ")
    assert rig.mails == [] and rig.noise() == [] and s.heard.turns == []

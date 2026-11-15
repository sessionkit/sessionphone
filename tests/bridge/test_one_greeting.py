"""One greeting on a call: the bot's, and the agent does not greet again.

With DC_ANSWER=auto (the default) the bot answers a call at once and greets
the caller (CallSession._open: speak(GREETING, opening=True)). It also mails
the agent CALL CONNECTED, which says how to answer: the mouth, else dc-say.
An agent told only that greets the caller itself, and the caller hears two
greetings on one call: the bot's, then the agent's, a gap apart.

The bot's greeting is the one that stays: it is the only one certain to
exist at pick-up. So CALL CONNECTED tells the agent the caller has already
been greeted, and in which words, and the agent answers what the caller says
instead of greeting.

A real CallSession._on_connected, a real speak() and a real PipVoiceTrack
with a real Floor (the rig of test_greeting_first.py). The agent is a fake
that reads the CALL CONNECTED mail and answers with the mouth, as mouthleg
does (a floor line). No network, no real root, no sleep.
"""
import asyncio
import re
import types

import numpy as np
import pytest
from test_greeting_first import GAP, GREET, LINE_S, Rig, greeting_s, order, start_of

from sessionphone.bridge import calls

AGENT = 9                                  # the agent's own line carries this value
AGENT_GREETING = "Hi, it's your agent here. What can I do for you?"


class Inline:
    """manager.loop: run_in_executor runs the job now, so the mail is in hand."""

    def run_in_executor(self, _executor, fn, *args):
        return fn(*args)


def told_greeted(text):
    """Does CALL CONNECTED tell the agent the caller has already been greeted,
    and in which words?"""
    return bool(re.search(r"\bgreeted\b", text, re.IGNORECASE)) and calls.GREETING in text


def agent_answers(s, text):
    """What an agent does with CALL CONNECTED. The mail names the mouth. Told the
    caller has already been greeted, it says nothing until the caller speaks;
    told nothing of a greeting, it greets the caller itself. -> its line, or None."""
    if told_greeted(text):
        return None
    ln = s.voice.line("Agent", floor=True)          # as mouthleg: voice.line(who, floor=True)
    ln.say(np.full(int(48000 * LINE_S), AGENT, dtype=np.int16))
    ln.end()
    return AGENT_GREETING


def connect(r, monkeypatch):
    """The call connects; the agent reads its CALL CONNECTED and answers.
    -> (the CALL CONNECTED text, what the agent said or None)."""
    mails = []
    monkeypatch.setattr(calls, "_hooks", types.SimpleNamespace(
        mail_in=lambda *a, **k: mails.append(a[3])))

    async def go():
        r.s.manager = types.SimpleNamespace(loop=Inline(), sessions={})
        await r.s._on_connected()
        (text,) = [m for m in mails if m.startswith("CALL CONNECTED")]
        return text, agent_answers(r.s, text)
    return asyncio.run(go())


def test_the_caller_hears_one_greeting_not_the_bots_and_then_the_agents(monkeypatch):
    r = Rig(monkeypatch)
    text, said = connect(r, monkeypatch)
    runs = r.frames(greeting_s() + GAP + 5.0)     # the caller is quiet: a waiting line would play
    assert start_of(runs, GREET) == 0.0           # the bot greets at pick-up
    assert said is None and order(runs) == [GREET], (
        f"two greetings on one call: the caller heard {order(runs)} "
        f"(GREET={GREET} the bot's, AGENT={AGENT} the agent's {said!r}), because "
        f"CALL CONNECTED does not tell the agent the caller was already greeted:\n{text}")


def test_the_call_connected_mail_names_the_greeting_already_said(monkeypatch):
    r = Rig(monkeypatch)
    text, _said = connect(r, monkeypatch)
    assert told_greeted(text), (
        f"CALL CONNECTED must say the caller has been greeted, quoting {calls.GREETING!r}:\n{text}")


# ---- the mail quotes the line the call really opened with ------------------------

def opened_with(r, monkeypatch):
    """Connect: -> (the CALL CONNECTED text, the opening line's sentences as spoken)."""
    text, _said = connect(r, monkeypatch)
    return text, r.spoken


@pytest.mark.parametrize("outgoing, voicemail, profile, chosen, expect", [
    (False, False, "", None, lambda: calls.GREETING),
    (False, True, "", None, lambda: calls.GREETING_VOICEMAIL),
    (True, False, "", None, lambda: calls.GREETING_OUT),
    (True, False, "public", None,
     lambda: f"Hi Bob Team, {calls.dc_env.GREETING_NAME} here."),
    (False, False, "", "Hello, you are through.", lambda: "Hello, you are through."),
    (True, False, "public", "Hello, you are through.", lambda: "Hello, you are through."),
], ids=["incoming", "voicemail", "outgoing", "outgoing-public", "chosen", "chosen-outgoing"])
def test_the_mail_quotes_the_greeting_actually_said(monkeypatch, outgoing, voicemail,
                                                    profile, chosen, expect):
    monkeypatch.setattr(calls, "PROFILE", profile)
    r = Rig(monkeypatch, outgoing=outgoing, voicemail=voicemail)
    r.s.greeting = chosen
    line = expect()
    text, spoken = opened_with(r, monkeypatch)
    assert spoken == calls.sentences(line)            # the call opened with this line
    assert f'They have already been greeted: "{line}" Do not greet them again' in text
    assert text.splitlines()[1].startswith("They have already been greeted:"), (
        "the greeting is told before how to answer, so it is read first")


def test_with_no_greeting_line_the_mail_claims_no_greeting():
    s = object.__new__(calls.CallSession)
    s.chat_id, s.caller, s.msg_id, s.root_id = 12, "Alice", 5531, "call-test@example.org"
    for greeted in (None, ""):
        assert "greeted" not in s._connected_text("12:34", "rang Bob", greeted)

"""SKILL.md tells the agent how to take turns on a call (PHONE-253).

The bot's floor control (bridge/floor.py) holds a reply while the caller is
speaking, but only up to its cap: after that the reply plays even over the
caller. And it still plays every reply it is given, in order. So the agent
side is a rule in the skill: one reply per caller turn, none to a fragment
that asks nothing, and anything more goes as text in the chat, by dc-dm.
The rules name a spoken reply, not `dc-say`: on a call the mouth leads and
dc-say is the fallback, as the call mail's own hint says (dc_env.reply_hint).
These tests hold the skill's "Turn-taking" part to those three rules, to
the reason quoted for them, and to the floor default the code uses.

They also hold the skill's `dc-say --text-only` lines (PHONE-252) to the
landed flag: a chat-only post, never spoken, never with `--call` (PHONE-262).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import _changelog

from sessionphone.bridge import dc_env, floor

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "sessionphone" / "SKILL.md"
# the entry: its fragment while pending, its CHANGELOG.md section once released
FRAGMENT = ("PHONE-253-call-turn-taking.md", "A call reply waits for the caller to finish speaking")
DC_SAY = ROOT / "bin" / "dc-say"
BOT = ROOT / "src" / "sessionphone" / "bridge" / "bot.py"
CHAT_ONLY_ACK = "NOT spoken (chat only)"
QUOTE = "If you've sent something to say, I need to speak next. Turn taking, please."


def _flat(text: str) -> str:
    """Whitespace folded to single spaces, so a wrapped line still matches."""
    return " ".join(text.split())


def _turn_taking() -> str:
    """The "### Turn-taking" part of "## On a call", flattened."""
    text = SKILL.read_text(encoding="utf-8")
    on_call = re.search(r"^## On a call\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    assert on_call, "no '## On a call' section in SKILL.md"
    part = re.search(r"^### Turn-taking\n(.*)", on_call.group(1), re.MULTILINE | re.DOTALL)
    assert part, "no '### Turn-taking' part under '## On a call'"
    return _flat(part.group(1))


def test_the_reason_is_quoted_verbatim():
    assert f'"{QUOTE}"' in _turn_taking()


def test_one_spoken_reply_per_caller_turn():
    text = _turn_taking()
    assert "One spoken reply per caller turn" in text
    assert "a sentence or two" in text
    # dc-say is the mouth's fallback on a call, never THE reply
    assert "One `dc-say` per caller turn" not in text


def test_no_reply_to_a_fragment_that_asks_nothing():
    text = _turn_taking()
    assert "No reply to a fragment that asks nothing" in text
    assert '"No."' in text and '"Okay."' in text


def test_never_a_second_spoken_reply_and_more_goes_as_chat_text():
    text = _turn_taking()
    assert "Never a second spoken reply before they speak again" in text
    assert "Never a second `dc-say`" not in text
    assert "as text in the chat" in text
    # A plain dc-say is spoken into a live call (bot.py speak_if_live), so
    # the text route must be one that never is: dc-dm (PHONE-253 F1).
    assert "as text in the chat, with `dc-dm NAME TEXT`" in text


def _on_a_call() -> str:
    """The "## On a call" section of SKILL.md, flattened."""
    text = SKILL.read_text(encoding="utf-8")
    on_call = re.search(r"^## On a call\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    assert on_call, "no '## On a call' section in SKILL.md"
    return _flat(on_call.group(1))


def test_skill_answers_a_call_as_the_call_mail_says_mouth_first():
    """The call mail tells the agent to answer with the mouth, and names
    dc-say only if the mouth fails. The skill says the same, in the mail's
    own words, and never presents a dc-say line as THE call reply."""
    hint = dc_env.reply_hint(12, call="N")
    first, fallback = hint.splitlines()
    assert first == 'Answer with: mouth say --device call:N "text"'
    assert fallback.startswith("If the mouth fails: ")
    text = _on_a_call()
    assert f"`{first}`" in text
    assert "only for when the mouth fails" in text
    # the old wording that made dc-say --call the way to answer a call
    assert 'dc-say --call "$(dc-live --id)" CHAT_ID "text"` speaks into the call' not in text


# dc-say --text-only (PHONE-252) landed beside PHONE-253's turn-taking rules.
# This file once asserted the skill never named it, written while 252 was
# unmerged; the merged tree went red (PHONE-262). These pin the skill to the
# landed contract, and the skill's words to the code that keeps them.


def test_skill_teaches_text_only_for_a_silent_chat_post():
    text = _on_a_call()
    assert '`dc-say --text-only CHAT "text"`' in text
    assert "posts to the chat and never speaks" in text
    assert f"The ack ends `{CHAT_ONLY_ACK}`" in text
    assert "`--text-only` with `--call` is a usage error (exit 1, nothing sent)" in text


def test_skill_never_offers_text_only_with_call():
    # No command the skill shows may combine the two flags: that is the
    # usage error, never a way to say something.
    spans = re.findall(r"`([^`]*)`", SKILL.read_text(encoding="utf-8"))
    both = [s for s in spans if "--text-only" in s and "--call" in s]
    assert both == [], both
    assert any(s.startswith("dc-say --text-only") for s in spans)


def test_skill_chat_only_ack_is_the_one_the_bot_writes():
    # The bot appends this to a no_speak job's ack (bot.py); the skill quotes
    # it so an agent can tell a silent post from a spoken one.
    assert f'"; {CHAT_ONLY_ACK}"' in BOT.read_text(encoding="utf-8")


def test_skill_usage_error_is_what_dc_say_does():
    # "--text-only with --call is a usage error (exit 1, nothing sent)": the
    # check runs before dc-say finds a root, so a missing one is never reached.
    env = {**os.environ, "DC_ROOT": "/nonexistent-phone-262"}
    for args in (("--text-only", "--call", "5", "12", "hi"), ("--call", "5", "--text-only", "12", "hi")):
        r = subprocess.run([str(DC_SAY), *args], capture_output=True, text=True,
                           timeout=60, check=False, env=env)
        assert r.returncode == 1, (args, r.returncode, r.stderr)
        assert "--text-only" in r.stderr and "--call" in r.stderr, r.stderr
        assert "Nothing was queued" in r.stderr, r.stderr


def test_floor_line_matches_the_code_default():
    text = _turn_taking()
    assert "`DC_FLOOR_GAP`" in text
    assert f"{floor.GAP_DEFAULT:g} s" in text
    # The cap releases a held reply even mid-speech: the skill must not
    # promise the bot never starts over the caller.
    assert "`DC_FLOOR_CAP`" in text
    assert f"{floor.CAP_DEFAULT:g} s at most" in text
    assert f"{floor.GAP_DEFAULT:g} s by default" in text
    assert "never starts over" not in text
    assert "does not start over" not in text


def test_changelog_fragment_carries_the_skill_rules():
    text = _flat(_changelog.entry(*FRAGMENT))
    assert "one `dc-say` per caller turn" in text
    assert "as text in the chat" in text

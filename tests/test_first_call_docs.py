"""A first "call me" on a fresh install runs clean: the skill says how.

The first call is driven by skills/sessionphone/SKILL.md, so each fault seen
on a fresh account is pinned here in the skill's own words:

1. "Already set up?" is ONE quiet probe that always exits 0, and a failed
   command is said in words, never pasted raw.
2. Step 1 asks no one-option question: it names a default and says how to
   change it.
3. The not-set-up branch says in one line what Delta Chat is.
4. Hearing is claimed only from a `hearing:` line that says `yes`.
5. Step 5 carries the phone walk-through itself and sends the agent to no
   file outside the user's working directory.
6. Step 6 expects the phone to join early and carries on.

The probe is also run, against a throwaway HOME, to show it really is quiet.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "sessionphone" / "SKILL.md"
PROBE = "sessionphone invite >/dev/null 2>&1 && echo 'set up' || echo 'not set up'"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _setup() -> str:
    """The "Set up Delta" section, up to the next `## ` heading."""
    m = re.search(r'^## "Set up Delta", in order\n(.*?)^## ', _text(), re.MULTILINE | re.DOTALL)
    assert m, 'SKILL.md has no "Set up Delta" section followed by another section'
    return m.group(1)


def _lead() -> str:
    """What the setup section says before its step 1."""
    lead = re.split(r"^1\. ", _setup(), maxsplit=1, flags=re.MULTILINE)
    assert len(lead) == 2, "the setup section has no step 1"
    return _flat(lead[0])


def _step(n: int) -> str:
    m = re.search(rf"^{n}\. (.*?)^{n + 1}\. ", _setup(), re.MULTILINE | re.DOTALL)
    assert m, f"no setup step {n} followed by a step {n + 1}"
    return _flat(m.group(1))


def _step7() -> str:
    m = re.search(r"^7\. (.*)", _setup(), re.MULTILINE | re.DOTALL)
    assert m, "no setup step 7"
    return _flat(m.group(1))


# ---- 1: the quiet probe ----

def test_the_probe_comes_before_step_1_word_for_word():
    lead = _lead()
    assert f"`{PROBE}`" in lead
    assert "always exit" in lead
    assert "`set up`: skip to step 3." in lead


def test_the_skill_forbids_probing_by_hand_and_pasting_a_raw_error():
    lead = _lead()
    assert "Never probe by hand" in lead and "`ls`" in lead
    assert "shows red" in lead
    assert "say what it means in words" in lead
    assert "never paste the raw error" in lead.lower()


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_the_probe_is_quiet_and_exits_0_when_nothing_is_set_up(tmp_path):
    """Run the skill's line with a stub `sessionphone` that fails loudly, the
    way the real one does with no account: one word on stdout, nothing on
    stderr, exit 0."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "sessionphone"
    stub.write_text("#!/bin/sh\necho 'sessionphone: no account - run setup first' >&2\nexit 2\n")
    stub.chmod(0o755)
    env = {"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin", "HOME": str(tmp_path)}
    run = subprocess.run(["sh", "-c", PROBE], env=env, capture_output=True, text=True, check=False)
    assert (run.returncode, run.stdout, run.stderr) == (0, "not set up\n", "")


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_the_probe_says_set_up_when_invite_succeeds(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "sessionphone"
    stub.write_text("#!/bin/sh\necho 'https://i.delta.chat/#X'\nexit 0\n")
    stub.chmod(0o755)
    env = {"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin", "HOME": str(tmp_path)}
    run = subprocess.run(["sh", "-c", PROBE], env=env, capture_output=True, text=True, check=False)
    assert (run.returncode, run.stdout, run.stderr) == (0, "set up\n", "")


# ---- 2: no one-option question ----

def test_step_1_uses_the_default_name_and_says_how_to_change_it():
    step = _step(1)
    assert "Ask what they'll call you" not in _text()
    assert "One question." not in step
    assert '**Name yourself "Claude"**' in step
    assert '`sessionphone setup --name "NEW"`' in step
    assert "Never ask a one-option question." in step


# ---- 3: what Delta Chat is ----

def test_the_not_set_up_branch_says_what_delta_chat_is_in_one_line():
    lead = _lead()
    assert "`not set up`: tell them in one line what Delta Chat is" in lead
    assert "a free chat app for their phone" in lead
    assert lead.index("`not set up`") < lead.index("then go to step 1")


# ---- 4: hearing only from a yes ----

def test_step_2_claims_hearing_only_from_a_yes_line_read_in_this_run():
    step = _step(2)
    assert "Say hearing works only from a `hearing:` line that says `yes` in this run" in step
    assert "else say nothing" in step


# ---- 5: no read outside the working directory ----

def test_step_5_sends_the_agent_to_no_file():
    step = _step(5)
    assert "references/" not in step
    assert "user-setup" not in _text()
    assert not re.search(r"\]\[[a-z-]+\]|\.md\b", step), step


def test_step_5_carries_what_the_person_does_in_their_words():
    step = _step(5)
    for words in ("App Store or Google Play", '"Create new profile"', "keep its defaults",
                  "scan as above", "Already on it? Just scan."):
        assert words in step, words
    banned = step.split("never say", 1)[1]
    for word in ('"email"', '"server"', '"relay"', '"chatmail"'):
        assert word in banned, word


def test_step_5_never_has_the_agent_say_a_word_it_bans():
    """The words step 5 has the agent say must not include the words it
    tells the agent never to say."""
    said, banned = _step(5).split("never say", 1)
    for word in re.findall(r'"([a-z]+)"', banned):
        assert not re.search(rf"\b{word}\b", said, re.IGNORECASE), word


def test_the_long_walk_through_stays_for_people_reading_the_repo():
    assert (ROOT / "skills" / "sessionphone" / "references" / "user-setup.md").is_file()


# ---- 6: the early join ----

def test_step_6_expects_an_early_join_and_carries_on():
    step = _step(6)
    assert "**`link` can exit 0 before you finish steps 4-5**" in step
    assert "That is the join" in step
    assert "go on to step 7" in step
    assert "`joined:` line names the owner" in step
    assert "don't stop to re-check who they are" in step


def test_step_7_still_needs_link_to_have_exited():
    assert "`link` must have exited first" in _step7()

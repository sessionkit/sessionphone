"""The docs say what the code does about hearing (PHONE-18).

SKILL.md, the CHANGELOG's Unreleased part and the pyproject comment must
match the code: the default STT URL comes from dc_env.STT_DEFAULT, the
offered install command from hearing.OFFERED_INSTALL, and the line that
restores the old default must still mean `ears serve`. These tests hold the
words to the code, the way test_media_docs does for the media pack.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from sessionphone import hearing
from sessionphone.bridge import dc_env, stt

ROOT = Path(__file__).resolve().parents[1]
OLD_DEFAULT = "http://127.0.0.1:2023/v1/audio/transcriptions"
RESTORE = f"DC_STT_URL={OLD_DEFAULT} sessionphone bot install"


def _flat(text: str) -> str:
    """Whitespace folded to single spaces, so a wrapped line still matches."""
    return " ".join(text.split())


def _skill() -> str:
    return _flat((ROOT / "skills" / "sessionphone" / "SKILL.md").read_text(encoding="utf-8"))


def _hearing_bullets() -> str:
    """Step 7's two hearing bullets, raw: from "Will you hear them?" to the next
    `## ` heading ("On a call"). S3 is about these words, not the whole skill."""
    text = (ROOT / "skills" / "sessionphone" / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^ *- \*\*Will you hear them\?\*\*(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    assert m, "no 'Will you hear them?' bullet in SKILL.md"
    return m.group(1)


def _section(text: str, heading: str) -> str:
    """From `heading` to the next heading of the same level."""
    level = heading.split(" ")[0]
    m = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^{level} |\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, f"no {heading!r} section"
    return m.group(1)


RELEASED_IN = "## [0.2.0]"       # the release these notes shipped in (PyPI 0.2.0)


def _unreleased() -> str:
    """The CHANGELOG part these notes live in: they shipped in 0.2.0, so its
    section, not [Unreleased] (which now holds only what came after)."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return text.split(RELEASED_IN, 1)[1].split("\n## [", 1)[0]


def test_changelog_changed_carries_the_one_line_that_restores_2023():
    changed = _section(_unreleased(), "### Changed")
    lines = [ln.strip() for ln in changed.splitlines()]
    assert RESTORE in lines                                     # on a line of its own, copyable
    assert dc_env.STT_DEFAULT in _flat(changed)
    # the line restores exactly the old default, and that still means `ears serve`
    assert stt.resolve(OLD_DEFAULT)[0] == "apple"
    assert dc_env.stt_url({"DC_STT_URL": OLD_DEFAULT}) == OLD_DEFAULT


def test_changelog_upgrade_notice_points_at_the_default_move_in_one_line():
    """do-002 review R4: the Upgrade notice carries ONE line on the U2 move,
    pointing at the restore line under Changed (PHONE-34 shares this section)."""
    notice = _section(_unreleased(), "### Upgrade notice (read this first)")
    ours = [ln for ln in notice.splitlines() if "PHONE-18" in ln or "DC_STT_URL" in ln]
    assert len(ours) == 1, ours
    line = ours[0]
    assert "(PHONE-18)" in line and "`:2023`" in line and "`:8890`" in line
    assert "`hearing: NO`" in line and 'under "Changed"' in line
    assert RESTORE in [ln.strip() for ln in _section(_unreleased(), "### Changed").splitlines()]


def test_changelog_added_names_the_hearing_line():
    added = _flat(_section(_unreleased(), "### Added"))
    assert "`bot install` says whether the bot will hear the caller** (PHONE-18)" in added
    assert hearing.OFFERED_INSTALL in added and "sessionphone does not install it" in added


def test_skill_step_7_states_the_cost_and_runs_voicemode_only_on_an_explicit_yes():
    skill = _skill()
    assert "`bot install` ends with a `hearing:` line" in skill
    assert f"`{hearing.OFFERED_INSTALL}`" in skill
    assert "background service" in skill and "downloads the model" in skill    # the cost
    assert "only on their explicit yes, given in this conversation" in skill    # S1
    assert "sessionphone does not install it" in skill
    assert "Ears is not needed to hear them" in skill
    assert "With ears installed, you hear them" not in skill                   # the old, false line


def test_skill_step_7_never_states_the_model_or_its_size_as_fact():
    """S3, scoped to step 7's hearing bullets (do-002 review R3): a size stated
    elsewhere in the skill is not this check's business."""
    step7 = _flat(_hearing_bullets())
    # the scope is the real text, not an empty match that passes anything
    assert "`hearing:` line" in step7 and hearing.OFFERED_INSTALL in step7
    assert "On a call" not in step7 and "Voice notes" not in step7
    assert "Parakeet" not in step7 and "GB" not in step7                        # never as fact
    assert "the model the bot asks for" in step7                               # named as requested


def test_skill_step_7_says_not_confirmed_means_read_the_reason():
    """do-002 review R1: `not confirmed` is also the unsent cases (no clip, no
    plist), so the gloss is "read the reason", not only "wait"."""
    step7 = _flat(_hearing_bullets())
    assert "`not confirmed` means it could not tell: read the line's reason, fix what it names" in step7
    assert "run `bot install` again in a few minutes" not in step7


def test_skill_on_a_call_says_dc_ears_is_needed_for_ears():
    skill = _skill()
    assert "Hearing them is the bot's job" in skill
    assert "Only with `DC_EARS=1` in the bot's environment does it listen through ears" in skill
    assert "Hearing them is ears' job" not in skill


def test_skill_voice_notes_names_the_codes_default():
    skill = _skill()
    assert f"The default is an mlx-audio server at `{dc_env.STT_DEFAULT}`" in skill
    assert "the kit's own speech server under Session Kit, or VoiceMode's standalone" in skill
    assert f"`ears serve` (Apple speech, macOS 27) is `{OLD_DEFAULT}`" in skill
    assert "A call uses the same endpoint for what the caller says" in skill


def test_pyproject_declares_no_ears_extra_and_says_why():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    extras = tomllib.loads(text)["project"].get("optional-dependencies", {})
    assert "ears" not in extras and "voice" not in extras
    assert not any(re.match(r"(ears|sessionvoice)\b", d) for ds in extras.values() for d in ds)
    flat = _flat(text)
    assert "# NO `ears` extra (not decided yet)." in flat
    assert "The bot hears a call through the # STT at DC_STT_URL, not through ears." in flat


def test_skill_step_7_splits_the_kit_from_standalone():
    """Under Session Kit the kit's own `ears services install` loads the
    mlx-audio server on :8890, and a second mlx-audio job crash-loops. So
    step 7 names the kit signal once, sends a kit seat to the read-only
    `ears services status`, says a first-run download reads `not confirmed`
    for a while, and keeps the VoiceMode offer for standalone only."""
    step7 = _flat(_hearing_bullets())
    kit, standalone = step7.split("**Standalone**", 1)
    kit = kit.split("**Under the kit,**", 1)[1]
    # the signal, stated once
    assert "It is when `command -v sessionkit` prints a path." in step7
    assert step7.count("command -v sessionkit") == 1
    # under the kit: read-only status, never VoiceMode's installer
    assert "run `ears services status` (read-only) and read its `speech` row" in kit
    assert f"Never offer `{hearing.OFFERED_INSTALL}`" in kit
    assert "crash-loop" in step7
    assert "for a minute or more. Wait, then run `sessionphone bot install` again" in kit
    assert "only on their explicit yes" not in kit
    # every form of the kit's `speech` row has an answer: up, not running, held, absent
    for row in ("`mlx-audio on :8890`", "`none on :8890`", "`none answering`",
                "`:8890 held`", "No `speech` row"):
        assert row in kit, row
    assert "install nothing over it" in kit
    # install loads the kit's server only on `none on :8890`; on `none answering` a job is
    # loaded, and install refuses a second (sessionvoice speech.py, `refuse-loaded-down`)
    installs = kit.split("`ears services install`", 1)
    assert "`none on :8890`" in installs[0] and "`none answering`" not in installs[0]
    assert "install will not add a second" in installs[1]
    # standalone: the VoiceMode offer, cost first, on an explicit yes
    assert f"`{hearing.OFFERED_INSTALL}`" in standalone
    assert "Tell them the cost first" in standalone
    assert "only on their explicit yes, given in this conversation" in standalone


def test_skill_step_7_and_the_hearing_line_agree_under_the_kit():
    """The skill and the `hearing:` line ask the same question (is `sessionkit`
    on PATH?) and, under the kit, name the same commands: the kit's status and
    installer, never VoiceMode's. The step says so, and no longer tells the
    seat to look past the line."""
    step7 = _flat(_hearing_bullets())
    kit = step7.split("**Standalone**", 1)[0].split("**Under the kit,**", 1)[1]
    assert f"`command -v {hearing.KIT_COMMAND}`" in step7
    assert f"`{hearing.KIT_STATUS}`" in kit and f"`{hearing.KIT_INSTALL}`" in kit
    # both forms the line gives, each with the Mac it fits; the bare one is refused
    assert f"`{hearing.KIT_INSTALL_PHONE}` on a Mac that only runs the bot" in kit
    assert f"`{hearing.KIT_INSTALL_AGENT}` where ears should wake an agent seat" in kit
    assert "A bare install that names no seat to wake is refused" in kit
    assert ("The `hearing:` line agrees: it finds the same `sessionkit` on PATH, points at "
            f"`{hearing.KIT_STATUS}`, and does not name VoiceMode's command.") in kit
    assert "even though the `hearing:` line names it" not in step7


def test_readme_voice_call_names_both_servers_and_no_second_install():
    text = _flat((ROOT / "README.md").read_text(encoding="utf-8"))
    para = text.split("**What a voice call needs.**", 1)[1].split("**What does not work", 1)[0]
    assert f"The default is an mlx-audio server, `{dc_env.STT_DEFAULT}`" in para
    assert "The default is VoiceMode's mlx-audio" not in para
    assert "Under Session Kit it is the kit's own speech server" in para
    assert "do not install VoiceMode's as well" in para

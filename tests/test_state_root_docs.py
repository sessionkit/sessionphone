"""The docs name the new root and the migrate verb (PHONE-270).

The default bot root is ~/.sessionkit/phone/state; ~/.sessionphone is only
the fallback and the symlink a migration leaves. No user-facing doc tells a
user to use the old path as the default, and `sessionphone bot migrate` is
documented with --dry-run and each of its refusals and cares.
"""
from __future__ import annotations

import re
from pathlib import Path

import _changelog

from sessionphone import paths

ROOT = Path(__file__).resolve().parents[1]
OLD = "~/.sessionphone"
NEW = "~/.sessionkit/phone/state"
# The skill's long reference moved to docs/ (PHONE-275): the two pages that name
# the root are read too.
DOCS = ["README.md", "skills/sessionphone/SKILL.md", "docs/configuration.md", "docs/settings.md",
        "docs/what-reaches-you.md", "docs/bot-files.md"]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    level = len(heading) - len(heading.lstrip("#"))
    m = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^#{{1,{level}}} |\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, f"no `{heading}` section"
    return _flat(m.group(1))


def test_the_new_root_is_the_one_the_code_spells():
    assert paths.DEFAULT_ROOT == NEW
    assert paths.OLD_ROOT == OLD


def test_no_doc_names_the_old_root_as_a_default_to_use():
    """The old path appears only where the fallback or the migration is explained."""
    for name in DOCS:
        for n, line in enumerate(_read(name).splitlines(), 1):
            if OLD not in line:
                continue
            flat = _flat(line)
            allowed = ("old " in flat or "Before 0.3.2" in flat or "fallback" in flat
                       or "was `~/.sessionphone`" in flat or "until" in flat)
            assert allowed, f"{name}:{n} tells a user to use {OLD}: {flat}"


def test_the_skill_opens_the_invite_at_the_new_root():
    skill = _read("skills/sessionphone/SKILL.md")
    assert f"open {NEW}/invite.png" in skill
    assert f"open {NEW}/connect.html" in skill
    # The kept-mail bullet is in the page the skill's "What reaches you" links.
    assert f"`{NEW}/undelivered/`" in _read("docs/what-reaches-you.md")


def test_the_readme_documents_migrate_with_its_dry_run_and_cares():
    sec = _section(_read("README.md"), "### Moving the root with bot migrate")
    assert "sessionphone bot migrate --dry-run" in sec
    for words in ("Stop the bot first", "refuses", "call is live", "rename", "never copy",
                  "symlink", "never delete", "plists", "account:", f"`{NEW}/`"):
        assert words in sec, words


def test_configuration_and_settings_point_at_the_migrate_section():
    for name in ("docs/configuration.md", "docs/settings.md"):
        text = _flat(_read(name))
        assert "sessionphone bot migrate" in text, name
        assert "README.md#moving-the-root-with-bot-migrate" in text, name


def test_a_changelog_fragment_names_the_root_and_the_verb():
    frag = _flat(_changelog.entry("PHONE-270-state-move-kit.md", "The bot's root moves to ~/.sessionkit/phone/state"))
    assert NEW in frag and "sessionphone bot migrate [--dry-run]" in frag
    assert "symlink" in frag and "never deleted" in frag

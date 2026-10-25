"""The docs tell a contributor to write a fragment, not a CHANGELOG.md line.

Each change writes changelog.d/<TASK-ID>-<slug>.md and scripts/release.py
collates the fragments at release (test_release_fragments covers the code).
These tests hold the words that send a contributor there: the CHANGELOG.md
header, AGENTS.md's spec rule, the README's Status section, the how-to in
changelog.d/README.md, and the Makefile's `changelog` target, whose recipe
is run here as written.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = "changelog.d/<TASK-ID>-<slug>.md"


def _flat(text: str) -> str:
    """Whitespace folded to single spaces, so a wrapped line still matches."""
    return " ".join(text.split())


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """From `heading` (a whole line) up to the next heading of its level or higher."""
    level = len(heading) - len(heading.lstrip("#"))
    m = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^#{{1,{level}}} |\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, f"no `{heading}` section"
    return _flat(m.group(1))


def _changelog_recipe() -> str:
    """The one recipe line under the Makefile's `changelog:` target."""
    text = _read("Makefile")
    m = re.search(r"^changelog:[^\n]*## [^\n]+\n\t(.+)$", text, re.MULTILINE)
    assert m, "Makefile has no `changelog:` target with a `## ` help line and one recipe"
    return m.group(1)


def test_the_changelog_header_points_at_the_fragments_above_unreleased():
    text = _read("CHANGELOG.md")
    header, sep, _ = text.partition("\n## [Unreleased]\n")
    assert sep, "CHANGELOG.md has no `## [Unreleased]` line"
    header = _flat(header)
    for phrase in ("`changelog.d/`", "`changelog.d/README.md`", "`make changelog`"):
        assert phrase in header, f"the CHANGELOG.md header, above [Unreleased], does not name {phrase}"


def test_the_spec_rule_names_the_fragment_not_a_changelog_line():
    rule = _section(_read("AGENTS.md"), "## The spec rule")
    assert f"`{FRAGMENT}`" in rule, "AGENTS.md's spec rule does not name the fragment path"
    assert "never in `CHANGELOG.md`" in rule
    assert "`make changelog`" in rule
    assert "adds a line under `[Unreleased]`" not in rule, "AGENTS.md still says to edit [Unreleased]"


def test_the_readme_status_names_the_fragments_and_make_changelog():
    status = _section(_read("README.md"), "## Status").split("**What works on master.**")[0]
    assert "`changelog.d/`" in status, "README Status does not say where pending changes are"
    assert "`make changelog`" in status, "README Status does not name `make changelog`"


def test_the_fragment_readme_says_how_to_write_one():
    text = _flat(_read("changelog.d/README.md"))
    for phrase in (
        "`changelog.d/<TASK-ID>-<slug>.md`",  # the path, one per task
        "`### <headline>`",  # sessionphone's shape
        "`Changelog: none, no user-visible change`",  # no fragment, said so
        "No relative links",  # a fragment's link breaks once collated
        "numbers compared as numbers",  # the order, which no author picks
        "no say in where an entry lands",
        "modify/delete conflict",  # the scrub that meets a release
        "Never resolve it by keeping the deletion alone",
        "`make scan`",
        "`make changelog`",
    ):
        assert phrase in text, f"changelog.d/README.md does not say {phrase!r}"


def test_the_makefile_changelog_target_previews_and_writes_nothing():
    recipe = _changelog_recipe()
    assert recipe == "@python3 scripts/release.py --preview", recipe
    before = _read("CHANGELOG.md")
    out = subprocess.run(
        [sys.executable, "scripts/release.py", "--preview"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    assert out.startswith("## [Unreleased]\n"), out[:80]
    assert "\n## [" not in out, "the preview printed more than [Unreleased]"
    for fragment in sorted((ROOT / "changelog.d").glob("*.md")):
        if fragment.name == "README.md":
            continue
        for line in fragment.read_text(encoding="utf-8").splitlines():
            if line.startswith("### "):
                assert f"\n{line}\n" in out, f"make changelog does not print {fragment.name}'s {line!r}"
    assert _read("CHANGELOG.md") == before, "make changelog wrote CHANGELOG.md"

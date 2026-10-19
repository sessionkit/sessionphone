"""The docs say what the code does about the media pack (PHONE-10).

SKILL.md, the README's Installation section and the CHANGELOG's Unreleased
part must tell a new user that setup is the small first download and that the
media pack comes once, at the first media verb. These tests hold the words to
the code, the way test_dc_shape and test_channels_cli hold other README and
CHANGELOG lines: the verbs come from the launcher's own table, the size from
media.SIZE, the extra's name from pyproject.toml and the install line from the
Makefile. A change to any of those that the docs do not follow fails here.
"""
from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

from sessionphone import media

ROOT = Path(__file__).resolve().parents[1]


def _flat(text: str) -> str:
    """Whitespace folded to single spaces, so a wrapped line still matches."""
    return " ".join(text.split())


def _skill() -> str:
    return _flat((ROOT / "skills" / "sessionphone" / "SKILL.md").read_text(encoding="utf-8"))


def _installation() -> str:
    """The README from `## Installation` up to the next `## ` heading."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^## Installation\n(.*?)^## ", text, re.MULTILINE | re.DOTALL)
    assert m, "README.md has no `## Installation` section followed by another `## ` section"
    return _flat(m.group(1))


def _unreleased() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[Unreleased\]\n(.*?)^## \[", text, re.MULTILINE | re.DOTALL)
    assert m, "CHANGELOG.md has no `## [Unreleased]` part followed by a release"
    return _flat(m.group(1))


def _media_verbs() -> list[str]:
    src = (ROOT / "bin" / "sessionphone").read_text(encoding="utf-8")
    m = re.search(r"^media_verbs=\((.*)\)$", src, re.MULTILINE)
    assert m, "bin/sessionphone has no `media_verbs=(...)` line"
    return shlex.split(m.group(1))


def _make_install() -> str:
    """The command `make install` runs, from the Makefile's recipe line."""
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("install:"))
    return lines[i + 1].strip().lstrip("@")


def test_skill_md_no_longer_says_the_first_run_takes_about_a_minute():
    skill = _skill()
    assert "builds its environment, which takes about a minute" not in skill
    assert "about a minute" not in skill


def test_skill_md_says_setup_is_small_and_the_pack_comes_at_bot_install():
    skill = _skill()
    assert "Setup comes first, and it is small." in skill
    assert "about 10 MB" in skill
    # the size the guard's refusal and the launcher's fetch line both print
    assert media.SIZE in skill
    assert "the first `bot install`" in skill


def test_skill_md_warns_the_first_bot_install_can_outrun_a_bash_timeout():
    skill = _skill()
    assert "can take minutes" in skill and "two-minute timeout" in skill
    assert "Run the same command again." in skill
    # the resume is uv's cache, not measured here; the doc must say so (N3)
    assert "not measured" in skill


# Step 7's hearing lines were PHONE-18's to write. PHONE-10 left them alone
# and pinned them here until PHONE-18 rewrote them. tests/test_hearing_docs.py
# now holds them to the code.


def test_readme_and_changelog_state_the_measured_sizes():
    for name, text in (("README Installation", _installation()), ("CHANGELOG Unreleased", _unreleased())):
        for size in ("10.0 MB", "52.5 MB", "30.8 MB", "25.3 MB", "40.8 MB", "66.1 MB"):
            assert size in text, f"{name} does not say {size}"


def test_readme_names_every_media_verb_the_launcher_fetches_for():
    text = _installation()
    verbs = _media_verbs()
    assert verbs, "the launcher's media_verbs table is empty"
    for verb in verbs:
        assert f"`{verb}`" in text, f"README Installation does not name the media verb `{verb}`"


def test_changelog_names_every_media_verb_the_launcher_fetches_for():
    text = _unreleased()
    for verb in _media_verbs():
        assert f"`{verb}`" in text, f"CHANGELOG Unreleased does not name the media verb `{verb}`"


def test_readme_and_changelog_name_the_extra_and_its_six_requirements():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    assert list(extras) == ["media"]
    names = [re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0] for req in extras["media"]]
    assert sorted(names) == sorted(media.MODULES)
    for name, text in (("README Installation", _installation()), ("CHANGELOG Unreleased", _unreleased())):
        assert "`media` extra" in text, f"{name} does not name the `media` extra"
        assert "`sessionphone[media]`" in text or "'sessionphone[media]'" in text, name
        for req in names:
            assert req in text, f"{name} does not name {req}"


def test_readme_and_changelog_quote_the_makefiles_install_line():
    line = _make_install()
    assert line == "uv tool install -e '.[media]'", line
    for name, text in (("README Installation", _installation()), ("CHANGELOG Unreleased", _unreleased())):
        assert f"`{line}`" in text, f"{name} does not quote `{line}`"


def test_readme_and_changelog_tell_pip_and_uv_tool_users_to_ask_for_the_extra():
    for name, text in (("README Installation", _installation()), ("CHANGELOG Unreleased", _unreleased())):
        assert "uv tool install 'sessionphone[media]'" in text, name
        assert "pip install 'sessionphone[media]'" in text, name


def test_readme_and_changelog_say_a_kit_install_needs_sk_76():
    # The CHANGELOG keeps its bare tracker ref; the README says it in plain words
    # and carries no internal tracker ID (PHONE-172).
    assert "SK-76" in _unreleased(), "CHANGELOG Unreleased does not say a sessionkit install needs SK-76"
    assert "SK-76" not in _installation(), "README Installation carries the internal tracker ID SK-76"
    for name, text in (("README Installation", _installation()), ("CHANGELOG Unreleased", _unreleased())):
        assert "sessionkit" in text, name


def test_readme_and_changelog_say_a_failed_fetch_exits_2_and_changes_nothing_else():
    for name, text in (("README Installation", _installation()), ("CHANGELOG Unreleased", _unreleased())):
        assert "exits 2" in text, name
        assert "setup, qr and link still work" in text.lower(), name

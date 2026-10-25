"""The dependency docs keep the family's shape, and follow the code.

Three files carry it: the README's `## Requirements` (the operating systems,
then one line linking the page), `docs/dependencies.md` (one entry per direct
dependency and tool, with the full resolved set left to `uv.lock`), and
`.github/dependabot.yml` (uv, daily). These tests hold the shape, and hold the
page's facts to the sources they come from: the Python packages to
pyproject.toml, the media verbs to the launcher's own table, ffmpeg's lookup
to hearing.py, and the bot's fallback voice to bridge/calls.py.
"""
from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest

from sessionphone import hearing

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "dependencies.md"
LOCK_SENTENCE = ("The full resolved set, every package at the version this repo is tested "
                 "with, is in [`uv.lock`][uv-lock].")
HELD = ("Homebrew", "ffmpeg")      # the class is not decided: facts only, no install step


def _flat(text: str) -> str:
    """Whitespace folded to single spaces, so a wrapped line still matches."""
    return " ".join(text.split())


def _section(text: str, heading: str, level: str = "##") -> str:
    """TEXT from HEADING up to the next heading of the same level or above."""
    stop = "|".join(re.escape("#" * n + " ") for n in range(1, len(level) + 1))
    m = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^(?:{stop})|\Z)", text,
                  re.MULTILINE | re.DOTALL)
    assert m, f"no `{heading}` section"
    return m.group(1)


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _first_screen() -> str:
    """The page up to its first `## ` heading."""
    return _page().split("\n## ", 1)[0]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _names(reqs: list[str]) -> list[str]:
    return [re.split(r"[<>=!~\[; ]", r, maxsplit=1)[0] for r in reqs]


# --- the README ---------------------------------------------------------------

def test_requirements_is_the_os_checkboxes_then_one_line_linking_the_page():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    body = _section(readme, "## Requirements").strip()
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert lines[:4] == ["- [x] macOS (tested on macOS 27, Apple silicon)",
                         "- [ ] Linux", "- [ ] WSL", "- [ ] Windows"]
    rest = _flat(" ".join(lines[4:]))
    assert rest.endswith("[dependencies][dependencies]."), rest
    assert "[dependencies]: docs/dependencies.md" in readme.splitlines()
    assert "**Required:**" not in body and "**Optional:**" not in body
    assert "full list" not in body


def test_requirements_sits_between_installation_and_usage():
    """The family order: Quick start, Installation, then Requirements."""
    heads = re.findall(r"^## (.+)$", (ROOT / "README.md").read_text(encoding="utf-8"),
                       re.MULTILINE)
    i = heads.index("Requirements")
    assert heads[i - 1:i + 2] == ["Installation", "Requirements", "Usage"]
    assert heads.index("Quick start") < heads.index("Installation")


# --- docs/dependencies.md -----------------------------------------------------

def test_the_first_screen_has_the_four_lists_and_the_lock_sentence():
    first = _first_screen()
    assert LOCK_SENTENCE in first, "the uv.lock sentence, exactly, unwrapped"
    for label in ("**Required:**", "**Optional:**", "**Pending a ruling:**", "**Upgrade:**"):
        assert label in first, label
    assert re.search(r"^\[uv-lock\]: \.\./uv\.lock$", _page(), re.MULTILINE)
    assert (ROOT / "uv.lock").is_file()


def test_the_page_says_direct_and_never_the_full_list():
    flat = _flat(_page())
    assert "direct dependencies and the tools a user meets" in flat
    assert "full list" not in flat


@pytest.mark.parametrize("held", HELD)
def test_a_held_dependency_has_facts_and_no_class_or_install_step(held):
    entry = _section(_section(_page(), "## Pending a ruling"),
                     next(h for h in re.findall(r"^### .+$", _page(), re.MULTILINE)
                          if held in h), "###")
    assert "Status: pending a ruling" in entry
    for fact in ("**What uses it:**", "**Without it:**", "**Where it comes from today:**"):
        assert fact in entry, fact
    assert not re.search(r"brew install|\bRequired\b|\bOptional\b", entry), entry
    for table in (_section(_page(), "## Required"), _section(_page(), "## Optional")):
        assert not re.search(rf"^\| \[?{held}\b", table, re.MULTILINE), f"{held} is classed"


def test_the_ffmpeg_entry_names_the_silent_failure_and_how_the_bot_looks():
    entry = _flat(_section(_page(), "### [ffmpeg][ffmpeg]", "###"))
    assert "fails silently" in entry
    assert "the reply goes to the chat as text" in entry
    assert "`speaking:` line" in entry
    # the order the bot looks in, as hearing.ffmpeg_for (and bridge/calls.py) have it
    assert "`DC_FFMPEG`, then on its job's `PATH`, then in Homebrew's two `bin` folders" in entry
    assert len(hearing.FFMPEG_HOMEBREW) == 2
    assert all(p.startswith(("/opt/homebrew/", "/usr/local/")) for p in hearing.FFMPEG_HOMEBREW)


def test_every_direct_python_dependency_is_named_with_its_floor():
    proj = _pyproject()["project"]
    flat = _flat(_page())
    for req in proj["dependencies"] + proj["optional-dependencies"]["media"]:
        name = _names([req])[0]
        assert name in flat, f"{name} is a direct dependency the page does not name"
        m = re.search(r"(?:>=|==)([0-9][0-9.]*)", req)
        if m:
            floor = m.group(1)
            assert re.search(rf"{re.escape(name)}[^.]*?\b{re.escape(floor)}\b|"
                             rf"\b{re.escape(floor)}\b[^.]*?{re.escape(name)}", flat), \
                f"{name}: the page does not give {floor}"


def test_the_python_range_follows_requires_python():
    rp = _pyproject()["project"]["requires-python"]
    lo, hi = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", rp.replace(" ", "")).groups()
    assert f"Python 3.{lo} to 3.{int(hi) - 1}" in _flat(_page())


def test_the_media_verbs_are_the_launchers():
    src = (ROOT / "bin" / "sessionphone").read_text(encoding="utf-8")
    verbs = shlex.split(re.search(r"^media_verbs=\((.*)\)$", src, re.MULTILINE).group(1))
    flat = _flat(_page())
    said = "The `media` extra, for " + ", ".join(f"`{v}`" for v in verbs[:-1]) + \
        f" and `{verbs[-1]}`"
    assert said in flat


def test_the_fallback_voice_is_macos_say():
    calls = (ROOT / "src" / "sessionphone" / "bridge" / "calls.py").read_text(encoding="utf-8")
    assert 'SAY_BIN = "/usr/bin/say"' in calls
    assert "macOS `say`" in _page()


def test_the_page_names_no_tmux_or_notmuch_dependency():
    flat = _flat(_page())
    assert "sessionphone uses neither tmux nor notmuch." in flat
    code = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for d in ("src", "bin") for p in (ROOT / d).rglob("*") if p.is_file()
                     and p.suffix in ("", ".py", ".sh"))
    assert not re.search(r"""["'\s]tmux["'\s]""", code)
    assert not re.search(r"""\[\s*["']notmuch["']""", code)


# --- .github/dependabot.yml ---------------------------------------------------

STANDARD = """\
version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "daily"
"""


def test_dependabot_is_the_standard_uv_file_daily():
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    body = "".join(ln for ln in text.splitlines(keepends=True) if not ln.startswith("#"))
    assert body == STANDARD
    assert not (ROOT / ".github" / "workflows").exists(), \
        "a workflow is here: add the github-actions entry to dependabot.yml"


def test_dependabot_parses_as_yaml_when_pyyaml_is_there():
    """The standard's own check is `uv run --with pyyaml`; the dev group does not lock
    PyYAML, so here it runs only where it imports."""
    yaml = pytest.importorskip("yaml")
    conf = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert conf == {"version": 2, "updates": [
        {"package-ecosystem": "uv", "directory": "/", "schedule": {"interval": "daily"}}]}

"""The skill's how-to for a bot's picture and name runs as written.

SKILL.md's "The bot's picture and name" gives four lines: the picture and the
name, for this bot and for another agent's (`DC_AGENT=AGENT`). These tests
pin the lines in SKILL.md and references/profile.md, and run the picture line
through the CLI with two bots recorded on two roots: `DC_AGENT` picks the bot,
and the account opened (or the outbox a held account's job goes to) is under
that bot's root. With two bots and none named, the command refuses and opens
nothing. The name line is the one install itself prints (displayname), and
DC_NAME is kept per bot, so a later install keeps it.

Fakes only: settings.FILE is a tmp file (conftest), the account is a fake
behind a fake `_open`, and no bot runs.
"""
import json
import pathlib
import shutil

import pytest
from click.testing import CliRunner

from sessionphone import avatar, displayname, paths, settings
from sessionphone.cli import main

TOP = pathlib.Path(__file__).resolve().parents[1]
SKILL = (TOP / "skills/sessionphone/SKILL.md").read_text()
PROFILE = (TOP / "skills/sessionphone/references/profile.md").read_text()
SECTION = SKILL.split("\n## The bot's picture and name\n", 1)[1].split("\n## ", 1)[0]
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

PICTURE = "sessionphone avatar /path/to/face.png"
OTHER_PICTURE = "DC_AGENT=AGENT sessionphone avatar /path/to/face.png"
NAME = "DC_NAME='Name' sessionphone bot install"
OTHER_NAME = "DC_AGENT=AGENT DC_NAME='Name' sessionphone bot install"


def _lines(text: str) -> list[str]:
    """The commands in TEXT's bash blocks, comments stripped."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = line.startswith("```bash")
            continue
        if inside and line.strip():
            out.append(line.split("  #", 1)[0].strip())
    return out


# ---- the docs say it ------------------------------------------------------


def test_the_section_gives_the_four_lines_in_order():
    assert _lines(SECTION) == [PICTURE, OTHER_PICTURE, NAME, OTHER_NAME]


def test_the_section_is_short_and_points_to_the_profile_page():
    assert len(SECTION.strip().splitlines()) <= 15
    assert "references/profile.md" in SECTION
    assert "--clear" in SECTION


def test_the_profile_page_agrees():
    cmds = _lines(PROFILE)
    for line in (PICTURE, OTHER_PICTURE, OTHER_NAME):
        assert line in cmds, line
    flat = " ".join(PROFILE.split())
    assert "With two or more recorded and none named, the command refuses (exit 1)" in flat


def test_the_name_line_is_the_one_install_prints():
    assert NAME == displayname.change_line("Name")
    assert settings.savable("DC_NAME")      # kept per bot, so a later install keeps it


# ---- the picture line runs as written ---------------------------------------


@pytest.fixture
def two_bots(tmp_path, monkeypatch):
    """phone.json records two bots, each on its own 0700 root; a fake account
    that remembers which root it was opened under."""
    roots = {}
    for name in ("claude", "versa"):
        r = tmp_path / f"{name}-root"
        r.mkdir(mode=0o700)
        roots[name] = r
    pathlib.Path(settings.FILE).write_text(json.dumps(
        {"bots": {n: {"SESSIONPHONE_ROOT": str(r)} for n, r in roots.items()}}))
    for k in ("SESSIONPHONE_ROOT", "DC_ROOT", "DC_AGENT", "DC_OUTBOX", "DC_ACKS"):
        monkeypatch.delenv(k, raising=False)
    opened = []

    class Account:
        def __init__(self):
            self.avatar = None

        def set_avatar(self, path=None):
            if path is None:
                self.avatar = None
                return
            blob = paths.root() / "blob.png"
            shutil.copyfile(path, blob)
            self.avatar = str(blob)

        def get_avatar(self):
            return self.avatar

    def fake_open(create=False):
        opened.append(paths.root())
        yield Account()
    monkeypatch.setattr(avatar.account_mod, "_open", fake_open)
    face = tmp_path / "face.png"
    face.write_bytes(PNG)
    return roots, opened, face


@pytest.mark.parametrize("agent", ["versa", "claude"])
def test_dc_agent_sets_that_bots_picture(two_bots, monkeypatch, agent):
    roots, opened, face = two_bots
    monkeypatch.setenv("DC_AGENT", agent)
    r = CliRunner().invoke(main, ["avatar", str(face)])
    assert r.exit_code == 0, r.output
    assert opened == [roots[agent]]
    assert (roots[agent] / "blob.png").read_bytes() == PNG
    other = next(n for n in roots if n != agent)
    assert not (roots[other] / "blob.png").exists()


def test_dc_agent_clears_that_bots_picture_and_marks_only_its_root(two_bots, monkeypatch):
    roots, opened, _face = two_bots
    monkeypatch.setenv("DC_AGENT", "versa")
    r = CliRunner().invoke(main, ["avatar", "--clear"])
    assert r.exit_code == 0, r.output
    assert opened == [roots["versa"]]
    assert (roots["versa"] / avatar.NONE_MARKER).exists()
    assert not (roots["claude"] / avatar.NONE_MARKER).exists()


def test_a_held_account_gets_the_job_in_that_bots_outbox(two_bots, monkeypatch):
    roots, _opened, _face = two_bots
    monkeypatch.setenv("DC_AGENT", "versa")
    assert avatar.outbox() == roots["versa"] / "outbox"
    assert avatar.acks_dir() == roots["versa"] / "acks"


def test_two_bots_and_none_named_refuses_and_opens_nothing(two_bots):
    roots, opened, face = two_bots
    r = CliRunner().invoke(main, ["avatar", str(face)])
    assert r.exit_code == 1, r.output
    assert "records 2 bots" in r.output and "DC_AGENT" in r.output
    assert opened == []
    assert not any((root / "blob.png").exists() for root in roots.values())


def test_dc_agent_names_the_bot_install_acts_on(two_bots, monkeypatch):
    monkeypatch.setenv("DC_AGENT", "versa")
    assert settings.bot_name()[0] == "versa"

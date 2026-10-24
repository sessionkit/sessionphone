"""`bot run`, bridge/dc_env.py and bin/dc-env pick the bot the way install does.

Install names the bot DC_AGENT, else this session's durable seat as
sessionmail names it (`message subscription list`), else the one bot
phone.json records, else claude. The readers used to take DC_AGENT, else
CLAUDE_CODE_AGENT, else claude: a foreground `bot run`, or a dc-* tool, from
a plain shell was then `claude` whatever install had recorded, and mailed
another box. Now each picks as install does, and says which bot it picked
when the shell named none. bin/dc-env mirrors dc_env.py: the agreement test
runs both, and install's own pick, in the same environments.

sessionmail is a fake `message` (DC_MESSAGE) answering `subscription list`
with the text a real one prints (test_hear_measured's fixtures); HOME is a
tmp dir. Nothing real is asked.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest
from click.testing import CliRunner
from test_hear_measured import DURABLE, PLAIN, TASK, TASK_SEAT, TERMINAL_ERR

from sessionphone import reach, settings
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

REPO = pathlib.Path(__file__).resolve().parents[1]
DC_ENV = REPO / "bin" / "dc-env"
DEFAULT_FILE = settings.FILE            # read at import, before conftest moves it
SEAT_FROM = settings.SEAT_FROM
RECORDED = "~/.sessionkit/phone/settings/phone.json, the one bot it records"


# ---- the agreement: dc-env, dc_env.py and install, in one environment ------------

FAKE = """#!/bin/sh
here="$(dirname "$0")"
if [ "$*" = "subscription list" ]; then
  cat "$here/answer.out"; cat "$here/answer.err" >&2; exit "$(cat "$here/answer.rc")"
fi
echo "fake message: not this: $*" >&2
exit 3
"""

INSTALL = """import os
from sessionphone import settings
try:
    name, came = settings.bot_name(os.environ, None, ask=True)
except settings.Bad:
    name, came = "REFUSED", ""
print(name); print(came)
"""
PYTHON = """from sessionphone.bridge import dc_env
print(dc_env.AGENT); print(dc_env.AGENT_FROM); print(dc_env.ROOT)
"""
SHELL = 'source "$1"; printf "%s\\n%s\\n" "$DC_AGENT" "$DC_ROOT"'


@pytest.fixture
def world(tmp_path):
    """A scratch HOME, a fake sessionmail, and the three readers run in it."""
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "fake-mail" / "message"
    fake.parent.mkdir()
    fake.write_text(FAKE)
    fake.chmod(0o755)

    class World:
        def __init__(self):
            self.home, self.tmp = home, tmp_path

        def says(self, out: str = "", rc: int = 0, err: str = ""):
            for name, text in (("answer.out", out), ("answer.err", err), ("answer.rc", str(rc))):
                (fake.parent / name).write_text(text)

        def records(self, bots: dict):
            p = home / ".sessionkit" / "phone" / "settings" / "phone.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"bots": bots}))

        def env(self, **extra) -> dict:
            e = {"HOME": str(home), "PATH": "/usr/bin:/bin", "DC_MESSAGE": str(fake),
                 "LANG": os.environ.get("LANG", "C.UTF-8")}
            e.update(extra)
            return e

        def run(self, argv, **extra):
            r = subprocess.run(argv, env=self.env(**extra), cwd=tmp_path, capture_output=True,
                               text=True, timeout=60, check=False)
            assert r.returncode == 0, r.stderr[-2000:]
            return r

        def install(self, **extra) -> list[str]:
            return self.run([sys.executable, "-c", INSTALL], **extra).stdout.splitlines()

        def python(self, **extra) -> list[str]:
            return self.run([sys.executable, "-c", PYTHON], **extra).stdout.splitlines()

        def shell(self, **extra) -> tuple[list[str], str]:
            r = self.run(["bash", "-c", SHELL, "dc-tool", str(DC_ENV)], **extra)
            return r.stdout.splitlines(), r.stderr

    w = World()
    w.says(PLAIN)
    return w


CASES = {
    # case: (shell, phone.json bots, sessionmail's answer (out, rc, err), the bot, where from)
    "a plain claude session, nothing recorded": ({}, None, (PLAIN, 0, ""), "claude", "the default"),
    "a durable seat": ({}, None, (DURABLE, 0, ""), "ada", SEAT_FROM),
    "a durable seat over a recorded bot": ({}, {"bo": {}}, (DURABLE, 0, ""), "ada", SEAT_FROM),
    "a task seat, its role never the name": (
        {"EMAIL": f"{TASK_SEAT}@host.example", "CLAUDE_CODE_AGENT": "worker"}, None,
        (TASK, 0, ""), "claude", "the default"),
    "a plain session, one bot recorded": ({}, {"bo": {}}, (PLAIN, 0, ""), "bo", RECORDED),
    "a Terminal, one bot recorded": ({}, {"bo": {}}, ("", 2, TERMINAL_ERR), "bo", RECORDED),
    "DC_AGENT over everything": ({"DC_AGENT": "Cy"}, {"bo": {}}, (DURABLE, 0, ""), "cy",
                                 "DC_AGENT in this shell"),
    "no answer from sessionmail: CLAUDE_CODE_AGENT": (
        {"CLAUDE_CODE_AGENT": "Dee"}, None, ("", 1, "No such command 'subscription'."), "dee",
        "CLAUDE_CODE_AGENT in this shell"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_dc_env_dc_env_py_and_install_pick_one_bot(world, case):
    shell, bots, (out, rc, err), want, came = CASES[case]
    world.says(out, rc, err)
    if bots is not None:
        world.records(bots)
    installed = world.install(**shell)
    py = world.python(**shell)
    (sh_agent, sh_root), said = world.shell(**shell)
    assert installed == [want, came]
    assert py[:2] == [want, came]
    assert sh_agent == want
    assert sh_root == py[2]
    if came.endswith(" in this shell"):
        assert "dc-tool: bot" not in said
    else:
        assert f"dc-tool: bot {want} ({came})" in said


def test_dc_env_and_dc_env_py_take_the_root_kept_for_the_bot_they_pick(world):
    root = world.tmp / "ada-root"
    world.says(DURABLE)
    world.records({"ada": {"SESSIONPHONE_ROOT": str(root)}, "bo": {}})
    py = world.python()
    (sh_agent, sh_root), _said = world.shell()
    assert py == ["ada", SEAT_FROM, str(root)]
    assert (sh_agent, sh_root) == ("ada", str(root))


def test_two_bots_and_no_name_is_claude_in_both_and_a_refusal_in_install(world):
    world.records({"ab": {}, "cd": {}})
    assert world.install() == ["REFUSED", ""]
    py = world.python()
    (sh_agent, _root), said = world.shell()
    assert py[0] == sh_agent == "claude"
    assert "dc-tool: bot claude (the default (" in said


def test_dc_env_says_the_default_when_the_package_cannot_be_asked(world, tmp_path):
    """A dc-env with no package beside it (and a python3 that has none) falls
    back to claude, and says why."""
    lone = tmp_path / "lone" / "bin"
    lone.mkdir(parents=True)
    (lone / "dc-env").write_text(DC_ENV.read_text())
    (lone / "python3").write_text("#!/bin/sh\nexit 1\n")    # a python3 with no sessionphone
    (lone / "python3").chmod(0o755)
    r = subprocess.run(["bash", "-c", SHELL, "dc-tool", str(lone / "dc-env")],
                       env=world.env(PATH=f"{lone}:/usr/bin:/bin"), capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.stdout.splitlines()[0] == "claude"
    assert "dc-tool: bot claude (the default: the package could not be asked" in r.stderr


# ---- dc_env.py, in this process ------------------------------------------------------

@pytest.fixture
def bound(monkeypatch):
    """dc_env's bound agent values, put back after the test."""
    for k in dc_env._BY_AGENT:
        monkeypatch.setattr(dc_env, k, getattr(dc_env, k))
    monkeypatch.setattr(dc_env, "BAD", [])
    return dc_env


def test_agent_from_takes_the_deprecated_alias_too(monkeypatch, bound, capsys):
    monkeypatch.delenv("DC_AGENT", raising=False)
    assert dc_env.agent_from({"PIP_CHAT_AGENT": "Eve"}) == ("eve", "DC_AGENT in this shell")


def test_agent_from_asks_as_install_does(monkeypatch, bound, tmp_path):
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "phone.json"))
    monkeypatch.setattr(reach, "ask", lambda shell, env=None, exe=None: reach.Reach(
        answered=True, session=True, seat="ada", addresses=frozenset({"ada"})))
    reach._ASKED.clear()
    assert dc_env.agent_from({"PATH": "/usr/bin"}) == ("ada", SEAT_FROM)


def test_an_unusable_phone_json_is_claude_and_bad_says_why(monkeypatch, bound, tmp_path):
    p = tmp_path / "phone.json"
    p.write_text("{not json")
    monkeypatch.setattr(settings, "FILE", str(p))
    name, came = dc_env.agent_from({"PATH": "/usr/bin"})
    assert name == "claude"
    assert came.startswith("the default (")
    assert any("is not JSON" in b for b in dc_env.BAD)


def test_the_agent_is_bound_on_first_use_not_at_import(monkeypatch, bound):
    for k in dc_env._BY_AGENT:
        monkeypatch.delattr(dc_env, k)
    monkeypatch.setenv("DC_AGENT", "Fay")
    monkeypatch.delenv("DC_ADDR", raising=False)
    monkeypatch.delenv("DC_MAILTO", raising=False)
    assert "AGENT" not in vars(dc_env)
    assert dc_env.MAILTO == f"fay@{dc_env.HOST}"
    assert (dc_env.AGENT, dc_env.AGENT_NAME, dc_env.ADDR) == ("fay", "Fay", f"fay-chat@{dc_env.HOST}")
    assert dc_env.GREETING == "Fay here. Go ahead."


def test_mailto_picks_as_install_does(monkeypatch, bound, tmp_path):
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "phone.json"))
    (tmp_path / "phone.json").write_text(json.dumps({"bots": {"bo": {}}}))
    assert dc_env.mailto({"DC_HOST": "h.example", "PATH": "/usr/bin"}) == "bo@h.example"
    assert dc_env.mailto({"DC_HOST": "h.example", "DC_AGENT": "Cy"}) == "cy@h.example"


# ---- bot run ------------------------------------------------------------------------

@pytest.fixture
def run_rig(monkeypatch, tmp_path, bound):
    """`sessionphone bot run` with the bot's main() stubbed: -> what it saw."""
    monkeypatch.setenv("DC_AGENT", "placeholder")      # so the test's DC_AGENT is undone after
    from sessionphone.bridge import bot  # its argv fill sees DC_AGENT set
    monkeypatch.delenv("DC_AGENT")
    monkeypatch.delenv("CLAUDE_CODE_AGENT", raising=False)
    for k in ("DC_ADDR", "DC_MAILTO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "phone.json"))
    seen = {}
    monkeypatch.setattr(bot, "main", lambda: seen.update(
        env=os.environ.get("DC_AGENT"), agent=dc_env.AGENT, mailto=dc_env.MAILTO))

    def run(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        r = CliRunner().invoke(cli, ["bot", "run"])
        return r, seen
    run.records = lambda bots: (tmp_path / "phone.json").write_text(json.dumps({"bots": bots}))
    return run


def test_bot_run_from_a_plain_shell_is_the_bot_install_recorded(run_rig):
    run_rig.records({"bo": {}})
    r, seen = run_rig()
    assert r.exit_code == 0, r.output
    assert seen == {"env": "bo", "agent": "bo", "mailto": f"bo@{dc_env.HOST}"}
    assert r.output.splitlines()[0] == (f"bot:     bo ({settings.shown(settings.file_path())}, "
                                        "the one bot it records)")


def test_bot_run_takes_the_durable_seat_and_says_so(run_rig, monkeypatch):
    run_rig.records({"bo": {}})
    monkeypatch.setattr(reach, "ask", lambda shell, env=None, exe=None: reach.Reach(
        answered=True, session=True, seat="ada", addresses=frozenset({"ada"})))
    reach._ASKED.clear()
    r, seen = run_rig()
    assert r.exit_code == 0, r.output
    assert seen["agent"] == "ada"
    assert r.output.splitlines()[0] == f"bot:     ada ({SEAT_FROM})"


def test_bot_run_with_dc_agent_says_nothing_of_which(run_rig):
    run_rig.records({"bo": {}})
    r, seen = run_rig(DC_AGENT="Cy")
    assert r.exit_code == 0, r.output
    assert seen["agent"] == "cy"
    assert "bot:" not in r.output


def test_bot_run_with_nothing_recorded_is_claude_and_says_so(run_rig):
    r, seen = run_rig()
    assert r.exit_code == 0, r.output
    assert seen["agent"] == "claude"
    assert r.output.splitlines()[0] == "bot:     claude (the default)"


def test_bot_run_refuses_two_recorded_bots_and_no_name(run_rig):
    run_rig.records({"ab": {}, "cd": {}})
    r, seen = run_rig()
    assert r.exit_code == 2
    assert "records 2 bots (ab, cd)" in r.output
    assert "`DC_AGENT=NAME sessionphone bot run`" in r.output
    assert seen == {}

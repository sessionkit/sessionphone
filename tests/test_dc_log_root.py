"""dc-log and dc-log-web read and write under the bot's own root, never a persona's.

dc-log fell back to a fixed agent name when DC_AGENT and CLAUDE_CODE_AGENT were
unset, so a stranger's dc-log read another person's tree (~/.<name>/delta-chat).
dc-log-web wrote by default into a directory from the author's own setup, and
`dc-log-web --help` was not help: it ran the loop and wrote the page.

Now both follow the env contract (bin/dc-env, dc_env.py): the root is DC_ROOT,
else SESSIONPHONE_ROOT, else ~/.sessionphone. dc-log reads DC_LOG, else
<root>/bot.log. dc-log-web writes DC_WEB_OUT, else <root>/calllog.html, and
answers -h/--help without touching the disk. Every run here has the agent and
root variables scrubbed and HOME pointed at a tmp dir.
"""
import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
SCRUB = ("DC_AGENT", "CLAUDE_CODE_AGENT", "DC_ROOT", "SESSIONPHONE_ROOT", "DC_LOG",
         "DC_WEB_OUT", "PIP_CHAT_ROOT", "PIP_CHAT_LOG", "PIP_CHAT_AGENT")


def stranger_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def run(tool, home, *args, **extra):
    return subprocess.run([str(BIN / tool), *args], env=stranger_env(home, **extra),
                          cwd=home, capture_output=True, text=True, timeout=30, check=False)


def log_at(path, line):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line + "\n")
    return path


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    # a persona tree the old default would have read: it must never be touched
    log_at(h / ".carol/delta-chat/bot/bot.log", "PERSONA TREE")
    log_at(h / ".claude/delta-chat/bot/bot.log", "PERSONA TREE")
    return h


# --- dc-log: SC1 -------------------------------------------------------------

def test_dc_log_default_is_the_sessionphone_root(home):
    log_at(home / ".sessionphone/bot.log", "THE STRANGER'S OWN BOT")
    r = run("dc-log", home, "--raw")
    assert r.returncode == 0, r.stderr
    assert "THE STRANGER'S OWN BOT" in r.stdout
    assert "PERSONA TREE" not in r.stdout


def test_dc_log_with_no_log_names_the_contract_path_not_a_persona(home):
    r = run("dc-log", home)
    assert r.returncode == 1
    assert str(home / ".sessionphone/bot.log") in r.stderr
    assert "DC_LOG" in r.stderr and "DC_ROOT" in r.stderr
    assert "delta-chat" not in r.stderr and ".carol" not in r.stderr


def test_dc_log_ignores_the_agent_name(home):
    log_at(home / ".sessionphone/bot.log", "ROOT LOG")
    r = run("dc-log", home, "--raw", DC_AGENT="carol", CLAUDE_CODE_AGENT="someone")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ROOT LOG"


@pytest.mark.parametrize("var", ["SESSIONPHONE_ROOT", "DC_ROOT", "PIP_CHAT_ROOT"])
def test_dc_log_reads_under_a_named_root(home, tmp_path, var):
    root = tmp_path / "elsewhere"
    log_at(root / "bot.log", "NAMED ROOT")
    log_at(home / ".sessionphone/bot.log", "DEFAULT ROOT")
    r = run("dc-log", home, "--raw", **{var: str(root)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "NAMED ROOT"


def test_dc_root_beats_sessionphone_root_and_dc_log_beats_both(home, tmp_path):
    log_at(tmp_path / "dc/bot.log", "DC_ROOT")
    log_at(tmp_path / "sp/bot.log", "SESSIONPHONE_ROOT")
    log_at(tmp_path / "own.log", "DC_LOG")
    both = {"DC_ROOT": str(tmp_path / "dc"), "SESSIONPHONE_ROOT": str(tmp_path / "sp")}
    assert run("dc-log", home, "--raw", **both).stdout.strip() == "DC_ROOT"
    r = run("dc-log", home, "--raw", DC_LOG=str(tmp_path / "own.log"), **both)
    assert r.stdout.strip() == "DC_LOG"


def test_an_empty_dc_root_counts_as_unset(home):
    log_at(home / ".sessionphone/bot.log", "DEFAULT ROOT")
    r = run("dc-log", home, "--raw", DC_ROOT="", DC_LOG="")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "DEFAULT ROOT"


@pytest.mark.parametrize("extra", [
    {},
    {"SESSIONPHONE_ROOT": "/tmp/sp-root"},
    {"DC_ROOT": "/tmp/dc-root"},
    {"DC_ROOT": "/tmp/dc-root", "SESSIONPHONE_ROOT": "/tmp/sp-root"},
    {"DC_LOG": "/tmp/own/bot.log", "DC_ROOT": "/tmp/dc-root"},
])
def test_dc_log_names_the_same_log_as_dc_env(home, extra):
    """The shell contract and dc-log agree: one answer, never a third copy that drifts."""
    sh = subprocess.run(["bash", "-c", 'source "$1"; printf %s "$DC_LOG"', "_", str(BIN / "dc-env")],
                        env=stranger_env(home, **extra), capture_output=True, text=True,
                        timeout=30, check=True).stdout
    r = run("dc-log", home, **extra)     # no log there: the error names the path it read
    assert r.returncode == 1
    assert f"no log at {sh} " in r.stderr


# --- dc-log: the old default <root>/bot/bot.log (never silently empty) -------

def test_dc_log_reads_the_old_default_and_says_so_on_stderr(home):
    old = log_at(home / ".sessionphone/bot/bot.log", "THE OLD DEFAULT")
    new = home / ".sessionphone/bot.log"
    r = run("dc-log", home, "--raw")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "THE OLD DEFAULT"
    assert str(new) in r.stderr and str(old) in r.stderr
    assert "DC_LOG" in r.stderr
    assert not new.exists()             # it reads; it never moves or creates a log


def test_dc_log_header_names_the_old_file_it_read(home):
    old = log_at(home / ".sessionphone/bot/bot.log", "")
    r = run("dc-log", home)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0].startswith(f"- {old}  ")


def test_dc_log_old_default_follows_a_named_root(home, tmp_path):
    root = tmp_path / "bot-root"
    log_at(root / "bot/bot.log", "OLD UNDER DC_ROOT")
    r = run("dc-log", home, "--raw", DC_ROOT=str(root))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OLD UNDER DC_ROOT"
    assert str(root / "bot.log") in r.stderr


def test_dc_log_prefers_the_new_log_when_both_exist(home):
    log_at(home / ".sessionphone/bot/bot.log", "OLD")
    log_at(home / ".sessionphone/bot.log", "NEW")
    r = run("dc-log", home, "--raw")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "NEW"
    assert r.stderr == ""


def test_an_explicit_dc_log_is_never_swapped_for_the_old_default(home, tmp_path):
    log_at(tmp_path / "bot/bot.log", "OLD BESIDE DC_LOG")
    log_at(home / ".sessionphone/bot/bot.log", "OLD UNDER THE ROOT")
    r = run("dc-log", home, "--raw", DC_LOG=str(tmp_path / "bot.log"))
    assert r.returncode == 1
    assert f"no log at {tmp_path / 'bot.log'} " in r.stderr
    assert r.stdout == ""


def test_dc_log_help_exits_0(home):
    for flag in ("-h", "--help"):
        r = run("dc-log", home, flag)
        assert r.returncode == 0
        assert "dc-log" in r.stdout and "DC_LOG" in r.stdout


# --- dc-log-web: SC2 ---------------------------------------------------------

def test_dc_log_web_help_prints_usage_exits_0_and_writes_nothing(home):
    before = sorted(p.relative_to(home) for p in home.rglob("*") if "Library" not in p.parts)
    for flag in ("-h", "--help"):
        r = run("dc-log-web", home, flag)
        assert r.returncode == 0, r.stderr
        assert "usage: dc-log-web" in r.stdout
        assert "DC_WEB_OUT" in r.stdout
    assert not (home / ".sessionphone").exists()
    assert not (home / ".sessionphone/calllog.html").exists()
    assert not (home / ".show-me").exists()
    after = sorted(p.relative_to(home) for p in home.rglob("*") if "Library" not in p.parts)
    assert after == before


def test_dc_log_web_unknown_arg_exits_nonzero_and_writes_nothing(home):
    r = run("dc-log-web", home, "--bogus")
    assert r.returncode != 0
    assert "--bogus" in r.stderr
    assert not (home / ".sessionphone").exists()


def test_dc_log_web_default_page_is_under_the_bot_root(home):
    log_at(home / ".sessionphone/bot.log", "")
    r = run("dc-log-web", home, "--once")
    assert r.returncode == 0, r.stderr
    page = home / ".sessionphone/calllog.html"
    assert r.stdout.strip() == str(page)
    assert "<title>call log</title>" in page.read_text()
    assert not (home / ".show-me").exists()
    assert not (BIN / "__pycache__").exists()


def test_dc_log_web_makes_a_missing_bot_root_private(home):
    """The default page lives in the bot's root, which holds the keys: a root
    dc-log-web has to create is 0700, never world-readable."""
    r = run("dc-log-web", home, "--once")
    assert r.returncode == 0, r.stderr
    assert (home / ".sessionphone").stat().st_mode & 0o777 == 0o700


def test_dc_log_web_follows_dc_root(home, tmp_path):
    root = tmp_path / "bot"
    r = run("dc-log-web", home, "--once", DC_ROOT=str(root))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(root / "calllog.html")
    assert (root / "calllog.html").exists()
    assert not (home / ".sessionphone").exists()


def test_dc_web_out_overrides(home, tmp_path):
    out = tmp_path / "served/calllog.html"
    r = run("dc-log-web", home, "--once", DC_WEB_OUT=str(out))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(out)
    assert out.exists()
    assert not (home / ".sessionphone").exists()

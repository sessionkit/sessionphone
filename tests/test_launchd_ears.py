"""The launchd bot sees an ears installed as a uv tool (PHONE-60, half 1).

A walk-through on a fresh Mac (F4, 2030-06-09): with `uv tool install ears` done,
`sessionphone bot install` started a bot whose bot.log said "ears is not
installed (No module named 'ears'): calls work, with no turn previews".

ears is a uv tool: its own venv under ~/.local/share/uv/tools/<dist> (the
distribution is `sessionvoice` since 2030-06-09, not `ears`), its shim linked
from ~/.local/bin. The bot runs the plugin venv's interpreter with the job's
EnvironmentVariables and nothing else, so before fix-001 ears was in neither
its PATH (service.BASE_PATH) nor its sys.path. fix-001 (rca-001): `bot install`
finds the tool by its shim and carries its site-packages as DC_EARS_SRC,
which bridge/calls.py adds to sys.path after the bot's own imports. Never
PYTHONPATH: it puts the tool's compiled dependencies ahead of the bot's.

These tests build the job the way `bot install` does (service.job) and run
the job's own interpreter under the job's own environment, plus the HOME
launchd gives a user agent and nothing from this shell, then ask the bot's
own import (bridge/calls.py, EARS_MISSING) whether it found ears. The uv tool
is a fake one, laid out where `uv tool install` puts it, in a scratch HOME:
no real ears, no uv, no launchctl, no bot.
"""
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from sessionphone import service

# the bot's own import, its own verdict: None means it found ears
ASK = "from sessionphone.bridge import calls; print('MISSING=' + repr(calls.EARS_MISSING))"


def fake_uv_tool_ears(home: pathlib.Path, dist: str = "sessionvoice", bindir: pathlib.Path | None = None,
                      package: bool = True) -> pathlib.Path:
    """ears as `uv tool install` leaves it, under HOME: a venv at
    ~/.local/share/uv/tools/<dist> holding the package, and its shim linked
    from ~/.local/bin (or BINDIR). The dist is named `sessionvoice`, as the
    real one is, so the lookup is proven name-free. PACKAGE False leaves the
    ears package out (a shim called ears from some other tool). -> the tool's venv."""
    tool = home / ".local" / "share" / "uv" / "tools" / dist
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tool)], check=True)
    site = next(tool.glob("lib/python3*/site-packages"))
    pkg = site / ("ears" if package else "other")
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""a stand-in ears (PHONE-60 tests)."""\n')
    (pkg / "previews.py").write_text(textwrap.dedent("""\
        class Previews:
            FAKE = "uv tool ears"

            def on_phrase(self, text, now):
                return None

            def on_turn(self, text, now):
                return None

            def tick(self, now):
                return None
        """))
    shim = tool / "bin" / "ears"
    shim.write_text(f"#!{tool / 'bin' / 'python'}\nfrom ears import previews\n")
    shim.chmod(0o755)
    bindir = bindir or home / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "ears").symlink_to(shim)
    return tool


def site_of(tool: pathlib.Path) -> str:
    return str(next(tool.glob("lib/python3*/site-packages")))


def run_as_launchd(job: dict, home: pathlib.Path) -> subprocess.CompletedProcess:
    """The job's interpreter, in the job's directory, with the job's env and
    the HOME (and TMPDIR) launchd gives a user agent: nothing of this shell."""
    env = dict(job["EnvironmentVariables"])
    env["HOME"] = str(home)
    env["TMPDIR"] = os.environ.get("TMPDIR", "/tmp")
    python = job["ProgramArguments"][0]
    return subprocess.run([python, "-c", ASK], env=env, cwd=job["WorkingDirectory"],
                          capture_output=True, text=True, timeout=120, check=False)


@pytest.fixture
def the_job(monkeypatch, tmp_path):
    """The job `bot install` writes, for a user whose HOME is tmp_path/home.
    Its interpreter is this suite's venv python: the plugin venv's stand-in,
    with sessionphone in it and no ears (as the plugin's own venv has none)."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.delenv("DC_AGENT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_AGENT", raising=False)
    for k in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "XDG_DATA_HOME"):    # uv's bin dir is ~/.local/bin here
        monkeypatch.delenv(k, raising=False)
    return service.job("agent", python=sys.executable), home


def bot_install_job() -> dict:
    """The job `bot install` would write now, after the fake tool is in place."""
    return service.job("agent", python=sys.executable)


def missing(r: subprocess.CompletedProcess) -> str:
    assert r.returncode == 0, f"the bot's import crashed under the job env:\n{r.stderr}"
    line = [x for x in r.stdout.splitlines() if x.startswith("MISSING=")]
    assert line, f"no verdict from the bot's import:\n{r.stdout}\n{r.stderr}"
    return line[-1].removeprefix("MISSING=")


def test_the_suite_venv_has_no_ears_of_its_own(the_job):
    """The rig's premise: without the uv tool, the job's interpreter cannot
    import ears. If it could, the repro below would pass for the wrong reason."""
    job, home = the_job
    assert missing(run_as_launchd(job, home)) != "None"


def test_the_fake_uv_tool_ears_is_importable_when_on_the_path(the_job):
    """The rig's other premise: the stand-in ears is a real package. Put its
    site-packages on PYTHONPATH by hand and the bot's import finds it, so the
    repro below fails on the job's env, not on a broken fake."""
    job, home = the_job
    tool = fake_uv_tool_ears(home)
    job["EnvironmentVariables"]["PYTHONPATH"] = str(next(tool.glob("lib/python3*/site-packages")))
    assert missing(run_as_launchd(job, home)) == "None"


def test_without_ears_the_bot_still_starts_and_says_so(the_job):
    """Unchanged by PHONE-60: no ears anywhere, the bot still starts and its
    import says why (EARS_MISSING). Since PHONE-152 bot.log is silent about it
    by default: calls.setup logs calls.ears_missing_line() only when
    DC_PREVIEWS=1 or DC_EARS=1 asked for what ears provides, and that line
    names the feature that is off."""
    job, home = the_job
    assert "No module named 'ears'" in missing(run_as_launchd(job, home))


def test_the_launchd_bot_imports_ears_installed_as_a_uv_tool(the_job):
    """F4 half 1. With ears installed as a uv tool for this user, the bot that
    `bot install` sets up imports it: EARS_MISSING is None, so
    calls.ears_missing_line() is None and bot.log carries no ears line, even
    under DC_PREVIEWS=1 or DC_EARS=1. Failed before fix-001: No module named 'ears'."""
    _, home = the_job
    tool = fake_uv_tool_ears(home)
    job = bot_install_job()
    env = job["EnvironmentVariables"]
    assert env.get("DC_EARS_SRC") == site_of(tool), env
    assert "PYTHONPATH" not in env, "PYTHONPATH shadows the bot's own numpy with the tool's (rca-001)"
    assert env["PATH"] == service.BASE_PATH
    assert missing(run_as_launchd(job, home)) == "None", (
        f"the launchd bot cannot see the uv-tool ears; job env={env}")


def test_the_tool_cannot_shadow_what_the_bot_already_imported(the_job):
    """Why DC_EARS_SRC and not PYTHONPATH (rca-001, row 3): a tool built on
    another Python carries compiled modules the bot cannot load. calls.py
    imports numpy before it adds DC_EARS_SRC, so a numpy in the tool's
    site-packages is never the one the bot runs."""
    _, home = the_job
    tool = fake_uv_tool_ears(home)
    trap = pathlib.Path(site_of(tool)) / "numpy"
    trap.mkdir()
    (trap / "__init__.py").write_text('raise ImportError("the uv tool\'s numpy, not the bot\'s")\n')
    assert missing(run_as_launchd(bot_install_job(), home)) == "None"


def test_the_shim_is_found_in_uv_tool_bin_dir(the_job, monkeypatch, tmp_path):
    """uv links its shims into UV_TOOL_BIN_DIR when that is set, and the
    lookup reads it the way uv does."""
    _, home = the_job
    bindir = tmp_path / "uvbin"
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bindir))
    tool = fake_uv_tool_ears(home, bindir=bindir)
    job = bot_install_job()
    assert job["EnvironmentVariables"].get("DC_EARS_SRC") == site_of(tool)
    assert missing(run_as_launchd(job, home)) == "None"


def test_the_operators_dc_ears_src_is_kept(the_job, monkeypatch, tmp_path):
    """DC_EARS_SRC set in the installing shell (an ears checkout, for
    development) is carried as it is; the uv tool does not replace it."""
    _, home = the_job
    fake_uv_tool_ears(home)
    mine = str(tmp_path / "ears-checkout" / "src")
    monkeypatch.setenv("DC_EARS_SRC", mine)
    assert bot_install_job()["EnvironmentVariables"]["DC_EARS_SRC"] == mine


def test_a_shim_from_a_tool_with_no_ears_package_carries_nothing(the_job):
    """A shim called ears whose tool has no ears package: nothing is carried,
    and the bot still starts without ears (EARS_MISSING says why). bot.log
    says so only if DC_PREVIEWS=1 or DC_EARS=1 asked for ears
    (calls.ears_missing_line(), PHONE-152)."""
    _, home = the_job
    fake_uv_tool_ears(home, package=False)
    job = bot_install_job()
    assert "DC_EARS_SRC" not in job["EnvironmentVariables"]
    assert "No module named 'ears'" in missing(run_as_launchd(job, home))


def test_a_shim_that_is_no_link_carries_nothing(the_job):
    """A plain-file `ears` in the bin dir (not a uv tool's link): there is no
    tool beside it to read, so nothing is carried."""
    _, home = the_job
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "ears").write_text("#!/bin/sh\nexit 0\n")
    assert service.ears_src() is None
    assert "DC_EARS_SRC" not in bot_install_job()["EnvironmentVariables"]


def test_no_ears_tool_carries_nothing(the_job):
    """Unchanged without ears: the job carries no DC_EARS_SRC."""
    job, _ = the_job
    assert "DC_EARS_SRC" not in job["EnvironmentVariables"]

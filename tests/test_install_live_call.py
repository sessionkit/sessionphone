"""PHONE-250: `bot install` must not drop a live call.

While a call is up the bot writes call-live.json (bridge/calls.py LIVE_FILE,
DC_LIVE overrides its path) and removes it at hang-up. `bot install` booted the
job out (service.install, "a reinstall replaces the old job") with no look at
that file, so a reinstall on a user's Mac dropped call 1 with no warning
(Sun 2030-06-09 in this repo's test time). `bot uninstall` booted it out too.

repro-001 wrote the two refusal tests as strict xfails; fix-001 (the guard,
service.check_no_live_call) removed the marks. A call is live only when the bot
that wrote the file still runs (its bot.pid, as dc-live judges it) and started
before the call (findings/f01-rca.md: a bot stopped mid-call leaves the file
for the next bot). So a live call here has a live bot behind it: a sleeping
stand-in whose command line names sessionphone, with the bot.pid the bot writes
for it, started in its own session and stopped by killing that process group,
never by a pattern. launchd here is tests/conftest.py's FakeLaunchd, never the
real one.
"""
import contextlib
import json
import os
import pathlib
import plistlib
import signal
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from sessionphone import service
from sessionphone.bridge import dc_env, pidfile
from sessionphone.cli import main as cli

REFUSAL = "a call is live; hang up or pass --force"
SINCE = 1907272800.0        # Sun 2030-06-09: after any stand-in's start


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    for k in [k for k in os.environ if k.startswith("DC_")]:
        monkeypatch.delenv(k)
    given_seat("claude-chat")
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    callers = tmp_path / "callers.txt"
    callers.write_text("owner@phone.example.org\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    chans = tmp_path / "channels.txt"
    chans.write_text("owner  owner@phone.example.org\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    live = tmp_path / "call-live.json"
    monkeypatch.setenv("DC_LIVE", str(live))
    launchd.live = live
    launchd.root = tmp_path / "root"
    launchd.root.mkdir(mode=0o700)
    launchd.pid_file = launchd.root / "bot.pid"     # <home>/bot.pid: the default profile's home is the root
    return launchd


def call_up(live, since=SINCE):
    """The file calls.py's _live_write leaves while a call is up."""
    live.write_text(json.dumps({"call_id": 7, "chat_id": 12, "dir": "call-7",
                                "since": since, "caller": "owner@phone.example.org"}))


@contextlib.contextmanager
def stand_in(argv=None, shows="sessionphone.bridge.bot claude"):
    """A sleeping process (ARGV, else a python carrying the bot's argv) whose
    command line shows SHOWS, in its own session, so its own process group:
    stopped by killing that group, never by a pattern."""
    argv = argv or [sys.executable, "-c", "import time; time.sleep(120)", "sessionphone.bridge.bot", "claude"]
    p = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while shows not in _command(p.pid):
            assert p.poll() is None, f"stand-in exited {p.returncode}"
            assert time.monotonic() < deadline, "the stand-in never showed its argv"
            time.sleep(0.02)
        yield p
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=10)


def _command(pid):
    try:
        return pidfile.ps(pid, "command")
    except OSError:
        return ""


@pytest.fixture
def bot(rig):
    """The bot runs on the rig's root: a stand-in, and the bot.pid the bot
    writes for it (pidfile.write, the bot's own writer)."""
    with stand_in() as p:
        assert pidfile.write(rig.pid_file, rig.root, log=print, pid=p.pid)
        yield p


def test_the_bot_reads_the_live_file_this_rig_writes(rig):
    """The rig's DC_LIVE is the path the bot writes: dc_env.path, read at call time."""
    assert dc_env.path("LIVE", "call-live.json") == rig.live


def test_no_call_install_boots_out_and_reinstalls_as_before(rig):
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert "bootout" in rig.verbs() and rig.loaded


def test_install_during_a_live_call_refuses_and_restarts_nothing(rig, bot):
    call_up(rig.live)
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert "bootout" not in rig.verbs(), "the live call was dropped: install booted the bot out"
    assert r.exit_code == 2, r.output
    assert REFUSAL in r.output
    assert "call msg 7 - chat 12 with owner@phone.example.org" in r.output
    assert not (rig.root.parent / "job.plist").exists(), "a refused install wrote the plist"


def test_uninstall_during_a_live_call_refuses_and_stops_nothing(rig, bot):
    call_up(rig.live)
    r = CliRunner().invoke(cli, ["bot", "uninstall"])
    assert "bootout" not in rig.verbs(), "the live call was dropped: uninstall booted the bot out"
    assert r.exit_code == 2, r.output
    assert REFUSAL in r.output


# ---- --force: the person asked for the drop -----------------------------------

def test_install_force_restarts_during_a_live_call(rig, bot):
    call_up(rig.live)
    r = CliRunner().invoke(cli, ["bot", "install", "--force"])
    assert r.exit_code == 0, r.output
    assert "bootout" in rig.verbs() and rig.loaded
    assert REFUSAL not in r.output


def test_uninstall_force_stops_during_a_live_call(rig, bot):
    call_up(rig.live)
    r = CliRunner().invoke(cli, ["bot", "uninstall", "--force"])
    assert r.exit_code == 0, r.output
    assert rig.verbs() == ["bootout", "bootout"]


def test_uninstall_with_no_call_stops_as_before(rig, bot):
    r = CliRunner().invoke(cli, ["bot", "uninstall"])
    assert r.exit_code == 0, r.output
    assert rig.verbs() == ["bootout", "bootout"]


# ---- a stale file is no call ---------------------------------------------------

def test_a_live_file_with_no_bot_pid_is_stale(rig):
    """The bot that wrote it is gone and none has started since: no call."""
    call_up(rig.live)
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert "bootout" in rig.verbs()


def test_a_live_file_whose_bot_is_dead_is_stale(rig):
    with stand_in() as p:
        assert pidfile.write(rig.pid_file, rig.root, log=print, pid=p.pid)
    call_up(rig.live)           # the stand-in is gone, its bot.pid left (a SIGTERM skips atexit)
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert "bootout" in rig.verbs()


def test_a_live_file_older_than_the_running_bot_is_stale(rig, bot):
    """A bot stopped mid-call leaves the file; the next bot runs beside it and
    never removes it. The call is older than the bot, so it is not this bot's."""
    start, why = pidfile.started(rig.pid_file, rig.root)
    assert why == "" and start is not None
    call_up(rig.live, since=start - 60)
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert "bootout" in rig.verbs()


def test_another_roots_bot_is_no_bot_here(rig, tmp_path):
    with stand_in() as p:
        other = tmp_path / "other-root"
        other.mkdir()
        assert pidfile.write(rig.pid_file, other, log=print, pid=p.pid)
        call_up(rig.live)
        r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert "bootout" in rig.verbs()


def test_an_unreadable_file_beside_a_running_bot_refuses(rig, bot):
    """A call may be connecting (the bot writes the file in place): the doubt refuses."""
    rig.live.write_text('{"call_id": 7, "chat')
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2, r.output
    assert REFUSAL in r.output and "cannot be read" in r.output
    assert "bootout" not in rig.verbs()


# ---- the files are the RUNNING job's, from its plist ---------------------------

def _plist_with(path: pathlib.Path, env: dict) -> None:
    with open(path, "wb") as fh:
        plistlib.dump({"Label": "x", "EnvironmentVariables": env}, fh)


def test_the_installed_jobs_plist_names_the_files(rig, tmp_path, monkeypatch):
    """The bot runs with its plist's environment, not this shell's: a reinstall
    that moves the root must still see the call on the old root."""
    old = tmp_path / "old-root"
    old.mkdir()
    _plist_with(tmp_path / "job.plist", {"SESSIONPHONE_ROOT": str(old)})
    with stand_in() as p:
        assert pidfile.write(old / "bot.pid", old, log=print, pid=p.pid)
        call_up(old / "call-live.json")
        r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2, r.output
    assert REFUSAL in r.output and str(old / "call-live.json") in r.output
    assert "bootout" not in rig.verbs()


def test_check_judges_every_job_it_would_boot_out(rig, tmp_path):
    """--replace rivals and the legacy job are booted out too: a call on any
    of them refuses, each judged on its own plist."""
    quiet, busy = tmp_path / "quiet", tmp_path / "busy"
    for r in (quiet, busy):
        r.mkdir()
    _plist_with(tmp_path / "a.plist", {"SESSIONPHONE_ROOT": str(quiet)})
    _plist_with(tmp_path / "b.plist", {"SESSIONPHONE_ROOT": str(busy), "DC_PROFILE": "public"})
    (busy / "public").mkdir()
    with stand_in() as p:
        assert pidfile.write(busy / "public" / "bot.pid", busy, log=print, pid=p.pid)
        call_up(busy / "public" / "call-live.json")
        with pytest.raises(service.CallLive, match="a call is live; hang up or pass --force"):
            service.check_no_live_call([tmp_path / "a.plist", tmp_path / "none.plist", tmp_path / "b.plist"], {})
        service.check_no_live_call([tmp_path / "a.plist"], {})          # the quiet one alone: no call
        service.check_no_live_call([tmp_path / "b.plist"], {}, force=True)


# ---- pidfile.started: dc-live's rule, in Python --------------------------------

def test_started_gives_a_running_bots_start_time(tmp_path):
    with stand_in() as p:
        f = tmp_path / "bot.pid"
        assert pidfile.write(f, tmp_path, log=print, pid=p.pid)
        start, why = pidfile.started(f, tmp_path)
        assert why == ""
        assert abs(start - time.time()) < 120


def test_started_refuses_what_dc_live_refuses(tmp_path):
    f = tmp_path / "bot.pid"
    assert pidfile.started(f, tmp_path) == (None, f"no {f}: no bot has started on this root")
    f.write_text("1\n")
    assert "names no bot's pid" in pidfile.started(f, tmp_path)[1]
    with stand_in(["/bin/sleep", "120"], shows="sleep 120") as p:     # no python: its path may name the repo
        assert pidfile.write(f, tmp_path, log=print, pid=p.pid)
        assert "no sessionphone bot" in pidfile.started(f, tmp_path)[1]
    with stand_in() as p:
        assert pidfile.write(f, tmp_path, log=print, pid=p.pid)
        lines = f.read_text().split("\n")
        f.write_text("\n".join([lines[0], "Sun Jun  9 00:00:00 2030", *lines[2:]]))
        assert "another process now" in pidfile.started(f, tmp_path)[1]

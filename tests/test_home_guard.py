"""The suite never changes the real home (PHONE-264).

Run alone, tests/test_callers_on_install.py and tests/test_avatar.py wrote the real
~/.sessionphone/.bot.lock: their first test imports the bot itself and runs
bot.main(), after the autouse home_lock_is_tmp had looked for a loaded bot and
found none, and dc_env.ROOT, bound at import, was the real root.

tests/conftest.py now (1) moves HOME to tmp before any sessionphone import, (2)
gives every test its own tmp HOME and SESSIONPHONE_ROOT, and (3) fails the run if
the real home's sessionphone paths changed (tests/_home_guard.py). These prove
each, and prove the guard fires, all on FAKE homes: nothing here reads or writes
the real one beyond the guard's own stat.
"""
import os
import pathlib
import pwd
import subprocess
import sys

import _home_guard
import pytest

from sessionphone import paths
from sessionphone.bridge import dc_env

REPO = pathlib.Path(__file__).resolve().parents[1]
PROBE = "tests/_home_guard_probe.py"
LEAKED = ("tests/test_callers_on_install.py::test_a_bot_nobody_may_ring_says_so_at_start",
          "tests/test_avatar.py::test_the_bot_sets_the_avatar_and_acks_the_blob_path")


STATE = pathlib.Path(".sessionkit", "phone", "state")    # the default root below a home (paths.DEFAULT_ROOT)


def _pw_home() -> pathlib.Path:
    return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)


# ---- (1) and (2): HOME and the root are tmp, for the binds at import and per test ----

def test_the_root_bound_at_import_is_not_the_real_home(suite_home):
    assert dc_env.ROOT == pathlib.Path(suite_home) / STATE
    assert dc_env.ROOT not in (_pw_home() / STATE, _pw_home() / ".sessionphone")


_SEEN_HOMES = []


@pytest.mark.parametrize("n", [1, 2])
def test_each_test_has_its_own_tmp_home_and_root(n, home_is_tmp, suite_home):
    home = pathlib.Path(os.environ["HOME"])
    assert home == home_is_tmp
    assert home != _pw_home() and home != pathlib.Path(suite_home)
    assert pathlib.Path(os.path.expanduser("~")) == home
    assert pathlib.Path(os.environ["SESSIONPHONE_ROOT"]) == home / STATE
    assert paths.root() == home / STATE
    _SEEN_HOMES.append(home)
    if n == 2:
        assert len(set(_SEEN_HOMES)) == len(_SEEN_HOMES)


def test_a_child_process_inherits_the_tmp_home(home_is_tmp):
    out = subprocess.run([sys.executable, "-c", "import os; print(os.path.expanduser('~'))"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == str(home_is_tmp)


# ---- (3) the snapshot and its diff, on a fake home ----------------------------------

def test_a_write_under_the_root_is_seen(tmp_path):
    home = tmp_path / "fake-home"
    (home / ".sessionphone" / "bin").mkdir(parents=True)
    (home / ".sessionphone" / "bot.log").write_text("old\n")
    before = _home_guard.snapshot(home)
    assert _home_guard.changes(before, _home_guard.snapshot(home)) == []
    lock = home / ".sessionphone" / ".bot.lock"
    lock.write_text("52484\n")
    lines = _home_guard.changes(before, _home_guard.snapshot(home))
    assert f"+ {lock}" in lines
    assert any(x.startswith(f"~ {home / '.sessionphone'} (mtime") for x in lines)   # the dir's own too


def test_a_change_a_removal_and_a_mode_are_seen(tmp_path):
    home = tmp_path / "fake-home"
    root = home / ".sessionphone"
    root.mkdir(parents=True)
    log, gone = root / "bot.log", root / "bot.pid"
    log.write_text("a\n")
    gone.write_text("1\n")
    before = _home_guard.snapshot(home)
    log.write_text("a longer line\n")
    gone.unlink()
    root.chmod(0o700)
    lines = _home_guard.changes(before, _home_guard.snapshot(home))
    assert any(x.startswith(f"~ {log} (") and "size" in x for x in lines)
    assert f"- {gone}" in lines
    assert any(x.startswith(f"~ {root} (") and "mode" in x for x in lines)


def test_a_root_made_where_there_was_none_is_seen(tmp_path):
    home = tmp_path / "fake-home"
    home.mkdir()
    before = _home_guard.snapshot(home)
    assert before == {}
    (home / ".sessionphone").mkdir()
    assert _home_guard.changes(before, _home_guard.snapshot(home)) == [f"+ {home / '.sessionphone'}"]


def test_the_kit_folder_and_the_bot_plists_are_watched_and_nothing_else(tmp_path):
    home = tmp_path / "fake-home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    before = _home_guard.snapshot(home)
    (agents / "com.example.other.plist").write_text("x")            # not the bot's: not watched
    (home / ".sessionkit" / "mail").mkdir(parents=True)              # another package: not watched
    (home / "elsewhere").write_text("x")
    assert _home_guard.changes(before, _home_guard.snapshot(home)) == []
    plist = agents / "dev.sessionkit.phone.bot.ada.plist"
    old = agents / "com.sessionphone.bot.ada.plist"
    phone_json = home / ".sessionkit" / "phone" / "settings" / "phone.json"
    phone_json.parent.mkdir(parents=True)
    for p in (plist, old, phone_json):
        p.write_text("x")
    lines = _home_guard.changes(before, _home_guard.snapshot(home))
    for p in (plist, old, phone_json):
        assert f"+ {p}" in lines


def test_a_symlink_in_the_root_is_not_followed(tmp_path):
    home, outside = tmp_path / "fake-home", tmp_path / "outside"
    (home / ".sessionphone").mkdir(parents=True)
    outside.mkdir()
    (home / ".sessionphone" / "link").symlink_to(outside)
    before = _home_guard.snapshot(home)
    (outside / "f").write_text("x")
    assert _home_guard.changes(before, _home_guard.snapshot(home)) == []


def test_the_report_names_each_change_and_the_rule(tmp_path):
    text = _home_guard.report(tmp_path, ["+ /h/.sessionphone/.bot.lock"])
    assert "+ /h/.sessionphone/.bot.lock" in text and "PHONE-264" in text and str(tmp_path) in text


# ---- the guard end to end: a child pytest whose "real" home is a fake -----------------

def _child(home, *tests, **env):
    e = {k: v for k, v in os.environ.items() if k not in ("SESSIONPHONE_ROOT", "HOME_GUARD_PROBE_WRITE")}
    e.update(HOME=str(home), **env)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
                          cwd=REPO, env=e, capture_output=True, text=True, timeout=300, check=False)


def _fake_real_home(tmp_path):
    """A fake home shaped like the one this was found on: a stale lock, bin/, a log."""
    home = tmp_path / "real-home"
    root = home / ".sessionphone"
    (root / "bin").mkdir(parents=True)
    (root / ".bot.lock").write_text("52484\n")
    (root / "bot.log").write_text("old\n")
    return home


def test_the_guard_fails_a_green_run_that_wrote_the_real_root(tmp_path):
    home = _fake_real_home(tmp_path)
    lock = home / ".sessionphone" / ".bot.lock"
    r = _child(home, PROBE, HOME_GUARD_PROBE_WRITE=str(lock))
    out = r.stdout + r.stderr
    assert "1 passed" in r.stdout, out[-3000:]           # the test itself passed...
    assert r.returncode == 1, out[-3000:]                # ...and the run failed
    assert "changed the REAL home" in out and f"~ {lock} (" in out, out[-3000:]


def test_the_guard_names_a_root_made_from_nothing(tmp_path):
    home = tmp_path / "real-home"
    home.mkdir()
    lock = home / ".sessionphone" / ".bot.lock"
    r = _child(home, PROBE, HOME_GUARD_PROBE_WRITE=str(lock))
    out = r.stdout + r.stderr
    assert r.returncode == 1, out[-3000:]
    assert f"+ {home / '.sessionphone'}" in out and f"+ {lock}" in out, out[-3000:]


def test_the_guard_passes_a_run_that_left_the_real_home_alone(tmp_path):
    home = _fake_real_home(tmp_path)
    before = _home_guard.snapshot(home)
    r = _child(home, PROBE)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    assert "changed the REAL home" not in r.stdout
    assert _home_guard.snapshot(home) == before


def test_the_tests_that_leaked_run_alone_leave_the_real_home_alone(tmp_path):
    """The receipt's two tests, each the first test of its own child run (so the bot
    is NOT loaded when the autouse fixtures run, as when found): green, and the fake
    real home is byte for byte as it was."""
    home = _fake_real_home(tmp_path)
    before = _home_guard.snapshot(home)
    for test in LEAKED:
        r = _child(home, test)
        assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    assert _home_guard.changes(before, _home_guard.snapshot(home)) == []

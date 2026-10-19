"""bin/bridge-check gives the same verdict whatever the caller's TMPDIR (PHONE-143).

The call legs' tests bind unix sockets under tempfile.mkdtemp(), that is under
TMPDIR, and macOS caps a socket path (sun_path) at 104 bytes. A long TMPDIR
(a Claude Code scratchpad) turned the check RED with 23 failures that looked
like a regression. bridge-check now makes a short scratch root under /tmp and
hands the tests TMPDIR inside it, whatever the caller's TMPDIR says.

Each test runs bridge-check on a scratch copy: its bin/, the real src and venv,
and a tests/bridge/ holding only the stand-in tests written here. A stand-in
records the TMPDIR it was given, so the test can see that the dir is gone after.
"""
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIMIT = 104                       # macOS sun_path, the NUL included

# Binds a unix socket under tempfile.mkdtemp(), as test_earleg/test_mouthleg do,
# and leaves the dir behind, as they do. Records where its TMPDIR was.
BINDS_UNDER_TMPDIR = '''
import os, socket, tempfile, pathlib
pathlib.Path(os.environ["SEEN"]).write_text(tempfile.gettempdir())
d = tempfile.mkdtemp(prefix="el-")
s = socket.socket(socket.AF_UNIX); s.bind(os.path.join(d, "s.sock")); s.close()
'''

# Binds on a path over the limit whatever TMPDIR says: the case the check must
# name in words, not report as a regression.
BINDS_TOO_LONG = '''
import os, socket, tempfile, pathlib
pathlib.Path(os.environ["SEEN"]).write_text(tempfile.gettempdir())
d = pathlib.Path(tempfile.mkdtemp(prefix="long-")) / ("x" * 120)
d.mkdir()
s = socket.socket(socket.AF_UNIX); s.bind(str(d / "s.sock"))
'''

# Records its TMPDIR, then waits to be killed.
WAITS = '''
import os, tempfile, time, pathlib
pathlib.Path(os.environ["SEEN"]).write_text(tempfile.gettempdir())
time.sleep(60)
'''


@pytest.fixture
def rig(tmp_path):
    if not (REPO / ".venv" / "bin" / "python").exists():
        pytest.skip("no .venv: run `uv sync`")
    copy = tmp_path / "repo"
    (copy / "bin").mkdir(parents=True)
    for tool in ("bridge-check", "dc-env"):
        shutil.copy2(REPO / "bin" / tool, copy / "bin" / tool)
    (copy / "src").symlink_to(REPO / "src")
    (copy / ".venv").symlink_to(REPO / ".venv")
    (copy / "tests" / "bridge").mkdir(parents=True)
    long_tmp = tmp_path / ("t" * 100)
    long_tmp.mkdir()
    assert len(str(long_tmp)) >= 90
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_", "SESSIONPHONE_"))}
    env.update(HOME=str(tmp_path / "home"), DC_ROOT=str(tmp_path / "root"), TMPDIR=str(long_tmp),
               SEEN=str(tmp_path / "seen"))
    return copy, env, long_tmp, tmp_path / "seen"


def stand_in(copy, body, name="test_stand_in.py"):
    (copy / "tests" / "bridge" / name).write_text(body)


def check(copy, env):
    return subprocess.run([str(copy / "bin" / "bridge-check")], env=env, capture_output=True,
                          text=True, check=False, timeout=120)


def test_a_long_tmpdir_is_all_green_and_leaves_nothing(rig):
    copy, env, long_tmp, seen = rig
    stand_in(copy, BINDS_UNDER_TMPDIR)
    r = check(copy, env)
    assert r.returncode == 0 and "ALL GREEN" in r.stdout, r.stdout + r.stderr
    given = seen.read_text()
    assert given.startswith("/tmp/dcc.") and len(given) + len("/el-xxxxxxxx/s.sock") < LIMIT
    assert not Path(given).parent.exists()          # the scratch root went, with what the test left
    assert list(long_tmp.iterdir()) == []           # and nothing landed in the caller's TMPDIR


def test_an_over_long_socket_path_is_said_in_words(rig):
    copy, env, _, seen = rig
    stand_in(copy, BINDS_TOO_LONG)
    r = check(copy, env)
    assert r.returncode == 1 and "RED - do not commit" in r.stdout, r.stdout + r.stderr
    assert "FAIL: a unix socket path is over the 104-byte limit" in r.stdout
    assert "not a code regression" in r.stdout
    assert "Traceback" not in r.stdout and "OSError" not in r.stdout
    assert not Path(seen.read_text()).parent.exists()   # removed on a failing run too


def test_an_ordinary_failure_is_not_blamed_on_the_path(rig):
    copy, env, _, _ = rig
    stand_in(copy, "raise SystemExit('a real failure')\n")
    r = check(copy, env)
    assert r.returncode == 1 and "a real failure" in r.stdout
    assert "104-byte" not in r.stdout


def test_a_killed_run_removes_its_scratch_root(rig):
    copy, env, _, seen = rig
    stand_in(copy, WAITS)
    p = subprocess.Popen([str(copy / "bin" / "bridge-check")], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not seen.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert seen.exists(), "the stand-in never started"
        root = Path(seen.read_text()).parent
        assert root.is_dir()
        os.killpg(p.pid, signal.SIGTERM)                # this run's own group only
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
    assert p.returncode != 0
    assert not root.exists()

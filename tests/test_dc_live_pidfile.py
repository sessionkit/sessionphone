"""dc-live knows the bot runs from the bot's own pid file, never from argv text.

dc-live used to ask `pgrep -f "$DC_BOT"` (DC_BOT: `sessionphone.bridge.bot
<agent>`), an unanchored regex over every process's argv. Measured, that was
wrong in five of six cases: any argv carrying the string counted, `.` matched
any character, the foreground `sessionphone bot run` never carried it, and a
bot on another root counted too. Now the bot writes <root>/bot.pid (pid, start
time, command line, root; bridge/pidfile.py) and dc-live checks it.

The first six tests are the six measured cases, one each. Each one fails on
the pgrep check. The stand-ins are sleeping processes with the argv of the
case, as in the measurement; each is started in its own session and stopped
by killing its own process group, never by a pattern. A stand-in that plays
a bot gets a bot.pid written by `_record` below, which writes the file's
format on its own (no sessionphone import), so these tests run, and fail
for the right reason, against a tree that predates the pid file.
`test_the_bots_own_record_is_the_one_dc_live_reads` holds the bot's writer
to the same format.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
DC_LIVE = REPO / "bin" / "dc-live"
DC_ENV = REPO / "bin" / "dc-env"
PS_ENV = {"TZ": "UTC0", "LC_ALL": "C"}      # as dc-live and the bot ask ps
SLEEP = "import time; time.sleep(120)"


# ---- the rig -----------------------------------------------------------------

def _ps(pid: int, field: str) -> str:
    r = subprocess.run(["ps", "-ww", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True,
                       env={**os.environ, **PS_ENV}, check=False)
    return r.stdout.strip()


def _record(pid: int, root: pathlib.Path) -> str:
    """bot.pid's four lines for PID on ROOT, as the bot writes them."""
    return f"{pid}\n{_ps(pid, 'lstart')}\n{_ps(pid, 'command')}\n{os.path.realpath(root)}\n"


def _agent() -> str:
    """An agent name no real bot uses (and no other run of this file)."""
    return f"claude-x{uuid.uuid4().hex[:10]}"


def _root(tmp_path: pathlib.Path, name: str = "root") -> pathlib.Path:
    """A root with a live call in it: a running bot gives exit 0 and the call."""
    r = tmp_path / name
    r.mkdir(mode=0o700)
    (r / "call-live.json").write_text(json.dumps(
        {"call_id": 5531, "chat_id": 12, "caller": "owner", "since": time.time(), "dir": str(r / "calls" / "x")}))
    return r


def _env(tmp_path: pathlib.Path, root: pathlib.Path, agent: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_", "SESSIONPHONE_"))}
    env.update(HOME=str(tmp_path / "home"), DC_ROOT=str(root), DC_AGENT=agent)
    return env


def _live(tmp_path, root, agent, *args, **extra) -> subprocess.CompletedProcess:
    return subprocess.run([str(DC_LIVE), *args], env={**_env(tmp_path, root, agent), **extra},
                          capture_output=True, text=True, check=False, timeout=60)


def _running(r: subprocess.CompletedProcess) -> bool:
    """dc-live said the bot runs (the live call answered), or that it does not."""
    if r.returncode == 0 and "call msg 5531" in r.stdout:
        return True
    assert r.returncode == 2 and "the bot is NOT RUNNING" in r.stderr, (r.returncode, r.stdout, r.stderr)
    return False


@contextlib.contextmanager
def _stand_in(argv: list[str], shows: str):
    """A sleeping process whose command line starts with SHOWS. Its own session
    (so its own process group), stopped by killing that group, never by name."""
    p = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not _ps(p.pid, "command").startswith(shows):
            assert p.poll() is None, f"stand-in exited {p.returncode}: {argv}"
            assert time.monotonic() < deadline, f"stand-in never showed {shows!r}: {_ps(p.pid, 'command')!r}"
            time.sleep(0.02)
        yield p
    finally:
        _stop(p)


def _stop(p: subprocess.Popen) -> None:
    """Kill the stand-in's own process group (start_new_session: its pgid is its pid),
    then reap it. Unreaped, its pid cannot be reused, so the group is still ours."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(p.pid, signal.SIGTERM)
    p.wait(timeout=10)


def _python(*args: str) -> tuple[list[str], str]:
    """A python stand-in carrying ARGS, as the measurement ran it."""
    return [sys.executable, "-c", SLEEP, *args], sys.executable


def _named(argv0: str) -> tuple[list[str], str]:
    """A stand-in whose whole command line is ARGV0 (then sleep's own 120)."""
    return ["bash", "-c", 'exec -a "$0" sleep 120', argv0], argv0


# ---- the six measured cases, one test each -----------------------------------

def test_row1_the_real_launchd_form_is_this_roots_bot_and_no_other_roots(tmp_path):
    """`python -m sessionphone.bridge.bot A`, with its bot.pid in its root: a bot
    for that root. The same agent on another root with no bot of its own is
    NOT running (pgrep was root-blind: it said running there too)."""
    agent = _agent()
    mine, other = _root(tmp_path, "mine"), _root(tmp_path, "other")
    with _stand_in(*_python("-m", "sessionphone.bridge.bot", agent)) as bot:
        (mine / "bot.pid").write_text(_record(bot.pid, mine))
        assert _running(_live(tmp_path, mine, agent))
        r = _live(tmp_path, other, agent)
        assert not _running(r)
        assert f"no {other / 'bot.pid'}" in r.stderr


def test_row2_another_agents_bot_whose_name_starts_with_this_one_is_not_this_bot(tmp_path):
    """A bot for agent A-2 (its argv carries `sessionphone.bridge.bot A` as a
    prefix) runs on its own root. A's root has no bot: NOT running. A-2's own
    root does see its bot, so the stand-in is a real bot where it should be."""
    agent = _agent()
    mine, theirs = _root(tmp_path, "mine"), _root(tmp_path, "theirs")
    with _stand_in(*_python("-m", "sessionphone.bridge.bot", f"{agent}-2")) as bot:
        (theirs / "bot.pid").write_text(_record(bot.pid, theirs))
        assert _running(_live(tmp_path, theirs, f"{agent}-2"))
        assert not _running(_live(tmp_path, mine, agent))


@pytest.mark.parametrize("case, argv0", [
    pytest.param("tail -f", "tail -f {root}/sessionphone.bridge.bot {agent}.log", id="row3-tail"),
    pytest.param("grep", "grep sessionphone.bridge.bot {agent}", id="row4-grep"),
    pytest.param("regex dot", "sessionphoneXbridgeXbot {agent}", id="row5-regex-dot"),
])
def test_rows3_to_5_a_process_whose_argv_carries_the_bots_name_is_no_bot(tmp_path, case, argv0):
    """A `tail -f` of a file named after the bot, an editor or grep with the
    string in its argv, and `sessionphoneXbridgeXbot` (`.` is a regex
    wildcard): none is a bot, so with no bot on the root it is NOT running."""
    agent = _agent()
    root = _root(tmp_path)
    with _stand_in(*_named(argv0.format(root=root, agent=agent))):
        r = _live(tmp_path, root, agent)
        assert not _running(r), case
        assert f"no {root / 'bot.pid'}" in r.stderr


def test_row6_the_foreground_bot_run_form_is_this_roots_bot(tmp_path):
    """`sessionphone bot run` runs the bot in-process: its argv never carries
    `sessionphone.bridge.bot`, so pgrep said NOT RUNNING while it ran. Its
    bot.pid says it runs."""
    agent = _agent()
    root = _root(tmp_path)
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as bot:
        (root / "bot.pid").write_text(_record(bot.pid, root))
        assert _running(_live(tmp_path, root, agent))
        r = _live(tmp_path, root, agent, "--id")
        assert (r.returncode, r.stdout.strip(), r.stderr) == (0, "5531", "")


# ---- a stale bot.pid reads NOT running ---------------------------------------

def test_a_bot_pid_naming_a_dead_pid_is_not_running(tmp_path):
    agent = _agent()
    root = _root(tmp_path)
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as bot:
        (root / "bot.pid").write_text(_record(bot.pid, root))
        assert _running(_live(tmp_path, root, agent))
    r = _live(tmp_path, root, agent)        # the stand-in is gone; its file stays
    assert not _running(r)
    assert f"{root / 'bot.pid'} is stale: pid {bot.pid} is not running" in r.stderr


def test_a_bot_pid_whose_pid_another_program_holds_now_is_not_running(tmp_path):
    """The bot died uncleanly and the system gave its pid to another program:
    the pid is alive, but its start time and command line are not the bot's."""
    agent = _agent()
    root = _root(tmp_path)
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as bot:
        was = _record(bot.pid, root)
    with _stand_in(*_named("some-other-program --serve")) as other:
        (root / "bot.pid").write_text(f"{other.pid}\n" + was.split("\n", 1)[1])
        r = _live(tmp_path, root, agent)
        assert not _running(r)
        # started in the same second as the bot, it differs by its command line alone
        assert f"is stale: pid {other.pid} " in r.stderr


def test_a_bot_pid_whose_pid_a_later_process_with_the_same_argv_holds_is_not_running(tmp_path):
    """The command line alone is not identity: a later process with the bot's
    very argv, started at another time, is not the process that wrote the file."""
    agent = _agent()
    root = _root(tmp_path)
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as later:
        pid, _started, rest = _record(later.pid, root).split("\n", 2)
        (root / "bot.pid").write_text(f"{pid}\nMon Jan  1 00:00:00 2001\n{rest}")
        r = _live(tmp_path, root, agent)
        assert not _running(r)
        assert f"is stale: pid {later.pid} is another process now" in r.stderr


def test_a_bot_pid_copied_from_another_root_is_not_this_roots_bot(tmp_path):
    agent = _agent()
    root, copy = _root(tmp_path, "root"), _root(tmp_path, "copy")
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as bot:
        (root / "bot.pid").write_text(_record(bot.pid, root))
        (copy / "bot.pid").write_text((root / "bot.pid").read_text())
        assert _running(_live(tmp_path, root, agent))
        r = _live(tmp_path, copy, agent)
        assert not _running(r)
        assert "is another root's" in r.stderr


def test_a_bot_pid_naming_a_live_process_that_is_no_sessionphone_bot_is_not_running(tmp_path):
    agent = _agent()
    root = _root(tmp_path)
    with _stand_in(*_named("some-other-program --serve")) as other:
        (root / "bot.pid").write_text(_record(other.pid, root))
        r = _live(tmp_path, root, agent)
        assert not _running(r)
        assert f"names pid {other.pid}, which is no sessionphone bot" in r.stderr


@pytest.mark.parametrize("text", ["", "not-a-pid\n", "0\n", "1\n"])
def test_a_bot_pid_that_names_no_bots_pid_is_not_running(tmp_path, text):
    agent = _agent()
    root = _root(tmp_path)
    (root / "bot.pid").write_text(text)
    r = _live(tmp_path, root, agent)
    assert not _running(r)
    assert "names no bot's pid" in r.stderr


def test_dc_bot_is_ignored_and_dc_live_says_so(tmp_path):
    """DC_BOT once named the argv text pgrep looked for. It is read no more:
    set, it changes nothing, and dc-live says so (never with --id)."""
    agent = _agent()
    root = _root(tmp_path)
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as bot:
        (root / "bot.pid").write_text(_record(bot.pid, root))
        r = _live(tmp_path, root, agent, DC_BOT="no-such-bot-at-all")
        assert _running(r)
        assert "DC_BOT is set and IGNORED" in r.stderr
        r = _live(tmp_path, root, agent, "--id", DC_BOT="no-such-bot-at-all")
        assert (r.returncode, r.stderr) == (0, "")


def test_dc_live_asks_no_process_list_by_pattern():
    """No pgrep (or pkill) in the tools or the package: argv text is not identity."""
    hits = [str(f.relative_to(REPO)) for d in ("bin", "src") for f in (REPO / d).rglob("*")
            if f.is_file() and f.suffix not in (".pyc",)
            and any(w in f.read_text(encoding="utf-8", errors="replace")
                    for w in ("pgrep ", "pkill ", '"pgrep"', '"pkill"'))]
    assert hits == []


# ---- the bot's own writer, and dc-env's path ---------------------------------

def test_the_bots_own_record_is_the_one_dc_live_reads(tmp_path):
    """bridge/pidfile.py writes the four lines `_record` writes, so the cases
    above stand for the real bot."""
    from sessionphone.bridge import pidfile
    root = _root(tmp_path)
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as bot:
        assert pidfile.record(root, bot.pid) == _record(bot.pid, root)


def test_a_bot_writes_its_pid_file_and_takes_it_back_at_a_clean_exit(tmp_path):
    """End to end with the real writer: a process with the foreground bot's argv
    writes its own bot.pid; dc-live sees it; at its clean exit it removes the file."""
    agent = _agent()
    root = _root(tmp_path)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from sessionphone.bridge import pidfile; "
            "f = sys.argv[2]; assert pidfile.write(f, sys.argv[3], print); print('ready', flush=True); "
            "sys.stdin.readline(); print(pidfile.clear(f), flush=True)")
    p = subprocess.Popen([sys.executable, "-c", code, str(REPO / "src"), str(root / "bot.pid"), str(root),
                          "sessionphone", "bot", "run"],
                         start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        assert (root / "bot.pid").read_text().split("\n")[0] == str(p.pid)
        assert _running(_live(tmp_path, root, agent))
        out, _ = p.communicate("\n", timeout=30)
        assert out.strip() == "True"
    finally:
        if p.poll() is None:
            _stop(p)
    assert not (root / "bot.pid").exists()
    r = _live(tmp_path, root, agent)
    assert not _running(r)
    assert f"no {root / 'bot.pid'}" in r.stderr


def test_clear_leaves_a_file_another_bot_wrote_alone(tmp_path):
    from sessionphone.bridge import pidfile
    f = tmp_path / "bot.pid"
    f.write_text(f"{os.getpid() + 1}\nx\ny\nz\n")
    assert pidfile.clear(f) is False and f.exists()
    f.write_text(f"{os.getpid()}\nx\ny\nz\n")
    assert pidfile.clear(f) is True and not f.exists()
    assert pidfile.clear(f) is False        # gone: nothing to do, no error


@pytest.mark.parametrize("how", ["no directory", "ps gives nothing", "ps cannot run"])
def test_a_pid_file_that_cannot_be_written_is_one_line_and_no_error(tmp_path, how):
    from sessionphone.bridge import pidfile
    logged = []
    f = tmp_path / "bot.pid"
    run = None
    if how == "no directory":
        f = tmp_path / "gone" / "bot.pid"
    elif how == "ps gives nothing":
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "")
    else:
        def run(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "ps")
    assert pidfile.write(f, tmp_path, logged.append, run=run) is None
    assert len(logged) == 1 and logged[0].startswith(f"bot.pid NOT WRITTEN at {f}: ")
    assert not f.exists()
    if f.parent.exists():
        assert list(f.parent.glob(".*.part")) == []     # no half-written file left


def test_the_pid_file_is_written_whole_and_private(tmp_path):
    from sessionphone.bridge import pidfile
    f = tmp_path / "w" / "bot.pid"
    f.parent.mkdir()
    assert pidfile.write(f, tmp_path, pytest.fail) == f
    assert f.read_text().split("\n")[0] == str(os.getpid())
    assert f.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in f.parent.iterdir()] == ["bot.pid"]      # no .part left


@pytest.mark.parametrize("given", [None, "elsewhere/my.pid"])
def test_dc_env_and_dc_env_py_name_one_pid_file(tmp_path, given):
    """bin/dc-env mirrors bridge/dc_env.py: the path dc-live reads is the one
    the bot writes, with DC_PID_FILE unset and set."""
    root = tmp_path / "root"
    root.mkdir()
    env = _env(tmp_path, root, "claude-mirror")
    if given:
        env["DC_PID_FILE"] = str(tmp_path / given)
    sh = subprocess.run(["bash", "-c", f'source "{DC_ENV}"; printf %s "$DC_PID_FILE"'],
                        env=env, capture_output=True, text=True, check=True).stdout
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from sessionphone.bridge import dc_env; print(dc_env.PID_FILE)")
    py = subprocess.run([sys.executable, "-c", code, str(REPO / "src")],
                        env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert sh == py == str(tmp_path / given if given else root / "bot.pid")

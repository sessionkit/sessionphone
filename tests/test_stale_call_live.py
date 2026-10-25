"""A bot stopped mid-call leaves call-live.json, and dc-live believes it.

The bot writes <home>/call-live.json when a call connects and removes it when
the call closes (bridge/calls.py, `_live_write` / `_live_clear`). Nothing else
removes it. Two ways leave it behind:

- launchd stops a job with SIGTERM (a bootout, an install over a running bot).
  The bot installs no SIGTERM handler, so the default action kills it: no
  close(), no atexit, and the file stays (measured: exit 143, file kept).
- the next bot starts beside the old file and leaves it there; it writes a
  valid bot.pid, so dc-live, which reads the two files, reports the dead call
  as live until a new call connects and overwrites it.

The tests marked xfail(strict=True) reproduce the failure: each fails today,
for the reason it names, and passes once the bot (a) removes, at start and
once it holds the account, a call-live.json older than itself, and (b) on
SIGTERM ends a live call and removes the file. strict: a test that starts
passing fails the run until its marker is taken off.

The bot runs in a child process on fakes (tests/_stopped_bot.py), with its
root and home in tmp, so the paths it binds at import are tmp paths. A child
is stopped by killing its own process group, never by a pattern.
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

import pytest
from test_dc_live_pidfile import _agent, _live, _python, _record, _running, _stand_in

HERE = pathlib.Path(__file__).resolve().parent
RIG = HERE / "_stopped_bot.py"
CALL_ID = 5531
GRACE_S = 10            # under launchd's default exit timeout (20 s) before it sends SIGKILL
STALE = "a call-live.json older than the bot is still there after the bot started"

pytestmark = pytest.mark.skipif(
    __import__("sessionphone.media", fromlist=["missing"]).missing() != [],
    reason="the media pack is missing: a calls-on bot cannot load its call manager")


# ---- the rig -----------------------------------------------------------------

def _home(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict]:
    """A root (the default profile's home) in tmp, and the env that binds the bot to it."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (tmp_path / "home").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_", "SESSIONPHONE_"))}
    env.update(HOME=str(tmp_path / "home"), DC_ROOT=str(root), DC_AGENT=_agent(), PYTHONUNBUFFERED="1")
    return root, env


def _live_file(root: pathlib.Path, since: float) -> pathlib.Path:
    """call-live.json as a connected call writes it (bridge/calls.py `_live_write`)."""
    f = root / "call-live.json"
    f.write_text(json.dumps({"call_id": CALL_ID, "chat_id": 12, "dir": "call-x", "since": since,
                             "caller": "owner"}))
    return f


@contextlib.contextmanager
def _bot(tmp_path: pathlib.Path, env: dict, mode: str, name: str = "bot"):
    """The rig's bot, in its own session (so its own process group), up to its
    first outbox pass ("ready"). Yields (process, out dir). Stopped by killing
    that group, never by a name."""
    out = tmp_path / name
    p = subprocess.Popen([sys.executable, str(RIG), mode, str(out)], env=env, cwd=HERE,
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline().strip()
        if line != "ready":
            p.kill()
            _, err = p.communicate(timeout=10)
            pytest.fail(f"the rig's bot never came up ({p.returncode}): {line!r}\n{err}\n{_log(out)}")
        yield p, out
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        p.communicate(timeout=10)


def _log(out: pathlib.Path) -> str:
    f = out / "log.txt"
    return f.read_text() if f.exists() else ""


def _term(p: subprocess.Popen) -> int:
    """SIGTERM to the bot alone, as launchd sends it; its exit code, or None
    if it is still running GRACE_S later (launchd would SIGKILL it then)."""
    os.kill(p.pid, signal.SIGTERM)
    try:
        return p.wait(timeout=GRACE_S)
    except subprocess.TimeoutExpired:
        return None


# ---- (1) at start: a call-live.json older than this bot ----------------------

def test_a_live_file_older_than_the_bot_is_gone_once_it_holds_the_account(tmp_path):
    """The file a bot stopped mid-call left (its `since` an hour before this
    bot started) is removed at start, once this bot holds the account, and
    the bot says so in its log, once. Fails today: the file is still there,
    and nothing in the log names it."""
    root, env = _home(tmp_path)
    stale = _live_file(root, time.time() - 3600)
    with _bot(tmp_path, env, "start") as (_p, out):
        assert (root / "bot.pid").exists(), "the bot holds the account and says so"
        log = _log(out)
        assert not stale.exists(), f"{stale} is still there after the bot started:\n{log}"
        assert sum("call-live.json" in line for line in log.splitlines()) == 1, log


def test_a_second_bot_refused_on_the_same_home_leaves_the_live_file_alone(tmp_path):
    """The guard on (1), passing today and to keep passing: a bot that does NOT
    hold the account (a second bot on a home another bot runs on) removes
    nothing. The first bot's call is live; its file stays."""
    root, env = _home(tmp_path)
    with _bot(tmp_path, env, "start", "first"):
        live = _live_file(root, time.time())        # the first bot's call connects
        r = subprocess.run([sys.executable, str(RIG), "start", str(tmp_path / "second")], env=env, cwd=HERE,
                           capture_output=True, text=True, timeout=60, start_new_session=True, check=False)
        assert r.returncode == 2 and "REFUSED to start" in r.stderr, (r.returncode, r.stdout, r.stderr)
        assert live.exists()
        assert json.loads(live.read_text())["call_id"] == CALL_ID


# ---- (2) SIGTERM during a call ------------------------------------------------

def test_sigterm_during_a_call_ends_it_and_removes_the_live_file(tmp_path):
    """launchd's stop, mid-call: the bot ends the call (end_call, so the phone
    hangs up too), removes call-live.json, and exits within launchd's grace.
    Fails today: SIGTERM's default action kills the bot (exit -15, a shell's
    143), the call is never ended, and the file stays."""
    root, env = _home(tmp_path)
    with _bot(tmp_path, env, "call") as (p, out):
        live = root / "call-live.json"
        assert json.loads(live.read_text())["call_id"] == CALL_ID, "the rig's call is up"
        code = _term(p)
        assert code is not None, f"still running {GRACE_S} s after SIGTERM:\n{_log(out)}"
        assert code == 128 + signal.SIGTERM, f"exit {code}, not a SIGTERM's 143:\n{_log(out)}"
        assert not live.exists(), f"exit {code}, and {live} is still there:\n{_log(out)}"
        assert (out / f"end_call-{CALL_ID}").exists(), f"exit {code}, the call was never ended:\n{_log(out)}"


def test_sigterm_with_no_call_up_stops_the_bot(tmp_path):
    """The rig's premise, passing today: SIGTERM stops a bot with no call up,
    within the grace, and there is no live file before or after."""
    root, env = _home(tmp_path)
    with _bot(tmp_path, env, "start") as (p, out):
        assert not (root / "call-live.json").exists()
        assert _term(p) is not None, _log(out)
        assert not (root / "call-live.json").exists()


# ---- (3) dc-live: a live file older than the running bot ----------------------

@pytest.mark.parametrize("form", ["launchd", "bot run"])
def test_dc_live_does_not_report_a_live_file_older_than_the_running_bot(tmp_path, form):
    """The bot runs (its bot.pid is valid), and call-live.json is an hour older
    than it: a call from an earlier bot, not this one's. dc-live says no call
    is live, and --id names none. Fails today: dc-live reads the file alone,
    exit 0, "call msg 5531", and --id prints 5531 for `dc-say --call`."""
    agent = _agent()
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    argv = (_python("-m", "sessionphone.bridge.bot", agent) if form == "launchd"
            else _python("/venv/bin/sessionphone", "bot", "run"))
    with _stand_in(*argv) as bot:
        (root / "bot.pid").write_text(_record(bot.pid, root))
        _live_file(root, time.time() - 3600)
        r = _live(tmp_path, root, agent)
        assert not (r.returncode == 0 and f"call msg {CALL_ID}" in r.stdout), (r.returncode, r.stdout, r.stderr)
        r = _live(tmp_path, root, agent, "--id")
        assert (r.returncode, r.stdout.strip()) != (0, str(CALL_ID)), r.stdout


def test_dc_live_reports_a_call_newer_than_the_running_bot(tmp_path):
    """The guard on (3), passing today and to keep passing: a call that
    connected after the bot started is live."""
    agent = _agent()
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as bot:
        (root / "bot.pid").write_text(_record(bot.pid, root))
        time.sleep(1.1)                     # ps gives the start to the second
        _live_file(root, time.time())
        assert _running(_live(tmp_path, root, agent))
        r = _live(tmp_path, root, agent, "--id")
        assert (r.returncode, r.stdout.strip()) == (0, str(CALL_ID))



def test_dc_live_says_why_a_live_file_older_than_the_running_bot_is_not_live(tmp_path):
    """What dc-live says of such a file: "no call is live" (exit 1, as with no
    file), the reason on stderr; --id prints nothing, so `dc-say --call`
    is given no call to speak into."""
    agent = _agent()
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    with _stand_in(*_python("/venv/bin/sessionphone", "bot", "run")) as bot:
        (root / "bot.pid").write_text(_record(bot.pid, root))
        _live_file(root, time.time() - 3600)
        r = _live(tmp_path, root, agent)
        assert (r.returncode, r.stdout.strip()) == (1, "no call is live"), (r.stdout, r.stderr)
        assert "is stale: call msg 5531 began" in r.stderr and "before the running bot started" in r.stderr
        r = _live(tmp_path, root, agent, "--id")
        assert (r.returncode, r.stdout, r.stderr) == (1, "", "")


# ---- the parts, one at a time ----------------------------------------------------

def _lines(monkeypatch, mod) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(mod, "log", said.append)
    return said


@pytest.mark.parametrize("case", ["older", "torn", "no since"])
def test_clear_stale_live_removes_a_file_no_call_of_this_bot_wrote(tmp_path, monkeypatch, case):
    from sessionphone.bridge import bot
    said = _lines(monkeypatch, bot)
    f = tmp_path / "call-live.json"
    f.write_text({"older": json.dumps({"call_id": CALL_ID, "since": 1000.0}),
                  "torn": '{"call_id": 55',
                  "no since": json.dumps({"call_id": CALL_ID})}[case])
    assert bot.clear_stale_live(f, started=2000.0) is True
    assert not f.exists()
    assert len(said) == 1 and "removed call-live.json left by an earlier bot" in said[0], said
    assert ("call msg 5531" in said[0]) == (case == "older"), said


def test_clear_stale_live_leaves_a_file_newer_than_this_bot_and_says_nothing(tmp_path, monkeypatch):
    from sessionphone.bridge import bot
    said = _lines(monkeypatch, bot)
    f = tmp_path / "call-live.json"
    f.write_text(json.dumps({"call_id": CALL_ID, "since": 2000.5}))
    assert bot.clear_stale_live(f, started=2000.0) is False
    assert f.exists() and said == []
    assert bot.clear_stale_live(tmp_path / "none.json", started=2000.0) is False     # no file: nothing
    assert said == []


@pytest.mark.parametrize("live", ["none", "newer"])
def test_clear_stale_live_sweeps_the_temp_files_an_earlier_bot_left(tmp_path, monkeypatch, live):
    """A bot killed between the temp write and the rename leaves its part;
    at start every part beside the file goes, with one line, whatever the
    file itself is. Anything else in the directory stays."""
    from sessionphone.bridge import bot
    said = _lines(monkeypatch, bot)
    f = tmp_path / "call-live.json"
    if live == "newer":
        f.write_text(json.dumps({"call_id": CALL_ID, "since": 2000.5}))
    parts = [tmp_path / ".call-live.json.4242.part", tmp_path / ".call-live.json.7.part"]
    for p in parts:
        p.write_text('{"call_id": 55')
    others = [tmp_path / ".call-live.json.x.part", tmp_path / ".other.json.4242.part",
              tmp_path / "call-live.json.4242.part", tmp_path / "bot.pid"]
    for o in others:
        o.write_text("keep")
    assert bot.clear_stale_live(f, started=2000.0) is False
    assert not any(p.exists() for p in parts)
    assert all(o.exists() for o in others)
    assert f.exists() == (live == "newer")
    assert said == ["removed 2 temp file(s) of call-live.json left by an earlier bot stopped mid-write"]


def test_clear_stale_live_names_a_temp_file_it_cannot_remove(tmp_path, monkeypatch):
    from sessionphone.bridge import bot
    said = _lines(monkeypatch, bot)
    f = tmp_path / "call-live.json"
    part = tmp_path / ".call-live.json.4242.part"
    part.write_text("{")
    real_unlink = pathlib.Path.unlink

    def refuse(self, *a, **k):
        if self == part:
            raise PermissionError("not permitted")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "unlink", refuse)
    assert bot.clear_stale_live(f, started=2000.0) is False
    assert part.exists()
    assert len(said) == 1 and "NOT removed" in said[0] and part.name in said[0], said


def test_the_bot_and_the_calls_name_one_live_file(monkeypatch):
    """The bot clears at start the same file the calls write: dc_env's."""
    from sessionphone.bridge import bot, dc_env
    assert bot.LIVE_FILE == dc_env.LIVE_FILE == bot.calls.LIVE_FILE


def _session(calls, manager, msg_id=CALL_ID, end=None):
    """A bare call session: what stop() and the live file read of one."""
    s = calls.CallSession.__new__(calls.CallSession)
    s.manager, s.msg_id, s.chat_id, s.caller = manager, msg_id, 12, "owner"
    s.dir = pathlib.Path("call-x")
    s.account = type("A", (), {"get_message_by_id": lambda self, n: type(
        "M", (), {"end_call": lambda self: (end or (lambda: None))(n)})()})()
    manager.sessions[msg_id] = s
    return s


@pytest.fixture
def calls_mod(monkeypatch, live_file_in_tmp):
    from sessionphone.bridge import bot
    calls = bot.calls
    said: list[str] = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(said.append, None, None))
    monkeypatch.setattr(calls, "said", said, raising=False)
    # every manager opens an event loop: close each one here, so none is left
    # for the garbage collector to close in the middle of a later test
    made = []

    class Manager(calls.CallManager):
        def __init__(self):
            super().__init__()
            made.append(self)
    monkeypatch.setattr(calls, "CallManager", Manager)
    yield calls
    for m in made:
        if m.thread.is_alive():
            m.loop.call_soon_threadsafe(m.loop.stop)
            m.thread.join(5)
        if not m.loop.is_running():
            m.loop.close()


def test_live_write_goes_whole_through_a_temp_file_renamed_in(calls_mod, monkeypatch, live_file_in_tmp):
    calls = calls_mod
    renamed = []
    real = os.replace
    monkeypatch.setattr(calls.os, "replace", lambda a, b: (renamed.append((pathlib.Path(a), pathlib.Path(b))),
                                                           real(a, b))[1])
    s = _session(calls, calls.CallManager())
    s._live_write()
    (part, dest), = renamed
    assert dest == live_file_in_tmp and part.parent == dest.parent and part.name.endswith(".part")
    assert json.loads(dest.read_text())["call_id"] == CALL_ID
    assert sorted(p.name for p in dest.parent.iterdir()) == ["call-live.json"]


def test_live_write_that_cannot_rename_keeps_the_old_file_whole_and_no_temp(calls_mod, monkeypatch,
                                                                             live_file_in_tmp):
    calls = calls_mod
    live_file_in_tmp.write_text('{"call_id": 1, "since": 1.0}')

    def refuse(a, b):
        raise OSError("no rename")
    monkeypatch.setattr(calls.os, "replace", refuse)
    _session(calls, calls.CallManager())._live_write()
    assert live_file_in_tmp.read_text() == '{"call_id": 1, "since": 1.0}'
    assert sorted(p.name for p in live_file_in_tmp.parent.iterdir()) == ["call-live.json"]
    assert any("live file write failed" in line for line in calls.said)


def test_stop_ends_the_call_removes_its_file_and_survives_a_close_that_raises(calls_mod, live_file_in_tmp):
    calls = calls_mod
    m = calls.CallManager()
    m.thread.start()
    try:
        ended = []
        s = _session(calls, m, end=ended.append)

        async def close(why):
            raise RuntimeError("close broke")
        s.close = close
        s._live_write()
        assert m.stop("a test stop", budget=5) == 1
        assert ended == [CALL_ID]
        assert not live_file_in_tmp.exists()
        assert any("close on stop failed" in line for line in calls.said), calls.said
    finally:
        m.loop.call_soon_threadsafe(m.loop.stop)


def test_stop_is_bounded_when_end_call_never_returns(calls_mod, live_file_in_tmp):
    """A stuck account: the file is gone at once and stop() returns at its
    budget, so the bot exits inside launchd's grace."""
    import threading
    calls = calls_mod
    m = calls.CallManager()             # its loop not running: no close() to wait for
    stuck = threading.Event()
    s = _session(calls, m, end=lambda n: stuck.wait())
    s._live_write()
    t0 = time.monotonic()
    try:
        assert m.stop("a test stop", budget=0.5) == 1
        assert time.monotonic() - t0 < 3
        assert not live_file_in_tmp.exists()
    finally:
        stuck.set()


def test_stop_leaves_the_file_of_a_call_it_does_not_hold(calls_mod, live_file_in_tmp):
    calls = calls_mod
    m = calls.CallManager()
    _session(calls, m, msg_id=777)
    live_file_in_tmp.write_text(json.dumps({"call_id": CALL_ID, "since": time.time()}))
    m.stop("a test stop", budget=0.5)
    assert json.loads(live_file_in_tmp.read_text())["call_id"] == CALL_ID


def test_stop_with_no_call_does_nothing(calls_mod):
    assert calls_mod.CallManager().stop("a test stop", budget=0.5) == 0
    assert calls_mod.said == []


def _cut_print(*a, **k):
    raise RuntimeError("reentrant call inside <_io.BufferedWriter name='<stdout>'>")


def test_a_log_line_cut_by_a_signal_still_reaches_the_log_file(tmp_path, monkeypatch):
    """SIGTERM while the main thread is inside its own print: on_sigterm's log
    line gets "reentrant call". It must not raise (the stop would never run),
    and bot.log still has the line."""
    from sessionphone.bridge import bot
    monkeypatch.setattr(bot, "LOG", tmp_path / "bot.log")
    monkeypatch.setattr(bot, "print", _cut_print, raising=False)
    bot.log("SIGTERM: stopping")
    assert (tmp_path / "bot.log").read_text().endswith(" SIGTERM: stopping\n")


def test_stop_with_its_print_cut_by_a_signal_still_ends_the_call(calls_mod, tmp_path, monkeypatch,
                                                                    live_file_in_tmp):
    """calls.stop() runs in the SIGTERM handler, on the main thread; its CALL
    lines go through bot.log, whose print a signal can cut. The call is still
    ended and its file removed."""
    from sessionphone.bridge import bot
    calls = calls_mod
    monkeypatch.setattr(bot, "LOG", tmp_path / "bot.log")
    monkeypatch.setattr(bot, "print", _cut_print, raising=False)
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(bot.log, None, None))
    m = calls.CallManager()             # its loop not running: no close() to wait for
    ended = []
    _session(calls, m, end=ended.append)._live_write()
    assert m.stop("a test stop", budget=2) == 1
    assert ended == [CALL_ID] and not live_file_in_tmp.exists()
    assert "stopping (a test stop): ending 1 call(s)" in (tmp_path / "bot.log").read_text()

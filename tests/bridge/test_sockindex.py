"""The call sockets' index: while the bridge's legs serve, the bot links each
socket it bound under ~/.sessionkit/phone/run/<bot>[.<profile>]/, so a reader
finds it whatever the bot's root is. A fake HOME, a real loop, real sockets.
"""
import asyncio
import json
import os
import pathlib
import shutil
import stat
import tempfile

import pytest

from sessionphone import paths
from sessionphone.bridge import dc_env, earleg, mouthleg, sockindex


class Manager:
    def __init__(self):
        self.sessions = {}


@pytest.fixture
def home(monkeypatch):
    """A SHORT fake HOME (macOS caps a socket path at 104 bytes, and a reader
    connects through the link's path), and a short root apart from it."""
    h = pathlib.Path(tempfile.mkdtemp(prefix="si-", dir="/tmp"))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(dc_env, "AGENT", "claude", raising=False)
    monkeypatch.setattr(dc_env, "PROFILE", "")
    yield h
    shutil.rmtree(h, ignore_errors=True)


def serve_both(root, log=None):
    """Both legs as the bot serves them (serve_published), at ROOT's sockets;
    probe each through its link. -> (lines logged, probe replies by name)."""
    lines = []
    log = log or lines.append

    async def main():
        mgr = Manager()
        servers = [await earleg.serve_published(mgr, root / "call-ears.sock", log),
                   await mouthleg.serve_published(mgr, root / "call-mouth.sock", log)]
        got = {}
        for d in paths.run_dir().iterdir():
            for link in d.iterdir():
                r, w = await asyncio.open_unix_connection(str(link))
                w.write(b'{"probe": true}\n')
                await w.drain()
                got[f"{d.name}/{link.name}"] = json.loads(await r.readline())
                w.close()
        for s in servers:
            s.close()
            await s.wait_closed()
        return got
    return lines, asyncio.run(main())


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_the_run_dir_is_spelled_once_in_the_kit():
    assert paths.RUN_DIR == paths.kit_dir("phone", "run") == "~/.sessionkit/phone/run"
    assert paths.run_entry("claude").name == "claude"
    assert paths.run_entry("claude", "private").name == "claude.private"


def test_a_default_profile_bot_publishes_both_sockets(home, monkeypatch):
    root = home / "r"
    root.mkdir()
    monkeypatch.setattr(sockindex.atexit, "register", lambda *a: None)
    _, got = serve_both(root)
    entry = home / ".sessionkit/phone/run/claude"
    assert os.readlink(entry / "call-mouth.sock") == str(root / "call-mouth.sock")
    assert os.readlink(entry / "call-ears.sock") == str(root / "call-ears.sock")
    assert mode(entry) == 0o700 and mode(entry.parent) == 0o700
    assert got["claude/call-mouth.sock"]["ok"] and got["claude/call-ears.sock"]["rate"] == 16000


def test_a_named_profile_bot_publishes_under_bot_dot_profile(home, monkeypatch):
    root = home / "r" / "private"
    root.mkdir(parents=True)
    monkeypatch.setattr(dc_env, "PROFILE", "private")
    monkeypatch.setattr(sockindex.atexit, "register", lambda *a: None)
    _, got = serve_both(root)
    run = home / ".sessionkit/phone/run"
    assert [d.name for d in run.iterdir()] == ["claude.private"]
    assert os.readlink(run / "claude.private/call-mouth.sock") == str(root / "call-mouth.sock")
    assert os.readlink(run / "claude.private/call-ears.sock") == str(root / "call-ears.sock")
    assert mode(run / "claude.private") == 0o700
    assert set(got) == {"claude.private/call-mouth.sock", "claude.private/call-ears.sock"}


def test_a_mouth_sock_override_is_linked_as_bound(home, monkeypatch):
    """DC_MOUTH_SOCK names the socket; the link points at that path, not the home's."""
    elsewhere = home / "x" / "m.sock"
    monkeypatch.setenv("DC_MOUTH_SOCK", str(elsewhere))
    monkeypatch.setattr(mouthleg, "SOCK", dc_env.path("MOUTH_SOCK", "call-mouth.sock"))
    monkeypatch.setattr(sockindex.atexit, "register", lambda *a: None)

    async def main():
        s = await mouthleg.serve_published(Manager(), None, lambda m: None)
        s.close()
        await s.wait_closed()
    asyncio.run(main())
    assert os.readlink(home / ".sessionkit/phone/run/claude/call-mouth.sock") == str(elsewhere)


def test_start_publishes_on_the_calls_loop(home, monkeypatch):
    """The bridge's own entry (CallManager.start calls mouthleg.start)."""
    import threading
    mgr = Manager()
    mgr.loop = asyncio.new_event_loop()
    t = threading.Thread(target=mgr.loop.run_forever, daemon=True)
    t.start()
    monkeypatch.setattr(sockindex.atexit, "register", lambda *a: None)
    try:
        server = mouthleg.start(mgr, lambda m: None, home / "m.sock").result(5)
        asyncio.run_coroutine_threadsafe(_close(server), mgr.loop).result(5)
    finally:
        mgr.loop.call_soon_threadsafe(mgr.loop.stop)
        t.join(5)
    assert os.readlink(home / ".sessionkit/phone/run/claude/call-mouth.sock") == str(home / "m.sock")


async def _close(server):
    server.close()
    await server.wait_closed()


def test_a_stale_link_is_replaced(home):
    entry = home / ".sessionkit/phone/run/claude"
    entry.mkdir(parents=True)
    os.symlink(home / "gone" / "call-mouth.sock", entry / "call-mouth.sock")
    link = sockindex.publish(sockindex.MOUTH, home / "call-mouth.sock", log=lambda m: None)
    assert link == entry / "call-mouth.sock"
    assert os.readlink(link) == str(home / "call-mouth.sock")
    assert sorted(p.name for p in entry.iterdir()) == ["call-mouth.sock"]   # no tmp left


def test_a_wide_run_dir_is_made_private(home):
    run = home / ".sessionkit/phone/run"
    run.mkdir(parents=True, mode=0o755)
    run.chmod(0o755)
    sockindex.publish(sockindex.EARS, home / "e.sock", log=lambda m: None)
    assert mode(run) == 0o700 and mode(run / "claude") == 0o700


def test_our_link_goes_at_exit_and_a_foreign_one_stays(home):
    ours = home / "call-mouth.sock"
    exits = []
    link = sockindex.published(sockindex.MOUTH, ours, log=lambda m: None,
                               at_exit=lambda f, *a: exits.append((f, a)))
    assert exits == [(sockindex.unpublish, (link, ours))]
    # another bot of this name took the entry over: its link is not ours to remove
    os.unlink(link)
    os.symlink(home / "other.sock", link)
    f, a = exits[0]
    assert f(*a) is False
    assert os.readlink(link) == str(home / "other.sock")
    # still ours: removed
    os.unlink(link)
    os.symlink(ours, link)
    assert f(*a) is True and not os.path.lexists(link)
    # gone already, or not a link at all: left alone, no raise
    assert f(*a) is False
    link.write_text("x")
    assert f(*a) is False and link.exists()


@pytest.mark.parametrize("how", ["run-is-a-file", "parent-read-only"])
def test_an_unwritable_run_dir_does_not_stop_the_leg(home, monkeypatch, how):
    kit = home / ".sessionkit/phone"
    kit.mkdir(parents=True)
    if how == "run-is-a-file":
        (kit / "run").write_text("not a folder")
    else:
        kit.chmod(0o500)
    root = home / "r"
    root.mkdir()
    lines = []

    async def main():
        s = await mouthleg.serve_published(Manager(), root / "call-mouth.sock", lines.append)
        r, w = await asyncio.open_unix_connection(str(root / "call-mouth.sock"))
        w.write(b'{"probe": true}\n')
        await w.drain()
        reply = json.loads(await r.readline())
        w.close()
        s.close()
        await s.wait_closed()
        return reply
    try:
        assert asyncio.run(main())["ok"]
    finally:
        kit.chmod(0o700)
    assert any("not published" in m and "serves on" in m for m in lines), lines


@pytest.mark.parametrize("bot,profile", [("", ""), ("..", ""), ("a/b", ""), ("claude", "x/y")])
def test_a_name_that_is_not_a_folder_publishes_nothing(home, bot, profile):
    lines = []
    assert sockindex.publish(sockindex.MOUTH, home / "m.sock", bot=bot, profile=profile,
                             log=lines.append) is None
    assert not (home / ".sessionkit").exists() and "not published" in lines[0]


PROBE = """
import asyncio, os
from sessionphone.bridge import mouthleg, earleg

class M:
    sessions = {}

async def main():
    for leg in (mouthleg, earleg):
        s = await leg.serve_published(M(), None, lambda m: None)
        s.close()
        await s.wait_closed()
asyncio.run(main())
run = os.path.expanduser("~/.sessionkit/phone/run")
for d in sorted(os.listdir(run)):
    for n in sorted(os.listdir(os.path.join(run, d))):
        print(f"{d}/{n} {os.readlink(os.path.join(run, d, n))}")
"""     # before its exit: at a normal exit the bot removes its own links


@pytest.mark.parametrize("var", ["DC_ROOT", "SESSIONPHONE_ROOT"])
def test_the_index_is_under_home_whatever_the_root(home, var):
    """The bot's root moves its sockets, never the index: a reader looks only at
    ~/.sessionkit/phone/run. A child process, so the root is bound at import as
    the bot binds it; the sockets are at their defaults under that root."""
    import subprocess
    import sys
    root = home / "elsewhere"
    root.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items()
           if k not in ("DC_ROOT", "SESSIONPHONE_ROOT") and not k.startswith("DC_")}
    env.update(HOME=str(home), DC_AGENT="claude", DC_PROFILE="private", DC_CALLS="off",
               **{var: str(root)})
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        f"claude.private/call-ears.sock {root / 'private' / 'call-ears.sock'}",
        f"claude.private/call-mouth.sock {root / 'private' / 'call-mouth.sock'}"]
    assert not (root / ".sessionkit").exists() and not (root / "run").exists()
    # and the exit removed them: they were still this bot's
    assert list((home / ".sessionkit/phone/run/claude.private").iterdir()) == []

"""`sessionphone bot migrate [--dry-run]`: the old default root moves into the
kit, keys and all (PHONE-270 do-001).

The old root, ~/.sessionphone, holds the bot's Delta Chat account and its keys.
migrate refuses while a bot runs on it or a call is live, moves it by rename
(the same inodes: nothing copied), leaves the old path as a link to the new
root, never deletes it, and re-renders only the plists whose root is the old
one. --dry-run changes nothing.

Every home here is a tmp dir (PHONE-264): the real ~/.sessionphone is never
read or written. LaunchAgents is the tmp home's. The "bot" is a sleeping
stand-in whose command line names sessionphone, in its own session, stopped
by killing that process group, never by a pattern. The Delta Chat account is
a fixture (an accounts folder with a dc.db holding a key pair), opened through
account._open with the rpc-server faked: no real account, relay or launchd.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import plistlib
import signal
import sqlite3
import subprocess
import sys
import time
from typing import ClassVar

import pytest
from click.testing import CliRunner

from sessionphone import account, migrate, paths, service, settings
from sessionphone.bridge import pidfile
from sessionphone.cli import main as cli

SCRUB_PREFIXES = ("DC_", "PIP_CHAT_")
SCRUB = ("SESSIONPHONE_ROOT", "CLAUDE_CODE_AGENT", "EMAIL")
DEFAULT_FILE = settings.FILE
UUID = "1"
PRIVATE = b"-----PRIVATE KEY fixture-----\x00\x01\x02"
PUBLIC = b"-----PUBLIC KEY fixture-----\x03\x04\x05"
SINCE = 1907272800.0        # Sun 2030-06-09: after any stand-in's start


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A tmp HOME with no root configured anywhere (conftest's
    SESSIONPHONE_ROOT goes too), and the once-per-process notice forgotten."""
    for k in [k for k in os.environ if k.startswith(SCRUB_PREFIXES) or k in SCRUB]:
        monkeypatch.delenv(k)
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(settings, "FILE", DEFAULT_FILE)
    monkeypatch.setattr(paths, "_said_old", "")
    return h


def old(h: pathlib.Path) -> pathlib.Path:
    return h / ".sessionphone"


def new(h: pathlib.Path) -> pathlib.Path:
    return h / ".sessionkit" / "phone" / "state"


def agents(h: pathlib.Path) -> pathlib.Path:
    return h / "Library" / "LaunchAgents"


def make_account(accounts: pathlib.Path) -> pathlib.Path:
    """A fixture Delta Chat account: accounts/<id>/dc.db holding one key pair,
    the folders 0700 as the rpc-server makes them."""
    accounts.mkdir(parents=True, mode=0o700)
    accounts.chmod(0o700)
    (accounts / "accounts.toml").write_text(f'selected_account = {UUID}\nnext_id = 2\n')
    d = accounts / UUID
    d.mkdir(mode=0o700)
    db = d / "dc.db"
    with contextlib.closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE keypairs (id INTEGER PRIMARY KEY, private_key BLOB, public_key BLOB)")
        c.execute("INSERT INTO keypairs VALUES (1, ?, ?)", (PRIVATE, PUBLIC))
        c.commit()
    db.chmod(0o600)
    return db


@pytest.fixture
def root(home):
    """The old default root as a Mac running sessionphone before PHONE-270 has
    it: 0700, an account with its keys, the bot's files, and the stale
    .bot.lock and call-live.json a stopped bot leaves (as one host's was)."""
    r = old(home)
    r.mkdir(mode=0o700)
    r.chmod(0o700)
    make_account(r / "accounts")
    (r / "callers.txt").write_text("owner@phone.example.org\n")
    (r / "calls").mkdir()
    (r / "calls" / "call-1.wav").write_bytes(b"RIFF")
    (r / ".bot.lock").write_text("4242\n")
    return r


def snapshot(*tops: pathlib.Path) -> dict:
    """Every path under TOPS: its kind, mode, inode, size, mtime and content
    hash (a link: its target). Equal snapshots, nothing changed."""
    out = {}
    for top in tops:
        if not os.path.lexists(top):
            out[str(top)] = None
            continue
        for p in [top, *sorted(top.rglob("*"))]:
            st = os.lstat(p)
            if p.is_symlink():
                out[str(p)] = ("link", os.readlink(p))
            elif p.is_dir():
                out[str(p)] = ("dir", st.st_mode, st.st_ino, st.st_mtime_ns)
            else:
                out[str(p)] = ("file", st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns,
                               hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def run(*args):
    return CliRunner().invoke(cli, ["bot", "migrate", *args])


def write_plist(path: pathlib.Path, env: dict, workdir: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": path.stem, "ProgramArguments": ["/venv/libexec/session-phone", "-m",
                                                     "sessionphone.bridge.bot", "claude"],
            "EnvironmentVariables": env, "WorkingDirectory": workdir, "KeepAlive": True,
            "StandardOutPath": "/tmp/x.log"}
    raw = plistlib.dumps(data)
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def load(path: pathlib.Path) -> dict:
    return plistlib.loads(path.read_bytes())


# ---- a bot stand-in, as tests/test_install_live_call.py runs one ---------------

@contextlib.contextmanager
def stand_in():
    argv = [sys.executable, "-c", "import time; time.sleep(120)", "sessionphone.bridge.bot", "claude"]
    p = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while "sessionphone.bridge.bot" not in _command(p.pid):
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


@contextlib.contextmanager
def lock_held(lock: pathlib.Path):
    """A bot's hold on its home: the flock bridge/bot.py take_home_lock takes."""
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.pwrite(fd, b"5150\n", 0)
        yield
    finally:
        os.close(fd)


def call_up(live: pathlib.Path, since: float = SINCE) -> None:
    live.write_text(json.dumps({"call_id": 7, "chat_id": 12, "dir": "call-7",
                                "since": since, "caller": "owner@phone.example.org"}))


# ---- the names migrate shares with the bot ---------------------------------------

def test_the_lock_name_is_the_bots():
    from sessionphone.bridge import bot
    assert migrate.LOCK_NAME == bot.LOCK_NAME


def test_the_cli_lists_migrate():
    r = CliRunner().invoke(cli, ["bot", "--help"])
    assert "migrate" in r.output
    r = run("--help")
    assert r.exit_code == 0 and "--dry-run" in r.output


# ---- refusals: a live bot, a live call; nothing moved ----------------------------

def test_refuses_while_a_bot_holds_the_roots_lock(home, root):
    with lock_held(root / ".bot.lock"):
        before = snapshot(home)
        r = run()
        after = snapshot(home)
    assert r.exit_code == 2, r.output
    assert "a bot (pid 5150) runs on" in r.output and "Nothing was moved." in r.output
    assert before == after
    assert root.is_dir() and not root.is_symlink() and not os.path.lexists(new(home))


def test_refuses_while_a_profiles_bot_holds_its_lock(home, root):
    prof = root / "public"
    prof.mkdir(mode=0o700)
    (prof / ".bot.lock").write_text("")
    with lock_held(prof / ".bot.lock"):
        before = snapshot(home)
        r = run()
        after = snapshot(home)
    assert r.exit_code == 2, r.output
    assert str(prof) in r.output
    assert before == after


def test_dry_run_refuses_a_live_bot_too(home, root):
    with lock_held(root / ".bot.lock"):
        r = run("--dry-run")
    assert r.exit_code == 2, r.output


def test_refuses_a_lock_it_cannot_read(home, root):
    """Whether a bot runs cannot be told: the doubt refuses, no traceback."""
    (root / ".bot.lock").chmod(0o000)
    try:
        r = run()
    finally:
        (root / ".bot.lock").chmod(0o600)
    assert r.exit_code == 2, r.output
    assert "cannot be read" in r.output and "Nothing was moved." in r.output
    assert root.is_dir() and not root.is_symlink()


def test_dry_run_after_the_move_still_changes_nothing(home, root):
    p = agents(home) / f"{service.LABEL_PREFIX}.claude.plist"
    assert run().exit_code == 0
    raw = write_plist(p, {"SESSIONPHONE_ROOT": str(old(home))}, str(old(home)))
    r = run("--dry-run")
    assert r.exit_code == 0, r.output
    assert p.read_bytes() == raw and "would be re-rendered" in r.output
    assert r.output.rstrip().endswith("dry run:  nothing was changed")


def test_refuses_while_a_bot_runs_by_its_pid_file(home, root):
    with stand_in() as p:
        assert pidfile.write(root / "bot.pid", root, log=print, pid=p.pid)
        before = snapshot(home)
        r = run()
        after = snapshot(home)
    assert r.exit_code == 2, r.output
    assert "names a running bot" in r.output
    assert before == after


@pytest.fixture
def pid_check_off(monkeypatch):
    """held()'s own bot.pid check set aside (its first pidfile.started), so
    the CALL is what refuses: live_call asks pidfile.started again, for real."""
    real = pidfile.started
    seen = []

    def started(path, r, run=None):
        seen.append(path)
        return (None, "set aside") if len(seen) == 1 else real(path, r, run)
    monkeypatch.setattr(pidfile, "started", started)


def test_refuses_while_a_call_is_live(home, root, pid_check_off):
    """A live call: call-live.json written after its running bot started."""
    with stand_in() as p:
        assert pidfile.write(root / "bot.pid", root, log=print, pid=p.pid)
        call_up(root / "call-live.json")
        before = snapshot(home)
        r = run()
        after = snapshot(home)
    assert r.exit_code == 2, r.output
    assert "a call is live on" in r.output and "call msg 7 - chat 12" in r.output
    assert before == after


def test_refuses_a_live_call_a_jobs_own_dc_live_names(home, root, tmp_path, pid_check_off):
    """The job's plist may move call-live.json (DC_LIVE): migrate reads it there."""
    live = tmp_path / "elsewhere-live.json"
    write_plist(agents(home) / f"{service.LABEL_PREFIX}.claude.plist",
                {"SESSIONPHONE_ROOT": str(root), "DC_LIVE": str(live)}, str(root))
    with stand_in() as p:
        assert pidfile.write(root / "bot.pid", root, log=print, pid=p.pid)
        call_up(live)
        r = run()
    assert r.exit_code == 2, r.output
    assert str(live) in r.output


def test_a_stale_lock_and_a_stale_call_file_do_not_refuse(home, root):
    """A real host's root: a .bot.lock no bot holds, a call-live.json and bot.pid from a
    bot long gone. None of them is a live bot or a call."""
    call_up(root / "call-live.json")
    (root / "bot.pid").write_text("999999\nThu Oct 22 02:19:57 2026\nx sessionphone\n/nowhere\n")
    r = run()
    assert r.exit_code == 0, r.output
    assert new(home).is_dir()


# ---- the move: rename, link, nothing deleted ---------------------------------------

def test_moves_by_rename_and_leaves_a_link(home, root):
    db = root / "accounts" / UUID / "dc.db"
    inodes = {p.relative_to(root): os.lstat(p).st_ino for p in [root, *root.rglob("*")]}
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    r = run()
    assert r.exit_code == 0, r.output
    n = new(home)
    # the same inodes under the new root: renamed, never copied
    assert {p.relative_to(n): os.lstat(p).st_ino for p in [n, *n.rglob("*")]} == inodes
    assert hashlib.sha256((n / "accounts" / UUID / "dc.db").read_bytes()).hexdigest() == digest
    assert oct(os.stat(n).st_mode & 0o777) == oct(0o700)
    assert oct(os.stat(n / "accounts").st_mode & 0o777) == oct(0o700)
    # the old path is never deleted: it is a link to the new root
    o = old(home)
    assert o.is_symlink() and pathlib.Path(os.readlink(o)) == n
    assert (o / "callers.txt").read_text() == "owner@phone.example.org\n"
    assert f"old:      {o}" in r.output and f"new:      {n}" in r.output
    assert f"moved:    {o} to {n}, by rename" in r.output
    assert f"link:     {o} -> {n}" in r.output


def test_migrate_never_copies(home, root, monkeypatch):
    """No copy anywhere in the move: shutil's copiers and a copy+delete are out."""
    import shutil
    for name in ("copy", "copy2", "copyfile", "copytree", "move", "rmtree"):
        monkeypatch.setattr(shutil, name, lambda *a, **k: pytest.fail("copied or deleted"))
    r = run()
    assert r.exit_code == 0, r.output


def test_after_migrate_the_resolver_reads_the_new_root_and_says_nothing(home, root, capsys):
    assert run().exit_code == 0
    capsys.readouterr()
    paths._said_old = ""        # the home fixture restores it
    assert paths.root() == new(home)
    assert capsys.readouterr().err == "" and paths._said_old == ""


def test_refuses_when_the_new_root_is_there(home, root):
    new(home).mkdir(parents=True)
    before = snapshot(home)
    r = run()
    assert r.exit_code == 2, r.output
    assert "already there" in r.output and "never a merge" in r.output
    assert snapshot(home) == before


def test_refuses_across_filesystems_before_anything(home, root, monkeypatch):
    devs = {}

    def device(p):
        return devs.setdefault(str(p), len(devs))   # every path its own filesystem
    monkeypatch.setattr(migrate, "_device", device)
    before = snapshot(home)
    r = run()
    assert r.exit_code == 2, r.output
    assert "different filesystems" in r.output and "never copied" in r.output
    assert snapshot(home) == before


def test_a_rename_that_would_cross_filesystems_moves_nothing(home, root, monkeypatch):
    def exdev(a, b):
        raise OSError(errno.EXDEV, "Cross-device link")
    monkeypatch.setattr(migrate.os, "rename", exdev)
    r = run()
    assert r.exit_code == 2, r.output
    assert "different filesystems" in r.output and "Nothing was moved." in r.output
    assert root.is_dir() and not root.is_symlink()
    assert (root / "accounts" / UUID / "dc.db").exists()


def test_nothing_to_move(home):
    r = run()
    assert r.exit_code == 0, r.output
    assert "nothing to move" in r.output
    assert not os.path.lexists(old(home)) and not os.path.lexists(new(home))


def test_a_second_run_finds_it_moved(home, root):
    assert run().exit_code == 0
    before = snapshot(home)
    r = run()
    assert r.exit_code == 0, r.output
    assert "already" in r.output
    assert snapshot(home) == before


def test_refuses_an_old_path_that_links_elsewhere(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    old(home).symlink_to(elsewhere)
    r = run()
    assert r.exit_code == 2, r.output
    assert "not the root to move" in r.output
    assert old(home).is_symlink() and not os.path.lexists(new(home))


def test_an_explicit_root_is_never_touched(home, root, tmp_path, monkeypatch):
    mine = tmp_path / "mine"
    make_account(mine / "accounts")
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(mine))
    monkeypatch.setenv("DC_ROOT", str(mine))
    before = snapshot(mine)
    r = run()
    assert r.exit_code == 0, r.output
    assert snapshot(mine) == before
    assert new(home).is_dir() and old(home).is_symlink()


# ---- plists: the old root's re-rendered, every other left alone ---------------------

def test_plists_naming_the_old_root_are_re_rendered_and_others_left(home, root, tmp_path):
    a = agents(home)
    o, n = str(root), str(new(home))
    mine = write_plist(a / f"{service.LABEL_PREFIX}.claude.plist",
                       {"SESSIONPHONE_ROOT": o, "DC_AGENT": "claude",
                        "DC_CALLERS_FILE": f"{o}/callers.txt", "DC_STT_URL": "http://x/v1"}, o)
    legacy = write_plist(a / f"{service.LEGACY_LABEL_PREFIX}.claude.plist", {"SESSIONPHONE_ROOT": o}, o)
    other = write_plist(a / f"{service.LABEL_PREFIX}.alice.plist",
                        {"SESSIONPHONE_ROOT": str(tmp_path / "alice")}, str(tmp_path / "alice"))
    by_dc_root = write_plist(a / f"{service.LABEL_PREFIX}.pip.plist",
                             {"SESSIONPHONE_ROOT": o, "DC_ROOT": str(tmp_path / "pip")}, o)
    foreign = write_plist(a / "com.example.other.plist", {"SESSIONPHONE_ROOT": o}, o)
    r = run()
    assert r.exit_code == 0, r.output
    got = load(a / f"{service.LABEL_PREFIX}.claude.plist")
    assert got["EnvironmentVariables"] == {"SESSIONPHONE_ROOT": n, "DC_AGENT": "claude",
                                           "DC_CALLERS_FILE": f"{n}/callers.txt",
                                           "DC_STT_URL": "http://x/v1"}
    assert got["WorkingDirectory"] == n
    # only the root moved: the rest is as it was
    was = plistlib.loads(mine)
    assert {k: v for k, v in got.items() if k not in ("EnvironmentVariables", "WorkingDirectory")} == \
        {k: v for k, v in was.items() if k not in ("EnvironmentVariables", "WorkingDirectory")}
    assert oct((a / f"{service.LABEL_PREFIX}.claude.plist").stat().st_mode & 0o777) == oct(0o600)
    assert load(a / f"{service.LEGACY_LABEL_PREFIX}.claude.plist")["EnvironmentVariables"] == \
        {"SESSIONPHONE_ROOT": n}
    assert legacy != (a / f"{service.LEGACY_LABEL_PREFIX}.claude.plist").read_bytes()
    # an explicit other root, a DC_ROOT that wins, a plist not ours: byte for byte
    assert (a / f"{service.LABEL_PREFIX}.alice.plist").read_bytes() == other
    assert (a / f"{service.LABEL_PREFIX}.pip.plist").read_bytes() == by_dc_root
    assert (a / "com.example.other.plist").read_bytes() == foreign
    assert "re-rendered" in r.output and f"alice.plist left as it is: its root is {tmp_path / 'alice'}" in r.output


def test_a_plist_naming_the_old_root_by_tilde_counts(home, root):
    p = agents(home) / f"{service.LABEL_PREFIX}.claude.plist"
    write_plist(p, {"SESSIONPHONE_ROOT": "~/.sessionphone"}, str(root))
    assert run().exit_code == 0
    assert load(p)["EnvironmentVariables"]["SESSIONPHONE_ROOT"] == str(new(home))


def test_a_second_run_re_renders_a_plist_the_first_left(home, root):
    """A run that moved the root but could not write a plist is finished by
    the next one: the old path is the link, the plist still names it."""
    assert run().exit_code == 0
    p = agents(home) / f"{service.LABEL_PREFIX}.claude.plist"
    write_plist(p, {"SESSIONPHONE_ROOT": str(old(home))}, str(old(home)))
    r = run()
    assert r.exit_code == 0, r.output
    assert load(p)["EnvironmentVariables"]["SESSIONPHONE_ROOT"] == str(new(home))


def test_a_plist_that_cannot_be_written_fails_after_the_move(home, root, monkeypatch):
    p = agents(home) / f"{service.LABEL_PREFIX}.claude.plist"
    write_plist(p, {"SESSIONPHONE_ROOT": str(root)}, str(root))

    def boom(*a, **k):
        raise OSError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(migrate, "write_plist", boom)
    r = run()
    assert r.exit_code == 1, r.output
    assert "the root is moved" in r.output and "migrate` again" in r.output
    assert new(home).is_dir() and old(home).is_symlink()


# ---- --dry-run: nothing changes; the account's new path is said --------------------

def test_dry_run_changes_nothing_and_says_where_the_account_opens(home, root):
    p = agents(home) / f"{service.LABEL_PREFIX}.claude.plist"
    raw = write_plist(p, {"SESSIONPHONE_ROOT": str(root)}, str(root))
    before = snapshot(home)
    r = run("--dry-run")
    assert r.exit_code == 0, r.output
    assert snapshot(home) == before
    assert p.read_bytes() == raw
    assert f"account:  would open at {new(home) / 'accounts'} (1 account, keys kept" in r.output
    assert "would rename" in r.output and "would leave" in r.output
    assert "would be re-rendered" in r.output
    assert r.output.rstrip().endswith("dry run:  nothing was changed")


def test_dry_run_names_a_profiles_account_too(home, root):
    prof = root / "public"
    prof.mkdir(mode=0o700)
    (prof / ".bot.lock").write_text("")
    make_account(prof / "accounts")
    r = run("--dry-run")
    assert r.exit_code == 0, r.output
    assert f"would open at {new(home) / 'public' / 'accounts'}" in r.output



def test_dry_run_names_an_account_of_a_profile_that_never_ran_here(home, root):
    """A profile set up (accounts/ made) but never run on this host holds no
    lock, pid or call file: its account is still named (do-001 review, minor 1)."""
    prof = root / "public"
    prof.mkdir(mode=0o700)
    make_account(prof / "accounts")
    assert not any((prof / n).exists() for n in (".bot.lock", "bot.pid", "call-live.json"))
    before = snapshot(home)
    r = run("--dry-run")
    assert r.exit_code == 0, r.output
    assert snapshot(home) == before
    assert f"would open at {new(home) / 'public' / 'accounts'} (1 account, keys kept" in r.output
    assert f"would open at {new(home) / 'accounts'} (1 account" in r.output


def test_homes_counts_a_folder_holding_accounts_but_not_any_folder(root):
    (root / "public" / "accounts").mkdir(parents=True)
    (root / "scratch").mkdir()
    found = migrate.homes(root)
    assert root / "public" in found
    assert root / "scratch" not in found
    assert found[0] == root


def test_a_profile_with_only_accounts_does_not_block_the_move(home, root):
    """Counting an accounts-only profile adds no refusal: no lock, pid or call
    is found there, and the move goes ahead with its keys."""
    prof = root / "public"
    prof.mkdir(mode=0o700)
    make_account(prof / "accounts")
    r = run()
    assert r.exit_code == 0, r.output
    assert (new(home) / "public" / "accounts").is_dir()
    assert not (new(home) / "public" / ".bot.lock").exists()

# ---- the account opens at the new path with the same keys --------------------------

class _FakeAccount:
    def __init__(self, d: pathlib.Path):
        self.dir = d

    def is_configured(self):
        return True

    def keys(self):
        with contextlib.closing(sqlite3.connect(f"file:{self.dir / 'dc.db'}?mode=ro", uri=True)) as c:
            return c.execute("SELECT private_key, public_key FROM keypairs").fetchall()


class _FakeRpc:
    """The rpc-server, faked: it opens the accounts folder it is given."""
    opened: ClassVar[list[pathlib.Path]] = []

    def __init__(self, accounts_dir, rpc_server_path=None):
        self.where = pathlib.Path(accounts_dir)
        _FakeRpc.opened.append(self.where)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDeltaChat:
    def __init__(self, rpc):
        self.rpc = rpc

    def get_all_accounts(self):
        return [_FakeAccount(d) for d in sorted(self.rpc.where.iterdir()) if (d / "dc.db").exists()]


def _open_account():
    gen = account._open()
    try:
        acct = next(gen)
        return acct.dir, acct.keys()
    finally:
        gen.close()


def test_the_fixture_account_opens_at_the_new_path_with_the_same_keys(home, root, monkeypatch):
    monkeypatch.setattr(account, "Rpc", _FakeRpc)
    monkeypatch.setattr(account, "DeltaChat", _FakeDeltaChat)
    monkeypatch.setattr(account, "server_path", lambda: "rpc-server")
    _FakeRpc.opened = []
    where, keys = _open_account()           # before: the old root, by the fallback
    assert where.parent == old(home) / "accounts"
    assert keys == [(PRIVATE, PUBLIC)]
    assert run().exit_code == 0
    monkeypatch.setattr(paths, "_said_old", "")
    where, after = _open_account()          # after: the new root, no fallback
    assert _FakeRpc.opened[-1] == new(home) / "accounts"
    assert where == new(home) / "accounts" / UUID
    assert after == keys

"""One root is one Delta Chat account, so `bot install` keeps one bot on it.

A bot renamed by a second install (a durable seat now names it, or DC_AGENT
changed) used to leave the old bot's launchd job running on the same root:
two bots on one account, the old one still mailing its own box, and install
said nothing. Now install refuses while phone.json records another bot on
the same root (its SESSIONPHONE_ROOT, DC_ROOT or DC_ACCOUNTS), naming it and
the ways past it, and writes nothing. `--replace NAME` retires that bot: its
job is booted out, its plist removed, and phone.json forgets it once the new
job is loaded.

launchd is a per-label fake below (conftest's FakeLaunchd holds one job), HOME
is a tmp dir, and sessionmail is the suite's stub: nothing real is touched.
"""
import json
import os
import pathlib
import plistlib
import subprocess

import pytest
from click.testing import CliRunner

from sessionphone import reach, service, settings
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

OWNER = "owner@phone.example.org"
DEFAULT_FILE = settings.FILE            # read at import, before conftest moves it


class Launchd:
    """launchd with one state per label: print finds a loaded label, bootout
    unloads it (unless it is STUCK), bootstrap loads the plist's own label."""

    def __init__(self):
        self.loaded: set[str] = set()
        self.stuck: set[str] = set()
        self.calls: list[tuple] = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        verb, target = args[0], args[-1]
        lab = target.rsplit("/", 1)[-1]
        if verb == "print":
            if lab in self.loaded:
                return subprocess.CompletedProcess(args, 0, "state = running\npid = 4242\n", "")
            return subprocess.CompletedProcess(args, 113, "", f'Could not find service "{lab}"')
        if verb == "bootout":
            if lab not in self.loaded:
                return subprocess.CompletedProcess(args, 3, "", "Boot-out failed: 3: No such process")
            if lab not in self.stuck:
                self.loaded.discard(lab)
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "bootstrap":
            self.loaded.add(plistlib.loads(pathlib.Path(target).read_bytes())["Label"])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected launchctl verb: {args}")


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("CLAUDE_CODE_AGENT", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings, "FILE", DEFAULT_FILE)      # under the scratch HOME
    for k in ("XDG_BIN_HOME", "XDG_DATA_HOME", "UV_TOOL_BIN_DIR"):   # no ears shim found
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    callers = tmp_path / "callers.txt"
    callers.write_text(f"{OWNER}\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    chans = tmp_path / "channels.txt"
    chans.write_text(f"owner  {OWNER}\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    fake = Launchd()
    monkeypatch.setattr(service, "_launchctl", fake)
    fake.home, fake.root, fake.tmp = home, root, tmp_path

    def as_(name: str | None):
        """This shell names bot NAME (None: it names none), with NAME's signer given."""
        if name is None:
            monkeypatch.delenv("DC_AGENT", raising=False)
        else:
            monkeypatch.setenv("DC_AGENT", name)
        given_seat(f"{name or 'claude'}-chat")
    fake.as_ = as_
    return fake


def _install(*args):
    return CliRunner().invoke(cli, ["bot", "install", *args])


def _plist(name: str) -> pathlib.Path:
    return service.plist_path(name)


def _bots() -> list[str]:
    p = settings.file_path()
    return sorted(json.loads(p.read_text())["bots"]) if p.exists() else []


def _installed(rig, name: str):
    rig.as_(name)
    r = _install()
    assert r.exit_code == 0, r.output
    assert service.label(name) in rig.loaded


# ---- the refusal ------------------------------------------------------------------

def test_a_rename_on_the_same_root_is_refused_naming_the_old_bot(rig):
    _installed(rig, "claude")
    old_plist = _plist("claude").read_bytes()
    saved = settings.file_path().read_bytes()
    rig.as_("ada")
    r = _install()
    assert r.exit_code == 2, r.output
    out = " ".join(r.output.split())
    assert "records bot claude on the same root as bot ada" in out
    assert str(rig.root.name) in out
    assert "the old one keeps mailing its own box (claude)" in out
    assert "`sessionphone bot install --replace claude`" in out
    assert "`DC_AGENT=claude sessionphone bot install`" in out
    assert "`SESSIONPHONE_ROOT=DIR sessionphone bot install`" in out
    # nothing written, nothing booted out
    assert not _plist("ada").exists()
    assert _plist("claude").read_bytes() == old_plist
    assert settings.file_path().read_bytes() == saved
    assert service.label("claude") in rig.loaded
    # the one bootout of claude is the first install's own, before its bootstrap
    assert rig.calls.count(("bootout", f"{service.domain()}/{service.label('claude')}")) == 1


def test_a_plain_install_then_a_durable_seat_is_the_refusal_not_a_second_bot(rig, monkeypatch,
                                                                             given_seat):
    """The field case: the first install named no bot (claude); a later one,
    from a session started as ada, is named ada by sessionmail's answer."""
    _installed(rig, None)
    assert _bots() == ["claude"]
    monkeypatch.setattr(reach, "ask", lambda shell, env=None, exe=None: reach.Reach(
        answered=True, session=True, seat="ada", addresses=frozenset({"ada"})))
    reach._ASKED.clear()
    rig.as_(None)
    given_seat("ada-chat")
    r = _install()
    assert r.exit_code == 2, r.output
    assert "records bot claude on the same root as bot ada" in " ".join(r.output.split())
    assert not _plist("ada").exists()


def test_the_same_accounts_folder_on_another_root_is_refused_too(rig, monkeypatch, tmp_path):
    shared = str(tmp_path / "accounts")
    monkeypatch.setenv("DC_ACCOUNTS", shared)
    _installed(rig, "claude")
    other = tmp_path / "other-root"
    other.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(other))
    rig.as_("ada")
    r = _install()
    assert r.exit_code == 2, r.output
    assert "records bot claude on the same root as bot ada" in " ".join(r.output.split())


def test_another_root_is_another_account_and_installs_beside_it(rig, monkeypatch, tmp_path):
    _installed(rig, "claude")
    other = tmp_path / "other-root"
    other.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(other))
    _installed(rig, "ada")
    assert _bots() == ["ada", "claude"]
    assert {service.label("ada"), service.label("claude")} <= rig.loaded


def test_reinstalling_the_same_bot_is_no_rival(rig):
    _installed(rig, "claude")
    _installed(rig, "claude")
    assert _bots() == ["claude"]


def test_a_recorded_bot_with_no_job_is_still_named(rig):
    """`bot uninstall` keeps the bot in phone.json: it is still a bot on this
    root, so it is named, and --replace forgets it."""
    _installed(rig, "claude")
    service.uninstall("claude")
    rig.as_("ada")
    r = _install()
    assert r.exit_code == 2, r.output
    r = _install("--replace", "claude")
    assert r.exit_code == 0, r.output
    assert (f"replaced bot claude, on the same root ({settings.shown(rig.root)}): launchd job "
            f"{service.label('claude')} was not installed") in r.output
    assert _bots() == ["ada"]


# ---- --replace ----------------------------------------------------------------------

def test_replace_retires_the_old_bot_and_phone_json_forgets_it(rig):
    _installed(rig, "claude")
    rig.as_("ada")
    r = _install("--replace", "Claude")
    assert r.exit_code == 0, r.output
    assert (f"replaced bot claude, on the same root ({settings.shown(rig.root)}): launchd job "
            f"{service.label('claude')} booted out, plist removed") in r.output
    assert (f"  replaced bot claude (it kept the same root, {settings.shown(rig.root)}): "
            "removed from ~/.sessionkit/phone/settings/phone.json") in r.output
    assert service.label("claude") not in rig.loaded
    assert not _plist("claude").exists()
    assert service.label("ada") in rig.loaded
    assert _bots() == ["ada"]
    # the old job went before the new one was started
    order = [c for c in rig.calls if c[0] in ("bootout", "bootstrap")]
    assert order.index(("bootout", f"{service.domain()}/{service.label('claude')}"), 1) < \
        next(i for i, c in enumerate(order) if c[0] == "bootstrap" and "ada" in c[-1])


def test_replace_also_retires_the_old_bots_legacy_job(rig):
    _installed(rig, "claude")
    legacy = service.legacy_plist_path("claude")
    legacy.write_bytes(plistlib.dumps({"Label": service.legacy_label("claude")}))
    rig.loaded.add(service.legacy_label("claude"))
    rig.as_("ada")
    r = _install("--replace", "claude")
    assert r.exit_code == 0, r.output
    assert f"retired legacy launchd job {service.legacy_label('claude')} (plist removed)" in r.output
    assert not legacy.exists()
    assert service.legacy_label("claude") not in rig.loaded


def test_a_replaced_bot_launchd_still_lists_stops_the_install(rig):
    _installed(rig, "claude")
    rig.stuck.add(service.label("claude"))
    rig.as_("ada")
    r = _install("--replace", "claude")
    assert r.exit_code == 1, r.output
    assert "is still going" in r.output
    assert "Re-run `sessionphone bot install --replace claude`" in r.output
    assert not _plist("ada").exists()
    assert service.label("ada") not in rig.loaded
    assert _bots() == ["claude"]          # forgotten only once the new job is loaded


@pytest.mark.parametrize("flag, says", [
    ("ada", "--replace ada: that is the bot this install is"),
    ("bob", "--replace bob: ~/.sessionkit/phone/settings/phone.json records no other bot bob"),
])
def test_a_replace_that_cannot_be_done_is_refused(rig, flag, says):
    _installed(rig, "claude")
    saved = settings.file_path().read_bytes()
    rig.as_("ada")
    r = _install("--replace", flag)
    assert r.exit_code == 2, r.output
    assert says in " ".join(r.output.split())
    assert settings.file_path().read_bytes() == saved
    assert not _plist("ada").exists()


def test_replace_of_a_bot_on_another_root_is_refused(rig, monkeypatch, tmp_path):
    _installed(rig, "claude")
    other = tmp_path / "other-root"
    other.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(other))
    rig.as_("ada")
    r = _install("--replace", "claude")
    assert r.exit_code == 2, r.output
    assert "records no other bot claude on this bot's root" in " ".join(r.output.split())
    assert service.label("claude") in rig.loaded


def test_a_rival_only_a_drop_in_records_is_left_to_that_file(rig):
    _installed(rig, "claude")
    d = settings.file_path().with_name(settings.DROPINS)
    d.mkdir()
    (d / "10-bob.json").write_text(json.dumps({"bots": {"bob": {"SESSIONPHONE_ROOT": str(rig.root)}}}))
    rig.as_("ada")
    r = _install("--replace", "claude", "--replace", "bob")
    assert r.exit_code == 2, r.output
    assert "--replace bob: it is recorded in" in r.output
    assert "10-bob.json, which install does not write" in " ".join(r.output.split())


def test_two_rivals_are_both_named_and_both_flags_given(rig, monkeypatch):
    _installed(rig, "claude")
    data = json.loads(settings.file_path().read_text())
    data["bots"]["bob"] = {"SESSIONPHONE_ROOT": str(rig.root)}
    settings.file_path().write_text(json.dumps(data))
    rig.as_("ada")
    r = _install()
    assert r.exit_code == 2, r.output
    out = " ".join(r.output.split())
    assert "records bots bob, claude on the same root as bot ada" in out
    assert "`sessionphone bot install --replace bob --replace claude`" in out
    r = _install("--replace", "bob", "--replace", "claude")
    assert r.exit_code == 0, r.output
    assert _bots() == ["ada"]


# ---- the pure parts ------------------------------------------------------------------

def test_place_reads_the_root_as_the_job_does(tmp_path):
    home_root = os.path.realpath(os.path.expanduser("~/.sessionphone"))
    assert settings.place({}) == (home_root, os.path.join(home_root, "accounts"))
    r = str(tmp_path / "r")
    assert settings.place({"SESSIONPHONE_ROOT": r})[0] == os.path.realpath(r)
    assert settings.place({"SESSIONPHONE_ROOT": "/x", "DC_ROOT": r})[0] == os.path.realpath(r)
    assert settings.place({"DC_ACCOUNTS": str(tmp_path / "a")})[1] == os.path.realpath(tmp_path / "a")

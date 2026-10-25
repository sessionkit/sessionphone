"""Every reader of a setting `bot install` keeps reads it the way install does:
this shell, then the file that keeps it, then the default.

Install reads its own settings from the phone tool's host file
(~/.sessionkit/phone/settings/phone.json, one map per bot), and the mail-route
settings are sessionmail's, kept in its own mail.json and asked of the
installed sessionmail itself. Other readers still read the environment alone:

- the mail host (dc_env.mail_host, and so dc_env.mailto: the bot's default
  To) and the carrier the bot names (seatmail.carrier) never ask sessionmail,
  so a host or carrier kept only in mail.json gives a wrong answer;
- `bot status`, `bot uninstall` and paths.root() never read phone.json, so a
  bot and root it records are found by install alone;
- two messages are false: a failed save after the job is loaded exits before
  `installed:` is said, and a lock on a key no file sets refuses "at None".

Each of those was measured here as a strict xfail first; they pass now. The
tests beside them are what must stay true: this shell first, the default last.

Fakes only: FakeLaunchd (conftest), plists in tmp_path, a scratch HOME, and a
`message` whose own Python is a shell script that answers as a sessionmail
that reads mail.json would.
"""
import json
import os
import pathlib
import plistlib

import pytest
from click.testing import CliRunner

from sessionphone import paths, service, settings
from sessionphone.bridge import dc_env, seatmail
from sessionphone.cli import main as cli

OWNER = "owner@phone.example.org"
DEFAULT_FILE = settings.FILE            # read at import, before conftest moves it
REAL_MAIL_FILE = settings.mail_file     # conftest stubs it per test; these tests ask

MAIL_ENV = ("SESSIONMAIL_SENDMAIL", "SESSIONMAIL_HOST", "SESSION_MAIL_HOST",
            "SESSIONMAIL_ROOT", "SESSIONMAIL_RELAY_DOMAIN", "SESSIONMAIL_HOME")


# ---- a sessionmail that reads its own mail.json -----------------------------------

def _sessionmail(tmp_path, **keys) -> str:
    """A `message` whose interpreter answers sessionmail's settings probe with
    KEYS ({mail.json key: (value, source)}; the rest unset, from the default).
    -> the path of that `message`."""
    answer = {"sendmail": [None, "default"], "root": [None, "default"],
              "host": [None, "default"], "postfix.relay_domain": [None, "default"]}
    answer.update({k: list(v) for k, v in keys.items()})
    d = tmp_path / "sessionmail"
    d.mkdir()
    py = d / "python3"
    py.write_text(f"#!/bin/sh\nprintf '%s\\n' '{json.dumps(answer)}'\n")
    py.chmod(0o755)
    exe = d / "message"
    exe.write_text(f"#!{py}\nprint('a message script')\n")
    exe.chmod(0o755)
    return str(exe)


@pytest.fixture
def mail_json(tmp_path, monkeypatch):
    """-> make(**keys): an environment like the bot's job, with no mail-route
    variable set and DC_MESSAGE naming a sessionmail whose mail.json holds
    KEYS. This machine's own name is othermac, so a reader that falls back
    to it is seen."""
    for k in MAIL_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(settings, "mail_file", REAL_MAIL_FILE)
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "othermac.local")

    def make(**keys):
        return {"DC_AGENT": "ada", "DC_MESSAGE": _sessionmail(tmp_path, **keys),
                "PATH": "/usr/bin:/bin"}
    return make


def test_the_fake_sessionmail_says_it_reads_mail_json(mail_json):
    env = mail_json(host=("mailhost", "host"))
    assert REAL_MAIL_FILE(env["DC_MESSAGE"])["host"] == ("mailhost", "host")


# ---- (1a) the mail host, and the bot's default To built on it ----------------------

def test_mail_host_is_the_host_sessionmail_keeps_in_mail_json(mail_json):
    env = mail_json(host=("mailhost", "host"))
    assert dc_env.mail_host(env) == "mailhost"


def test_the_default_to_is_bare_whatever_the_mail_json_host(mail_json):
    env = mail_json(host=("mailhost", "host"))
    assert dc_env.mail_host(env) == "mailhost"
    assert dc_env.mailto(env) == "ada"           # bare (PHONE-248): the host never reaches the To


def test_a_mail_host_in_this_shell_wins_over_mail_json(mail_json):
    env = {**mail_json(host=("mailhost", "host")), "SESSIONMAIL_HOST": "shellhost"}
    assert dc_env.mail_host(env) == "shellhost"
    assert dc_env.mailto(env) == "ada"


def test_with_no_host_anywhere_the_mail_host_is_this_machines_name(mail_json):
    env = mail_json()
    assert dc_env.mail_host(env) == "othermac"
    assert dc_env.mailto(env) == "ada"


# ---- (1b) the carrier the bot names ------------------------------------------------

@pytest.mark.parametrize("source", ["host", "default"])
def test_carrier_names_the_drop_sessionmail_reads(mail_json, source):
    """`drop` from mail.json, or as sessionmail's own default: the bot's mail
    goes by the drop, and no MTA is named."""
    said = seatmail.carrier(mail_json(sendmail=("drop", source)))
    assert said.startswith(seatmail.DROP)
    assert seatmail.DEFAULT_MTA not in said


def test_carrier_names_the_mta_mail_json_names(mail_json):
    said = seatmail.carrier(mail_json(sendmail=("/opt/mta/sendmail", "host")))
    assert said.startswith("MTA /opt/mta/sendmail")


def test_a_carrier_in_this_shell_wins_over_mail_json(mail_json):
    env = {**mail_json(sendmail=("/opt/mta/sendmail", "host")), "SESSIONMAIL_SENDMAIL": "drop"}
    assert seatmail.carrier(env).startswith(seatmail.DROP)


# ---- (2) the bot and root phone.json records: paths.root, bot status, bot uninstall -

@pytest.fixture
def phone_home(monkeypatch, tmp_path):
    """A scratch HOME with phone.json in its real place, a bot named only by
    the file, and plists named by label, as launchd keeps them."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_", "SESSIONMAIL_"))]:
        monkeypatch.delenv(k)
    for k in ("SESSIONPHONE_ROOT", "CLAUDE_CODE_AGENT"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings, "FILE", DEFAULT_FILE)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    monkeypatch.setattr(service, "plist_path",
                        lambda name=None: agents / f"{service.label(name)}.plist")
    return home


def _remember(root: pathlib.Path, name: str = "ada") -> None:
    """phone.json as install leaves it: one bot, its root."""
    p = pathlib.Path(DEFAULT_FILE).expanduser()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"bots": {name: {"SESSIONPHONE_ROOT": str(root)}}}))


def _installed(root: pathlib.Path, name: str = "ada") -> pathlib.Path:
    """A plist for bot NAME as install writes it, run from ROOT."""
    p = service.plist_path(name)
    p.write_bytes(plistlib.dumps(service.job(name, python="/venv/libexec/session-phone",
                                             given={"SESSIONPHONE_ROOT": str(root)})))
    return p


@pytest.fixture
def remembered(phone_home, tmp_path):
    """A root phone.json records for its one bot, ada, with a log line in it."""
    root = tmp_path / "kept-root"
    root.mkdir(mode=0o700)
    (root / "bot.log").write_text("2030-06-09 00:00:00 the line in the kept root\n")
    _remember(root)
    return root


def test_paths_root_is_the_root_phone_json_records(remembered):
    assert paths.root() == remembered


def test_a_root_in_this_shell_wins_over_phone_json(remembered, monkeypatch, tmp_path):
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "shell-root"))
    assert paths.root() == tmp_path / "shell-root"


def test_with_no_root_anywhere_paths_root_is_the_default(phone_home):
    assert paths.root() == phone_home / ".sessionphone"


def test_bot_status_finds_the_bot_phone_json_records(remembered, launchd):
    _installed(remembered)
    r = CliRunner().invoke(cli, ["bot", "status"])
    assert f"job:     {service.LABEL_PREFIX}.ada (installed)" in r.output


def test_bot_status_reads_the_log_in_the_root_phone_json_records(remembered, launchd, monkeypatch):
    """The shell names the bot; its plist is gone (uninstalled), its log stays
    in the root phone.json records."""
    monkeypatch.setenv("DC_AGENT", "ada")
    r = CliRunner().invoke(cli, ["bot", "status"])
    assert f"log:     {remembered / 'bot.log'}" in r.output
    assert "the line in the kept root" in r.output


def test_bot_uninstall_removes_the_bot_phone_json_records(remembered, launchd):
    p = _installed(remembered)
    r = CliRunner().invoke(cli, ["bot", "uninstall"])
    assert r.exit_code == 0, r.output
    assert f"removed launchd job {service.LABEL_PREFIX}.ada" in r.output
    assert not p.exists()


def test_a_bot_this_shell_names_wins_over_phone_json(remembered, launchd, monkeypatch):
    p = _installed(remembered, "bo")
    monkeypatch.setenv("DC_AGENT", "bo")
    r = CliRunner().invoke(cli, ["bot", "uninstall"])
    assert f"removed launchd job {service.LABEL_PREFIX}.bo" in r.output
    assert not p.exists()


# ---- (3) messages that say what is true --------------------------------------------

@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    """A first install, as the settings tests make it: the bot ada, its root,
    its signer, the drop."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_", "SESSIONMAIL_"))]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("CLAUDE_CODE_AGENT", raising=False)
    pub = given_seat("ada-chat")
    (pub.parent / "agent.sock").touch()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings, "FILE", DEFAULT_FILE)
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    for k, v in {"DC_AGENT": "ada", "DC_HOST": "mac.example.net", "DC_VOICE": "af_sky",
                 "SESSIONMAIL_SENDMAIL": "drop"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(service, "ears_src", lambda: None)
    # this machine is mac, so the To (ada@mac.example.net) is on this host,
    # whatever host runs the suite: off it, the drop refuses the install
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "mac.local")
    return tmp_path / "job.plist"


def test_a_failed_save_says_installed_first_then_exits_1(rig, monkeypatch):
    """The job is loaded; only phone.json could not be written. The operator is
    told the bot is installed and running, then why its settings were not kept."""
    assert CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER]).exit_code == 0
    folder = pathlib.Path(DEFAULT_FILE).expanduser().parent
    monkeypatch.setenv("DC_VOICE", "af_bella")         # a change, so there is something to save
    folder.chmod(0o500)
    try:
        r = CliRunner().invoke(cli, ["bot", "install"])
    finally:
        folder.chmod(0o700)
    assert r.exit_code == 1
    assert "were NOT saved" in r.output
    assert "installed: " in r.output
    assert r.output.index("installed: ") < r.output.index("were NOT saved")


def test_a_failed_save_still_leaves_the_job_loaded(rig, launchd, monkeypatch):
    assert CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER]).exit_code == 0
    folder = pathlib.Path(DEFAULT_FILE).expanduser().parent
    monkeypatch.setenv("DC_VOICE", "af_bella")
    folder.chmod(0o500)
    try:
        r = CliRunner().invoke(cli, ["bot", "install"])
    finally:
        folder.chmod(0o700)
    assert r.exit_code == 1
    assert launchd.loaded
    assert plistlib.loads(rig.read_bytes())["EnvironmentVariables"]["DC_VOICE"] == "af_bella"


def _lock_unset(phone_home) -> None:
    p = pathlib.Path(DEFAULT_FILE).expanduser()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"_locked": ["bots.ada.DC_VOICE"], "bots": {"ada": {}}}))


def test_a_lock_on_a_key_no_file_sets_says_so(phone_home):
    _lock_unset(phone_home)
    with pytest.raises(settings.Bad) as got:
        settings.resolve({"DC_AGENT": "ada", "DC_VOICE": "af_bella"})
    said = str(got.value)
    assert "DC_VOICE is locked by ~/.sessionkit/phone/settings/phone.json" in said
    assert "None" not in said

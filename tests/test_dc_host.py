"""PHONE-98 gap (a): a junk mail host is refused, loudly, never mailed.

With no DC_HOST, dc_env fell back to socket.getfqdn(), and on macOS that
can be a reverse-DNS name (1.0.0...ip6.arpa). ADDR and MAILTO became
<agent>@<junk>, and incoming chat was mailed to a dead address while the bot
looked healthy (measured live on a user's Mac, Sun 2030-06-09). Now
the bot refuses to start, and `bot install` refuses to install, with a line
naming DC_HOST. Nothing here touches launchd or the real root: _launchctl
and the log are faked.
"""
import os
import plistlib

import pytest
from click.testing import CliRunner

from sessionphone import service
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

JUNK = [
    "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa",
    "4.3.2.1.in-addr.arpa",
    "",
    "localhost",
    "LOCALHOST.",
    "macbook",           # no dot: a bare machine name is not a mail domain
]
GOOD = ["mac.example.net", "box.example.org"]


@pytest.mark.parametrize("fqdn", JUNK)
def test_no_dc_host_and_a_junk_fqdn_is_refused_naming_dc_host(fqdn):
    why = dc_env.host_problem({}, fqdn=fqdn)
    assert why and "DC_HOST" in why


@pytest.mark.parametrize("fqdn", GOOD)
def test_a_good_fqdn_still_starts(fqdn):
    assert dc_env.host_problem({}, fqdn=fqdn) is None


@pytest.mark.parametrize("fqdn", JUNK)
def test_a_dc_host_is_trusted_whatever_the_fqdn(fqdn):
    assert dc_env.host_problem({"DC_HOST": "mac.example.net"}, fqdn=fqdn) is None
    assert dc_env.host_problem({"PIP_CHAT_HOST": "mac.example.net"}, fqdn=fqdn) is None


def test_an_empty_dc_host_counts_as_unset():
    assert "DC_HOST" in dc_env.host_problem({"DC_HOST": ""}, fqdn="x.ip6.arpa")


def test_host_is_moot_when_both_addresses_are_named():
    """HOST then only mints Message-IDs: no address is built from it."""
    both = {"DC_ADDR": "ada-chat@mac.example.net", "DC_MAILTO": "ada@mac.example.net"}
    assert dc_env.host_problem(both, fqdn="x.ip6.arpa") is None
    for one in ("DC_ADDR", "DC_MAILTO"):   # one address still built from the junk host
        assert "DC_HOST" in dc_env.host_problem({one: both[one]}, fqdn="x.ip6.arpa")


def test_the_reason_names_the_junk_address_and_the_fix():
    why = dc_env.host_problem({"DC_AGENT": "ada"}, fqdn="1.0.ip6.arpa")
    assert "ada@1.0.ip6.arpa" in why
    assert "DC_HOST=" in why and "sessionphone bot install" in why


def test_import_never_crashes_it_records_the_problem():
    assert dc_env.HOST_PROBLEM == dc_env.host_problem()


# --- the bot refuses in main(), before it opens the account ----------------

@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.setenv("DC_AGENT", "ada")          # bot.py reads argv[1] when it is unset
    from sessionphone.bridge import bot as b
    monkeypatch.setattr(b, "LOG", tmp_path / "bot.log")
    monkeypatch.setattr(b, "check_root", lambda: None)
    monkeypatch.setattr(b, "Rpc", lambda **k: pytest.fail("opened the account despite a junk host"))
    return b


def test_the_bot_refuses_to_start_on_a_junk_host(bot, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(dc_env, "HOST_PROBLEM", dc_env.host_problem({}, fqdn="1.0.ip6.arpa"))
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code != 0
    assert "DC_HOST" in capsys.readouterr().err
    assert "DC_HOST" in (tmp_path / "bot.log").read_text()


def test_the_bot_passes_the_host_check_on_a_good_host(bot, monkeypatch):
    monkeypatch.setattr(dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot, "OUTBOX", bot.LOG.parent / "outbox")
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "server_choice", lambda: ("/x", "venv", "0"))

    class Reached(Exception):
        pass

    def rpc(**k):
        raise Reached
    monkeypatch.setattr(bot, "Rpc", rpc)
    with pytest.raises(Reached):     # got as far as opening the account
        bot.main()


# --- `bot install` refuses too, before it touches launchd ------------------

@pytest.fixture
def rig(monkeypatch, tmp_path, launchd):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    callers = tmp_path / "callers.txt"   # someone may ring (gap 6); DC_CALLERS grants nothing
    callers.write_text("owner@phone.example.org\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    chans = tmp_path / "channels.txt"   # somewhere the bot may send (PHONE-102 rule 6)
    chans.write_text("owner  owner@phone.example.org\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    return launchd.calls        # conftest's FakeLaunchd: every launchctl call, none real


def test_install_refuses_a_junk_host_and_leaves_the_old_job_alone(rig, monkeypatch, tmp_path):
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "1.0.ip6.arpa")
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2
    assert "DC_HOST" in r.output
    assert rig == []                                 # no bootout, no bootstrap
    assert not (tmp_path / "job.plist").exists()


def test_install_judges_the_host_the_job_will_carry(rig, monkeypatch, tmp_path):
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "1.0.ip6.arpa")
    monkeypatch.setenv("DC_HOST", "mac.example.net")  # carried into the plist, so it is good
    service.install("ada")
    j = plistlib.loads((tmp_path / "job.plist").read_bytes())
    assert j["EnvironmentVariables"]["DC_HOST"] == "mac.example.net"
    assert ("bootstrap",) in [c[:1] for c in rig]

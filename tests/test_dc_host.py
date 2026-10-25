"""PHONE-98 gap (a): a junk mail host is refused, loudly, never mailed.

With no DC_HOST, dc_env fell back to socket.getfqdn(), and on macOS that
can be a reverse-DNS name (1.0.0...ip6.arpa). ADDR and MAILTO became
<agent>@<junk>, and incoming chat was mailed to a dead address while the bot
looked healthy (measured live on a user's Mac, Sun 2030-06-09). The default
host is now sessionmail's own (dc_env.mail_host), the host its carriers
deliver this machine's boxes on; one that names no machine (empty,
localhost) still makes the bot refuse to start, and `bot install` refuse to
install, with a line naming DC_HOST. Nothing here touches launchd or the real root: _launchctl
and the log are faked.
"""
import os
import pathlib
import plistlib
import re

import pytest
from click.testing import CliRunner

from sessionphone import service
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

JUNK_FQDN = [
    "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa",
    "4.3.2.1.in-addr.arpa",
    "",
    "localhost",
    "macbook",
]
# a mail host names no machine: empty, or localhost
JUNK = ["", "localhost", "LOCALHOST.", "localhost.localdomain"]
# sessionmail's short host is a mail host: it has no dot by design
GOOD = ["stranger", "macbook", "mac.example.net", "box.example.org"]


@pytest.mark.parametrize("host", JUNK)
def test_no_dc_host_and_a_junk_mail_host_is_refused_naming_both_settings(host):
    why = dc_env.host_problem({}, host=host)
    assert why and "DC_HOST" in why and "SESSIONMAIL_HOST" in why


@pytest.mark.parametrize("host", GOOD)
def test_a_short_or_dotted_mail_host_starts(host):
    assert dc_env.host_problem({}, host=host) is None


@pytest.mark.parametrize("host", JUNK)
def test_a_dc_host_is_trusted_whatever_the_mail_host(host):
    assert dc_env.host_problem({"DC_HOST": "mac.example.net"}, host=host) is None
    assert dc_env.host_problem({"PIP_CHAT_HOST": "mac.example.net"}, host=host) is None


def test_an_empty_dc_host_counts_as_unset():
    assert "DC_HOST" in dc_env.host_problem({"DC_HOST": ""}, host="")


def test_host_is_moot_when_dc_addr_is_named():
    """ADDR is the only address built on HOST: MAILTO's default is bare (PHONE-248).
    With DC_ADDR named, no address is built from the junk host."""
    addr = {"DC_ADDR": "ada-chat@mac.example.net"}
    assert dc_env.host_problem(addr, host="localhost") is None
    assert dc_env.host_problem({**addr, "DC_MAILTO": "ada@mac.example.net"}, host="localhost") is None
    # DC_MAILTO alone: ADDR is still built from the junk host
    assert "DC_HOST" in dc_env.host_problem({"DC_MAILTO": "ada@mac.example.net"}, host="localhost")


def test_the_reason_names_the_junk_address_and_the_fix():
    why = dc_env.host_problem({"DC_AGENT": "ada"}, host="localhost")
    assert "ada-chat@localhost" in why and "ada@localhost" not in why
    assert "DC_HOST=" in why and "sessionphone bot install" in why


# --- the default host is sessionmail's, never a DNS answer (no DC_HOST) ------

ARPA = "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa"


def test_the_default_is_sessionmail_s_short_host_not_getfqdn(monkeypatch):
    """A reverse-DNS FQDN no longer reaches the default To: the mail host does."""
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: ARPA)
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger.local")
    for k in dc_env.MAIL_HOST_VARS:
        monkeypatch.delenv(k, raising=False)
    assert dc_env.mail_host() == "stranger"
    assert dc_env.mailto({}) == "claude"                 # the To is bare (PHONE-248): no host at all
    assert dc_env.mailto({"DC_AGENT": "Ada"}) == "ada"
    assert dc_env.host_problem({}) is None


def test_sessionmail_host_is_honoured_both_spellings(monkeypatch):
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger.local")
    assert dc_env.mail_host({"SESSIONMAIL_HOST": "mac"}) == "mac"
    assert dc_env.mail_host({"SESSION_MAIL_HOST": "mac2"}) == "mac2"
    assert dc_env.mail_host({"SESSIONMAIL_HOST": "", "SESSION_MAIL_HOST": "mac2"}) == "mac2"
    assert dc_env.mail_host({"SESSIONMAIL_HOST": ""}) == "stranger"     # empty is unset
    assert dc_env.mailto({"SESSIONMAIL_HOST": "mac"}) == "claude"     # the host never reaches the To


def test_dc_host_does_not_reach_the_default_to_and_dc_mailto_wins(monkeypatch):
    """PHONE-248: a DC_HOST builds ADDR and the web Reply-To, never MAILTO's default,
    which stays bare; a DC_MAILTO is kept as given."""
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger")
    assert dc_env.mailto({"DC_HOST": "mac.example.net"}) == "claude"
    assert dc_env.mailto({"DC_MAILTO": "box@elsewhere.example", "DC_HOST": "x"}) == "box@elsewhere.example"


def test_id_host_and_mail_host_are_one_answer(monkeypatch):
    from sessionphone.bridge import seatmail
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger.local")
    for e in ({}, {"SESSIONMAIL_HOST": "mac"}, {"SESSION_MAIL_HOST": "m2"}, {"SESSIONMAIL_HOST": ""}):
        assert seatmail.id_host(e) == dc_env.mail_host(e)


@pytest.mark.parametrize("fqdn", JUNK_FQDN)
def test_a_dns_name_is_still_judged_where_it_is_used(fqdn):
    """fqdn_problem, the judge for a host built from getfqdn() itself, still refuses these."""
    assert "DC_HOST" in dc_env.fqdn_problem({}, fqdn)
    assert dc_env.fqdn_problem({"DC_HOST": "mac.example.net"}, fqdn) is None
    assert dc_env.fqdn_problem({}, "mac.example.net") is None


BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"


def test_bin_has_no_host_rule_of_its_own_so_every_default_host_is_dc_env_s():
    """AGENTS.md: bin/dc-env mirrors dc_env.py, change both or neither. PHONE-217 changed
    dc_env's host rule (host_part) and no file in bin/: none derives a host. The one
    DC_HOST in bin/ pins the channels pre-check (which uses no host), never the job."""
    rule = re.compile(r"getfqdn|gethostname|\bhostname\b|uname|SESSIONMAIL_HOST|SESSION_MAIL_HOST|\.arpa",
                      re.IGNORECASE)
    files = [f for f in sorted(BIN.iterdir()) if f.is_file()]
    assert any(f.name == "dc-env" for f in files)
    for f in files:
        text = f.read_text(errors="replace")
        assert not rule.findall(text), f"bin/{f.name} derives a host of its own"
        hosts = [line for line in text.splitlines() if "DC_HOST" in line and not line.lstrip().startswith("#")]
        if f.name == "dc-env":
            assert len(hosts) == 1 and 'DC_HOST="${DC_HOST:-localhost}"' in hosts[0] and "precheck" in text
        else:
            assert hosts == [], f"bin/{f.name} sets DC_HOST"


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
    monkeypatch.setattr(dc_env, "HOST_PROBLEM", dc_env.host_problem({}, host="localhost"))
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
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
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
    monkeypatch.setenv("SESSIONMAIL_HOST", "localhost")     # carried: the job's mail host
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2
    assert "DC_HOST" in r.output
    assert rig == []                                 # no bootout, no bootstrap
    assert not (tmp_path / "job.plist").exists()


def test_install_passes_on_a_reverse_dns_fqdn_with_no_dc_host(rig, monkeypatch, tmp_path, given_seat):
    """A fresh Mac whose DNS name is a reverse-DNS name installs: the default To is
    the bare agent (PHONE-248), whose box the box step then proves."""
    given_seat("ada-chat")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: ARPA)
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger.local")
    monkeypatch.delenv("SESSIONMAIL_HOST", raising=False)
    monkeypatch.delenv("SESSION_MAIL_HOST", raising=False)
    boxes = []
    service.install("ada", seated=lambda made, box: boxes.append(box))
    assert boxes[0].mailto == "ada" and boxes[0].path
    assert ("bootstrap",) in [c[:1] for c in rig]


def test_install_judges_the_host_the_job_will_carry(rig, monkeypatch, tmp_path, given_seat):
    given_seat("ada-chat")
    monkeypatch.setenv("SESSIONMAIL_HOST", "localhost")
    monkeypatch.setenv("DC_HOST", "mac.example.net")  # carried into the plist, so it is good
    service.install("ada")
    j = plistlib.loads((tmp_path / "job.plist").read_bytes())
    assert j["EnvironmentVariables"]["DC_HOST"] == "mac.example.net"
    assert ("bootstrap",) in [c[:1] for c in rig]

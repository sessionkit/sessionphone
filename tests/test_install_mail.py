"""PHONE-179: the bot's launchd job carries sessionmail's mail-route settings.

job() carried PATH, SESSIONPHONE_ROOT, DC_AGENT, PYTHONUNBUFFERED and DC_* only,
so `SESSIONMAIL_SENDMAIL=drop sessionphone bot install` gave a bot whose
`message send` took /usr/sbin/sendmail: on a Mac with no postfix the call spoke
and every turn was lost. The design call is CARRY, never detect: the carrier is
sessionmail's to choose, and the environment is its seam. So job() carries the
named mail-route allowlist (service.MAIL_KEYS) when set and not empty, never the
installing seat's own SESSIONMAIL_* (by ruling: an allowlist, because a
denylist fails open); `bot install` says what it carried and which carrier the
bot will use, and the bot's start line names that carrier.

Fakes only: FakeLaunchd (conftest), a plist in tmp_path, no real launchd job.
"""
import os
import plistlib

import pytest
from click.testing import CliRunner

from sessionphone import service
from sessionphone.bridge import dc_env, seatmail
from sessionphone.cli import main as cli

OWNER = "bea@phone.example.org"
BASE = {"PATH", "SESSIONPHONE_ROOT", "DC_AGENT", "PYTHONUNBUFFERED"}


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """No SESSIONMAIL_* from the shell running the tests; a scratch root."""
    for k in [k for k in os.environ if k.startswith("SESSIONMAIL_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(service, "ears_src", lambda: None)
    return monkeypatch


def _env():
    return service.job("dave", python="/p")["EnvironmentVariables"]


# ---- job(): what the launchd job carries -------------------------------------

def test_sessionmail_sendmail_drop_is_carried_in(clean):
    clean.setenv("SESSIONMAIL_SENDMAIL", "drop")
    assert _env()["SESSIONMAIL_SENDMAIL"] == "drop"


def test_unset_sessionmail_sendmail_is_absent(clean):
    assert "SESSIONMAIL_SENDMAIL" not in _env()


def test_empty_sessionmail_sendmail_is_absent(clean):
    """Empty counts as unset, as it does for DC_* and in sessionmail."""
    clean.setenv("SESSIONMAIL_SENDMAIL", "")
    assert "SESSIONMAIL_SENDMAIL" not in _env()


MAIL_ROUTE = {"SESSIONMAIL_SENDMAIL": "/opt/mta/sendmail", "SESSIONMAIL_ORIGIN": "example.net",
              "SESSIONMAIL_RELAY_DOMAIN": "relay.example", "SESSIONMAIL_HOME": "/h/.sessionmail",
              "SESSIONMAIL_ROOT": "/h/.mail/agents", "SESSIONMAIL_HOST": "mac"}
# the installing seat's own settings, read by sessionmail but never the bot's
SEAT_KEYS = {"SESSIONMAIL_SUBSCRIBE": "proj+task.foreman", "SESSIONMAIL_FROM_NAME": "Some Seat",
             "SESSIONMAIL_PAGE_RULES": "to:me page", "SESSIONMAIL_PAGER_REQUIRED": "1",
             "SESSIONMAIL_PAGER": "on", "SESSIONMAIL_MONITOR": "1",
             "SESSIONMAIL_IMAP_PASSWORD_FILE": "/k", "SESSIONMAIL_ANYTHING_NEW": "x"}


def test_the_allowlist_is_the_ruled_six():
    assert service.MAIL_KEYS == tuple(MAIL_ROUTE)


def test_every_mail_route_key_is_carried(clean):
    for k, v in MAIL_ROUTE.items():
        clean.setenv(k, v)
    env = _env()
    assert {k: env[k] for k in env if k.startswith("SESSIONMAIL_")} == MAIL_ROUTE


def test_no_seat_key_is_carried(clean):
    """SUBSCRIBE would name the installer's address, FROM_NAME make the bot's
    mail claim its name: the allowlist carries SENDMAIL and nothing of theirs,
    nor a SESSIONMAIL_* nobody has named yet."""
    for k, v in {**SEAT_KEYS, "SESSIONMAIL_SENDMAIL": "drop"}.items():
        clean.setenv(k, v)
    env = _env()
    assert {k: env[k] for k in env if k.startswith("SESSIONMAIL_")} == {"SESSIONMAIL_SENDMAIL": "drop"}


def test_legacy_spelling_is_not_carried(clean):
    for k in ("SESSION_MAIL_SENDMAIL", "SESSION_MAIL_HOST", "SESSION_MAIL_ROOT"):
        clean.setenv(k, "x")
    assert not any(k.startswith(("SESSION_MAIL_", "SESSIONMAIL_")) for k in _env())


def test_nothing_but_dc_and_sessionmail_is_newly_carried(clean):
    """No secret, no seat identity, no other spelling rides in on this rule:
    the job's keys are the base four, DC_* and SESSIONMAIL_*, nothing else."""
    for k, v in {"SECRET_TOKEN": "s3cret", "EMAIL": "someone@example.org",
                 "TM_AGENT_ADDRESS": "seat", "CLAUDE_SESSION_PUB": "/k.pub",
                 "SESSION_MAIL_SENDMAIL": "drop", "XSESSIONMAIL_SENDMAIL": "drop",
                 "sessionmail_sendmail": "drop", "SESSIONMAIL_SENDMAIL": "drop"}.items():
        clean.setenv(k, v)
    env = _env()
    stray = {k for k in env if k not in BASE and not k.startswith(("DC_", "SESSIONMAIL_"))}
    assert stray == set()
    assert "SESSION_MAIL_SENDMAIL" not in env and env["SESSIONMAIL_SENDMAIL"] == "drop"


def test_carried_mail_reads_the_mapping_it_is_given():
    assert service.carried_mail({"SESSIONMAIL_SENDMAIL": "drop", "SESSIONMAIL_HOST": "",
                                 "SESSIONMAIL_FROM_NAME": "Seat", "DC_Y": "1",
                                 "OTHER": "2"}) == {"SESSIONMAIL_SENDMAIL": "drop"}


def test_the_rendered_plist_shows_the_key(clean):
    """Effect, not attempt: the plist launchd reads carries it."""
    clean.setenv("SESSIONMAIL_SENDMAIL", "drop")
    back = plistlib.loads(plistlib.dumps(service.job("dave", python="/p")))
    assert back["EnvironmentVariables"]["SESSIONMAIL_SENDMAIL"] == "drop"


def test_install_help_names_every_carried_key():
    """`bot install --help` says which SESSIONMAIL_* ride, by their names, so a
    key added to MAIL_KEYS without the help saying so fails here."""
    out = " ".join(CliRunner().invoke(cli, ["bot", "install", "--help"]).output.split())
    assert "MAIL_KEYS" not in out
    for k in service.MAIL_KEYS:
        assert k.removeprefix("SESSIONMAIL") in out, k


# ---- seatmail.carrier(): sessionmail's rule, named ---------------------------

@pytest.mark.parametrize("env, said", [
    ({"SESSIONMAIL_SENDMAIL": "drop"}, "drop (SESSIONMAIL_SENDMAIL=drop, no MTA)"),
    ({"SESSIONMAIL_SENDMAIL": " drop\n"}, "drop (SESSIONMAIL_SENDMAIL=drop, no MTA)"),  # sm strips
    ({"SESSIONMAIL_SENDMAIL": "/opt/mta/sendmail"}, "MTA /opt/mta/sendmail (SESSIONMAIL_SENDMAIL)"),
    ({"SESSIONMAIL_SENDMAIL": ""}, "MTA /usr/sbin/sendmail (SESSIONMAIL_SENDMAIL unset)"),
    ({}, "MTA /usr/sbin/sendmail (SESSIONMAIL_SENDMAIL unset)"),
    ({"SESSION_MAIL_SENDMAIL": "drop"}, "MTA /usr/sbin/sendmail (SESSIONMAIL_SENDMAIL unset)"),
], ids=["drop", "drop-spaced", "path", "empty", "unset", "other-spelling"])
def test_carrier_names_what_sessionmail_will_use(env, said):
    assert seatmail.carrier(env) == said


# ---- service.mail_line(): what `bot install` says ----------------------------

def test_mail_line_names_drop_with_its_value():
    line = service.mail_line({"SESSIONMAIL_SENDMAIL": "drop", "SESSIONMAIL_HOST": "mac", "DC_X": "1"})
    assert line == ("carried SESSIONMAIL_HOST, SESSIONMAIL_SENDMAIL=drop; the bot's mail goes by "
                    "drop (SESSIONMAIL_SENDMAIL=drop, no MTA)")


def test_mail_line_names_other_settings_without_their_values():
    line = service.mail_line({"SESSIONMAIL_HOST": "secret-ish-value"})
    assert "carried SESSIONMAIL_HOST;" in line and "secret-ish-value" not in line
    assert "MTA /usr/sbin/sendmail" in line and "export SESSIONMAIL_SENDMAIL=drop" in line


def test_mail_line_says_plainly_when_none_was_carried():
    line = service.mail_line({"PATH": "/bin", "DC_AGENT": "x"})
    assert line.startswith("carried no SESSIONMAIL_* setting; the bot's mail goes by MTA /usr/sbin/sendmail")
    assert "export SESSIONMAIL_SENDMAIL=drop, then run `sessionphone bot install` again" in line


# ---- `sessionphone bot install`: the mail: line, judged on the plist ---------

@pytest.fixture
def rig(clean, tmp_path, launchd):
    """test_hearing's rig: FakeLaunchd, a scratch HOME and root, an owner."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        clean.delenv(k)
    (tmp_path / "home").mkdir()
    clean.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "root").mkdir(mode=0o700)
    clean.setattr(service.paths, "ensure_private", lambda *a: None)
    clean.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    clean.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    clean.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    return tmp_path / "job.plist"


def _install():
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 0, r.output
    return r.output


def _mail(output):
    return [ln for ln in output.splitlines() if ln.startswith("mail:")]


def test_install_says_it_carried_drop(rig, clean):
    clean.setenv("SESSIONMAIL_SENDMAIL", "drop")
    out = _install()
    assert _mail(out) == [("mail:      carried SESSIONMAIL_SENDMAIL=drop; the bot's mail goes by "
                           "drop (SESSIONMAIL_SENDMAIL=drop, no MTA)")]
    assert plistlib.loads(rig.read_bytes())["EnvironmentVariables"]["SESSIONMAIL_SENDMAIL"] == "drop"
    assert out.index("log:") < out.index("mail:") < out.index("hearing:")


def test_install_says_plainly_when_none_was_carried(rig):
    [line] = _mail(_install())
    assert line.startswith("mail:      carried no SESSIONMAIL_* setting; the bot's mail goes by "
                           "MTA /usr/sbin/sendmail (SESSIONMAIL_SENDMAIL unset)")
    assert "export SESSIONMAIL_SENDMAIL=drop" in line


def test_install_judges_the_plist_not_the_shell(rig, clean, monkeypatch):
    """The line reads the job back: a plist that lost the key says so, whatever the shell has."""
    clean.setenv("SESSIONMAIL_SENDMAIL", "drop")
    real = service.install

    def install(**kw):
        p = real(**kw)
        j = plistlib.loads(p.read_bytes())
        del j["EnvironmentVariables"]["SESSIONMAIL_SENDMAIL"]
        p.write_bytes(plistlib.dumps(j))
        return p
    monkeypatch.setattr(service, "install", install)
    [line] = _mail(_install())
    assert "carried no SESSIONMAIL_* setting" in line


def test_install_with_an_unreadable_plist_says_not_confirmed(rig, monkeypatch):
    real = service.install

    def install(**kw):
        p = real(**kw)
        p.write_bytes(b"not a plist")
        return p
    monkeypatch.setattr(service, "install", install)
    [line] = _mail(_install())
    assert line.startswith("mail:      not confirmed. The job's environment could not be read back")

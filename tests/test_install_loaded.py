"""PHONE-98 gap (b): `bot install` must leave the job LOADED, or say it did not.

install() booted out the old job and bootstrapped the new one at once. But
launchd's bootout is asynchronous: it returns before the old job is gone, and
a bootstrap in that window fails "5: Input/output error" or races. The
operator was told "installed" while the job was unloaded (measured live on a
user's Mac during a cutover, Sun 2030-06-09). Now install waits (bounded) for
the old job to go, retries a bootstrap that fails with error 5, and then
checks with `launchctl print` that the job is loaded. Not loaded: install
raises service.NotLoaded and `sessionphone bot install` exits non-zero
saying so. launchd here is tests/conftest.py's FakeLaunchd, never the real one.
"""
import os

import pytest
from click.testing import CliRunner

from sessionphone import service
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    for k in [k for k in os.environ if k.startswith("DC_")]:
        monkeypatch.delenv(k)
    given_seat("ada-chat")     # install's signer, given: no seat is made
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    callers = tmp_path / "callers.txt"   # someone may ring (gap 6); DC_CALLERS grants nothing
    callers.write_text("owner@phone.example.org\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    chans = tmp_path / "channels.txt"   # somewhere the bot may send (PHONE-102 rule 6)
    chans.write_text("owner  owner@phone.example.org\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    return launchd


def test_install_waits_out_the_bootout_before_it_bootstraps(rig, clock):
    rig.linger = 3                      # the old job answers print 3 more times
    service.install("ada")
    first_bootstrap = rig.verbs().index("bootstrap")
    # the poll that saw the old job gone came right before the bootstrap
    assert rig.verbs()[first_bootstrap - 1] == "print"
    assert rig.verbs().count("bootstrap") == 1     # it waited, so it never hit error 5
    assert rig.loaded
    assert clock.slept                              # it waited by sleeping, not spinning


def test_install_retries_a_bootstrap_that_fails_with_error_5(rig, clock):
    rig.bootstrap_rc = [5]              # the window outlasted print's answer
    service.install("ada")
    assert rig.verbs().count("bootstrap") == 2
    assert rig.loaded
    assert clock.slept


def test_install_verifies_the_job_is_loaded(rig):
    service.install("ada")
    assert rig.verbs()[-1] == "print"               # the last word is launchd's
    assert rig.calls[-1][1] == f"{service.domain()}/{service.label('ada')}"


def test_a_job_that_did_not_load_is_a_loud_failure(rig):
    rig.never_loads = True              # bootstrap says 0, nothing is loaded
    with pytest.raises(service.NotLoaded) as e:
        service.install("ada")
    msg = str(e.value)
    assert service.label("ada") in msg and "NOT loaded" in msg


def test_error_5_that_never_clears_is_a_loud_failure_and_bounded(rig, clock):
    rig.linger = 10 ** 9                # the old job never goes away
    with pytest.raises(service.NotLoaded) as e:
        service.install("ada")
    assert "5: Input/output error" in str(e.value)  # launchctl's own reason
    assert sum(clock.slept) < 60                    # bounded, not a hang
    assert rig.verbs().count("bootstrap") == service.BOOTSTRAP_TRIES


def test_a_fresh_install_with_no_old_job_does_not_wait(rig, clock):
    rig.loaded = False
    service.install("ada")
    assert rig.verbs().count("bootstrap") == 1
    assert rig.loaded
    assert sum(clock.slept) == 0


def test_bot_install_says_not_loaded_and_exits_non_zero(rig, given_seat):
    given_seat("claude-chat")     # this test installs as claude
    rig.never_loads = True
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code != 0
    assert "NOT loaded" in r.output
    assert "installed:" not in r.output             # never "installed" with an unloaded job


def test_bot_install_reports_installed_only_when_loaded(rig, given_seat):
    given_seat("claude-chat")     # this test installs as claude
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert "installed:" in r.output and "pid 4242" in r.output

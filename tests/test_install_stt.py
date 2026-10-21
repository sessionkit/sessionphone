"""PHONE-151: `bot install` refuses a DC_STT_URL it can name no model for.

The bot refuses to START on such a URL (bot.check_stt -> stt.check,
PHONE-110): every voice note would fail. `bot install` used to write and load
that job anyway, so launchd restarted a bot that refused every 30 s. Now
install judges the URL the job will carry with the same stt.check, before it
writes anything, and exits 2 as it does for a junk mail host (NoMailHost).
Nothing here touches launchd or the real root: _launchctl is faked.
"""
import os
import plistlib

import pytest
from click.testing import CliRunner

from sessionphone import service
from sessionphone.bridge import stt
from sessionphone.cli import main as cli

UNKNOWN = "http://127.0.0.1:9/v1/audio/transcriptions"        # no tag, no port guess
UNKNOWN_TAG = "acme+http://127.0.0.1:9/v1/audio/transcriptions"  # a kind with no default
KNOWN = "http://127.0.0.1:2022/inference"                       # untagged, whisper by port
OWNER = "owner@phone.example.org"


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    given_seat("ada-chat")     # install's signer, given: no seat is made
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("DC_HOST", "mac.example.net")    # a good host: only STT is judged
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    # neither file exists: an install with --owner would write both
    monkeypatch.setenv("DC_CALLERS_FILE", str(tmp_path / "callers.txt"))
    monkeypatch.setenv("DC_CHANNELS_FILE", str(tmp_path / "channels.txt"))
    return launchd.calls        # conftest's FakeLaunchd: every launchctl call, none real


def _nothing_written(rig, tmp_path):
    assert rig == []                                    # no bootout, no bootstrap
    assert not (tmp_path / "job.plist").exists()
    assert not (tmp_path / "job.plist.part").exists()
    assert not (tmp_path / "callers.txt").exists()      # add_owner never ran
    assert not (tmp_path / "channels.txt").exists()     # no channels seed


@pytest.mark.parametrize("url", [UNKNOWN, UNKNOWN_TAG])
def test_bot_install_refuses_a_stt_url_with_no_model_and_writes_nothing(rig, monkeypatch,
                                                                       tmp_path, url):
    monkeypatch.setenv("DC_STT_URL", url)
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 2, r.output
    assert "DC_STT_URL" in r.output and "DC_STT_MODEL" in r.output
    assert "installed:" not in r.output
    _nothing_written(rig, tmp_path)


def test_the_refusal_is_the_bots_own_start_check(rig, monkeypatch):
    """Reused, not copied: install raises exactly what stt.check says, the
    rule bot.check_stt runs at start, so the two cannot drift apart."""
    monkeypatch.setenv("DC_STT_URL", UNKNOWN)
    with pytest.raises(service.NoSTTModel) as e:
        service.install("ada", owner=OWNER)
    assert str(e.value) == stt.check(UNKNOWN, None)

    seen = []

    def check(url, model=None):
        seen.append((url, model))
        return "refused by stt.check"
    monkeypatch.setattr(stt, "check", check)
    monkeypatch.setenv("DC_STT_MODEL", "some-model")
    with pytest.raises(service.NoSTTModel, match="refused by stt.check"):
        service.install("ada")
    assert seen == [(UNKNOWN, "some-model")]            # the job's env, both settings


def test_install_judges_the_stt_url_the_job_will_carry(rig, monkeypatch, tmp_path, given_seat):
    """With DC_STT_MODEL set, the same URL installs: the model is carried."""
    given_seat("claude-chat")     # this test installs as claude
    monkeypatch.setenv("DC_STT_URL", UNKNOWN)
    monkeypatch.setenv("DC_STT_MODEL", "my-model")
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 0, r.output
    assert "installed:" in r.output
    env = plistlib.loads((tmp_path / "job.plist").read_bytes())["EnvironmentVariables"]
    assert env["DC_STT_URL"] == UNKNOWN and env["DC_STT_MODEL"] == "my-model"
    assert ("bootstrap",) in [c[:1] for c in rig]


@pytest.mark.parametrize("url", [KNOWN, "mlx-audio+http://127.0.0.1:9/v1/audio/transcriptions",
                                 None])
def test_install_takes_a_url_whose_kind_names_a_model(rig, monkeypatch, tmp_path, url):
    """A known kind (tagged, port-guessed, or the bridge's own default when
    DC_STT_URL is unset) needs no DC_STT_MODEL."""
    if url:
        monkeypatch.setenv("DC_STT_URL", url)
    service.install("ada", owner=OWNER)
    assert (tmp_path / "job.plist").exists()
    assert ("bootstrap",) in [c[:1] for c in rig]

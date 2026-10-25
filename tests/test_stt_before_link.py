"""Speech-to-text is checked BEFORE the phone is linked, and by `bot status` (PHONE-251).

On a fresh Mac `hearing: NO` showed only at `bot install`, the last step, and
the speech install (minutes, gigabytes) came after the link. A bot whose STT
died after install was never reported: `bot status` asked nothing. Now:

- `setup` probes the STT the bot will use (DC_STT_URL in its shell, else the
  default) after its invite and box lines, says it was checked before the
  link, and names the install step: the kit's `ears services install --only
  speech` under Session Kit, VoiceMode's standalone. Its exit codes do not
  change, and its "check again" line is setup itself.
- `bot status` gives ONE `hearing:` line, judged on the job's plist, with a
  short cap. NO names the URL that did not answer and points at `bot
  install`. The exit code stays the job's.

Fakes only: FakeCurl for the STT, the `rig` fixture's FakeLaunchd, a faked
account and box. No real account, relay, launchd, mail or speech server.
"""
import os
import plistlib
import subprocess

import pytest
from click.testing import CliRunner
from test_hearing import (  # noqa: F401 - rig and kit_path are fixtures
    DEFAULT,
    OWN,
    OWN_BARE,
    UNDER_KIT,
    FakeCurl,
    _flat,
    _install,
    kit_path,
    rig,
)

from sessionphone import account, hearing, seat, service
from sessionphone.cli import main as cli

DEAD = FakeCurl(rc=7, stderr="curl: (7) Failed to connect to 127.0.0.1 port 2022")
DEAD_URL = "whisper+http://127.0.0.1:2022/v1/audio/transcriptions"
DEAD_BARE = "http://127.0.0.1:2022/v1/audio/transcriptions"


# ---- setup: the probe, before the link --------------------------------------

class _Box:
    path = None
    name = "x"

    def line(self):
        return "x (made)"


@pytest.fixture
def setup_rig(monkeypatch, tmp_path):
    """`sessionphone setup` with the account and the box faked. The shell holds
    no DC_* and no PIP_CHAT_*; a test sets the ones it means. -> the FakeCurl
    holder: set_curl(curl) makes it hearing.RUN, recording what was said first."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setattr(account, "setup", lambda name, status, relay: {
        "root": str(tmp_path), "addr": "bot@relay.example", "displayname": name,
        "created": True, "invite_file": str(tmp_path / "invite.txt"),
        "invite": "https://i.delta.chat/#x"})
    monkeypatch.setattr(seat, "pin_message", lambda *a, **k: None)
    monkeypatch.setattr(seat, "ensure_box", lambda *a, **k: _Box())
    monkeypatch.setattr(hearing, "under_kit", lambda shell: False)
    monkeypatch.setattr(hearing, "weights_state", lambda *a, **k: None)

    def set_curl(curl):
        monkeypatch.setattr(hearing, "RUN", curl)
        return curl
    return set_curl


def _setup(*args):
    return CliRunner().invoke(cli, ["setup", "--name", "x", *args])


def _block(output):
    """The `probing` line and the `hearing:` block that ends setup's output."""
    lines = output.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("hearing:"))
    return lines[i - 1], lines[i:]


def test_setup_probes_the_default_before_the_link_and_names_voicemode(setup_rig):
    """The repro's second case, inverted: setup asks the STT, says so first,
    and on NO names the standalone install step. Exit 0, as before."""
    curl = setup_rig(FakeCurl(rc=7))
    r = _setup()
    assert r.exit_code == 0, r.output
    assert len(curl.calls) == 1
    probing, block = _block(r.output)
    assert probing == f"probing {DEFAULT} (read-only)"
    assert r.output.index("INVITE:") < r.output.index("box:") < r.output.index("probing")
    assert r.output.splitlines()[-len(block):] == block            # the hearing block ends it
    text = _flat(block)
    assert block[0].startswith("hearing:   NO. Checked before the phone is linked.")
    assert DEFAULT in text and "the default" in text and "curl rc=7" in text
    assert any(ln.strip() == hearing.OFFERED_INSTALL for ln in block)
    assert any(ln.strip() == "sessionphone setup --name x" for ln in block)   # check again
    assert hearing.KIT_INSTALL not in text


def test_setup_judges_the_env_bot_install_will_carry(setup_rig, monkeypatch):
    """DC_STT_URL from this shell (what `bot install` bakes into the job) and the
    job's PATH, never this shell's PATH: the curl that answers is the job's."""
    monkeypatch.setenv("DC_STT_URL", DEAD_URL)
    monkeypatch.setenv("PATH", "/nonexistent/shell-bin:" + os.environ["PATH"])
    curl = setup_rig(FakeCurl(rc=7))
    r = _setup()
    assert r.exit_code == 0, r.output
    call = curl.calls[0]
    assert DEAD_BARE in call["argv"]
    assert call["kw"]["env"]["PATH"] == service.BASE_PATH
    assert call["kw"]["timeout"] == hearing.RUN_TIMEOUT_S             # setup waits as install does
    text = _flat(_block(r.output)[1])
    assert DEAD_BARE in text and "the DC_STT_URL you set" in text
    assert "voicemode" not in text.lower()                          # a URL of the user's own


def test_setup_under_the_kit_names_ears_services_install_only_speech(setup_rig, monkeypatch, kit_path):  # noqa: F811
    """Kit ruling: speech is installed before the link, by
    `ears services install --only speech`. Never VoiceMode's, and never the
    two seat forms install gives after the link."""
    monkeypatch.setattr(hearing, "under_kit", UNDER_KIT)
    monkeypatch.setenv("PATH", f"{kit_path}:{os.environ['PATH']}")
    setup_rig(FakeCurl(rc=7))
    r = _setup()
    assert r.exit_code == 0, r.output
    _, block = _block(r.output)
    text = _flat(block)
    assert block[0].startswith("hearing:   NO. Checked before the phone is linked.")
    assert "This is Session Kit" in text and "before the phone is linked" in text
    assert "see the kit's install steps" in text
    cmds = [ln.strip() for ln in block if ln.startswith(" " * (len(hearing.HEAD) + 2))]
    assert hearing.KIT_INSTALL_SPEECH in cmds and hearing.KIT_STATUS in cmds
    assert "sessionphone setup --name x" in cmds
    assert hearing.KIT_INSTALL_PHONE not in cmds and hearing.KIT_INSTALL_AGENT not in cmds
    assert "voicemode service install" not in r.output


def test_setup_says_yes_on_one_line(setup_rig):
    setup_rig(FakeCurl(body='{"text": ""}'))
    r = _setup()
    assert r.exit_code == 0, r.output
    _, block = _block(r.output)
    assert len(block) == 1, block
    assert block[0].startswith("hearing:   yes, checked before the phone is linked: ")
    assert DEFAULT in block[0] and block[0].endswith("so the bot will hear the caller.")


def test_setup_not_confirmed_points_at_setup_again(setup_rig):
    """A server still loading its model: check again with setup, not `bot
    install`, which refuses before a phone is linked."""
    setup_rig(FakeCurl(rc=28))
    r = CliRunner().invoke(cli, ["setup", "--name", "Dr Who"])
    assert r.exit_code == 0, r.output
    text = _flat(_block(r.output)[1])
    assert text.startswith("hearing: not confirmed. Checked before the phone is linked.")
    assert "Run `sessionphone setup --name 'Dr Who'` again in a few minutes." in text
    assert "sessionphone bot install" not in text


def test_setup_names_an_alias_the_job_would_drop(setup_rig, monkeypatch):
    """PIP_CHAT_STT_URL is not carried into the job, so setup probes the default
    and says to rename it, as install does."""
    monkeypatch.setenv("PIP_CHAT_STT_URL", DEAD_URL)
    curl = setup_rig(FakeCurl(rc=7))
    r = _setup()
    assert r.exit_code == 0, r.output
    assert DEFAULT in curl.calls[0]["argv"]
    assert "PIP_CHAT_STT_URL is set in this shell" in _flat(_block(r.output)[1])


def test_a_failed_account_exits_1_and_probes_nothing(setup_rig, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no relay answered")
    monkeypatch.setattr(account, "setup", boom)
    curl = setup_rig(FakeCurl())
    r = _setup()
    assert r.exit_code == 1 and curl.calls == [] and "hearing" not in r.output


def test_a_box_that_cannot_be_made_still_exits_2(setup_rig, monkeypatch):
    def nobox(*a, **k):
        raise seat.NoBox("run `message setup --box x`")
    monkeypatch.setattr(seat, "ensure_box", nobox)
    curl = setup_rig(FakeCurl())
    r = _setup()
    assert r.exit_code == 2 and curl.calls == []


def test_setup_with_a_runner_that_raises_is_no_never_a_traceback(setup_rig):
    setup_rig(FakeCurl(raises=OSError("curl: exec format error")))
    r = _setup()
    assert r.exit_code == 0 and r.exception is None, r.output
    assert "curl could not run" in _flat(_block(r.output)[1])


# ---- bot status: one hearing line --------------------------------------------

def _status_hearing(output):
    found = [ln for ln in output.splitlines() if ln.startswith("hearing:")]
    assert len(found) == 1, output
    return found[0]


def _status(monkeypatch, curl):
    monkeypatch.setattr(hearing, "RUN", curl)
    return CliRunner().invoke(cli, ["bot", "status"])


@pytest.mark.usefixtures("rig")
def test_status_reports_a_dead_stt_in_one_line(monkeypatch):
    """The repro's first case, inverted: status asks the job's STT and says NO
    in one line, naming the URL and `bot install`. Exit code unchanged."""
    monkeypatch.setenv("DC_STT_URL", DEAD_URL)
    r, _ = _install(monkeypatch, DEAD)
    assert r.exit_code == 0, r.output
    curl = FakeCurl(rc=7, stderr="curl: (7) Failed to connect to 127.0.0.1 port 2022")
    monkeypatch.delenv("DC_STT_URL")        # judged on the plist, not this shell
    st = _status(monkeypatch, curl)
    line = _status_hearing(st.output)
    assert line.startswith("hearing:  NO. " + DEAD_BARE + " (DC_STT_URL) did not answer")
    assert "callers will not be heard" in line and "`sessionphone bot install` says how" in line
    assert len(curl.calls) == 1 and DEAD_BARE in curl.calls[0]["argv"]
    running = service.status()["pid"] is not None
    assert st.exit_code == (0 if running else 1), st.output
    lines = st.output.splitlines()
    assert lines.index(line) < next(i for i, ln in enumerate(lines) if ln.startswith("speaking:"))
    assert lines[-1].startswith("last")          # the log's last line stays last


@pytest.mark.usefixtures("rig")
def test_status_heads_align(monkeypatch):
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    st = _status(monkeypatch, FakeCurl())
    line = _status_hearing(st.output)
    speak = next(ln for ln in st.output.splitlines() if ln.startswith("speaking:"))
    assert line[len("hearing:  "):][0] != " " and len(hearing.STATUS_HEAD) == len("speaking: ")
    assert line.index("yes") == speak.index(speak.split()[1])


@pytest.mark.usefixtures("rig")
def test_status_says_yes_in_one_short_line(monkeypatch):
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    st = _status(monkeypatch, FakeCurl())
    line = _status_hearing(st.output)
    assert line.startswith(f"hearing:  yes. {DEFAULT} answered a half-second test clip (HTTP 200")
    assert "probing" not in st.output


@pytest.mark.usefixtures("rig")
def test_status_caps_its_probe(monkeypatch):
    """Status never waits install's 30 s: curl's -m and the run's own cap are short."""
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    curl = FakeCurl()
    _status(monkeypatch, curl)
    argv, kw = curl.calls[0]["argv"], curl.calls[0]["kw"]
    assert argv[argv.index("-m") + 1] == str(hearing.STATUS_MAX_TIME_S)
    assert kw["timeout"] == hearing.STATUS_RUN_TIMEOUT_S
    assert hearing.STATUS_MAX_TIME_S < hearing.STATUS_RUN_TIMEOUT_S < hearing.MAX_TIME_S


@pytest.mark.usefixtures("rig")
@pytest.mark.parametrize("curl", [FakeCurl(rc=28), FakeCurl(raises=subprocess.TimeoutExpired("curl", 10))],
                         ids=["curl-28", "run-timeout"])
def test_a_slow_server_is_not_confirmed_not_no(monkeypatch, curl):
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    line = _status_hearing(_status(monkeypatch, curl).output)
    assert line.startswith(f"hearing:  not confirmed. {DEFAULT} (DC_STT_URL, the default) gave ")
    assert f"no answer within {hearing.STATUS_MAX_TIME_S} s" in line
    assert "`sessionphone bot install` waits longer" in line


@pytest.mark.usefixtures("rig")
def test_a_server_that_answers_but_fails_is_no_with_its_reply(monkeypatch):
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    line = _status_hearing(_status(monkeypatch, FakeCurl(body="boom", status=500)).output)
    assert line.startswith(f"hearing:  NO. {DEFAULT} (DC_STT_URL, the default) answered")
    assert "HTTP 500" in line and "`sessionphone bot install` says how" in line


@pytest.mark.usefixtures("rig")
def test_status_with_no_job_probes_nothing(monkeypatch):
    curl = FakeCurl()
    st = _status(monkeypatch, curl)
    assert "(not installed)" in st.output and "hearing:" not in st.output and curl.calls == []


@pytest.mark.usefixtures("rig")
def test_status_with_an_unreadable_plist_is_not_confirmed(monkeypatch, tmp_path):
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    (tmp_path / "job.plist").write_bytes(b"not a plist")
    curl = FakeCurl()
    st = _status(monkeypatch, curl)
    line = _status_hearing(st.output)
    assert line.startswith("hearing:  not confirmed. The job's environment could not be read back")
    assert curl.calls == []


# ---- status_line: the one line, each verdict ---------------------------------

def _verdict(**kw):
    base = {"answer": "NO", "url": OWN, "bare": OWN_BARE, "user_set": True, "sent": True,
            "came_back": "curl rc=7, connection refused"}
    return hearing.Verdict(**{**base, **kw})


def test_status_line_with_calls_off_speaks_of_voice_notes():
    line = hearing.status_line(_verdict(), {"DC_CALLS": "off"})
    assert "voice notes will not be transcribed" in line and "callers" not in line


def test_status_line_for_a_url_naming_no_model_is_no():
    line = hearing.status_line(_verdict(refused="DC_STT_MODEL is not set", sent=False), {})
    assert line.startswith(f"hearing:  NO. {OWN_BARE} (DC_STT_URL) names no model: DC_STT_MODEL")


def test_status_line_is_never_wrapped():
    for v in (_verdict(), _verdict(answer="yes", came_back="HTTP 200 in 0.1 s"),
              _verdict(answer="not confirmed", sent=False, came_back="the test clip could not be written")):
        assert "\n" not in hearing.status_line(v, {})


def test_the_plist_env_is_what_status_judges(tmp_path):
    """_status_hearing reads the plist's EnvironmentVariables, as _speaking does."""
    from sessionphone import cli as cli_mod
    plist = tmp_path / "p.plist"
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"DC_STT_URL": OWN}}))
    env, exc = cli_mod._job_env(plist)
    assert exc is None and env["DC_STT_URL"] == OWN

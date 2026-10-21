"""The launchd job the bot runs under: what it runs, and what it carries."""
import plistlib

from sessionphone import service


def test_the_job_runs_the_bridge_as_the_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path))
    monkeypatch.delenv("DC_AGENT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_AGENT", raising=False)
    j = service.job(python="/venv/bin/python")
    assert j["Label"] == "dev.sessionkit.phone.bot.claude"
    assert j["ProgramArguments"] == ["/venv/bin/python", "-m", "sessionphone.bridge.bot", "claude"]
    assert j["EnvironmentVariables"]["SESSIONPHONE_ROOT"] == str(tmp_path)
    assert j["EnvironmentVariables"]["DC_AGENT"] == "claude"
    assert "/opt/homebrew/bin" in j["EnvironmentVariables"]["PATH"]
    assert j["StandardOutPath"].startswith(str(tmp_path))


def test_the_seat_names_the_agent(monkeypatch):
    monkeypatch.delenv("DC_AGENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "Carol")
    assert service.agent() == "carol"
    assert service.label() == "dev.sessionkit.phone.bot.carol"


def test_dc_settings_in_the_shell_are_carried_in(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path))
    monkeypatch.setenv("DC_MESSAGE", "/opt/sessionmail/bin/message")
    monkeypatch.setenv("DC_EMPTY", "")
    env = service.job("dave", python="/p")["EnvironmentVariables"]
    assert env["DC_MESSAGE"] == "/opt/sessionmail/bin/message"
    assert "DC_EMPTY" not in env
    assert env["DC_AGENT"] == "dave"


def test_a_clean_exit_is_restarted_and_a_refusal_only_slowly(tmp_path, monkeypatch):
    """PHONE-98 gap 3: KeepAlive {SuccessfulExit: False} left a bot that exited
    0 down for good, silently. KeepAlive is now plain true, so launchd restarts
    ANY exit, clean or not; ThrottleInterval keeps a refusing bot (exit 2: no
    account, unsafe root, junk host) from spinning."""
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path))
    j = service.job(python="/p")
    assert j["KeepAlive"] is True, f"KeepAlive {j['KeepAlive']!r}: a clean exit would stay down"
    assert j["RunAtLoad"] is True
    assert j["ThrottleInterval"] >= 10
    back = plistlib.loads(plistlib.dumps(j))   # it serialises, and launchd reads <true/>
    assert back["KeepAlive"] is True and back["ThrottleInterval"] == j["ThrottleInterval"]


def test_install_runs_the_bot_by_its_project_name(monkeypatch, tmp_path, launchd, given_seat):
    """`bot install` writes the libexec link into the plist, so a reinstall
    never puts bin/python (shown as "Python") back (PHONE-80)."""
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(service.dc_env.socket, "getfqdn", lambda: "mac.example.net")  # not this machine's
    callers = tmp_path / "callers.txt"   # someone may ring (PHONE-98 gap 6); DC_CALLERS grants nothing
    callers.write_text("owner@phone.example.org\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    chans = tmp_path / "channels.txt"   # somewhere the bot may send (PHONE-102 rule 6)
    chans.write_text("owner  owner@phone.example.org\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    given_seat("alice-chat")
    service.install("alice")
    j = plistlib.loads((tmp_path / "job.plist").read_bytes())
    assert j["ProgramArguments"][0] == "/venv/libexec/session-phone"

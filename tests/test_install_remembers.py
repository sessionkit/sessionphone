"""A second `sessionphone bot install` from an empty shell keeps every setting
the first one used, and says where each value came from.

The job's settings resolve in the family's order: this shell, then the phone
tool's host file (~/.sessionkit/phone/settings/phone.json, one map per bot),
then the default. Install writes back what the shell gave once the job is
loaded. sessionmail's mail-route settings are not kept there: they are
sessionmail's, in its own mail.json, and install says for each one the shell
set whether it is kept and how to keep it.

Fakes only: FakeLaunchd (conftest), a plist in tmp_path, the tripwire
`message` as the signer's sessionmail, a scratch HOME.
"""
import json
import os
import pathlib
import plistlib
import stat

import pytest
from click.testing import CliRunner

from sessionphone import seat, service, settings
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

OWNER = "owner@phone.example.org"
DEFAULT_FILE = settings.FILE            # read at import, before conftest moves it
REAL_MAIL_FILE = settings.mail_file

# what a first install is given: the bot's identity, its speech servers and the mail route
FIRST = {
    "DC_AGENT": "ada", "DC_HOST": "mac.example.net",
    "DC_ADDR": "ada-chat@mac.example.net", "DC_MAILTO": "ada@mac.example.net",
    "DC_STT_URL": "http://127.0.0.1:9/v1/audio/transcriptions", "DC_STT_MODEL": "whisper-1",
    "DC_TTS_URL": "http://127.0.0.1:9/v1/audio/speech", "DC_VOICE": "af_sky", "DC_ANSWER": "auto",
    "SESSIONMAIL_SENDMAIL": "drop", "SESSIONMAIL_RELAY_DOMAIN": "relay.example",
    "SESSIONMAIL_HOST": "mac",
}
MAIL = ("SESSIONMAIL_SENDMAIL", "SESSIONMAIL_RELAY_DOMAIN", "SESSIONMAIL_HOST",
        "SESSIONMAIL_HOME", "SESSIONMAIL_ROOT")


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_", "SESSIONMAIL_"))]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("CLAUDE_CODE_AGENT", raising=False)
    pub = given_seat("ada-chat")
    (pub.parent / "agent.sock").touch()     # a saved signer must still be there
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings, "FILE", DEFAULT_FILE)      # under the scratch HOME
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setenv("SESSIONMAIL_HOME", str(home / ".sessionmail"))
    monkeypatch.setenv("SESSIONMAIL_ROOT", str(home / ".mail" / "agents"))
    for k, v in FIRST.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(service, "ears_src", lambda: None)
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    # this machine is mac, so a shell with no SESSIONMAIL_HOST still finds its
    # To (ada@mac.example.net) on this host, whatever host runs the suite
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "mac.local")
    return tmp_path / "job.plist"


def _job_env(plist):
    return plistlib.loads(plist.read_bytes())["EnvironmentVariables"]


def _phone_json() -> pathlib.Path:
    return pathlib.Path(os.environ["HOME"]) / ".sessionkit" / "phone" / "settings" / "phone.json"


def _saved() -> dict:
    return json.loads(_phone_json().read_text())


def _empty_shell(monkeypatch):
    """The second shell: HOME and a PATH with no sessionmail on it, nothing else."""
    for k in [k for k in os.environ
              if k.startswith(("DC_", "PIP_CHAT_", "SESSIONMAIL_")) or k == "SESSIONPHONE_ROOT"]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def _install(*args):
    return CliRunner().invoke(cli, ["bot", "install", *args])


def _row(output: str, key: str) -> str:
    """The settings block's line for KEY."""
    return next(ln for ln in output.splitlines() if ln.startswith(f"  {key} "))


def _without_mail(env: dict) -> dict:
    return {k: v for k, v in env.items() if k not in MAIL}


# ---- the contract -----------------------------------------------------------------

def test_the_first_install_carries_what_it_was_given(rig):
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    env = _job_env(rig)
    assert {k: env.get(k) for k in FIRST} == FIRST
    assert env["SESSIONPHONE_ROOT"] == os.environ["SESSIONPHONE_ROOT"]


def test_a_second_install_from_an_empty_shell_keeps_every_setting(rig, monkeypatch):
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    first = _job_env(rig)
    _empty_shell(monkeypatch)
    r = _install()
    assert r.exit_code == 0, r.output
    # every setting but sessionmail's own, which sessionmail reads from its mail.json
    assert _job_env(rig) == _without_mail(first)
    # and it says where each value came from
    assert "settings:  ~/.sessionkit/phone/settings/phone.json (unchanged), bot ada" in r.output
    assert _row(r.output, "DC_AGENT").endswith("~/.sessionkit/phone/settings/phone.json, the one bot it records")
    for k in ("SESSIONPHONE_ROOT", "DC_HOST", "DC_VOICE", "DC_SIGNER_PUB", "DC_MESSAGE"):
        assert _row(r.output, k).endswith("  ~/.sessionkit/phone/settings/phone.json"), r.output


def test_a_second_install_given_only_name_and_signer_keeps_the_rest(rig, monkeypatch):
    """The default root, and the second shell re-gives the name and the signer:
    the bot's address, host and speech servers are still kept."""
    monkeypatch.delenv("SESSIONPHONE_ROOT")
    (pathlib.Path(os.environ["HOME"]) / ".sessionkit" / "phone" / "state").mkdir(parents=True, mode=0o700)
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    first = _job_env(rig)
    given = {k: os.environ[k] for k in ("DC_AGENT", "DC_MESSAGE", "DC_SIGNER_SOCK", "DC_SIGNER_PUB")}
    _empty_shell(monkeypatch)
    for k, v in given.items():
        monkeypatch.setenv(k, v)
    r = _install()
    assert r.exit_code == 0, r.output
    second = _job_env(rig)
    assert {k: second.get(k) for k in _without_mail(first)} == _without_mail(first)
    assert _row(r.output, "SESSIONPHONE_ROOT").endswith("the default")
    assert _row(r.output, "DC_SIGNER_PUB").endswith("this shell (as saved)")
    assert _row(r.output, "DC_AGENT").endswith("DC_AGENT in this shell")


def test_the_file_lives_in_the_kit_and_holds_only_what_was_given(rig):
    assert DEFAULT_FILE == "~/.sessionkit/phone/settings/phone.json"
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    p = _phone_json()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    saved = _saved()
    assert list(saved) == ["bots"] and list(saved["bots"]) == ["ada"]
    bot = saved["bots"]["ada"]
    assert {k: bot[k] for k in FIRST if k.startswith("DC_") and k != "DC_AGENT"} == {
        k: v for k, v in FIRST.items() if k.startswith("DC_") and k != "DC_AGENT"}
    assert bot["SESSIONPHONE_ROOT"] == os.environ["SESSIONPHONE_ROOT"]
    assert "DC_AGENT" not in bot                       # the map's own key
    assert not [k for k in bot if k.startswith("SESSIONMAIL_")]   # sessionmail's, not ours
    assert "settings:  ~/.sessionkit/phone/settings/phone.json (saved), bot ada" in r.output


# ---- what is never saved ------------------------------------------------------------

def test_a_pinned_message_is_derived_each_run_and_never_saved(rig, monkeypatch):
    exe = os.environ["DC_MESSAGE"]
    monkeypatch.delenv("DC_MESSAGE")
    monkeypatch.setenv("PATH", f"{os.path.dirname(exe)}:/usr/bin:/bin")
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    assert _job_env(rig)["DC_MESSAGE"] == exe
    assert "DC_MESSAGE" not in _saved()["bots"]["ada"]
    assert _row(r.output, "DC_MESSAGE").endswith("pinned from this shell's PATH (not saved)")


def test_mail_route_keys_are_carried_for_the_run_and_say_they_are_not_kept(rig):
    """This sessionmail does not read mail.json (or cannot tell): each key the
    shell set is named as NOT kept, with the one step that keeps it."""
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    env = _job_env(rig)
    assert env["SESSIONMAIL_SENDMAIL"] == "drop"       # carried this run, as before
    row = _row(r.output, "SESSIONMAIL_SENDMAIL")
    assert "NOT kept: this sessionmail reads it from the environment only" in row
    assert 'upgrade sessionmail, then write "sendmail": "drop" in ~/.sessionkit/mail/settings/mail.json' in row
    assert 'write "postfix.relay_domain": "relay.example"' in _row(r.output, "SESSIONMAIL_RELAY_DOMAIN")
    assert 'write "root": "~/.mail"' in _row(r.output, "SESSIONMAIL_ROOT")
    assert 'write "host": "mac"' in _row(r.output, "SESSIONMAIL_HOST")
    assert "NOT kept: sessionmail has no settings key for it" in _row(r.output, "SESSIONMAIL_HOME")


def test_with_a_sessionmail_that_reads_mail_json_it_names_the_missing_key(rig, monkeypatch):
    filed = {"sendmail": ("drop", "host"), "root": (None, "default"),
             "host": ("other", "host"), "postfix.relay_domain": ("relay.home", "default")}
    monkeypatch.setattr(settings, "mail_file", lambda exe: filed)
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    assert "mail.json `sendmail` holds it too" in _row(r.output, "SESSIONMAIL_SENDMAIL")
    host = _row(r.output, "SESSIONMAIL_HOST")
    assert 'NOT kept' in host and 'write "host": "mac"' in host and 'it holds "other"' in host
    relay = _row(r.output, "SESSIONMAIL_RELAY_DOMAIN")
    assert 'write "postfix.relay_domain": "relay.example"' in relay and "it has no such key" in relay


def test_with_mail_json_an_empty_shell_names_the_carrier_sessionmail_reads(rig, monkeypatch):
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    _empty_shell(monkeypatch)
    filed = {"sendmail": ("drop", "host"), "root": (None, "default"),
             "host": (None, "default"), "postfix.relay_domain": ("relay.home", "default")}
    monkeypatch.setattr(settings, "mail_file", lambda exe: filed)
    r = _install()
    assert r.exit_code == 0, r.output
    assert "SESSIONMAIL_SENDMAIL" not in _job_env(rig)
    assert "mail.json `sendmail` (not carried: the bot's `message` reads it)" in _row(r.output, "SESSIONMAIL_SENDMAIL")
    mail = next(ln for ln in r.output.splitlines() if ln.startswith("mail:"))
    assert "goes by drop (sessionmail's ~/.sessionkit/mail/settings/mail.json `sendmail`)" in mail
    assert "reaches no one" not in mail


# ---- precedence, names, forgetting -------------------------------------------------

def test_a_value_in_this_shell_wins_and_replaces_the_saved_one(rig, monkeypatch):
    assert _install("--owner", OWNER).exit_code == 0
    _empty_shell(monkeypatch)
    monkeypatch.setenv("DC_VOICE", "af_bella")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(rig)["DC_VOICE"] == "af_bella"
    assert _saved()["bots"]["ada"]["DC_VOICE"] == "af_bella"
    assert _row(r.output, "DC_VOICE").endswith("this shell (saved; was af_sky)")


def test_an_empty_value_in_this_shell_is_no_value(rig, monkeypatch):
    assert _install("--owner", OWNER).exit_code == 0
    _empty_shell(monkeypatch)
    monkeypatch.setenv("DC_VOICE", "")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(rig)["DC_VOICE"] == "af_sky"


def test_forget_drops_a_saved_value_for_this_run_and_later(rig, monkeypatch):
    assert _install("--owner", OWNER).exit_code == 0
    _empty_shell(monkeypatch)
    r = _install("--forget", "DC_VOICE", "--forget", "DC_ANSWER")
    assert r.exit_code == 0, r.output
    env = _job_env(rig)
    assert "DC_VOICE" not in env and "DC_ANSWER" not in env
    bot = _saved()["bots"]["ada"]
    assert "DC_VOICE" not in bot and "DC_ANSWER" not in bot and bot["DC_HOST"] == "mac.example.net"
    assert "forgot DC_VOICE (was af_sky): removed from ~/.sessionkit/phone/settings/phone.json" in r.output
    r = _install()
    assert r.exit_code == 0 and "DC_VOICE" not in _job_env(rig)


@pytest.mark.parametrize("args, said", [
    (("--forget", "DC_AGENT"), "--forget DC_AGENT: install keeps no such setting"),
    (("--forget", "SESSIONMAIL_SENDMAIL"), "--forget SESSIONMAIL_SENDMAIL: install keeps no such setting"),
])
def test_forget_of_a_key_install_does_not_keep_is_refused(rig, args, said):
    r = _install("--owner", OWNER, *args)
    assert r.exit_code == 2
    assert said in r.output
    assert not _phone_json().exists() and not rig.exists()


def test_forget_while_this_shell_sets_it_is_refused(rig):
    r = _install("--owner", OWNER, "--forget", "DC_VOICE")
    assert r.exit_code == 2
    assert "--forget DC_VOICE while this shell sets DC_VOICE" in r.output


@pytest.mark.parametrize("shell, recorded, want", [
    ({"DC_AGENT": "Ada ", "CLAUDE_CODE_AGENT": "bo"}, ["cy"], "ada"),
    ({"CLAUDE_CODE_AGENT": "Bo"}, ["cy"], "bo"),       # the shell names before the file
    ({}, ["cy"], "cy"),                                # the one bot the file records
    ({}, [], "claude"),
])
def test_which_bot_this_shell_then_the_one_recorded_then_claude(shell, recorded, want):
    assert settings.pick_name(shell, recorded)[0] == want


def test_two_bots_recorded_and_no_name_is_refused_naming_them(rig, monkeypatch):
    assert _install("--owner", OWNER).exit_code == 0
    data = _saved()
    data["bots"]["bo"] = {"DC_HOST": "mac.example.net"}
    _phone_json().write_text(json.dumps(data))
    before = rig.read_bytes()
    _empty_shell(monkeypatch)
    r = _install()
    assert r.exit_code == 2
    assert "records 2 bots (ada, bo) and this shell names none" in r.output
    assert "DC_AGENT=ada sessionphone bot install" in r.output
    assert rig.read_bytes() == before


# ---- refusals write nothing ---------------------------------------------------------

def test_a_refused_install_writes_no_file(rig, monkeypatch):
    monkeypatch.setenv("DC_CALLERS", "someone@phone.example.org")    # refused while set
    r = _install("--owner", OWNER)
    assert r.exit_code == 2, r.output
    assert not _phone_json().exists()


def test_a_job_that_does_not_load_saves_nothing(rig, launchd):
    launchd.never_loads = True
    r = _install("--owner", OWNER)
    assert r.exit_code == 1
    assert not _phone_json().exists()


@pytest.mark.parametrize("key", ["DC_SIGNER_PUB", "DC_SIGNER_SOCK"])
def test_a_saved_signer_path_that_has_gone_is_refused(rig, monkeypatch, key):
    assert _install("--owner", OWNER).exit_code == 0
    gone = pathlib.Path(os.environ[key])
    _empty_shell(monkeypatch)
    gone.unlink()
    # the refusal's agent-run line is test_first_seat's; here the tripwire is never asked
    monkeypatch.setattr(seat, "runs_agent", lambda given, shell: False)
    before, saved = rig.read_bytes(), _phone_json().read_bytes()
    r = _install()
    assert r.exit_code == 2
    assert f"{key} in ~/.sessionkit/phone/settings/phone.json names {gone}, which is not there" in r.output
    assert f"--forget {key}" in r.output
    assert rig.read_bytes() == before and _phone_json().read_bytes() == saved


@pytest.mark.parametrize("text, said", [
    ("{not json", "is not JSON"),
    ("[]", "is not a JSON object"),
    ('{"bots": {"ada": {"DC_VOICE": 1}}}', "has `bots.ada.DC_VOICE`, which is not a string"),
    ('{"_locked": "bots"}', "has a `_locked` that is not a list"),
])
def test_a_file_that_cannot_be_used_is_refused_naming_it(rig, text, said):
    _phone_json().parent.mkdir(parents=True)
    _phone_json().write_text(text)
    r = _install("--owner", OWNER)
    assert r.exit_code == 2
    assert f"~/.sessionkit/phone/settings/phone.json {said}" in r.output
    assert _phone_json().read_text() == text and not rig.exists()


def test_an_unreadable_file_is_refused_naming_it(rig):
    _phone_json().parent.mkdir(parents=True)
    _phone_json().write_text("{}")
    _phone_json().chmod(0)
    try:
        r = _install("--owner", OWNER)
    finally:
        _phone_json().chmod(0o600)
    assert r.exit_code == 2
    assert "~/.sessionkit/phone/settings/phone.json could not be read" in r.output


# ---- drop-ins and locks, as the kit's host layer is read ---------------------------

def _write(p: pathlib.Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


def test_drop_ins_are_read_after_the_file_in_name_order(rig, monkeypatch):
    assert _install("--owner", OWNER).exit_code == 0
    d = _phone_json().with_name("phone.d")
    _write(d / "50-a.json", {"bots": {"ada": {"DC_VOICE": "from-a", "DC_ANSWER": "from-a"}}})
    _write(d / "90-b.json", {"bots": {"ada": {"DC_VOICE": "from-b"}}})
    _empty_shell(monkeypatch)
    r = _install()
    assert r.exit_code == 0, r.output
    env = _job_env(rig)
    assert env["DC_VOICE"] == "from-b" and env["DC_ANSWER"] == "from-a"
    assert _row(r.output, "DC_VOICE").endswith("~/.sessionkit/phone/settings/phone.d/90-b.json")


def test_a_shell_value_a_drop_in_overrides_is_not_saved_and_says_so(rig, monkeypatch):
    assert _install("--owner", OWNER).exit_code == 0
    _write(_phone_json().with_name("phone.d") / "50-a.json", {"bots": {"ada": {"DC_VOICE": "from-a"}}})
    _empty_shell(monkeypatch)
    monkeypatch.setenv("DC_VOICE", "af_bella")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(rig)["DC_VOICE"] == "af_bella"
    assert _saved()["bots"]["ada"]["DC_VOICE"] == "af_sky"
    assert "not saved: ~/.sessionkit/phone/settings/phone.d/50-a.json sets it" in _row(r.output, "DC_VOICE")


def test_a_locked_key_holds_against_later_files_and_refuses_this_shell(rig, monkeypatch):
    assert _install("--owner", OWNER).exit_code == 0
    data = _saved()
    data["_locked"] = ["bots.ada.DC_VOICE"]
    _phone_json().write_text(json.dumps(data))
    _write(_phone_json().with_name("phone.d") / "50-a.json", {"bots": {"ada": {"DC_VOICE": "from-a"}}})
    _empty_shell(monkeypatch)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(rig)["DC_VOICE"] == "af_sky"
    assert _row(r.output, "DC_VOICE").endswith("phone.json (locked)")
    monkeypatch.setenv("DC_VOICE", "af_bella")
    r = _install()
    assert r.exit_code == 2
    assert "DC_VOICE is locked by ~/.sessionkit/phone/settings/phone.json" in r.output
    r = _install("--forget", "DC_VOICE")
    assert r.exit_code == 2


# ---- asking sessionmail whether it reads mail.json ---------------------------------

def _message_under(tmp_path, interpreter: str) -> str:
    exe = tmp_path / "message"
    exe.write_text(f"#!{interpreter}\nprint('a message script')\n")
    exe.chmod(0o755)
    return str(exe)


def _fake_python(tmp_path, body: str) -> str:
    py = tmp_path / "py" / "python3"
    py.parent.mkdir()
    py.write_text(f"#!/bin/sh\n{body}\n")
    py.chmod(0o755)
    return str(py)


def test_mail_file_asks_the_message_scripts_own_python(tmp_path):
    answer = {"sendmail": ["drop", "host"], "root": [None, "default"],
              "host": ["mac", "host"], "postfix.relay_domain": ["relay.home", "default"]}
    py = _fake_python(tmp_path, f"printf '%s\\n' '{json.dumps(answer)}'")
    got = REAL_MAIL_FILE(_message_under(tmp_path, py))
    assert got == {k: tuple(v) for k, v in answer.items()}


def test_mail_file_is_none_when_that_sessionmail_has_no_key_table(tmp_path):
    py = _fake_python(tmp_path, "exit 3")
    assert REAL_MAIL_FILE(_message_under(tmp_path, py)) is None


def test_mail_file_runs_nothing_for_a_message_that_is_not_a_python_script(tmp_path):
    trip = tmp_path / "ran"
    sh = tmp_path / "message"
    sh.write_text(f"#!/bin/sh\ntouch {trip}\n")
    sh.chmod(0o755)
    assert REAL_MAIL_FILE(str(sh)) is None
    assert REAL_MAIL_FILE(None) is None
    assert not trip.exists()


# ---- the mail: line when sessionmail's own file cannot give a carrier -----------

def _probe(sendmail):
    return {"sendmail": sendmail, "root": (None, "default"), "host": (None, "default"),
            "postfix.relay_domain": ("relay.home", "default")}


def test_mail_line_says_the_carrier_is_not_known_when_mail_json_cannot_be_read():
    line = service.mail_line({"DC_AGENT": "ada"}, probe=_probe((None, settings.UNREADABLE)))
    assert "carrier is not known: sessionmail could not read ~/.sessionkit/mail/settings/mail.json" in line
    assert "None" not in line and "default" not in line


def test_mail_line_says_a_sendmail_that_is_not_a_carrier_goes_nowhere():
    line = service.mail_line({"DC_AGENT": "ada"}, probe=_probe(("smtp", "host")))
    assert 'goes nowhere: ~/.sessionkit/mail/settings/mail.json `sendmail` is "smtp"' in line
    assert "goes by smtp" not in line


@pytest.mark.parametrize("sendmail", [(None, settings.UNREADABLE), ("smtp", "host")])
def test_a_carried_sendmail_wins_over_whatever_mail_json_holds(sendmail):
    line = service.mail_line({"DC_AGENT": "ada", "SESSIONMAIL_SENDMAIL": "drop"},
                             probe=_probe(sendmail))
    assert "carried SESSIONMAIL_SENDMAIL=drop; the bot's mail goes by drop" in line
    assert "not known" not in line and "nowhere" not in line


def test_a_shell_mail_key_beside_an_unreadable_mail_json_says_it_could_not_be_read():
    probe = {k: (None, settings.UNREADABLE) for k in settings.MAIL_KEYS.values()}
    note = settings.mail_note("SESSIONMAIL_HOST", "mac", probe)
    assert 'write "host": "mac"' in note and note.endswith("it could not be read)")

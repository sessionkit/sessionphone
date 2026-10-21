"""Each reader of a kept setting takes this shell, then the file, then the
default, as `bot install` does, and says which one it took.

The companion of test_readers_honour_settings.py (which measured the wrong
answers): here, the edges of each order, the words each reader says, the
cost of asking sessionmail, and the messages that must be true.

Fakes only: FakeLaunchd (conftest), plists in tmp_path, a scratch HOME, and
sessionmail's answer stubbed (settings.mail_file) or given by a `message`
whose own Python is a shell script.
"""
import json
import os
import pathlib
import plistlib
import subprocess

import pytest
from click.testing import CliRunner

from sessionphone import paths, service, settings
from sessionphone.bridge import bot, dc_env, seatmail
from sessionphone.cli import main as cli

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_FILE = settings.FILE            # read at import, before conftest moves it
REAL_MAIL_FILE = settings.mail_file     # conftest stubs it per test
MAIL_JSON = "~/.sessionkit/mail/settings/mail.json"
PHONE_JSON = "~/.sessionkit/phone/settings/phone.json"
MAIL_ENV = ("SESSIONMAIL_SENDMAIL", "SESSIONMAIL_HOST", "SESSION_MAIL_HOST",
            "SESSIONMAIL_ROOT", "SESSIONMAIL_RELAY_DOMAIN", "SESSIONMAIL_HOME")


def _answer(**keys):
    """sessionmail's answer to the settings probe: KEYS over 'unset, from the default'."""
    out = {"sendmail": (None, "default"), "root": (None, "default"),
           "host": (None, "default"), "postfix.relay_domain": (None, "default")}
    out.update(keys)
    return out


@pytest.fixture
def no_mail_env(monkeypatch):
    for k in MAIL_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "othermac.local")


@pytest.fixture
def asked(monkeypatch, no_mail_env):
    """settings.mail_file answers as a sessionmail whose mail.json holds KEYS;
    -> set(**keys), and .calls, each `message` it was asked of."""
    calls = []
    box = {"answer": None}

    def fake(exe):
        calls.append(exe)
        return box["answer"]
    monkeypatch.setattr(settings, "mail_file", fake)

    def set_(**keys):
        box["answer"] = _answer(**keys)
    set_.calls = calls
    return set_


def _sessionmail(d: pathlib.Path, answer: dict, count: pathlib.Path) -> str:
    """A `message` whose interpreter answers the probe with ANSWER and
    appends a line to COUNT each time it is asked. -> its path."""
    d.mkdir()
    py = d / "python3"
    py.write_text(f"#!/bin/sh\necho asked >> '{count}'\n"
                  f"printf '%s\\n' '{json.dumps({k: list(v) for k, v in answer.items()})}'\n")
    py.chmod(0o755)
    exe = d / "message"
    exe.write_text(f"#!{py}\nprint('a message script')\n")
    exe.chmod(0o755)
    return str(exe)


# ---- (1a) the mail host: the order, its words, and the pin -------------------

def test_the_mail_host_says_it_came_from_mail_json(asked):
    asked(host=(" mailhost\n", "host"))
    assert dc_env.mail_host_from({"PATH": "/usr/bin"}) == (
        "mailhost", f"sessionmail's {MAIL_JSON} `host`")


@pytest.mark.parametrize("var", ["SESSIONMAIL_HOST", "SESSION_MAIL_HOST"])
def test_a_host_in_the_environment_is_taken_before_sessionmail_is_asked(asked, var):
    asked(host=("mailhost", "host"))
    assert dc_env.mail_host_from({var: "shellhost"}) == ("shellhost", f"{var} in this environment")
    assert asked.calls == []


@pytest.mark.parametrize("filed", [("mailhost", "default"), ("", "host"), ("  ", "template"),
                                   (None, "unreadable"), (7, "host")],
                         ids=["default-source", "empty", "blank", "unreadable", "not-a-string"])
def test_only_a_host_mail_json_gives_is_taken(asked, filed):
    """sessionmail's own judgement (its file_text): a non-empty string from the file."""
    asked(host=filed)
    assert dc_env.mail_host_from({}) == ("othermac", dc_env.FROM_MACHINE)


def test_with_no_answer_the_mail_host_is_this_machines_name(monkeypatch, no_mail_env):
    monkeypatch.setattr(settings, "mail_file", lambda exe: None)
    assert dc_env.mail_host_from({}) == ("othermac", "this machine's name")


def test_ask_false_never_asks_sessionmail(asked):
    asked(host=("mailhost", "host"))
    assert dc_env.mail_host({}, ask=False) == "othermac"
    assert asked.calls == []


def test_the_message_asked_is_the_one_the_bot_sends_with(asked, tmp_path):
    exe = tmp_path / "bin" / "message"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    asked(host=("mailhost", "host"))
    dc_env.mail_host({"DC_MESSAGE": str(exe)})
    dc_env.mail_host({"PATH": str(exe.parent)})
    assert asked.calls == [str(exe), str(exe)]


def test_the_send_pins_the_host_mail_json_gives(asked):
    """child_env pins SESSIONMAIL_HOST into every send: it must be the host
    sessionmail would have read itself, never this machine's name over it."""
    asked(host=("mailhost", "host"))
    assert seatmail.child_env({"PATH": "/usr/bin"})["SESSIONMAIL_HOST"] == "mailhost"
    assert seatmail.id_host({"PATH": "/usr/bin"}) == "mailhost"


def test_host_problem_judges_the_mail_json_host(asked):
    asked(host=("localhost", "host"))
    assert "'localhost', which names no machine" in dc_env.host_problem({})
    asked(host=("mailhost", "host"))
    assert dc_env.host_problem({}) is None


# ---- (1a) at the bot's start: rebound once, from the full order ---------------

@pytest.fixture
def import_time(monkeypatch):
    """dc_env's import-time answers, put back after the test."""
    for k in ("HOST", "ADDR", "MAILTO", "HOST_PROBLEM", "AGENT"):
        monkeypatch.setattr(dc_env, k, getattr(dc_env, k))
    monkeypatch.setattr(dc_env, "AGENT", "ada")


def test_the_bot_rebinds_its_addresses_on_the_mail_json_host(asked, import_time, monkeypatch):
    for k in ("DC_HOST", "DC_ADDR", "DC_MAILTO"):
        monkeypatch.delenv(k, raising=False)
    asked(host=("mailhost", "host"))
    assert dc_env.take_mail_host() == f"sessionmail's {MAIL_JSON} `host`"
    assert (dc_env.HOST, dc_env.ADDR, dc_env.MAILTO) == ("mailhost", "ada-chat@mailhost", "ada@mailhost")
    assert dc_env.HOST_PROBLEM is None


def test_the_bot_refuses_a_mail_json_host_that_names_no_machine(asked, import_time, monkeypatch):
    monkeypatch.delenv("DC_HOST", raising=False)
    asked(host=("localhost", "host"))
    dc_env.take_mail_host()
    assert "names no machine" in dc_env.HOST_PROBLEM


def test_names_the_operator_gave_are_kept_at_the_rebind(asked, import_time, monkeypatch):
    monkeypatch.setenv("DC_HOST", "mac.example.net")
    monkeypatch.setenv("DC_MAILTO", "box@elsewhere.example")
    asked(host=("mailhost", "host"))
    dc_env.take_mail_host()
    assert dc_env.HOST == "mac.example.net"
    assert dc_env.MAILTO == "box@elsewhere.example"


def test_without_a_mail_json_host_nothing_is_rebound(asked, import_time):
    asked()
    before = (dc_env.HOST, dc_env.ADDR, dc_env.MAILTO)
    dc_env.HOST_PROBLEM = "as it was set"       # import_time puts it back
    assert dc_env.take_mail_host() == dc_env.FROM_MACHINE
    assert (dc_env.HOST, dc_env.ADDR, dc_env.MAILTO) == before
    assert dc_env.HOST_PROBLEM == "as it was set"


def test_importing_dc_env_asks_no_sessionmail():
    """No probe at import: every `sessionphone` verb imports dc_env."""
    code = ("import sys\n"
            "from sessionphone import settings\n"
            "asked = []\n"
            "settings.mail_file = lambda exe: asked.append(exe)\n"
            "from sessionphone.bridge import dc_env\n"
            "print(len(asked))\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "SESSIONMAIL_"))}
    env["HOME"] = str(REPO / "no-such-home")
    r = subprocess.run([str(REPO / ".venv/bin/python"), "-c", code], capture_output=True,
                       text=True, env=env, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0"


# ---- the probe is asked once per `message` --------------------------------------

def test_sessionmail_is_asked_once_per_message(monkeypatch, tmp_path, no_mail_env):
    monkeypatch.setattr(settings, "mail_file", REAL_MAIL_FILE)
    count = tmp_path / "asked"
    exe = _sessionmail(tmp_path / "sm", _answer(host=("mailhost", "host")), count)
    for _ in range(3):
        assert dc_env.mail_host({"DC_MESSAGE": exe}) == "mailhost"
    assert count.read_text().splitlines() == ["asked"]
    settings.forget_mail_file()
    assert dc_env.mail_host({"DC_MESSAGE": exe}) == "mailhost"
    assert len(count.read_text().splitlines()) == 2


def test_a_kept_answer_cannot_be_changed_by_its_caller(monkeypatch, tmp_path, no_mail_env):
    monkeypatch.setattr(settings, "mail_file", REAL_MAIL_FILE)
    exe = _sessionmail(tmp_path / "sm", _answer(host=("mailhost", "host")), tmp_path / "n")
    REAL_MAIL_FILE(exe)["host"] = ("changed", "host")
    assert REAL_MAIL_FILE(exe)["host"] == ("mailhost", "host")


# ---- (1b) the carrier: one speaker, every answer sessionmail can give -------------

@pytest.mark.parametrize("filed, said", [
    (("drop", "host"), f"drop (sessionmail's {MAIL_JSON} `sendmail`)"),
    (("drop", "default"), "drop (sessionmail's default)"),
    (("/opt/mta/sendmail", "template"), f"MTA /opt/mta/sendmail (sessionmail's {MAIL_JSON} `sendmail`)"),
    ((None, "unreadable"), (f"not known: sessionmail could not read {MAIL_JSON}, and sends nothing "
                            "until it can")),
    (("smtp", "host"), (f'nowhere: {MAIL_JSON} `sendmail` is "smtp", which is not a carrier (`drop`, '
                        "or the absolute path of an MTA), so sessionmail refuses to send")),
], ids=["file-drop", "default-drop", "file-mta", "unreadable", "not-a-carrier"])
def test_carrier_names_what_sessionmail_reads_and_where(asked, filed, said):
    asked(sendmail=filed)
    assert seatmail.carrier({}) == said


def test_carrier_given_a_probe_asks_nothing(asked):
    asked(sendmail=("drop", "host"))
    assert seatmail.carrier({}, probe=None).startswith("MTA ")
    assert seatmail.carrier({}, probe=_answer(sendmail=("drop", "default"))) == "drop (sessionmail's default)"
    assert asked.calls == []


def test_the_mail_line_and_the_carrier_say_one_thing(no_mail_env):
    probe = _answer(sendmail=("/opt/mta/sendmail", "host"))
    line = service.mail_line({"DC_AGENT": "ada"}, probe=probe)
    assert f"the bot's mail goes by {seatmail.carrier({}, probe)}." in line
    assert f'write "sendmail": "drop" in {MAIL_JSON}' in line


def test_the_start_line_names_the_mail_json_host_and_carrier(asked, import_time, monkeypatch, tmp_path,
                                                            seat_key, fake_message):
    """The bot's log says where its host and carrier came from, so a deployed
    bot can be checked from its log alone."""
    for k in ("DC_HOST", "DC_ADDR", "DC_MAILTO"):
        monkeypatch.delenv(k, raising=False)
    seat_key("ada-chat seat")
    asked(host=("mailhost", "host"), sendmail=("drop", "host"))

    class Reached(Exception):
        pass

    def rpc(**k):
        raise Reached
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(bot, "LOG", tmp_path / "bot.log")
    monkeypatch.setattr(bot, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "server_choice", lambda: ("/x", "venv", "0"))
    monkeypatch.setattr(bot, "Rpc", rpc)
    with pytest.raises(Reached):
        bot.main()
    [line] = [m for m in logged if m.startswith("agent mail: ")]
    assert f"(ids at mailhost, from sessionmail's {MAIL_JSON} `host`)" in line
    assert line.endswith(f"carrier drop (sessionmail's {MAIL_JSON} `sendmail`)")


# ---- (2) the bot and the root: the order's edges and words ---------------------------

@pytest.fixture
def phone_home(monkeypatch, tmp_path):
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


def _phone_json(data) -> pathlib.Path:
    p = pathlib.Path(DEFAULT_FILE).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def test_the_root_says_where_it_came_from(phone_home, monkeypatch, tmp_path):
    assert paths.root_from() == (phone_home / ".sessionphone", "the default")
    _phone_json({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/ada-root"}}})
    assert paths.root_from() == (phone_home / "ada-root", PHONE_JSON)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "r"))
    assert paths.root_from() == (tmp_path / "r", "SESSIONPHONE_ROOT in this shell")


def test_an_empty_root_in_this_shell_is_no_root(phone_home, monkeypatch):
    """Empty is unset everywhere else in phone: not the current directory."""
    monkeypatch.setenv("SESSIONPHONE_ROOT", "")
    assert paths.root() == phone_home / ".sessionphone"
    _phone_json({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/ada-root"}}})
    assert paths.root() == phone_home / "ada-root"


def test_the_root_is_the_named_bots(phone_home, monkeypatch):
    _phone_json({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/ada"}, "bo": {"SESSIONPHONE_ROOT": "~/bo"}}})
    monkeypatch.setenv("DC_AGENT", "bo")
    assert paths.root() == phone_home / "bo"
    monkeypatch.setenv("DC_AGENT", "cy")       # a bot the file does not record: the default
    assert paths.root() == phone_home / ".sessionphone"


def test_a_drop_in_root_wins_and_a_lock_holds_the_earlier_one(phone_home):
    main = _phone_json({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/main"}}})
    d = main.with_name("phone.d")
    d.mkdir()
    (d / "10.json").write_text(json.dumps({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/dropin"}}}))
    assert paths.root_from() == (phone_home / "dropin", "~/.sessionkit/phone/settings/phone.d/10.json")
    _phone_json({"_locked": ["bots.ada.SESSIONPHONE_ROOT"],
                 "bots": {"ada": {"SESSIONPHONE_ROOT": "~/main"}}})
    assert paths.root() == phone_home / "main"


def test_a_reader_follows_this_shell_over_a_locked_root(phone_home, monkeypatch, tmp_path):
    """Only install, which writes, enforces a lock; a reader never refuses to find a directory."""
    _phone_json({"_locked": ["bots.ada"], "bots": {"ada": {"SESSIONPHONE_ROOT": "~/locked"}}})
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "shell"))
    assert paths.root() == tmp_path / "shell"


def test_two_bots_and_no_name_refuses_the_root_naming_the_way_past(phone_home):
    _phone_json({"bots": {"ada": {}, "bo": {}}})
    with pytest.raises(settings.Bad) as got:
        paths.root()
    assert "records 2 bots (ada, bo)" in str(got.value)
    assert "export DC_AGENT=ada" in str(got.value)


def test_dc_env_root_at_import_never_raises(phone_home, monkeypatch):
    _phone_json("{not json")
    monkeypatch.setattr(dc_env, "BAD", [])
    assert dc_env._root() == phone_home / ".sessionphone"
    [said] = dc_env.BAD
    assert f"{PHONE_JSON} is not JSON" in said and "Using ~/.sessionphone for now." in said


def test_a_verb_that_needs_the_root_refuses_a_broken_phone_json(phone_home):
    _phone_json("{not json")
    r = CliRunner().invoke(cli, ["callers"])
    assert r.exit_code == 2, r.output
    assert f"sessionphone: {PHONE_JSON} is not JSON" in r.output
    assert "then run it again" in r.output
    assert "Traceback" not in r.output


def test_bot_status_says_which_bot_and_where_from(phone_home, launchd, monkeypatch):
    r = CliRunner().invoke(cli, ["bot", "status"])
    assert "bot:     claude (the default)" in r.output
    _phone_json({"bots": {"ada": {}}})
    r = CliRunner().invoke(cli, ["bot", "status"])
    assert f"bot:     ada ({PHONE_JSON}, the one bot it records)" in r.output
    assert r.output.splitlines()[:2] == [f"job:     {service.LABEL_PREFIX}.ada (not installed)",
                                         f"bot:     ada ({PHONE_JSON}, the one bot it records)"]
    monkeypatch.setenv("DC_AGENT", "Bo")     # the name asked for: nothing to say
    r = CliRunner().invoke(cli, ["bot", "status"])
    assert f"job:     {service.LABEL_PREFIX}.bo (not installed)" in r.output
    assert "bot:" not in r.output


def test_bot_uninstall_says_which_bot_the_file_picked(phone_home, launchd):
    _phone_json({"bots": {"ada": {}}})
    r = CliRunner().invoke(cli, ["bot", "uninstall"])
    lines = r.output.splitlines()
    assert lines[0] == f"bot:     ada ({PHONE_JSON}, the one bot it records)"
    assert lines[1].startswith(f"removed launchd job {service.LABEL_PREFIX}.ada ")


@pytest.mark.parametrize("verb", ["status", "uninstall"])
def test_two_bots_and_no_name_refuses_status_and_uninstall(phone_home, launchd, verb):
    _phone_json({"bots": {"ada": {}, "bo": {}}})
    r = CliRunner().invoke(cli, ["bot", verb])
    assert r.exit_code == 2
    assert f"Name one: `DC_AGENT=ada sessionphone bot {verb}`" in r.output


@pytest.mark.parametrize("verb", ["status", "uninstall"])
def test_a_broken_phone_json_refuses_naming_the_shell_way_past(phone_home, launchd, verb):
    _phone_json("[]")
    r = CliRunner().invoke(cli, ["bot", verb])
    assert r.exit_code == 2
    assert f"{PHONE_JSON} is not a JSON object" in r.output
    assert f"run `sessionphone bot {verb}` again" in r.output
    assert f"`DC_AGENT=NAME sessionphone bot {verb}`" in r.output


def test_a_bot_named_in_this_shell_is_uninstalled_past_a_broken_phone_json(phone_home, launchd, monkeypatch):
    """The shell's name is taken before any file is read: a broken file never
    keeps a running job from being removed."""
    p = service.plist_path("ada")
    p.write_bytes(plistlib.dumps(service.job("ada", python="/venv/libexec/session-phone",
                                             given={"SESSIONPHONE_ROOT": str(phone_home / "r")})))
    _phone_json("{not json")
    monkeypatch.setenv("DC_AGENT", "ada")
    r = CliRunner().invoke(cli, ["bot", "uninstall"])
    assert r.exit_code == 0, r.output
    assert not p.exists()


# ---- (3) the messages ------------------------------------------------------------

def test_a_lock_with_a_value_still_names_it(phone_home):
    _phone_json({"_locked": ["bots.ada.DC_VOICE"], "bots": {"ada": {"DC_VOICE": "af_sky"}}})
    with pytest.raises(settings.Bad) as got:
        settings.resolve({"DC_AGENT": "ada", "DC_VOICE": "af_bella"})
    assert f"DC_VOICE is locked by {PHONE_JSON} at 'af_sky'" in str(got.value)


def test_a_lock_with_no_value_says_the_bot_takes_its_default(phone_home):
    _phone_json({"_locked": ["bots.ada.DC_VOICE"], "bots": {"ada": {}}})
    with pytest.raises(settings.Bad) as got:
        settings.resolve({"DC_AGENT": "ada", "DC_VOICE": "af_bella"})
    assert str(got.value) == (
        f"DC_VOICE is locked by {PHONE_JSON}, which gives it no value (the bot takes its "
        "default), and this shell sets 'af_bella': nothing was written. Unset DC_VOICE, or set it "
        f"(or drop the lock) in {PHONE_JSON}.")


def test_a_lock_with_no_value_refuses_nothing_the_shell_leaves_unset(phone_home):
    _phone_json({"_locked": ["bots.ada.DC_VOICE"], "bots": {"ada": {}}})
    assert "DC_VOICE" not in settings.resolve({"DC_AGENT": "ada"}).given


def test_the_settings_header_says_not_saved_when_the_write_failed(phone_home, monkeypatch):
    res = settings.resolve({"DC_AGENT": "ada", "DC_VOICE": "af_bella"})
    res.env = {"DC_AGENT": "ada", "DC_VOICE": "af_bella"}
    monkeypatch.setattr(settings, "_mkdirs", lambda d: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(settings.NotSaved) as got:
        settings.save(res)
    assert got.value.plist is None   # declared on the class; install fills it in before it re-raises
    assert settings.block(res, None)[0] == f"settings:  {PHONE_JSON} (NOT saved), bot ada"


def test_pick_name_names_the_verb_to_run_again():
    with pytest.raises(settings.Bad) as got:
        settings.pick_name({}, ["ada", "bo"], "bot status")
    assert "`DC_AGENT=ada sessionphone bot status`" in str(got.value)
    with pytest.raises(settings.Bad) as got:
        settings.pick_name({}, ["ada", "bo"])
    assert "`DC_AGENT=ada sessionphone bot install`" in str(got.value)


# ---- the dc-* tools read the same root (bin/dc-env, bin/dc-log) ---------------------

def _dc_env_root(home: pathlib.Path, **extra) -> str:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", **extra}
    r = subprocess.run(["bash", "-c", f'. "{REPO}/bin/dc-env"; printf %s "$DC_ROOT"'],
                       capture_output=True, text=True, env=env, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_dc_env_reads_the_root_phone_json_keeps(phone_home):
    _phone_json({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/ada-root"}}})
    assert _dc_env_root(phone_home) == str(phone_home / "ada-root")


def test_dc_env_takes_this_shells_root_first(phone_home, tmp_path):
    _phone_json({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/ada-root"}}})
    assert _dc_env_root(phone_home, SESSIONPHONE_ROOT=str(tmp_path / "s")) == str(tmp_path / "s")
    assert _dc_env_root(phone_home, DC_ROOT=str(tmp_path / "d")) == str(tmp_path / "d")


def test_dc_env_falls_back_to_the_default_when_the_question_fails(phone_home):
    _phone_json("{not json")
    assert _dc_env_root(phone_home) == str(phone_home / ".sessionphone")


def test_dc_log_reads_the_root_phone_json_keeps_and_survives_a_broken_one(phone_home):
    root = phone_home / "ada-root"
    root.mkdir()
    (root / "bot.log").write_text("2026-01-01 00:00:00 a line in the kept root\n")
    _phone_json({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/ada-root"}}})
    env = {"HOME": str(phone_home), "PATH": f"{REPO / '.venv/bin'}:/usr/bin:/bin"}
    run = [str(REPO / ".venv/bin/python"), str(REPO / "bin/dc-log"), "--raw"]
    r = subprocess.run(run, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert "a line in the kept root" in r.stdout, r.stderr
    _phone_json("{not json")
    r = subprocess.run(run, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert "Traceback" not in r.stderr
    assert str(phone_home / ".sessionphone") in r.stdout + r.stderr

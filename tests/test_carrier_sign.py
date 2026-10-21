"""Signing follows the carrier: ONE rule, seatmail.signing().

The carrier is the one this bot's own `message send` will use, as its
sessionmail's own resolver answers it (settings.mail_carrier, asked with the
job's SESSIONMAIL_SENDMAIL). An MTA is signed (must-sign); the mail drop with
no signer is unsigned; a signer given is used on any carrier; a carrier
sessionmail refuses, or one that cannot be known, refuses the start.

Fakes only: no real sessionmail, `message`, key or MTA.
"""
import ast
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from sessionphone import seat, settings
from sessionphone.bridge import bot, dc_env, seatmail
from sessionphone.web import turns

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "sessionphone"
MAIL_JSON = settings.shown(settings.MAIL_FILE)


def answer(kind, path=None, source="default", why=""):
    """sessionmail's resolver's answer, as settings.mail_carrier returns it."""
    return {"kind": kind, "path": path, "source": source, "why": why}


FRESH_MAC = answer("drop")                          # nothing set, no `sendmail` key: sessionmail's default
MTA = answer("mta", "/usr/sbin/sendmail", "host")   # the `sendmail` key in mail.json names an MTA
UNREADABLE = answer("refused", None, "unreadable",
                    f"{MAIL_JSON} could not be read (bad json), so the carrier is not known")
NOWHERE = answer("refused", None, "host", f'"sendmail": "smtp" in {MAIL_JSON} is not a carrier')


@pytest.fixture
def carrier(monkeypatch):
    """carrier(ans) -> settings.mail_carrier answers ANS (None: cannot be asked).
    .asked holds each (exe, sendmail_value) it was asked with."""
    box = {"ans": None}
    asked = []

    def fake(exe, sendmail_value=None):
        asked.append((exe, sendmail_value))
        return None if box["ans"] is None else dict(box["ans"])

    def set_(ans):
        box["ans"] = ans
    set_.asked = asked
    monkeypatch.setattr(settings, "mail_carrier", fake)
    return set_


@pytest.fixture
def keyless(monkeypatch):
    """No signer given: the tripwire's DC_SIGNER_* are unset."""
    for k in ("DC_SIGNER_SOCK", "DC_SIGNER_PUB", "PIP_CHAT_SIGNER_SOCK", "PIP_CHAT_SIGNER_PUB"):
        monkeypatch.delenv(k, raising=False)


# ---- the rule: (mode, reason) from sessionmail's answer ---------------------------

@pytest.mark.parametrize("ans, mode, reason", [
    (MTA, "signed", f"carrier /usr/sbin/sendmail is an MTA, from sessionmail's {MAIL_JSON} `sendmail`"),
    (answer("mta", "/opt/mta/sendmail", "variable"), "signed",
     "carrier /opt/mta/sendmail is an MTA, from SESSIONMAIL_SENDMAIL"),
    (answer("drop", None, "host"), "unsigned",
     f"carrier drop: this bot writes into the box itself, from sessionmail's {MAIL_JSON} `sendmail`"),
    (FRESH_MAC, "unsigned", "carrier drop: this bot writes into the box itself, from sessionmail's default"),
    (answer("drop", None, "variable"), "unsigned",
     "carrier drop: this bot writes into the box itself, from SESSIONMAIL_SENDMAIL"),
], ids=["settings-mta", "variable-mta", "settings-drop", "fresh-mac-default-drop", "variable-drop"])
def test_the_mode_and_its_reason_come_from_sessionmails_answer(carrier, keyless, ans, mode, reason):
    carrier(ans)
    assert seatmail.signing({"PATH": "/usr/bin"}) == (mode, reason)


def test_sessionmail_is_asked_with_the_jobs_own_sendmail_variable(carrier, fake_message):
    """SESSIONMAIL_SENDMAIL wins in sessionmail's own order: the rule hands the
    JOB's value, as it stands, to sessionmail's resolver; it never reads it itself."""
    carrier(answer("drop", None, "variable"))
    env = {"DC_MESSAGE": str(fake_message.exe), "SESSIONMAIL_SENDMAIL": " drop\n"}
    assert seatmail.signing(env)[0] == "unsigned"
    assert carrier.asked == [(str(fake_message.exe), " drop\n")]
    carrier.asked.clear()
    seatmail.signing({"DC_MESSAGE": str(fake_message.exe)})
    assert carrier.asked == [(str(fake_message.exe), None)]


def test_no_env_and_no_sendmail_key_is_the_drop_the_fresh_mac_case(carrier, keyless):
    """Measured on a fresh Mac, nothing set: sessionmail's resolver says
    Carrier(kind='drop', path=None, source='default'), while the old words say
    MTA /usr/sbin/sendmail. The rule takes sessionmail's answer."""
    carrier(FRESH_MAC)
    mode, reason = seatmail.signing({})
    assert mode == "unsigned" and "/usr/sbin/sendmail" not in reason
    assert seatmail.carrier({}) == "drop (sessionmail's default)"   # and the log words agree


def test_postfix_present_but_no_sendmail_key_is_still_the_drop(carrier, keyless, monkeypatch):
    """A launch agent with no `sendmail` key DROPS even on a Mac with postfix:
    the rule never looks for an MTA on this host, only at sessionmail's answer."""
    carrier(FRESH_MAC)
    with monkeypatch.context() as m:          # postfix is "installed": every MTA path is there
        m.setattr(os.path, "exists", lambda p: True)
        m.setattr(os.path, "isfile", lambda p: True)
        m.setattr(os, "access", lambda p, mode: True)
        m.setattr(seatmail.shutil, "which", lambda *a, **k: "/usr/sbin/sendmail")
        assert seatmail.signing({"PATH": "/usr/sbin:/usr/bin"})[0] == "unsigned"
    fn = _fn(SRC / "bridge/seatmail.py", "signing")
    calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert not [c for c in calls if c.startswith(("os.path", "shutil", "pathlib", "subprocess"))], calls


@pytest.mark.parametrize("ans", [MTA, answer("drop", None, "host"), FRESH_MAC, None, UNREADABLE],
                         ids=["mta", "drop", "default-drop", "not-asked", "refused"])
def test_a_signer_given_is_used_on_any_carrier(carrier, ans):
    """The tripwire gives DC_SIGNER_*: the key is handed over whatever the
    carrier, and must-sign is on (a keyed bot keeps signing, drop included)."""
    carrier(ans)
    env = seatmail.child_env()
    assert env["CLAUDE_SESSION_AGENT_SOCK"] == os.environ["DC_SIGNER_SOCK"]
    assert env["CLAUDE_SESSION_PUB"] == os.environ["DC_SIGNER_PUB"]
    assert env["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1"
    if ans is not None and ans["kind"] == "drop":
        given = "a signer is given in DC_SIGNER_SOCK and DC_SIGNER_PUB, on carrier drop"
        assert seatmail.signing() == ("signed", given)


# ---- an MTA: exactly today's must-sign ------------------------------------------

def test_mta_forces_must_sign_and_drops_the_callers_own_value(carrier, keyless, monkeypatch):
    carrier(MTA)
    monkeypatch.setenv("SESSIONMAIL_REQUIRE_SIGNATURE", "0")
    monkeypatch.setenv("SESSION_MAIL_REQUIRE_SIGNATURE", "off")
    env = seatmail.child_env()
    assert env["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1" and "SESSION_MAIL_REQUIRE_SIGNATURE" not in env
    assert "CLAUDE_SESSION_PUB" not in env and "CLAUDE_SESSION_AGENT_SOCK" not in env


def test_mta_with_no_usable_key_refuses_the_start(carrier, keyless):
    """PHONE-197 criterion 5, on an MTA carrier: no key, no start."""
    carrier(MTA)
    why = seatmail.check()
    assert why.startswith("no signer: DC_SIGNER_SOCK and DC_SIGNER_PUB are not set, and the bot never "
                          "sends its agent unsigned mail here (signed: carrier /usr/sbin/sendmail is an MTA")


def test_mta_a_send_with_no_usable_key_is_refused(carrier, keyless, monkeypatch, tmp_path):
    """The send itself, under an MTA: must-sign reaches `message send`, which
    refuses a send it cannot sign (kind identity), and nothing is sent."""
    carrier(MTA)
    exe = tmp_path / "message"
    exe.write_text("#!/bin/sh\ncat > /dev/null\n"
                   'if [ "$SESSIONMAIL_REQUIRE_SIGNATURE" = 1 ] && [ -z "$CLAUDE_SESSION_PUB" ]; then\n'
                   '  echo \'{"sent": "no", "kind": "identity", "message": "cannot be signed"}\'; exit 2\nfi\n'
                   'echo \'{"sent": "yes", "id": "<1@h>"}\'\n')
    exe.chmod(0o755)
    monkeypatch.setattr(seatmail, "MESSAGE", str(exe))
    sent = seatmail.send("ada@example.net", "s", "b")
    assert not sent.ok and sent.kind == "identity"


# ---- the drop, no signer: unsigned ------------------------------------------------

def test_drop_sets_no_must_sign_and_hands_over_no_key(carrier, keyless, monkeypatch):
    carrier(FRESH_MAC)
    monkeypatch.setenv("SESSIONMAIL_REQUIRE_SIGNATURE", "1")       # the caller's: dropped either way
    env = seatmail.child_env()
    assert not [k for k in env if k in seatmail.MUST_SIGN_VARS]
    assert not [k for k in env if k.startswith("CLAUDE_SESSION_")]


def test_drop_starts_with_no_signer_and_still_probes_message(carrier, keyless, fake_message):
    carrier(FRESH_MAC)
    assert seatmail.check() is None
    assert fake_message.probes() == 1                            # the --message-id probe still runs


def test_drop_half_a_signer_refuses(carrier, keyless, monkeypatch, fake_message):
    carrier(FRESH_MAC)
    monkeypatch.setenv("DC_SIGNER_SOCK", "/tmp/agent.sock")
    why = seatmail.check()
    assert why.startswith("half a signer: DC_SIGNER_SOCK is set and DC_SIGNER_PUB is not.")


def test_every_send_path_takes_the_one_rule(carrier, keyless, monkeypatch):
    """seatmail.send (chat), web/turns.py (a web call's turns) and the spool's
    retries all build their environment in child_env, under signing()."""
    seen = []

    def run(argv, **k):
        seen.append(k["env"])
        return subprocess.CompletedProcess(argv, 0, '{"sent": "yes", "id": "<1@h>"}', "")

    monkeypatch.setattr(seatmail.subprocess, "run", run)
    monkeypatch.setattr(seatmail, "MESSAGE", "/fake/message")
    for ans, must in ((FRESH_MAC, None), (MTA, "1")):
        carrier(ans)
        seen.clear()
        assert seatmail.send("ada@example.net", "s", "b").ok
        mail = turns.turn_mail(1, 1, "hello", turns.Seat("ada@example.net", "web-chat@example.net",
                                                          "Ada", "example.net"), "caller")
        assert mail.send().ok
        assert seatmail.send_args(["send", "x@y"], "b").ok     # the spool's retry door (spool._one)
        assert [e.get("SESSIONMAIL_REQUIRE_SIGNATURE") for e in seen] == [must] * 3


# ---- refused, not known, nowhere: their own state, a loud refusal -----------------

WILL_USE = "the carrier this bot's own `message send` will use"


@pytest.mark.parametrize("ans, says", [
    (None, f"{WILL_USE} is not known: its sessionmail could not be asked"),
    (UNREADABLE, f"sessionmail refuses {WILL_USE}: {MAIL_JSON} could not be read"),
    (NOWHERE, f'sessionmail refuses {WILL_USE}: "sendmail": "smtp"'),
    (answer("smtp-relay"), f'sessionmail names {WILL_USE} "smtp-relay", a kind this bot does not know'),
], ids=["not-known", "refused-unreadable", "nowhere", "unknown-kind"])
def test_no_carrier_is_its_own_state_and_refuses_loudly(carrier, keyless, ans, says, monkeypatch, tmp_path):
    carrier(ans)
    mode, reason = seatmail.signing()
    assert mode == "refused" and reason.startswith(says)
    assert "/usr/sbin/sendmail" not in reason                         # never the guessed MTA
    assert seatmail.child_env()["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1"   # never unsigned
    why = seatmail.check()
    assert why.startswith(f"no mail can reach the agent: {says}")
    env = {**os.environ, "DC_MESSAGE": os.environ["DC_MESSAGE"]}
    with pytest.raises(seat.NoCarrier) as e:
        seat.plan(env, "claude-chat", os.environ)
    assert "Nothing was written." in str(e.value)


@pytest.mark.parametrize("ans", [None, UNREADABLE, NOWHERE], ids=["not-known", "refused", "nowhere"])
def test_the_bot_refuses_to_start_on_no_carrier(carrier, ans, monkeypatch, capsys):
    """Signer given or not: sessionmail would send nothing, so the bot says so and exits 2."""
    carrier(ans)
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(dc_env, "take_mail_host", lambda: "h")
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    assert "sessionphone bot: no mail can reach the agent: " in capsys.readouterr().err


# ---- settings.mail_carrier: sessionmail's own resolver, asked ------------------------

FAKE_SESSIONMAIL = '''
import os
CARRIER_VAR = "SESSIONMAIL_SENDMAIL"
class Carrier:
    def __init__(self, kind, path, source, why=""):
        self.kind, self.path, self.source, self.why = kind, path, source, why
def carrier(env=None):
    env = os.environ if env is None else env
    v = (env.get(CARRIER_VAR) or "").strip()
    if v:
        if v == "drop":
            return Carrier("drop", None, "variable")
        if v.startswith("/"):
            return Carrier("mta", v, "variable")
        return Carrier("refused", None, "variable", "$SESSIONMAIL_SENDMAIL is " + repr(v))
    filed = os.environ.get("FAKE_FILED")      # the `sendmail` key in mail.json
    if filed:
        return Carrier("mta", filed, "host") if filed.startswith("/") else Carrier("drop", None, "host")
    return Carrier("drop", None, "default")
'''


@pytest.fixture
def real_sessionmail(tmp_path, monkeypatch):
    """A `message` whose interpreter runs the REAL probe against a fake
    `sessionmail._settings` (FAKE_SESSIONMAIL): sessionmail's resolver order.
    -> (exe, log of each probe's argv)."""
    monkeypatch.setattr(settings, "mail_carrier", REAL_MAIL_CARRIER)
    settings.forget_mail_file()
    pkg = tmp_path / "lib" / "sessionmail"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "_settings.py").write_text(FAKE_SESSIONMAIL)
    log = tmp_path / "asked"
    py = tmp_path / "bin" / "python3"
    py.parent.mkdir()
    # drops -I (which would hide the fake package) and keeps everything else
    py.write_text(f'#!/bin/sh\necho asked >> "{log}"\n[ "$1" = -I ] && shift\n'
                  f'PYTHONPATH="{tmp_path / "lib"}" exec "{sys.executable}" -s "$@"\n')
    py.chmod(0o755)
    exe = tmp_path / "bin" / "message"
    exe.write_text(f"#!{py}\nprint('a message script')\n")
    exe.chmod(0o755)
    yield str(exe), log
    settings.forget_mail_file()


REAL_MAIL_CARRIER = settings.mail_carrier    # taken at import: conftest stubs it in every test


def test_mail_carrier_asks_sessionmails_resolver_with_the_jobs_variable(real_sessionmail):
    exe, log = real_sessionmail
    assert settings.mail_carrier(exe) == answer("drop")                    # the fresh Mac
    assert settings.mail_carrier(exe, "/opt/mta/sendmail") == answer("mta", "/opt/mta/sendmail", "variable")
    assert settings.mail_carrier(exe, "drop") == answer("drop", None, "variable")
    assert settings.mail_carrier(exe, "smtp")["kind"] == "refused"
    assert len(log.read_text().splitlines()) == 4


def test_mail_carrier_reads_no_variable_but_the_one_it_is_given(real_sessionmail, monkeypatch):
    """The probe runs in a clean environment: the installer's shell variable
    does not reach it, as a shell variable does not reach launchd."""
    exe, _ = real_sessionmail
    monkeypatch.setenv("SESSIONMAIL_SENDMAIL", "/from/the/shell")
    assert settings.mail_carrier(exe) == answer("drop")


def test_mail_carrier_is_asked_once_per_message_and_value(real_sessionmail):
    exe, log = real_sessionmail
    for _ in range(3):
        settings.mail_carrier(exe)
        settings.mail_carrier(exe, "drop")
    assert len(log.read_text().splitlines()) == 2
    got = settings.mail_carrier(exe)
    got["kind"] = "mta"
    assert settings.mail_carrier(exe)["kind"] == "drop"                    # a kept answer is a copy


def test_mail_carrier_cannot_ask_a_message_that_is_no_python_script(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "mail_carrier", REAL_MAIL_CARRIER)
    sh = tmp_path / "message"
    sh.write_text("#!/bin/sh\necho hi\n")
    sh.chmod(0o755)
    assert settings.mail_carrier(str(sh)) is None
    assert settings.mail_carrier(None) is None


@pytest.mark.parametrize("out, rc", [("not json", 0), ('{"kind": 3}', 0), ("[]", 0),
                                     (json.dumps(answer("drop")), 3)],
                         ids=["not-json", "no-kind", "not-an-object", "no-resolver"])
def test_mail_carrier_answers_none_when_sessionmail_gives_no_answer(tmp_path, monkeypatch, out, rc):
    monkeypatch.setattr(settings, "mail_carrier", REAL_MAIL_CARRIER)
    settings.forget_mail_file()
    py = tmp_path / "python3"
    py.write_text(f"#!/bin/sh\nprintf '%s\\n' '{out}'\nexit {rc}\n")
    py.chmod(0o755)
    exe = tmp_path / "message"
    exe.write_text(f"#!{py}\n")
    exe.chmod(0o755)
    assert settings.mail_carrier(str(exe)) is None


# ---- receipts: one rule, one key name, the words ------------------------------------

def _fn(path: pathlib.Path, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(ast.parse(path.read_text()))
                if isinstance(n, ast.FunctionDef) and n.name == name)


def test_only_child_env_sets_must_sign_and_only_by_the_rule():
    """Grep receipt: no second site forces must-sign. The must-sign variable is
    named in seatmail.py alone (nothing in the plist, web/turns.py or the
    spool), and set in child_env alone, under `if signing(...)`."""
    hits = []
    for p in sorted(SRC.rglob("*.py")) + sorted((REPO / "bin").glob("*")):
        if p.is_file():
            for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if "REQUIRE_SIGNATURE" in line:
                    hits.append(f"{p.relative_to(REPO).as_posix()}:{i}")
    assert hits and {h.split(":")[0] for h in hits} == {"src/sessionphone/bridge/seatmail.py"}
    tree = ast.parse((SRC / "bridge/seatmail.py").read_text())
    sets = [(fn.name, n) for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
            for n in ast.walk(fn) if isinstance(n, ast.Assign)
            and any("MUST_SIGN_VARS" in ast.unparse(t) for t in n.targets)]
    assert [name for name, _ in sets] == ["child_env"]
    child_env = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "child_env")
    guard = next(n for n in ast.walk(child_env) if isinstance(n, ast.If) and sets[0][1] in n.body)
    assert ast.unparse(guard.test) == "signing(e)[0] != UNSIGNED"


def test_the_signing_mode_is_decided_in_signing_alone():
    """The mode's readers call seatmail.signing(): the bot, the seat, and the
    web's start line (turns.start_lines, which says the mode). The spool
    retries by seatmail.send_args and the web call's turns by seatmail.send,
    so both build their environment in child_env, under the rule."""
    readers = {p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
               for c in ast.walk(ast.parse(p.read_text())) if isinstance(c, ast.Call)
               and ast.unparse(c.func) in ("signing", "seatmail.signing")}
    assert readers == {"bridge/seatmail.py", "bridge/bot.py", "seat.py", "web/turns.py"}
    assert "seatmail.send_args" in (SRC / "bridge/spool.py").read_text()
    assert "seatmail.send(" in (SRC / "web/turns.py").read_text()
    assert "subprocess" not in (SRC / "web/turns.py").read_text()


def test_the_code_reads_the_sendmail_key_never_carrier():
    assert seatmail.CARRIER_KEY == "sendmail" == settings.MAIL_KEYS["SESSIONMAIL_SENDMAIL"]
    for p in sorted(SRC.rglob("*.py")):
        assert not re.search(r"""["']carrier["']\s*[\]:)]|get\(["']carrier["']""", p.read_text()), p


def test_no_words_say_the_environment_cannot_flip_it():
    for p in [*sorted(SRC.rglob("*.py")), REPO / "README.md", *sorted((REPO / "skills").rglob("*.md"))]:
        assert "nothing in the environment can flip it" not in p.read_text().lower(), p

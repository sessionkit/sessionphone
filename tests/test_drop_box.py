"""On the mail drop: the box before the first send, no Cc that could sink the
mail, sessionmail's warnings once per run, and the words for who hears it.

The drop never makes a box, and ONE recipient it cannot deliver to refuses
the WHOLE mail (rc=3, kind undeliverable), the agent's own copy included. So:

1. the bot makes the agent's box at every start, before its first send
   (seat.have_box), or refuses to start;
2. a Cc goes only to a box sessionmail says is there, and on the drop only on
   this host (seatmail.guard_cc); one left off is said once per run;
3. sessionmail's warnings (the UNSIGNED and MagicDNS notices) reach the bot's
   log once per run, never once per send;
4. the hear words name `claude --agent NAME`: seat mail reaches a session
   only through the doorbell, and a plain `claude` gets no copy.

Fakes only. A fake `message` over a temp mail root behaves as the drop does,
as measured on sessionmail: `path NAME` finds `<root>/NAME`, `setup --box
NAME` makes it, and `send` refuses the whole mail when any recipient has no
box or is off this host, else writes one file into each box and reports the
two notices a keyless send gets.
"""
import json
import os
import pathlib
import sys

import pytest

from sessionphone import seat, settings
from sessionphone.bridge import bot, dc_env, seatmail

HOST = "testhost"

FAKE_MESSAGE = r'''#!{python}
import json, os, pathlib, sys
a = sys.argv[1:]
# as sessionmail before SM-910: `path` and `setup` read only the legacy
# SESSION_MAIL_ROOT; `send` (the drop) reads SESSIONMAIL_ROOT first
legacy = os.environ.get("SESSION_MAIL_ROOT")
fresh = os.environ.get("SESSIONMAIL_ROOT")
root = pathlib.Path((fresh if a[:1] == ["send"] else None) or legacy or os.environ["FAKE_ROOT"])
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(a) + "\n")
if a[:1] == ["path"]:
    box = root / a[1]
    if box.is_dir():
        print(box); sys.exit(0)
    print(f"message path: no box {{a[1]!r}} under {{root}}", file=sys.stderr); sys.exit(2)
if a[:2] == ["setup", "--box"]:
    if os.environ.get("FAKE_SETUP_FAILS"):
        print("setup: FAILED - cannot make it", file=sys.stderr); sys.exit(1)
    for sub in ("new", "cur", "tmp"):
        (root / a[2] / sub).mkdir(parents=True, exist_ok=True)
    print(f"box: {{a[2]}} DONE"); sys.exit(4)        # steps left for the operator, box made
def box_of(local):
    # as the drop: the box LOCAL, else for `base+tag` the box `base`
    exact = root / local
    return exact if exact.is_dir() or "+" not in local else root / local.partition("+")[0]
if a[:1] == ["send"]:
    to = [a[1]] + [a[i + 1] for i, w in enumerate(a) if w == "-c"]
    body = sys.stdin.read()
    for rcpt in to:
        local, _, domain = rcpt.partition("@")
        if domain != "{host}":
            why = f"{{rcpt!r}} is not on this host (the drop delivers locally only)"
        elif not box_of(local).is_dir():
            why = f"{{rcpt!r}}: no mailbox row delivers {{rcpt}} to an existing maildir"
        else:
            continue
        print(json.dumps({{"sent": "no", "kind": "undeliverable", "notices": [],
                          "message": f"REFUSING TO SEND (mail drop) - {{why}}. NOTHING WAS DELIVERED, "
                                     "to this recipient or any other."}}))
        sys.exit(3)
    n = len(list(root.glob("*/new/*")))
    for rcpt in to:
        (box_of(rcpt.partition("@")[0]) / "new" / f"{{n}}.{{rcpt}}").write_text(body)
    notices = ["session-mail-send: sent via mail drop (no MTA)"]
    if not os.environ.get("CLAUDE_SESSION_PUB"):
        notices.insert(0, "session-mail-send: ⚠  sending UNSIGNED - no session key in this environment")
    notices.insert(1, "session-mail-send: ⚠  MagicDNS suffix unknown: set MAGICDNS_SUFFIX")
    print(json.dumps({{"sent": "yes", "id": f"<{{n}}@{host}>", "notices": notices}}))
    sys.exit(0)
sys.exit(1)
'''


class Drop:
    """The fake `message` and its temp mail root. .box(name) makes a box;
    .argvs() lists every call; .mail(name) the files a box holds."""

    def __init__(self, tmp_path: pathlib.Path, monkeypatch):
        self.root = tmp_path / "mail" / "agents"
        self.root.mkdir(parents=True)
        self.log = tmp_path / "calls"
        self.log.write_text("")
        self.exe = tmp_path / "bin" / "message"
        self.exe.parent.mkdir()
        self.exe.write_text(FAKE_MESSAGE.format(python=sys.executable, host=HOST))
        self.exe.chmod(0o755)
        for k in ("DC_SIGNER_SOCK", "DC_SIGNER_PUB", "DC_MAILTO", "DC_HOST", "SESSION_MAIL_HOST",
                  "SESSIONMAIL_ROOT", "SESSION_MAIL_ROOT"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("FAKE_ROOT", str(self.root))
        monkeypatch.setenv("FAKE_LOG", str(self.log))
        monkeypatch.setenv("DC_MESSAGE", str(self.exe))
        monkeypatch.setenv("SESSIONMAIL_HOST", HOST)
        monkeypatch.setenv("SESSIONMAIL_SENDMAIL", "drop")      # the carrier: the mail drop
        monkeypatch.setattr(seatmail, "MESSAGE", str(self.exe))
        seatmail.forget_boxes()

    def box(self, name: str) -> pathlib.Path:
        for sub in ("new", "cur", "tmp"):
            (self.root / name / sub).mkdir(parents=True, exist_ok=True)
        return self.root / name

    def argvs(self) -> list[list[str]]:
        return [json.loads(ln) for ln in self.log.read_text().splitlines()]

    def mail(self, name: str) -> list[pathlib.Path]:
        return sorted((self.root / name / "new").glob("*"))


@pytest.fixture
def drop(tmp_path, monkeypatch):
    d = Drop(tmp_path, monkeypatch)
    yield d
    seatmail.forget_boxes()


# ---- 1. the box before the first send ------------------------------------------

def test_have_box_makes_a_missing_box_before_any_send(drop):
    env = {**os.environ, "DC_MAILTO": f"claude@{HOST}"}
    box = seat.have_box(env, env)
    assert box.path == str(drop.root / "claude") and (drop.root / "claude" / "new").is_dir()
    assert [a[:2] for a in drop.argvs()] == [["path", "claude"], ["setup", "--box"], ["path", "claude"]]
    assert not [a for a in drop.argvs() if a[:1] == ["send"]]


def test_have_box_only_reads_when_the_box_is_there(drop):
    drop.box("claude")
    env = {**os.environ, "DC_MAILTO": f"claude@{HOST}"}
    assert seat.have_box(env, env).path == str(drop.root / "claude")
    assert drop.argvs() == [["path", "claude"]]                 # no setup: nothing written


def test_have_box_off_this_host_asks_nothing_on_an_mta(drop, monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_SENDMAIL", "/usr/sbin/sendmail")
    env = {**os.environ, "DC_MAILTO": "claude@elsewhere.example"}
    box = seat.have_box(env, env)
    assert box.path is None and drop.argvs() == []


def test_have_box_off_this_host_refuses_on_the_drop(drop):
    env = {**os.environ, "DC_MAILTO": "claude@elsewhere.example"}
    with pytest.raises(seat.NoBox) as e:
        seat.have_box(env, env)
    assert "claude@elsewhere.example is not on this host" in str(e.value)
    assert drop.argvs() == []


def test_have_box_that_cannot_be_made_refuses(drop, monkeypatch):
    monkeypatch.setenv("FAKE_SETUP_FAILS", "1")
    env = {**os.environ, "DC_MAILTO": f"claude@{HOST}"}
    with pytest.raises(seat.NoBox) as e:
        seat.have_box(env, env)
    assert "no box `claude` for Delta text" in str(e.value)


class Started(Exception):
    pass


def _start(monkeypatch, tmp_path, lines):
    """bot.main() over stubs up to its `agent box:` line (then Started)."""
    def log(msg):
        lines.append(msg)
        if msg.startswith("agent box:"):
            raise Started(msg)
    monkeypatch.setattr(bot, "log", log)
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(bot.seatmail, "check", lambda *a, **k: None)
    monkeypatch.setattr(bot.seatmail, "setup", lambda *a, **k: "stub")
    monkeypatch.setattr(bot.dc_env, "take_mail_host", lambda: HOST)
    monkeypatch.setattr(bot.dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot, "OUTBOX", tmp_path / "outbox")


def test_the_bot_makes_the_agents_box_at_start_before_its_first_send(drop, monkeypatch, tmp_path):
    """Under the drop nothing else makes the box, and a send to none is rc=3:
    the start makes it, and says who hears it."""
    monkeypatch.setenv("DC_MAILTO", f"claude@{HOST}")
    lines = []
    _start(monkeypatch, tmp_path, lines)
    with pytest.raises(Started):
        bot.main()
    assert (drop.root / "claude" / "new").is_dir()
    assert not [a for a in drop.argvs() if a[:1] == ["send"]]
    said = lines[-1]
    assert said.startswith(f"agent box: Delta text goes to claude@{HOST}, box {drop.root / 'claude'}. ")
    assert "session start claude (it runs `claude --agent claude`" in said
    assert "a plain `claude` gets no copy of its mail, and nothing says so" in said
    assert any(ln.startswith("agent mail: ") and "unsigned (carrier drop" in ln for ln in lines)


def test_the_bot_refuses_to_start_when_the_box_cannot_be_made(drop, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DC_MAILTO", f"claude@{HOST}")
    monkeypatch.setenv("FAKE_SETUP_FAILS", "1")
    _start(monkeypatch, tmp_path, [])
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    assert "sessionphone bot: no box `claude` for Delta text" in capsys.readouterr().err


# ---- 2. no Cc that could sink the whole mail -------------------------------------

def test_drop_a_missing_box_or_an_off_host_cc_is_left_off_and_the_agent_gets_its_mail(drop):
    drop.box("claude")
    drop.box("guest")
    sent = seatmail.send(f"claude@{HOST}", "s", "b",
                         cc=[f"guest@{HOST}", f"nobox@{HOST}", "far@elsewhere.example"])
    assert sent.ok, sent.error
    send = next(a for a in drop.argvs() if a[:1] == ["send"])
    assert [send[i + 1] for i, w in enumerate(send) if w == "-c"] == [f"guest@{HOST}"]
    assert sent.dropped == [
        (f"nobox@{HOST}", "sessionmail has no box `nobox` on this host"),
        ("far@elsewhere.example", "it is not on this host, and the mail drop delivers locally only")]
    assert len(drop.mail("claude")) == 1 and len(drop.mail("guest")) == 1


def test_without_the_guard_one_missing_cc_would_refuse_the_whole_mail(drop):
    """What the guard prevents, on the same fake drop: the agent loses its copy."""
    drop.box("claude")
    sent = seatmail.send_args(seatmail.args(f"claude@{HOST}", "s", cc=[f"nobox@{HOST}"]), "b")
    assert not sent.ok and sent.kind == "undeliverable" and drop.mail("claude") == []


def test_a_plus_address_cc_is_kept_when_its_base_box_is_there(drop):
    """A fleet seat's address is `base+tag@host`. `message path base+tag` says
    no box, but the drop delivers it to the box `base`: the Cc is kept, and
    only a plus address with no base box is left off."""
    drop.box("claude")
    drop.box("proj")
    sent = seatmail.send(f"claude@{HOST}", "s", "b",
                         cc=[f"proj+task.worker@{HOST}", f"gone+x@{HOST}"])
    assert sent.ok, sent.error
    send = next(a for a in drop.argvs() if a[:1] == ["send"])
    assert [send[i + 1] for i, w in enumerate(send) if w == "-c"] == [f"proj+task.worker@{HOST}"]
    assert sent.dropped == [(f"gone+x@{HOST}", "sessionmail has no box `gone+x` on this host")]
    assert len(drop.mail("proj")) == 1 and len(drop.mail("claude")) == 1
    asked = [a[1] for a in drop.argvs() if a[:1] == ["path"]]
    assert asked == ["proj+task.worker", "proj", "gone+x", "gone"]


def test_cc_boxes_is_the_name_then_a_plus_addresss_base():
    assert seatmail.cc_boxes("ada") == ("ada",)
    assert seatmail.cc_boxes("proj+task.worker") == ("proj+task.worker", "proj")
    assert seatmail.cc_boxes("+x") == ("+x",)


def test_mta_an_off_host_cc_is_kept_and_a_missing_local_box_left_off(drop, monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_SENDMAIL", "/usr/sbin/sendmail")
    kept, dropped = seatmail.guard_cc([f"nobox@{HOST}", "far@elsewhere.example"])
    assert kept == ["far@elsewhere.example"]
    assert dropped == [(f"nobox@{HOST}", "sessionmail has no box `nobox` on this host")]


def test_a_box_there_is_asked_once_and_a_missing_one_asked_again_later(drop, monkeypatch):
    drop.box("guest")
    clock = [1000.0]
    monkeypatch.setattr(seatmail.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        seatmail.guard_cc([f"guest@{HOST}", f"late@{HOST}"])
    asked = [a[1] for a in drop.argvs() if a[:1] == ["path"]]
    assert asked == ["guest", "late"]                            # each asked once, for now
    drop.box("late")
    clock[0] += seatmail.BOX_RECHECK_S + 1
    assert seatmail.guard_cc([f"guest@{HOST}", f"late@{HOST}"]) == ([f"guest@{HOST}", f"late@{HOST}"], [])
    assert [a[1] for a in drop.argvs() if a[:1] == ["path"]] == ["guest", "late", "late"]


def test_no_cc_asks_nothing(drop):
    assert seatmail.guard_cc(()) == ([], []) and seatmail.guard_cc(None) == ([], [])
    assert drop.argvs() == []


# ---- SM-910 workaround: SESSIONMAIL_ROOT set, the box check and the drop agree ----

def test_sessionmail_root_set_the_box_check_and_the_drop_agree(drop, tmp_path, monkeypatch):
    """`message path`/`setup` read only SESSION_MAIL_ROOT (SM-910) while the
    drop reads SESSIONMAIL_ROOT: the box tools get both, so the box the start
    makes is the box the send delivers to, and the Cc guard sees it too."""
    other = tmp_path / "other" / "agents"
    other.mkdir(parents=True)
    monkeypatch.setenv("SESSIONMAIL_ROOT", str(other))
    env = {**os.environ, "DC_MAILTO": f"claude@{HOST}"}
    box = seat.have_box(env, env)
    assert box.path == str(other / "claude") and not (drop.root / "claude").exists()
    (other / "guest" / "new").mkdir(parents=True)
    sent = seatmail.send(f"claude@{HOST}", "s", "b", cc=[f"guest@{HOST}"])
    assert sent.ok and sent.dropped == [], sent.error
    assert len(list((other / "claude" / "new").glob("*"))) == 1
    assert len(list((other / "guest" / "new").glob("*"))) == 1


def test_without_the_root_workaround_they_would_disagree(drop, tmp_path, monkeypatch):
    """The control: with no SESSION_MAIL_ROOT mirror, `path` looks in the
    default root while the drop delivers under SESSIONMAIL_ROOT."""
    other = tmp_path / "other" / "agents"
    (other / "claude" / "new").mkdir(parents=True)
    monkeypatch.setenv("SESSIONMAIL_ROOT", str(other))
    monkeypatch.setattr(seatmail, "root_env", dict)
    assert not seatmail.box_there("claude")


def test_root_env_mirrors_only_a_set_root():
    assert seatmail.root_env({"SESSIONMAIL_ROOT": " /r "})["SESSION_MAIL_ROOT"] == "/r"
    assert "SESSION_MAIL_ROOT" not in seatmail.root_env({"SESSIONMAIL_ROOT": ""})
    assert seatmail.root_env({"SESSION_MAIL_ROOT": "/old"}) == {"SESSION_MAIL_ROOT": "/old"}
    assert seat.tool_env({"SESSIONMAIL_ROOT": "/r", "PATH": "/bin"}, "/x/message")["SESSION_MAIL_ROOT"] == "/r"


# ---- 3. sessionmail's warnings, once per run ---------------------------------------

def _mail_in_twice(monkeypatch, cc=None):
    lines = []
    monkeypatch.setattr(bot, "log", lines.append)
    monkeypatch.setattr(bot, "_SAID_ONCE", set())
    monkeypatch.setattr(bot.dc_env, "MAILTO", f"claude@{HOST}")
    for i in range(2):
        assert bot.mail_in("chat", 7, "Ada", f"hello {i}", cc=cc)
    return lines


def test_the_unsigned_and_magicdns_notices_print_once_per_run_not_per_send(drop):
    drop.box("claude")
    with pytest.MonkeyPatch.context() as m:
        lines = _mail_in_twice(m)
    unsigned = [ln for ln in lines if "sending UNSIGNED" in ln]
    magic = [ln for ln in lines if "MagicDNS" in ln]
    assert len(unsigned) == 1 and unsigned[0].startswith("sessionmail says (once per run): ")
    assert len(magic) == 1
    assert len([ln for ln in lines if ln.startswith("bridged to mail:")]) == 2
    assert not [ln for ln in lines if "sent via mail drop" in ln]     # not a warning: never logged
    assert len(drop.mail("claude")) == 2


def test_a_cc_left_off_is_said_once_per_run(drop):
    drop.box("claude")
    with pytest.MonkeyPatch.context() as m:
        lines = _mail_in_twice(m, cc=[f"nobox@{HOST}"])
    off = [ln for ln in lines if ln.startswith(f"Cc nobox@{HOST} left off agent mail")]
    assert len(off) == 1 and "sessionmail has no box `nobox` on this host" in off[0]
    assert len(drop.mail("claude")) == 2


def test_say_once_logs_a_key_only_the_first_time(monkeypatch):
    lines = []
    monkeypatch.setattr(bot, "log", lines.append)
    monkeypatch.setattr(bot, "_SAID_ONCE", set())
    assert bot.say_once("k", "one") and not bot.say_once("k", "two")
    assert lines == ["one"]


# ---- 4. the words: who hears the seat box --------------------------------------------

def test_the_hear_words_state_the_claude_agent_condition():
    words = seat.hear_words("ada")
    assert words.startswith("To hear Delta messages, start Claude with: session start ada ")
    assert "`claude --agent ada`" in words
    assert "a plain `claude` gets no copy of its mail, and nothing says so" in words


def test_seatmail_on_host_is_the_drops_judgement():
    env = {"SESSIONMAIL_HOST": HOST}
    assert seatmail.on_host(f"a@{HOST}", env) and seatmail.on_host(f"a@{HOST}.tail.example", env)
    assert not seatmail.on_host("a@elsewhere.example", env) and not seatmail.on_host(f"a@x{HOST}", env)
    assert seat.on_host(f"a@{HOST}", env)                       # seat asks the same judgement


def test_settings_carrier_is_the_suites_stub():
    """The suite never asks a real sessionmail: the drop here is SESSIONMAIL_SENDMAIL's."""
    assert settings.mail_carrier("/x", "drop")["kind"] == "drop"
    assert dc_env.mail_host({"SESSIONMAIL_HOST": HOST}) == HOST


# ---- 5. `sessionphone web --legs`: the seat's box before the first turn ----------
#
# The web's turns go by the same `message send` (web/turns.py, TurnMail.send), to
# the seat's To. Under the drop a turn to a box that is not there is refused, so
# every turn of the call would be lost: the web start makes the box, as the bot's
# does, or refuses loudly, naming it.

def test_have_box_for_a_given_mailto_makes_that_box_not_the_bots(drop):
    env = {**os.environ, "DC_MAILTO": f"claude@{HOST}"}
    box = seat.have_box(env, env, f"alice@{HOST}")
    assert box.name == "alice" and box.path == str(drop.root / "alice")
    assert not (drop.root / "claude").exists()


def test_have_box_a_plus_address_whose_base_is_there_makes_nothing(drop):
    """As the drop delivers `base+tag` to the box `base` (cc_boxes)."""
    drop.box("alice")
    env = dict(os.environ)
    box = seat.have_box(env, env, f"alice+web@{HOST}")
    assert box.path == str(drop.root / "alice")
    assert drop.argvs() == [["path", "alice+web"], ["path", "alice"]]       # read only: nothing made


def test_the_words_of_a_box_that_cannot_be_made_name_what_it_is_for(drop, monkeypatch):
    monkeypatch.setenv("FAKE_SETUP_FAILS", "1")
    env = dict(os.environ)
    with pytest.raises(seat.NoBox) as e:
        seat.have_box(env, env, f"alice@{HOST}", what="the web call's turns",
                      again="`sessionphone web --legs` again")
    said = str(e.value)
    assert said.startswith("no box `alice` for the web call's turns: `message setup --box alice` did not")
    assert said.endswith("run it yourself, then `sessionphone web --legs` again.")


@pytest.fixture
def web_root():
    """A scratch root with a SHORT path (a Unix socket path is capped at 104 bytes)."""
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp(prefix="spb-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _web_legs(drop, web_root, monkeypatch, tmp_path):
    """server.run with --legs, the seat alice@HOST, on the fake drop -> (rc, lines).
    seatmail.check is stubbed: its --message-id probe is the web suite's to test
    (tests/web/test_call.py), and this fake does not answer it."""
    import asyncio

    from sessionphone.web import server as srvmod
    for k in ("DC_AGENT", "PIP_CHAT_AGENT", "PIP_CHAT_MAILTO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DC_HOST", HOST)
    monkeypatch.setattr(seatmail, "check", lambda *a, **k: None)
    monkeypatch.setattr(srvmod.WebServer, "_own_origins", lambda self: {f"http://localhost:{self.port}"})
    out = []
    rc = srvmod.run(web_root / "web", port=0, host="127.0.0.1", tls=None, origins=(), legs=True,
                    explicit_root=True, mail_to=f"alice@{HOST}", heard_dir=tmp_path / "conversations",
                    echo=out.append, until=lambda: asyncio.sleep(0))
    return rc, out


def test_web_legs_makes_the_seats_missing_box_at_start_and_a_turn_lands(drop, web_root, monkeypatch, tmp_path):
    from sessionphone.web import turns
    rc, out = _web_legs(drop, web_root, monkeypatch, tmp_path)
    assert rc == 0, out
    assert (drop.root / "alice" / "new").is_dir()
    assert [a[:2] for a in drop.argvs()] == [["path", "alice"], ["setup", "--box"], ["path", "alice"]]
    assert f"  turns box: {drop.root / 'alice'}" in out
    assert any(m.startswith("  turns: ") and m.endswith(
        ", unsigned (carrier drop: this bot writes into the box itself, from SESSIONMAIL_SENDMAIL))")
        for m in out), out
    # a real turn, by the web's own send, lands in the box the start made
    turn = turns.TurnMail(to=f"alice@{HOST}", subject="[web 1] Ada: hello", headers=(("X-Web-Call", "1"),),
                          msgid=f"call-turn-web-1-1@{HOST}", thread=f"web-call-1@{HOST}", seat="alice-chat",
                          from_name="Alice", body="hello")
    sent = turn.send()
    assert sent.ok, sent.error
    assert len(drop.mail("alice")) == 1


def test_web_legs_refuses_loudly_at_start_when_the_box_cannot_be_made(drop, web_root, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_SETUP_FAILS", "1")
    rc, out = _web_legs(drop, web_root, monkeypatch, tmp_path)
    assert rc == 2
    (why,) = out                                                  # refused before anything was served
    assert why.startswith("sessionphone web: no box `alice` for the web call's turns")
    assert why.endswith("then `sessionphone web --legs` again.")
    assert not list(web_root.rglob("*.sock"))
    assert not [a for a in drop.argvs() if a[:1] == ["send"]]


def test_without_the_box_a_web_turn_is_refused_on_the_drop(drop):
    """The control: what the start's box step prevents. No box, and the turn is
    refused whole (rc=3, undeliverable): every turn of the call would be lost."""
    from sessionphone.web import turns
    turn = turns.TurnMail(to=f"alice@{HOST}", subject="[web 1] Ada: hello", headers=(),
                          msgid=f"call-turn-web-1-1@{HOST}", thread=f"web-call-1@{HOST}", seat="alice-chat",
                          from_name="Alice", body="hello")
    sent = turn.send()
    assert not sent.ok and sent.kind == "undeliverable"

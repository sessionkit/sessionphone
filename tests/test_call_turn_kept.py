"""PHONE-249 repro-001: a refused call turn is thrown away, and the caller hears nothing.

Case 162 (a fresh Mac): every turn of two calls was refused by the mail drop and
dropped, and the caller talked into silence for minutes. Three gaps, each pinned
here by a test that FAILS on origin/master 5aefa77 and passes once fixed:

(a) D8: bot.mail_in with a call mail (M5, a bot-minted msgid) whose seatmail send
    fails logs "BRIDGE MAIL FAILED ...; a call mail, not kept" and returns: nothing
    goes to the undelivered spool (bot.py, the `elif m5:` branch). Any other bridge
    mail is kept there and retried.
(b) D5: when a turn's mail is refused, the call says nothing to the caller;
    CallSession._sent only logs `TURN <n> NOT mailed ..., taken=no`.
(c) the waiting count: dc-live's `undelivered mail to the agent: N waiting` and
    the bot's start line (SPOOL.counts) never count a refused call turn, and
    `sessionphone bot status` has no undelivered line at all.

The duplicate check before a retry (the target box read for the Message-ID) is
designed in rca-001 and tested in fix-001; it has no code to fail against yet.
The spool and dc-live already agree on <DC_ROOT>/undelivered
(tests/test_spool.py::test_the_default_dir_is_under_dc_root), so (c) is (a)
for dc-live and the start line: a kept call turn is counted where it is kept.
Each test below asserts the behaviour the fix must give, and is a STRICT xfail
on master: it fails today with an AssertionError naming its defect, and an
unexpected pass (the defect fixed) turns the suite red until the fix removes the
marker. fix-001 removed D8 and COUNT, fix-002 removed D5: they all pass now.

The tests that pin today's opposite rule go with fix-001's change, not here:
tests/test_seatmail.py::test_an_m5_refusal_is_never_spooled,
test_the_delta_call_path_makes_one_try (its SPOOL.counts() == (0, 0)), and
tests/test_spool.py::test_a_call_mail_is_not_spooled.
Fakes only: conftest's fake `message`, a scratch spool, no call, no network.
"""
import asyncio
import json
import os
import pathlib
import re
import subprocess
import types

import pytest
from click.testing import CliRunner

from sessionphone import cli as cli_mod
from sessionphone.bridge import bot, calls, dc_env

REPO = pathlib.Path(__file__).resolve().parents[1]
SEAT = "ada-chat"
BOX = "ada@example.net"
TURN_ID = "call-turn-12-1@host"
ROOT_ID = "call-12-1@host"


# the refusals a mail drop gives that heal (spool.RETRIED): the Air's were of these
HEALS = [
    (2, {"sent": "no", "kind": "identity", "message": "cannot be signed"}),
    (3, {"sent": "no", "kind": "undeliverable", "message": "no such box"}),
    (3, {"sent": "unknown", "kind": "mta", "message": "sendmail exited 75"}),
]


@pytest.fixture
def seat(monkeypatch, seat_key):
    """The bot is seat ada-chat (as tests/test_seatmail.py)."""
    monkeypatch.setattr(dc_env, "ADDR", f"{SEAT}@example.net")
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Ada")
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    monkeypatch.setattr(bot, "PROFILE", "")
    seat_key(f"{SEAT} seat")


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(bot, "log", lines.append)
    return lines


def _refused_turn(fake_message, reply):
    """One call turn (M5) whose send is refused with REPLY. -> mail_in's answer, refusal list."""
    fake_message.answer(reply)
    why = []
    ok = bot.mail_in("Team", 12, "Caller", "(on the call) can you check the build",
                     msgid=TURN_ID, in_reply_to=ROOT_ID, refusal=why)
    return ok, why


# ---- (a) D8: the refused call turn is kept, with its Message-ID ---------------

@pytest.mark.parametrize("reply", HEALS, ids=[r[1]["kind"] for r in HEALS])
def test_a_refused_call_turn_is_kept_in_the_spool(fake_message, seat, logged, undelivered_in_tmp, reply):
    ok, why = _refused_turn(fake_message, reply)
    assert ok is False and why                        # still refused, and the call path is told
    assert bot.SPOOL.counts() == (1, 0), logged       # failed on 5aefa77: (0, 0), the drop


@pytest.mark.parametrize("reply", HEALS[:1], ids=["identity"])
def test_the_kept_call_turn_carries_its_own_message_id(fake_message, seat, logged, undelivered_in_tmp, reply):
    """A retry must go under the SAME id, so the duplicate check can find it in the box."""
    _refused_turn(fake_message, reply)
    kept = sorted(undelivered_in_tmp.glob("*.json"))
    assert len(kept) == 1, logged                     # failed on 5aefa77: nothing kept
    args = json.loads(kept[0].read_text())["args"]
    i = args.index("--message-id")
    assert args[i + 1] == TURN_ID
    assert "-r" in args and args[args.index("-r") + 1] == f"<{ROOT_ID}>"


def test_the_log_no_longer_says_the_call_mail_is_dropped(fake_message, seat, logged, undelivered_in_tmp):
    _refused_turn(fake_message, HEALS[0])
    assert not [ln for ln in logged if ln.endswith("a call mail, not kept")], logged


# ---- (b) D5: the caller is told, once, that the turn did not get through -------

class _NoSink:
    def turn(self, *a, **k):
        pass

    def partial(self, *a, **k):
        pass

    def saying(self, *a, **k):
        pass

    def said(self, *a, **k):
        pass


async def _quiet():
    return {"n": 1, "failed": 0, "error": None, "cut": False}


def _live_session(said):
    """A CallSession on a live call (object.__new__, as tests/bridge/test_call_turn_log.py).
    speak() records the line AT THE CALL, so a fire-and-forget speak still counts."""
    s = object.__new__(calls.CallSession)
    s.chat_id, s.chat_name, s.caller = 12, "Caller", "Caller"
    s.lines, s.preview_id, s.awaiting = [], None, ()
    s.root_id, s.msg_id = ROOT_ID, 5531
    s.guest_list = list
    s.heard = _NoSink()
    s.pc, s.voicemail, s.closed, s.speaking = object(), False, False, False
    s.noreply_task = None

    def speak(text, voice=None, who=None, fallback=True):
        said.append(text)
        return _quiet()
    s.speak = speak
    return s


@pytest.fixture
def refusing_call(monkeypatch):
    """calls' mail hook refuses every turn, as bot.mail_in does on a refused send."""
    r = types.SimpleNamespace(logs=[], said=[])

    def mail_in(*a, **k):
        if k.get("refusal") is not None:
            k["refusal"].append("rc=3 kind=undeliverable: no such box - NOTHING WAS SENT")
        return False
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: "", mail_in))
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    monkeypatch.setattr(calls, "_call_id", None)
    return r


def test_a_refused_turn_is_said_on_the_call(refusing_call):
    s = _live_session(refusing_call.said)
    asyncio.run(s._mail_turn("can you check the build"))
    assert any("taken=no" in ln for ln in refusing_call.logs)      # logged, as today
    assert len(refusing_call.said) == 1, refusing_call.logs        # failed on 5aefa77: nothing said


def test_the_line_is_the_undelivered_line_said_as_the_bridge(refusing_call):
    s = _live_session(refusing_call.said)
    who = []
    speak = s.speak
    s.speak = lambda text, voice=None, w=None, fallback=True: (who.append(w), speak(text))[1]
    asyncio.run(s._mail_turn("can you check the build"))
    assert refusing_call.said == [calls.UNDELIVERED_LINE] == ["I couldn't reach the agent."]
    assert who == ["bridge"]
    assert [ln for ln in refusing_call.logs if ln.startswith("CALL UNDELIVERED")] == [
        f"CALL UNDELIVERED box={BOX} turn=1: said on the call"]


def test_a_turn_of_many_chunks_is_said_once(refusing_call):
    """The chunks join into ONE turn mail (_turn_out): one line, never one per chunk."""
    s = _live_session(refusing_call.said)
    s.turn_parts = [(0.0, "can you"), (1.0, "check"), (2.0, "the build")]
    s.previews = types.SimpleNamespace(on_turn=lambda *a: None)
    asyncio.run(s._turn_out())
    assert refusing_call.said == [calls.UNDELIVERED_LINE]
    assert sum("taken=no" in ln for ln in refusing_call.logs) == 1


def test_each_undelivered_turn_is_said_once(refusing_call):
    """Once per undelivered turn (the foreman's D5 ruling): two refused turns, two lines."""
    s = _live_session(refusing_call.said)
    asyncio.run(s._mail_turn("one"))
    asyncio.run(s._mail_turn("two"))
    assert refusing_call.said == [calls.UNDELIVERED_LINE] * 2


def test_a_mailed_turn_says_nothing(monkeypatch):
    said = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, lambda p: "", lambda *a, **k: True))
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    monkeypatch.setattr(calls, "NO_REPLY_S", 0)
    s = _live_session(said)
    asyncio.run(s._mail_turn("can you check the build"))
    assert said == []


@pytest.mark.parametrize("state", ["closed", "no pc"])
def test_a_closing_call_says_nothing(refusing_call, state):
    s = _live_session(refusing_call.said)
    if state == "closed":
        s.closed = "hung up"
    else:
        s.pc = None
    asyncio.run(s._mail_turn("bye"))
    assert any("taken=no" in ln for ln in refusing_call.logs) and refusing_call.said == []


def test_voicemail_is_told_too(refusing_call):
    s = _live_session(refusing_call.said)
    s.voicemail = True
    asyncio.run(s._mail_turn("call me back"))
    assert refusing_call.said == [calls.UNDELIVERED_LINE]


def test_a_hook_that_raises_is_said_too(monkeypatch):
    """_not_sent: the mail hook raised, so the turn did not go either."""
    r = types.SimpleNamespace(logs=[], said=[])

    def boom(*a, **k):
        raise OSError("no message here")
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: "", boom))
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    s = _live_session(r.said)

    async def run():
        with pytest.raises(OSError):
            await s._mail_turn("can you check the build")
    asyncio.run(run())
    assert any("OSError: no message here, taken=no" in ln for ln in r.logs)
    assert r.said == [calls.UNDELIVERED_LINE]


def test_a_late_refusal_is_said_once(refusing_call):
    """A send whose waiter was cancelled lands later (_sent_late): still one line."""
    s = _live_session(refusing_call.said)

    async def run():
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(([], False, ["rc=3 kind=undeliverable: no such box"]))
        s._sent_late(1, TURN_ID, fut)
    asyncio.run(run())
    assert refusing_call.said == [calls.UNDELIVERED_LINE]


def test_no_loop_says_nothing_and_does_not_raise(refusing_call):
    s = _live_session(refusing_call.said)
    s._sent(1, TURN_ID, [], False, ["refused"])
    assert refusing_call.said == []


# ---- (c) the waiting count includes call turns -------------------------------

def _dc_live_env(tmp_path, undelivered):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_", "SESSIONPHONE_"))}
    env.update(HOME=str(tmp_path / "home"), DC_ROOT=str(tmp_path / "root"),
               DC_UNDELIVERED=str(undelivered), DC_AGENT="nobody-call-turn-kept-test")
    return env


def test_dc_live_counts_a_refused_call_turn(fake_message, seat, logged, undelivered_in_tmp, tmp_path):
    _refused_turn(fake_message, HEALS[0])
    r = subprocess.run([str(REPO / "bin" / "dc-live")], env=_dc_live_env(tmp_path, undelivered_in_tmp),
                       capture_output=True, text=True, check=False)
    # failed on 5aefa77: nothing kept, so dc-live says nothing about undelivered mail
    assert re.search(r"undelivered[^\n]*\b1\b[^\n]*waiting", r.stderr), r.stderr


def test_the_bots_start_line_counts_a_refused_call_turn(fake_message, seat, logged, undelivered_in_tmp):
    """bot.main()'s `undelivered mail: N waiting, ...` is SPOOL.counts()."""
    _refused_turn(fake_message, HEALS[0])
    waiting, _dead = bot.SPOOL.counts()
    assert waiting == 1                               # failed on 5aefa77: 0


def test_bot_status_says_what_waits(monkeypatch, tmp_path):
    """`sessionphone bot status` says what waits in the bot's spool, the one its
    installed job names (service.undelivered_dir, faked here)."""
    spool_dir = tmp_path / "bots-undelivered"
    spool_dir.mkdir()
    (spool_dir / "1-1.json").write_text("{}")
    monkeypatch.setattr(cli_mod.service, "undelivered_dir", lambda name=None: spool_dir)
    monkeypatch.setattr(cli_mod, "_which_bot", lambda verb: ("ada", None))
    monkeypatch.setattr(cli_mod.service, "status", lambda name=None: {
        "label": "test.sessionphone.ada", "installed": False, "legacy": None,
        "pid": None, "last_exit": None, "plist": str(tmp_path / "none.plist")})
    monkeypatch.setattr(cli_mod.service, "last_log", lambda name=None: (tmp_path / "bot.log", None, False))
    st = CliRunner().invoke(cli_mod.main, ["bot", "status"])
    assert re.search(r"undelivered[^\n]*\b1\b[^\n]*waiting", st.output), st.output   # failed on 5aefa77


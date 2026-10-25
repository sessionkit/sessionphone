"""The no-reply watchdog.

A caller's turn was mailed, nobody answered, and he hung up after 45 s of
silence. With DC_NO_REPLY_S set above 0, a turn with no reply inside that
window hears the holding line ONCE, and the log says
`CALL NO-REPLY box=<box> turn=<n>`. The watchdog is OFF by default
(DC_NO_REPLY_S defaults to 0), so every test here that needs it
sets the window explicitly.
A reply inside the window means neither; a reply after it still plays.
The real CallSession methods run on object.__new__ sessions, with the window
at 0.05 s and speak() faked to record. No call, no network, no TTS.
"""
import asyncio
import os
import re
import subprocess
import sys
import threading
import types

import pytest

from sessionphone.bridge import calls, dc_env, earleg

BOX = "agent@example.org"
W = 0.05          # the window under test
SETTLE = 0.2      # comfortably past it


class _NoSink:
    def turn(self, *a, **k):
        pass

    def partial(self, *a, **k):
        pass


@pytest.fixture
def rig(monkeypatch):
    r = types.SimpleNamespace(logs=[], spoken=[], done=[], send_gate={})

    def mail_in(*a, **k):
        gate = r.send_gate.get(a[3].removeprefix("(on the call) "))
        if gate:
            gate.wait(5)                # a slow send, released by the test
        return True
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, None, mail_in))
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    monkeypatch.setattr(calls, "_call_id", None)
    monkeypatch.setattr(calls, "NO_REPLY_S", W)
    r.noreply = lambda: [m for m in r.logs if m.startswith("CALL NO-REPLY ")]
    r.verdicts = lambda: [m for m in r.logs if re.match(r"CALL TURN \d+ taken=", m)]
    return r


def _session(rig, speak_s=0.0):
    s = object.__new__(calls.CallSession)
    s.chat_id, s.chat_name, s.caller = 12, "Caller", "Caller"
    s.lines, s.preview_id = [], None
    s.root_id = "call-12-1789@example.org"
    s.guest_list = list
    s.heard = _NoSink()
    s.pc = object()                     # live

    async def speak(text, voice=None, who=None):
        rig.spoken.append((text, who))
        await asyncio.sleep(speak_s)
        rig.done.append(text)
    s.speak = speak
    return s


def run(coro):
    return asyncio.run(coro)


# ---- (a) no reply: the holding line, once, and the NO-REPLY line ----

def test_a_no_reply_speaks_the_holding_line_once_and_logs_it(rig):
    s = _session(rig)

    async def go():
        await s._mail_turn("can you check the build")
        await asyncio.sleep(SETTLE)
        await asyncio.sleep(SETTLE)      # a second window passes: still once
    run(go())
    assert rig.spoken == [(calls.HOLDING_LINE, "bridge")]
    assert rig.noreply() == [f"CALL NO-REPLY box={BOX} turn=1 after={W:g}s: holding line spoken"]
    assert [t[0] for t in s.awaiting] == [1]       # still pending: a late reply can resolve it


def test_the_holding_line_is_the_bridges_and_never_mailed(rig):
    s = _session(rig)
    mailed = []
    rig_mail = calls._hooks.mail_in
    calls._hooks.mail_in = lambda *a, **k: mailed.append(a[3]) or rig_mail(*a, **k)

    async def go():
        await s._mail_turn("hello")
        await asyncio.sleep(SETTLE)
    run(go())
    assert rig.spoken == [(calls.HOLDING_LINE, "bridge")]
    assert mailed == ["(on the call) hello"]


# ---- (b) a reply inside the window: neither ----

def test_b_a_reply_inside_the_window_means_no_holding_line_and_no_log(rig):
    s = _session(rig)

    async def go():
        await s._mail_turn("hello")
        s.replied("mouth")
        await asyncio.sleep(SETTLE)
    run(go())
    assert rig.spoken == [] and rig.noreply() == []
    assert rig.verdicts()[0].startswith("CALL TURN 1 taken=replied via=mouth after ")


# ---- (c) a late reply still plays, resolves the turn, and cancels nothing ----

def test_c_a_reply_after_the_window_is_logged_late_and_the_holding_line_finishes(rig):
    s = _session(rig, speak_s=0.1)       # the holding line takes 0.1 s to say

    async def go():
        await s._mail_turn("hello")
        await asyncio.sleep(W + 0.03)    # the watchdog fired; the line is mid-TTS
        assert rig.spoken and not rig.done
        s.replied("dc-say")              # the late reply
        await asyncio.sleep(SETTLE)
    run(go())
    assert rig.done == [calls.HOLDING_LINE]      # the reply did not cut it off
    v, = rig.verdicts()
    after = float(re.fullmatch(r"CALL TURN 1 taken=replied via=dc-say after (\S+)s", v).group(1))
    assert after >= W                             # a reader sees it was late


# ---- (d) voicemail: no holding line ----

def test_d_voicemail_gets_no_holding_line(rig):
    s = _session(rig)
    s.voicemail = True

    async def go():
        await s._mail_turn("leaving a message")
        await asyncio.sleep(SETTLE)
    run(go())
    assert rig.spoken == [] and rig.noreply() == []
    assert "taken=pending" in rig.logs[-1]       # the box line still stands


# ---- (e) two quick turns: one holding line ----

def test_e_two_quick_turns_mean_one_holding_line_for_the_newer(rig):
    s = _session(rig)

    async def go():
        await s._mail_turn("one")
        await s._mail_turn("two")
        await asyncio.sleep(SETTLE)
    run(go())
    assert rig.spoken == [(calls.HOLDING_LINE, "bridge")]
    assert rig.noreply() == [f"CALL NO-REPLY box={BOX} turn=2 after={W:g}s: holding line spoken"]
    assert rig.verdicts() == ["CALL TURN 1 taken=unknown: no reply before the next turn"]
    assert [t[0] for t in s.awaiting] == [2]


# ---- (f) turn 1's send lands after turn 2's: turn 2's timer stands ----

def test_f_an_older_send_landing_late_never_cancels_the_newer_timer(rig):
    s = _session(rig)
    slow = rig.send_gate["one"] = threading.Event()

    async def go():
        a = asyncio.ensure_future(s._mail_turn("one"))
        await asyncio.sleep(0)
        await s._mail_turn("two")        # turn 2 is mailed and arms
        slow.set()                       # now turn 1's send lands, late
        await a
        await asyncio.sleep(SETTLE)
    run(go())
    mailed = [m.split(" mailed")[0] for m in rig.logs if " mailed to " in m]
    assert mailed == ["CALL TURN 2", "CALL TURN 1"]
    assert rig.noreply() == [f"CALL NO-REPLY box={BOX} turn=2 after={W:g}s: holding line spoken"]
    assert len(rig.spoken) == 1
    assert rig.verdicts() == ["CALL TURN 1 taken=unknown: no reply before the next turn"]


# ---- review note 3: a reply while turn n is still sending ----

def test_a_reply_during_a_send_resolves_only_the_older_turn(rig):
    # The reply answers turn 1 (or anything before turn 2 was mailed); turn 2
    # was not yet mailed, so nobody has replied to it: it arms and may speak.
    s = _session(rig)
    slow = rig.send_gate["two"] = threading.Event()

    async def go():
        await s._mail_turn("one")
        b = asyncio.ensure_future(s._mail_turn("two"))
        await asyncio.sleep(0.01)        # turn 2 is in its send
        s.replied("mail")
        slow.set()
        await b
        await asyncio.sleep(SETTLE)
    run(go())
    assert [v.split(" after")[0] for v in rig.verdicts()] == ["CALL TURN 1 taken=replied via=mail"]
    assert rig.noreply() == [f"CALL NO-REPLY box={BOX} turn=2 after={W:g}s: holding line spoken"]


# ---- the caller is talking: log only ----

def test_a_caller_already_talking_hears_no_holding_line(rig):
    s = _session(rig)

    async def go():
        await s._mail_turn("one")
        s.speaking = True                # his next turn is forming
        await asyncio.sleep(SETTLE)
    run(go())
    assert rig.spoken == []
    assert rig.noreply() == [f"CALL NO-REPLY box={BOX} turn=1 after={W:g}s: caller talking, no holding line"]


# ---- the rules that keep it off ----

def test_a_window_of_zero_turns_the_watchdog_off(rig, monkeypatch):
    monkeypatch.setattr(calls, "NO_REPLY_S", 0)
    s = _session(rig)

    async def go():
        await s._mail_turn("hello")
        await asyncio.sleep(SETTLE)
    run(go())
    assert rig.spoken == [] and rig.noreply() == [] and s.noreply_task is None
    assert "taken=pending" in rig.logs[-1]       # the taken= lines stay


def test_a_refused_send_arms_nothing(rig):
    s = _session(rig)
    calls._hooks.mail_in = lambda *a, **k: False

    async def go():
        await s._mail_turn("hello")
        await asyncio.sleep(SETTLE)
    run(go())
    # no watchdog and no holding line: the caller hears the undelivered line instead (PHONE-249 D5)
    assert rig.spoken == [(calls.UNDELIVERED_LINE, "bridge")] and s.noreply_task is None
    assert calls.HOLDING_LINE not in [t for t, _ in rig.spoken]


def test_a_turn_cancelled_mid_send_still_gets_its_watchdog(rig):
    # the join timer is cancelled while the send is in flight (do-001 F1):
    # the send lands through _sent_late, and that path arms too
    s = _session(rig)
    slow = rig.send_gate["one"] = threading.Event()

    async def go():
        t = asyncio.ensure_future(s._mail_turn("one"))
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        slow.set()
        await asyncio.sleep(SETTLE)
    run(go())
    assert rig.noreply() == [f"CALL NO-REPLY box={BOX} turn=1 after={W:g}s: holding line spoken"]


def test_hang_up_cancels_the_watchdog(rig, monkeypatch):
    s = _session(rig)

    class PC:
        async def close(self):
            pass
    s.pc, s.msg_id, s.answer_go, s.closed = PC(), 5531, None, None
    s.turn_pending, s.turn_parts, s.tasks, s.turn_timer = set(), [], [], None
    s.connected, s.previews = False, calls.Previews()
    s.manager = types.SimpleNamespace(forget=lambda x: None)
    s._live_clear = lambda: None
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(earleg, "end", lambda *a: None)

    async def go():
        await s._mail_turn("bye")
        task = s.noreply_task
        await s.close("hung up")
        await asyncio.sleep(SETTLE)
        return task
    task = run(go())
    assert task.cancelled() and s.noreply_task is None
    assert rig.spoken == [] and rig.noreply() == []
    assert rig.verdicts() == ["CALL TURN 1 taken=unknown: no reply before hang-up"]


# ---- the knobs: dc_env, the printout, the start line ----

def _dc_env(**env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("DC_")}
    e.update(env)
    return subprocess.run([sys.executable, "-m", "sessionphone.bridge.dc_env"],
                          env=e, capture_output=True, text=True, timeout=30, check=True)


def test_the_window_defaults_to_0_off_and_shows_in_the_settings_printout(tmp_path):
    out = _dc_env(DC_ROOT=str(tmp_path)).stdout
    assert re.search(r"^NO_REPLY_S\s+0\.0$", out, re.MULTILINE), out


def test_a_window_set_in_the_env_turns_the_watchdog_on(tmp_path):
    out = _dc_env(DC_ROOT=str(tmp_path), DC_NO_REPLY_S="8").stdout
    assert re.search(r"^NO_REPLY_S\s+8\.0$", out, re.MULTILINE), out


def test_regression_unset_env_means_no_holding_line(tmp_path):
    # With DC_NO_REPLY_S unset, the watchdog is off. The real
    # modules load in a clean process (no DC_* at all, nothing monkeypatched),
    # and the real CallManager.start logs the off line.
    code = "\n".join([
        "import types",
        "from sessionphone.bridge import calls, dc_env, earleg, mouthleg",
        "logs = []",
        "calls._hooks = calls.Hooks(logs.append, None, None)",
        "calls._call_id = None",
        "calls.MailLeg = lambda m: types.SimpleNamespace(start=lambda: None)",
        "earleg.start = lambda *a: None",
        "mouthleg.start = lambda *a: None",
        "mgr = object.__new__(calls.CallManager)",
        "mgr.thread = types.SimpleNamespace(start=lambda: None)",
        "mgr.start()",
        "print(repr(dc_env.NO_REPLY_S)); print(repr(calls.NO_REPLY_S))",
        "print([m for m in logs if 'no-reply watchdog' in m])",
    ])
    e = {k: v for k, v in os.environ.items() if not k.startswith("DC_")}
    e.update(DC_ROOT=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], env=e, capture_output=True,
                         text=True, timeout=30, check=True).stdout.splitlines()
    assert out[0] == "0.0" and out[1] == "0.0", out
    assert out[2] == "['CALL no-reply watchdog: off']", out


def test_a_window_that_is_not_a_number_falls_back_and_says_so(tmp_path):
    code = ("from sessionphone.bridge import dc_env as d; "
            "print(d.NO_REPLY_S); print(d.BAD); print(d.HOLDING_LINE)")
    e = {k: v for k, v in os.environ.items() if not k.startswith("DC_")}
    e.update(DC_ROOT=str(tmp_path), DC_NO_REPLY_S="soon", DC_HOLDING_LINE="One moment.")
    out = subprocess.run([sys.executable, "-c", code], env=e, capture_output=True,
                         text=True, timeout=30, check=True).stdout.splitlines()
    assert out[0] == "0.0"
    assert "DC_NO_REPLY_S='soon' is not a number; using 0" in out[1]
    assert out[2] == "One moment."


@pytest.mark.parametrize("window,line", [(8.0, "CALL no-reply watchdog: 8s"),
                                         (0, "CALL no-reply watchdog: off")])
def test_the_start_line_says_the_window(monkeypatch, window, line):
    logs = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logs.append, None, None))
    monkeypatch.setattr(calls, "_call_id", None)
    monkeypatch.setattr(calls, "NO_REPLY_S", window)
    monkeypatch.setattr(calls, "MailLeg", lambda m: types.SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(earleg, "start", lambda *a: None)
    from sessionphone.bridge import mouthleg
    monkeypatch.setattr(mouthleg, "start", lambda *a: None)
    mgr = object.__new__(calls.CallManager)
    mgr.thread = types.SimpleNamespace(start=lambda: None)
    mgr.start()
    i = next(i for i, m in enumerate(logs) if m.startswith("CALL incoming calls answer: "))
    assert logs[i + 1] == line

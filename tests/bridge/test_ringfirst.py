"""Ring first, answer second.

DC_ANSWER=auto answers at once, as before. DC_ANSWER=ring mails the agent
RINGING and holds the call - BEFORE any peer connection exists, so no ICE
check is sent into an unanswered call - until
dc-answer, and after RING_WAIT seconds answers as voicemail. A call that
ends or is declined while it rings is never accepted, and is mailed MISSED.
"""
import asyncio
import threading
import types

import pytest

from sessionphone.bridge import calls, dc_env


class FakePC:
    localDescription = types.SimpleNamespace(sdp="v=0\na=candidate:1 1 udp 1 10.0.0.1 9 typ host\n")

    async def setRemoteDescription(self, _):
        pass

    async def createAnswer(self):
        return object()

    async def setLocalDescription(self, _):
        pass


class Rig:
    """One incoming CallSession with the network faked out, and what it did."""

    def __init__(self, monkeypatch, tmp_path, ring, wait=5.0):
        self.logged, self.mailed, self.accepted, self.spoken = [], [], [], []
        self.order, self.forgot, self.ended = [], [], []
        monkeypatch.setattr(calls, "log", self.logged.append)
        monkeypatch.setattr(calls, "RING", ring)
        monkeypatch.setattr(calls, "RING_WAIT", wait)
        monkeypatch.setattr(calls, "_hooks", types.SimpleNamespace(
            mail_in=lambda *a, **k: self.mailed.append((a, k))))
        msg = types.SimpleNamespace(accept_incoming_call=self.accepted.append,
                                    end_call=lambda: self.ended.append(True))
        s = object.__new__(calls.CallSession)
        s.account = types.SimpleNamespace(get_message_by_id=lambda _id: msg)
        s.manager = types.SimpleNamespace(forget=self.forgot.append)
        s.chat_id, s.msg_id, s.offer_sdp = 12, 7, "v=0\n"
        s.chat_name, s.caller, s.has_video = "Bob Team", "Bob Team", False
        s.root_id, s.dir, s.pc = "call-12-1@test", tmp_path / "call", None

        async def names():
            pass

        async def build_pc():
            self.order.append("build_pc")
            s.pc = FakePC()
            return s.pc
        s._names, s._build_pc = names, build_pc
        self.s = s

    def subjects(self):
        return [k.get("subject", "") for _a, k in self.mailed]

    async def ringing(self):
        """Wait (briefly) until the RINGING mail has gone out."""
        for _ in range(200):
            if self.mailed:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no RINGING mail")


def run(coro):
    return asyncio.run(coro)


def pick(s):
    """What CallManager.answer does on the calls loop."""
    s.picked = True
    s.answer_go.set()


def test_auto_answers_at_once_and_mails_nothing(monkeypatch, tmp_path):
    r = Rig(monkeypatch, tmp_path, ring=False)
    run(r.s.run())
    assert r.accepted and not r.mailed and not r.s.voicemail
    assert r.s.answer_go is None


def test_ring_holds_with_no_peer_connection_until_dc_answer(monkeypatch, tmp_path):
    r = Rig(monkeypatch, tmp_path, ring=True, wait=5.0)

    async def go():
        t = asyncio.ensure_future(r.s.run())
        await r.ringing()
        await asyncio.sleep(0.05)
        assert not r.accepted, "accepted before anyone picked up"
        # no pc, so no ICE is running into an unanswered call,
        # and nothing treats a ringing call as live
        assert r.s.pc is None and r.order == [] and r.s.ringing
        r.order.append("picked")
        pick(r.s)
        await asyncio.wait_for(t, 2)
    run(go())
    assert r.order == ["picked", "build_pc"]
    assert r.accepted and not r.s.voicemail and not r.s.ringing
    (args, kw), = r.mailed
    assert "RINGING" in kw["subject"] and "dc-answer 12" in kw["subject"]
    assert "dc-answer 12" in args[3] and "dc-hangup 12" in args[3]
    assert any(line.startswith("PICKED UP msg 7") for line in r.logged)


def test_no_pick_up_goes_to_voicemail(monkeypatch, tmp_path):
    r = Rig(monkeypatch, tmp_path, ring=True, wait=0.2)
    run(r.s.run())
    assert r.accepted and r.s.voicemail
    assert any(line.startswith("VOICEMAIL msg 7") for line in r.logged)
    assert not r.s.ringing, "a late dc-answer must find nothing ringing"


def test_an_event_set_without_a_pick_is_not_a_pick_up(monkeypatch, tmp_path):
    r = Rig(monkeypatch, tmp_path, ring=True, wait=5.0)

    async def go():
        t = asyncio.ensure_future(r.s.run())
        await r.ringing()
        r.s.answer_go.set()                       # woken, but nobody picked
        await asyncio.wait_for(t, 2)
    run(go())
    assert r.s.voicemail


def test_a_caller_who_hangs_up_while_ringing_is_never_accepted(monkeypatch, tmp_path):
    r = Rig(monkeypatch, tmp_path, ring=True, wait=30.0)

    async def go():
        t = asyncio.ensure_future(r.s.run())
        await r.ringing()
        await r.s.close("CallEnded event")        # the real close(): no pc yet
        await asyncio.wait_for(t, 1)              # a 30 s ring, woken at once
        await asyncio.sleep(0.05)                 # the MISSED mail is in the executor
    run(go())
    assert not r.accepted and not r.s.voicemail and r.order == []
    assert any("CallEnded event while ringing" in line for line in r.logged)
    assert r.forgot == [r.s]
    assert any(sub.startswith("MISSED Bob Team: CallEnded event") for sub in r.subjects())


def test_close_before_the_pc_exists_is_honoured_in_auto_too(monkeypatch, tmp_path):
    r = Rig(monkeypatch, tmp_path, ring=False)
    closes = []

    async def stub_close(why):
        closes.append(why)
        r.s.pc = None

    async def build_pc():
        await r.s.close("CallEnded event")        # the real close(): it ends before the pc exists
        r.s.close = stub_close                    # the rest of close() is not under test
        r.s.pc = FakePC()
        return r.s.pc
    r.s._build_pc = build_pc
    run(r.s.run())
    assert not r.accepted
    assert closes == ["CallEnded event"], "the pc made after the end must be closed"
    assert any("NOT ACCEPTING msg 7" in line for line in r.logged)


class Manager:
    """A CallManager with a real calls loop in a thread, as the bot runs it."""

    def __init__(self, monkeypatch, sessions):
        self.logged = []
        monkeypatch.setattr(calls, "log", self.logged.append)
        self.m = object.__new__(calls.CallManager)
        self.m.sessions = sessions
        self.m.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.m.loop.run_forever, daemon=True)
        self.t.start()

    def on_loop(self, fn):
        async def go():
            return fn()
        return asyncio.run_coroutine_threadsafe(go(), self.m.loop).result(2)

    def stop(self):
        self.m.loop.call_soon_threadsafe(self.m.loop.stop)
        self.t.join(2)


def ringing_session(mgr, chat, msg):
    s = object.__new__(calls.CallSession)
    s.chat_id, s.msg_id, s.pc = chat, msg, None
    s.ringing = True
    s.answer_go = mgr.on_loop(asyncio.Event)
    return s


def test_dc_answer_picks_up_only_what_is_ringing_and_says_so(monkeypatch):
    mgr = Manager(monkeypatch, {})
    try:
        a, b = ringing_session(mgr, 12, 7), ringing_session(mgr, 13, 8)
        mgr.m.sessions.update({7: a, 8: b})
        ok, why = mgr.m.answer(12)
        assert ok and "picked up msg 7 in chat 12" in why
        assert a.picked and a.answer_go.is_set() and not b.picked and not b.answer_go.is_set()
        ok, why = mgr.m.answer(12)                # a second dc-answer: already picked
        assert not ok and "nothing ringing" in why
    finally:
        mgr.stop()


def test_a_late_dc_answer_is_told_no(monkeypatch):
    mgr = Manager(monkeypatch, {})
    try:
        s = ringing_session(mgr, 12, 7)
        s.ringing = False                         # voicemail took it
        s.pc = object()                           # and it is live now
        mgr.m.sessions[7] = s
        ok, why = mgr.m.answer(12)
        assert not ok and not s.picked
        assert "nothing ringing in chat 12" in why and "dc-say" in why
    finally:
        mgr.stop()


def test_dc_hangup_declines_a_ringing_call(monkeypatch):
    mgr = Manager(monkeypatch, {})
    try:
        s = ringing_session(mgr, 12, 7)
        ended = []
        s.account = types.SimpleNamespace(get_message_by_id=lambda _id: types.SimpleNamespace(
            end_call=lambda: ended.append(True)))
        mgr.m.sessions[7] = s
        assert mgr.m.hangup(12) is True
        for _ in range(100):
            if s.closed:
                break
            threading.Event().wait(0.01)
        assert ended and s.closed == f"declined by {dc_env.AGENT_NAME}" and s.answer_go.is_set()
        assert any(line == "DECLINED msg 7" for line in mgr.logged)
    finally:
        mgr.stop()


def test_a_bad_ring_wait_never_stops_the_bot(monkeypatch):
    monkeypatch.setenv("DC_RING_WAIT", "abc")
    monkeypatch.setattr(dc_env, "BAD", [])
    assert dc_env.number("RING_WAIT", 25.0) == 25.0
    assert dc_env.BAD == ["DC_RING_WAIT='abc' is not a number; using 25"]
    monkeypatch.setenv("DC_RING_WAIT", "40")
    assert dc_env.number("RING_WAIT", 25.0) == 40.0


def test_voicemail_greets_as_voicemail(monkeypatch, tmp_path):
    for vm, want in ((True, calls.GREETING_VOICEMAIL), (False, calls.GREETING)):
        r = Rig(monkeypatch, tmp_path, ring=True)
        s = r.s
        s.voicemail, s.outgoing, s.greeting, s.greet_voice = vm, False, None, None
        s._live_write = lambda: None
        s.guest_list = lambda: []
        s.manager = types.SimpleNamespace(loop=types.SimpleNamespace(
            run_in_executor=lambda _ex, f: f()))
        monkeypatch.setattr(calls, "_recorder", object())
        monkeypatch.setattr(calls, "set_call_id", lambda _id: None)

        async def speak(text, voice=None, who=None, r=r, **_kw):
            r.spoken.append(text)
        s.speak = speak
        run(s._on_connected())
        assert r.spoken == [want]
        (args, _kw), = r.mailed
        assert ("VOICEMAIL" in args[3]) is vm


# ---- the re-review's edges, turned into asserts


def test_a_call_that_ends_before_it_rings_never_rings(monkeypatch, tmp_path):
    """CallEnded lands while _names() awaits RPC, before _ring() starts."""
    r = Rig(monkeypatch, tmp_path, ring=True, wait=5.0)

    async def names():
        await r.s.close("CallEnded event")
    r.s._names = names

    async def go():
        t = asyncio.get_running_loop().time()
        await asyncio.wait_for(r.s.run(), 1)      # never the 5 s ring
        await asyncio.sleep(0.05)
        return asyncio.get_running_loop().time() - t
    took = run(go())
    assert took < 1 and not r.accepted and not r.s.ringing
    subs = r.subjects()
    assert not any(sub.startswith("RINGING") for sub in subs)
    assert any(sub.startswith("MISSED Bob Team: CallEnded event") for sub in subs)


def test_dc_answer_never_picks_up_a_closed_call(monkeypatch):
    mgr = Manager(monkeypatch, {})
    try:
        s = ringing_session(mgr, 12, 7)
        s.closed = "CallEnded event"              # ended, the ring not yet woken
        mgr.m.sessions[7] = s
        ok, why = mgr.m.answer(12)
        assert not ok and not s.picked and "nothing ringing" in why
    finally:
        mgr.stop()


def test_an_end_after_pick_up_during_ice_still_mails_missed(monkeypatch, tmp_path):
    """Picked up, then the caller hangs up while the answer is gathered."""
    r = Rig(monkeypatch, tmp_path, ring=True, wait=5.0)
    s = r.s

    class SlowPC(FakePC):
        async def setLocalDescription(self, _):
            await asyncio.sleep(0.2)

        async def close(self):
            pass

    async def build_pc():
        s.pc = SlowPC()
        return s.pc
    s._build_pc = build_pc
    for name in ("_live_clear", "_turn_cancel"):
        setattr(s, name, lambda: None)
    s.tasks, s.turn_pending, s.turn_parts, s.connected = [], [], [], False
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda *a: None)

    async def go():
        task = asyncio.ensure_future(s.run())
        await r.ringing()
        pick(s)
        await asyncio.sleep(0.1)                  # inside setLocalDescription
        try:
            await s.close("CallEnded event")
        except Exception:
            pass                                  # the transcript half of close() is not under test
        await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.05)
    run(go())
    assert not r.accepted
    missed = [k for _a, k in r.mailed if k.get("subject", "").startswith("MISSED")]
    assert missed and "while connecting" in missed[0]["subject"]


def test_missed_threads_under_its_ringing_mail(monkeypatch, tmp_path):
    r = Rig(monkeypatch, tmp_path, ring=True, wait=30.0)

    async def go():
        t = asyncio.ensure_future(r.s.run())
        await r.ringing()
        await r.s.close("CallEnded event")
        await asyncio.wait_for(t, 1)
        await asyncio.sleep(0.05)
    run(go())
    ring = next(k for _a, k in r.mailed if k["subject"].startswith("RINGING"))
    missed = next(k for _a, k in r.mailed if k["subject"].startswith("MISSED"))
    assert ring["msgid"] == f"ring-{r.s.root_id}" and ring["in_reply_to"] == r.s.root_id
    assert missed["in_reply_to"] == ring["msgid"]

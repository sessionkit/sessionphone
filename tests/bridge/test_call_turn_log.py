"""PHONE-114 R1+R2 (SC1): each call turn logs the box it was mailed to, and
then exactly one `taken=` verdict the bridge measured, never one it guessed.

A caller's turn was heard and mailed, nobody took it, and the log could not
say where it went. Now `CALL TURN <n> mailed to <box>: msg <id>, taken=pending`
names the box, and a reply line arriving in the call (mouth, dc-say or mail)
upgrades it to `taken=replied`. A turn still waiting at hang-up is `unknown`.
The real CallSession methods run on sessions made with object.__new__, as in
test_medium_header. No call, no network, no mail.
"""
import asyncio
import json
import pathlib
import re
import struct
import tempfile
import types

import pytest

from sessionphone.bridge import bot, calls, dc_env, mouthleg

BOX = "agent@example.org"


class _NoSink:
    def turn(self, *a, **k):
        pass

    def partial(self, *a, **k):
        pass

    def saying(self, *a, **k):
        pass

    def said(self, *a, **k):
        pass


def _session(guests=()):
    s = object.__new__(calls.CallSession)
    s.chat_id, s.chat_name, s.caller = 12, "Caller", "Caller"
    s.lines, s.preview_id = [], None
    s.root_id = "call-12-1789@example.org"
    s.guest_list = lambda: list(guests)
    s.heard = _NoSink()
    return s


@pytest.fixture
def rig(monkeypatch):
    r = types.SimpleNamespace(logs=[], mails=[], ok=True, why=None)

    def mail_in(*a, **k):
        r.mails.append((a, k))
        if isinstance(r.ok, BaseException):
            raise r.ok
        if r.ok is False and r.why and k.get("refusal") is not None:
            k["refusal"].append(r.why)          # as bot.mail_in says a refused send
        return r.ok
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: "", mail_in))
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    monkeypatch.setattr(calls, "_call_id", None)
    r.turns = lambda: [m for m in r.logs if re.match(r"CALL TURN \d", m)]
    return r


def turn(s, text="can you check the build"):
    asyncio.run(s._mail_turn(text))


# ---- R1: the box line ----

def test_a_mailed_turn_names_its_box_and_its_message_id(rig):
    s = _session()
    turn(s)
    (_, kw), = rig.mails
    line, = rig.turns()
    assert re.fullmatch(
        rf"CALL TURN 1 mailed to {re.escape(BOX)}: msg (call-turn-12-\d+@\S+), taken=pending", line)
    assert line.endswith(f"msg {kw['msgid']}, taken=pending")   # the id that went out
    assert [t[0] for t in s.awaiting] == [1]


def test_the_turn_id_keeps_the_mail_legs_chat_fallback_working(rig):
    # MailLeg falls back to the CHAT when a guest replies to a turn, not the root
    s = _session()
    turn(s)
    (_, kw), = rig.mails
    assert re.findall(r'call-(?:[a-z]+-)?(\d+)-\d+@', kw["msgid"]) == ["12"]


def test_guests_on_the_call_are_named_as_cc(rig):
    turn(_session(guests=["alice@example.org", "carol@example.org"]))
    line, = rig.turns()
    assert line.startswith(f"CALL TURN 1 mailed to {BOX} cc alice@example.org,carol@example.org: msg ")


def test_turns_are_numbered_from_one_within_a_call(rig):
    s = _session()
    turn(s, "one")
    turn(s, "two")
    assert [m.split(" mailed")[0] for m in rig.turns()] == ["CALL TURN 1", "CALL TURN 2"]


def test_a_hook_that_returns_none_reads_as_sent(rig):
    rig.ok = None                       # every older fake returns None
    turn(_session())
    assert "taken=pending" in rig.turns()[0]


# ---- R2: the hard `no` is a refused send ----

def test_a_refused_send_is_not_mailed_and_taken_no(rig):
    rig.ok = False
    s = _session()
    turn(s)
    assert rig.turns() == [f"CALL TURN 1 NOT mailed to {BOX}: refused, taken=no"]
    assert s.awaiting == ()


def test_a_refused_send_logs_the_refusal_itself(rig):
    """PHONE-72 part 2b: the line says message send's refusal, never "sendmail failed"."""
    rig.ok, rig.why = False, "rc=2 kind=identity: cannot be signed - NOTHING WAS SENT"
    turn(_session())
    assert rig.turns() == [f"CALL TURN 1 NOT mailed to {BOX}: rc=2 kind=identity: cannot be signed - "
                           "NOTHING WAS SENT, taken=no"]


def test_a_send_that_raises_is_logged_then_raised(rig):
    rig.ok = OSError("no sendmail")
    s = _session()
    with pytest.raises(OSError):
        turn(s)
    assert rig.turns() == [f"CALL TURN 1 NOT mailed to {BOX}: OSError: no sendmail, taken=no"]
    assert s.awaiting == ()


def test_bot_mail_in_returns_whether_message_send_accepted(monkeypatch):
    monkeypatch.setattr(bot, "log", lambda m: None)
    for rc, want in ((0, True), (75, False)):
        done = types.SimpleNamespace(returncode=rc, stderr="")
        monkeypatch.setattr(bot.subprocess, "run", lambda *a, _done=done, **k: _done)
        assert bot.mail_in("Caller", 12, "Caller", "(on the call) hi") is want


# ---- R2: replied, unknown ----

def test_a_reply_resolves_the_turn_as_replied(rig):
    s = _session()
    turn(s)
    s.replied("mouth")
    assert re.fullmatch(r"CALL TURN 1 taken=replied via=mouth after \d+\.\ds", rig.turns()[1])
    assert s.awaiting == ()


def test_one_reply_resolves_every_awaiting_turn_without_guessing(rig):
    s = _session()
    turn(s, "one")
    turn(s, "two")
    s.replied("dc-say")
    got = [m.split(" after")[0] for m in rig.turns()[2:]]
    assert got == ["CALL TURN 1 taken=replied via=dc-say", "CALL TURN 2 taken=replied via=dc-say"]


def test_a_reply_with_nothing_awaiting_logs_nothing(rig):
    s = _session()
    s.replied("mail")
    assert rig.turns() == []


def test_after_is_measured_from_the_mailed_line(rig):
    s = _session()
    turn(s)
    n, mid, t = s.awaiting[0]
    s.awaiting = ((n, mid, t - 9.0),)   # as if mailed 9 s ago
    s.replied("mail")
    assert rig.turns()[1] == "CALL TURN 1 taken=replied via=mail after 9.0s"


def _closable(s, monkeypatch):
    class PC:
        async def close(self):
            pass
    s.pc, s.msg_id, s.answer_go, s.closed = PC(), 5531, None, None
    s.turn_pending, s.turn_parts, s.tasks, s.turn_timer = set(), [], [], None
    s.connected, s.previews = False, calls.Previews()
    s.manager = types.SimpleNamespace(forget=lambda x: None)
    s._live_clear = lambda: None
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda *a: None)


def test_a_turn_still_waiting_at_hang_up_is_unknown_not_no(rig, monkeypatch):
    s = _session()
    turn(s)
    _closable(s, monkeypatch)
    asyncio.run(s.close("hung up"))
    assert rig.turns()[-1] == "CALL TURN 1 taken=unknown: no reply before hang-up"
    assert s.awaiting == ()


def test_the_last_words_at_hang_up_get_a_verdict_too(rig, monkeypatch):
    s = _session()
    _closable(s, monkeypatch)
    s.turn_parts = [(1, "bye then")]       # his last words, mailed inside close()
    asyncio.run(s.close("hung up"))
    t = rig.turns()
    assert t[0].startswith(f"CALL TURN 1 mailed to {BOX}: msg ")
    assert t[1] == "CALL TURN 1 taken=unknown: no reply before hang-up"


def test_a_replied_turn_gets_no_second_verdict_at_hang_up(rig, monkeypatch):
    s = _session()
    turn(s)
    s.replied("mouth")
    _closable(s, monkeypatch)
    asyncio.run(s.close("hung up"))
    assert not any("taken=unknown" in m for m in rig.turns())


# ---- numbering at entry: concurrent ears sends that finish out of order ----

def test_turns_keep_speech_order_when_sends_finish_out_of_order(rig, monkeypatch):
    s = _session()
    import threading
    first_may_return = threading.Event()

    def mail_in(*a, **k):
        if "one" in a[3]:
            first_may_return.wait(5)    # turn 1's send is slow
        else:
            first_may_return.set()      # turn 2's returns first, then lets 1 go
        return True
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(rig.logs.append, None, mail_in))

    async def both():
        a = asyncio.ensure_future(s._mail_turn("one"))
        await asyncio.sleep(0)
        b = asyncio.ensure_future(s._mail_turn("two"))
        await asyncio.gather(a, b)
    asyncio.run(both())
    mailed = [m for m in rig.turns() if "mailed to" in m]
    assert [m.split(" mailed")[0] for m in mailed] == ["CALL TURN 2", "CALL TURN 1"]
    assert [t[0] for t in s.awaiting] == [1, 2]          # kept sorted by n


def test_awaiting_is_never_shared_between_sessions(rig):
    a, b = _session(), _session()
    turn(a)
    assert a.awaiting and b.awaiting == () and calls.CallSession.awaiting == ()


# ---- the hook sites: who calls replied(), and who never does ----

class _Replies:
    def __init__(self):
        self.vias = []

    def __call__(self, via):
        self.vias.append(via)


def test_dc_say_into_a_live_call_counts_as_a_reply_before_it_is_spoken(monkeypatch):
    order = []
    s = types.SimpleNamespace(chat_id=12, pc=object(), replied=lambda via: None,
                              speak=lambda *a: order.append("speak"))
    loop = types.SimpleNamespace(call_soon_threadsafe=lambda fn, *a: order.append(("replied",) + a))
    mgr = types.SimpleNamespace(live_for_chat=lambda c: [s], loop=loop)
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", lambda coro, lp: coro)
    assert calls.CallManager.speak_if_live(mgr, 12, "on it")
    assert order == [("replied", "dc-say"), "speak"]


def test_mail_into_a_live_call_counts_as_a_reply(monkeypatch, tmp_path):
    vias = []
    s = object.__new__(calls.CallSession)
    s.pc, s.root_id, s.chat_id = object(), "call-12-1789@example.org", 12
    s.account = None
    s.speak = lambda *a: None
    loop = types.SimpleNamespace(call_soon_threadsafe=lambda fn, via: vias.append((fn, via)))
    lg = object.__new__(calls.MailLeg)
    lg.manager = types.SimpleNamespace(sessions={1: s}, loop=loop)
    lg.seen = lambda f: None
    logs = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logs.append, None, None))
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", lambda coro, lp: None)
    monkeypatch.setattr(calls, "post_to_chat", lambda *a, **k: None)
    monkeypatch.setattr(calls, "VOICE_MAP_FILE", tmp_path / "voices.json")
    m = tmp_path / "m.eml"
    m.write_text("From: Alice <alice@example.org>\nTo: carol-chat@example.org\nSubject: re\n"
                 "In-Reply-To: <call-12-1789@example.org>\n\nOn it.\n")
    lg.one(m)
    assert [(fn.__func__, via) for fn, via in vias] == [(calls.CallSession.replied, "mail")]


def test_the_bridges_own_mail_is_not_a_reply(monkeypatch, tmp_path):
    vias = []
    s = object.__new__(calls.CallSession)
    s.pc, s.root_id, s.chat_id = object(), "call-12-1789@example.org", 12
    lg = object.__new__(calls.MailLeg)
    lg.manager = types.SimpleNamespace(
        sessions={1: s}, loop=types.SimpleNamespace(call_soon_threadsafe=lambda *a: vias.append(a)))
    lg.seen = lambda f: None
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, None, None))
    m = tmp_path / "m.eml"
    m.write_text(f"From: Bridge <{dc_env.ADDR}>\nTo: x@example.org\nSubject: t\n"
                 "In-Reply-To: <call-12-1789@example.org>\n\nturn text\n")
    lg.one(m)
    assert vias == []


def test_a_broken_loop_never_stops_the_reply(monkeypatch):
    logs = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logs.append, None, None))
    s = object.__new__(calls.CallSession)
    calls.reply_seen(None, s, "mail")     # loop=None, as some rigs have it
    assert logs == ["CALL reply not counted (via=mail): AttributeError"]


class _MouthSession:
    def __init__(self):
        self.pc, self.lines = object(), []
        self.replied = _Replies()
        self.voice = calls.PipVoiceTrack()      # mouth lines and sounds, as on a call
        self.echo = types.SimpleNamespace(spoken=lambda *a, **k: None, cut=lambda *a, **k: "cut")


async def _mouth(sess, header, frames):
    path = pathlib.Path(tempfile.mkdtemp(prefix="tl-")) / "s.sock"
    server = await mouthleg.serve(types.SimpleNamespace(sessions={1: sess}), path, lambda m: None)
    r, w = await asyncio.open_unix_connection(str(path))
    w.write((json.dumps(header) + "\n").encode())
    first = json.loads(await r.readline())
    if first.get("ok") and "probe" not in header:
        for f in frames:
            w.write(f)
        await w.drain()
        await r.readline()
    server.close()
    return first


def _frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


def test_a_mouth_line_counts_as_a_reply():
    s = _MouthSession()
    first = asyncio.run(_mouth(s, {"call": 1, "text": "On it."},
                               [_frame(b"A", b"\0\0" * 4800), _frame(b"D")]))
    assert first["ok"] and s.replied.vias == ["mouth"]


@pytest.mark.parametrize("header", [{"probe": True}, {"call": 77, "text": "hi"},
                                    {"call": 1, "rate": 24000}])
def test_a_mouth_probe_or_refusal_is_not_a_reply(header):
    s = _MouthSession()
    asyncio.run(_mouth(s, header, []))
    assert s.replied.vias == []


def test_a_mouth_sound_is_not_a_reply():
    s = _MouthSession()
    asyncio.run(_mouth(s, {"call": 1, "sound": True, "name": "chime"},
                       [_frame(b"A", b"\0\0" * 4800), _frame(b"D")]))
    assert s.replied.vias == []


def test_the_bridges_own_speech_is_never_a_reply(rig, monkeypatch):
    # the greeting, the invite notice and (R3) the holding line all go through
    # speak(); replied() is called only at the three reply sites above
    s = _session()
    turn(s)
    s.voice = calls.PipVoiceTrack()
    s.echo = types.SimpleNamespace(spoken=lambda *a, **k: None)
    monkeypatch.setattr(calls, "tts_pcm48", lambda text, voice: b"\0\0" * 480)
    s.replied = lambda via: pytest.fail(f"speak() called replied({via})")
    asyncio.run(s.speak("Hello, you have reached the bridge.", who="bridge"))
    assert len(s.awaiting) == 1       # still waiting: speaking is not a reply


# ---- a cancel mid-send (he started again, or hang-up) must not lose the box line ----

def test_a_turn_timer_cancelled_mid_send_still_logs_its_box(rig, monkeypatch):
    # _turn_cancel() runs on every chunk close and every speech start. If the
    # join timer has already fired and is inside the send, the cancel lands on
    # the await: the mail still goes (the executor thread runs on), so its box
    # line and its verdict must still be written when the send returns.
    import threading
    s = _session()
    sending, release = threading.Event(), threading.Event()

    def mail_in(*a, **k):
        sending.set()
        release.wait(5)
        return True
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(rig.logs.append, None, mail_in))

    async def go():
        t = asyncio.ensure_future(s._mail_turn("one"))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sending.wait, 5)
        t.cancel()                       # he started speaking again
        with pytest.raises(asyncio.CancelledError):
            await t
        release.set()
        for _ in range(100):             # let the send land
            if rig.turns():
                break
            await asyncio.sleep(0.01)
    asyncio.run(go())
    line, = rig.turns()
    assert line.startswith(f"CALL TURN 1 mailed to {BOX}: msg call-turn-12-")
    assert [t[0] for t in s.awaiting] == [1]


def test_a_send_that_lands_after_hang_up_gets_its_verdict(rig, monkeypatch):
    import threading
    s = _session()
    s.closed = "hung up"                 # close() ran while the send was in flight
    sending, release = threading.Event(), threading.Event()

    def mail_in(*a, **k):
        sending.set()
        release.wait(5)
        return True
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(rig.logs.append, None, mail_in))

    async def go():
        t = asyncio.ensure_future(s._mail_turn("bye"))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sending.wait, 5)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        release.set()
        for _ in range(100):
            if len(rig.turns()) >= 2:
                break
            await asyncio.sleep(0.01)
    asyncio.run(go())
    assert rig.turns()[1:] == ["CALL TURN 1 taken=unknown: no reply before hang-up"]
    assert s.awaiting == ()

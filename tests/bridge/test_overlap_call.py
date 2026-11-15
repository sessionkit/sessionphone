"""Two calls on one chat: the newest is THE live call (PHONE-297).

Mike rang from a second device while call 52 was still held open (its audio
had stopped two minutes in; it closed 136 minutes later). The new call was
msg 62. Then:

- its setup lines were logged as [call 52]: the call-id stamp was ONE module
  global, set at a call's open and cleared at any call's close;
- call-live.json said 62 (last writer wins), but live_call_id said 52 (the
  OLDEST live session), so `dc-say --call 62` was refused: "call 62 is over;
  the live call is msg 52";
- a plain `dc-say` was spoken into BOTH sessions: speak_if_live spoke into
  every live session in the chat, so the line played twice.

The rules these tests pin:

- the newest call in a chat supersedes the older ones: live_call_id,
  speak_if_live, invite and call-live.json agree on it;
- `dc-say --call NEW` is sent and spoken once, into NEW; `dc-say --call OLD`
  is refused, and nothing is sent, rendered or spoken;
- a call's log lines carry its own id from its first line, and an old call's
  close neither unstamps the new call nor clears its call-live.json;
- a call with no audio from the caller for LIVE_SILENT_S no longer counts as
  live. It is not ended here: ending it is PHONE-299's.

The outbox rig runs one pass of bot.main()'s outbox loop over fakes, built
like the rig in tests/test_say_tts_truth.py (copied, not imported). The
calls side is real: a CallManager with a running loop, and CallSession.speak
on a real PipVoiceTrack; only the TTS render is a fake, which counts renders.
"""
import asyncio
import json
import threading
import time
import types

import pytest

from sessionphone.bridge import bot, calls

OWNER = "alice@relay.example"
LIST = f"family  chat:12   # the family group\nowner   {OWNER}   # owner\n"
CHAT = 12
OLD, NEW = 52, 62           # the incident's ids: 52 held open, 62 the new call


class Done(Exception):
    """Ends main()'s loop at its first sleep: one pass of the outbox."""


class BotTime:
    """bot.py's `time`: sleep raises Done; every other caller keeps the real module."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, s):
        raise Done


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeChat:
    def __init__(self, account, cid):
        self.account, self.id = account, cid

    def get_basic_snapshot(self):
        return Obj(chat_type="Group")

    def get_contacts(self):
        return []

    def send_text(self, text):
        self.account.sent.append((self.id, text))
        return Obj(id=77)


class FakeAccount:
    def __init__(self):
        self.sent = []
        self.chats = {CHAT: FakeChat(self, CHAT)}
        self.ended = []

    def is_configured(self):
        return True

    def start_io(self):
        pass

    def get_config(self, key):
        return "bot@test"

    def get_contacts(self):
        return []

    def get_contact_by_addr(self, addr):
        return None

    def get_chat_by_id(self, cid):
        if cid not in self.chats:
            raise ValueError(f"no chat {cid}")
        return self.chats[cid]

    def get_message_by_id(self, n):
        return Obj(end_call=lambda: self.ended.append(n))


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def call_session(msg_id, spoken, manager=None):
    """A call that is up in chat 12: a peer connection, the real voice track,
    the real speak(). SPOKEN gets (msg_id, text) for each line it is asked to say."""
    s = object.__new__(calls.CallSession)
    s.chat_id, s.msg_id, s.pc = CHAT, msg_id, object()
    s.manager = manager
    s.lines = []
    s.voice = calls.PipVoiceTrack()
    s.replied = lambda via: None        # turn-log bookkeeping, not under test
    s.heard = Obj(saying=lambda *a, **k: None, said=lambda *a, **k: None)
    s.echo = Obj(spoken=lambda *a, **k: None)
    real = calls.CallSession.speak

    async def speak(text, voice=None, who=None, **kw):
        spoken.append((s.msg_id, text))
        return await real(s, text, voice, who, **kw)
    s.speak = speak
    return s


class Rig:
    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig()
    r.out, r.fallback = tmp_path / "outbox", tmp_path / "fallback"
    r.logs, r.account = [], FakeAccount()
    stop = threading.Event()
    lists = tmp_path / "channels.txt"
    lists.write_text(LIST)
    lists.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(lists))
    monkeypatch.setenv("DC_OUTBOX", str(r.fallback))
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(bot.dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "OUTBOX", r.out)
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "DeltaChat", lambda rpc: Obj(get_all_accounts=lambda: [r.account]))
    monkeypatch.setattr(bot, "server_choice", lambda: (None, "package", "test"))
    monkeypatch.setattr(bot, "sweep", lambda account: None)
    monkeypatch.setattr(bot, "handle_events", lambda account: stop.wait())
    monkeypatch.setattr(bot, "alive_or_exit", lambda t, dog: None)
    monkeypatch.setattr(bot, "INVITE", None)
    monkeypatch.setattr(bot, "log", r.logs.append)
    monkeypatch.setattr(bot, "time", BotTime())
    monkeypatch.setattr(bot, "_SPEECH_PENDING", [], raising=False)
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: None)
    monkeypatch.setattr(bot, "CALLS", True)
    monkeypatch.setattr(bot, "calls", calls)
    monkeypatch.setattr(calls, "setup", lambda *a, **k: None)
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: p, lambda *a, **k: None))
    monkeypatch.setattr(calls, "_call_id", None)
    r.rendered = []

    def tts(text, voice=None):
        r.rendered.append(text)
        return b"\0\0" * 4800
    monkeypatch.setattr(calls, "tts_pcm48", tts)

    manager = calls.CallManager()
    r.spoken = []
    # call 52 came first and is still held; call 62 came in after it
    manager.sessions[OLD] = call_session(OLD, r.spoken, manager)
    manager.sessions[NEW] = call_session(NEW, r.spoken, manager)
    r.manager = manager
    loop = threading.Thread(target=manager.loop.run_forever, name="calls-test", daemon=True)
    loop.start()
    monkeypatch.setattr(calls, "speak_if_live", manager.speak_if_live)
    monkeypatch.setattr(calls, "live_call_id", manager.live_call_id)

    def run(job):
        r.out.mkdir(parents=True, exist_ok=True)
        ack = tmp_path / "acks" / "00.json"
        (r.out / "00.json").write_text(json.dumps(dict(job, ack=str(ack))))
        try:
            with pytest.raises(Done):
                bot.main()
        finally:
            stop.set()
        return json.loads(ack.read_text())
    r.run = run
    yield r
    manager.loop.call_soon_threadsafe(manager.loop.stop)
    loop.join(5)
    if not manager.loop.is_running():
        manager.loop.close()


# ---- the outbox: dc-say into two overlapping calls ---------------------------

def test_dc_say_call_new_is_sent_and_spoken_once_into_the_new_call(rig) -> None:
    ack = rig.run({"chat_id": CHAT, "text": "I hear you on Delta.", "call_id": NEW})
    assert ack["ok"] is True, ack
    assert ack["detail"].endswith("; spoken into the call"), ack["detail"]
    assert rig.account.sent == [(CHAT, "I hear you on Delta.")]
    assert rig.spoken == [(NEW, "I hear you on Delta.")], "spoken into the new call only, once"
    assert rig.rendered == ["I hear you on Delta."], "one render: the line played once"


def test_dc_say_call_old_is_refused_and_nothing_is_sent_or_spoken(rig) -> None:
    ack = rig.run({"chat_id": CHAT, "text": "I hear you on Delta.", "call_id": OLD})
    assert ack["ok"] is False, ack
    assert ack["detail"] == f"call {OLD} is over; the live call is msg {NEW}", ack["detail"]
    assert rig.account.sent == [], "a refused dc-say sends no text"
    assert rig.spoken == [], "a refused dc-say is spoken into no call"


def test_a_refused_dc_say_produces_no_audio(rig) -> None:
    rig.run({"chat_id": CHAT, "text": "I hear you on Delta.", "call_id": OLD})
    time.sleep(0.2)                     # anything scheduled on the calls loop has run
    assert rig.rendered == [], "nothing was rendered: no audio exists for a refused line"
    for s in rig.manager.sessions.values():
        assert s.voice.said_n == 0 and not s.voice.queue, f"audio queued on call {s.msg_id}"


def test_a_plain_dc_say_is_spoken_once_into_the_newest_call(rig) -> None:
    ack = rig.run({"chat_id": CHAT, "text": "I hear you on Delta."})
    assert ack["ok"] is True, ack
    assert rig.spoken == [(NEW, "I hear you on Delta.")], "once, into the newest call"
    assert rig.rendered == ["I hear you on Delta."]


# ---- the manager: the newest call is THE live call ---------------------------

def _loop_manager():
    m = calls.CallManager()
    t = threading.Thread(target=m.loop.run_forever, name="calls-test", daemon=True)
    t.start()
    return m, t


def _stop(m, t):
    m.loop.call_soon_threadsafe(m.loop.stop)
    t.join(5)
    if not m.loop.is_running():
        m.loop.close()


def _wait(pred, s=5.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _on_loop(m, coro):
    """Run CORO on the manager's calls loop, as the bot does, and wait for it."""
    return asyncio.run_coroutine_threadsafe(coro, m.loop).result(5)


def _manager_with(*ids, chat=CHAT):
    m = calls.CallManager.__new__(calls.CallManager)
    m.sessions = {}
    for n in ids:
        s = object.__new__(calls.CallSession)
        s.chat_id, s.msg_id, s.pc, s.manager = chat, n, object(), m
        m.sessions[n] = s
    return m


def test_live_call_id_is_the_newest_call_in_the_chat() -> None:
    m = _manager_with(OLD, NEW)
    assert m.live_call_id(CHAT) == NEW


def test_invite_goes_to_the_newest_call_only() -> None:
    m, t = _loop_manager()
    got = []
    try:
        for n in (OLD, NEW):
            s = object.__new__(calls.CallSession)
            s.chat_id, s.msg_id, s.pc, s.manager = CHAT, n, object(), m

            async def invite(addr, n=n):
                got.append((n, addr))
            s.invite = invite
            m.sessions[n] = s
        assert m.invite(CHAT, "bob@relay.example") is True
        assert _wait(lambda: got)
        time.sleep(0.1)                  # a second invite, if one was scheduled, has run
    finally:
        _stop(m, t)
    assert got == [(NEW, "bob@relay.example")]


# ---- the bound: a call with no audio from the caller is not live -------------

def test_the_bound_is_named_and_sane() -> None:
    assert isinstance(calls.LIVE_SILENT_S, (int, float))
    assert 60 <= calls.LIVE_SILENT_S <= 900, calls.LIVE_SILENT_S


def test_a_connected_call_with_no_audio_past_the_bound_is_not_live(monkeypatch) -> None:
    now = 10_000.0
    monkeypatch.setattr(calls, "time", types.SimpleNamespace(
        time=lambda: now, monotonic=time.monotonic, strftime=time.strftime, localtime=time.localtime))
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda *_: None, None, None))
    m = _manager_with(OLD)
    s = m.sessions[OLD]
    s.connected, s.started = True, now - 3600
    s.last_heard = now - calls.LIVE_SILENT_S - 1
    assert m.live_for_chat(CHAT) == []
    assert m.live_call_id(CHAT) is None
    s.last_heard = now - calls.LIVE_SILENT_S + 1
    assert m.live_call_id(CHAT) == OLD, "audio inside the bound: still live"
    s.last_heard = None                  # connected, never a frame since the open
    assert m.live_call_id(CHAT) is None
    s.started = now - 5
    assert m.live_call_id(CHAT) == OLD, "just opened: live"


def test_a_connecting_call_is_live_whatever_the_clock(monkeypatch) -> None:
    m = _manager_with(NEW)
    s = m.sessions[NEW]
    s.connected, s.started, s.last_heard = False, None, None
    assert m.live_call_id(CHAT) == NEW


def test_a_silent_old_call_does_not_take_a_dc_say_and_is_not_ended(monkeypatch) -> None:
    """The incident's 136 minutes: 52 silent, 62 live. 52 is not live, and
    is not ended here (PHONE-299 owns end_call)."""
    now = 10_000.0
    monkeypatch.setattr(calls, "time", types.SimpleNamespace(
        time=lambda: now, monotonic=time.monotonic, strftime=time.strftime, localtime=time.localtime))
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda *_: None, None, None))
    m = _manager_with(OLD)
    s = m.sessions[OLD]
    s.connected, s.started, s.last_heard = True, now - 8160, now - 8100
    assert m.live_call_id(CHAT) is None
    assert OLD in m.sessions and s.pc is not None, "not ended, not forgotten"


# ---- the log stamp follows the call ------------------------------------------

def test_a_new_call_logs_its_own_id_from_its_first_line(tmp_path, monkeypatch) -> None:
    """Call 52 is open (the stamp says 52). Call 62 rings: every line it logs,
    from INCOMING on, says [call 62], never [call 52]."""
    lines = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lines.append, lambda p: p, lambda *a, **k: None))
    monkeypatch.setattr(calls, "_call_id", OLD)              # 52's open stamped it
    monkeypatch.setattr(calls, "CALLS_DIR", tmp_path / "calls")
    monkeypatch.setattr(calls, "PROFILE", "")
    monkeypatch.setattr(calls, "RING", False)
    monkeypatch.setattr(calls.seatmail, "id_host", lambda: "test")
    monkeypatch.setattr(calls.keygate, "caller", lambda *a, **k: True)

    async def names(self):
        self.chat_name, self.caller = "Owner", "owner"
    monkeypatch.setattr(calls.CallSession, "_names", names)

    async def no_pc(self):
        raise RuntimeError("the rig stops here, after the setup lines")
    monkeypatch.setattr(calls.CallSession, "_build_pc", no_pc)
    m, t = _loop_manager()
    try:
        account = FakeAccount()
        m.incoming(account, Obj(msg_id=NEW, chat_id=CHAT, place_call_info="v=0", has_video=False))
        assert _wait(lambda: account.ended == [NEW]), lines
    finally:
        _stop(m, t)
    mine = [ln for ln in lines if "INCOMING" in ln or "session failed" in ln]
    assert len(mine) == 2, lines
    for ln in lines:
        assert ln.endswith(f"[call {NEW}]"), f"a line of call {NEW} carries another stamp: {ln!r}"


def _closable(msg_id, manager):
    """A bare session close() can run on: a pc, nothing connected."""
    s = object.__new__(calls.CallSession)
    s.chat_id, s.msg_id, s.manager = CHAT, msg_id, manager
    s.caller, s.connected, s.tasks = "owner", False, []
    s.turn_pending, s.turn_parts = set(), []

    class PC:
        async def close(self):
            pass
    s.pc = PC()
    for name in ("_roots_end", "_turn_cancel", "_noreply_cancel", "_rec_end"):
        setattr(s, name, lambda *a: None)
    s._unanswered = lambda *a: None
    s._close_line = lambda why, pc: why
    manager.sessions[msg_id] = s
    return s


def test_the_old_calls_close_keeps_the_new_calls_stamp_and_live_file(tmp_path, monkeypatch,
                                                                     live_file_in_tmp) -> None:
    lines = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lines.append, lambda p: p, lambda *a, **k: None))
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda *a, **k: None)
    m, t = _loop_manager()
    try:
        _closable(OLD, m)
        new = _closable(NEW, m)
        new.dir = tmp_path / "call-62"
        new._live_write()                 # 62 opened after 52: it owns call-live.json
        monkeypatch.setattr(calls, "_call_id", NEW)   # and the stamp
        m.ended(OLD)                      # 52's "connection closed", from the event thread
        assert _wait(lambda: OLD not in m.sessions), lines
    finally:
        _stop(m, t)
    closing = [ln for ln in lines if f"closing msg {OLD}" in ln]
    assert closing and closing[0].endswith(f"[call {OLD}]"), lines
    assert calls._call_id == NEW, "the old call's close unstamped the new call"
    calls.log("a line from no call")
    assert lines[-1].endswith(f"[call {NEW}]"), lines[-1]
    assert json.loads(calls.LIVE_FILE.read_text())["call_id"] == NEW
    assert new.pc is not None and NEW in m.sessions


@pytest.mark.parametrize("old_silent", [False, True], ids=["old-live", "old-silent"])
def test_the_new_calls_close_hands_the_live_file_to_the_old_live_call(tmp_path, monkeypatch,
                                                                     live_file_in_tmp,
                                                                     old_silent) -> None:
    """The newest call closes while an older one in its chat is still open.
    One id everywhere: call-live.json, live_call_id and the stamp all name the
    older call if it is still live, and none of them names a call if it is not."""
    lines = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lines.append, lambda p: p, lambda *a, **k: None))
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda *a, **k: None)
    m, t = _loop_manager()
    try:
        old = _closable(OLD, m)
        old.dir = tmp_path / "call-52"
        if old_silent:                    # its audio gone past the bound: open, not live
            old.connected, old.started = True, time.time() - 3600
            old.last_heard = time.time() - calls.LIVE_SILENT_S - 60
        new = _closable(NEW, m)
        new.dir = tmp_path / "call-62"
        new._live_write()                 # 62 opened after 52: it owns call-live.json
        monkeypatch.setattr(calls, "_call_id", NEW)   # and the stamp
        m.ended(NEW)                      # 62 hangs up; 52 is still held open
        assert _wait(lambda: NEW not in m.sessions), lines
        live_id = m.live_call_id(CHAT)
    finally:
        _stop(m, t)
    assert old.pc is not None and OLD in m.sessions, "the old call was not ended"
    on_file = (json.loads(calls.LIVE_FILE.read_text())["call_id"]
               if calls.LIVE_FILE.exists() else None)
    want = None if old_silent else OLD
    assert live_id == want
    assert on_file == live_id, f"call-live.json says {on_file}, live_call_id says {live_id}"
    assert calls._call_id == want, "the stamp disagrees with the live call"


def _openable(msg_id, manager, tmp_path):
    """A bare session _open() can run on: the live file and the stamp are real,
    the chat line, the mail, the notices and the greeting are stubs."""
    s = object.__new__(calls.CallSession)
    s.chat_id, s.msg_id, s.manager, s.pc = CHAT, msg_id, manager, object()
    s.caller, s.outgoing, s.voicemail, s.wait_task = "owner", False, False, None
    s.chat_name, s.root_id, s.greet_voice = "Owner", f"call-12-{msg_id}@test", None
    s.dir = tmp_path / f"call-{msg_id}"
    s.heard = Obj(chat_name=None, event=lambda *a, **k: None)
    s._rec_start = lambda: None
    s._rec_notice_post = lambda: None

    async def nothing(*a, **k):
        return None
    s._rec_notice_speak = nothing
    s.speak = nothing
    s._greeting_line = lambda: "Hello."
    s._connected_text = lambda *a: "connected"
    s.guest_list = list
    manager.sessions[msg_id] = s
    return s


@pytest.mark.parametrize("order", [(OLD, NEW), (NEW, OLD)], ids=["new-opens-last", "old-opens-last"])
def test_call_live_json_and_the_stamp_are_the_newest_calls_whichever_opens_last(
        tmp_path, monkeypatch, live_file_in_tmp, order) -> None:
    """52 came in first, 62 after it. Whichever connects last, call-live.json,
    the stamp and live_call_id all say 62."""
    lines = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lines.append, lambda p: p, lambda *a, **k: None))
    monkeypatch.setattr(calls, "_call_id", None)
    monkeypatch.setattr(calls, "FIRST_CALL_WAIT", False)
    m, t = _loop_manager()
    try:
        made = {n: _openable(n, m, tmp_path) for n in (OLD, NEW)}     # 52 in first
        for n in order:
            made[n].connected = True
            _on_loop(m, made[n]._open())
    finally:
        _stop(m, t)
    assert json.loads(calls.LIVE_FILE.read_text())["call_id"] == NEW
    assert calls._call_id == NEW
    assert m.live_call_id(CHAT) == NEW
    if order == (OLD, NEW):
        assert any(f"msg {NEW} supersedes msg {OLD}" in ln for ln in lines), lines
    else:
        assert any(f"msg {OLD} opened behind a newer call" in ln for ln in lines), lines


def test_a_calls_hang_up_verdicts_carry_its_own_id(tmp_path, monkeypatch, live_file_in_tmp) -> None:
    lines = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lines.append, lambda p: p, lambda *a, **k: None))
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda *a, **k: None)
    monkeypatch.setattr(calls, "_call_id", NEW)
    m, t = _loop_manager()
    try:
        old = _closable(OLD, m)
        old._unanswered = lambda why: calls.log(f"TURN 1 taken=unknown: no reply before {why}")
        m.ended(OLD)
        assert _wait(lambda: OLD not in m.sessions), lines
    finally:
        _stop(m, t)
    verdict = [ln for ln in lines if "taken=unknown" in ln]
    assert verdict == [f"CALL TURN 1 taken=unknown: no reply before hang-up [call {OLD}]"], lines


# ---- the mail leg: a stale issued root reaches THE live call, once -----------

def test_a_mail_on_a_stale_root_is_spoken_once_into_the_newest_call(tmp_path, monkeypatch) -> None:
    """A guest's mail threads to a root this bridge issued for the chat, but
    no held call has that root (it went stale when a call turned over). With
    52 held open and 62 live, the line is spoken once, into 62: the mail leg's
    fallback spoke into every call with a peer connection in the chat."""
    stale, old_root, new_root = (f"call-{CHAT}-{n}@relay.example" for n in (1907204101, OLD, NEW))
    monkeypatch.setattr(calls, "ISSUED", calls.IssuedRoots())
    calls.ISSUED.mint(CHAT, stale, now=1907204101)
    calls.ISSUED.end(CHAT, now=1907204389)
    calls.ISSUED.mint(CHAT, new_root, now=1907204400)
    lines, spoken, posted = [], [], []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lines.append, None, None))
    monkeypatch.setattr(calls, "VOICE_MAP_FILE", tmp_path / "voices.json")
    monkeypatch.setattr(calls, "reply_seen", lambda *a: None)
    monkeypatch.setattr(calls, "post_to_chat", lambda acct, chat, text, what: posted.append(chat))

    def run(coro, loop):
        spoken.append(coro.cr_frame.f_locals["self"].msg_id)
        coro.close()
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", run)
    m = _manager_with(OLD, NEW)
    m.loop = None
    m.sessions[OLD].root_id, m.sessions[NEW].root_id = old_root, new_root
    for s in m.sessions.values():
        s.account = None
    f = tmp_path / "new" / "m1"
    f.parent.mkdir()
    f.write_text(f"From: Carol <carol@example.org>\nTo: ada@host\nSubject: re: call\n"
                 f"In-Reply-To: <{stale}>\nReferences: <{stale}>\n"
                 "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\nHello the call.\n")
    calls.MailLeg(m).one(f)
    assert spoken == [NEW], f"spoken once, into the newest call: {spoken} {lines}"
    assert posted == [CHAT], "posted to the chat once"

"""A dc-say into a live call whose speech fails must not be acked "spoken".

PHONE-229. On a setup walk with no ffmpeg, every spoken reply failed
(`No such file or directory: 'ffmpeg'`) and fell back to chat text, yet each
dc-say ack read "spoken into the call". The agent passed that on, so the
person was told they had been heard when they had not. Only bot.log had the
failure.

The rig runs one pass of main()'s outbox loop over fakes, built like the rig
in tests/test_group_job.py (copied, not imported). Only the edges are fake:
the Delta account, the TTS render (`calls.tts_pcm48` raises the error the
walk saw) and the call itself (a CallSession with a fake peer connection).
The speech path that runs is the real one: CallManager.speak_if_live
schedules the real CallSession.speak on a real calls loop, and that speak
falls back to chat text exactly as it did on the walk. The test waits for the
fallback job, so the TTS failure has happened before the ack is read.

The first test failed on cd618d9 with an AssertionError: the ack read "sent
to chat 12, msg 77; spoken into the call". The rig's own checks raise
RuntimeError instead, so a broken rig never passes as the defect.

The fix (bot.py's speech-truth block) waits up to SPEAK_WAIT for speak()'s
result. The other tests pin the rest of it: a line rendered and handed to the
call is acked "spoken"; a render that outlasts the wait is acked "queued to
speak", and its later failure is mailed to the agent.

The outbox loop is serial, so the wait is one SPEAK_WAIT per pass, shared by
every speaking job in it (fix-002). With one wait per job, a burst of speaking
dc-says acked at 3, 6, 9 s: the 3rd dc-say --call missed its 8 s ack budget
(a false NO ANSWER) and later jobs the 10 s take budget (a false NOT TAKEN),
though the bot sent them. The burst test pins the shared budget.
"""
import concurrent.futures
import json
import re
import threading
import time

import pytest

from sessionphone.bridge import bot, calls

OWNER = "alice@relay.example"
LIST = f"family  chat:12   # the family group\nowner   {OWNER}   # owner\n"
CHAT = 12
CALL = 7                    # the live call's msg id in chat 12
FFMPEG = FileNotFoundError(2, "No such file or directory", "ffmpeg")


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


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def live_session():
    """A call that is up in chat 12: a peer connection, the real voice track."""
    s = object.__new__(calls.CallSession)
    s.chat_id, s.msg_id, s.pc = CHAT, CALL, object()
    s.lines = []
    s.voice = calls.PipVoiceTrack()
    s.replied = lambda via: None        # turn-log bookkeeping, not under test
    # said-rows and echo bookkeeping, when a sentence does reach the track
    s.heard = Obj(saying=lambda *a, **k: None, said=lambda *a, **k: None)
    s.echo = Obj(spoken=lambda *a, **k: None)
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
    # speak()'s fallback job: its own dir, so the loop under test never reads it
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
    # raising=False: the list is new with the fix, and on the code before it
    # the repro must still reach its assert (the defect), not fail in setup
    monkeypatch.setattr(bot, "_SPEECH_PENDING", [], raising=False)
    r.mails = []
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: r.mails.append((a, k)))
    # calls on, with a live call; nothing else of the calls side starts
    monkeypatch.setattr(bot, "CALLS", True)
    monkeypatch.setattr(bot, "calls", calls)
    monkeypatch.setattr(calls, "setup", lambda *a, **k: None)
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: p, lambda *a, **k: None))

    def no_ffmpeg(text, voice=None):
        raise FFMPEG
    r.tts = no_ffmpeg
    monkeypatch.setattr(calls, "tts_pcm48", lambda text, voice=None: r.tts(text, voice))

    manager = calls.CallManager()
    manager.sessions[CALL] = live_session()
    loop = threading.Thread(target=manager.loop.run_forever, name="calls-test", daemon=True)
    loop.start()
    monkeypatch.setattr(calls, "speak_if_live", manager.speak_if_live)
    monkeypatch.setattr(calls, "live_call_id", manager.live_call_id)

    def run(job, fallback=True):
        r.out.mkdir(parents=True, exist_ok=True)
        ack = tmp_path / "acks" / "00.json"
        (r.out / "00.json").write_text(json.dumps(dict(job, ack=str(ack))))
        try:
            with pytest.raises(Done):
                bot.main()
        finally:
            stop.set()
        # The TTS failure must have happened before the ack is judged: wait
        # for speak()'s fallback job, the words it could not say.
        if fallback:
            r.wait_fallback()
        return json.loads(ack.read_text())

    def wait_fallback():
        deadline = time.monotonic() + 10
        while not list(r.fallback.glob("*.json")) and time.monotonic() < deadline:
            time.sleep(0.05)
        r.fallen = [json.loads(p.read_text()) for p in sorted(r.fallback.glob("*.json"))]
    def run_burst(jobs):
        """Every job in the outbox before the pass: one pass takes them all."""
        r.out.mkdir(parents=True, exist_ok=True)
        acks = [tmp_path / "acks" / f"{k:02d}.json" for k in range(len(jobs))]
        for k, (job, a) in enumerate(zip(jobs, acks)):
            (r.out / f"{k:02d}.json").write_text(json.dumps(dict(job, ack=str(a))))
        try:
            with pytest.raises(Done):
                bot.main()
        finally:
            stop.set()
        return [json.loads(a.read_text()) for a in acks]
    r.run, r.run_burst, r.wait_fallback = run, run_burst, wait_fallback
    yield r
    manager.loop.call_soon_threadsafe(manager.loop.stop)
    loop.join(5)


def says_spoken(detail):
    """True if the ack claims the line was voiced. "NOT spoken" is honest."""
    return re.search(r"(?<!not )\bspoken\b", detail, re.IGNORECASE) is not None


def rig_check(ok, what):
    """The repro's preconditions: a miss is a broken rig, not the defect."""
    if not ok:
        raise RuntimeError(f"rig broken: {what}")


@pytest.mark.parametrize("call_id", [None, CALL], ids=["dc-say", "dc-say --call"])
def test_a_tts_failure_on_a_live_call_is_not_acked_as_spoken(rig, call_id) -> None:
    job = {"chat_id": CHAT, "text": "Hi Alice."}
    if call_id is not None:
        job["call_id"] = call_id
    ack = rig.run(job)
    rig_check(ack["ok"] is True, f"the text went, so the ack is ok: {ack}")
    # the walk's failure, reproduced: the text went, TTS failed, it fell back
    rig_check(rig.account.sent == [(CHAT, "Hi Alice.")], f"text not sent: {rig.account.sent}")
    rig_check(any("tts failed" in m and "ffmpeg" in m for m in rig.logs), f"TTS did not fail: {rig.logs}")
    rig_check(rig.fallen and rig.fallen[0]["no_speak"] is True, f"no fallback job: {rig.fallen}")
    # the ack must not claim the line was spoken, and must name why it was not
    assert not says_spoken(ack["detail"]), ack["detail"]
    assert "ffmpeg" in ack["detail"], ack["detail"]
    assert "NOT spoken" in ack["detail"], ack["detail"]
    assert rig.mails == [], "a failure named in the ack is not mailed again"


def test_a_line_rendered_and_handed_to_the_call_is_acked_spoken(rig) -> None:
    rig.tts = lambda text, voice=None: b"\0\0" * 4800
    ack = rig.run({"chat_id": CHAT, "text": "Hi Alice.", "call_id": CALL}, fallback=False)
    rig_check(ack["ok"] is True, f"the text went, so the ack is ok: {ack}")
    assert ack["detail"].endswith("; spoken into the call"), ack["detail"]
    assert bot._SPEECH_PENDING == []


def test_a_slow_render_is_queued_and_its_later_failure_is_mailed(rig, monkeypatch) -> None:
    monkeypatch.setattr(bot, "SPEAK_WAIT", 0.2)
    release = threading.Event()

    def slow_then_no_ffmpeg(text, voice=None):
        release.wait(10)
        raise FFMPEG
    rig.tts = slow_then_no_ffmpeg
    ack = rig.run({"chat_id": CHAT, "text": "Hi Alice."}, fallback=False)
    # the ack cannot wait for the outcome: it says queued, never spoken
    rig_check(ack["ok"] is True, f"the text went, so the ack is ok: {ack}")
    assert ack["detail"].endswith("; queued to speak"), ack["detail"]
    assert not says_spoken(ack["detail"]), ack["detail"]
    assert len(bot._SPEECH_PENDING) == 1
    bot.speech_sweep()          # still rendering: nothing to report yet
    assert rig.mails == [] and len(bot._SPEECH_PENDING) == 1

    release.set()
    rig.wait_fallback()
    rig_check(rig.fallen and rig.fallen[0]["no_speak"] is True, f"no fallback job: {rig.fallen}")
    concurrent.futures.wait(bot._SPEECH_PENDING[0][0], timeout=10)
    bot.speech_sweep()          # the outbox loop's next pass
    assert bot._SPEECH_PENDING == []
    assert len(rig.mails) == 1, rig.mails
    (_, chat_id, _, text), kw = rig.mails[0]
    assert chat_id == CHAT
    assert "NOT spoken" in text and "ffmpeg" in text and "msg 77" in text, text
    assert "NOT spoken" in kw["subject"], kw
    assert not says_spoken(text), text


def test_a_slow_render_that_succeeds_mails_nothing(rig, monkeypatch) -> None:
    monkeypatch.setattr(bot, "SPEAK_WAIT", 0.2)
    release = threading.Event()

    def slow(text, voice=None):
        release.wait(10)
        return b"\0\0" * 4800
    rig.tts = slow
    ack = rig.run({"chat_id": CHAT, "text": "Hi Alice."}, fallback=False)
    assert ack["detail"].endswith("; queued to speak"), ack["detail"]
    release.set()
    concurrent.futures.wait(bot._SPEECH_PENDING[0][0], timeout=10)
    bot.speech_sweep()
    assert bot._SPEECH_PENDING == [] and rig.mails == []


def test_a_queued_line_with_no_outcome_is_dropped_after_the_watch(monkeypatch) -> None:
    logs = []
    monkeypatch.setattr(bot, "log", logs.append)
    monkeypatch.setattr(bot, "_SPEECH_PENDING", [([concurrent.futures.Future()], CHAT, 77, 0.0)])
    bot.speech_sweep()
    assert bot._SPEECH_PENDING == []
    assert any("not watched any more" in m for m in logs), logs


@pytest.mark.parametrize("result, ok, note", [
    ({"n": 2, "failed": 0, "error": None, "cut": False}, True, "spoken into the call"),
    ({"n": 2, "failed": 2, "error": "boom", "cut": False}, False, "NOT spoken: tts failed: boom"),
    ({"n": 3, "failed": 1, "error": "boom", "cut": False}, False,
     "NOT spoken in full: 1 of 3 sentences failed, tts failed: boom"),
    ({"n": 2, "failed": 0, "error": None, "cut": True}, False, "NOT spoken in full: cut off on the call"),
    ({"n": 0, "failed": 0, "error": None, "cut": False}, False, "NOT spoken: nothing to say"),
    (RuntimeError("no\nline"), False, "NOT spoken: RuntimeError: no line"),
    (None, None, "queued to speak"),
], ids=["spoken", "all-failed", "partly-failed", "cut", "empty", "raised", "unknown"])
def test_speech_note_says_spoken_only_for_a_whole_line(result, ok, note) -> None:
    assert bot.speech_note(result) == (ok, note)
    assert says_spoken(note) is (ok is True)


def test_an_ack_with_no_futures_is_queued_not_spoken() -> None:
    """A speak_if_live that returns a bare True (an older fake) is not proof."""
    assert bot.speech_ack(True, CHAT, 77) == "queued to speak"


# dc-say --call waits 8 s for its ack (bin/dc-say); every dc-say waits 10 s for
# its take (dc_taken, bin/dc-env). A job can land just after a pass began, so it
# may wait out that pass and the loop's 1 s sleep before its own pass starts.
CALL_ACK_BUDGET, TAKEN_BUDGET, PASS_GAP = 8.0, 10.0, 1.0


def test_a_burst_of_speaking_jobs_shares_one_speech_wait(rig, monkeypatch) -> None:
    """N speaking dc-says in one pass, every line still rendering: the pass
    holds SPEAK_WAIT in all, and every ack is inside both budgets. With one
    wait per job they acked at 3, 6, 9, 12, 15 s."""
    release = threading.Event()

    def slow(text, voice=None):
        release.wait(30)
        return b"\0\0" * 4800
    rig.tts = slow
    t0, at = time.monotonic(), []
    real_write = bot.ack.write

    def timed_write(path, ok, detail="", log=None):
        at.append(time.monotonic() - t0)
        return real_write(path, ok, detail, log=log)
    monkeypatch.setattr(bot.ack, "write", timed_write)
    n = 5
    # dc-say --call and plain dc-say, mixed: both kinds speak into the live call
    jobs = [dict({"chat_id": CHAT, "text": f"Line {k}."}, **({"call_id": CALL} if k % 2 else {}))
            for k in range(n)]
    try:
        acks = rig.run_burst(jobs)
        took = time.monotonic() - t0
    finally:
        release.set()
    rig_check(len(at) == n, f"one ack per job: {at}")
    rig_check(len(rig.account.sent) == n, f"every text went: {rig.account.sent}")
    for a in acks:
        assert a["ok"] is True, a
        assert a["detail"].endswith("; queued to speak"), a["detail"]
        assert not says_spoken(a["detail"]), a["detail"]
    # the whole pass held one speech wait, not one per job
    assert took < bot.SPEAK_WAIT + 1.0, (took, at)
    # each ack (and the take, which follows it at once) is inside its budget,
    # even for a job that waited out the pass before its own
    for t in at:
        assert bot.SPEAK_WAIT + PASS_GAP + t < CALL_ACK_BUDGET, at
        assert bot.SPEAK_WAIT + PASS_GAP + t < TAKEN_BUDGET, at
    assert max(at) < bot.SPEAK_WAIT + 1.0, at
    # every queued line is still watched: its outcome is reported later
    assert len(bot._SPEECH_PENDING) == n
    for futs, *_ in list(bot._SPEECH_PENDING):
        concurrent.futures.wait(futs, timeout=30)
    bot.speech_sweep()
    assert bot._SPEECH_PENDING == [] and rig.mails == []


def test_a_spent_speech_wait_does_not_wait_again(monkeypatch) -> None:
    """A job reached after the pass's wait is spent acks at once: queued if
    still rendering, its real outcome if already done."""
    monkeypatch.setattr(bot, "log", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_SPEECH_PENDING", [])
    spent = time.monotonic() - 1.0
    t0 = time.monotonic()
    assert bot.speech_ack([concurrent.futures.Future()], CHAT, 77, spent) == "queued to speak"
    assert len(bot._SPEECH_PENDING) == 1
    done = concurrent.futures.Future()
    done.set_result({"n": 1, "failed": 0, "error": None, "cut": False})
    assert bot.speech_ack([done], CHAT, 78, spent) == "spoken into the call"
    failed = concurrent.futures.Future()
    failed.set_result({"n": 1, "failed": 1, "error": "boom", "cut": False})
    assert bot.speech_ack([failed], CHAT, 79, spent) == "NOT spoken: tts failed: boom"
    assert time.monotonic() - t0 < 0.5
    assert len(bot._SPEECH_PENDING) == 1


def test_an_ack_waits_only_to_the_deadline_it_is_given(monkeypatch) -> None:
    monkeypatch.setattr(bot, "log", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_SPEECH_PENDING", [])
    t0 = time.monotonic()
    assert bot.speech_ack([concurrent.futures.Future()], CHAT, 77, t0 + 0.3) == "queued to speak"
    assert 0.25 <= time.monotonic() - t0 < 1.5

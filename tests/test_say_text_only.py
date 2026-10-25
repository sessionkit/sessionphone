"""dc-say --text-only posts to the chat and never speaks it into a call (PHONE-252).

Case 162: on a fresh Mac an agent's report, sent with a plain `dc-say`, was read
aloud into the owner's live call, because a plain `dc-say` is spoken into any
call live in the chat. `--text-only` sets the job's `no_speak` key (jobs.py
already accepted it; the bot already skips speech for it).

Two layers, as in tests/test_dc_channels.py and tests/test_say_tts_truth.py
(their rigs are copied here, not imported):

- bin level: the real `bin/dc-say` against a fake bot. The job carries
  `"no_speak": true` with the flag and no such key without it; `--text-only`
  with `--call` is a usage error, exit 1, nothing queued.
- bot level: one pass of main()'s outbox loop over fakes with a call live in
  the chat. A `--text-only` job is posted, `speak_if_live` is NOT called, and
  the ack says it was not spoken. The control (no flag) speaks.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sessionphone import jobs
from sessionphone.bridge import bot, calls

BIN = Path(__file__).resolve().parent.parent / "bin"

BOT_OWNER = "alice@relay.example"
LIST = f"family  chat:12   # the family group\nowner   {BOT_OWNER}   # owner\n"
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
    r.run, r.wait_fallback = run, wait_fallback
    yield r
    manager.loop.call_soon_threadsafe(manager.loop.stop)
    loop.join(5)


# ---- bin level ---------------------------------------------------------------

TOOL_LIST = "owner  bob@relay.example  # owner\nfamily  chat:42  # the family group\n"


def run_tool(root, *args, wait="0"):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    e.update(DC_ROOT=str(root), DC_TAKEN_WAIT=wait)
    return subprocess.run([str(BIN / "dc-say"), *args], env=e, capture_output=True,
                          text=True, timeout=60, check=False)


class FakeBot:
    """Answers each job in its ack file before the unlink, as the real bot does."""

    def __init__(self, root):
        self.out, self.jobs = root / "outbox", []
        self.out.mkdir(parents=True, exist_ok=True)
        jobs.write_stamp(self.out)
        self.stop = threading.Event()
        self.t = threading.Thread(target=self.loop)

    def loop(self):
        while not self.stop.is_set():
            for f in sorted(self.out.glob("*.json")):
                job = json.loads(f.read_text())
                self.jobs.append(job)
                Path(job["ack"]).write_text(json.dumps({"ok": True, "detail": "done"}))
                f.unlink()
            time.sleep(0.05)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()


@pytest.fixture
def root(tmp_path):
    p = tmp_path / "channels.txt"
    p.write_text(TOOL_LIST)
    p.chmod(0o600)
    return tmp_path


def test_text_only_puts_no_speak_in_the_job(root) -> None:
    with FakeBot(root) as fb:
        r = run_tool(root, "--text-only", "12", "the", "report")
    assert r.returncode == 0, r.stderr
    job = dict(fb.jobs[0])
    job.pop("ack")
    assert job == {"chat_id": 12, "text": "the report", "no_speak": True}


def test_text_only_flag_may_follow_other_flags(root) -> None:
    with FakeBot(root) as fb:
        r = run_tool(root, "--who", "Ada", "--text-only", "owner", "hi")
    assert r.returncode == 0, r.stderr
    assert fb.jobs[0]["no_speak"] is True and fb.jobs[0]["who"] == "Ada"


def test_without_the_flag_the_job_has_no_no_speak_key(root) -> None:
    with FakeBot(root) as fb:
        r = run_tool(root, "12", "hello")
    assert r.returncode == 0, r.stderr
    assert "no_speak" not in fb.jobs[0]


def test_text_only_with_call_is_a_usage_error_and_queues_nothing(root) -> None:
    with FakeBot(root) as fb:
        for args in (("--text-only", "--call", "5", "12", "hi"), ("--call", "5", "--text-only", "12", "hi")):
            r = run_tool(root, *args)
            assert r.returncode == 1, (args, r.returncode, r.stderr)
            assert "--text-only" in r.stderr and "--call" in r.stderr and "Nothing was queued" in r.stderr
    assert fb.jobs == []
    assert list((root / "outbox").glob("*.json")) == []


def test_usage_and_header_say_when_to_use_it() -> None:
    text = (BIN / "dc-say").read_text()
    head = text.split("set -euo pipefail")[0]
    assert "--text-only" in head and "report" in head and "no_speak" in head
    r = subprocess.run([str(BIN / "dc-say"), "--text-only", "12"], capture_output=True, text=True,
                       timeout=60, check=False, env={**os.environ, "DC_ROOT": "/nonexistent-phone-252"})
    assert r.returncode == 1 and "--text-only" in r.stderr, r.stderr


# ---- bot level: a call is live in the chat -----------------------------------


def spoken_calls(monkeypatch):
    seen = []
    real = calls.speak_if_live

    def spy(cid, text, voice, who):
        seen.append((cid, text))
        return real(cid, text, voice, who)
    monkeypatch.setattr(calls, "speak_if_live", spy)
    return seen


def test_text_only_on_a_live_call_posts_to_the_chat_and_does_not_speak(rig, monkeypatch) -> None:
    speak = spoken_calls(monkeypatch)
    rig.tts = lambda text, voice=None: b"\0\0" * 4800
    ack = rig.run({"chat_id": CHAT, "text": "Report: all green.", "no_speak": True}, fallback=False)
    assert rig.account.sent == [(CHAT, "Report: all green.")]
    assert speak == [], "speak_if_live was called for a --text-only job"
    assert ack["ok"] is True, ack
    assert ack["detail"].endswith("; NOT spoken (chat only)"), ack["detail"]
    assert "spoken into the call" not in ack["detail"] and "queued to speak" not in ack["detail"]
    assert not any("SPEAKING into call" in m for m in rig.logs), rig.logs


def test_control_without_the_flag_speaks_into_the_live_call(rig, monkeypatch) -> None:
    speak = spoken_calls(monkeypatch)
    rig.tts = lambda text, voice=None: b"\0\0" * 4800
    ack = rig.run({"chat_id": CHAT, "text": "Report: all green."}, fallback=False)
    # the rig is only honest if the same job, minus the flag, does speak
    assert rig.account.sent == [(CHAT, "Report: all green.")]
    assert speak == [(CHAT, "Report: all green.")]
    assert ack["detail"].endswith("; spoken into the call"), ack["detail"]

"""dc-say --quote MSG_ID: an agent answers a chat line as a quote-reply (PHONE-310).

In a group with several agents, plain lines cannot be told apart: the reader
cannot see which line each one answers. `dc-say --quote N` posts the text as a
quote-reply to message N, and the footer of a bridged mail that carries
X-Delta-Chat-Msg-Id shows that form, so an agent answers the line it got.

Three layers, rigs copied from tests/test_say_text_only.py (not imported):

- bin level: the real `bin/dc-say` against a fake bot. The job carries
  `quote_msg_id` (never `msg_id`, which means "react to this"); a bad or
  missing id is a usage error, exit 1, nothing queued; a bot refusal is exit 3.
- bot level: one pass of main()'s outbox loop over a fake account. A msg in the
  chat is sent with quoted_msg=N; a msg in another chat, or none, is REFUSED:
  no send_* call, nothing spoken, ack false with the reason, a REFUSED log line.
  With a live call, the quote changes the chat message only, never the speech.
- footer: mail_in()'s reply hint carries `--quote <msg id>` exactly when the
  mail carries X-Delta-Chat-Msg-Id; a call or webxdc mail keeps today's hint.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sessionphone import jobs
from sessionphone.bridge import bot, calls, dc_env

BIN = Path(__file__).resolve().parent.parent / "bin"

BOT_OWNER = "alice@relay.example"
LIST = f"family  chat:12   # the family group\nother  chat:13   # another group\nowner   {BOT_OWNER}   # owner\n"
CHAT, OTHER = 12, 13
CALL = 7                    # the live call's msg id in chat 12
LINE = 5531                 # a message in chat 12: the line being answered
ELSEWHERE = 900             # a message in chat 13


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
        self.account.sent.append(("send_text", self.id, {"text": text}))
        return Obj(id=77)

    def send_message(self, **kw):
        self.account.sent.append(("send_message", self.id, kw))
        return Obj(id=78)


class FakeMessage:
    def __init__(self, account, mid):
        self.account, self.id = account, mid

    def get_snapshot(self):
        if self.id not in self.account.msgs:
            raise RuntimeError(f"message {self.id} not found")      # as the rpc says
        return Obj(id=self.id, chat_id=self.account.msgs[self.id])


class FakeAccount:
    def __init__(self):
        self.sent = []
        self.chats = {CHAT: FakeChat(self, CHAT), OTHER: FakeChat(self, OTHER)}
        self.msgs = {LINE: CHAT, ELSEWHERE: OTHER}      # msg id -> its chat

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

    def get_message_by_id(self, mid):
        return FakeMessage(self, mid)


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
    s.replied = lambda via: None
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
    monkeypatch.setattr(bot, "_SPEECH_PENDING", [])
    r.mails = []
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: r.mails.append((a, k)))
    monkeypatch.setattr(bot, "CALLS", True)
    monkeypatch.setattr(bot, "calls", calls)
    monkeypatch.setattr(calls, "setup", lambda *a, **k: None)
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: p, lambda *a, **k: None))
    monkeypatch.setattr(calls, "tts_pcm48", lambda text, voice=None: b"\0\0" * 4800)

    manager = calls.CallManager()
    loop = threading.Thread(target=manager.loop.run_forever, name="calls-test", daemon=True)
    loop.start()
    r.manager = manager
    r.spoken = []

    def spy(cid, text, voice, who):
        r.spoken.append((cid, text))
        return manager.speak_if_live(cid, text, voice, who)
    monkeypatch.setattr(calls, "speak_if_live", spy)
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
        assert not list(r.out.glob("*.json")), "the job was not unlinked"
        return json.loads(ack.read_text())
    r.run = run

    def go_live():
        manager.sessions[CALL] = live_session()
    r.go_live = go_live
    yield r
    manager.loop.call_soon_threadsafe(manager.loop.stop)
    loop.join(5)


# ---- bot level ---------------------------------------------------------------

def test_a_line_in_the_chat_is_sent_as_a_quote_reply(rig) -> None:
    ack = rig.run({"chat_id": CHAT, "text": "Yes, done.", "quote_msg_id": LINE})
    assert rig.account.sent == [("send_message", CHAT, {"text": "Yes, done.", "html": None,
                                                        "quoted_msg": LINE})]
    assert ack["ok"] is True and ack["detail"].startswith(f"sent to chat {CHAT}, msg 78"), ack
    assert any(f"quoting msg {LINE}" in m for m in rig.logs), rig.logs


def test_a_job_without_a_quote_is_sent_as_before(rig) -> None:
    ack = rig.run({"chat_id": CHAT, "text": "plain"})
    assert rig.account.sent == [("send_text", CHAT, {"text": "plain"})]
    assert ack["ok"] is True


def test_the_quote_rides_with_html_and_who(rig, monkeypatch) -> None:
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Ada")
    ack = rig.run({"chat_id": CHAT, "text": "preview", "html": "<b>full</b>", "who": "Bob",
                   "quote_msg_id": LINE})
    assert rig.account.sent == [("send_message", CHAT, {"text": "Bob: preview", "html": "<b>full</b>",
                                                        "quoted_msg": LINE})]
    assert ack["ok"] is True


@pytest.mark.parametrize("mid, why", [
    (ELSEWHERE, f"msg {ELSEWHERE} is in chat {OTHER}, not chat {CHAT}"),
    (424242, "msg 424242 is not a message this account has"),
])
def test_a_msg_not_in_the_chat_is_refused_and_nothing_is_sent(rig, mid, why) -> None:
    rig.go_live()                          # a live call: the refusal stops the speech too
    ack = rig.run({"chat_id": CHAT, "text": "Yes, done.", "quote_msg_id": mid})
    assert rig.account.sent == [], "a send_* call was made for a refused quote"
    assert rig.spoken == [], "a refused quote was spoken"
    assert ack["ok"] is False and why in ack["detail"] and "nothing was sent" in ack["detail"], ack
    assert any(m.startswith(f"REFUSED dc-say --quote {mid}: ") and why in m for m in rig.logs), rig.logs


@pytest.mark.parametrize("bad", [0, -3, "abc", True])
def test_a_job_with_a_bad_quote_id_is_refused_not_crashed(rig, bad) -> None:
    ack = rig.run({"chat_id": CHAT, "text": "x", "quote_msg_id": bad})
    assert rig.account.sent == []
    assert ack["ok"] is False and "positive number" in ack["detail"], ack


def test_quote_with_a_live_call_speaks_the_text_and_quotes_in_the_chat(rig) -> None:
    rig.go_live()
    ack = rig.run({"chat_id": CHAT, "text": "On it.", "call_id": CALL, "quote_msg_id": LINE})
    assert rig.account.sent == [("send_message", CHAT, {"text": "On it.", "html": None,
                                                        "quoted_msg": LINE})]
    assert rig.spoken == [(CHAT, "On it.")], "the speech is the text alone, as today"
    assert ack["ok"] is True and "spoken into the call" in ack["detail"], ack


def test_quote_with_a_call_that_is_over_is_refused_by_the_call_gate(rig) -> None:
    ack = rig.run({"chat_id": CHAT, "text": "late", "call_id": CALL, "quote_msg_id": LINE})
    assert rig.account.sent == [] and rig.spoken == []
    assert ack["ok"] is False and "no live call" in ack["detail"], ack


def test_quote_with_text_only_quotes_and_is_never_spoken(rig) -> None:
    rig.go_live()
    ack = rig.run({"chat_id": CHAT, "text": "Report.", "no_speak": True, "quote_msg_id": LINE})
    assert rig.account.sent == [("send_message", CHAT, {"text": "Report.", "html": None,
                                                        "quoted_msg": LINE})]
    assert rig.spoken == []
    assert ack["ok"] is True and ack["detail"].endswith("; NOT spoken (chat only)"), ack


def test_the_quote_gate_runs_after_the_channels_gate(rig) -> None:
    """A chat that is not listed is refused by the list, before any lookup."""
    ack = rig.run({"chat_id": 99, "text": "x", "quote_msg_id": LINE})
    assert rig.account.sent == [] and ack["ok"] is False
    assert not any("REFUSED dc-say --quote" in m for m in rig.logs)


def test_quote_msg_id_is_a_job_key_the_bot_stamps() -> None:
    assert "quote_msg_id" in jobs.KEYS
    # an unstamped (older) bot would drop the quote and send plain: refused instead
    assert "quote_msg_id" not in jobs.LEGACY_KEYS


# ---- bin level ---------------------------------------------------------------

TOOL_LIST = "owner  bob@relay.example  # owner\nfamily  chat:42  # the family group\n"


def run_tool(root, *args, wait="0"):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    e.update(DC_ROOT=str(root), DC_TAKEN_WAIT=wait)
    return subprocess.run([str(BIN / "dc-say"), *args], env=e, capture_output=True,
                          text=True, timeout=60, check=False)


class FakeBot:
    """Answers each job in its ack file before the unlink, as the real bot does.
    ANSWER(job) -> (ok, detail)."""

    def __init__(self, root, answer=lambda job: (True, "done")):
        self.out, self.jobs, self.answer = root / "outbox", [], answer
        self.out.mkdir(parents=True, exist_ok=True)
        jobs.write_stamp(self.out)
        self.stop = threading.Event()
        self.t = threading.Thread(target=self.loop)

    def loop(self):
        while not self.stop.is_set():
            for f in sorted(self.out.glob("*.json")):
                job = json.loads(f.read_text())
                self.jobs.append(job)
                ok, detail = self.answer(job)
                Path(job["ack"]).write_text(json.dumps({"ok": ok, "detail": detail}))
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


def job_of(fb):
    job = dict(fb.jobs[0])
    job.pop("ack")
    return job


def test_quote_puts_quote_msg_id_in_the_job_never_msg_id(root) -> None:
    with FakeBot(root) as fb:
        r = run_tool(root, "--quote", "5531", "family", "yes,", "done")
    assert r.returncode == 0, r.stderr
    assert job_of(fb) == {"chat": "family", "text": "yes, done", "quote_msg_id": 5531}
    assert "quoting msg 5531" in r.stdout


def test_without_quote_the_job_has_no_quote_key(root) -> None:
    with FakeBot(root) as fb:
        r = run_tool(root, "12", "hello")
    assert r.returncode == 0, r.stderr
    assert "quote_msg_id" not in fb.jobs[0]


def test_quote_combines_with_text_only_who_and_html(root, tmp_path) -> None:
    html = tmp_path / "full.html"
    html.write_text("<b>full</b>")
    with FakeBot(root) as fb:
        r = run_tool(root, "--who", "Ada", "--quote", "7", "--html", str(html), "--text-only", "owner", "hi")
    assert r.returncode == 0, r.stderr
    job = job_of(fb)
    assert job["quote_msg_id"] == 7 and job["no_speak"] is True and job["who"] == "Ada"
    assert job["html"] == "<b>full</b>"


def test_quote_combines_with_a_live_call(root) -> None:
    (root / "call-live.json").write_text(json.dumps({"call_id": 5600}))
    with FakeBot(root, lambda job: (True, "sent to chat 12, msg 9; spoken into the call")) as fb:
        r = run_tool(root, "--call", "5600", "--quote", "5531", "12", "on", "it")
    assert r.returncode == 0, r.stderr
    assert job_of(fb) == {"chat_id": 12, "text": "on it", "call_id": 5600, "quote_msg_id": 5531}
    assert "call 5600" in r.stdout and "quoting msg 5531" in r.stdout


@pytest.mark.parametrize("args", [
    ("--quote", "abc", "12", "hi"),
    ("--quote", "12abc", "12", "hi"),
    ("--quote", "-5", "12", "hi"),
    ("--quote", "0", "12", "hi"),
    ("--quote", "000", "12", "hi"),
    ("--quote", "", "12", "hi"),
    ("--quote",),
    ("--quote", "--text-only", "12", "hi"),
])
def test_a_bad_or_missing_quote_is_a_usage_error_and_queues_nothing(root, args) -> None:
    with FakeBot(root) as fb:
        r = run_tool(root, *args)
    assert r.returncode == 1, (args, r.returncode, r.stderr)
    assert "--quote" in r.stderr and "Nothing was queued" in r.stderr, r.stderr
    assert fb.jobs == [] and list((root / "outbox").glob("*.json")) == []


def test_a_quote_the_bot_refuses_is_exit_3_with_the_reason(root) -> None:
    why = "msg 900 is in chat 13, not chat 42: a reply quotes a message in its own chat; nothing was sent"
    with FakeBot(root, lambda job: (False, why)):
        r = run_tool(root, "--quote", "900", "family", "hi", wait="5")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "msg 900 is in chat 13" in r.stderr and r.stdout == ""


def test_a_quote_the_bot_refuses_on_a_call_is_exit_3(root) -> None:
    (root / "call-live.json").write_text(json.dumps({"call_id": 5600}))
    with FakeBot(root, lambda job: (False, "msg 424242 is not a message this account has")):
        r = run_tool(root, "--call", "5600", "--quote", "424242", "12", "hi")
    assert r.returncode == 3 and "NOT SENT - msg 424242" in r.stderr, r.stderr


def test_usage_and_header_name_quote() -> None:
    text = (BIN / "dc-say").read_text()
    head = text.split("set -euo pipefail")[0]
    assert "--quote MSG_ID" in head and "X-Delta-Chat-Msg-Id" in head and "exit 3" in head
    r = subprocess.run([str(BIN / "dc-say"), "--quote", "5", "12"], capture_output=True, text=True,
                       timeout=60, check=False, env={**os.environ, "DC_ROOT": "/nonexistent-quote"})
    assert r.returncode == 1 and "--quote MSG_ID" in r.stderr, r.stderr


# ---- the footer of a bridged mail -------------------------------------------

@pytest.fixture
def sent(monkeypatch, tmp_path):
    """mail_in()'s send, captured: (subject, body, headers) per mail. No mail leaves."""
    out = []

    class Sent:
        ok, id, error, dropped, notices = True, "<1@host>", "", (), ()

    def send(to, subject, body, **kw):
        out.append((subject, body, kw.get("headers") or {}))
        return Sent()
    monkeypatch.setattr(bot.seatmail, "send", send)
    monkeypatch.setattr(bot, "log", lambda m: None)
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setenv("DC_OUTBOX", str(tmp_path / "public" / "outbox"))
    return out


def foot(sent):
    (_, body, _), = sent
    return body.split("\n---\n", 1)[1]


def test_a_message_mail_footer_answers_its_line_as_a_quote(sent) -> None:
    bot.mail_in("family", 12, "Alice", "hello", kind="message", dc_msg_id=5531,
                channel=bot.Channel("family"))
    assert sent[0][2]["X-Delta-Chat-Msg-Id"] == "5531"
    assert f"To answer in that chat:\n  {dc_env.SAY} --quote 5531 family 'your reply'\n" in foot(sent)


def test_a_message_mail_with_no_channel_answer_still_quotes(sent) -> None:
    bot.mail_in("family", 12, "Alice", "hello", kind="message", dc_msg_id=5531)
    assert f"  {dc_env.SAY} --quote 5531 12 'your reply'\n" in foot(sent)


def test_a_reaction_mail_footer_quotes_the_reacted_message(sent) -> None:
    bot.mail_in("reaction", 12, "Alice", "Alice reacted +1 to message 44", kind="reaction",
                dc_msg_id=44, channel=bot.Channel("family"))
    assert f"  {dc_env.SAY} --quote 44 family 'your reply'\n" in foot(sent)


def test_the_public_profile_footer_quotes_by_its_outbox(sent, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bot, "PROFILE", "public")
    bot.mail_in("family", 12, "Alice", "hello", kind="message", dc_msg_id=5531,
                channel=bot.Channel("family"))
    box = tmp_path / "public" / "outbox"
    assert f"  DC_OUTBOX={box} {dc_env.SAY} --quote 5531 family 'your reply'\n" in foot(sent)
    assert "--profile" not in foot(sent)


@pytest.mark.parametrize("kind", ["webxdc", "call"])
def test_a_mail_with_no_msg_id_keeps_todays_footer(sent, kind) -> None:
    bot.mail_in(kind, 12, kind, "tap", kind=kind, dc_msg_id=5531, channel=bot.Channel("family"))
    assert "X-Delta-Chat-Msg-Id" not in sent[0][2]
    assert "--quote" not in foot(sent)
    assert f"  {dc_env.SAY} family 'your reply'\n" in foot(sent)


def test_a_call_line_keeps_the_mouth_footer(sent) -> None:
    bot.mail_in("Alice", 12, "Alice", "(on the call) hello", call=5600, kind="message", dc_msg_id=5531)
    assert "--quote" not in foot(sent)
    assert 'Answer with: mouth say --device call:5600 "text"' in foot(sent)


def test_a_call_mail_on_minted_ids_never_quotes(sent) -> None:
    bot.mail_in("chat12", 12, "call", "RINGING", msgid="ring-1@host", in_reply_to="call-1@host",
                subject="RINGING Alice", dc_msg_id=7, channel=bot.Channel("family"))
    assert "--quote" not in foot(sent)


def test_an_unlisted_chat_footer_names_no_dc_say(sent) -> None:
    bot.mail_in("stranger", 50, "Bob", "hi", kind="request", dc_msg_id=30,
                channel=bot.Channel(None, "not listed"))
    assert "not on the channels list" in foot(sent)
    assert "--quote" not in foot(sent) and "dc-say 50" not in foot(sent)


def test_reply_hint_takes_a_quote() -> None:
    assert dc_env.reply_hint(12, profile="", quote=5531) == f"{dc_env.SAY} --quote 5531 12 'your reply'"
    assert dc_env.reply_hint(12, profile="", channel="family", quote="44") == \
        f"{dc_env.SAY} --quote 44 family 'your reply'"
    assert dc_env.reply_hint(12, profile="", quote=None) == f"{dc_env.SAY} 12 'your reply'"
    lead, fallback = dc_env.reply_hint(12, call=5600, profile="", quote=5531).splitlines()
    assert "--quote" not in lead + fallback

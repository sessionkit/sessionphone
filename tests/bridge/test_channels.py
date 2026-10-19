"""The outbox gate (PHONE-102, design/allow-list.md rules 2-5, inventory rows 1-10).

One pass of main()'s outbox loop over fakes, on test_send_ack.py's rig: every
job kind that has a destination is checked against channels.txt BEFORE it
sends. A refused job sends NOTHING (the fakes record every send), and its ack
is ok:false naming the list. Fakes only: no account, no rpc-server, no call.
"""
import json
import os
import threading
import time

import pytest

from sessionphone.bridge import bot, calls

OWNER = "alice@relay.example"
STRANGER = "bob@relay.example"
LIST = f"family  chat:12   # the family group\nowner   {OWNER}   # owner\n"


class Done(Exception):
    """Ends main()'s loop at its first sleep: one pass of the outbox."""


class BotTime:
    """bot.py's `time`: the time module, but sleep raises Done. Only bot.py's
    own calls see it; the start probe's child wait and every thread keep the
    real one (patched process-wide, the pass could end inside the probe, PHONE-171)."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, s):
        raise Done


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeContact:
    def __init__(self, account, addr, name, chat_id):
        self.account, self.addr, self.name, self.chat_id = account, addr, name, chat_id

    def get_snapshot(self):
        return Obj(address=self.addr, display_name=self.name, name="")

    def create_chat(self):          # opens the 1:1; sends nothing
        return self.account.get_chat_by_id(self.chat_id)


class FakeMsg:
    def __init__(self, account, mid, chat_id):
        self.account, self.id, self.chat_id = account, mid, chat_id

    def get_snapshot(self):
        return Obj(chat_id=self.chat_id)

    def send_reaction(self, reaction):
        self.account.record("reaction", self.chat_id)
        return Obj(id=79)

    def send_webxdc_status_update(self, update, info):
        self.account.record("xdc", self.chat_id)


class FakeChat:
    def __init__(self, account, cid, chat_type, members):
        self.account, self.id, self.chat_type, self.members = account, cid, chat_type, members

    def get_basic_snapshot(self):
        return Obj(chat_type=self.chat_type)

    def get_contacts(self):
        return [self.account.contacts[a] for a in self.members]

    def get_messages(self):
        return [FakeMsg(self.account, 600 + self.id, self.id)]

    def send_text(self, text):
        self.account.record("text", self.id)
        return Obj(id=77)

    def send_message(self, **kw):
        self.account.record("message", self.id)
        return Obj(id=78)


class FakeAccount:
    """12 a listed group; 20 the owner's 1:1; 13 a stranger's 1:1; 43 a group with the owner in it."""

    def __init__(self):
        self.sent, self.on_send = [], None
        self.contacts = {OWNER: FakeContact(self, OWNER, "Alice", 20),
                         STRANGER: FakeContact(self, STRANGER, "Bob", 13)}
        self.chats = {12: FakeChat(self, 12, "Group", [OWNER, STRANGER]),
                      20: FakeChat(self, 20, "Single", [OWNER]),
                      13: FakeChat(self, 13, "Single", [STRANGER]),
                      43: FakeChat(self, 43, "Group", [OWNER])}
        self.msgs = {5: FakeMsg(self, 5, 12), 6: FakeMsg(self, 6, 13)}

    def record(self, kind, chat_id):
        self.sent.append((kind, chat_id))
        if self.on_send:
            self.on_send()

    def is_configured(self):
        return True

    def start_io(self):
        pass

    def get_config(self, key):
        return "carol@test"

    def get_contacts(self):
        return list(self.contacts.values())

    def get_contact_by_addr(self, addr):
        return self.contacts.get(addr)

    def get_chat_by_id(self, cid):
        if cid not in self.chats:
            raise ValueError(f"no chat {cid}")
        return self.chats[cid]

    def get_message_by_id(self, mid):
        if mid not in self.msgs:
            raise ValueError(f"no msg {mid}")
        return self.msgs[mid]


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Rig:
    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig()
    r.out, r.path, r.logs, r.hung, r.answered = tmp_path / "outbox", tmp_path / "channels.txt", [], [], []
    r.file = tmp_path / "note.txt"
    r.file.write_text("a file\n")
    r.account = FakeAccount()
    stop = threading.Event()
    monkeypatch.setenv("DC_CHANNELS_FILE", str(r.path))
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "OUTBOX", r.out)
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "DeltaChat", lambda rpc: Obj(get_all_accounts=lambda: [r.account]))
    monkeypatch.setattr(bot, "server_choice", lambda: (None, "package", "test"))
    monkeypatch.setattr(bot, "sweep", lambda account: None)
    monkeypatch.setattr(bot, "handle_events", lambda account: stop.wait())
    monkeypatch.setattr(bot, "CALLS", True)
    monkeypatch.setattr(bot, "INVITE", None)
    monkeypatch.setattr(bot, "log", r.logs.append)
    # the list alert (PHONE-129) goes by seatmail (PHONE-106): faked and recorded, never `message`
    r.mails = []
    monkeypatch.setattr(bot.seatmail, "send", lambda to, subject, body, **k: (
        r.mails.append(f"To: {to}\nSubject: {subject}\n\n{body}"), bot.seatmail.Sent(True, id="<1.2.3@host.session-mail>"))[1])
    # the call side, faked: what was placed, invited, hung up, answered
    monkeypatch.setattr(calls, "setup", lambda *a: None)
    monkeypatch.setattr(calls, "place", lambda acct, cid, g=None, v=None: r.account.record("place", cid) or True)
    monkeypatch.setattr(calls, "invite", lambda cid, addr: r.account.record("invite", cid) or True)
    monkeypatch.setattr(calls, "hangup", lambda cid: r.hung.append(cid) or True)
    monkeypatch.setattr(calls, "answer", lambda cid: (r.answered.append(cid), (True, "answered"))[1])
    monkeypatch.setattr(calls, "live_call_id", lambda cid: None)
    monkeypatch.setattr(calls, "speak_if_live", lambda *a: False)

    monkeypatch.setattr(bot, "time", BotTime())

    def write(text=LIST, mode=0o600):
        r.path.write_text(text)
        r.path.chmod(mode)
    r.write = write

    def run(*jobs):
        r.out.mkdir(parents=True, exist_ok=True)
        for i, given in enumerate(jobs):
            job = json.loads(json.dumps(given).replace('"FILE"', json.dumps(str(r.file))))
            job["ack"] = str(tmp_path / "acks" / f"{i:02d}.json")
            (r.out / f"{i:02d}.json").write_text(json.dumps(job))
        with pytest.raises(Done):
            bot.main()
        stop.set()
        assert not list(r.out.glob("*.json"))                # every job unlinked
        return [json.loads((tmp_path / "acks" / f"{i:02d}.json").read_text()) for i in range(len(jobs))]
    r.run = run
    yield r
    if r.path.exists():
        r.path.chmod(0o600)                                   # so tmp_path can be cleaned


# Every gated kind (inventory rows 1-8), each aimed at chat 13 (a stranger's
# 1:1, unlisted) or at msg 6, which is in chat 13.
GATED = {
    "say": {"chat_id": 13, "text": "hi"},
    "say-html": {"chat_id": 13, "text": "hi", "html": "<b>hi</b>"},
    "say-by-number-text": {"chat_id": "13", "text": "hi"},
    "send-file": {"chat_id": 13, "file": "FILE", "text": "a file"},
    "react-msg": {"msg_id": 6, "reaction": "+1"},
    "react-chat": {"chat_id": 13, "reaction": "+1"},
    "xdc-update": {"webxdc_update": 6, "payload": {"n": 1}},
    "dm": {"dm": "Bob", "text": "hi"},
    "ring": {"call_to": STRANGER},
    "call": {"call": 13},
    "invite": {"invite": "guest@example.org", "chat_id": 13},
}
# The same kinds aimed at what IS listed: chat 12 (chat:12), msg 5 (in chat
# 12), and Alice, whose 1:1 is chat 20 (the owner address entry).
LISTED = {
    "say": {"chat_id": 12, "text": "hi"},
    "send-file": {"chat_id": 12, "file": "FILE"},
    "react-msg": {"msg_id": 5, "reaction": "+1"},
    "react-chat": {"chat_id": 12, "reaction": "+1"},
    "xdc-update": {"webxdc_update": 5, "payload": {"n": 1}},
    "dm": {"dm": "Alice", "text": "hi"},
    "ring": {"call_to": "Alice"},
    "call": {"call": 12},
    "invite": {"invite": "guest@example.org", "chat_id": 12},
}


def refused_lines(rig):
    return [m for m in rig.logs if m.startswith("REFUSED")]


# ---- listed sends work ----

def test_a_listed_send_goes_exactly_once(rig) -> None:
    rig.write()
    acks = rig.run({"chat_id": 12, "text": "hi"},          # chat:12, by number
                   {"chat": "owner", "text": "hi"},        # owner <addr>, by NAME
                   {"chat_id": 20, "text": "hi"})          # a raw number: the address's 1:1
    assert [a["ok"] for a in acks] == [True, True, True], acks
    assert acks[1]["detail"] == "sent to chat 20, msg 77"
    assert rig.account.sent == [("text", 12), ("text", 20), ("text", 20)]
    assert not refused_lines(rig)


def test_every_listed_kind_sends(rig) -> None:
    rig.write()
    acks = rig.run(*LISTED.values())
    assert all(a["ok"] for a in acks), acks
    assert rig.account.sent == [("text", 12), ("message", 12), ("reaction", 12), ("reaction", 12),
                                ("xdc", 12), ("text", 20), ("place", 20), ("place", 12), ("invite", 12)]


# ---- an unlisted send is refused, for EVERY gated kind ----

@pytest.mark.parametrize("kind", GATED)
def test_an_unlisted_send_is_refused_and_nothing_goes(rig, kind) -> None:
    rig.write()
    (ack,) = rig.run(GATED[kind])
    assert rig.account.sent == []                            # ZERO sends
    assert ack["ok"] is False
    assert str(rig.path) in ack["detail"] and "nothing was sent" in ack["detail"], ack
    assert ack["detail"].startswith("REFUSED: chat 13 is not on the allow-list")
    assert any(m.startswith(f"REFUSED send to chat 13: not in {rig.path}") for m in rig.logs), rig.logs
    assert not any(m.startswith("SEND FAILED") for m in rig.logs)


def test_an_address_never_passes_a_group_that_contact_is_in(rig) -> None:
    rig.write()                                             # owner is listed; chat 43 is a group with them in it
    acks = rig.run({"chat_id": 43, "text": "hi"}, {"call": 43}, {"chat_id": 43, "reaction": "+1"})
    assert [a["ok"] for a in acks] == [False, False, False]
    assert rig.account.sent == []
    assert all(str(rig.path) in a["detail"] for a in acks)


# ---- it fails closed ----

@pytest.mark.parametrize("state", ["missing", "unreadable", "0660"])
def test_a_missing_or_bad_list_refuses_every_send(rig, state) -> None:
    if state == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root reads a 000 file")
        rig.write(mode=0o000)
    elif state == "0660":
        rig.write(mode=0o660)
    acks = rig.run(*LISTED.values(), {"chat": "owner", "text": "hi"})
    assert rig.account.sent == []
    for a in acks:
        assert a["ok"] is False and str(rig.path) in a["detail"] and "nothing was sent" in a["detail"], a
    want = {"missing": f"NO channels file at {rig.path}", "unreadable": f"cannot read {rig.path}",
            "0660": "mode 0660"}[state]
    assert all(want in a["detail"] for a in acks), acks


def test_hangup_and_answer_work_with_no_list(rig) -> None:
    acks = rig.run({"hangup": 13}, {"answer": 13})
    assert [a["ok"] for a in acks] == [True, True], acks
    assert rig.hung == [13] and rig.answered == [13]


@pytest.mark.parametrize("job", [{"text": "hi"}, {"bogus": 1}, {"chat": "", "text": "hi"}])
def test_a_job_with_no_destination_is_refused(rig, job) -> None:
    rig.write()
    (ack,) = rig.run(job)
    assert ack["ok"] is False and ack["detail"] == "REFUSED: this job names no chat; nothing was sent."
    assert rig.account.sent == []
    assert "REFUSED job with no destination" in rig.logs


# ---- a NAME (rule 2) ----

def test_a_name_is_resolved_in_every_field_that_takes_a_chat(rig) -> None:
    rig.write()
    acks = rig.run({"call": "family"},                      # the kind's own field holds the NAME
                   {"call": None, "chat": "owner"},         # or "chat" does
                   {"invite": "guest@example.org", "chat": "family"},
                   {"chat": "family", "file": "FILE"},
                   {"chat": "family", "reaction": "+1"},
                   {"hangup": "family"},
                   {"answer": "owner"})
    assert all(a["ok"] for a in acks), acks
    assert rig.account.sent == [("place", 12), ("place", 20), ("invite", 12), ("message", 12), ("reaction", 12)]
    assert rig.hung == [12] and rig.answered == [20]


def test_an_unknown_name_is_refused_naming_the_list(rig) -> None:
    rig.write()
    (ack,) = rig.run({"chat": "nobody", "text": "hi"})
    assert ack["ok"] is False and str(rig.path) in ack["detail"] and "'nobody'" in ack["detail"]
    assert rig.account.sent == []
    assert f"REFUSED send to channel 'nobody': not in {rig.path}" in rig.logs


def test_a_name_with_no_list_is_refused(rig) -> None:
    (ack,) = rig.run({"chat": "owner", "text": "hi"})
    assert ack["ok"] is False and f"NO channels file at {rig.path}" in ack["detail"]
    assert rig.account.sent == []


def test_a_listed_address_this_profile_never_met_is_refused(rig) -> None:
    rig.write(LIST + "ghost  ghost@example.org\n")
    (ack,) = rig.run({"chat": "ghost", "text": "hi"})
    assert ack["ok"] is False and "not a contact of this profile" in ack["detail"]
    assert rig.account.sent == []


def test_dm_takes_a_channel_name_too(rig) -> None:
    rig.write()
    acks = rig.run({"dm": "family", "text": "hi"}, {"dm": "owner", "text": "hi"})
    assert [a["ok"] for a in acks] == [True, True], acks
    assert acks[0]["detail"] == "sent to channel family, chat 12, msg 77"
    assert rig.account.sent == [("text", 12), ("text", 20)]


# ---- the file is re-read every job, and its changes are logged by name ----

def test_the_start_log_counts_the_list(rig) -> None:
    rig.write()
    rig.run()
    assert f"channels: 2 listed from {rig.path}" in rig.logs


def test_the_start_log_is_loud_with_no_list(rig) -> None:
    rig.run()
    assert f"channels: NO channels file at {rig.path}: every send is refused" in rig.logs


def test_an_edit_takes_effect_at_the_next_job_and_is_logged(rig) -> None:
    rig.write()
    # the owner takes family off the list the moment the first send goes
    rig.account.on_send = lambda: rig.write(f"owner  {OWNER}\ncarol  chat:7\n")
    acks = rig.run({"chat_id": 12, "text": "one"}, {"chat_id": 12, "text": "two"})
    assert [a["ok"] for a in acks] == [True, False]
    assert rig.account.sent == [("text", 12)]
    assert f"channels: {rig.path} changed: added carol (chat:7); removed family (chat:12)" in rig.logs


def test_changes_are_logged_once_by_name(rig) -> None:
    rig.write()
    w = bot.ChannelsWatch()
    w.refresh()
    rig.write("family  chat:14\ncarol  chat:7\n")
    w.refresh()
    w.refresh()                                             # no change: nothing more
    os.unlink(rig.path)
    w.refresh()
    rig.write()
    w.refresh()
    assert rig.logs == [
        f"channels: 2 listed from {rig.path}",
        (f"channels: {rig.path} changed: added carol (chat:7); removed owner ({OWNER}); "
         "changed family (chat:12 -> chat:14)"),
        f"channels: NO channels file at {rig.path}: every send is refused",
        f"channels: 2 listed from {rig.path}",
    ]


def test_a_bad_line_is_logged_once_not_every_job(rig) -> None:
    rig.write(LIST + "Bad Line Here\n")
    acks = rig.run({"chat_id": 12, "text": "a"}, {"chat_id": 12, "text": "b"}, {"chat_id": 12, "text": "c"})
    assert all(a["ok"] for a in acks)
    assert len([m for m in rig.logs if m.startswith("IGNORED line 3")]) == 1, rig.logs


# ---- ONE digit rule (do-007, from the do-003 review, advisory 2) ----
# The tools read a chat id with channels.is_chat_id (ASCII digits only). The
# bot used bare str.isdigit(), which also takes "١٢" or "１２" - and int() of
# those is 12. Now both sides use the one rule: those strings are NAMEs.

@pytest.mark.parametrize("ref", ["١٢", "１２"])        # Arabic-Indic and fullwidth "12"
def test_a_non_ascii_digit_string_is_a_name_not_a_chat_id(rig, ref) -> None:
    rig.write()
    listing = bot.channels.load(rig.path)
    with pytest.raises(bot.channels.Refused, match="no channel"):
        bot.resolve_chat(rig.account, ref, listing)
    assert bot.resolve_chat(rig.account, "12", listing) == 12     # ASCII digits: still a chat id
    (ack,) = rig.run({"chat_id": ref, "text": "hi"})                # chat:12 is listed; this is not it
    assert ack["ok"] is False and "no channel" in ack["detail"]
    assert rig.account.sent == []


# ---- row 10: the TTS fallback is an ordinary outbox job, so the gate covers it ----
# (do-004.) When TTS fails mid-call, CallSession.speak() writes the words it
# could not say to the outbox as a chat_id job. That job meets the same gate
# as dc-say: an unlisted call chat gets nothing, and the refusal is logged.

def _tts_fallback_job(rig, monkeypatch, chat_id):
    """Drive the REAL speak() into its fallback; the job lands in rig.out."""
    import asyncio
    monkeypatch.setenv("DC_OUTBOX", str(rig.out))
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(rig.logs.append, lambda p: p, lambda *a, **k: None))

    def refused(text, voice):
        raise RuntimeError("tts curl rc=7 bytes=0")
    monkeypatch.setattr(calls, "tts_pcm48", refused)
    s = object.__new__(calls.CallSession)
    s.chat_id, s.lines = chat_id, []
    asyncio.run(s.speak("Hi Alice."))
    (job,) = [json.loads(p.read_text()) for p in rig.out.glob("*.json")]
    assert job["chat_id"] == chat_id and job["text"].startswith("(unspoken, TTS failed) ")
    return job


def test_tts_fallback_to_an_unlisted_chat_is_refused(rig, monkeypatch) -> None:
    rig.write()
    _tts_fallback_job(rig, monkeypatch, 13)
    rig.run()                                                # one outbox pass over that job
    assert rig.account.sent == []                            # ZERO sends
    assert any(m.startswith(f"REFUSED send to chat 13: not in {rig.path}") for m in rig.logs), rig.logs


def test_tts_fallback_to_a_listed_chat_is_sent_once(rig, monkeypatch) -> None:
    rig.write()
    _tts_fallback_job(rig, monkeypatch, 12)
    rig.run()
    assert rig.account.sent == [("text", 12)]
    assert not refused_lines(rig)

"""PHONE-106 do-005, contract §3: the headers an agent can filter on.

    X-Delta-Chat-Kind     message | request | reaction | webxdc | call
    X-Delta-Chat-Msg-Id   the Delta message id; message, request, reaction only
    X-Delta-Chat-Sender   the Delta contact's address
    X-Delta-Chat-Channel  the PHONE-102 channel NAME the chat is listed as

and the reply hint, which names that channel, or says the chat is not listed
instead of giving a dc-say the outbox would refuse. Existing headers, the Subject
form, the body and the --- footer are unchanged.

Each of M1-M4 runs the REAL bot path (handle_events / sweep / xdc_update) and the
REAL mail_in, into a fake `message` that records every argv and body. So does M5,
the call mails (by `message send --message-id` since PHONE-72 part 2b). Fakes
only: no account, rpc-server, sendmail, message or seat key.
"""
import email
import email.policy
import json
import types

import pytest

from sessionphone.bridge import bot, calls, dc_env

OWNER = "alice@relay.example"
STRANGER = "bob@relay.example"
# family is a listed group, owner is the owner's 1:1 (an address entry)
LIST = f"family  chat:12   # the family group\nowner   {OWNER}   # owner\n"
NEW = ("X-Delta-Chat-Kind", "X-Delta-Chat-Msg-Id", "X-Delta-Chat-Sender", "X-Delta-Chat-Channel")


class Done(Exception):
    """The fake's events ran out: handle_events() loops forever."""


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeContact:
    def __init__(self, addr, name):
        self.addr, self.name = addr, name

    def get_snapshot(self):
        return Obj(address=self.addr, display_name=self.name, name="")


CONTACTS = {OWNER: FakeContact(OWNER, "Alice"), STRANGER: FakeContact(STRANGER, "Bob")}


class FakeChat:
    def __init__(self, cid, chat_type, members, request=False):
        self.id, self.members = cid, members
        self.info = Obj(name=f"chat{cid}", chat_type=chat_type, is_contact_request=request)

    def get_basic_snapshot(self):
        return self.info

    def get_contacts(self):
        return [CONTACTS[a] for a in self.members]

    def accept(self):
        self.info.is_contact_request = False


class FakeMsg:
    def __init__(self, mid, chat_id, sender, text="hello there", updates=()):
        self.id, self.updates = mid, list(updates)
        self.snap = Obj(id=mid, chat_id=chat_id, text=text, file=None, file_mime="",
                        view_type="Text", quote=None, sender=sender)

    def get_snapshot(self):
        return self.snap

    def get_webxdc_status_updates(self, since=0):
        return [u for u in self.updates if u["serial"] > since]


class FakeAccount:
    """12 the listed group `family`; 20 the owner's 1:1 (listed as `owner`);
    43 an unlisted group; 50 a stranger's contact request (unlisted)."""

    def __init__(self):
        self.chats = {12: FakeChat(12, "Group", [OWNER, STRANGER]),
                      20: FakeChat(20, "Single", [OWNER]),
                      43: FakeChat(43, "Group", [OWNER]),
                      50: FakeChat(50, "Single", [STRANGER], request=True)}
        self.contacts_by_id = {1: CONTACTS[OWNER], 2: CONTACTS[STRANGER]}
        self.msgs, self.events, self.fresh = {}, [], []

    def add(self, mid, chat_id, addr=OWNER, **kw):
        self.msgs[mid] = FakeMsg(mid, chat_id, CONTACTS[addr], **kw)
        return self.msgs[mid]

    def arrive(self, mid):
        m = self.msgs[mid]
        self.events.append(Obj(kind=bot.EventType.INCOMING_MSG, msg_id=mid, chat_id=m.snap.chat_id))
        self.fresh.append(m)

    def wait_for_event(self):
        if not self.events:
            raise Done
        return self.events.pop(0)

    def get_message_by_id(self, mid):
        return self.msgs[mid]

    def get_chat_by_id(self, cid):
        if cid not in self.chats:
            raise ValueError(f"no chat {cid}")
        return self.chats[cid]

    def get_contact_by_id(self, cid):
        return self.contacts_by_id[cid]

    def get_fresh_messages(self):
        return list(self.fresh)

    def mark_seen_messages(self, msgs):
        for m in msgs:
            if m in self.fresh:
                self.fresh.remove(m)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """The PRIVATE profile (inbound not gated), LIST as channels.txt (0600), and a fake
    `message` that appends {argv, body} per send to sends.jsonl and prints --json's yes."""
    lst = tmp_path / "channels.txt"
    lst.write_text(LIST)
    lst.chmod(0o600)
    rec = tmp_path / "sends.jsonl"
    exe = tmp_path / "bin" / "message"
    exe.parent.mkdir()
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(rec)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'body': sys.stdin.read()}}) + '\\n')\n"
        "print(json.dumps({'sent': 'yes', 'id': '<1.2.3@host.session-mail>', 'notices': []}))\n")
    exe.chmod(0o755)
    monkeypatch.setenv("DC_MESSAGE", str(exe))
    monkeypatch.setenv("DC_CHANNELS_FILE", str(lst))
    monkeypatch.setattr(dc_env, "ADDR", "ada-chat@example.net")
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Ada")
    monkeypatch.setattr(dc_env, "MAILTO", "ada@example.net")
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", False)
    monkeypatch.setattr(bot, "SEEN", tmp_path / "bridged-ids.txt")
    monkeypatch.setattr(bot, "XDC_SEEN", {})
    monkeypatch.setattr(bot, "XDC_SENT", {})
    monkeypatch.setattr(bot, "XDC_IN", None)
    monkeypatch.setattr(bot, "XDC_MATCH", [])
    logs = []
    monkeypatch.setattr(bot, "log", logs.append)
    r = types.SimpleNamespace(account=FakeAccount(), list=lst, logs=logs)

    def sends():
        return [json.loads(ln) for ln in rec.read_text().splitlines()] if rec.exists() else []
    r.sends = sends

    def run():
        with pytest.raises(Done):
            bot.handle_events(r.account)
    r.run = run
    return r


def heads(argv):
    """--header values as {name: value}, in order."""
    out = {}
    for i, a in enumerate(argv):
        if a == "--header":
            k, _, v = argv[i + 1].partition(": ")
            out[k] = v
    return out


def footer(body):
    return body.split("\n---\n", 1)[1]


def one(rig):
    sends = rig.sends()
    assert len(sends) == 1, sends
    return sends[0]["argv"], heads(sends[0]["argv"]), sends[0]["body"]


# ---- one test per M1-M4: Kind, Msg-Id and Sender present or absent ----------------

def test_m1_a_chat_message_is_kind_message_with_its_id_sender_and_channel(rig):
    rig.account.add(10, 12)                 # Alice, in the listed group `family`
    rig.account.arrive(10)
    rig.run()
    argv, h, body = one(rig)
    assert h == {"X-Delta-Chat-Bridge": "1", "X-Delta-Chat-Id": "12",       # unchanged, first
                 "X-Delta-Chat-Kind": "message", "X-Delta-Chat-Msg-Id": "10",
                 "X-Delta-Chat-Sender": OWNER, "X-Delta-Chat-Channel": "family"}
    assert argv[argv.index("-s") + 1] == "[chat12 12] Alice: hello there"    # Subject form unchanged
    assert body.startswith("hello there\n\n---\nchat: chat12 (id 12)\nfrom: Alice\n\n")
    assert footer(body).endswith("To answer in that chat:\n  dc-say family 'your reply'\n")


def test_m1_the_startup_sweep_carries_the_same_headers(rig):
    rig.account.add(11, 20)                 # the owner's 1:1, listed by address as `owner`
    rig.account.fresh.append(rig.account.msgs[11])
    bot.sweep(rig.account)
    _, h, body = one(rig)
    assert (h["X-Delta-Chat-Kind"], h["X-Delta-Chat-Msg-Id"], h["X-Delta-Chat-Sender"],
            h["X-Delta-Chat-Channel"]) == ("message", "11", OWNER, "owner")
    assert "  dc-say owner 'your reply'\n" in footer(body)


def test_m2_a_held_request_is_kind_request_and_its_chat_is_not_listed(rig):
    rig.account.add(30, 50, addr=STRANGER, text="please call me")
    rig.account.arrive(30)
    rig.run()
    argv, h, body = one(rig)
    assert h["X-Delta-Chat-Kind"] == "request"
    assert h["X-Delta-Chat-Msg-Id"] == "30"
    assert h["X-Delta-Chat-Sender"] == STRANGER
    assert "X-Delta-Chat-Channel" not in h                  # chat 50 is not listed
    assert argv[argv.index("-s") + 1].startswith("[chat50 50] REQUEST Bob: please call me")
    tail = footer(body)
    assert "dc-say 50" not in tail and "dc-say chat" not in tail   # no hint that will be refused
    assert "not on the channels list" in tail and f"not in {rig.list}" in tail
    assert "sessionphone channels add NAME chat:50" in tail


def test_m2_the_sweep_mails_a_held_request_with_its_msg_id(rig):
    rig.account.add(31, 50, addr=STRANGER)
    rig.account.fresh.append(rig.account.msgs[31])
    bot.sweep(rig.account)
    _, h, _ = one(rig)
    assert (h["X-Delta-Chat-Kind"], h["X-Delta-Chat-Msg-Id"], h["X-Delta-Chat-Sender"]) == (
        "request", "31", STRANGER)


def test_m3_a_reaction_is_kind_reaction_with_the_reacted_message_id(rig):
    rig.account.events.append(Obj(kind=bot.EventType.INCOMING_REACTION, chat_id=20, contact_id=1,
                                  msg_id=7, reaction="+1"))
    rig.run()
    _, h, body = one(rig)
    assert h["X-Delta-Chat-Kind"] == "reaction"
    assert h["X-Delta-Chat-Msg-Id"] == "7"               # what dc-react --msg takes
    assert h["X-Delta-Chat-Sender"] == OWNER
    assert h["X-Delta-Chat-Channel"] == "owner"
    assert body.startswith("Alice reacted +1 to message 7\n\n---\n")


def test_m4_a_tap_as_mail_is_kind_webxdc_with_no_msg_id_and_no_sender(rig):
    rig.account.msgs[900] = FakeMsg(900, 43, CONTACTS[OWNER],
                                    updates=[{"serial": 101, "payload": {"tap": "yes"}, "info": ""}])
    rig.account.events.append(Obj(kind=bot.EventType.WEBXDC_STATUS_UPDATE, msg_id=900,
                                  status_update_serial=101))
    rig.run()
    _, h, body = one(rig)
    assert h["X-Delta-Chat-Kind"] == "webxdc"
    assert "X-Delta-Chat-Msg-Id" not in h and "X-Delta-Chat-Sender" not in h   # the tapper is unknown
    assert "X-Delta-Chat-Channel" not in h                                     # chat 43 is not listed
    assert "sessionphone channels add NAME chat:43" in footer(body)


def test_m4_a_tap_in_a_listed_chat_names_its_channel(rig):
    rig.account.msgs[901] = FakeMsg(901, 12, CONTACTS[OWNER],
                                    updates=[{"serial": 5, "payload": {"tap": 1}, "info": ""}])
    rig.account.events.append(Obj(kind=bot.EventType.WEBXDC_STATUS_UPDATE, msg_id=901,
                                  status_update_serial=5))
    rig.run()
    _, h, body = one(rig)
    assert h["X-Delta-Chat-Channel"] == "family" and "  dc-say family 'your reply'\n" in footer(body)


# ---- M5: the call mails carry the kind, and nothing else new ------------------------

def test_m5_a_call_mail_is_kind_call_with_no_msg_id_sender_or_channel(rig):
    assert bot.mail_in("chat12", 12, "call", "RINGING", msgid="ring-call-12-1@host",
                       in_reply_to="call-12-1@host", subject="RINGING Alice",
                       dc_msg_id=7, channel=bot.Channel("family")) is True
    (send,) = rig.sends()                                     # by message send (part 2b)
    h = heads(send["argv"])
    assert list(h)[:3] == ["X-Delta-Chat-Bridge", "X-Delta-Chat-Id", "X-Delta-Chat-Kind"]
    assert h["X-Delta-Chat-Kind"] == "call" and h["X-Delta-Chat-Id"] == "12"
    assert not any(n in h for n in NEW[1:])                   # an M5 names no Msg-Id or Channel


# ---- the channel header and hint, listed and not -----------------------------------

def test_no_channels_file_still_mails_and_says_why(rig):
    rig.list.unlink()
    rig.account.add(12, 12)
    rig.account.arrive(12)
    rig.run()
    _, h, body = one(rig)
    assert "X-Delta-Chat-Channel" not in h and h["X-Delta-Chat-Kind"] == "message"
    assert f"NO channels file at {rig.list}" in footer(body)
    assert "dc-say 12" not in footer(body)


def test_the_public_profile_hint_names_its_outbox_and_its_list(rig, monkeypatch):
    monkeypatch.setattr(bot, "PROFILE", "public")
    outbox = dc_env.path("OUTBOX", "outbox")
    listed = bot.channel_hint(12, bot.Channel("family", "", rig.list))
    assert listed == f"DC_OUTBOX={outbox} dc-say family 'your reply'"
    unlisted = bot.channel_hint(43, bot.Channel(None, f"not in {rig.list}", rig.list))
    assert f"DC_CHANNELS_FILE={rig.list} sessionphone channels add NAME chat:43" in unlisted


def test_chat_channel_asks_as_the_outbox_asks(rig):
    a = rig.account
    assert bot.chat_channel(a, 12) == bot.Channel("family", "", rig.list)
    assert bot.chat_channel(a, 20) == bot.Channel("owner", "", rig.list)     # 1:1 by address
    assert bot.chat_channel(a, 43).name is None                              # the owner's GROUP is not
    gone = bot.chat_channel(a, 999)                                          # no such chat: not listed
    assert gone.name is None and "could not read the chat" in gone.why and "no chat 999" in gone.why


def test_a_chat_channel_that_raises_is_not_listed_and_the_mail_still_goes(rig, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("core gone")
    monkeypatch.setattr(bot.channels, "check", boom)
    ch = bot.chat_channel(rig.account, 12)
    assert ch.name is None and "RuntimeError: core gone" in ch.why
    assert bot.mail_in("chat12", 12, "Alice", "hi", kind="message", channel=ch) is True
    assert "X-Delta-Chat-Channel" not in heads(rig.sends()[0]["argv"])


# ---- the values: one ASCII line, and only where the contract puts them ---------------

def test_a_sender_a_stranger_chose_is_one_ascii_line(rig):
    assert bot.mail_in("c", 13, "Bob", "hi", kind="message", dc_msg_id=5,
                       dc_sender="bob@relay.example\nX-Evil: 1 \u00e9") is True
    h = heads(rig.sends()[0]["argv"])
    assert h["X-Delta-Chat-Sender"] == "bob@relay.example X-Evil: 1 ?"
    assert "X-Evil" not in h


@pytest.mark.parametrize("kind, mid, want", [
    ("message", 5, {"X-Delta-Chat-Kind": "message", "X-Delta-Chat-Msg-Id": "5"}),
    ("webxdc", 5, {"X-Delta-Chat-Kind": "webxdc"}),                 # Msg-Id: message/request/reaction only
    ("call", 5, {"X-Delta-Chat-Kind": "call"}),
    ("chat", 5, {}),                                                # not a contract kind
    (None, 5, {}),
    ("reaction", "5; x", {"X-Delta-Chat-Kind": "reaction"}),        # an id is digits, or it is left out
    ("request", None, {"X-Delta-Chat-Kind": "request"}),
])
def test_contract_headers_only_where_the_contract_puts_them(kind, mid, want):
    assert bot.contract_headers(kind, mid) == want


def test_a_sender_of_question_mark_is_no_sender():
    assert bot.contract_headers("request", 1, "?") == {"X-Delta-Chat-Kind": "request",
                                                       "X-Delta-Chat-Msg-Id": "1"}


def test_a_bare_mail_in_is_as_before(rig):
    """A caller that passes none of the new arguments gets today's mail exactly."""
    assert bot.mail_in("Team", 12, "Alice", "hello there") is True
    _, h, body = one(rig)
    assert h == {"X-Delta-Chat-Bridge": "1", "X-Delta-Chat-Id": "12"}
    assert footer(body).endswith("To answer in that chat:\n  dc-say 12 'your reply'\n")


# ---- the footer cut: the mail leg still stops at --- -------------------------------

def test_the_mail_leg_still_speaks_only_the_text_above_the_footer(rig):
    rig.account.add(10, 43, text="say this")                # an unlisted chat: the longest footer
    rig.account.arrive(10)
    rig.run()
    _, _, body = one(rig)
    msg = email.message_from_string("Subject: x\nContent-Type: text/plain; charset=utf-8\n\n" + body,
                                    policy=email.policy.default)
    assert calls.mail_speech(msg) == "say this"


def test_reply_hint_takes_a_channel_name_but_a_call_ignores_it():
    assert dc_env.reply_hint(12, profile="", channel="family") == "dc-say family 'your reply'"
    assert dc_env.reply_hint(12, profile="") == "dc-say 12 'your reply'"
    assert dc_env.reply_hint(12, call=5, profile="", channel="family") == dc_env.reply_hint(12, call=5, profile="")

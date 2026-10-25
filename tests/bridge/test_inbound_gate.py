"""The INBOUND gate on the PUBLIC profile (PHONE-102 do-008).

Strangers on the public profile are logged, never forwarded: the record says
that each was rejected, and why. With PROFILE set, a message, contact request,
reaction or webxdc tap from a chat NOT on that profile's channels.txt makes
ZERO mail_in calls and ZERO xdc-in lines, and exactly ONE rejected.jsonl
record, naming the reason and the path, with NO message text. A listed chat is
unchanged; the PRIVATE profile (PROFILE unset) is not gated at all.

Fakes only: no account, no rpc-server, no whisper, no sendmail.
"""
import json
import os
import stat

import _keyfakes
import pytest

from sessionphone import channels
from sessionphone.bridge import bot

OWNER = "alice@relay.example"
STRANGER = "bob@relay.example"
SECRET = "the vault code is 4471"      # message text: must never be stored or logged
LIST = f"family  chat:12   # the family group\nowner   {OWNER}  key:{_keyfakes.FPR_K1}   # owner\n"


class Done(Exception):
    """The fake's events ran out: handle_events() loops forever."""


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeContact:
    def __init__(self, addr, name):
        self.addr, self.name = addr, name

    def get_snapshot(self):
        return Obj(address=self.addr, display_name=self.name, name="", is_key_contact=True)

    def make_vcard(self):           # the owner holds K1, anyone else K2
        return _keyfakes.vcard(self.addr, _keyfakes.K1 if self.addr == OWNER else _keyfakes.K2)


class FakeChat:
    def __init__(self, cid, chat_type, members, request=False):
        self.id, self.chat_type, self.members = cid, chat_type, members
        self.info = Obj(name=f"chat{cid}", chat_type=chat_type, is_contact_request=request)
        self.accepted = False

    def get_basic_snapshot(self):
        return self.info

    def get_contacts(self):
        return [CONTACTS[a] for a in self.members]

    def accept(self):
        self.accepted = True
        self.info.is_contact_request = False


class FakeMsg:
    def __init__(self, mid, chat_id, sender, text=SECRET, updates=()):
        self.id, self.updates = mid, list(updates)
        self.snap = Obj(id=mid, chat_id=chat_id, text=text, file=None, file_mime="",
                        view_type="Text", quote=None, sender=sender)

    def get_snapshot(self):
        return self.snap

    def get_webxdc_status_updates(self, since=0):
        return [u for u in self.updates if u["serial"] > since]


CONTACTS = {OWNER: FakeContact(OWNER, "Alice"), STRANGER: FakeContact(STRANGER, "Bob")}


class FakeAccount:
    """12 a listed group; 20 the owner's 1:1; 13 a stranger's 1:1; 43 an unlisted
    group the owner is in; 50 a stranger's contact request; 51 an owner's request."""

    def __init__(self):
        self.chats = {12: FakeChat(12, "Group", [OWNER, STRANGER]),
                      20: FakeChat(20, "Single", [OWNER]),
                      13: FakeChat(13, "Single", [STRANGER]),
                      43: FakeChat(43, "Group", [OWNER]),
                      50: FakeChat(50, "Single", [STRANGER], request=True),
                      51: FakeChat(51, "Single", [OWNER], request=True)}
        self.contacts_by_id = {1: CONTACTS[OWNER], 2: CONTACTS[STRANGER]}
        self.msgs, self.events, self.fresh, self.marked = {}, [], [], []

    def add(self, mid, chat_id, addr=STRANGER, **kw):
        self.msgs[mid] = FakeMsg(mid, chat_id, CONTACTS[addr], **kw)
        return self.msgs[mid]

    def arrive(self, mid):
        m = self.msgs[mid]
        self.events.append(Obj(kind=bot.EventType.INCOMING_MSG, msg_id=mid, chat_id=m.snap.chat_id))
        self.fresh.append(m)

    def react(self, chat_id, contact_id, msg_id=7):
        self.events.append(Obj(kind=bot.EventType.INCOMING_REACTION, chat_id=chat_id,
                               contact_id=contact_id, msg_id=msg_id, reaction="+1"))

    # -- the account surface bot.py uses
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
            self.marked.append(m.id)
            if m in self.fresh:
                self.fresh.remove(m)


class Rig:
    def run(self):
        with pytest.raises(Done):
            bot.handle_events(self.account)

    def records(self):
        p = self.rejected
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """The PUBLIC profile with LIST as its channels.txt (0600), every mail_in,
    log line and xdc-in line recorded."""
    r = Rig()
    r.account = FakeAccount()
    r.path = tmp_path / "public" / "channels.txt"
    r.path.parent.mkdir(mode=0o700)
    r.path.write_text(LIST)
    r.path.chmod(0o600)
    r.rejected = r.path.parent / "rejected.jsonl"
    r.xdc_in = tmp_path / "xdc-in.jsonl"
    r.mailed, r.logs = [], []
    monkeypatch.setenv("DC_CHANNELS_FILE", str(r.path))
    monkeypatch.setattr(bot, "PROFILE", "public")
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", False)
    monkeypatch.setattr(bot, "SEEN", tmp_path / "bridged-ids.txt")
    monkeypatch.setattr(bot, "XDC_SEEN", {})
    monkeypatch.setattr(bot, "XDC_SENT", {})
    monkeypatch.setattr(bot, "XDC_IN", None)
    monkeypatch.setattr(bot, "XDC_MATCH", [])
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: r.mailed.append((a, k)))
    monkeypatch.setattr(bot, "log", r.logs.append)
    monkeypatch.setattr(bot, "transcribe", lambda path: SECRET)
    return r


def _xdc_event(account, chat_id, mid=900, serials=(101,), payload=None):
    payload = payload or {"from": "xdc", "kind": "set", "secret": SECRET}
    account.msgs[mid] = FakeMsg(mid, chat_id, CONTACTS[STRANGER],
                                updates=[{"serial": s, "payload": payload, "info": ""} for s in serials])
    for s in serials:
        account.events.append(Obj(kind=bot.EventType.WEBXDC_STATUS_UPDATE, msg_id=mid,
                                  status_update_serial=s))


def _assert_one_clean_record(rig, kind, chat_id, addr, name):
    recs = rig.records()
    assert len(recs) == 1, recs
    rec = recs[0]
    assert rec["kind"] == kind and rec["chat_id"] == chat_id and rec["profile"] == "public"
    assert rec["address"] == addr and rec["name"] == name
    assert str(rig.path) in rec["reason"] and f"chat {chat_id}" in rec["reason"]
    assert set(rec) == {"ts", "profile", "chat_id", "address", "name", "kind", "reason"}
    raw = rig.rejected.read_text()
    assert SECRET not in raw and "4471" not in raw          # NO message text, ever
    assert not any(SECRET in line for line in rig.logs)     # nor in the bot's log
    assert stat.S_IMODE(rig.rejected.stat().st_mode) == 0o600


# --- public profile, unlisted chat: recorded, never passed on ---------------------------

def test_an_unlisted_message_is_recorded_not_mailed(rig):
    rig.account.add(10, 43, addr=OWNER)     # the owner, but in an unlisted GROUP
    rig.account.arrive(10)
    rig.run()
    assert rig.mailed == []
    _assert_one_clean_record(rig, "message", 43, OWNER, "Alice")
    assert any("REJECTED inbound message from chat 43" in ln for ln in rig.logs)
    assert rig.account.marked == [10]                       # remembered + seen: no replay
    assert "10" in bot.SEEN.read_text().split()


def test_an_unlisted_contact_request_is_a_record_not_a_notice(rig):
    rig.account.add(11, 50)
    rig.account.arrive(11)
    rig.run()
    assert rig.mailed == []                                 # request_in's notice mail is gone
    assert not rig.account.chats[50].accepted
    _assert_one_clean_record(rig, "contact-request", 50, STRANGER, "Bob")


def test_an_unlisted_contact_request_is_not_accepted_even_with_accept_on(rig, monkeypatch):
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", True)
    rig.account.add(11, 50)
    rig.account.arrive(11)
    rig.run()
    assert rig.mailed == [] and not rig.account.chats[50].accepted
    assert len(rig.records()) == 1


def test_an_unlisted_reaction_is_recorded_not_mailed(rig):
    rig.account.react(13, contact_id=2)
    rig.run()
    assert rig.mailed == []
    _assert_one_clean_record(rig, "reaction", 13, STRANGER, "Bob")


def test_an_unlisted_webxdc_tap_writes_no_xdc_in_line(rig, monkeypatch):
    monkeypatch.setattr(bot, "XDC_IN", rig.xdc_in)
    _xdc_event(rig.account, 43)
    rig.run()
    assert not rig.xdc_in.exists() and rig.mailed == []
    _assert_one_clean_record(rig, "webxdc", 43, None, None)


def test_an_unlisted_webxdc_tap_is_not_mailed(rig):
    _xdc_event(rig.account, 43)
    rig.run()
    assert rig.mailed == []
    assert len(rig.records()) == 1


def test_an_unlisted_non_tap_update_leaves_no_payload_in_the_log(rig, monkeypatch):
    # do-008 review advisory 2: the "not a tap" skip line runs BEFORE the gate, and
    # used to log 70 chars of the stranger's own payload.
    monkeypatch.setattr(bot, "XDC_MATCH", [("from", "desk")])      # {"from": "xdc"} is no tap
    _xdc_event(rig.account, 43)
    rig.run()
    assert any("XDC SKIP msg 900" in ln for ln in rig.logs)         # still logged, as a skip
    assert not any(SECRET in ln or "4471" in ln for ln in rig.logs)
    assert rig.mailed == []


def test_a_serial_less_update_leaves_no_payload_in_the_log(rig):
    rig.account.msgs[900] = FakeMsg(900, 43, CONTACTS[STRANGER])
    rig.account.msgs[900].get_webxdc_status_updates = lambda since=0: [
        {"serial": "x", "payload": SECRET, "info": ""}]
    rig.account.events.append(Obj(kind=bot.EventType.WEBXDC_STATUS_UPDATE, msg_id=900,
                                  status_update_serial=101))
    rig.run()
    assert any("without a serial skipped" in ln for ln in rig.logs)
    assert not any(SECRET in ln or "4471" in ln for ln in rig.logs)


def test_the_private_profile_still_logs_the_skipped_payload(rig, monkeypatch):
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setattr(bot, "XDC_MATCH", [("from", "desk")])
    _xdc_event(rig.account, 43)
    rig.run()
    assert any("XDC SKIP" in ln and "4471" in ln for ln in rig.logs)


def test_one_webxdc_event_with_many_taps_is_one_record(rig):
    # one event announcing serial 101 fetches 101..103 (the core's batch)
    _xdc_event(rig.account, 43, serials=(101, 102, 103))
    rig.account.events[1:] = []
    rig.run()
    assert rig.mailed == [] and len(rig.records()) == 1
    assert bot.XDC_SEEN[900] == 103                         # moved on: none replayed


def test_the_catch_up_sweep_records_and_does_not_mail(rig):
    rig.account.add(30, 13)
    rig.account.add(31, 50)
    rig.account.fresh = [rig.account.msgs[30], rig.account.msgs[31]]
    bot.sweep(rig.account)
    assert rig.mailed == []
    assert [(r["chat_id"], r["kind"]) for r in rig.records()] == [(13, "message"), (50, "contact-request")]
    assert rig.account.marked == [30, 31]
    assert SECRET not in rig.rejected.read_text()


# --- public profile, listed chat: unchanged ----------------------------------------------

def test_a_listed_group_message_is_mailed_once(rig):
    rig.account.add(10, 12)
    rig.account.arrive(10)
    rig.run()
    assert len(rig.mailed) == 1 and not rig.rejected.exists()


def test_the_listed_owners_1to1_is_mailed_once(rig):
    rig.account.add(10, 20, addr=OWNER)
    rig.account.arrive(10)
    rig.run()
    assert len(rig.mailed) == 1 and not rig.rejected.exists()


def test_a_listed_owners_contact_request_is_held_as_today(rig):
    rig.account.add(10, 51, addr=OWNER)
    rig.account.arrive(10)
    rig.run()
    (args, _kw), = rig.mailed                              # request_in's flagged notice
    assert "CONTACT REQUEST - NOT ACCEPTED" in args[3] and not rig.rejected.exists()
    # PHONE-127 SC6: the warning names the role, not a person
    assert "ask your owner before accepting" in args[3] and "Alice" not in args[3]


def test_a_listed_reaction_and_tap_pass(rig, monkeypatch):
    monkeypatch.setattr(bot, "XDC_IN", rig.xdc_in)
    rig.account.react(12, contact_id=2)
    _xdc_event(rig.account, 12)
    rig.run()
    assert len(rig.mailed) == 1                             # the reaction
    assert len(rig.xdc_in.read_text().splitlines()) == 1    # the tap
    assert not rig.rejected.exists()


def test_a_listed_sweep_message_is_mailed(rig):
    rig.account.add(30, 12)
    rig.account.fresh = [rig.account.msgs[30]]
    bot.sweep(rig.account)
    assert len(rig.mailed) == 1 and not rig.rejected.exists()


# --- the PRIVATE profile is not gated --------------------------------------------------

@pytest.mark.parametrize("chat_id", [12, 43, 13])
def test_private_profile_inbound_is_unchanged(rig, monkeypatch, chat_id):
    monkeypatch.setattr(bot, "PROFILE", "")
    rig.path.unlink()                                       # no list at all: still unchanged
    rig.account.add(10, chat_id)
    rig.account.arrive(10)
    rig.account.react(chat_id, contact_id=2)
    _xdc_event(rig.account, chat_id)
    rig.run()
    assert len(rig.mailed) == 3                             # message, reaction, tap
    assert not rig.rejected.exists()
    assert not any("REJECTED" in ln for ln in rig.logs)


def test_private_profile_sweep_is_unchanged(rig, monkeypatch):
    monkeypatch.setattr(bot, "PROFILE", "")
    rig.account.add(30, 43)
    rig.account.fresh = [rig.account.msgs[30]]
    bot.sweep(rig.account)
    assert len(rig.mailed) == 1 and not rig.rejected.exists()


# --- fail closed ------------------------------------------------------------------------

def test_a_missing_list_rejects_every_inbound_and_says_why(rig):
    rig.path.unlink()
    rig.account.add(10, 12)                                 # would be listed
    rig.account.arrive(10)
    rig.account.react(20, contact_id=1)
    rig.run()
    assert rig.mailed == []
    recs = rig.records()
    assert len(recs) == 2
    assert all(f"NO channels file at {rig.path}" in r["reason"] for r in recs)


def test_a_loose_list_rejects_every_inbound_and_says_why(rig):
    rig.path.chmod(0o660)                                   # group-writable: rule 4 refuses it
    rig.account.add(10, 12)
    rig.account.arrive(10)
    rig.run()
    assert rig.mailed == []
    (rec,) = rig.records()
    assert "0660" in rec["reason"] and str(rig.path) in rec["reason"]


def test_a_check_that_raises_fails_closed(rig, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("core went away")
    monkeypatch.setattr(channels, "check", boom)
    rig.account.add(10, 12)
    rig.account.arrive(10)
    rig.run()
    assert rig.mailed == []
    (rec,) = rig.records()
    assert "core went away" in rec["reason"]


def test_a_record_write_failure_is_loud_and_still_forwards_nothing(rig):
    rig.rejected.mkdir()                                    # cannot be opened as a file
    rig.account.add(10, 43)
    rig.account.arrive(10)
    rig.account.react(13, contact_id=2)
    rig.run()
    assert rig.mailed == []
    fails = [ln for ln in rig.logs if ln.startswith("REJECTED RECORD FAILED")]
    assert len(fails) == 2 and str(rig.rejected) in fails[0] and "NOT passed" in fails[0]


def test_a_symlink_in_place_of_the_record_is_refused(rig, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("")
    rig.rejected.symlink_to(target)
    rig.account.add(10, 43)
    rig.account.arrive(10)
    rig.run()
    assert rig.mailed == [] and target.read_text() == ""
    assert any(ln.startswith("REJECTED RECORD FAILED") for ln in rig.logs)


def test_the_record_rotates_past_the_cap(rig, monkeypatch):
    monkeypatch.setattr(bot, "REJECTED_CAP", 600)
    for i in range(12):
        rig.account.react(13, contact_id=2, msg_id=i)
    rig.run()
    one = rig.rejected.with_name("rejected.jsonl.1")
    assert one.exists() and rig.rejected.exists()
    assert rig.rejected.stat().st_size <= 600 and one.stat().st_size <= 600
    assert not rig.rejected.with_name("rejected.jsonl.2").exists()     # one generation only
    assert stat.S_IMODE(rig.rejected.stat().st_mode) == 0o600
    assert stat.S_IMODE(one.stat().st_mode) == 0o600
    recs, bad = channels.read_rejected(rig.rejected)
    assert bad == 0 and 2 <= len(recs) < 12                  # the oldest rotated away
    assert rig.mailed == []


def test_a_loose_record_file_is_made_0600(rig):
    rig.rejected.write_text("")
    os.chmod(rig.rejected, 0o644)
    rig.account.react(13, contact_id=2)
    rig.run()
    assert stat.S_IMODE(rig.rejected.stat().st_mode) == 0o600


def test_a_strangers_name_cannot_inject_lines_or_bloat_the_record(rig):
    CONTACTS["evil@relay.example"] = FakeContact("evil@relay.example", "Eve\n{\"kind\":\"x\"}" + "A" * 5000)
    try:
        rig.account.contacts_by_id[9] = CONTACTS["evil@relay.example"]
        rig.account.react(13, contact_id=9)
        rig.run()
    finally:
        del CONTACTS["evil@relay.example"]
    (rec,) = rig.records()                                  # still exactly one line
    assert "\n" not in rec["name"] and len(rec["name"]) <= channels.FIELD_MAX

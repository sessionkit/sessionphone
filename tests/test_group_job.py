"""The group job under the allow-list (PHONE-94, design/group-job.md, option (d)).

`{"group": NAME, "members": [...]}` makes a Delta group of contacts the
profile already knows, adds them, and SENDS NOTHING: the chat it makes is on
no list (design/allow-list.md inventory row 16). Its ack names the new chat
and how to list it; the first words go later by dc-say, through the gate.
Every check, and every member, is settled before the group is made.

One pass of main()'s outbox loop over fakes, on the rig of
tests/bridge/test_channels.py (copied, not imported: tests/bridge is neither
collected by `make test` nor linted). The fakes record every call in order:
create_group, add, and any create_chat (which would open a 1:1). Fakes only:
no account, no rpc-server, never a real Delta send.

The pass ends at main()'s OWN sleep, the outbox loop's `time.sleep(1)`. The
rig swaps bot's `time` name, never the time module: that module is shared by
every thread and library, and main's start probe (seatmail.probe, a child
process run with a timeout) waits for its child with time.sleep. Patched
process-wide, the pass ended inside the probe whenever the child was still
exiting, before any job was read (PHONE-171).
"""
import json
import os
import re
import subprocess
import threading
import time

import _keyfakes
import pytest

from sessionphone.bridge import bot, calls

OWNER = "alice@relay.example"
STRANGER = "bob@relay.example"
OTHER_BOB = "bob@elsewhere.example"
LIST = f"family  chat:12   # the family group\nowner   {OWNER}  key:{_keyfakes.FPR_K1}   # owner\n"
NAME = "Book Club"
NEW = 50        # the chat id the fake core gives the group it makes
AGENT_BOX = "agent@host.example"    # DC_MAILTO: where the list alert goes (PHONE-129)


def made(k, whos):
    """The exact ok:true ack detail for a group made as chat NEW."""
    return (f"made group {NAME!r}, chat {NEW}, members {k}: {whos}; nothing was sent. "
            f"List it: sessionphone channels add NAME chat:{NEW}, then dc-say NAME TEXT")


class Done(Exception):
    """Ends main()'s loop at its first sleep: one pass of the outbox."""


class BotTime:
    """bot.py's `time`: the time module, but sleep raises Done. Only bot.py's
    own calls see it; the probe's child wait and every thread keep the real one."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, s):
        raise Done


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeContact:
    def __init__(self, account, cid, addr, name, chat_id):
        self.account, self.id, self.addr, self.name, self.chat_id = account, cid, addr, name, chat_id

    def get_snapshot(self):
        return Obj(address=self.addr, display_name=self.name, name="", is_key_contact=True)

    def make_vcard(self):           # the owner holds K1, anyone else K2
        return _keyfakes.vcard(self.addr, _keyfakes.K1 if self.addr == OWNER else _keyfakes.K2)

    def create_chat(self):          # opens the 1:1; sends nothing, but is recorded
        self.account.calls.append(("create_chat", self.addr))
        return self.account.get_chat_by_id(self.chat_id)


class FakeChat:
    def __init__(self, account, cid, chat_type, members):
        self.account, self.id, self.chat_type, self.members = account, cid, chat_type, members

    def get_basic_snapshot(self):
        return Obj(chat_type=self.chat_type)

    def get_contacts(self):
        return [self.account.contacts[a] for a in self.members]

    def add_contact(self, contact):
        self.account.calls.append(("add", self.id, contact.addr))
        self.account.adds += 1
        if self.account.add_fails_at == self.account.adds:
            raise RuntimeError("core said no")
        self.members.append(contact.addr)

    def send_text(self, text):
        self.account.sent.append(("text", self.id))
        return Obj(id=77)

    def send_message(self, **kw):
        self.account.sent.append(("message", self.id))
        return Obj(id=78)


class FakeAccount:
    """12 a listed group; 20 the owner's 1:1; 13 a stranger's 1:1. A group it makes is NEW."""

    def __init__(self):
        self.sent, self.calls, self.adds, self.add_fails_at, self.on_create = [], [], 0, None, None
        self.contacts = {OWNER: FakeContact(self, 1, OWNER, "Alice", 20),
                         STRANGER: FakeContact(self, 2, STRANGER, "Bob", 13)}
        self.chats = {12: FakeChat(self, 12, "Group", [OWNER, STRANGER]),
                      20: FakeChat(self, 20, "Single", [OWNER]),
                      13: FakeChat(self, 13, "Single", [STRANGER])}

    def is_configured(self):
        return True

    def start_io(self):
        pass

    def get_config(self, key):
        return "bot@test"

    def get_contacts(self, query=None):
        q = (query or "").lower()
        return [c for c in self.contacts.values() if q in c.addr.lower()]

    def get_contact_by_addr(self, addr):
        return self.contacts.get(addr)

    def get_chat_by_id(self, cid):
        if cid not in self.chats:
            raise ValueError(f"no chat {cid}")
        return self.chats[cid]

    def create_group(self, name):   # local and unpromoted: sends nothing
        self.calls.append(("create_group", name))
        self.chats[NEW] = FakeChat(self, NEW, "Group", [])
        if self.on_create:
            self.on_create(NEW)
        return self.chats[NEW]


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
    r.out, r.path, r.logs, r.mails = tmp_path / "outbox", tmp_path / "channels.txt", [], []
    r.account = FakeAccount()
    stop = threading.Event()
    monkeypatch.setenv("DC_CHANNELS_FILE", str(r.path))
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
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "INVITE", None)
    monkeypatch.setattr(bot, "log", r.logs.append)
    # the list alert (PHONE-129) goes by seatmail (PHONE-106): faked and recorded, never `message`
    monkeypatch.setattr(bot.seatmail, "send", lambda to, subject, body, **k: (
        r.mails.append(f"To: {to}\nSubject: {subject}\n\n{body}"), bot.seatmail.Sent(True, id="<1.2.3@host.session-mail>"))[1])
    monkeypatch.setattr(bot.dc_env, "MAILTO", AGENT_BOX)
    monkeypatch.setattr(calls, "speak_if_live", lambda *a: False)

    monkeypatch.setattr(bot, "time", BotTime())

    def write(text=LIST, mode=0o600):
        r.path.write_text(text)
        r.path.chmod(mode)
    r.write = write

    def run(*jobs):
        r.out.mkdir(parents=True, exist_ok=True)
        for i, job in enumerate(jobs):
            job = dict(job, ack=str(tmp_path / "acks" / f"{i:02d}.json"))
            (r.out / f"{i:02d}.json").write_text(json.dumps(job))
        try:
            with pytest.raises(Done):
                bot.main()
        finally:
            stop.set()
        assert not list(r.out.glob("*.json"))                # every job unlinked
        return [json.loads((tmp_path / "acks" / f"{i:02d}.json").read_text()) for i in range(len(jobs))]
    r.run = run
    yield r
    if r.path.exists():
        r.path.chmod(0o600)


def group(members=("Alice", "bob@relay.example"), **kw):
    return {"group": NAME, "members": list(members), **kw}


def made_nothing(rig):
    return not any(c[0] == "create_group" for c in rig.account.calls)


# ---- 1-3: SC3, every member matches exactly once, or nothing is made ----

def test_every_member_matches_once_so_the_group_is_made_and_nothing_sent(rig) -> None:
    rig.write()
    (ack,) = rig.run(group())
    whos = f"Alice <{OWNER}>, Bob <{STRANGER}>"
    assert ack["ok"] is True and ack["detail"] == made(2, whos), ack
    assert rig.account.calls == [("create_group", NAME), ("add", NEW, OWNER), ("add", NEW, STRANGER)]
    assert rig.account.sent == []                            # nothing sent, no 1:1 opened
    assert f"GROUP {NAME!r} made, chat {NEW}, members 2: {whos}; nothing sent" in rig.logs


def test_a_member_matching_no_contact_makes_nothing(rig) -> None:
    rig.write()
    (ack,) = rig.run(group(["Alice", "nobody"]))
    assert ack["ok"] is False and ack["detail"] == (
        "group member 'nobody' matches no contact this profile knows. Check the spelling or the "
        "address, or add them as a contact first. Nothing was made."), ack
    assert made_nothing(rig) and rig.account.calls == []
    assert any(m.startswith("SEND FAILED for 00.json: group member 'nobody' matches no contact")
               for m in rig.logs)


def test_a_member_matching_two_contacts_makes_nothing(rig) -> None:
    rig.account.contacts[OTHER_BOB] = FakeContact(rig.account, 3, OTHER_BOB, "Bob", 14)
    rig.write()
    (ack,) = rig.run(group(["Alice", "Bob"]))
    assert ack["ok"] is False and ack["detail"] == (
        f"group member 'Bob' matches 2 contacts: Bob <{STRANGER}>, Bob <{OTHER_BOB}>. Name one by its "
        "address, or list it (sessionphone channels add NAME ADDRESS) and use that NAME. Nothing was made."), ack
    assert rig.account.calls == [] and rig.account.sent == []


# ---- 4-6: SC4, the gate is deliberate ----

def test_the_new_chat_is_unlisted_so_the_gate_refuses_a_send_to_it(rig) -> None:
    rig.write()
    acks = rig.run(group(), {"chat_id": NEW, "text": "hi"})
    assert acks[0]["ok"] is True
    assert acks[1]["ok"] is False
    assert acks[1]["detail"].startswith(f"REFUSED: chat {NEW} is not on the allow-list"), acks[1]
    assert rig.account.sent == []


def test_once_the_owner_lists_it_a_send_goes_once(rig) -> None:
    rig.write()
    rig.account.on_create = lambda cid: rig.write(LIST + f"club  chat:{cid}\n")    # the owner lists it
    acks = rig.run(group(), {"chat": "club", "text": "hi"})
    assert [a["ok"] for a in acks] == [True, True], acks
    assert rig.account.sent == [("text", NEW)]               # the list is re-read per job (rule 4)


@pytest.mark.parametrize("state", ["missing", "0660"])
def test_an_unusable_list_refuses_before_anything_is_made(rig, state) -> None:
    if state == "0660":
        rig.write(mode=0o660)
    (ack,) = rig.run(group())
    assert ack["ok"] is False and str(rig.path) in ack["detail"], ack
    # PHONE-129: the refusal ends with the alert's note, and the alert went once
    assert ack["detail"].startswith("REFUSED: ") and re.search(
        r": every send is refused; nothing was made\.; the owner's agent "
        + re.escape(AGENT_BOX) + r" was mailed at \d{4}-\d\d-\d\dT\S+$", ack["detail"]), ack
    assert len(rig.mails) == 1 and f"To: {AGENT_BOX}\n" in rig.mails[0], rig.mails
    assert rig.account.calls == [] and rig.account.sent == []
    assert any(m.startswith("REFUSED group job: ") for m in rig.logs), rig.logs


# ---- 7-9: the job's shape, all refused before anything is made ----

NO_MEMBERS = 'REFUSED: a group job needs "members", a non-empty list of contacts; nothing was made.'


@pytest.mark.parametrize("members", ["absent", None, [], "Alice", ["Alice", ""], ["Alice", 7]])
def test_no_or_bad_members_are_refused(rig, members) -> None:      # m1
    rig.write()
    job = {"group": NAME} if members == "absent" else {"group": NAME, "members": members}
    (ack,) = rig.run(job)
    assert ack["ok"] is False and ack["detail"] == NO_MEMBERS, ack
    assert rig.account.calls == []
    assert "REFUSED group job: no members" in rig.logs


def test_a_job_carrying_text_is_refused_and_empty_text_is_none(rig) -> None:
    rig.write()
    acks = rig.run(group(text="hi"))
    assert acks[0]["ok"] is False and acks[0]["detail"] == (
        "REFUSED: a group job sends nothing, because the chat it makes is on no list; "
        'drop "text", then list the chat and dc-say it. Nothing was made.'), acks[0]
    assert rig.account.calls == [] and rig.account.sent == []
    assert "REFUSED group job: it carries text" in rig.logs


@pytest.mark.parametrize("text", ["", None])
def test_empty_or_null_text_counts_as_no_text(rig, text) -> None:
    rig.write()
    (ack,) = rig.run(group(text=text))
    assert ack["ok"] is True, ack
    assert rig.account.calls[0] == ("create_group", NAME) and rig.account.sent == []


@pytest.mark.parametrize("name", [None, "", "  ", 7])
def test_no_or_bad_group_name_is_refused(rig, name) -> None:
    rig.write()
    (ack,) = rig.run({"group": name, "members": ["Alice"]})
    assert ack["ok"] is False
    assert ack["detail"] == 'REFUSED: a group job needs "group", a non-empty name; nothing was made.', ack
    assert rig.account.calls == []
    assert "REFUSED group job: no group name" in rig.logs


def test_a_job_with_no_group_key_is_not_a_group_job_and_makes_nothing(rig) -> None:
    # "group" absent: the job names no branch, so it falls to dc-say, which
    # refuses a job with no destination (tests/bridge/test_channels.py pins that)
    rig.write()
    (ack,) = rig.run({"members": ["Alice"]})
    assert ack["ok"] is False and ack["detail"] == "REFUSED: this job names no chat; nothing was sent."
    assert rig.account.calls == [] and rig.account.sent == []


# ---- 10: channel NAMEs as members ----

def test_an_address_channel_is_a_member_and_a_chat_channel_is_not(rig) -> None:
    rig.write()
    acks = rig.run(group(["owner"]), group(["Alice", "family"]))
    assert acks[0]["ok"] is True and acks[0]["detail"] == made(1, "channel owner"), acks[0]
    assert acks[1]["ok"] is False and acks[1]["detail"] == (
        "REFUSED: group member 'family' is channel chat:12, not a contact; nothing was made."), acks[1]
    assert "REFUSED group job: channel 'family' is a chat, not a contact" in rig.logs
    assert rig.account.calls == [("create_group", NAME), ("add", NEW, OWNER)]      # job 01 made nothing
    assert rig.account.sent == []


# ---- 11-12: m3 and m2 ----

def test_the_same_contact_named_twice_is_added_once(rig) -> None:     # m3
    rig.write()
    (ack,) = rig.run(group(["Alice", "alice@relay.example"]))
    assert ack["ok"] is True and ack["detail"] == made(1, f"Alice <{OWNER}>"), ack
    assert rig.account.calls == [("create_group", NAME), ("add", NEW, OWNER)]


def test_a_failed_add_names_the_chat_and_says_nothing_was_sent(rig) -> None:    # m2
    rig.write()
    rig.account.add_fails_at = 2
    (ack,) = rig.run(group())
    why = f"group chat {NEW} was made but adding member 2 of 2 failed (core said no); nothing was sent"
    assert ack["ok"] is False and ack["detail"] == f"RuntimeError: {why}", ack
    assert f"SEND FAILED for 00.json: {why}" in rig.logs
    assert rig.account.sent == []


# ---- PHONE-171: the pass ends at main()'s own sleep, never inside the start probe ----

def test_a_probe_child_still_exiting_does_not_end_the_pass(rig, monkeypatch) -> None:
    # Forces the interleaving host load made: the start probe's child has closed
    # its pipes but is not reaped yet, so Popen's timed wait finds it running and
    # sleeps before it polls again. That sleep is the time module's, not main()'s:
    # the probe finishes, and the pass reads the job.
    real, polls = subprocess.Popen._try_wait, []

    def try_wait(self, flags):
        if flags & os.WNOHANG:
            polls.append(self.args)
            if len(polls) == 1:
                return (0, 0)           # still running: _wait sleeps, then polls again
        return real(self, flags)
    monkeypatch.setattr(subprocess.Popen, "_try_wait", try_wait)
    rig.write()
    (ack,) = rig.run(group())
    assert len(polls) >= 2 and polls[0][1:] == bot.seatmail.PROBE_ARGS, polls   # the probe, seen running once
    assert ack["ok"] is True and ack["detail"] == made(2, f"Alice <{OWNER}>, Bob <{STRANGER}>"), ack


def test_no_rig_patches_the_time_module_sleep() -> None:
    # Every rig that ends bot.main() at a sleep swaps bot's own `time` name. A rig
    # that patched the time module's sleep would end the pass wherever any caller
    # in the process slept first: the probe's child wait among them. tests/bridge
    # is searched too: make test does not collect it, but a copy must not carry this.
    tests = os.path.dirname(os.path.abspath(__file__))
    shapes = re.compile(r"bot\.time" + r"""(\s*,\s*["']sleep["']|\.sleep\s*=)""")
    hits = []
    for top, _dirs, files in os.walk(tests):
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(top, name)
                with open(path, encoding="utf-8") as fh:
                    hits += [f"{os.path.relpath(path, tests)}:{n}" for n, line in enumerate(fh, 1)
                             if shapes.search(line)]
    assert not hits, hits

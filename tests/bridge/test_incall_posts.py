"""In-call posts are gated too (PHONE-102, design/allow-list.md rule 5, rows 11-13).

Three paths in calls.py put text in a chat outside the outbox: mail into the
call's chat (MailLeg, row 11), the barge-in remainder (row 12) and the
transcript (row 13, off today). Each one asks channels.check() first. A chat
that is not listed gets ZERO send_text calls and a logged refusal naming the
path and the list; a listed chat gets exactly one post. Only the chat TEXT is
gated: the speech into the call, and the transcript mail, still happen.

Fakes only: no account, no rpc-server, no call, no TTS.
"""
import asyncio
import pathlib
import types

import numpy as np
import _keyfakes
import pytest

from sessionphone.bridge import calls

OWNER = "erin@relay.example"
STRANGER = "bob@relay.example"
# 12 is a listed group; 20 is the owner's 1:1 (listed by address); 13 is a
# stranger's 1:1, which nothing lists.
LIST = f"family  chat:12   # the family group\nowner   {OWNER}  key:{_keyfakes.FPR_K1}   # owner\n"

SAID = "We have done it four times, in the care packs for Ben and Ada."
REAL = "Git bundle create and send the file into the chat."


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeContact:
    def __init__(self, addr):
        self.addr = addr

    def get_snapshot(self):
        return Obj(address=self.addr, is_key_contact=True)

    def make_vcard(self):           # the owner holds K1, anyone else K2
        return _keyfakes.vcard(self.addr, _keyfakes.K1 if self.addr == OWNER else _keyfakes.K2)


class FakeChat:
    def __init__(self, account, cid, chat_type, members):
        self.account, self.id, self.chat_type, self.members = account, cid, chat_type, members

    def get_basic_snapshot(self):
        return Obj(chat_type=self.chat_type)

    def get_contacts(self):
        return [FakeContact(a) for a in self.members]

    def send_text(self, text):
        self.account.posted.append((self.id, text))
        return Obj(id=77)


class FakeAccount:
    def __init__(self):
        self.posted = []
        self.chats = {12: FakeChat(self, 12, "Group", [OWNER, STRANGER]),
                      20: FakeChat(self, 20, "Single", [OWNER]),
                      13: FakeChat(self, 13, "Single", [STRANGER])}

    def get_chat_by_id(self, cid):
        if cid not in self.chats:
            raise ValueError(f"no chat {cid}")
        return self.chats[cid]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = types.SimpleNamespace(path=tmp_path / "channels.txt", logs=[], mails=[],
                              spoken=[], account=FakeAccount(), tmp=tmp_path)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(r.path))
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(
        r.logs.append, lambda p: REAL, lambda *a, **k: r.mails.append((a, k))))
    monkeypatch.setattr(calls, "VOICE_MAP_FILE", tmp_path / "voices.json")
    # MailLeg speaks through run_coroutine_threadsafe; record it, run nothing
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: r.spoken.append(coro))

    def write(text=LIST, mode=0o600):
        r.path.write_text(text)
        r.path.chmod(mode)
    r.write = write

    def refused(what):
        return [m for m in r.logs if m.startswith(f"CALL REFUSED {what} to chat ")]
    r.refused = refused
    yield r
    if r.path.exists():
        r.path.chmod(0o600)


# ---- the one helper every in-call post goes through ----

def test_post_to_chat_sends_once_to_a_listed_chat(rig) -> None:
    rig.write()
    assert calls.post_to_chat(rig.account, 12, "hi", "test post") is True
    assert calls.post_to_chat(rig.account, 20, "hi", "test post") is True   # the owner's 1:1
    assert rig.account.posted == [(12, "hi"), (20, "hi")]
    assert not rig.refused("test post")


def test_post_to_chat_refuses_an_unlisted_chat_and_logs_it(rig) -> None:
    rig.write()
    assert calls.post_to_chat(rig.account, 13, "hi", "test post") is False
    assert rig.account.posted == []                                       # ZERO sends
    (line,) = rig.refused("test post")
    assert f"chat 13: not in {rig.path}" in line, line


@pytest.mark.parametrize("state", ["missing", "unreadable", "loose"])
def test_post_to_chat_refuses_everything_without_a_good_list(rig, state) -> None:
    if state == "unreadable":
        rig.write(mode=0o000)
    elif state == "loose":
        rig.write(mode=0o660)
    assert calls.post_to_chat(rig.account, 12, "hi", "test post") is False
    assert rig.account.posted == []
    (line,) = rig.refused("test post")
    assert str(rig.path) in line, line


def test_an_address_entry_never_passes_a_group(rig) -> None:
    # the owner is in group 12; with only the owner listed, 12 is refused
    rig.write(f"owner  {OWNER}\n")
    assert calls.post_to_chat(rig.account, 12, "hi", "test post") is False
    assert rig.account.posted == []


# ---- row 11: mail into the call's chat (MailLeg) ----

def _session(account, chat_id, root):
    s = object.__new__(calls.CallSession)
    s.pc, s.root_id, s.chat_id, s.account = object(), root, chat_id, account
    s.speak = lambda text, voice, who, floor=False: ("speak", who, text)
    return s


def _leg(sessions):
    lg = object.__new__(calls.MailLeg)
    lg.manager = types.SimpleNamespace(sessions=dict(enumerate(sessions)), loop=None)
    lg.seen = lambda f: None
    return lg


def _mail(tmp, refs, body="Hello from Alice."):
    p = tmp / "m.eml"
    p.write_text(f"From: Alice <alice@example.org>\nTo: carol-chat@example.org\n"
                 f"Subject: re: call\nIn-Reply-To: <{refs}>\nReferences: <{refs}>\n\n{body}\n")
    return p


def test_mail_leg_posts_once_to_a_listed_chat(rig) -> None:
    rig.write()
    _leg([_session(rig.account, 12, "call-12-1789@example.org")]).one(
        _mail(rig.tmp, "call-12-1789@example.org"))
    assert len(rig.spoken) == 1
    # unsigned, so unverified (PHONE-155): the post must not read as trusted
    assert rig.account.posted == [(12, "Alice <alice@example.org> (unverified sender): Hello from Alice.")]
    assert not rig.refused("mail-leg post")


def test_mail_leg_to_an_unlisted_chat_speaks_but_posts_nothing(rig) -> None:
    rig.write()
    _leg([_session(rig.account, 13, "call-13-1789@example.org")]).one(
        _mail(rig.tmp, "call-13-1789@example.org"))
    assert len(rig.spoken) == 1                       # the call still hears it
    assert rig.account.posted == []                   # ZERO send_text
    (line,) = rig.refused("mail-leg post")
    assert f"chat 13: not in {rig.path}" in line, line


def test_mail_leg_with_a_references_derived_chat_posts_nothing_unlisted(rig, monkeypatch) -> None:
    # The root is stale: an ISSUED root of an earlier call in chat 13 reaches
    # the live call there (the turnover fallback, PHONE-155 ruling (ii)), and
    # anyone who ever saw that root can write to it.
    issued = calls.IssuedRoots()
    issued.mint(13, "call-13-1789@example.org", now=1000)
    issued.end(13, now=1000)
    issued.mint(13, "call-13-9999@example.org", now=1011)
    monkeypatch.setattr(calls, "ISSUED", issued)
    rig.write()
    _leg([_session(rig.account, 13, "call-13-9999@example.org")]).one(
        _mail(rig.tmp, "call-13-1789@example.org"))
    assert any("root is stale" in m for m in rig.logs), rig.logs   # the fallback ran
    assert len(rig.spoken) == 1
    assert rig.account.posted == []
    assert rig.refused("mail-leg post")


# ---- row 12: the barge-in remainder ----

def _speaking_session(account, chat_id):
    s = object.__new__(calls.CallSession)
    s.account, s.chat_id = account, chat_id
    s.voice = calls.PipVoiceTrack()
    s.echo = calls.EchoGuard(spool=None)
    s.lines, s.turn_parts, s.turn_pending = [], [], set()
    s.turn_timer, s.speaking, s.previews = None, False, None
    s.preview_id, s.chunk_n = None, 0
    s._turn_arm = lambda: None
    return s


def _barge_in(rig, monkeypatch, chat_id):
    monkeypatch.setattr(calls, "BARGE_IN", True)
    monkeypatch.setattr(calls, "BARGE_IN_KEEP_TEXT", True)
    monkeypatch.setattr(calls, "tts_pcm48", lambda text, voice: np.zeros(
        int(calls.OUT_RATE * 30.0), dtype=np.int16))
    s = _speaking_session(rig.account, chat_id)

    async def go():
        await s.speak(SAID)
        assert s.voice.speaking(), "nothing queued to interrupt"
        await s._heard(pathlib.Path("caller-001.wav"), 1)
    asyncio.run(go())
    assert any("BARGE-IN" in m for m in rig.logs), rig.logs      # the cut happened
    assert not s.voice.speaking()


def test_barge_in_remainder_posts_once_to_a_listed_chat(rig, monkeypatch) -> None:
    rig.write()
    _barge_in(rig, monkeypatch, 12)
    assert len(rig.account.posted) == 1
    cid, text = rig.account.posted[0]
    assert cid == 12 and text.startswith("(cut off) ")
    assert not rig.refused("barge-in remainder")


def test_barge_in_remainder_to_an_unlisted_chat_posts_nothing(rig, monkeypatch) -> None:
    rig.write()
    _barge_in(rig, monkeypatch, 13)
    assert rig.account.posted == []                   # the audio is cut; no text goes
    (line,) = rig.refused("barge-in remainder")
    assert f"chat 13: not in {rig.path}" in line, line


# ---- row 13: the transcript (POST_TRANSCRIPT is off today; gated anyway) ----

def _ended_session(account, chat_id, tmp):
    s = object.__new__(calls.CallSession)
    s.account, s.chat_id, s.chat_name = account, chat_id, "call"
    s.dir, s.started, s.caller, s.root_id = tmp / "call", None, "Erin", "call-x@example.org"
    s.lines = [(0.0, "Erin", "hello")]
    s.guest_list = list
    return s


def test_transcript_posts_once_to_a_listed_chat(rig, monkeypatch) -> None:
    monkeypatch.setattr(calls, "POST_TRANSCRIPT", True)
    rig.write()
    _ended_session(rig.account, 12, rig.tmp).transcript_out()
    assert len(rig.account.posted) == 1 and rig.account.posted[0][0] == 12
    assert any(m.startswith("CALL TRANSCRIPT posted to chat 12") for m in rig.logs)
    assert len(rig.mails) == 1                        # the mail thread still closes


def test_transcript_to_an_unlisted_chat_posts_nothing(rig, monkeypatch) -> None:
    monkeypatch.setattr(calls, "POST_TRANSCRIPT", True)
    rig.write()
    _ended_session(rig.account, 13, rig.tmp).transcript_out()
    assert rig.account.posted == []
    (line,) = rig.refused("transcript post")
    assert f"chat 13: not in {rig.path}" in line, line
    assert not any("TRANSCRIPT posted" in m for m in rig.logs), "claims a post that never went"
    assert len(rig.mails) == 1                        # mail is not a Delta send: it still goes
    assert (rig.tmp / "call" / "transcript.md").exists()


def test_transcript_off_posts_nothing_and_checks_nothing(rig, monkeypatch) -> None:
    monkeypatch.setattr(calls, "POST_TRANSCRIPT", False)
    rig.write()
    _ended_session(rig.account, 12, rig.tmp).transcript_out()
    assert rig.account.posted == [] and not rig.refused("transcript post")


# ---- no in-call path sends chat text around the gate ----

def test_every_send_text_in_calls_goes_through_post_to_chat() -> None:
    src = pathlib.Path(calls.__file__).read_text()
    body = src[src.index("def post_to_chat("):]
    body = body[:body.index("# <<< channels-gate")]
    assert src.count(".send_text(") == body.count(".send_text(") == 1, \
        "a send_text in calls.py outside post_to_chat skips the allow-list"

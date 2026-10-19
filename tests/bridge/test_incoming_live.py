"""The live INCOMING_MSG path (PHONE-22, gaps 4, 5 and 6).

  gap 4 - a bridged live message is marked seen, as the startup sweep does
          (the ported-from bridge's bot.py:225).
  gap 5 - a held contact request mails message_text(msg): a voice note shows
          its transcript and a reply its quote (the ported-from bridge's
          bot.py:219,223).
  gap 6 - a held request is remember()ed, so the next restart's startup
          sweep does not mail it again (the ported-from bridge's bot.py:224).

Fakes only: no account, no rpc-server, no whisper, no sendmail.
"""
import pytest

from sessionphone.bridge import bot


class Done(Exception):
    """Raised by the fake account when its events run out; handle_events()
    loops forever and does not catch around wait_for_event()."""


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeMsg:
    """What account.get_message_by_id() and get_fresh_messages() return."""

    def __init__(self, snap):
        self.id = snap.id
        self.snap = snap

    def get_snapshot(self):
        return self.snap


class FakeChat:
    def __init__(self, name, is_contact_request):
        self.info = Obj(name=name, is_contact_request=is_contact_request)
        self.accepted = False

    def get_basic_snapshot(self):
        return self.info

    def accept(self):
        self.accepted = True
        self.info.is_contact_request = False


class FakeAccount:
    """One account: messages by id, chats by id, a queue of events, and a
    record of every mark_seen_messages() call."""

    def __init__(self):
        self.msgs, self.chats, self.events, self.fresh = {}, {}, [], []
        self.marked = []

    def add(self, mid, chat_id, text="", *, name="Stranger", request=False,
            file=None, file_mime="", view_type="Text", quote=None):
        sender = Obj(get_snapshot=lambda: Obj(display_name=name, address=f"{name.lower()}@relay"))
        snap = Obj(id=mid, chat_id=chat_id, text=text, file=file, file_mime=file_mime,
                   view_type=view_type, quote=quote, sender=sender)
        self.msgs[mid] = FakeMsg(snap)
        self.chats.setdefault(chat_id, FakeChat(f"chat{chat_id}", request))
        return self.msgs[mid]

    def arrive(self, mid):
        """The core's INCOMING_MSG for mid, and the message is fresh until
        someone marks it seen."""
        m = self.msgs[mid]
        self.events.append(Obj(kind=bot.EventType.INCOMING_MSG, msg_id=mid, chat_id=m.snap.chat_id))
        self.fresh.append(m)

    # -- the account surface bot.py uses
    def wait_for_event(self):
        if not self.events:
            raise Done
        return self.events.pop(0)

    def get_message_by_id(self, mid):
        return self.msgs[mid]

    def get_chat_by_id(self, chat_id):
        return self.chats[chat_id]

    def get_fresh_messages(self):
        return list(self.fresh)

    def mark_seen_messages(self, msgs):
        for m in msgs:
            self.marked.append(m.id)
            if m in self.fresh:
                self.fresh.remove(m)


def run_live(account):
    with pytest.raises(Done):
        bot.handle_events(account)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Fresh module state with SEEN in a scratch dir; every mail_in and log
    line recorded, and transcribe() answering without whisper."""
    monkeypatch.setattr(bot, "SEEN", tmp_path / "bridged-ids.txt")
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", False)
    mailed, logged = [], []
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: mailed.append((a, k)))
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot, "transcribe", lambda path: "please call me back about the parcel")
    return FakeAccount(), mailed, logged


# -- gap 4: marked seen ------------------------------------------------------

def test_a_bridged_live_message_is_marked_seen(world):
    account, mailed, _ = world
    account.add(10, chat_id=3, text="hello", name="Bob")
    account.arrive(10)
    run_live(account)
    assert len(mailed) == 1
    assert account.marked == [10]
    assert account.fresh == []


def test_an_accepted_contact_request_is_marked_seen(world, monkeypatch):
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", True)
    account, mailed, _ = world
    account.add(11, chat_id=4, text="hi", request=True)
    account.arrive(11)
    run_live(account)
    assert account.chats[4].accepted
    assert len(mailed) == 1 and account.marked == [11]


def test_a_held_request_is_marked_seen_and_not_accepted(world):
    account, mailed, _ = world
    account.add(12, chat_id=5, text="who is this", request=True)
    account.arrive(12)
    run_live(account)
    assert not account.chats[5].accepted
    assert len(mailed) == 1 and account.marked == [12]


def test_remember_comes_before_mark_seen(world, monkeypatch):
    """A mark-seen that fails must not leave the message un-remembered:
    the next sweep would mail it a second time."""
    account, mailed, logged = world

    def boom(msgs):
        raise RuntimeError("rpc gone")
    monkeypatch.setattr(account, "mark_seen_messages", boom)
    account.add(13, chat_id=3, text="hello")
    account.arrive(13)
    run_live(account)
    assert len(mailed) == 1
    assert "13" in bot.SEEN.read_text().split()
    assert any("RECV handler failed" in line for line in logged)


# -- gap 5: a held request mails message_text() ------------------------------

def test_a_held_voice_note_mails_its_transcript(world):
    account, mailed, _ = world
    account.add(20, chat_id=6, text="", request=True, file="/scratch/voice.m4a",
                file_mime="audio/mp4", view_type="Voice")
    account.arrive(20)
    run_live(account)
    (args, kw), = mailed
    body = args[3]
    assert body.startswith("CONTACT REQUEST - NOT ACCEPTED."), body
    assert "please call me back about the parcel" in body
    assert "[no text]" not in body
    assert kw["subject"] == "REQUEST Stranger: please call me back about the "   # [:30]; mail_in trims


def test_a_held_reply_carries_its_quote_and_the_subject_does_not(world):
    account, mailed, _ = world
    quote = {"authorDisplayName": "Carol", "text": "the old words", "messageId": 7}
    account.add(21, chat_id=6, text="my answer", request=True, quote=quote)
    account.arrive(21)
    run_live(account)
    (args, kw), = mailed
    assert "> in reply to Carol (msg 7):\n> the old words" in args[3]
    assert args[3].rstrip().endswith("my answer")
    assert kw["subject"] == "REQUEST Stranger: my answer"


def test_a_held_empty_message_still_says_no_text(world):
    account, mailed, _ = world
    account.add(22, chat_id=6, text="", request=True)
    account.arrive(22)
    run_live(account)
    (args, _), = mailed
    assert args[3].endswith("[no text]")


def test_a_held_voice_note_is_transcribed_once(world, monkeypatch):
    account, _, _ = world
    calls = []
    monkeypatch.setattr(bot, "transcribe", lambda path: calls.append(path) or "once")
    account.add(23, chat_id=6, request=True, file="/scratch/v.ogg", file_mime="audio/ogg")
    account.arrive(23)
    run_live(account)
    assert calls == ["/scratch/v.ogg"]


def test_the_sweep_mails_a_held_voice_note_transcribed_too(world):
    """request_in is shared: a request that arrived while the bot was down
    reaches the sweep, and gets the same body."""
    account, mailed, _ = world
    m = account.add(24, chat_id=6, request=True, file="/scratch/v.ogg", file_mime="audio/ogg")
    account.fresh.append(m)
    bot.sweep(account)
    (args, _), = mailed
    assert "please call me back about the parcel" in args[3]
    assert account.marked == [24]


# -- gap 6: a held request is remembered; the next sweep does not re-mail ----

def test_a_held_request_is_remembered(world):
    account, _, _ = world
    account.add(30, chat_id=8, text="let me in", request=True)
    account.arrive(30)
    run_live(account)
    assert "30" in bot.SEEN.read_text().split()


@pytest.mark.parametrize("is_request", [True, False], ids=["held-request", "bridged"])
def test_the_next_startup_sweep_does_not_mail_it_again(world, is_request):
    """The restart case end to end: a live message is mailed once, the bot
    restarts, and the core still lists the message as fresh (the mark-seen
    was lost, or never reached every device). SEEN alone must stop it."""
    account, mailed, _ = world
    account.add(31, chat_id=8, text="let me in", request=is_request)
    account.arrive(31)
    run_live(account)
    assert len(mailed) == 1
    # restart: the message is fresh again as far as the core is concerned
    account.fresh.append(account.msgs[31])
    bot.sweep(account)
    assert len(mailed) == 1, "the startup sweep mailed it a second time"


def test_the_sweep_still_mails_a_request_it_never_saw(world):
    """Control for the test above: SEEN is what stops the sweep, not
    something else in the fakes."""
    account, mailed, _ = world
    m = account.add(32, chat_id=8, text="arrived while down", request=True)
    account.fresh.append(m)
    bot.sweep(account)
    assert len(mailed) == 1
    assert "32" in bot.SEEN.read_text().split()

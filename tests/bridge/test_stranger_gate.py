"""A stranger is held as a contact request again, not auto-accepted.

With the core's `bot` flag set, the core accepted every contact request by
itself: a stranger who had the address landed in an accepted chat
(is_contact_request False), and on the default profile the bot mailed them
in as an ordinary message, with their image read. `sessionphone setup` no
longer sets the flag, and the bot turns it off at start (bot_flag_off),
before start_io().

  - bot_flag_off(): "1" -> "0" and one loud bot.log line; "0" and unset are
    left alone; a config call that fails is logged and never stops the bot.
  - main() calls it before start_io().
  - With the flag off the core marks a stranger's chat as a contact request
    again. On the DEFAULT profile such a chat is held and flagged (request_in,
    the CONTACT REQUEST banner, a REQUEST subject, kind=request), and its
    image is not read: live and in the startup sweep.
  - An owner who joined by the invite (is_contact_request False) is bridged
    as an ordinary message, image read, unchanged.
  - DC_ACCEPT_REQUESTS=on still accepts, and the message is routed as one.

Already covered elsewhere, not repeated here:
  tests/bridge/test_incoming_live.py  - a held request is marked seen and not
      accepted; an accepted request is marked seen; the sweep mails a held
      request; a held request is remembered.
  tests/bridge/test_image_text.py     - a contact request's image is never
      read, held or auto-accepted, live and sweep.
  tests/test_account.py               - setup() on a fresh account never sets
      the flag.

Fakes only: no account, no rpc-server, no Vision, no mail delivery.
"""
import inspect

import _chatlist
import pytest

from sessionphone import imgtext
from sessionphone.bridge import bot

BANNER = "CONTACT REQUEST - NOT ACCEPTED."
SIGN = ["Pillar Point 800m >"]


class Done(Exception):
    """The fake account's events ran out; handle_events() loops forever."""


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeMsg:
    def __init__(self, snap):
        self.id, self.snap = snap.id, snap

    def get_snapshot(self):
        return self.snap


class FakeChat:
    def __init__(self, name, request):
        self.info = Obj(name=name, is_contact_request=request)
        self.accepted = False

    def get_basic_snapshot(self):
        return self.info

    def accept(self):
        self.accepted = True
        self.info.is_contact_request = False


class FakeAccount(_chatlist.Chatlist):
    """The account surface handle_events(), sweep() and bot_flag_off() use."""

    def __init__(self, config=None):
        self.msgs, self.chats, self.events, self.fresh, self.marked = {}, {}, [], [], []
        self.config = dict(config or {})
        self.set_calls = []

    def add(self, mid, chat_id, text="", *, request=False, image=False, name="Stranger"):
        sender = Obj(get_snapshot=lambda: Obj(display_name=name, address=f"{name.lower()}@relay"))
        extra = (dict(file="/scratch/sign.png", file_name="sign.png", file_mime="image/png",
                      view_type="Image") if image else dict(file=None, file_mime="", view_type="Text"))
        snap = Obj(id=mid, chat_id=chat_id, text=text, quote=None, sender=sender, **extra)
        self.msgs[mid] = FakeMsg(snap)
        self.chats.setdefault(chat_id, FakeChat(f"chat{chat_id}", request))
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

    def get_chat_by_id(self, chat_id):
        return self.chats[chat_id]

    def mark_seen_messages(self, msgs):
        for m in msgs:
            self.marked.append(m.id)
            if m in self.fresh:
                self.fresh.remove(m)

    def get_config(self, key):
        return self.config.get(key)

    def set_config(self, key, value):
        self.set_calls.append((key, value))
        self.config[key] = value


@pytest.fixture
def world(monkeypatch, tmp_path):
    """The DEFAULT profile, DC_ACCEPT_REQUESTS off, SEEN in a scratch dir;
    mail_in, log and imgtext.read recorded."""
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setattr(bot, "SEEN", tmp_path / "bridged-ids.txt")
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", False)
    monkeypatch.setattr(bot, "IMAGE_TEXT", True)
    w = Obj(mailed=[], logged=[], reads=[], account=FakeAccount())
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: w.mailed.append((a, k)))
    monkeypatch.setattr(bot, "log", w.logged.append)

    def read(path, log):
        w.reads.append(path)
        return list(SIGN)
    monkeypatch.setattr(imgtext, "read", read)
    return w


def deliver(account, mid, path):
    """Hand message MID to the bridge the way PATH would see it."""
    if path == "live":
        account.arrive(mid)
        with pytest.raises(Done):
            bot.handle_events(account)
    else:
        account.fresh.append(account.msgs[mid])
        bot.sweep(account)


# -- bot_flag_off() ----------------------------------------------------------

def test_the_flag_on_is_turned_off_and_said_loudly(world):
    account = FakeAccount({"bot": "1"})
    assert bot.bot_flag_off(account) is True
    assert account.set_calls == [("bot", "0")]
    line, = world.logged
    assert "bot flag OFF" in line
    assert "held as a contact request" in line
    assert "already accepted stay accepted" in line


@pytest.mark.parametrize("value", ["0", "", None], ids=["off", "empty", "unset"])
def test_the_flag_not_on_is_left_alone(world, value):
    account = FakeAccount({} if value is None else {"bot": value})
    assert bot.bot_flag_off(account) is False
    assert account.set_calls == []
    assert world.logged == []


def test_a_config_call_that_fails_is_logged_and_never_stops_the_bot(world):
    class Broken(FakeAccount):
        def get_config(self, key):
            raise RuntimeError("rpc gone")
    assert bot.bot_flag_off(Broken()) is False
    line, = world.logged
    assert line.startswith("WARNING: could not turn the account's bot flag off")
    assert "rpc gone" in line


def test_main_turns_the_flag_off_before_start_io():
    """main() cannot run here (rpc-server, account); its source is the check."""
    lines = [line.strip() for line in inspect.getsource(bot.main).splitlines()]
    flag = next(i for i, line in enumerate(lines) if line.startswith("bot_flag_off(account)"))
    assert lines[flag + 1] == "account.start_io()"


# -- a stranger on the default profile: held and flagged, image not read -------

@pytest.mark.parametrize("path", ["live", "sweep"])
def test_a_strangers_request_is_held_and_flagged(world, path):
    world.account.add(40, chat_id=7, text="hello, who is this", request=True)
    deliver(world.account, 40, path)
    (args, kw), = world.mailed
    assert args[3].startswith(BANNER), args[3]
    assert args[3].rstrip().endswith("hello, who is this")
    assert kw["kind"] == "request"
    assert kw["subject"].startswith("REQUEST Stranger: ")
    assert not world.account.chats[7].accepted
    assert world.account.marked == [40]
    assert any(line.startswith("REQUEST (not accepted) chat 7") for line in world.logged)


@pytest.mark.parametrize("path", ["live", "sweep"])
def test_a_strangers_image_is_not_read(world, path):
    world.account.add(41, chat_id=7, request=True, image=True)
    deliver(world.account, 41, path)
    (args, kw), = world.mailed
    assert world.reads == []
    assert kw["kind"] == "request"
    assert args[3].startswith(BANNER)
    assert imgtext.OPEN not in args[3]


# -- an owner who joined by the invite: unchanged ------------------------------

@pytest.mark.parametrize("path", ["live", "sweep"])
def test_an_invite_joined_owner_is_bridged_as_a_message(world, path):
    world.account.add(50, chat_id=3, text="call me", request=False, name="Owner")
    deliver(world.account, 50, path)
    (args, kw), = world.mailed
    assert kw["kind"] == "message"
    assert not args[3].startswith(BANNER)
    assert "call me" in args[3]
    assert world.account.marked == [50]


@pytest.mark.parametrize("path", ["live", "sweep"])
def test_an_invite_joined_owners_image_is_read(world, path):
    world.account.add(51, chat_id=3, request=False, image=True, name="Owner")
    deliver(world.account, 51, path)
    (args, kw), = world.mailed
    assert world.reads == ["/scratch/sign.png"]
    assert kw["kind"] == "message"
    assert imgtext.OPEN in args[3]


# -- DC_ACCEPT_REQUESTS=on still accepts -----------------------------------------

@pytest.mark.parametrize("path", ["live", "sweep"])
def test_accept_requests_on_still_accepts_and_routes_a_message(world, monkeypatch, path):
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", True)
    world.account.add(60, chat_id=8, text="hi there", request=True)
    deliver(world.account, 60, path)
    assert world.account.chats[8].accepted
    (args, kw), = world.mailed
    assert kw["kind"] == "message"
    assert not args[3].startswith(BANNER)
    assert "hi there" in args[3]

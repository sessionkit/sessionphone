"""The startup sweep sees contact requests and muted chats, and a message
fetched at start is mailed once.

The sweep used to list its work with core's get_fresh_messages(), which is
a notification badge query: it leaves out muted chats and contact requests.
A request or a muted-chat message that arrived while the bot was down, and
that no live event reached, was never mailed. The sweep now lists every
unseen incoming message (bot.unseen_incoming).

start_io() runs before the sweep, and the event thread starts after it, so a
message fetched at start is both swept and queued as IncomingMsg. The live
path now skips an id the sweep already remember()ed.

Fakes only: no account, no rpc-server, no OCR helper, no whisper, no
sendmail, no real timer. The fake's get_fresh_messages() leaves out muted
chats and requests as core's does (tests/_chatlist.py), so a sweep that went
back to it fails here.
"""
import pytest
import test_incoming_live as base
import test_premessage_hold as hold

from sessionphone import imgtext
from sessionphone.bridge import bot

Obj, run_live = base.Obj, base.run_live
BANNER = "CONTACT REQUEST - NOT ACCEPTED."


@pytest.fixture
def w(monkeypatch, tmp_path):
    """Module state in a scratch dir: SEEN, HELD and the timer factory fresh;
    mail_in, log, imgtext.read and transcribe recorded."""
    monkeypatch.setattr(bot, "SEEN", tmp_path / "bridged-ids.txt")
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", False)
    monkeypatch.setattr(bot, "IMAGE_TEXT", True)
    monkeypatch.setattr(bot, "HELD", {})
    monkeypatch.setattr(bot, "HOLD_TIMER", hold.FakeTimer)
    hold.FakeTimer.made = []
    r = Obj(mailed=[], logged=[], account=hold.Account())
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: r.mailed.append((a, k)))
    monkeypatch.setattr(bot, "log", r.logged.append)
    monkeypatch.setattr(imgtext, "read", lambda path, log: list(hold.SIGN))
    monkeypatch.setattr(bot, "transcribe", lambda path: "please call me back")
    return r


def seen():
    return set(bot.SEEN.read_text().split()) if bot.SEEN.exists() else set()


def down(account, mid, chat_id, text="", **kw):
    """A message that arrived while the bot was down or deaf: core holds it
    unseen, and no IncomingMsg for it will ever be read."""
    m = account.add(mid, chat_id, text, **kw)
    account.fresh.append(m)
    return m


def kinds(w):
    return [(k["kind"], k["dc_msg_id"]) for _, k in w.mailed]


# -- 1. a request while down -----------------------------------------------------

def test_a_request_while_down_is_mailed_once_held_and_not_accepted(w):
    a = w.account
    down(a, 50, chat_id=5, text="who is this", request=True)
    assert a.get_fresh_messages() == [], "the badge query must not list a request"
    bot.sweep(a)
    assert kinds(w) == [("request", 50)]
    assert w.mailed[0][0][3].startswith(BANNER)
    assert not a.chats[5].accepted
    assert "50" in seen() and a.marked == [50]
    bot.sweep(a)                                # the next restart: SEEN stops it
    assert len(w.mailed) == 1


# -- 2. a muted-chat message while down, archived or not --------------------------

@pytest.mark.parametrize("archived", [False, True], ids=["muted", "muted-archived"])
def test_a_muted_chat_message_while_down_is_mailed_once(w, archived):
    a = w.account
    down(a, 60, chat_id=6, text="the parcel came", name="Bob")
    a.chats[6].info.is_muted = True
    a.chats[6].info.archived = archived
    assert a.get_fresh_messages() == [], "the badge query must not list a muted chat"
    bot.sweep(a)
    assert kinds(w) == [("message", 60)]
    assert "the parcel came" in w.mailed[0][0][3]
    assert "60" in seen() and a.marked == [60]
    bot.sweep(a)
    assert len(w.mailed) == 1


# -- 3, 4. no double mail around a restart ----------------------------------------

@pytest.mark.parametrize("request_", [False, True], ids=["whole-message", "request"])
def test_a_message_fetched_at_start_is_mailed_once(w, request_):
    """start_io() fetched it: core lists it unseen AND queued its
    IncomingMsg. The sweep mails it; the live path reads the queued event
    and skips it."""
    a = w.account
    a.add(70, chat_id=7, text="sent while you slept", request=request_)
    a.arrive(70)                                # fresh, and its IncomingMsg queued
    bot.sweep(a)
    assert len(w.mailed) == 1
    run_live(a)
    assert kinds(w) == [("request" if request_ else "message", 70)]
    assert not a.chats[7].accepted
    assert a.marked == [70]
    assert "RECV msg 70: already bridged (SEEN), skipped" in w.logged


# -- 5. what the listing leaves out, and its order --------------------------------

def test_the_listing_leaves_out_what_is_not_a_contact_message_and_runs_oldest_first(w):
    a = w.account
    down(a, 80, chat_id=8, text="device news")           # the device chat
    a.chats[8].info.is_device_chat = True
    a.msgs[80].snap.from_id = 5
    down(a, 81, chat_id=9, text="note to self")          # self-talk
    a.chats[9].info.is_self_talk = True
    down(a, 82, chat_id=10, text="Member added")         # an info message
    a.msgs[82].snap.is_info, a.msgs[82].snap.from_id = True, 2
    down(a, 83, chat_id=10, text="from this device")     # from self
    a.msgs[83].snap.from_id = 1
    a.add(84, chat_id=10, text="already read")           # not fresh
    down(a, 85, chat_id=11, text="blocked")              # core hides a blocked chat
    a.chats[11].blocked = True
    down(a, 86, chat_id=12, text="newer", request=True)  # kept: newer by its time
    a.msgs[86].snap.timestamp = 2000
    down(a, 87, chat_id=13, text="older")                # kept: older by its time
    a.msgs[87].snap.timestamp = 1000
    assert [m.id for m in bot.unseen_incoming(a)] == [87, 86]
    bot.sweep(a)
    assert kinds(w) == [("message", 87), ("request", 86)]


def test_the_listing_stops_at_the_chats_fresh_count(w):
    """Only a chat with a fresh count is read, newest back, and the walk
    stops once it has found that many."""
    a = w.account
    for mid in (90, 91, 92):
        a.add(mid, chat_id=14, text=f"m{mid}")
    a.fresh.append(a.msgs[92])
    read = []
    for mid in (90, 91, 92):
        m = a.msgs[mid]
        m.get_snapshot = lambda m=m: read.append(m.id) or m.snap
    assert [m.id for m in bot.unseen_incoming(a)] == [92]
    assert read == [92], "the older messages are never read"


# -- 6. the held path is unchanged -------------------------------------------------

def test_a_request_premessage_the_sweep_held_is_released_once_on_its_incoming_msg(w):
    """A pre-message in a contact request, fetched at start: the sweep holds
    it (not remembered), so the live path's SEEN skip does not shadow the
    release. One mail, as a request, and the chat is not accepted."""
    a = w.account
    a.premessage(100, 3, request=True)
    a.arrive(100)
    bot.sweep(a)
    assert w.mailed == [] and 100 in bot.HELD and "100" not in seen()
    a.land(100)
    run_live(a)
    assert kinds(w) == [("request", 100)]
    assert w.mailed[0][0][3].startswith(BANNER)
    assert bot.HELD == {} and not a.chats[3].accepted
    hold.FakeTimer.made[0].fire()               # a late timer: nothing more
    assert len(w.mailed) == 1


# -- 7. the live SEEN skip --------------------------------------------------------

def test_an_incoming_msg_for_an_id_in_seen_is_not_touched(w, monkeypatch):
    """Not mailed, not marked seen, not accepted, not held: even with
    requests accepted, and even for a pre-message."""
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", True)
    a = w.account
    a.premessage(110, chat_id=15, request=True)
    bot.remember(110)
    a.arrive(110)
    reads = a.reads
    run_live(a)
    assert w.mailed == [] and a.marked == []
    assert not a.chats[15].accepted and bot.HELD == {} and hold.FakeTimer.made == []
    assert a.reads == reads, "the skip comes before any read of the message"
    assert "RECV msg 110: already bridged (SEEN), skipped" in w.logged

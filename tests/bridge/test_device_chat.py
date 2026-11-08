"""An account with the core's `bot` flag off has a Device messages chat and a
Saved messages chat, and neither is mailed to the agent.

With the flag on, the core made neither chat. `sessionphone setup` no longer
sets it, and the bot turns an old one off at start, so a fresh account now
has both:

  - "Device messages" (is_device_chat): the core's own notices, from the
    device contact (id 5). A fresh account gets two at setup, a "Get in
    contact!" text and an image, both unseen (InFresh). The real core 2.62.0
    mailed both from the startup sweep before the sweep left this chat out
    (scratch/proof-001, row 5).
  - "Saved messages" (is_self_talk): the account's notes to itself. A saved
    copy keeps its original sender and is already seen (InSeen), measured on
    2.62.0 (scratch/fix-003/device_events.py).

The sweep leaves both chats out (bot.unseen_incoming). The live path skips
an IncomingMsg in either chat before anything else reads or mails it. The
core's add_device_message raises MsgsChanged there, not IncomingMsg
(measured), so this is the guard for any core notice that does raise one.

Each guard is tested on its own:

  - the real chats as 2.62.0 holds them: fails if the sweep lists core's
    fresh messages again, as it did before;
  - the chat flags alone, with an ordinary sender and unseen: fails if the
    sweep's device/self-talk skip is removed;
  - live: fails if the live path's device/self-talk skip is removed; it
    comes before the hold, so no timer releases a notice later.

Fakes only: no account, no rpc-server, no Vision, no mail delivery.
"""
import pytest
import test_incoming_live as base
import test_premessage_hold as hold
from deltachat_rpc_client.const import SpecialContactId

from sessionphone import imgtext
from sessionphone.bridge import bot

Obj, run_live = base.Obj, base.run_live
DEVICE_CHAT, SAVED_CHAT, STRANGER_CHAT, OWNER_CHAT = 11, 10, 12, 13
GET_IN_CONTACT = ('Get in contact!\n\n\U0001f64c Tap "QR code" on the main screen of both '
                  'devices. Choose "Scan QR Code" on one device, and point it at the other')
OWNER = 20  # an ordinary contact id, above SpecialContactId.LAST_SPECIAL


class Account(hold.Account):
    """The bridge's fake account, with the config calls bot_flag_off() makes."""

    def __init__(self, config):
        super().__init__()
        self.config, self.set_calls = dict(config), []

    def get_config(self, key):
        return self.config.get(key)

    def set_config(self, key, value):
        self.set_calls.append((key, value))
        self.config[key] = value


@pytest.fixture(params=[{"bot": "0"}, {}], ids=["bot-0", "bot-unset"])
def w(request, monkeypatch, tmp_path):
    """A flag-off account on the DEFAULT profile, DC_ACCEPT_REQUESTS off,
    SEEN in a scratch dir; mail_in, log and imgtext.read recorded."""
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setattr(bot, "SEEN", tmp_path / "bridged-ids.txt")
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", False)
    monkeypatch.setattr(bot, "IMAGE_TEXT", True)
    monkeypatch.setattr(bot, "HELD", {})
    monkeypatch.setattr(bot, "HOLD_TIMER", hold.FakeTimer)
    hold.FakeTimer.made = []
    r = Obj(mailed=[], logged=[], ocr=[], account=Account(request.param))
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: r.mailed.append((a, k)))
    monkeypatch.setattr(bot, "log", r.logged.append)

    def read(path, log):
        r.ocr.append(path)
        return list(hold.SIGN)
    monkeypatch.setattr(imgtext, "read", read)
    return r


def seen():
    return set(bot.SEEN.read_text().split()) if bot.SEEN.exists() else set()


def mailed_ids(w):
    return [k["dc_msg_id"] for _, k in w.mailed]


def device_chat(a, *, sender=SpecialContactId.DEVICE, mids=(10, 11)):
    """The Device messages chat a fresh flag-off account has: an image and the
    "Get in contact!" text, from the device contact, both unseen."""
    img, txt = mids
    a.add(img, DEVICE_CHAT, "", name="Device messages", file="/blobs/welcome.jpg",
          file_mime="image/jpeg", view_type="Image")
    a.add(txt, DEVICE_CHAT, GET_IN_CONTACT, name="Device messages")
    a.chats[DEVICE_CHAT].info.name = "Device messages"
    a.chats[DEVICE_CHAT].info.is_device_chat = True
    for mid in mids:
        a.msgs[mid].snap.from_id = sender
        a.fresh.append(a.msgs[mid])


def saved_chat(a, mid, *, sender=SpecialContactId.SELF, fresh=False, text="note to self"):
    """The Saved messages chat, with one entry."""
    a.add(mid, SAVED_CHAT, text, name="Me")
    a.chats[SAVED_CHAT].info.name = "Saved messages"
    a.chats[SAVED_CHAT].info.is_self_talk = True
    a.msgs[mid].snap.from_id = sender
    if fresh:
        a.fresh.append(a.msgs[mid])


def start(a):
    """main()'s order up to the event loop, minus rpc and IO: the flag
    helper, then the sweep."""
    assert bot.bot_flag_off(a) is False, "a flag-off account is left alone"
    assert a.set_calls == []
    bot.sweep(a)


def assert_untouched(w, *mids):
    """Not mailed, not read by OCR, not marked seen, not remembered."""
    a = w.account
    for mid in mids:
        assert mid not in mailed_ids(w), f"msg {mid} was mailed"
        assert mid not in a.marked, f"msg {mid} was marked seen"
        assert str(mid) not in seen(), f"msg {mid} was remembered"
    assert w.ocr == [], "a device image was read"


# -- the startup sweep ----------------------------------------------------------

def test_the_sweep_mails_neither_the_device_nor_the_saved_chat(w):
    """As 2.62.0 holds them: the Device chat's two unseen notices, and a
    Saved copy of one (InSeen, its sender kept). A stranger who wrote while
    the bot was down is still mailed, as a request, so the sweep ran."""
    a = w.account
    device_chat(a)
    saved_chat(a, 12, sender=SpecialContactId.DEVICE, text=GET_IN_CONTACT)
    a.add(30, STRANGER_CHAT, "who is this", request=True)
    a.fresh.append(a.msgs[30])
    assert {m.id for m in a.get_fresh_messages()} >= {10, 11}, \
        "core's badge query lists the device notices; the sweep must not"
    start(a)
    assert mailed_ids(w) == [30]
    assert w.mailed[0][1]["kind"] == "request"
    assert_untouched(w, 10, 11, 12)
    assert a.msgs[10] in a.fresh and a.msgs[11] in a.fresh, "left as core holds them"


def test_the_chat_flags_alone_keep_both_chats_out_of_the_sweep(w):
    """The sweep's chat skip on its own: unseen entries from an ordinary
    contact, so the sender and state checks would let them through."""
    a = w.account
    device_chat(a, sender=OWNER)
    saved_chat(a, 12, sender=OWNER, fresh=True, text="a contact's message, saved")
    for mid in (10, 11, 12):
        assert a.msgs[mid].snap.from_id > SpecialContactId.LAST_SPECIAL
    start(a)
    assert w.mailed == []
    assert_untouched(w, 10, 11, 12)
    assert bot.unseen_incoming(a) == []


def test_the_next_restart_still_mails_neither(w):
    """Nothing was remembered or marked seen, so each restart lists them
    again; each restart leaves them out again."""
    a = w.account
    device_chat(a)
    start(a)
    start(a)
    assert_untouched(w, 10, 11)


# -- the live path ----------------------------------------------------------------

@pytest.mark.parametrize("which", ["device", "saved"])
def test_a_live_incoming_msg_in_either_chat_is_not_mailed(w, which):
    """The core raised IncomingMsg in the Device or the Saved chat, before
    an owner writes. The owner is mailed; the notice is not."""
    a = w.account
    if which == "device":
        device_chat(a)
        a.fresh.clear()
        a.arrive(10)
        a.arrive(11)
        mids = (10, 11)
    else:
        saved_chat(a, 12, sender=OWNER)
        a.arrive(12)
        mids = (12,)
    a.add(40, OWNER_CHAT, "call me", name="Owner")
    a.arrive(40)
    assert bot.bot_flag_off(a) is False
    run_live(a)
    assert mailed_ids(w) == [40], "only the owner is mailed"
    assert_untouched(w, *mids)
    for mid in mids:
        assert any(line.startswith(f"RECV msg {mid}: ") and "not mailed" in line
                   for line in w.logged), w.logged


def test_the_live_skip_comes_before_the_hold(w):
    """A device notice whose file has not landed: not held, no timer, so
    no later release mails it."""
    a = w.account
    device_chat(a)
    a.fresh.clear()
    a.msgs[10].snap.file, a.msgs[10].snap.download_state = None, "Available"
    assert bot.is_pending(a.msgs[10].snap), "the fake must be a pre-message"
    a.arrive(10)
    assert bot.bot_flag_off(a) is False
    run_live(a)
    assert w.mailed == []
    assert bot.HELD == {} and hold.FakeTimer.made == []
    assert_untouched(w, 10)

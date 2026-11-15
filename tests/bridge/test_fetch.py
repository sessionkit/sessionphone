"""dc-fetch's job in the bot: {"fetch": N} (PHONE-283 design §7 cases 1-5, §10 The fetch).

The bot fetches only what it itself mailed as not downloaded: the registry
(<home>/not-downloaded.json) says what that is, and with what route (`ocr`
False is a stranger's file, and stays one when it is fetched). A refusal is
channels.Refused, which the outbox loop acks ok:false and dc-fetch turns into
exit 3, nothing fetched.

  1. an id not in the registry (an Undecipherable partial is never recorded);
  2. a message that is gone: refused, its entry dropped;
  3. a chat that is still a contact request: refused, the entry kept;
  4. a file already there: marked `fetching`, then mailed whole now, the
     policy bypassed; the entry is taken out, so a second fetch is case 1;
  5. a pending file (Available or Failure): download_full_message once,
     marked `fetching`, then PHONE-284's hold(account, mid, chat_id,
     "message", ocr) with ocr from the registry. Mailed whole once on
     MsgsChanged Done; on its timeout the partial line, and the entry stays.

Fakes only: PHONE-284's FakeAccount (test_premessage_hold) with a fake rpc
for get_existing_msg_ids and download_full_message, a phone.json and blobs in
tmp. No account, relay, OCR helper, whisper or sendmail.
"""
from copy import copy as fresh_copy   # a new snapshot per read, as core gives
import os
import threading

import pytest
import test_download_policy as dp
import test_premessage_hold as hold
from deltachat_rpc_client.rpc import JsonRpcError

from sessionphone import channels, imgtext
from sessionphone.bridge import attach, bot

FakeTimer, run_live, body = hold.FakeTimer, hold.run_live, hold.body
UNTRUSTED, PARTIAL, BANNER = hold.UNTRUSTED, hold.PARTIAL, hold.BANNER
NOT_PASSED_ON, NOT_ON_DISK, MB20 = dp.NOT_PASSED_ON, dp.NOT_ON_DISK, dp.MB20
STARTED = "fetch started for msg 100; it is mailed when it lands"

world, phone_json, pw = hold.world, dp.phone_json, dp.pw   # the fixtures this file builds on


class FakeRpc:
    """The two rpc calls a fetch makes through account._rpc: which ids still
    exist, and the download. A download records its call, and may be refused
    (refuse) or run a hook (on_download) that stands for core's response."""

    def __init__(self, acct):
        self.acct = acct
        self.downloads = []
        self.refuse = None
        self.on_download = None

    def get_existing_msg_ids(self, account_id, ids):
        assert account_id == self.acct.id
        return [i for i in ids if i in self.acct.msgs and i not in self.acct.gone]

    def download_full_message(self, account_id, mid):
        assert account_id == self.acct.id
        self.downloads.append(mid)
        if self.refuse is not None:
            raise self.refuse
        if self.on_download is not None:
            self.on_download(mid)


class Account(dp.Account):
    """test_download_policy's account, plus the rpc a fetch uses, messages that
    are gone, a file that lands with real bytes, and download states read in a
    scripted order (a core that moves a state on LATE)."""

    id = 1

    def __init__(self):
        super().__init__()
        self._rpc = FakeRpc(self)
        self.gone = set()
        self.script = {}        # msg id -> the download states its next reads give

    def land_bytes(self, mid, data=b"the whole photo"):
        """The post-message landed: a real blob, Done, on the same row."""
        blob = self.blobs / f"{mid:04d}-landed.jpg"
        blob.write_bytes(data)
        self.update(mid, file=str(blob), download_state="Done")
        return blob

    def get_message_by_id(self, mid):
        m = super().get_message_by_id(mid)
        if self.script.get(mid):
            m.snap.download_state = self.script[mid].pop(0)
        return m


@pytest.fixture
def fw(pw, monkeypatch, tmp_path):
    """test_download_policy's world (the policy read at start, a home in tmp) on
    this Account; FETCH_SLEEP recorded, never slept. -> the world, with .rpc
    and .slept."""
    acct = Account()
    acct.blobs = pw.account.blobs
    pw.account, pw.rpc, pw.slept = acct, acct._rpc, []
    monkeypatch.setattr(bot, "FETCH_SLEEP", pw.slept.append)
    return pw


def registry(w):
    return attach.entries(w.home)


def fetch(w, mid=100):
    return bot.fetch_job(w.account, mid)


def refused(w, mid=100):
    with pytest.raises(channels.Refused) as e:
        fetch(w, mid)
    assert str(e.value).startswith("REFUSED: ") and str(e.value).endswith("nothing was fetched.")
    return str(e.value)


def turned_away(w, mid=100, chat_id=3, **kw):
    """A landed file the policy turned away at C (over the limit): mailed as
    a partial and recorded. The mail is cleared."""
    m = dp.live(w, mid, chat_id, size=MB20, **kw)
    assert registry(w)[mid]["reason"] == "size"
    w.mailed.clear()
    return m


def timed_out(w, mid=100, chat_id=3, **kw):
    """A pre-message held by PHONE-284, its hold run out: mailed as a timeout
    partial and recorded. The mail and the timers are cleared."""
    hold.held_then_live(w, mid, chat_id, **kw)
    FakeTimer.made[-1].fire()
    assert registry(w)[mid]["reason"] == "timeout"
    w.mailed.clear()
    FakeTimer.made.clear()


def failed(w, mid=100, chat_id=3):
    """A held pre-message core gave up on (Failure): mailed as a failure
    partial at once and recorded."""
    hold.held_then_live(w, mid, chat_id)
    w.account.update(mid, download_state="Failure")
    w.account.changed(mid, chat_id)
    run_live(w.account)
    assert registry(w)[mid]["reason"] == "failure"
    w.mailed.clear()
    FakeTimer.made.clear()


# -- 1. not in the registry: refused, nothing fetched -------------------------------

def test_an_unknown_id_is_refused_and_nothing_is_fetched(fw):
    fw.account.landed(100, 3)
    why = refused(fw)
    assert why == ("REFUSED: msg 100 was not left undownloaded by this bot: nothing to fetch; "
                   "nothing was fetched.")
    assert fw.rpc.downloads == [] and fw.mailed == [] and bot.HELD == {}
    assert any(line.startswith("REFUSED fetch of msg 100: not in the registry") for line in fw.logged)


def test_a_message_this_bot_mailed_whole_is_refused(fw):
    dp.live(fw)                             # passed on, saved, never recorded
    fw.mailed.clear()
    refused(fw)
    assert fw.mailed == []


@pytest.mark.parametrize("raw", ["100", True, 0, -5, None, 1.5, [100]])
def test_a_fetch_job_that_names_no_message_id_is_refused(fw, raw):
    with pytest.raises(channels.Refused) as e:
        bot.fetch_job(fw.account, raw)
    assert 'a fetch job needs "fetch", a message id (a positive integer)' in str(e.value)
    assert fw.rpc.downloads == []


def test_an_undecipherable_partial_is_refused(fw):
    """Never recorded (no fetch can decrypt it), so a fetch of it is case 1."""
    hold.held_then_live(fw)
    fw.account.update(100, download_state="Undecipherable")
    fw.account.changed(100, 3)
    run_live(fw.account)
    assert registry(fw) == {}
    refused(fw)
    assert fw.rpc.downloads == []


def test_an_entry_whose_message_became_undecipherable_is_refused_and_dropped(fw):
    timed_out(fw)
    fw.account.update(100, download_state="Undecipherable")
    why = refused(fw)
    assert "msg 100 could not be decrypted, and a second fetch cannot decrypt it" in why
    assert fw.rpc.downloads == [] and registry(fw) == {}


# -- 2. gone: refused, the entry dropped --------------------------------------------

def test_a_message_that_is_gone_is_refused_and_its_entry_dropped(fw):
    turned_away(fw)
    fw.account.gone.add(100)
    why = refused(fw)
    assert why == "REFUSED: msg 100 is gone (deleted, or expired): nothing to fetch; nothing was fetched."
    assert registry(fw) == {} and fw.mailed == [] and fw.rpc.downloads == []
    refused(fw)                             # and again: case 1 now


# -- 3. a contact request: refused until the owner accepts the chat ------------------

def test_a_strangers_file_is_refused_while_the_chat_is_a_contact_request(fw):
    turned_away(fw, request=True, sender="Stranger")
    why = refused(fw)
    assert why == ("REFUSED: msg 100 is a contact request: the owner accepts the chat first; "
                   "nothing was fetched.")
    assert registry(fw)[100]["ocr"] is False and "fetching" not in registry(fw)[100]
    assert fw.mailed == [] and fw.rpc.downloads == []


def test_a_pending_strangers_file_is_refused_while_the_chat_is_a_contact_request(fw):
    timed_out(fw, request=True, name="Stranger")
    refused(fw)
    assert fw.rpc.downloads == [] and bot.HELD == {}
    assert 100 in registry(fw)


# -- 4. the file is already here: mailed whole now, once, the policy bypassed --------

def test_a_done_entry_is_mailed_whole_once_with_the_policy_bypassed(fw):
    m = turned_away(fw)
    assert fetch(fw) == "msg 100 mailed"
    copy = fw.saves / "chat-3" / "100-IMG_0042.jpg"
    said = body(fw)
    assert said.startswith(f"[attachment: IMG_0042.jpg (image/jpeg) at {copy}]")
    assert PARTIAL not in said and "dc-fetch" not in said
    assert imgtext.OPEN in said             # an image, a trusted sender's: its text is read
    assert fw.ocr == [str(copy)]
    assert copy.read_bytes() == open(m.snap.file, "rb").read()
    (_, kw), = fw.mailed
    assert kw["kind"] == "message" and kw["dc_msg_id"] == 100
    assert fw.rpc.downloads == []           # it was here: nothing to download
    assert registry(fw) == {}
    refused(fw)                             # a second fetch: case 1
    assert len(fw.mailed) == 1


def test_a_type_off_file_fetched_is_passed_on_and_saved(fw):
    fw.write(download_types="image")        # video off
    fw.read()
    dp.live(fw, name="clip.mp4", mime="video/mp4", view="Video", size=2_000_000)
    fw.mailed.clear()
    assert fetch(fw) == "msg 100 mailed"
    copy = fw.saves / "chat-3" / "100-clip.mp4"
    assert body(fw) == f"[attachment: clip.mp4 (video/mp4) at {copy}]" and copy.exists()


def test_case_4_marks_the_entry_fetching_before_it_builds_the_text(fw, monkeypatch):
    """One bypass mechanism for cases 4 and 5: the `fetching` mark, put on the
    entry before message_text runs, and taken out by it."""
    turned_away(fw)
    order = []
    mark, text = attach.mark_fetching, bot.message_text

    def marking(home, mid):
        order.append("mark")
        return mark(home, mid)

    def building(snap, ocr=True):
        order.append(("text", dict(registry(fw)[snap.id])))
        return text(snap, ocr=ocr)
    monkeypatch.setattr(attach, "mark_fetching", marking)
    monkeypatch.setattr(bot, "message_text", building)
    fetch(fw)
    assert order[0] == "mark"
    assert order[1][0] == "text" and order[1][1]["fetching"] is True
    assert registry(fw) == {}


def test_a_strangers_done_file_fetched_after_acceptance_stays_untrusted(fw):
    """The route comes from the registry (ocr false), never from the chat now."""
    turned_away(fw, request=True, sender="Stranger")
    fw.account.chats[3].accept()
    assert fetch(fw) == "msg 100 mailed"
    copy = fw.saves / "untrusted" / "chat-3" / "100-IMG_0042.jpg"
    said = body(fw)
    assert f"[attachment: IMG_0042.jpg (image/jpeg) at {copy}{UNTRUSTED}]" in said
    assert imgtext.NOT_READ_REQUEST in said and fw.ocr == []
    assert copy.exists() and not (fw.saves / "chat-3").exists()
    (_, kw), = fw.mailed
    assert kw["kind"] == "message"          # the chat is accepted: no request banner
    assert registry(fw) == {}


def test_a_fetch_bypasses_the_policy_once_only(fw):
    """The entry is taken out when the text is built, so building that message's
    text again applies the policy again (and records it again)."""
    m = turned_away(fw)
    fetch(fw)
    assert bot.message_text(m.snap).startswith(PARTIAL)
    assert registry(fw)[100]["reason"] == "size"


# -- 5. the file is pending: download_full_message once, then PHONE-284's hold --------

def test_a_pending_entry_is_downloaded_once_marked_and_held_with_the_registry_route(fw, monkeypatch):
    timed_out(fw)
    calls = []
    real = bot.hold
    monkeypatch.setattr(bot, "hold", lambda *a, **k: (calls.append((a, k)), real(*a, **k)))
    assert fetch(fw) == STARTED
    assert fw.rpc.downloads == [100]
    (args, _), = calls
    assert args[:5] == (fw.account, 100, 3, "message", True)
    assert registry(fw)[100]["fetching"] is True
    assert 100 in bot.HELD and fw.mailed == []


def test_a_fetched_file_is_mailed_whole_once_on_msgs_changed_done(fw):
    timed_out(fw)
    fw.write(download_limit="100000")       # the next start: the landed photo is over it; the fetch bypasses it
    fw.read()
    fetch(fw)
    blob = fw.account.land_bytes(100)
    fw.account.changed(100, 3)
    fw.account.changed(100, 3)              # core may say it twice
    run_live(fw.account)
    copy = fw.saves / "chat-3" / "100-IMG_0042.jpg"
    said = body(fw)
    assert said.startswith(f"[attachment: IMG_0042.jpg (image/jpeg) at {copy}]\n\n{imgtext.OPEN}")
    assert copy.read_bytes() == blob.read_bytes()
    assert fw.ocr == [str(copy)]
    assert bot.HELD == {} and registry(fw) == {}
    assert fw.rpc.downloads == [100]
    refused(fw)                             # once: a second fetch is case 1
    assert len(fw.mailed) == 1


def test_a_fetch_that_times_out_gets_the_partial_line_and_the_entry_stays(fw):
    timed_out(fw)
    fetch(fw)
    FakeTimer.made[-1].fire()
    said = body(fw)
    assert said == (f"[partial, not downloaded yet: IMG_0042.jpg (image/jpeg, 345.9 KiB) - msg 100. "
                    f"{NOT_ON_DISK} Fetch it: dc-fetch 100.]")
    e = registry(fw)[100]
    assert e["reason"] == "timeout" and "fetching" not in e
    fw.mailed.clear()
    assert fetch(fw) == STARTED             # a second dc-fetch tries again
    assert fw.rpc.downloads == [100, 100]


def test_a_second_fetch_while_held_downloads_nothing_more(fw):
    timed_out(fw)
    fetch(fw)
    assert fetch(fw) == "fetch already started for msg 100; it is mailed when it lands"
    assert fw.rpc.downloads == [100] and len(FakeTimer.made) == 1


def test_a_second_fetch_after_the_file_landed_mails_it_once(fw):
    """The file lands while held, and a second dc-fetch comes before its
    MsgsChanged is heard: it goes out through the HELD pop, not case 4, so
    the MsgsChanged that follows mails nothing more (peer review do-007)."""
    timed_out(fw)
    assert fetch(fw) == STARTED
    fw.account.land_bytes(100)              # Done on the row; MsgsChanged not yet heard
    assert fetch(fw) == "msg 100 mailed"
    fw.account.changed(100, 3)
    run_live(fw.account)
    assert len(fw.mailed) == 1 and body(fw).startswith("[attachment: IMG_0042.jpg (image/jpeg) at ")
    assert bot.HELD == {} and registry(fw) == {} and fw.rpc.downloads == [100]
    refused(fw)                             # once: a third fetch is case 1


def test_a_pending_fetch_whose_entry_was_taken_meanwhile_holds_nothing(fw):
    """Case 5, as case 4: if the entry was taken after the job read it, the
    mark finds nothing, so the fetch is refused and nothing is held."""
    timed_out(fw)
    fw.rpc.on_download = lambda mid: attach.drop(fw.home, mid)
    why = refused(fw)
    assert "was not left undownloaded by this bot" in why
    assert fw.rpc.downloads == [100] and bot.HELD == {} and fw.mailed == [] and registry(fw) == {}


def test_a_held_fetch_that_became_undecipherable_is_refused_and_dropped(fw):
    timed_out(fw)
    fetch(fw)
    fw.account.update(100, download_state="Undecipherable")
    why = refused(fw)
    assert "a second fetch cannot decrypt it" in why
    assert registry(fw) == {} and fw.rpc.downloads == [100]


def test_a_file_that_lands_before_the_hold_is_mailed_at_once(fw):
    """core may finish before the hold can hear its MsgsChanged: the state is
    read again after the hold, and a Done file is released there."""
    timed_out(fw)
    fw.rpc.on_download = fw.account.land_bytes
    assert fetch(fw) == "msg 100 mailed"
    assert body(fw).startswith("[attachment: IMG_0042.jpg (image/jpeg) at ")
    assert bot.HELD == {} and registry(fw) == {}


def test_core_refusing_the_download_is_refused_and_the_entry_kept(fw):
    timed_out(fw)
    fw.rpc.refuse = JsonRpcError({"code": -1, "message": "Download already in progress."})
    why = refused(fw)
    assert "msg 100: Delta Chat core refused the fetch: Download already in progress." in why
    assert "fetching" not in registry(fw)[100] and bot.HELD == {}


def test_a_fetched_strangers_file_stays_under_untrusted_with_the_suffix(fw, monkeypatch):
    timed_out(fw, request=True, name="Stranger")
    fw.account.chats[3].accept()
    calls = []
    real = bot.hold
    monkeypatch.setattr(bot, "hold", lambda *a, **k: (calls.append(a), real(*a, **k)))
    assert fetch(fw) == STARTED
    assert calls[0][3:5] == ("message", False)     # ocr from the registry, not the chat now
    fw.account.land_bytes(100)
    fw.account.changed(100, 3)
    run_live(fw.account)
    copy = fw.saves / "untrusted" / "chat-3" / "100-IMG_0042.jpg"
    said = body(fw)
    assert f"at {copy}{UNTRUSTED}]" in said and imgtext.NOT_READ_REQUEST in said
    assert fw.ocr == [] and copy.exists()
    assert registry(fw) == {}


# -- PHONE-284's Failure, fetched: the state is read again after the call ------------

def test_a_failure_partial_is_fetched_with_one_download(fw):
    failed(fw)
    fw.rpc.on_download = lambda mid: fw.account.update(mid, download_state="InProgress")
    assert fetch(fw) == STARTED
    assert fw.rpc.downloads == [100] and 100 in bot.HELD


def test_a_failure_core_moves_on_late_is_not_released_back_as_a_failure(fw):
    """download_full_message returns while the message still reads Failure;
    core moves it to InProgress a few reads later. Held at once, the first
    MsgsChanged would release it straight back as a failure partial."""
    failed(fw)
    fw.account.script[100] = ["Failure"] * 4 + ["InProgress"]  # the first read is before the call
    assert fetch(fw) == STARTED
    assert fw.slept == [bot.FETCH_SETTLE_STEP_S] * 3     # re-read until it moved on
    fw.account.changed(100, 3)
    run_live(fw.account)
    assert fw.mailed == [] and 100 in bot.HELD           # still pending: held, not a partial
    fw.account.land_bytes(100)
    fw.account.changed(100, 3)
    run_live(fw.account)
    assert body(fw).startswith("[attachment: IMG_0042.jpg (image/jpeg) at ")
    assert registry(fw) == {} and fw.rpc.downloads == [100]


def test_a_failure_that_stays_a_failure_is_mailed_as_one_and_the_entry_stays(fw):
    failed(fw)
    assert fetch(fw) == STARTED
    assert len(fw.slept) == bot.FETCH_SETTLE_TRIES      # bounded: never a hang
    assert any("still Failure after the fetch" in line for line in fw.logged)
    fw.account.changed(100, 3)
    run_live(fw.account)
    said = body(fw)
    assert said.startswith("[partial, not downloaded yet (the download failed): ")
    assert said.endswith("Fetch it: dc-fetch 100.]")
    e = registry(fw)[100]
    assert e["reason"] == "failure" and "fetching" not in e


def test_an_available_entry_is_not_re_read(fw):
    timed_out(fw)                           # Available still
    fetch(fw)
    assert fw.slept == []


# -- the registry: the mark ----------------------------------------------------------

def test_a_landed_message_no_fetch_asked_for_keeps_its_entry(fw):
    """message_text takes an entry only when a fetch marked it."""
    m = turned_away(fw)
    bot.message_text(m.snap)
    assert 100 in registry(fw)


def test_without_a_policy_read_every_fetch_is_refused(fw, monkeypatch):
    turned_away(fw)
    monkeypatch.setattr(bot, "POLICY_HOME", None)
    refused(fw)


def test_the_fetch_tail_names_a_message_a_fetch_then_takes(fw):
    """The partial line's own tail (`dc-fetch 100`) is the id this job takes."""
    m = turned_away(fw)
    said = bot.message_text(m.snap)
    assert said.endswith(" Fetch it: dc-fetch 100.]")
    assert fetch(fw) == "msg 100 mailed"


def test_two_messages_one_fetched_one_left(fw):
    turned_away(fw, 100, 3)
    turned_away(fw, 101, 3)
    fetch(fw, 101)
    assert list(registry(fw)) == [100]
    assert os.path.exists(fw.saves / "chat-3" / "101-IMG_0042.jpg")


# -- once, whatever the threads do: the claim (peer review 2, do-007) ---------------
# Three races were found one at a time (3c91733, 57a441d, 8c401a5). The class is
# closed by one claim per msg id (bot.claim): a release holds it from its HELD pop
# to its mail, and fetch_job for the whole job. These tests strike every pair: a
# release (on landing, or the timer) at each point of a fetch, and a fetch at each
# point of a release, each on its own thread, as the event loop, the timer and the
# outbox loop are. A real core hands out a new snapshot on each read: so does
# the fake here (a shared one would hide a stale read).

WAIT_S = 0.3        # how long a thread is given to run into the claim


def fresh_snapshots(monkeypatch):
    monkeypatch.setattr(hold.base.FakeMsg, "get_snapshot", lambda self: fresh_copy(self.snap))


def at(monkeypatch, owner, name, act):
    """Run ACT once, the first time OWNER.NAME is called, before the call."""
    real, done = getattr(owner, name), []

    def first(*a, **k):
        if not done:
            done.append(act())
        return real(*a, **k)
    monkeypatch.setattr(owner, name, first)
    return done


def on_thread(fn, *args):
    """FN on its own thread, given WAIT_S to run. -> (the thread, its outcome
    list: the return value, or the exception), and whether it was still
    running after WAIT_S (it waits on the claim)."""
    out = []

    def run():
        try:
            out.append(fn(*args))
        except Exception as e:      # noqa: BLE001 - the outcome under test
            out.append(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(WAIT_S)
    return t, out, t.is_alive()


def finish(t):
    t.join(5)
    assert not t.is_alive(), "a thread never got the claim"


def whole(w):
    return body(w).startswith("[attachment: IMG_0042.jpg (image/jpeg) at ")


# every point of a second fetch of a held message, and of a first fetch (case 5)
HELD_POINTS = [("attach", "entry"), ("rpc", "get_existing_msg_ids"),
               ("account", "get_message_by_id"), ("account", "get_chat_by_id")]
CASE5_POINTS = HELD_POINTS + [("rpc", "download_full_message"), ("attach", "mark_fetching"),
                              ("bot", "hold")]


def owner(w, which):
    return {"attach": attach, "bot": bot, "rpc": w.rpc, "account": type(w.account)}[which]


@pytest.mark.parametrize("which,name", HELD_POINTS)
def test_a_release_on_landing_at_any_point_of_a_second_fetch_mails_once(fw, monkeypatch, which, name):
    fresh_snapshots(monkeypatch)
    timed_out(fw)
    assert fetch(fw) == STARTED
    races = []
    at(monkeypatch, owner(fw, which), name, lambda: races.append(
        (fw.account.land_bytes(100), on_thread(bot.release, fw.account, 100, "landed"))))
    ack = fetch(fw)
    (_, (t, out, waited)), = races
    finish(t)
    assert waited, "the release ran inside the fetch: no claim"
    # exactly one of the two mailed it: the fetch (the file had landed when it read
    # the message) and the release then found nothing, or the release, after it
    assert (ack, out) in [("msg 100 mailed", [False]),
                          ("fetch already started for msg 100; it is mailed when it lands", [True])]
    assert len(fw.mailed) == 1 and whole(fw)
    assert bot.HELD == {} and registry(fw) == {} and fw.rpc.downloads == [100]
    refused(fw)


@pytest.mark.parametrize("which,name", CASE5_POINTS)
def test_a_release_on_landing_at_any_point_of_a_first_fetch_mails_once(fw, monkeypatch, which, name):
    fresh_snapshots(monkeypatch)
    timed_out(fw)
    races = []
    at(monkeypatch, owner(fw, which), name, lambda: races.append(
        (fw.account.land_bytes(100), on_thread(bot.release, fw.account, 100, "landed"))))
    fetch(fw)
    (_, (t, out, waited)), = races
    finish(t)
    assert waited and out == [False]
    assert len(fw.mailed) == 1 and whole(fw)
    assert bot.HELD == {} and registry(fw) == {}
    refused(fw)


@pytest.mark.parametrize("which,name", HELD_POINTS)
def test_the_timer_at_any_point_of_a_second_fetch_mails_one_partial_and_keeps_the_entry(
        fw, monkeypatch, which, name):
    fresh_snapshots(monkeypatch)
    timed_out(fw)
    assert fetch(fw) == STARTED
    timer, races = FakeTimer.made[-1], []
    at(monkeypatch, owner(fw, which), name, lambda: races.append(on_thread(timer.fire)))
    assert fetch(fw) == "fetch already started for msg 100; it is mailed when it lands"
    (t, _, waited), = races
    finish(t)
    assert waited
    assert len(fw.mailed) == 1 and body(fw).startswith(PARTIAL)
    e = registry(fw)[100]
    assert e["reason"] == "timeout" and "fetching" not in e and bot.HELD == {}
    assert fw.rpc.downloads == [100]
    assert fetch(fw) == STARTED and fw.rpc.downloads == [100, 100]     # the agent may try again


# every point of a release, from its HELD pop to its mail: whole (the entry
# taken), or a partial (the entry recorded again)
RELEASE_POINTS = [("account", "get_message_by_id"), ("bot", "bridge_one"),
                  ("attach", "take_fetching"), ("bot", "mail_in")]
PARTIAL_POINTS = [("account", "get_message_by_id"), ("bot", "bridge_one"),
                  ("attach", "record"), ("bot", "mail_in")]


@pytest.mark.parametrize("which,name", RELEASE_POINTS)
def test_a_fetch_at_any_point_of_a_release_on_landing_mails_nothing_more(fw, monkeypatch, which, name):
    fresh_snapshots(monkeypatch)
    timed_out(fw)
    assert fetch(fw) == STARTED
    fw.account.land_bytes(100)
    races = []
    at(monkeypatch, owner(fw, which), name, lambda: races.append(on_thread(fetch, fw)))
    assert bot.release(fw.account, 100, "landed")
    (t, out, waited), = races
    finish(t)
    assert waited, "the fetch ran inside the release: no claim"
    (e,) = out
    assert isinstance(e, channels.Refused) and "was not left undownloaded by this bot" in str(e)
    assert len(fw.mailed) == 1 and whole(fw)
    assert bot.HELD == {} and registry(fw) == {} and fw.rpc.downloads == [100]


@pytest.mark.parametrize("which,name", PARTIAL_POINTS)
def test_a_fetch_at_any_point_of_a_timeout_release_fetches_again_after_it(fw, monkeypatch, which, name):
    """The release mails the partial and records the entry again (unmarked); the
    fetch, after it, is a fresh case 5: one more download, held again."""
    fresh_snapshots(monkeypatch)
    timed_out(fw)
    assert fetch(fw) == STARTED
    races = []
    at(monkeypatch, owner(fw, which), name, lambda: races.append(on_thread(fetch, fw)))
    assert bot.release(fw.account, 100, "timeout")
    (t, out, waited), = races
    finish(t)
    assert waited and out == [STARTED]
    assert len(fw.mailed) == 1 and body(fw).startswith(PARTIAL)
    assert fw.rpc.downloads == [100, 100] and 100 in bot.HELD
    assert registry(fw)[100]["fetching"] is True


def test_the_claim_is_one_per_message_and_lets_others_by(fw):
    """Two ids on different stripes never wait for each other; one id always
    maps to one lock, and a fetch may release() under its own claim."""
    assert bot.claim(100) is bot.claim(100)
    assert bot.claim(100) is not bot.claim(101)
    def other():
        got = bot.claim(101).acquire(blocking=False)    # never waits: a timing-free check
        if got:
            bot.claim(101).release()
        return got
    with bot.claim(100):
        t, out, _ = on_thread(other)
        finish(t)
        assert out == [True]
        with bot.claim(100):    # reentrant
            pass


# -- core announces a fetch's finished download as IncomingMsg -----------------------
# Design §8: a bot=1 account hears of a split message only when it is complete,
# so a fetch's download may come back as IncomingMsg, not MsgsChanged. The live
# path decides under the message's claim and skips an id in SEEN under it, so
# the fetch and that IncomingMsg mail it once between them (do-007's final
# review, races A and B: both mailed it twice before the claim).

def announced(w):
    """core's response to download_full_message: the file lands, and the
    complete message is announced as IncomingMsg (queued on the event loop)."""
    def go(mid):
        w.account.land_bytes(mid)
        w.account.arrive(mid)
    return go


def test_an_incoming_msg_queued_after_the_fetch_released_it_mails_nothing_more(fw):
    """Race A, no threads: the file lands before the fetch's re-read after its
    hold, so the fetch mails it; the queued IncomingMsg is read after."""
    timed_out(fw)
    fw.rpc.on_download = announced(fw)
    fetch(fw)
    assert len(fw.mailed) == 1 and whole(fw)
    run_live(fw.account)                    # the event loop reads the queued IncomingMsg
    assert len(fw.mailed) == 1
    assert bot.HELD == {} and registry(fw) == {} and fw.rpc.downloads == [100]
    assert "RECV msg 100: already bridged (SEEN), skipped" in fw.logged


def test_an_incoming_msg_on_the_event_thread_during_the_fetch_waits_and_mails_nothing_more(
        fw, monkeypatch):
    """Race B: the IncomingMsg is handled on the event thread in the window
    from download_full_message to the fetch's hold(). It waits on the claim,
    and then finds the id in SEEN: the fetch's release mailed it."""
    fresh_snapshots(monkeypatch)
    timed_out(fw)
    races = []

    def go(mid):
        announced(fw)(mid)
        races.append(on_thread(run_live, fw.account))
    fw.rpc.on_download = go
    fetch(fw)
    (t, _, waited), = races
    finish(t)
    assert waited, "the IncomingMsg ran inside the fetch: no claim"
    assert len(fw.mailed) == 1 and whole(fw)
    assert bot.HELD == {} and registry(fw) == {} and fw.rpc.downloads == [100]


def test_an_over_limit_premessage_fetched_and_timed_out_says_timeout_not_size(fw):
    """Point B mailed it and recorded `size`; the agent fetched it, so its hold
    and its timeout are PHONE-284's, as for any held message: the line says
    not on disk, with no limit reason, and the entry stays for a second try."""
    fw.write(download_limit="100000")
    fw.read()
    hold.held_then_live(fw)                 # 345.9 KiB, over: mailed now, not held
    assert registry(fw)[100]["reason"] == "size" and bot.HELD == {}
    fw.mailed.clear()
    assert fetch(fw) == STARTED and 100 in bot.HELD
    fw.account.arrive(100)                  # a repeated IncomingMsg while it is fetched
    run_live(fw.account)
    assert fw.mailed == []                  # remembered at point B: skipped
    FakeTimer.made[-1].fire()
    assert body(fw) == ("[partial, not downloaded yet: IMG_0042.jpg (image/jpeg, 345.9 KiB) - msg 100. "
                        f"{NOT_ON_DISK} Fetch it: dc-fetch 100.]")
    e = registry(fw)[100]
    assert e["reason"] == "timeout" and "fetching" not in e


def test_a_fetched_over_limit_premessage_is_not_turned_away_by_point_b(fw):
    """A dc-fetch's pending message is the agent's ask: pending_refusal()
    passes it, so a hold() guard never mails it as over the limit."""
    fw.write(download_limit="100000")
    fw.read()
    hold.held_then_live(fw)
    fw.mailed.clear()
    fetch(fw)
    snap = fw.account.get_message_by_id(100).get_snapshot()
    assert bot.pending_refusal(snap) is None and not bot.mail_now(snap)
    fw.account.land_bytes(100)
    fw.account.changed(100, 3)
    run_live(fw.account)
    assert len(fw.mailed) == 1 and whole(fw) and registry(fw) == {}


# -- a fetch's download announced later as IncomingMsg, the only event (bot=1) -------
# Measured on core 2.62.0 (do-006, design §8): on a bot=1 account a fetched
# download's completion is IncomingMsg (Done) and NO MsgsChanged follows. The id
# is in SEEN (it was mailed as a partial before the fetch), and it is HELD (the
# fetch's hold). The SEEN skip must not swallow it: the held message is released
# now, not when its 300 s bound runs out.

def test_a_fetch_completed_later_as_incoming_msg_alone_is_mailed_whole_now(fw):
    timed_out(fw)
    assert fetch(fw) == STARTED and 100 in bot.HELD
    fw.account.land_bytes(100)
    fw.account.arrive(100)                  # core's only word of the landed file
    run_live(fw.account)
    assert len(fw.mailed) == 1 and whole(fw)
    assert bot.HELD == {} and registry(fw) == {}
    assert all(t.cancelled for t in FakeTimer.made)


def test_a_point_b_fetch_completed_later_as_incoming_msg_alone_is_mailed_whole_now(fw):
    fw.write(download_limit="100000")
    fw.read()
    hold.held_then_live(fw)                 # over: mailed now (point B), remembered
    fw.mailed.clear()
    assert fetch(fw) == STARTED and 100 in bot.HELD
    fw.account.land_bytes(100)
    fw.account.arrive(100)
    run_live(fw.account)
    assert len(fw.mailed) == 1 and whole(fw)
    assert bot.HELD == {} and registry(fw) == {}

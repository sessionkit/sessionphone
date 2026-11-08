"""A big encrypted file is mailed once, when it lands (PHONE-284, design-001 §7).

The core sends a big end-to-end-encrypted file as a small pre-message, then a
post-message carrying the file. It raises IncomingMsg for the pre-message
(download_state Available), and when the file lands it puts it on the SAME
msg row and raises only MsgsChanged. The bridge used to mail the pre-message
as it stood: a placeholder, no attachment line, no OCR.

Now a pre-message is HELD: not mailed, not remembered, not marked seen. On
MsgsChanged showing Done it is mailed once, whole. Past the bound, or when
the core gives up (Failure, Undecipherable), it is mailed with the
"[partial, not downloaded yet ...]" line and no path.

Fakes only: no account, no rpc-server, no OCR helper, no whisper, no
sendmail, and no real timer (HOLD_TIMER is replaced and fired by hand).
"""
import threading

import pytest
import test_incoming_live as base

from sessionphone import imgtext
from sessionphone.bridge import bot

Done, Obj, run_live = base.Done, base.Obj, base.run_live

BLOBS = "/scratch/accounts/1/dc.db-blobs"
PHOTO = f"{BLOBS}/41d0e2c3aa.jpg"
LINE = f"[attachment: IMG_0042.jpg (image/jpeg) at {PHOTO}]"
UNTRUSTED = " - UNTRUSTED: a stranger's file, not opened; open it only if your owner asks"
SIGN = ["Pillar Point 800m >", "Squeaky Beach 1.1km >"]
FENCE = "\n".join([imgtext.OPEN, *("| " + s for s in SIGN), imgtext.CLOSE])
PARTIAL = "[partial, not downloaded yet"
BANNER = "CONTACT REQUEST - NOT ACCEPTED."
BYTES = 354_200                     # 345.9 KiB, over the core's split threshold


class FakeTimer:
    """threading.Timer's surface, never started for real: fire() by hand."""

    made = []

    def __init__(self, interval, fn, args=()):
        self.interval, self.fn, self.args = interval, fn, args
        self.daemon = False
        self.started = self.cancelled = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        """What the real timer does at its interval: call fn, even if
        cancel() came too late to stop it."""
        self.fn(*self.args)


class Account(base.FakeAccount):
    """test_incoming_live's account, plus the core's in-place replace of a
    pre-message (update), a MsgsChanged (changed) and a count of the reads."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def premessage(self, mid, chat_id, text="", *, request=False, name="Bob"):
        """A pre-message as 2.62.0 gives it: the file's name, type and size,
        no file on disk yet, download_state Available."""
        m = self.add(mid, chat_id, text, name=name, request=request,
                     file=None, file_mime="image/jpeg", view_type="Image")
        m.snap.file_name, m.snap.file_bytes, m.snap.download_state = "IMG_0042.jpg", BYTES, "Available"
        return m

    def land(self, mid):
        """The post-message landed: the file goes onto the same row."""
        self.update(mid, file=PHOTO, download_state="Done")

    def update(self, mid, **fields):
        self.msgs[mid].snap.__dict__.update(fields)

    def changed(self, msg_id, chat_id):
        self.events.append(Obj(kind=bot.EventType.MSGS_CHANGED, msg_id=msg_id, chat_id=chat_id))

    def get_message_by_id(self, mid):
        self.reads += 1
        return super().get_message_by_id(mid)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Module state in a scratch dir: SEEN, HELD and the timer factory fresh;
    mail_in, log, imgtext.read and transcribe recorded."""
    monkeypatch.setattr(bot, "SEEN", tmp_path / "bridged-ids.txt")
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", False)
    monkeypatch.setattr(bot, "IMAGE_TEXT", True)
    monkeypatch.setattr(bot, "HELD", {})
    monkeypatch.setattr(bot, "HOLD_TIMER", FakeTimer)
    FakeTimer.made = []
    w = Obj(mailed=[], logged=[], ocr=[], heard=[], account=Account())
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: w.mailed.append((a, k)))
    monkeypatch.setattr(bot, "log", w.logged.append)

    def read(path, log):
        w.ocr.append(path)
        return list(SIGN)
    monkeypatch.setattr(imgtext, "read", read)

    def transcribe(path):
        w.heard.append(path)
        return "please call me back about the parcel"
    monkeypatch.setattr(bot, "transcribe", transcribe)
    return w


def seen():
    return set(bot.SEEN.read_text().split()) if bot.SEEN.exists() else set()


def body(w):
    (args, _), = w.mailed
    return args[3]


def held_then_live(w, mid=100, chat_id=3, **kw):
    """A pre-message arrives live and is held."""
    w.account.premessage(mid, chat_id, **kw)
    w.account.arrive(mid)
    run_live(w.account)


# -- 1. the pre-message is held ---------------------------------------------------

def test_a_premessage_is_held_not_mailed(world):
    held_then_live(world)
    assert world.mailed == []
    assert "100" not in seen()
    assert world.account.marked == []
    assert 100 in bot.HELD
    timer, = FakeTimer.made
    assert timer.started and timer.daemon
    assert timer.interval == bot.PREMESSAGE_WAIT_S == 300.0
    assert any(line.startswith("HELD msg 100 chat 3: pre-message") for line in world.logged)


# -- 2. MsgsChanged Done: mailed once, whole -------------------------------------

def test_the_full_file_landing_mails_it_once_with_its_line_and_ocr(world):
    held_then_live(world)
    world.account.land(100)
    world.account.changed(100, 3)
    world.account.changed(100, 3)           # core may say it twice
    run_live(world.account)
    assert body(world) == LINE + "\n\n" + FENCE
    assert world.ocr == [PHOTO]
    (_, kw), = world.mailed
    assert kw["kind"] == "message" and kw["dc_msg_id"] == 100
    assert "100" in seen()
    assert world.account.marked == [100]
    assert bot.HELD == {}
    assert FakeTimer.made[0].cancelled
    assert any(line.startswith("RELEASED msg 100 (landed") for line in world.logged)


# -- 3. MsgsChanged with msg id 0 --------------------------------------------------

@pytest.mark.parametrize("msg_id,chat_id", [(0, 3), (0, 0)], ids=["chat-wide", "global"])
def test_a_msgs_changed_without_a_msg_id_releases_it_too(world, msg_id, chat_id):
    held_then_live(world)
    world.account.land(100)
    world.account.changed(msg_id, chat_id)
    run_live(world.account)
    assert body(world) == LINE + "\n\n" + FENCE
    assert bot.HELD == {}


def test_a_chat_wide_msgs_changed_for_another_chat_leaves_it_held(world):
    held_then_live(world)
    world.account.land(100)
    world.account.changed(0, 99)
    run_live(world.account)
    assert world.mailed == [] and 100 in bot.HELD


# -- 4. nothing held: MsgsChanged costs nothing ----------------------------------

def test_msgs_changed_with_nothing_held_reads_nothing(world):
    world.account.changed(55, 3)
    world.account.changed(0, 0)
    run_live(world.account)
    assert world.account.reads == 0 and world.mailed == []


def test_msgs_changed_for_another_id_reads_nothing(world):
    held_then_live(world)
    reads = world.account.reads
    world.account.changed(101, 3)
    run_live(world.account)
    assert world.account.reads == reads
    assert world.mailed == [] and 100 in bot.HELD


# -- 5. still pending: still held -------------------------------------------------

@pytest.mark.parametrize("state", ["Available", "InProgress"])
def test_msgs_changed_while_still_pending_keeps_it_held(world, state):
    held_then_live(world)
    world.account.update(100, download_state=state)
    world.account.changed(100, 3)
    run_live(world.account)
    assert world.mailed == [] and 100 in bot.HELD
    assert not FakeTimer.made[0].cancelled


# -- 6. the bound: mailed as partial -------------------------------------------

def test_the_timer_mails_a_partial_with_no_path_and_no_ocr(world):
    held_then_live(world)
    FakeTimer.made[0].fire()
    said = body(world)
    assert said == ("[partial, not downloaded yet: IMG_0042.jpg (image/jpeg, 345.9 KiB) - msg 100. "
                    "The file is not on disk yet; do not look for it.]")
    assert "[attachment:" not in said and BLOBS not in said
    assert world.ocr == []
    assert "100" in seen() and world.account.marked == [100]
    assert bot.HELD == {}
    assert any("not downloaded after 300 s; mailed as partial" in line for line in world.logged)
    # the file lands late: nothing more is mailed
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    assert len(world.mailed) == 1


# -- 7. the timer after a release ------------------------------------------------

def test_the_timer_firing_after_a_release_is_a_no_op(world):
    held_then_live(world)
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    FakeTimer.made[0].fire()                # cancel() was too late
    assert len(world.mailed) == 1


# -- 8. the timer, file landed but no MsgsChanged came --------------------------

def test_the_timer_mails_the_whole_message_if_the_file_landed_unannounced(world):
    held_then_live(world)
    world.account.land(100)
    FakeTimer.made[0].fire()
    assert body(world) == LINE + "\n\n" + FENCE
    assert world.ocr == [PHOTO]


# -- 9. the core gives up --------------------------------------------------------

@pytest.mark.parametrize("state,why", [("Failure", "the download failed"),
                                       ("Undecipherable", "it could not be decrypted")])
def test_a_download_the_core_gave_up_on_is_mailed_now_as_partial(world, state, why):
    held_then_live(world)
    world.account.update(100, download_state=state)
    world.account.changed(100, 3)
    run_live(world.account)
    said = body(world)
    assert said.startswith(f"[partial, not downloaded yet ({why}): IMG_0042.jpg"), said
    assert "msg 100" in said and "[attachment:" not in said
    assert FakeTimer.made[0].cancelled and bot.HELD == {}


# -- 10. the race: the file landed before IncomingMsg was handled ---------------

def test_a_premessage_already_done_is_mailed_at_once_and_never_held(world):
    world.account.premessage(100, 3)
    world.account.land(100)
    world.account.arrive(100)
    world.account.changed(100, 3)
    run_live(world.account)
    assert body(world) == LINE + "\n\n" + FENCE
    assert FakeTimer.made == [] and bot.HELD == {}


# -- 11, 12. a restart -----------------------------------------------------------

def test_the_sweep_holds_a_fresh_premessage(world):
    m = world.account.premessage(100, 3)
    world.account.fresh.append(m)
    bot.sweep(world.account)
    assert world.mailed == []
    assert "100" not in seen() and world.account.marked == []
    assert 100 in bot.HELD and FakeTimer.made[0].started
    # its MsgsChanged, read once the event loop starts
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    assert body(world) == LINE + "\n\n" + FENCE


def test_the_sweep_mails_a_file_that_landed_while_the_bot_was_down(world):
    m = world.account.premessage(100, 3)
    world.account.land(100)
    world.account.fresh.append(m)
    bot.sweep(world.account)
    assert body(world) == LINE + "\n\n" + FENCE
    assert FakeTimer.made == [] and "100" in seen()
    bot.sweep(world.account)                # still fresh to the core: SEEN stops it
    assert len(world.mailed) == 1


def test_a_restart_while_held_mails_it_once(world):
    """Held live, the bot stops (HELD is memory), the sweep finds it fresh."""
    held_then_live(world)
    bot.HELD.clear()                        # the restart
    bot.sweep(world.account)
    assert world.mailed == [] and 100 in bot.HELD
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    assert len(world.mailed) == 1


def test_a_premessage_the_sweep_held_and_its_queued_incoming_msg_mail_it_once(world):
    """start_io() runs before the sweep, so a message fetched at start is
    fresh AND queued as IncomingMsg. The sweep holds it; by the time the
    event loop reads the IncomingMsg its file has landed. One mail, not
    one from the IncomingMsg and another from the MsgsChanged."""
    world.account.premessage(100, 3)
    world.account.arrive(100)               # queued IncomingMsg, and fresh
    bot.sweep(world.account)
    assert world.mailed == [] and 100 in bot.HELD
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    assert body(world) == LINE + "\n\n" + FENCE
    assert bot.HELD == {} and FakeTimer.made[0].cancelled
    assert world.account.marked == [100]
    FakeTimer.made[0].fire()                # a late timer: nothing more
    assert len(world.mailed) == 1


def test_a_premessage_the_sweep_held_stays_held_on_its_queued_incoming_msg(world):
    """The same, with the file still to come: the IncomingMsg starts no
    second hold and no second timer."""
    world.account.premessage(100, 3)
    world.account.arrive(100)
    bot.sweep(world.account)
    run_live(world.account)
    assert world.mailed == [] and 100 in bot.HELD and len(FakeTimer.made) == 1


def test_the_sweep_skips_a_partial_already_mailed(world):
    held_then_live(world)
    FakeTimer.made[0].fire()
    world.account.fresh.append(world.account.msgs[100])    # the mark-seen was lost
    bot.sweep(world.account)
    assert len(world.mailed) == 1 and bot.HELD == {}


def test_a_sweep_failure_still_remembers_the_message(world, monkeypatch):
    """A message that breaks the sweep is not retried on every restart, as
    before PHONE-284: it is remembered."""
    m = world.account.add(7, chat_id=3, text="hello")
    world.account.fresh.append(m)

    def boom(*a, **k):
        raise RuntimeError("rpc gone")
    monkeypatch.setattr(bot, "chat_channel", boom)
    bot.sweep(world.account)
    assert "7" in seen()
    assert any("startup sweep failed" in line for line in world.logged)


# -- 13. a stranger --------------------------------------------------------------

def test_a_strangers_premessage_is_held_then_mailed_as_a_request(world):
    held_then_live(world, request=True, name="Stranger")
    assert world.mailed == [] and bot.HELD[100]["route"] == "request"
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    (args, kw), = world.mailed
    assert kw["kind"] == "request"
    said = args[3]
    assert said.startswith(BANNER)
    assert LINE[:-1] + UNTRUSTED + "]" in said
    assert imgtext.NOT_READ_REQUEST in said
    assert world.ocr == []
    assert not world.account.chats[3].accepted


def test_a_strangers_partial_is_marked_and_has_no_path(world):
    held_then_live(world, request=True, name="Stranger")
    FakeTimer.made[0].fire()
    (args, kw), = world.mailed
    assert kw["kind"] == "request"
    said = args[3]
    assert said.startswith(BANNER)
    assert PARTIAL in said and "msg 100 - a stranger's file." in said
    assert "[attachment:" not in said and BLOBS not in said
    assert world.ocr == []


def test_accepting_the_chat_while_held_keeps_it_a_strangers_file(world):
    held_then_live(world, request=True, name="Stranger")
    world.account.chats[3].accept()         # the owner accepts on the phone
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    (args, kw), = world.mailed
    assert kw["kind"] == "request" and UNTRUSTED in args[3]
    assert world.ocr == []


def test_an_auto_accepted_requests_premessage_is_held_and_not_read(world, monkeypatch):
    """ACCEPT_REQUESTS: accepted at once, as today; its file, a stranger's,
    is still not read (ocr = not request, as for a whole message)."""
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", True)
    held_then_live(world, request=True, name="Stranger")
    assert world.account.chats[3].accepted and 100 in bot.HELD
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    (args, kw), = world.mailed
    assert kw["kind"] == "message" and UNTRUSTED in args[3] and world.ocr == []


# -- 14. the inbound gate --------------------------------------------------------

def test_a_rejected_premessage_is_never_held(world, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "inbound_ok", lambda *a, **k: calls.append(a) or False)
    held_then_live(world)
    assert bot.HELD == {} and FakeTimer.made == [] and world.mailed == []
    assert "100" in seen() and world.account.marked == [100]
    assert len(calls) == 1


def test_the_gate_runs_once_not_again_at_release(world, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "inbound_ok", lambda *a, **k: calls.append(a) or True)
    held_then_live(world)
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    assert len(world.mailed) == 1 and len(calls) == 1


def test_the_sweep_never_holds_a_rejected_premessage(world, monkeypatch):
    monkeypatch.setattr(bot, "inbound_ok", lambda *a, **k: False)
    m = world.account.premessage(100, 3)
    world.account.fresh.append(m)
    bot.sweep(world.account)
    assert bot.HELD == {} and world.mailed == [] and "100" in seen()


# -- 15. text typed beside the file ---------------------------------------------

def test_typed_text_beside_the_file_is_mailed_once_above_the_line(world):
    held_then_live(world, text="the sign at the car park")
    world.account.land(100)
    world.account.changed(100, 3)
    run_live(world.account)
    said = body(world)
    assert said == "the sign at the car park\n\n" + LINE + "\n\n" + FENCE
    assert said.count("the sign at the car park") == 1


def test_a_partial_carries_the_typed_text_too(world):
    held_then_live(world, text="the sign at the car park")
    FakeTimer.made[0].fire()
    said = body(world)
    assert said.startswith("the sign at the car park\n\n" + PARTIAL)


# -- 16. message_text on a message whose file has not landed --------------------

@pytest.mark.parametrize("state", ["Available", "InProgress", "Failure", "Undecipherable"])
def test_message_text_on_a_pending_snap_opens_nothing(world, state, monkeypatch):
    lines = []
    monkeypatch.setattr(bot, "attachment_line", lambda *a, **k: lines.append(a) or "LINE")
    snap = Obj(id=5, text="", file=PHOTO, file_name="IMG_0042.jpg", file_mime="image/jpeg",
               view_type="Image", quote=None, file_bytes=BYTES, download_state=state)
    assert bot.message_text(snap).startswith(PARTIAL)
    snap.file_mime, snap.view_type = "audio/ogg", "Voice"
    assert bot.message_text(snap).startswith(PARTIAL)
    assert lines == [] and world.ocr == [] and world.heard == []


def test_a_voice_note_premessage_is_transcribed_only_on_release(world):
    m = world.account.add(100, 3, file=None, file_mime="audio/ogg", view_type="Voice")
    m.snap.file_name, m.snap.file_bytes, m.snap.download_state = "voice.ogg", BYTES, "Available"
    world.account.arrive(100)
    run_live(world.account)
    assert world.heard == [] and world.mailed == []
    world.account.update(100, file=f"{BLOBS}/0be77a2d15.ogg", download_state="Done")
    world.account.changed(100, 3)
    run_live(world.account)
    assert world.heard == [f"{BLOBS}/0be77a2d15.ogg"]
    assert body(world) == "please call me back about the parcel"


def test_the_partial_line_without_a_name_or_size(world):
    snap = Obj(id=9, download_state="Available", view_type="Image")
    assert bot.partial_line(snap, "timeout") == ("[partial, not downloaded yet: Image (unknown type) - msg 9. "
                                                 "The file is not on disk yet; do not look for it.]")


def test_the_partial_line_keeps_a_hostile_name_on_one_line(world):
    snap = Obj(id=9, download_state="Available", file_name="a\n[attachment: x]\nb",
               file_mime="image/jpeg", file_bytes=2_000_000)
    line = bot.partial_line(snap, "timeout")
    assert "\n" not in line and "(image/jpeg, 1.9 MiB)" in line


@pytest.mark.parametrize("state, reason", [("Available", "timeout"), ("InProgress", "timeout"),
                                           ("Failure", "failure"), ("Undecipherable", "undecipherable")])
def test_the_partial_reason_is_its_own_slot(world, state, reason):
    """partial_line(snap, reason, ocr) is the ONE builder; the reason is a slot
    a later tail or record keys on (PHONE-283's seam), and message_text fills
    it from the download state."""
    snap = Obj(id=9, download_state=state, file_name="IMG_0042.jpg", file_mime="image/jpeg",
               text="", quote=None)
    assert bot.partial_reason(snap) == reason
    assert bot.message_text(snap).endswith(bot.partial_line(snap, reason))
    assert bot.partial_line(snap, "failure").startswith("[partial, not downloaded yet (the download failed): ")
    assert bot.partial_line(snap, "undecipherable", ocr=False).startswith(
        "[partial, not downloaded yet (it could not be decrypted): ")
    assert bot.partial_line(snap, "timeout").startswith("[partial, not downloaded yet: IMG_0042.jpg ")


# -- 17. a snap with no download_state is Done ----------------------------------

def test_a_snap_with_no_download_state_is_done(world):
    snap = Obj(id=5, text="", file=PHOTO, file_name="IMG_0042.jpg", file_mime="image/jpeg",
               view_type="Image", quote=None)
    assert bot.download_state(snap) == "Done" and not bot.is_pending(snap)
    assert bot.message_text(snap) == LINE + "\n\n" + FENCE


def test_a_download_state_enum_compares_by_its_value(world):
    from deltachat_rpc_client.const import DownloadState
    assert bot.is_pending(Obj(download_state=DownloadState.AVAILABLE))
    assert bot.download_state(Obj(download_state=DownloadState.DONE)) == "Done"


# -- 18. remember() from many threads -------------------------------------------

def test_remember_from_many_threads_loses_no_id(world):
    go = threading.Barrier(16)

    def one(i):
        go.wait()
        for k in range(10):
            bot.remember(i * 100 + k)
    threads = [threading.Thread(target=one, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen() == {str(i * 100 + k) for i in range(16) for k in range(10)}


# -- 19. a failure on the timer's thread ----------------------------------------

def test_a_failing_release_on_the_timer_is_logged_not_raised(world, monkeypatch):
    held_then_live(world)

    def boom(*a, **k):
        raise RuntimeError("rpc gone")
    monkeypatch.setattr(world.account, "get_message_by_id", boom)
    FakeTimer.made[0].fire()                # must not raise
    assert any("HELD msg 100: release on timeout failed" in line for line in world.logged)
    assert world.mailed == [] and "100" not in seen()
    # never remembered, never marked seen: the next restart's sweep has it
    assert world.account.msgs[100] in world.account.fresh

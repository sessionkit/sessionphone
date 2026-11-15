"""Point C: the download policy on a landed file (PHONE-283 do-005, design §2 C, §6, §7, §9 seam 1).

Inside message_text, on a message whose file is there, the bridge judges its
kind and size against the policy read ONCE at start (bot.read_policy, from the
host layer alone). A file the policy turns away is not saved, not named by
path, not OCR'd and not transcribed: it gets PHONE-284's ONE partial line,
with our reason in its reason slot and "It was not saved or passed on; do not
look for it." (never "not on disk yet": the file is on disk). A file it passes
on is copied under save_dir (bridge/attach.py) and the line names the copy.

The partial builder records every partial it tails in the registry
(<home>/not-downloaded.json): our size and type, and PHONE-284's own timeout
and Failure, a stranger's included (ocr false). Undecipherable is neither
recorded nor tailed. The tail is `Fetch it: dc-fetch N`, or, for a stranger's
file, ask your owner.

Fakes only: PHONE-284's FakeAccount (test_premessage_hold), a phone.json in
tmp, blobs in tmp. No account, relay, OCR helper, whisper or sendmail.
"""
import json
import os
import pathlib
import stat

import pytest
import test_premessage_hold as hold

from sessionphone import imgtext, settings
from sessionphone.bridge import attach, bot

Obj, FakeTimer, run_live, body = hold.Obj, hold.FakeTimer, hold.run_live, hold.body
UNTRUSTED, PARTIAL, BANNER = hold.UNTRUSTED, hold.PARTIAL, hold.BANNER
AGENT = "ada"
NOT_PASSED_ON = "It was not saved or passed on; do not look for it."
NOT_ON_DISK = "The file is not on disk yet; do not look for it."
KB360, MB12, MB20 = 360_000, 12_000_000, 20_000_000

world = hold.world      # PHONE-284's world: mail_in, log, imgtext.read and transcribe recorded


class Account(hold.Account):
    """PHONE-284's FakeAccount, plus a message whose file has LANDED: a real
    blob in tmp, with the name, type and size core gives it."""

    blobs = None

    def landed(self, mid, chat_id, *, name="IMG_0042.jpg", mime="image/jpeg", view="Image",
               size=KB360, data=None, request=False, sender="Bob", text=""):
        blob = self.blobs / f"{mid:04d}{os.path.splitext(name)[1] or '.bin'}"
        blob.write_bytes(data if data is not None else os.urandom(64))
        m = self.add(mid, chat_id, text, name=sender, request=request,
                     file=str(blob), file_mime=mime, view_type=view)
        m.snap.file_name, m.snap.file_bytes, m.snap.download_state = name, size, "Done"
        return m


@pytest.fixture
def phone_json(monkeypatch, tmp_path):
    """phone.json in tmp; -> a writer for its bots.ada map (the host layer)."""
    f = tmp_path / "settings" / "phone.json"
    monkeypatch.setattr(settings, "FILE", str(f))
    for k in settings.HOST_ONLY + ("DC_DOWNLOAD_LIMIT", "DC_DOWNLOAD_TYPES", "DC_SAVE_DIR"):
        monkeypatch.delenv(k, raising=False)

    def write(**keys):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"bots": {AGENT: keys}}))
        return f
    write()
    return write


@pytest.fixture
def pw(world, phone_json, monkeypatch, tmp_path):
    """PHONE-284's world on our Account, a home in tmp, and a reader: read()
    reads the policy as main() does at start (bot.read_policy over phone.json),
    with this home. -> the world, with .home, .saves (the default save_dir),
    .write (phone.json) and .read."""
    acct = Account()
    acct.blobs = tmp_path / "accounts" / "1" / "dc.db-blobs"
    acct.blobs.mkdir(parents=True)
    world.account = acct
    world.home = tmp_path / "home"
    world.home.mkdir()
    world.saves = world.home / "attachments"
    world.write = phone_json
    world.read = lambda: bot.read_policy(AGENT, world.home)
    monkeypatch.setattr(bot, "PROFILE", None)
    world.read()
    return world


def live(w, mid=100, chat_id=3, **kw):
    """A landed message arrives live (IncomingMsg, Done): mailed at once."""
    m = w.account.landed(mid, chat_id, **kw)
    w.account.arrive(mid)
    run_live(w.account)
    return m


def registry(w):
    return attach.entries(w.home)


def files_under(d):
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) if d.exists() else []


# -- a kind that is on, under the limit: saved, and the line names the copy ---------

def test_a_passed_image_is_saved_and_its_line_names_the_saved_copy(pw):
    m = live(pw)
    copy = pw.saves / "chat-3" / "100-IMG_0042.jpg"
    said = body(pw)
    assert said.startswith(f"[attachment: IMG_0042.jpg (image/jpeg) at {copy}]")
    assert m.snap.file not in said
    assert copy.read_bytes() == pathlib.Path(m.snap.file).read_bytes()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert pw.ocr == [str(copy)]            # OCR reads the saved copy
    assert registry(pw) == {}               # a file passed on is not recorded


def test_a_passed_voice_note_is_transcribed_from_the_saved_copy(pw):
    live(pw, name="voice.ogg", mime="audio/ogg", view="Voice", size=40_000)
    copy = pw.saves / "chat-3" / "100-voice.ogg"
    assert pw.heard == [str(copy)] and copy.exists()
    assert body(pw) == "please call me back about the parcel"


@pytest.mark.parametrize("size", [KB360, MB12])
def test_the_default_limit_passes_a_photo_and_a_whole_phone_photo(pw, size):
    live(pw, size=size)
    assert body(pw).startswith("[attachment: IMG_0042.jpg (image/jpeg) at ")
    assert files_under(pw.saves) == ["chat-3/100-IMG_0042.jpg"]


def test_building_the_same_messages_text_twice_gives_one_file(pw):
    m = pw.account.landed(100, 3)
    first, second = bot.message_text(m.snap), bot.message_text(m.snap)
    assert first == second
    assert files_under(pw.saves) == ["chat-3/100-IMG_0042.jpg"]


def test_a_save_that_fails_names_the_blob_as_before(pw):
    pw.saves.parent.mkdir(exist_ok=True)
    pw.saves.write_text("not a folder")     # save_dir cannot be made
    m = live(pw)
    assert body(pw).startswith(f"[attachment: IMG_0042.jpg (image/jpeg) at {m.snap.file}]")
    assert any(line.startswith("SAVE FAILED msg 100") for line in pw.logged)


def test_a_non_default_save_dir_moves_the_copy(pw, tmp_path):
    pw.write(save_dir=str(tmp_path / "elsewhere"))
    pw.read()
    live(pw)
    assert (tmp_path / "elsewhere" / "chat-3" / "100-IMG_0042.jpg").exists()
    assert not pw.saves.exists()


# -- turned away at C: the ONE partial line, our reason, never "not on disk" -------

def test_an_over_limit_unsplit_file_gets_the_partial_line_and_no_path(pw):
    m = live(pw, name="talk.pdf", mime="application/pdf", view="File", size=MB20)
    said = body(pw)
    assert said == ("[partial, not downloaded yet (over this bot's download limit, 15.3 MiB): talk.pdf "
                    "(application/pdf, 19.1 MiB) - msg 100. " + NOT_PASSED_ON + " Fetch it: dc-fetch 100.]")
    assert "[attachment:" not in said and m.snap.file not in said and str(pw.home) not in said
    assert NOT_ON_DISK not in said
    assert not pw.saves.exists()
    assert registry(pw) == {100: {"chat_id": 3, "ocr": True, "reason": "size", "kind": "file",
                                  "size": MB20, "at": registry(pw)[100]["at"]}}
    assert any(line.startswith("NOT PASSED ON msg 100: size") for line in pw.logged)


def test_a_type_off_video_gets_the_partial_line_and_is_recorded(pw):
    pw.write(download_types="image,sticker,voice,audio,file,vcard,webxdc")     # video off
    pw.read()
    live(pw, name="clip.mp4", mime="video/mp4", view="Video", size=2_000_000)
    said = body(pw)
    assert said == ("[partial, not downloaded yet (video is not passed on automatically here): clip.mp4 "
                    "(video/mp4, 1.9 MiB) - msg 100. " + NOT_PASSED_ON + " Fetch it: dc-fetch 100.]")
    assert not pw.saves.exists()
    e = registry(pw)[100]
    assert (e["reason"], e["kind"], e["ocr"]) == ("type", "video", True)


def test_a_turned_away_image_is_not_read_and_a_voice_note_not_transcribed(pw):
    pw.write(download_types="none")
    pw.read()
    live(pw, 100, 3)
    live(pw, 101, 3, name="voice.ogg", mime="audio/ogg", view="Voice")
    live(pw, 102, 3, name="memo.m4a", mime="audio/mp4", view="File")    # audio MIME, judged as a file
    assert pw.ocr == [] and pw.heard == []
    for (args, _), mid in zip(pw.mailed, (100, 101, 102)):
        assert args[3].startswith(PARTIAL) and f"msg {mid}. {NOT_PASSED_ON}" in args[3]
        assert "[attachment:" not in args[3] and imgtext.OPEN not in args[3]
    assert sorted(registry(pw)) == [100, 101, 102]
    assert registry(pw)[102]["kind"] == "file"
    assert not pw.saves.exists()


def test_typed_text_and_a_quote_stay_beside_a_turned_away_file(pw):
    live(pw, size=MB20, text="the plans, as promised")
    said = body(pw)
    assert said.startswith("the plans, as promised\n\n[partial, not downloaded yet (over this bot's")


def test_building_a_turned_away_messages_text_twice_gives_one_registry_entry(pw):
    m = pw.account.landed(100, 3, size=MB20)
    assert bot.message_text(m.snap) == bot.message_text(m.snap)
    assert list(registry(pw)) == [100]
    assert stat.S_IMODE(attach.registry_path(pw.home).stat().st_mode) == 0o600


def test_a_limit_of_exactly_the_files_size_passes_it(pw):
    pw.write(download_limit=str(KB360))
    pw.read()
    live(pw, size=KB360)
    assert body(pw).startswith("[attachment:")


def test_the_size_rule_reads_the_file_when_the_snapshot_has_no_size(pw):
    pw.write(download_limit="1000")
    pw.read()
    m = pw.account.landed(100, 3, data=b"x" * 2000)
    del m.snap.file_bytes
    said = bot.message_text(m.snap)
    assert said.startswith("[partial, not downloaded yet (over this bot's download limit, 1000 B): ")


def test_a_held_message_that_lands_over_the_limit_is_turned_away_at_release(pw):
    """The release mails it through message_text, so point C judges it too:
    the file is on disk now, and the line must not say it is not. Its
    pre-message gave no size, so point B could not turn it away: it was held."""
    pw.write(download_limit="100000")
    pw.read()
    pw.account.premessage(100, 3)
    pw.account.update(100, file_bytes=None)
    pw.account.arrive(100)
    run_live(pw.account)
    assert 100 in bot.HELD and pw.mailed == []
    pw.account.land(100)
    pw.account.update(100, file_bytes=hold.BYTES)
    pw.account.changed(100, 3)
    run_live(pw.account)
    said = body(pw)
    assert said == ("[partial, not downloaded yet (over this bot's download limit, 97.7 KiB): IMG_0042.jpg "
                    "(image/jpeg, 345.9 KiB) - msg 100. " + NOT_PASSED_ON + " Fetch it: dc-fetch 100.]")
    assert pw.ocr == [] and bot.HELD == {} and registry(pw)[100]["reason"] == "size"


# -- the setting changes the outcome: the policy read at start, never the env -----

def test_changing_download_limit_in_the_host_layer_changes_the_outcome(pw):
    m = pw.account.landed(100, 3, size=MB12)
    assert bot.message_text(m.snap).startswith("[attachment:")
    pw.write(download_limit="1000000")
    assert bot.message_text(m.snap).startswith("[attachment:")    # not re-read: the start's policy
    pw.read()                                                       # the next start
    said = bot.message_text(m.snap)
    assert said.startswith("[partial, not downloaded yet (over this bot's download limit, 976.6 KiB): ")
    pw.write(download_limit="0")                                    # no limit
    pw.read()
    assert bot.message_text(m.snap).startswith("[attachment:")


def test_changing_download_types_in_the_host_layer_changes_the_outcome(pw):
    m = pw.account.landed(100, 3)
    assert bot.message_text(m.snap).startswith("[attachment:")
    pw.write(download_types="voice,file")
    pw.read()
    assert "(image is not passed on automatically here)" in bot.message_text(m.snap)


@pytest.mark.parametrize("key", ["DC_DOWNLOAD_LIMIT", "download_limit"])
def test_a_limit_in_the_environment_changes_nothing(pw, monkeypatch, key):
    monkeypatch.setenv(key, "1000")
    pw.read()
    m = pw.account.landed(100, 3, size=MB12)
    assert bot.message_text(m.snap).startswith("[attachment:")


def test_with_no_policy_read_the_line_names_the_blob_and_nothing_is_saved(pw, monkeypatch):
    monkeypatch.setattr(bot, "POLICY", None)
    monkeypatch.setattr(bot, "POLICY_HOME", None)
    m = live(pw, size=MB20)
    assert body(pw).startswith(f"[attachment: IMG_0042.jpg (image/jpeg) at {m.snap.file}]")
    assert not pw.saves.exists() and registry(pw) == {}


# -- PHONE-284's partials: recorded and tailed, Undecipherable neither ------------

def test_a_hold_that_times_out_is_recorded_and_tailed(pw):
    hold.held_then_live(pw)
    FakeTimer.made[0].fire()
    assert body(pw) == ("[partial, not downloaded yet: IMG_0042.jpg (image/jpeg, 345.9 KiB) - msg 100. "
                        + NOT_ON_DISK + " Fetch it: dc-fetch 100.]")
    e = registry(pw)[100]
    assert (e["reason"], e["ocr"], e["chat_id"], e["size"]) == ("timeout", True, 3, hold.BYTES)


def test_a_failure_is_recorded_and_tailed(pw):
    hold.held_then_live(pw)
    pw.account.update(100, download_state="Failure")
    pw.account.changed(100, 3)
    run_live(pw.account)
    said = body(pw)
    assert said.startswith("[partial, not downloaded yet (the download failed): ")
    assert said.endswith(NOT_ON_DISK + " Fetch it: dc-fetch 100.]")
    assert registry(pw)[100]["reason"] == "failure"


def test_an_undecipherable_partial_is_neither_recorded_nor_tailed(pw):
    hold.held_then_live(pw)
    pw.account.update(100, download_state="Undecipherable")
    pw.account.changed(100, 3)
    run_live(pw.account)
    said = body(pw)
    assert said.endswith("msg 100. " + NOT_ON_DISK + "]")
    assert "dc-fetch" not in said and "ask your owner" not in said
    assert registry(pw) == {}


def test_a_strangers_timeout_is_recorded_with_ocr_false_and_says_ask_your_owner(pw):
    hold.held_then_live(pw, request=True, name="Stranger")
    FakeTimer.made[0].fire()
    (args, kw), = pw.mailed
    said = args[3]
    assert kw["kind"] == "request" and said.startswith(BANNER)
    assert said.endswith("msg 100 - a stranger's file. " + NOT_ON_DISK + " To have it, ask your owner.]")
    assert "dc-fetch" not in said
    e = registry(pw)[100]
    assert (e["reason"], e["ocr"]) == ("timeout", False)


def test_the_tail_names_a_second_profile(pw, monkeypatch):
    monkeypatch.setattr(bot, "PROFILE", "public")
    m = pw.account.landed(100, 3, size=MB20)
    assert bot.message_text(m.snap).endswith(" Fetch it: dc-fetch --profile public 100.]")


def test_a_record_that_fails_offers_no_verb(pw, monkeypatch):
    def refuse(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(attach, "record", refuse)
    m = pw.account.landed(100, 3, size=MB20)
    said = bot.message_text(m.snap)
    assert said.endswith("msg 100. " + NOT_PASSED_ON + "]") and "dc-fetch" not in said
    assert any("not recorded for dc-fetch" in line for line in pw.logged)


# -- a stranger: untrusted/, the suffix on every path, never read ------------------

def test_a_held_strangers_file_is_saved_under_untrusted_and_marked(pw):
    live(pw, request=True, sender="Stranger")
    (args, kw), = pw.mailed
    said = args[3]
    copy = pw.saves / "untrusted" / "chat-3" / "100-IMG_0042.jpg"
    assert kw["kind"] == "request" and said.startswith(BANNER)
    assert f"[attachment: IMG_0042.jpg (image/jpeg) at {copy}{UNTRUSTED}]" in said
    assert copy.exists() and not (pw.saves / "chat-3").exists()
    assert imgtext.NOT_READ_REQUEST in said
    assert pw.ocr == []
    assert not pw.account.chats[3].accepted


def test_an_accepted_first_strangers_file_is_saved_under_untrusted_and_marked(pw, monkeypatch):
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", True)
    live(pw, request=True, sender="Stranger")
    (args, kw), = pw.mailed
    copy = pw.saves / "untrusted" / "chat-3" / "100-IMG_0042.jpg"
    assert kw["kind"] == "message" and pw.account.chats[3].accepted
    assert f"at {copy}{UNTRUSTED}]" in args[3]
    assert pw.ocr == []


def test_a_strangers_failed_save_names_the_blob_with_the_suffix(pw):
    (pw.saves / "untrusted").mkdir(parents=True)
    os.symlink(pw.home, pw.saves / "untrusted" / "chat-3")     # refused: a symlink where the bridge keeps its folders
    m = live(pw, request=True, sender="Stranger")
    said = body(pw)
    assert f"[attachment: IMG_0042.jpg (image/jpeg) at {m.snap.file}{UNTRUSTED}]" in said
    assert pw.ocr == []
    assert any(line.startswith("SAVE FAILED msg 100") for line in pw.logged)


def test_a_turned_away_strangers_file_says_ask_your_owner_and_offers_no_verb(pw):
    live(pw, size=MB20, request=True, sender="Stranger")
    said = body(pw)
    assert said.startswith(BANNER)
    assert said.endswith("msg 100 - a stranger's file. " + NOT_PASSED_ON + " To have it, ask your owner.]")
    assert "dc-fetch" not in said and "[attachment:" not in said
    assert not pw.saves.exists() and pw.ocr == []
    e = registry(pw)[100]
    assert (e["reason"], e["ocr"]) == ("size", False)


def test_a_later_message_in_an_accepted_chat_is_trusted(pw):
    live(pw)
    assert (pw.saves / "chat-3" / "100-IMG_0042.jpg").exists()
    assert UNTRUSTED not in body(pw) and pw.ocr


# -- the line's words --------------------------------------------------------------

def test_one_partial_form_the_lead_and_the_msg_id_unchanged(pw):
    """Our reasons fill PHONE-284's slot: one lead, one `msg N`, one stranger
    marker, never a second form of the line."""
    m = pw.account.landed(100, 3, size=MB20)
    line = bot.message_text(m.snap, ocr=False)
    assert line.count("[partial, not downloaded yet") == 1 and line.count("msg 100") == 1
    assert line.count(" - a stranger's file") == 1


# -- point B: a pre-message over the limit is mailed now, never held ---------------
# Design §2 B: IncomingMsg (or the startup sweep) on a message whose file is still
# to come (is_pending), with fileBytes over the limit. Core leaves its file on the
# server, so a hold would only wait out its bound: it is mailed now, by
# message_text's partial branch (the ONE builder, which records it), and
# remembered and marked seen like any mailed message.

KB100 = "100000"        # under hold.BYTES (354,200 B, 345.9 KiB): the pre-message is over it


def swept(w, mid=100, chat_id=3, **kw):
    """A pre-message the startup sweep finds: fresh, never seen."""
    m = w.account.premessage(mid, chat_id, **kw)
    w.account.fresh.append(m)
    bot.sweep(w.account)
    return m


def over_limit(w, limit=KB100):
    w.write(download_limit=limit)
    w.read()


B_LINE = ("[partial, not downloaded yet (over this bot's download limit, 97.7 KiB): IMG_0042.jpg "
          "(image/jpeg, 345.9 KiB) - msg 100. " + NOT_ON_DISK + " Fetch it: dc-fetch 100.]")


def test_an_over_limit_premessage_is_mailed_now_recorded_remembered_and_seen(pw):
    over_limit(pw)
    hold.held_then_live(pw)
    said = body(pw)
    assert said == B_LINE
    assert "[attachment:" not in said and str(pw.home) not in said
    assert NOT_PASSED_ON not in said            # the file is on the server, not held back on disk
    assert bot.HELD == {} and FakeTimer.made == []
    assert registry(pw) == {100: {"chat_id": 3, "ocr": True, "reason": "size", "kind": None,
                                  "size": hold.BYTES, "at": registry(pw)[100]["at"]}}
    assert "100" in hold.seen() and pw.account.marked == [100]
    assert any(line.startswith("NOT HELD msg 100: over the download limit") for line in pw.logged)
    assert pw.ocr == [] and pw.heard == [] and not pw.saves.exists()


def test_an_under_limit_premessage_is_held_as_before(pw):
    hold.held_then_live(pw)                     # the default limit, 16 MB
    assert pw.mailed == [] and 100 in bot.HELD and registry(pw) == {}
    assert "100" not in hold.seen() and pw.account.marked == []


def test_a_premessage_of_exactly_the_limit_is_held(pw):
    over_limit(pw, str(hold.BYTES))
    hold.held_then_live(pw)
    assert pw.mailed == [] and 100 in bot.HELD


def test_a_premessage_with_no_size_is_held(pw):
    over_limit(pw)
    pw.account.premessage(100, 3)
    pw.account.update(100, file_bytes=None)
    pw.account.arrive(100)
    run_live(pw.account)
    assert pw.mailed == [] and 100 in bot.HELD


def test_with_no_limit_a_big_premessage_is_held(pw):
    over_limit(pw, "0")
    hold.held_then_live(pw)
    assert pw.mailed == [] and 100 in bot.HELD


def test_an_over_limit_premessage_is_mailed_once_on_a_repeated_incoming_msg(pw):
    over_limit(pw)
    hold.held_then_live(pw)
    pw.account.arrive(100)
    run_live(pw.account)
    assert len(pw.mailed) == 1 and list(registry(pw)) == [100]


def test_an_in_progress_premessage_over_the_limit_is_mailed_now(pw):
    over_limit(pw)
    pw.account.premessage(100, 3)
    pw.account.update(100, download_state="InProgress")
    pw.account.arrive(100)
    run_live(pw.account)
    assert body(pw) == B_LINE and bot.HELD == {}


def test_the_point_b_line_and_record_come_from_message_text_alone(pw, monkeypatch):
    """One builder: the guard only decides not to hold; the line and the
    registry record are message_text's partial branch's, built from fileBytes."""
    over_limit(pw)
    writes = []
    real = attach.record
    monkeypatch.setattr(attach, "record", lambda *a, **k: (writes.append((a, k)), real(*a, **k))[1])
    m = pw.account.premessage(100, 3)
    assert bot.message_text(m.snap) == B_LINE == bot.message_text(m.snap)
    assert list(registry(pw)) == [100] and len(writes) == 2     # built twice: one entry, replaced
    writes.clear()
    pw.account.arrive(100)
    run_live(pw.account)
    assert body(pw) == B_LINE and len(writes) == 1              # the live path: one build, one write


def test_a_failure_over_the_limit_keeps_its_failure_reason(pw):
    """Point B is a PENDING message's: core giving up is PHONE-284's partial."""
    over_limit(pw)
    m = pw.account.premessage(100, 3)
    pw.account.update(100, download_state="Failure")
    said = bot.message_text(m.snap)
    assert said.startswith("[partial, not downloaded yet (the download failed): ")
    assert registry(pw)[100]["reason"] == "failure"


def test_a_strangers_over_limit_premessage_is_mailed_now_and_offers_no_verb(pw):
    over_limit(pw)
    hold.held_then_live(pw, request=True, name="Stranger")
    (args, kw), = pw.mailed
    said = args[3]
    assert kw["kind"] == "request" and said.startswith(BANNER)
    assert said.endswith("msg 100 - a stranger's file. " + NOT_ON_DISK + " To have it, ask your owner.]")
    assert "dc-fetch" not in said and "[attachment:" not in said
    assert bot.HELD == {} and not pw.account.chats[3].accepted
    e = registry(pw)[100]
    assert (e["reason"], e["ocr"]) == ("size", False)
    assert "100" in hold.seen() and pw.account.marked == [100]


def test_the_sweep_mails_an_over_limit_premessage_now(pw):
    over_limit(pw)
    swept(pw)
    assert body(pw) == B_LINE
    assert bot.HELD == {} and FakeTimer.made == []
    assert registry(pw)[100]["reason"] == "size"
    assert "100" in hold.seen() and pw.account.marked == [100]
    pw.account.arrive(100)                      # its IncomingMsg, queued at start
    run_live(pw.account)
    assert len(pw.mailed) == 1


def test_the_sweep_holds_an_under_limit_premessage_as_before(pw):
    swept(pw)
    assert pw.mailed == [] and 100 in bot.HELD and registry(pw) == {}
    assert "100" not in hold.seen()


def test_the_sweep_mails_a_strangers_over_limit_premessage_as_a_request(pw):
    over_limit(pw)
    swept(pw, request=True, name="Stranger")
    (args, kw), = pw.mailed
    assert kw["kind"] == "request" and args[3].endswith(" To have it, ask your owner.]")
    assert bot.HELD == {} and registry(pw)[100]["ocr"] is False


# -- one unit: the limit and the file's size side by side --------------------------

MIB = 1024 * 1024


def test_a_15_5_mib_file_against_a_16_mb_limit_reads_as_over_it(pw):
    """16,000,000 B is 15.3 MiB; 15.5 MiB is 16,252,928 B, over it. Said in
    decimal MB beside a MiB size, the line read `limit, 16 MB` beside
    `15.5 MiB`: a file under its limit, as it seemed."""
    live(pw, name="clip.mov", mime="video/quicktime", view="Video", size=int(15.5 * MIB))
    said = body(pw)
    assert said.startswith("[partial, not downloaded yet (over this bot's download limit, 15.3 MiB): "
                           "clip.mov (video/quicktime, 15.5 MiB) - msg 100. ")


def test_the_same_at_point_b(pw):
    pw.account.premessage(100, 3)
    pw.account.update(100, file_bytes=int(15.5 * MIB))
    pw.account.arrive(100)
    run_live(pw.account)
    assert "(over this bot's download limit, 15.3 MiB): IMG_0042.jpg (image/jpeg, 15.5 MiB)" in body(pw)


def test_a_limit_under_1_mb_is_said_in_the_files_unit(pw):
    over_limit(pw, "500000")
    live(pw, size=600_000)
    assert "(over this bot's download limit, 488.3 KiB): IMG_0042.jpg (image/jpeg, 585.9 KiB)" in body(pw)


def as_bytes(said):
    n, unit = said.split()
    return float(n) * {"B": 1, "KiB": 1024, "MiB": MIB}[unit]


@pytest.mark.parametrize("limit", [1000, 100_000, 500_000, 1_048_576, 16_000_000, 50_000_000])
def test_a_file_under_the_limit_never_reads_as_over_it(limit):
    """One unit and one rounding: a file of LIMIT bytes or fewer never reads
    larger than its limit, and one larger never reads smaller."""
    shown = as_bytes(bot.size_text(limit))
    for size in (limit - 1, limit - limit // 100, limit // 2, limit + 1, limit + limit // 100):
        if size <= limit:
            assert as_bytes(bot.size_text(size)) <= shown, size
        else:
            assert as_bytes(bot.size_text(size)) >= shown, size

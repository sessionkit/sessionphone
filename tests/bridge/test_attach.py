"""bridge/attach: the kind of a file, the download decision, the save, the registry (PHONE-283).

design.md section 10, "Policy and save":

- The kind comes from the view type alone (Gif is an image; Unknown, or a view
  type a later core adds, is a file); file_mime is never consulted.
- decide(): 360 KB and 12 MB pass under the default limit, 20 MB gets `size`, a
  limit of 0 never refuses, and with `video` off a video gets `type` while an
  image is unaffected.
- The save: <save_dir>/chat-<id>/<msg id>-<name>, a stranger's under
  <save_dir>/untrusted/; folders 0700 opened O_NOFOLLOW (a symlink, or a folder
  this user does not own, refused); files 0600 via .part then rename; the bytes
  are the blob's; a hostile name is flattened and stays inside; idempotent per
  msg id; other bytes in the same name give -2.
- The registry: msg id -> {chat_id, ocr, reason, kind, size, at}, no route;
  reasons size/type/timeout/failure; a stranger's recorded with ocr false;
  Undecipherable never recorded.

Fakes only: scratch folders under tmp_path stand in for core's blobs and the
bot's home. No account, no relay.
"""
import json
import os
import pathlib
import stat

import pytest
from deltachat_rpc_client.const import ViewType

from sessionphone import settings
from sessionphone.bridge import attach, bot

KB, MB = 1000, 1000 * 1000


def policy(limit=settings.DEFAULT_LIMIT, types=frozenset(settings.KINDS), save_dir="/unused"):
    return settings.Policy(limit=limit, types=frozenset(types), save_dir=pathlib.Path(save_dir),
                           origin={k: settings.DEFAULT_SOURCE for k in settings.HOST_ONLY})


def mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


# ---- the kind ----------------------------------------------------------------

EXPECTED_KIND = {
    "Image": "image", "Gif": "image", "Sticker": "sticker", "Voice": "voice",
    "Audio": "audio", "Video": "video", "File": "file", "Webxdc": "webxdc",
    "Vcard": "vcard", "Unknown": "file", "Text": None,
}


@pytest.mark.parametrize("view", list(ViewType), ids=lambda v: v.value)
def test_every_client_view_type_has_its_kind(view):
    """Every view type the client names maps, as the enum and as the plain string."""
    assert view.value in EXPECTED_KIND, f"a view type this test does not know: {view.value}"
    assert attach.kind_of(view) == EXPECTED_KIND[view.value]
    assert attach.kind_of(view.value) == EXPECTED_KIND[view.value]
    assert attach.kind_of(view.value.lower()) == EXPECTED_KIND[view.value]


def test_gif_is_an_image():
    assert attach.kind_of("Gif") == "image"
    assert attach.kind_of(ViewType.GIF) == "image"


@pytest.mark.parametrize("view", ["Unknown", "Hologram", "", None])
def test_unknown_or_new_view_type_is_a_file(view):
    assert attach.kind_of(view) == "file"


@pytest.mark.parametrize("view", ["Text", "Call", "call"])
def test_no_file_no_kind(view):
    assert attach.kind_of(view) is None


def test_kinds_are_download_types_kinds():
    """Every kind kind_of answers is one download_types can name, and every kind it
    can name is reachable."""
    got = {attach.kind_of(v) for v in list(ViewType) + ["Hologram"]} - {None}
    assert got == set(settings.KINDS)


def test_file_mime_is_never_consulted():
    """kind_of takes the view type alone: a sender's MIME cannot make a file a voice
    note, nor a picture."""
    import inspect
    assert list(inspect.signature(attach.kind_of).parameters) == ["view_type"]
    assert attach.kind_of("File") == "file"     # whatever MIME (audio/ogg, image/png) it carries


# ---- decide ------------------------------------------------------------------

@pytest.mark.parametrize("size", [360 * KB, 12 * MB])
def test_photos_pass_under_the_default(size):
    assert attach.decide("image", size, policy()) is None
    assert attach.decide("file", size, policy()) is None


def test_twenty_mb_is_over_the_default():
    r = attach.decide("file", 20 * MB, policy())
    assert r == attach.Reason("size", kind="file", size=20 * MB, limit=settings.DEFAULT_LIMIT)


def test_the_limit_itself_passes():
    assert attach.decide("file", settings.DEFAULT_LIMIT, policy()) is None
    assert attach.decide("file", settings.DEFAULT_LIMIT + 1, policy()).why == "size"


def test_limit_zero_never_refuses():
    assert attach.decide("video", 10 ** 12, policy(limit=0)) is None


def test_changing_the_limit_changes_the_outcome():
    assert attach.decide("image", 360 * KB, policy(limit=100 * KB)).why == "size"
    assert attach.decide("file", 20 * MB, policy(limit=32 * MB)) is None


def test_video_off_refuses_video_not_image():
    p = policy(types=set(settings.KINDS) - {"video"})
    assert attach.decide("video", 1 * KB, p) == attach.Reason("type", kind="video", size=1 * KB)
    assert attach.decide("image", 1 * KB, p) is None


def test_none_refuses_every_kind():
    p = policy(types=frozenset())
    for k in settings.KINDS:
        assert attach.decide(k, 1, p).why == "type"


def test_pending_kind_unknown_gets_the_size_rule_alone():
    """A pending message (kind None, its view type is Text until the file lands):
    size only, even with every kind off."""
    p = policy(types=frozenset())
    assert attach.decide(None, 1 * KB, p) is None
    assert attach.decide(None, 20 * MB, p).why == "size"


def test_unknown_size_passes_the_size_rule():
    assert attach.decide("file", None, policy(limit=1)) is None


def test_reason_for_registry():
    r = attach.decide("file", 20 * MB, policy())
    assert attach.reason_for(r) == {"reason": "size", "kind": "file", "size": 20 * MB}


# ---- the name ----------------------------------------------------------------

def test_one_line_matches_the_bots():
    for s in ["a\nb", "x\t\0y", "  lead", "ok.pdf", "a b", "", "é ü"]:
        assert attach.one_line(s) == bot.one_line(s)


@pytest.mark.parametrize("hostile, want", [
    ("../x", "_x"),
    ("../../etc/passwd", "_.._etc_passwd"),
    (".bashrc", "bashrc"),
    ("...", "file"),
    ("", "file"),
    (None, "file"),
    ("a\nb.txt", "a b.txt"),
    ("a\\b.txt", "a_b.txt"),
    ("a\0b", "a b"),
    ("/abs/path.pdf", "_abs_path.pdf"),
    (" .  .hidden", "hidden"),          # dots and spaces mixed: still no leading dot
    (". ..", "file"),                   # never `..`
    (". . .x.txt", "x.txt"),
])
def test_hostile_names_are_flattened(hostile, want):
    assert attach.safe_name(hostile) == want


def test_long_name_is_cut_and_keeps_its_extension():
    name = "x" * 300 + ".pdf"
    got = attach.safe_name(name)
    assert got.endswith(".pdf") and len(got.encode()) == attach.NAME_BYTES
    multi = "é" * 200 + ".jpeg"
    got = attach.safe_name(multi)
    assert got.endswith(".jpeg") and len(got.encode()) <= attach.NAME_BYTES
    got.encode("utf-8")         # never cut inside a character


def test_long_extension_is_cut_with_the_name():
    got = attach.safe_name("a." + "b" * 300)
    assert len(got.encode()) <= attach.NAME_BYTES and "/" not in got


# ---- the save ----------------------------------------------------------------

@pytest.fixture
def blobs(tmp_path):
    d = tmp_path / "accounts" / "dc.db-blobs"
    d.mkdir(parents=True)

    def make(data=b"%PDF-1.4 fake\n", name="9f3c1ab07e.pdf"):
        p = d / name
        p.write_bytes(data)
        return p
    return make


@pytest.fixture
def save_dir(tmp_path):
    return tmp_path / "home" / "attachments"


def test_saved_where_the_design_says_with_the_blobs_bytes(blobs, save_dir):
    blob = blobs()
    got = attach.save(blob, save_dir, 12, 5531, "notes.pdf")
    assert got == save_dir / "chat-12" / "5531-notes.pdf"
    assert got.read_bytes() == blob.read_bytes()
    assert not got.is_symlink() and os.stat(got).st_ino != os.stat(blob).st_ino   # a copy


def test_modes_0700_and_0600_whatever_the_umask(blobs, save_dir):
    old = os.umask(0o002)
    try:
        got = attach.save(blobs(), save_dir, 12, 1, "run.sh")
        stranger = attach.save(blobs(), save_dir, 12, 2, "run.sh", trusted=False)
    finally:
        os.umask(old)
    assert mode(save_dir) == 0o700
    assert mode(save_dir / "chat-12") == 0o700
    assert mode(save_dir / "untrusted") == 0o700
    assert mode(save_dir / "untrusted" / "chat-12") == 0o700
    assert mode(got) == 0o600 and mode(stranger) == 0o600     # never executable


def test_a_non_default_save_dir_moves_it(blobs, tmp_path):
    elsewhere = tmp_path / "ada-files"
    got = attach.save(blobs(), elsewhere, 7, 9, "a.png")
    assert got == elsewhere / "chat-7" / "9-a.png" and got.exists()


def test_a_strangers_file_goes_under_untrusted(blobs, save_dir):
    got = attach.save(blobs(), save_dir, 12, 5533, "notes.pdf", trusted=False)
    assert got == save_dir / "untrusted" / "chat-12" / "5533-notes.pdf"
    assert not (save_dir / "chat-12").exists()           # the trees never mix
    assert attach.save_folder(save_dir, 12, trusted=False) == got.parent


def test_two_messages_one_name_two_files(blobs, save_dir):
    a = attach.save(blobs(b"one"), save_dir, 12, 1, "notes.pdf")
    b = attach.save(blobs(b"two"), save_dir, 12, 2, "notes.pdf")
    assert a != b and a.read_bytes() == b"one" and b.read_bytes() == b"two"


def test_the_same_message_twice_is_one_file(blobs, save_dir):
    blob = blobs()
    a = attach.save(blob, save_dir, 12, 1, "notes.pdf")
    b = attach.save(blob, save_dir, 12, 1, "notes.pdf")
    assert a == b
    assert sorted(os.listdir(save_dir / "chat-12")) == ["1-notes.pdf"]


def test_other_bytes_in_the_same_name_get_dash_two(blobs, save_dir):
    a = attach.save(blobs(b"first"), save_dir, 12, 1, "notes.pdf")
    b = attach.save(blobs(b"second", "other.pdf"), save_dir, 12, 1, "notes.pdf")
    c = attach.save(blobs(b"third", "third.pdf"), save_dir, 12, 1, "notes.pdf")
    assert b == save_dir / "chat-12" / "1-notes-2.pdf"
    assert c == save_dir / "chat-12" / "1-notes-3.pdf"
    assert a.read_bytes() == b"first" and b.read_bytes() == b"second"
    # and each is found again, not copied once more
    assert attach.save(blobs(b"second", "other.pdf"), save_dir, 12, 1, "notes.pdf") == b
    assert len(os.listdir(save_dir / "chat-12")) == 3


@pytest.mark.parametrize("hostile", ["../x", "../../escape.txt", ".bashrc", "a\nb.txt",
                                     "x" * 300 + ".pdf", "/etc/passwd"])
def test_a_hostile_name_stays_inside_its_folder(blobs, save_dir, hostile):
    got = attach.save(blobs(), save_dir, 12, 4, hostile)
    assert got.parent == save_dir / "chat-12"
    assert got.resolve().parent == (save_dir / "chat-12").resolve()
    assert not got.name.startswith(".") and "\n" not in got.name
    if "." in hostile.lstrip("."):
        assert got.suffix == os.path.splitext(hostile)[1]     # the extension is kept
    assert os.listdir(save_dir) == ["chat-12"]


def test_written_as_part_then_renamed(blobs, save_dir, monkeypatch):
    seen = []
    real = os.rename

    def spy(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        seen.append((src, dst, os.stat(src, dir_fd=src_dir_fd).st_size))
        return real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
    monkeypatch.setattr(attach.os, "rename", spy)
    got = attach.save(blobs(b"whole"), save_dir, 12, 3, "a.txt")
    assert seen == [(".3-a.txt.part", "3-a.txt", 5)]
    assert os.listdir(got.parent) == ["3-a.txt"]        # no .part left behind


def test_a_stale_part_is_replaced(blobs, save_dir):
    folder = save_dir / "chat-12"
    folder.mkdir(parents=True, mode=0o700)
    (folder / ".3-a.txt.part").write_bytes(b"half")
    got = attach.save(blobs(b"whole"), save_dir, 12, 3, "a.txt")
    assert got.read_bytes() == b"whole" and os.listdir(folder) == ["3-a.txt"]


@pytest.mark.parametrize("trusted, where", [(True, "chat-12"), (False, "untrusted"),
                                            (False, "untrusted/chat-12")])
def test_a_symlinked_folder_is_refused(blobs, save_dir, tmp_path, trusted, where):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = save_dir / where
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(attach.Refused):
        attach.save(blobs(), save_dir, 12, 1, "a.txt", trusted=trusted)
    assert list(outside.rglob("*")) == []


def test_a_file_where_a_folder_goes_is_refused(blobs, save_dir):
    save_dir.mkdir(parents=True)
    (save_dir / "chat-12").write_text("x")
    with pytest.raises(attach.Refused):
        attach.save(blobs(), save_dir, 12, 1, "a.txt")


def test_a_folder_this_user_does_not_own_is_refused(blobs, save_dir, monkeypatch):
    """Our folder seen as another uid's (no chown without root: getuid is faked)."""
    real = os.getuid()
    attach.save(blobs(), save_dir, 12, 1, "a.txt")      # made, ours
    monkeypatch.setattr(attach.os, "getuid", lambda: real + 1)
    with pytest.raises(attach.Refused, match="not this user"):
        attach.save(blobs(), save_dir, 12, 2, "b.txt")
    assert not (save_dir / "chat-12" / "2-b.txt").exists()


def test_a_symlink_at_the_files_name_is_never_followed(blobs, save_dir, tmp_path):
    target = tmp_path / "victim"
    target.write_bytes(b"keep")
    folder = save_dir / "chat-12"
    folder.mkdir(parents=True, mode=0o700)
    (folder / "1-a.txt").symlink_to(target)
    got = attach.save(blobs(b"new"), save_dir, 12, 1, "a.txt")
    assert got == folder / "1-a-2.txt" and got.read_bytes() == b"new"
    assert target.read_bytes() == b"keep"


def test_a_fifo_at_the_files_name_is_taken_not_waited_on(blobs, save_dir):
    """A FIFO at the name: seen as taken (-2), never opened blocking (that would
    hang the bridge waiting for a writer)."""
    import signal
    folder = save_dir / "chat-12"
    folder.mkdir(parents=True, mode=0o700)
    os.mkfifo(folder / "1-a.txt")
    old = signal.signal(signal.SIGALRM, lambda *_: pytest.fail("save() blocked on a FIFO"))
    signal.alarm(5)
    try:
        got = attach.save(blobs(b"new"), save_dir, 12, 1, "a.txt")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert got == folder / "1-a-2.txt" and got.read_bytes() == b"new"
    assert stat.S_ISFIFO(os.lstat(folder / "1-a.txt").st_mode)


def test_a_socket_at_the_files_name_is_taken_not_an_error(blobs, save_dir, monkeypatch):
    """A socket at the name: open() refuses it (ENXIO, or EOPNOTSUPP on macOS).
    It is taken (-2), like a FIFO or a symlink, and the save does not fail."""
    import socket
    folder = save_dir / "chat-12"
    folder.mkdir(parents=True, mode=0o700)
    monkeypatch.chdir(folder)           # bind by a short relative name: AF_UNIX paths are short
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind("1-a.txt")
        got = attach.save(blobs(b"new"), save_dir, 12, 1, "a.txt")
    finally:
        sock.close()
    assert got == folder / "1-a-2.txt" and got.read_bytes() == b"new"
    assert stat.S_ISSOCK(os.lstat(folder / "1-a.txt").st_mode)


def test_a_widened_folder_is_made_0700_again(blobs, save_dir):
    attach.save(blobs(), save_dir, 12, 1, "a.txt")
    os.chmod(save_dir / "chat-12", 0o755)
    attach.save(blobs(), save_dir, 12, 2, "b.txt")
    assert mode(save_dir / "chat-12") == 0o700


def test_try_save_logs_and_gives_none(blobs, save_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    save_dir.mkdir(parents=True)
    (save_dir / "chat-12").symlink_to(outside)
    said = []
    assert attach.try_save(blobs(), save_dir, 12, 1, "a.txt", log=said.append) is None
    assert len(said) == 1 and "SAVE FAILED msg 1" in said[0] and "blob" in said[0]
    assert attach.try_save(tmp_path / "no-such-blob", save_dir, 13, 1, "a.txt") is None
    ok = attach.try_save(blobs(), save_dir, 14, 1, "a.txt", log=said.append)
    assert ok == save_dir / "chat-14" / "1-a.txt" and len(said) == 1


# ---- the registry --------------------------------------------------------------

@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir(mode=0o700)
    return h


def test_a_record_holds_exactly_the_designs_fields(home):
    assert attach.record(home, 5531, 12, True, "size", kind="file", size=20 * MB,
                         at="2030-06-09T19:00:00+11:00")
    path = home / "not-downloaded.json"
    assert path == attach.registry_path(home)
    data = json.loads(path.read_text())
    assert data == {"5531": {"chat_id": 12, "ocr": True, "reason": "size", "kind": "file",
                             "size": 20 * MB, "at": "2030-06-09T19:00:00+11:00"}}
    assert "route" not in data["5531"]
    assert mode(path) == 0o600


def test_at_is_stamped_when_not_given(home):
    attach.record(home, 1, 12, True, "type", kind="video", size=5)
    assert attach.entry(home, 1)["at"][:4].isdigit()


@pytest.mark.parametrize("reason", ["size", "type", "timeout", "failure"])
def test_every_reason_is_recorded(home, reason):
    assert attach.record(home, 7, 12, True, reason)
    assert attach.entry(home, 7)["reason"] == reason


def test_undecipherable_is_never_recorded(home):
    assert attach.record(home, 7, 12, True, "undecipherable") is False
    assert attach.record(home, 7, 12, True, "Undecipherable") is False
    assert not (home / "not-downloaded.json").exists()
    assert attach.entry(home, 7) is None


def test_an_unknown_reason_is_an_error(home):
    with pytest.raises(ValueError):
        attach.record(home, 7, 12, True, "because")
    assert attach.entries(home) == {}


def test_a_strangers_is_recorded_with_ocr_false(home):
    assert attach.record(home, 5533, 12, False, "timeout")
    assert attach.entry(home, 5533)["ocr"] is False


def test_recording_one_message_again_replaces_its_entry(home):
    attach.record(home, 1, 12, True, "size", kind="file", size=20 * MB)
    attach.record(home, 1, 12, True, "type", kind="video", size=3)
    attach.record(home, 2, 13, False, "failure")
    got = attach.entries(home)
    assert sorted(got) == [1, 2] and got[1]["reason"] == "type"


def test_drop_gives_the_entry_once(home):
    attach.record(home, 1, 12, True, "size")
    attach.record(home, 2, 12, True, "size")
    first = attach.drop(home, 1)
    assert first["chat_id"] == 12
    assert attach.drop(home, 1) is None
    assert sorted(attach.entries(home)) == [2]


def test_an_unknown_id_has_no_entry(home):
    assert attach.entry(home, 99) is None and attach.drop(home, 99) is None


@pytest.mark.parametrize("junk", ["not json", "[1, 2]", '{"1": "x", "2": {"chat_id": 3}}'])
def test_a_registry_that_cannot_be_read_counts_what_it_can(home, junk):
    (home / "not-downloaded.json").write_text(junk)
    got = attach.entries(home)
    assert got == ({2: {"chat_id": 3}} if junk.startswith("{") else {})
    attach.record(home, 5, 12, True, "size")
    assert 5 in attach.entries(home)


# ---- a fetch: the `fetching` mark (design §7 cases 4 and 5) --------------------------

def test_a_fetch_mark_is_the_one_key_beyond_the_designs_fields(home):
    """test_a_record_holds_exactly_the_designs_fields' sibling: record() never
    writes `fetching`; only mark_fetching adds it, beside the six fields."""
    attach.record(home, 5531, 12, True, "size", kind="file", size=20 * MB,
                  at="2030-06-09T19:00:00+11:00")
    assert "fetching" not in attach.entry(home, 5531)
    got = attach.mark_fetching(home, 5531)
    data = json.loads((home / "not-downloaded.json").read_text())
    assert data == {"5531": {"chat_id": 12, "ocr": True, "reason": "size", "kind": "file",
                             "size": 20 * MB, "at": "2030-06-09T19:00:00+11:00", "fetching": True}}
    assert got == data["5531"]
    assert mode(home / "not-downloaded.json") == 0o600


def test_marking_an_id_with_no_entry_writes_nothing(home):
    assert attach.mark_fetching(home, 99) is None
    assert not (home / "not-downloaded.json").exists()


def test_recording_a_marked_message_again_clears_the_mark(home):
    """A fetch that timed out or failed is built again as a partial: its entry
    stays, without the mark, for a second dc-fetch."""
    attach.record(home, 1, 12, True, "size")
    attach.mark_fetching(home, 1)
    attach.record(home, 1, 12, True, "timeout")
    assert "fetching" not in attach.entry(home, 1)
    assert attach.entry(home, 1)["reason"] == "timeout"


def test_take_fetching_takes_a_marked_entry_once(home):
    attach.record(home, 1, 12, False, "size")
    attach.record(home, 2, 12, True, "size")
    attach.mark_fetching(home, 1)
    got = attach.take_fetching(home, 1)
    assert got["fetching"] is True and got["ocr"] is False
    assert attach.take_fetching(home, 1) is None
    assert sorted(attach.entries(home)) == [2]


def test_take_fetching_leaves_an_unmarked_entry_and_takes_no_lock(home):
    attach.record(home, 1, 12, True, "size")
    (home / ".not-downloaded.lock").unlink()
    assert attach.take_fetching(home, 1) is None
    assert attach.take_fetching(home, 99) is None
    assert 1 in attach.entries(home)
    assert not (home / ".not-downloaded.lock").exists()     # an ordinary message's text: no lock file

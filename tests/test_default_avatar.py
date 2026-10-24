"""The default picture: a slot the package can ship, set by the bot at its start.

A bot with no picture shows the same blank face as every other, so two bots
under one name cannot be told apart in the messenger. sessionphone has a slot
for a default picture, `src/sessionphone/images/default-avatar.png` (or
`.jpg`), package data. Nothing ships there yet: which picture may ship is a
licence question, so this suite holds the slot EMPTY in the tree and tests the
behaviour with a PNG it generates in a tmp dir.

The bot sets the default at its start, the one moment it holds the account
and so can read the picture there: only when a default ships, the account has
none, and the user has not run `sessionphone avatar --clear` (a marker under
the root, removed again by `avatar PATH`). `bot install` only says so.
Fakes only: a fake account, a fake `_open`, tests/conftest.py's FakeLaunchd.
"""
import os
import pathlib
import struct
import zlib

import pytest
from click.testing import CliRunner
from test_bot_online_as import FakeAccount as OnlineAccount
from test_bot_online_as import Online, stub_main

from sessionphone import avatar, service
from sessionphone.bridge import bot, dc_env
from sessionphone.cli import main as cli

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGE_EXTS = ("png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico", "heic", "tif", "tiff")
# The images that do ship, each for a reason of its own: a webxdc app's icon.
ALLOWED = {"src/sessionphone/apps/guide-trail/icon.png"}


def png_bytes() -> bytes:
    """A real 1x1 PNG, made here: no picture file is kept in the tree."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\x80\x80\xff")) + chunk(b"IEND", b""))


class Account:
    """get_avatar / set_avatar as core answers them: set copies into a blob."""

    def __init__(self, blobs: pathlib.Path, avatar_path: str | None = None):
        self.blobs, self.avatar, self.calls, self.reads = blobs, avatar_path, [], 0

    def get_avatar(self):
        self.reads += 1
        return self.avatar

    def set_avatar(self, path=None):
        self.calls.append(path)
        if path is None:
            self.avatar = None
            return
        self.blobs.mkdir(parents=True, exist_ok=True)
        blob = self.blobs / ("b10b" + os.path.splitext(path)[1])
        blob.write_bytes(pathlib.Path(path).read_bytes())
        self.avatar = str(blob)


@pytest.fixture
def slot(tmp_path, monkeypatch):
    """The package's images/ slot, moved to tmp: write a default into it to ship one."""
    d = tmp_path / "images"
    d.mkdir()
    monkeypatch.setattr(avatar, "IMAGES_DIR", d)
    return d


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "root"
    r.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    monkeypatch.delenv("DC_ROOT", raising=False)
    return r


# ---- nothing ships yet ----------------------------------------------------------------------

def test_no_image_file_ships_in_the_package():
    """No picture is committed until one is cleared to ship; the app icon is the one exception."""
    found = {p.relative_to(ROOT).as_posix() for ext in IMAGE_EXTS
             for p in (ROOT / "src").rglob("*") if p.suffix.lower() == f".{ext}"}
    assert found == ALLOWED


def test_the_default_slot_is_empty_in_the_tree():
    slot_dir = ROOT / "src" / "sessionphone" / "images"
    assert not slot_dir.exists() or list(slot_dir.iterdir()) == []
    assert avatar.IMAGES_DIR == slot_dir
    assert avatar.default_path() is None


# ---- the slot -------------------------------------------------------------------------------

def test_default_path_is_none_when_the_slot_is_empty(slot):
    assert avatar.default_path() is None


def test_default_path_is_none_when_the_slot_dir_is_absent(tmp_path):
    assert avatar.default_path(tmp_path / "no-such-dir") is None


def test_default_path_finds_a_png(slot):
    (slot / "default-avatar.png").write_bytes(png_bytes())
    assert avatar.default_path() == slot / "default-avatar.png"


def test_default_path_finds_a_jpg(slot):
    (slot / "default-avatar.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 32)
    assert avatar.default_path() == slot / "default-avatar.jpg"


def test_the_png_wins_over_the_jpg(slot):
    (slot / "default-avatar.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 32)
    (slot / "default-avatar.png").write_bytes(png_bytes())
    assert avatar.default_path() == slot / "default-avatar.png"


def test_a_directory_in_the_slot_is_not_a_picture(slot):
    (slot / "default-avatar.png").mkdir()
    assert avatar.default_path() is None


def test_other_names_in_the_slot_are_not_the_default(slot):
    (slot / "avatar.png").write_bytes(png_bytes())
    assert avatar.default_path() is None


# ---- the bot's start ------------------------------------------------------------------------

def test_the_bot_sets_the_default_when_the_account_has_none(slot, root, tmp_path):
    (slot / "default-avatar.png").write_bytes(png_bytes())
    acct = Account(tmp_path / "blobs")
    line = avatar.at_start(acct, root)
    assert acct.calls == [str(slot / "default-avatar.png")]
    assert line == "picture: default set (default-avatar.png)"


def test_a_picture_already_there_is_kept(slot, root, tmp_path):
    (slot / "default-avatar.png").write_bytes(png_bytes())
    acct = Account(tmp_path / "blobs", "/blobs/theirs.jpg")
    assert avatar.at_start(acct, root) == "picture: kept (the account has one)"
    assert acct.calls == [] and acct.avatar == "/blobs/theirs.jpg"


def test_a_cleared_picture_stays_cleared(slot, root, tmp_path):
    (slot / "default-avatar.png").write_bytes(png_bytes())
    avatar.record_choice(True, root)
    acct = Account(tmp_path / "blobs")
    line = avatar.at_start(acct, root)
    assert acct.calls == []
    assert line.startswith("picture: none (cleared with `sessionphone avatar --clear`)")


def test_no_default_shipped_reads_and_changes_nothing(slot, root, tmp_path):
    acct = Account(tmp_path / "blobs")
    assert avatar.at_start(acct, root) is None
    assert acct.reads == 0 and acct.calls == []


def test_a_slot_file_that_is_no_image_is_never_handed_to_core(slot, root, tmp_path):
    (slot / "default-avatar.png").write_bytes(b"not a picture at all")
    acct = Account(tmp_path / "blobs")
    line = avatar.at_start(acct, root)
    assert acct.calls == []
    assert line.startswith("picture: default not set: not a PNG, JPEG, GIF or BMP")


def test_core_keeping_no_picture_is_said(slot, root, tmp_path):
    (slot / "default-avatar.png").write_bytes(png_bytes())

    class Dropping(Account):
        def set_avatar(self, path=None):
            self.calls.append(path)

    acct = Dropping(tmp_path / "blobs")
    assert avatar.at_start(acct, root).startswith("picture: default not set:")


# ---- the marker: the user's choice ----------------------------------------------------------

def test_clear_writes_the_marker_0600_and_a_picture_removes_it(root):
    avatar.record_choice(True, root)
    m = root / avatar.NONE_MARKER
    assert m.is_file() and (m.stat().st_mode & 0o777) == 0o600
    assert avatar.chose_none(root)
    avatar.record_choice(False, root)
    assert not m.exists() and not avatar.chose_none(root)


def test_setting_a_picture_with_no_marker_is_fine(root):
    avatar.record_choice(False, root)
    assert not avatar.chose_none(root)


def test_the_marker_lives_under_the_root(root):
    assert avatar.marker() == root / avatar.NONE_MARKER


@pytest.fixture
def free(root, tmp_path, monkeypatch):
    """`sessionphone avatar` on a free account (the rig of tests/test_avatar.py, cut down)."""
    acct = Account(tmp_path / "blobs")

    def fake_open(create=False):
        yield acct
    monkeypatch.setattr(avatar.account_mod, "_open", fake_open)
    return acct


def test_avatar_clear_records_the_choice(free, root):
    r = CliRunner().invoke(cli, ["avatar", "--clear"])
    assert r.exit_code == 0, r.output
    assert avatar.chose_none(root)


def test_avatar_path_removes_the_choice(free, root, tmp_path):
    avatar.record_choice(True, root)
    face = tmp_path / "face.png"
    face.write_bytes(png_bytes())
    r = CliRunner().invoke(cli, ["avatar", str(face)])
    assert r.exit_code == 0, r.output
    assert not avatar.chose_none(root)


def test_a_refused_picture_leaves_the_choice_as_it_was(free, root, tmp_path):
    avatar.record_choice(True, root)
    bad = tmp_path / "face.png"
    bad.write_bytes(b"not a picture")
    r = CliRunner().invoke(cli, ["avatar", str(bad)])
    assert r.exit_code == 2
    assert avatar.chose_none(root)


def test_showing_the_picture_leaves_the_choice_as_it_was(free, root):
    avatar.record_choice(True, root)
    r = CliRunner().invoke(cli, ["avatar"])
    assert r.exit_code == 0, r.output
    assert avatar.chose_none(root)


# ---- bot.main(): the default goes on before the "online as" line ----------------------------

class PictureAccount(OnlineAccount, Account):
    def __init__(self, blobs):
        OnlineAccount.__init__(self, [{"addr": "bot@relay.example.org"}], {"displayname": "Bot"})
        Account.__init__(self, blobs)


def start_lines(monkeypatch, tmp_path, account) -> list[str]:
    lines = []

    def log(msg):
        lines.append(msg)
        if msg.startswith("online as"):
            raise Online(msg)

    stub_main(monkeypatch, tmp_path, account, log)
    with pytest.raises(Online):
        bot.main()
    return lines


def test_the_bot_start_sets_the_default_and_logs_one_line(monkeypatch, tmp_path, slot, root):
    (slot / "default-avatar.png").write_bytes(png_bytes())
    monkeypatch.setattr(dc_env, "ROOT", root)
    acct = PictureAccount(tmp_path / "blobs")
    lines = start_lines(monkeypatch, tmp_path, acct)
    assert acct.calls == [str(slot / "default-avatar.png")]
    assert [x for x in lines if x.startswith("picture:")] == ["picture: default set (default-avatar.png)"]


def test_the_bot_start_honours_the_marker_under_its_own_root(monkeypatch, tmp_path, slot, root):
    (slot / "default-avatar.png").write_bytes(png_bytes())
    monkeypatch.setattr(dc_env, "ROOT", root)
    avatar.record_choice(True, root)
    acct = PictureAccount(tmp_path / "blobs")
    start_lines(monkeypatch, tmp_path, acct)
    assert acct.calls == []


def test_a_failing_picture_never_stops_the_bot(monkeypatch, tmp_path, slot, root):
    (slot / "default-avatar.png").write_bytes(png_bytes())
    monkeypatch.setattr(dc_env, "ROOT", root)

    class Refusing(PictureAccount):
        def set_avatar(self, path=None):
            raise RuntimeError("core said no")

    lines = start_lines(monkeypatch, tmp_path, Refusing(tmp_path / "blobs"))
    assert "picture: default not set: core said no" in lines
    assert lines[-1].startswith("online as")


def test_with_nothing_shipped_the_bot_start_says_nothing(monkeypatch, tmp_path, slot, root):
    monkeypatch.setattr(dc_env, "ROOT", root)
    acct = PictureAccount(tmp_path / "blobs")
    lines = start_lines(monkeypatch, tmp_path, acct)
    assert not [x for x in lines if x.startswith("picture:")]
    assert acct.reads == 0


# ---- bot install: one line, only when a default ships ---------------------------------------

def test_install_line_is_none_when_nothing_ships(slot):
    assert avatar.install_line() is None


def test_install_line_names_the_default_and_the_conditions(slot):
    (slot / "default-avatar.png").write_bytes(png_bytes())
    line = avatar.install_line()
    assert line.startswith("picture:   the bot sets the default picture (default-avatar.png) at its start")
    assert "has none" in line and "not cleared it" in line


@pytest.fixture
def install_rig(monkeypatch, tmp_path, launchd, given_seat):
    """tests/test_install_loaded.py's rig: a fake launchd, a given seat, callers and channels."""
    for k in [k for k in os.environ if k.startswith("DC_")]:
        monkeypatch.delenv(k)
    given_seat("ada-chat")
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    callers = tmp_path / "callers.txt"
    callers.write_text("owner@phone.example.org\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    chans = tmp_path / "channels.txt"
    chans.write_text("owner  owner@phone.example.org\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    return launchd


def test_bot_install_says_the_bot_sets_the_default(install_rig, slot, given_seat):
    given_seat("claude-chat")
    (slot / "default-avatar.png").write_bytes(png_bytes())
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    said = [x for x in r.output.splitlines() if x.startswith("picture:")]
    assert said == [avatar.install_line()]


def test_bot_install_says_nothing_of_a_picture_when_none_ships(install_rig, slot, given_seat):
    given_seat("claude-chat")
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert "picture:" not in r.output

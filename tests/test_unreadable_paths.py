"""Unreadable is not absent, on every Python (PHONE-191 fix-002).

CPython 3.14 sends Path.exists/is_dir/is_file through os.path.*, which
swallows every OSError: for a path below a directory this process may not
search, 3.11-3.13 raise PermissionError and 3.14+ answer False. paths.exists,
paths.is_dir and paths.is_file answer as 3.13 did, on every version, and the
sites that must not read "unreadable" as "absent" ask them.

Every test here builds a REAL unreadable directory (chmod 000, or 0400: names
listable, nothing inside reachable) and asserts one behaviour, with no branch on
the running Python: the suite is run on 3.13 and on 3.14 and must pass on both.
Fakes only otherwise, as the rest of the suite.
"""
from __future__ import annotations

import json
import os
import threading

import pytest
from click.testing import CliRunner

from sessionphone import account, avatar, imgtext, paths, voicemsg
from sessionphone.bridge import calls, dc_env, recording, spool
from sessionphone.cli import main

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root reads through any mode")

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


@pytest.fixture
def lock(tmp_path):
    """lock(dir, mode=0) chmods DIR and gives it back; every lock is undone at teardown,
    so pytest can remove tmp_path."""
    held = []

    def lock_(d, mode=0o000):
        d.chmod(mode)
        held.append(d)
        return d

    yield lock_
    for d in reversed(held):
        d.chmod(0o700)


@pytest.fixture
def sealed(tmp_path, lock):
    """A dir at mode 000 holding a file and a dir: nothing below it can be reached."""
    d = tmp_path / "sealed"
    d.mkdir()
    (d / "f.txt").write_text("x")
    (d / "sub").mkdir()
    return lock(d)


def test_the_rig_really_is_unreadable(sealed):
    """os.stat raises below it on every Python: what every test below rests on."""
    with pytest.raises(PermissionError):
        os.stat(sealed / "f.txt")


# ---- the helpers --------------------------------------------------------------

@pytest.mark.parametrize("probe", [paths.exists, paths.is_dir, paths.is_file])
@pytest.mark.parametrize("name", ["f.txt", "sub", "no-such"])
def test_below_an_unsearchable_dir_every_helper_raises(sealed, probe, name):
    with pytest.raises(PermissionError):
        probe(sealed / name)


def test_the_helpers_answer_as_pathlib_did_where_nothing_is_locked(tmp_path):
    f, d = tmp_path / "f", tmp_path / "d"
    f.write_text("x")
    d.mkdir()
    (tmp_path / "link").symlink_to(f)
    (tmp_path / "dangling").symlink_to(tmp_path / "gone")
    for p, want in [(f, (True, False, True)), (d, (True, True, False)),
                    (tmp_path / "link", (True, False, True)),          # followed
                    (tmp_path / "dangling", (False, False, False)),
                    (tmp_path / "gone", (False, False, False)),
                    (f / "below-a-file", (False, False, False)),       # ENOTDIR is "not there"
                    (str(tmp_path / "nul\0x"), (False, False, False))]:
        assert (paths.exists(p), paths.is_dir(p), paths.is_file(p)) == want, p


def test_a_mode_000_FILE_in_a_readable_dir_still_exists(tmp_path, lock):
    """Only the dirs ABOVE a path matter to stat: a locked file is there, and a regular file."""
    d = tmp_path / "d"
    d.mkdir()
    f = d / "f"
    f.write_text("x")
    f.chmod(0o000)
    try:
        assert paths.exists(f) and paths.is_file(f)
    finally:
        f.chmod(0o600)


# ---- avatar: the wait loops (danger: "absent" read as "taken" or "acked") ------

def test_avatar_queue_raises_on_an_unsearchable_acks_dir_not_a_late_no_ack(tmp_path, lock, monkeypatch):
    """The acks dir exists but cannot be searched: the ack wait must not read "no ack
    yet" for ack_wait seconds and end NOT CONFIRMED. It raises, as 3.13 always did."""
    box, acks = tmp_path / "outbox", tmp_path / "acks"
    acks.mkdir()
    lock(acks)
    monkeypatch.setattr(avatar.jobs, "check", lambda box, job: None)
    monkeypatch.setattr(avatar, "POLL", 0.01)

    def take():                         # a bot that takes the one job, and cannot ack
        for _ in range(500):
            found = sorted(box.glob("*.json")) if box.is_dir() else []
            if found:
                found[0].unlink()
                return
            threading.Event().wait(0.01)
    t = threading.Thread(target=take, daemon=True)
    t.start()
    with pytest.raises(PermissionError):
        avatar.queue("/x.png", box=box, acks=acks, taken_wait=5.0, ack_wait=5.0)
    t.join(5)


def test_avatar_queue_raises_when_the_outbox_turns_unsearchable_mid_wait(tmp_path, lock, monkeypatch):
    """The job was written, then its box sealed: that is not "a bot took it"."""
    box, acks = tmp_path / "outbox", tmp_path / "acks"
    monkeypatch.setattr(avatar.jobs, "check", lambda box, job: None)
    monkeypatch.setattr(avatar, "POLL", 0.01)
    real_replace = os.replace

    def replace_then_seal(src, dst):
        real_replace(src, dst)
        lock(box)
    monkeypatch.setattr(avatar.os, "replace", replace_then_seal)
    with pytest.raises(PermissionError):
        avatar.queue("/x.png", box=box, acks=acks, taken_wait=5.0, ack_wait=5.0)


# ---- the message-only sites: one refusal, no traceback, on every Python --------

def test_avatar_precheck_names_the_unreadable_file_not_a_missing_one(sealed):
    with pytest.raises(avatar.Failed) as exc:
        avatar.precheck(sealed / "f.txt")
    assert exc.value.code == avatar.REFUSED
    assert "cannot read" in str(exc.value) and "Permission denied" in str(exc.value)
    assert "no such file" not in str(exc.value)


def test_voicemsg_check_says_cannot_be_read_not_no_such_file(sealed, capsys):
    why = voicemsg.problem(sealed / "note.ogg")
    assert why == "cannot be read: Permission denied"
    assert voicemsg.main(["check", str(sealed / "note.ogg")]) == 1
    assert "cannot be read: Permission denied" in capsys.readouterr().err


def test_invite_png_below_an_unsearchable_dir_is_exit_1_one_line(tmp_path, sealed, monkeypatch):
    r = tmp_path / "phone"
    r.mkdir(mode=0o700)
    r.chmod(0o700)
    (r / "invite.txt").write_text("https://i.delta.chat/#made-up\n")
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    res = CliRunner().invoke(main, ["invite", "--png", str(sealed / "sub" / "invite.png")])
    assert res.exit_code == 1
    assert res.stdout == ""
    assert res.stderr.count("\n") == 1 and "Traceback" not in res.stderr
    assert "cannot be read (Permission denied)" in res.stderr
    assert "no such directory" not in res.stderr


def test_imgtext_start_on_an_unsearchable_bin_dir_is_off_and_never_builds(sealed):
    """Does not rely on the raise; pinned. 3.13 raises at `binary.is_file()`; 3.14
    reads "no binary", then mkdir fails and the `tmp.unlink(missing_ok=True)` beside
    it raises PermissionError. Both end on the same line, and swiftc never runs."""
    ran, log = [], []
    eng = imgtext.ImageText()
    eng.start(sealed / "bin", log.append, run=lambda *a, **k: ran.append(a),
              which=lambda name: "/usr/bin/swiftc", platform="darwin", background=False)
    assert ran == []
    assert len(log) == 1 and log[0].startswith("IMGTEXT off: start-up failed: PermissionError")
    assert eng.OFF.is_set()


# ---- the callers file: unreadable is not "nobody" ------------------------------

def test_read_callers_and_owner_raise_on_an_unreadable_file(sealed):
    with pytest.raises(PermissionError):
        account.read_callers(sealed / "callers.txt")
    with pytest.raises(PermissionError):
        account.read_owner(sealed / "callers.txt")


def test_callers_problem_says_cannot_be_read_not_does_not_exist(sealed):
    why = dc_env.callers_problem({"DC_CALLERS_FILE": str(sealed / "callers.txt")})
    assert "cannot be read (Permission denied)" in why
    assert "does not exist" not in why


# ---- the mail leg: an unreachable box is loud, never an empty one --------------

def test_mail_leg_candidates_raise_on_an_unsearchable_box(tmp_path, lock, monkeypatch):
    box = tmp_path / "box"
    (box / "new").mkdir(parents=True)
    (box / "new" / "m1").write_text("From: a@b\n\nhi\n")
    monkeypatch.setattr(calls, "BOT_BOX", lock(box))
    with pytest.raises(PermissionError):
        calls.MailLeg(manager=None).candidates()


@pytest.mark.parametrize("sub", ["new", "cur"])
def test_mail_leg_candidates_raise_on_a_listable_but_unsearchable_subdir(tmp_path, lock, monkeypatch, sub):
    """0400: iterdir lists the names, but no entry can be stat'ed. 3.14 read every
    one as "not a file" and spoke nothing, in silence."""
    box = tmp_path / "box"
    (box / "new").mkdir(parents=True)
    (box / "cur").mkdir()
    (box / sub / "m1:2,").write_text("From: a@b\n\nhi\n")
    lock(box / sub, 0o400)
    monkeypatch.setattr(calls, "BOT_BOX", box)
    with pytest.raises(PermissionError):
        calls.MailLeg(manager=None).candidates()


# ---- recording: a transient dir that cannot be read is not "unmarked" ----------

def test_recording_clean_raises_on_an_unsearchable_call_dir(tmp_path, lock):
    d = tmp_path / "call-1"
    d.mkdir()
    (d / recording.TRANSIENT_MARK).write_text("transient\n")
    (d / "in.wav").write_bytes(b"RIFF")
    with pytest.raises(PermissionError):
        recording.clean(lock(d))


def test_recording_sweep_and_prune_raise_below_an_unsearchable_dir(sealed):
    with pytest.raises(PermissionError):
        recording.sweep(sealed / "sub")
    rec = recording.Record(on=True, source="test")
    with pytest.raises(PermissionError):
        recording.prune(sealed / "sub", rec, days=1)


# ---- spool: does not rely on the raise; pinned so it stays the same ------------

@pytest.mark.parametrize("mode", [0o000, 0o400])
def test_spool_reads_an_unreadable_spool_as_empty_on_every_python(tmp_path, lock, mode):
    root = tmp_path / "spool"
    (root / spool.DEAD).mkdir(parents=True)
    (root / "1-1.json").write_text(json.dumps({"v": 1}))
    (root / spool.DEAD / "1-2.json").write_text(json.dumps({"v": 1}))
    s = spool.Spool(lambda m: None, root=root)
    lock(root / spool.DEAD, mode)
    lock(root, mode)
    assert s.waiting() == []
    assert s.counts() == (0, 0)



def test_hang_up_raises_on_an_unsearchable_marked_call_dir_not_a_silent_skip(tmp_path, lock, monkeypatch):
    """The hang-up's TRANSIENT check gates the drain (and so clean()): on 3.14 a
    Path.exists there read "unmarked", skipped the drain and left the transient
    audio in silence. It raises, as 3.13 always did."""
    monkeypatch.setattr(calls, "CALLS_DIR", tmp_path)
    monkeypatch.setattr(calls, "_last_prune", None)
    d = tmp_path / "call-1"
    d.mkdir()
    (d / recording.TRANSIENT_MARK).write_text("transient\n")
    (d / "in.wav").write_bytes(b"RIFF")
    s = object.__new__(calls.CallSession)
    s.dir, s.rec_state, s.rec_drain = lock(d), object(), None
    s._rec_check = lambda: None
    with pytest.raises(PermissionError):
        s._rec_end()
    assert s.rec_drain is None

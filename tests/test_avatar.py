"""`sessionphone avatar [PATH | --clear]`: the bot's picture, set free or held (PHONE-126).

Core 2.62.0 copies a file it then refuses into the blob dir first (design.md
section 1), so the file is judged by its first bytes BEFORE any account is
opened. A free account is set directly; one the running bot holds gets an
{"avatar": ...} outbox job, judged by jobs.check first, and the CLI waits for
the bot's ack. Fakes only: a fake account behind a fake `_open`, a fake bot
thread on a tmp outbox, and one pass of bot.main()'s outbox loop over fakes
(the rig of tests/test_group_job.py, cut down). No rpc-server, no network.
"""
import json
import os
import shutil
import threading
import time

import pytest
from click.testing import CliRunner

from sessionphone import avatar, jobs
from sessionphone.cli import main

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
GIF87 = b"GIF87a" + b"\0" * 32
GIF89 = b"GIF89a" + b"\0" * 32
BMP = b"BM" + b"\0" * 32
LOCKED = ("RPC server failed to start: Error: Delta Chat is already running on this accounts "
          "directory (accounts.lock file is already locked)")


class FakeAccount:
    """set_avatar copies the file into the blob dir, as core does; None clears it."""

    def __init__(self, blobs, refuse=None):
        self.blobs, self.refuse, self.avatar, self.calls = blobs, refuse, None, []

    def set_avatar(self, path=None):
        self.calls.append(path)
        if self.refuse:
            raise RuntimeError(self.refuse)
        if path is None:
            self.avatar = None
            return
        self.blobs.mkdir(parents=True, exist_ok=True)
        blob = self.blobs / ("abc123" + os.path.splitext(path)[1])
        shutil.copyfile(path, blob)
        self.avatar = str(blob)

    def get_avatar(self):
        return self.avatar


class Rig:
    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig()
    r.root = tmp_path / "root"
    r.root.mkdir(mode=0o700)
    r.blobs = r.root / "accounts" / "uuid" / "dc.db-blobs"
    r.outbox, r.acks = r.root / "outbox", r.root / "acks"
    r.account = FakeAccount(r.blobs)
    r.opened = 0
    r.state = "free"        # free | held | none
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r.root))
    monkeypatch.setattr(avatar, "TAKEN_WAIT", 2.0)
    monkeypatch.setattr(avatar, "ACK_WAIT", 0.5)
    monkeypatch.setattr(avatar, "POLL", 0.01)

    def fake_open(create=False):
        r.opened += 1
        if r.state == "held":
            raise RuntimeError(LOCKED)
        if r.state == "none":
            raise avatar.paths.NotConfigured(f"no account under {r.root} - run `sessionphone setup` first")
        yield r.account
    monkeypatch.setattr(avatar.account_mod, "_open", fake_open)

    def img(name="face.png", data=PNG):
        p = tmp_path / name
        p.write_bytes(data)
        return p
    r.img = img

    def cli(*args):
        return CliRunner().invoke(main, ["avatar", *args])
    r.cli = cli
    return r


def stamp(r, keys=jobs.KEYS, pid=None):
    r.outbox.mkdir(parents=True, exist_ok=True)
    jobs.stamp_path(r.outbox).write_text(json.dumps(
        {"pid": os.getpid() if pid is None else pid, "started": "2030-12-08T23:34:56+1100",
         "keys": sorted(keys)}))


class FakeBot:
    """Takes ONE job from the outbox, as bot.main() does: ack (unless told not to), then unlink."""

    def __init__(self, r, ok=True, detail=None, ack=True):
        self.r, self.ok, self.detail, self.ack, self.jobs = r, ok, detail, ack, []
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        for _ in range(500):
            found = sorted(self.r.outbox.glob("*.json"))
            if found:
                f = found[0]
                job = json.loads(f.read_text())
                self.jobs.append(job)
                if self.ack:
                    if self.ok:
                        self.r.account.set_avatar(job["avatar"])
                    detail = self.detail if self.detail is not None else (self.r.account.get_avatar() or "none")
                    with open(job["ack"], "w") as fh:
                        json.dump({"ok": self.ok, "detail": detail}, fh)
                f.unlink()
                return
            threading.Event().wait(0.01)


def blob_files(r):
    return sorted(p.name for p in r.blobs.iterdir()) if r.blobs.exists() else []


# ---- the magic-byte sniff ----

@pytest.mark.parametrize("data,name", [(PNG, "PNG"), (JPEG, "JPEG"), (GIF87, "GIF"), (GIF89, "GIF"),
                                       (BMP, "BMP")])
def test_sniff_takes_the_four_formats_core_decodes(data, name):
    assert avatar.sniff(data[:avatar.SNIFF]) == name


@pytest.mark.parametrize("data", [b"", b"hello, a text file named .png", b"II*\0tiff",
                                  b"\0\0\0\x18ftypheic", b"<svg xmlns=", b"RIFF\0\0\0\0WEBP",
                                  b"\x89PNX\r\n\x1a\n", b"GIF88a"])
def test_sniff_refuses_anything_else(data):
    assert avatar.sniff(data[:avatar.SNIFF]) is None


# ---- free account: show, set, clear ----

def test_show_with_no_picture_prints_none(rig):
    res = rig.cli()
    assert res.exit_code == 0, res.output
    assert res.output == "none\n"


def test_set_then_show_then_clear(rig):
    res = rig.cli(str(rig.img()))
    assert res.exit_code == 0, res.output
    blob = str(rig.blobs / "abc123.png")
    assert f"avatar: {blob}\n" in res.output
    assert "shows after this account's next message; nothing was sent" in res.output
    assert "by the running bot" not in res.output
    assert rig.account.calls == [str(rig.img().resolve())]      # absolute, resolved
    assert rig.cli().output == f"{blob}\n"
    res = rig.cli("--clear")
    assert res.exit_code == 0, res.output
    assert "avatar: cleared\n" in res.output
    assert rig.account.calls[-1] is None
    assert rig.cli().output == "none\n"


def test_a_relative_path_is_given_to_core_absolute(rig, monkeypatch, tmp_path):
    rig.img("rel.jpg", JPEG)
    monkeypatch.chdir(tmp_path)
    res = rig.cli("rel.jpg")
    assert res.exit_code == 0, res.output
    assert rig.account.calls == [str((tmp_path / "rel.jpg").resolve())]


def test_the_extension_is_not_trusted_only_the_bytes(rig):
    res = rig.cli(str(rig.img("really-a-png.txt", PNG)))
    assert res.exit_code == 0, res.output


# ---- every refusal exits 2, never calls set_avatar, and touches no blob dir ----

def _refusals(rig, tmp_path):
    unreadable = rig.img("locked.png", PNG)
    unreadable.chmod(0)
    (tmp_path / "adir").mkdir()
    return {
        "missing": (str(tmp_path / "nope.png"), "no such file"),
        "directory": (str(tmp_path / "adir"), "not a regular file"),
        "unreadable": (str(unreadable), "cannot read"),
        "empty": (str(rig.img("empty.png", b"")), "not a PNG, JPEG, GIF or BMP"),
        "text named .png": (str(rig.img("fake.png", b"just some text\n")), "not a PNG, JPEG, GIF or BMP"),
        "tiff": (str(rig.img("pic.tiff", b"II*\0" + b"\0" * 20)), "not a PNG, JPEG, GIF or BMP"),
    }


@pytest.mark.parametrize("case", ["missing", "directory", "unreadable", "empty", "text named .png", "tiff"])
@pytest.mark.parametrize("state", ["free", "held"])
def test_a_refusal_exits_2_before_any_account_is_opened(rig, tmp_path, case, state):
    if case == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file")
    rig.state = state
    stamp(rig)      # a live bot that would take the job: it must never be asked
    rig.blobs.mkdir(parents=True)
    (rig.blobs / "existing.png").write_bytes(PNG)
    path, why = _refusals(rig, tmp_path)[case]
    res = rig.cli(path)
    assert res.exit_code == 2, res.output
    assert why in res.output and "nothing was changed" in res.output
    assert rig.opened == 0 and rig.account.calls == []
    assert blob_files(rig) == ["existing.png"]                  # no orphan blob
    assert not list(rig.outbox.glob("*.json")) and not rig.acks.exists()   # nothing queued


def test_core_refusing_a_file_that_passed_exits_2_with_its_text(rig):
    rig.account.refuse = "image decode failure: truncated"
    res = rig.cli(str(rig.img()))
    assert res.exit_code == 2, res.output
    assert "Delta Chat refused it: image decode failure: truncated; the old picture stays" in res.output


def test_profile_is_refused_plainly_and_never_applied_to_the_default(rig):
    for args in (["--profile", "public", str(rig.img())], ["--profile", "anything"],
                 ["--profile", "public", "--clear"]):
        res = rig.cli(*args)
        assert res.exit_code == 2, res.output
        assert "the public profile is not supported yet" in res.output
    assert rig.opened == 0 and rig.account.calls == []


def test_path_and_clear_together_is_refused(rig):
    res = rig.cli(str(rig.img()), "--clear")
    assert res.exit_code == 2 and rig.opened == 0 and rig.account.calls == []


def test_not_configured_exits_1(rig):
    rig.state = "none"
    for args in ([], [str(rig.img())], ["--clear"]):
        res = rig.cli(*args)
        assert res.exit_code == 1, res.output
        assert "run `sessionphone setup`" in res.output


def test_no_root_at_all_exits_1(rig, monkeypatch, tmp_path):
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "never-made"))
    res = rig.cli(str(rig.img()))
    assert res.exit_code == 1, res.output
    assert rig.opened == 0


def test_a_loose_root_is_refused(rig):
    rig.root.chmod(0o755)
    try:
        res = rig.cli()
    finally:
        rig.root.chmod(0o700)
    assert res.exit_code == 2 and "must be 0700" in res.output


# ---- held by the running bot: queue, judged first, wait for the ack ----

def test_held_by_the_bot_queues_the_job_and_exits_0_on_an_ok_ack(rig):
    rig.state = "held"
    stamp(rig)
    bot = FakeBot(rig)
    res = rig.cli(str(rig.img()))
    bot.t.join(2)
    assert res.exit_code == 0, res.output
    blob = str(rig.blobs / "abc123.png")
    assert f"avatar: {blob} (by the running bot)" in res.output
    assert "nothing was sent" in res.output
    (job,) = bot.jobs
    assert set(job) == {"avatar", "ack"} and job["avatar"] == str(rig.img().resolve())
    assert job["ack"].startswith(str(rig.acks))
    assert not list(rig.outbox.glob("*.json*"))                 # no job, no .part left behind


def test_held_clear_queues_null(rig):
    rig.state = "held"
    stamp(rig)
    rig.account.avatar = "/somewhere/old.png"
    bot = FakeBot(rig)
    res = rig.cli("--clear")
    bot.t.join(2)
    assert res.exit_code == 0, res.output
    assert "avatar: cleared (by the running bot)" in res.output
    assert bot.jobs[0]["avatar"] is None and rig.account.avatar is None


def test_held_and_the_bot_says_no_exits_3(rig):
    rig.state = "held"
    stamp(rig)
    bot = FakeBot(rig, ok=False, detail="JsonRpcError: image decode failure")
    res = rig.cli(str(rig.img()))
    bot.t.join(2)
    assert res.exit_code == 3, res.output
    assert "the bot did not take it: JsonRpcError: image decode failure; the old picture stays" in res.output


def test_held_by_an_older_bot_exits_3_and_queues_nothing(rig):
    rig.state = "held"
    stamp(rig, keys=jobs.KEYS - {"avatar"})                     # a live bot from before PHONE-126
    res = rig.cli(str(rig.img()))
    assert res.exit_code == 3, res.output
    assert "the running bot (pid" in res.output and "'avatar'" in res.output
    assert "restart the bot" in res.output and "nothing was queued" in res.output
    assert not list(rig.outbox.glob("*.json")) and not rig.acks.exists()


def test_held_by_link_exits_3_and_queues_nothing(rig):
    rig.state = "held"                                          # no bot stamp: `link` holds it
    for args in ([str(rig.img())], ["--clear"]):
        res = rig.cli(*args)
        assert res.exit_code == 3, res.output
        assert "`sessionphone link` is running, wait for it to finish" in res.output
        assert "nothing was queued" in res.output
    assert not rig.outbox.exists() or not list(rig.outbox.glob("*.json"))


def test_held_and_the_bot_took_it_but_wrote_no_ack_exits_4(rig):
    rig.state = "held"
    stamp(rig)
    bot = FakeBot(rig, ack=False)
    res = rig.cli(str(rig.img()))
    bot.t.join(2)
    assert res.exit_code == 4, res.output
    assert "NOT CONFIRMED (no ack)" in res.output


def test_held_and_no_bot_takes_it_withdraws_the_job_and_exits_3(rig, monkeypatch):
    monkeypatch.setattr(avatar, "TAKEN_WAIT", 0.1)
    rig.state = "held"
    stamp(rig)                                                  # a stamp, but nothing reads the outbox
    res = rig.cli(str(rig.img()))
    assert res.exit_code == 3, res.output
    assert "NOT TAKEN" in res.output and "withdrawn" in res.output
    assert not list(rig.outbox.glob("*.json"))                  # never applied later, unasked


def test_a_job_the_bot_read_just_before_the_withdraw_is_reported_by_its_ack(rig, monkeypatch):
    # The bot reads a job, acts, acks, THEN unlinks it. A withdraw that lands after
    # its read does not stop it, so "nothing was changed" would be false: the ack decides.
    monkeypatch.setattr(avatar, "TAKEN_WAIT", 0.1)
    rig.state = "held"
    stamp(rig)
    seen = []

    def slow_bot():
        for _ in range(500):
            found = sorted(rig.outbox.glob("*.json"))
            if found:
                f = found[0]
                job = json.loads(f.read_text())
                seen.append(job)
                while f.exists():                   # still working when the CLI withdraws it
                    threading.Event().wait(0.01)
                rig.account.set_avatar(job["avatar"])
                with open(job["ack"], "w") as fh:
                    json.dump({"ok": True, "detail": rig.account.get_avatar()}, fh)
                f.unlink(missing_ok=True)
                return
            threading.Event().wait(0.01)

    t = threading.Thread(target=slow_bot, daemon=True)
    t.start()
    res = rig.cli(str(rig.img()))
    t.join(2)
    assert seen, "the fake bot never read the job"
    assert res.exit_code == 0, res.output
    assert "withdrawn" not in res.output
    assert f"avatar: {rig.blobs / 'abc123.png'} (by the running bot)" in res.output


def test_show_while_held_exits_3_and_says_who_holds_it(rig):
    rig.state = "held"
    stamp(rig)
    res = rig.cli()
    assert res.exit_code == 3, res.output
    assert "the running bot (pid" in res.output and "cannot be read from here" in res.output
    assert not list(rig.outbox.glob("*.json"))


def test_the_outbox_and_acks_follow_dc_root(rig, monkeypatch, tmp_path):
    other = tmp_path / "botroot"
    monkeypatch.setenv("DC_ROOT", str(other))
    rig.outbox, rig.acks = other / "outbox", other / "acks"
    rig.state = "held"
    stamp(rig)
    bot = FakeBot(rig)
    res = rig.cli(str(rig.img()))
    bot.t.join(2)
    assert res.exit_code == 0, res.output
    assert bot.jobs[0]["ack"].startswith(str(other / "acks"))


def test_held_is_the_lock_error_only():
    assert avatar.held(RuntimeError(LOCKED))
    assert not avatar.held(RuntimeError("RPC server failed to start: something else"))


# ---- the bot's branch: one pass of bot.main()'s outbox loop over fakes ----

class Done(Exception):
    """Ends main()'s loop at its first sleep: one pass of the outbox."""


class BotTime:
    """bot.py's `time`: the time module, but sleep raises Done. Only bot.py's
    own calls see it; the start probe's child wait and every thread keep the
    real one (patched process-wide, the pass could end inside the probe, PHONE-171)."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, s):
        raise Done


class BotAccount(FakeAccount):
    def is_configured(self):
        return True

    def start_io(self):
        pass

    def get_config(self, key):
        return "bot@test"


@pytest.fixture
def botrig(tmp_path, monkeypatch, lock_home_in_tmp):
    from sessionphone.bridge import bot
    lock_home_in_tmp(bot)       # imported here: run alone, the autouse fixtures saw no bot (PHONE-264)

    r = Rig()
    r.out, r.logs = tmp_path / "outbox", []
    r.account = BotAccount(tmp_path / "blobs")
    stop = threading.Event()

    class FakeRpc:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    monkeypatch.setenv("DC_CHANNELS_FILE", str(tmp_path / "channels.txt"))
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(bot.dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "OUTBOX", r.out)
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "DeltaChat", lambda rpc: Obj(get_all_accounts=lambda: [r.account]))
    monkeypatch.setattr(bot, "server_choice", lambda: (None, "package", "test"))
    monkeypatch.setattr(bot, "sweep", lambda account: None)
    monkeypatch.setattr(bot, "handle_events", lambda account: stop.wait())
    monkeypatch.setattr(bot, "alive_or_exit", lambda t, dog: None)
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "INVITE", None)
    monkeypatch.setattr(bot, "log", r.logs.append)
    monkeypatch.setattr(bot.seatmail, "send", lambda to, subject, body, **k: bot.seatmail.Sent(True, id="<1@x>"))
    monkeypatch.setattr(bot.dc_env, "MAILTO", "agent@host.example")

    monkeypatch.setattr(bot, "time", BotTime())

    def run(*jobs_):
        r.out.mkdir(parents=True, exist_ok=True)
        for i, job in enumerate(jobs_):
            job = dict(job, ack=str(tmp_path / "acks" / f"{i:02d}.json"))
            (r.out / f"{i:02d}.json").write_text(json.dumps(job))
        try:
            with pytest.raises(Done):
                bot.main()
        finally:
            stop.set()
        assert not list(r.out.glob("*.json"))
        return [json.loads((tmp_path / "acks" / f"{i:02d}.json").read_text()) for i in range(len(jobs_))]
    r.run = run
    r.tmp = tmp_path
    return r


def test_the_bot_sets_the_avatar_and_acks_the_blob_path(botrig):
    src = botrig.tmp / "face.png"
    src.write_bytes(PNG)
    (a,) = botrig.run({"avatar": str(src)})
    assert a == {"ok": True, "detail": str(botrig.tmp / "blobs" / "abc123.png")}
    assert botrig.account.calls == [str(src)]


def test_a_bot_started_by_a_test_builds_no_ocr_helper(botrig, imgtext_starts, monkeypatch):
    # PHONE-190: bot.main() asks for the OCR helper in the user's real root
    # (dc_env.ROOT is bound at import); conftest's fake engine takes the ask,
    # so no swiftc runs and nothing is written there.
    from sessionphone.bridge import bot
    monkeypatch.setattr(bot, "IMAGE_TEXT", True)
    botrig.run({"avatar": None})
    assert imgtext_starts == [bot.dc_env.ROOT / "bin"]
    assert not any("IMGTEXT" in line for line in botrig.logs)


def test_the_bot_clears_the_avatar_and_acks_none(botrig):
    botrig.account.avatar = "/old.png"
    (a,) = botrig.run({"avatar": None})
    assert a == {"ok": True, "detail": "none"}
    assert botrig.account.calls == [None] and botrig.account.avatar is None


def test_the_bot_acks_a_core_refusal_as_not_ok(botrig):
    botrig.account.refuse = "image decode failure"
    (a,) = botrig.run({"avatar": "/x.png"})
    assert a["ok"] is False and "image decode failure" in a["detail"]


def test_the_stamp_the_bot_writes_lets_an_avatar_job_through(tmp_path):
    jobs.write_stamp(tmp_path)
    assert jobs.check(tmp_path, {"avatar": "/x.png", "ack": "/a.json"}) is None
    assert jobs.check(tmp_path / "nobot", {"avatar": None, "ack": "/a.json"}) is not None

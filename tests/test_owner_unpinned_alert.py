"""The start migration that leaves the OWNER unpinned must say so, by mail.

At the bot's first start after an update, the one-time key migration
(keygate.migrate for callers.txt, channels.migrate for channels.txt) pins the
owner line to its one key contact. When it finds none, it pins nothing, and
from then on the owner can no longer ring (callers.txt) and the bot writes
to no 1:1 chat with him (channels.txt). Before the fix that was a log line
only ("UNPINNED ...", "nobody can ring"), in a log nobody reads: neither the
owner nor the agent seat was told.

Expected: when the migration leaves the OWNER line unpinned, the agent seat
(dc_env.MAILTO, by seatmail, the path the list alert takes) is mailed once,
with the fix in words: re-link with `sessionphone link` (the owner scans), or
pin the key by hand (`callers pin ADDR --key FPR`, FPR from the phone's
Encryption screen). Pinned: no mail. A later start (the file is marked by
then): no second mail. A send that fails is not spent: a later start mails.

These tests run bot.main() through its start and one pass of its outbox
loop, with the real migration over tmp files and a fake account. Every
step around the account is stubbed as tests/test_bot_online_as.py and
tests/_stopped_bot.py stub it. seatmail.send is a fake that records what it
was given, so no mail is sent. Fakes only: no rpc-server, relay, launchctl
or live bot.
"""
from __future__ import annotations

import json
import shlex
import types

import _keyfakes as kf
import pytest

from sessionphone import channels, keygate
from sessionphone.bridge import bot

OWNER = "owner@example.org"
BOT_ADDR = "bot1x2y3z@relay.example.org"
CALLERS_OWNER = f"{OWNER}  # owner, joined 2030-06-09T00:00:00+0000\n"


class Passed(Exception):
    """Raised at the end of the outbox loop's first pass: main() stops there."""


class FakeAccount(kf.Account):
    """A configured account: the key contacts it knows (kf.Account), and the
    few calls main() makes on it before its loop."""

    def is_configured(self):
        return True

    def list_transports(self):
        return [{"addr": BOT_ADDR}]

    def get_config(self, key):
        return {"displayname": "Sam"}.get(key)

    def start_io(self):
        pass


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _write(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def start(monkeypatch, tmp_path):
    """-> run(account, ok=True): one bot start, through the first pass of the
    outbox loop. Every seatmail.send it makes is appended to `start.mails` as
    (to, subject, body); OK says whether the send is accepted. The callers
    and channels files are tmp_path/callers.txt and tmp_path/channels.txt,
    and persist (with the outbox) from one start to the next."""
    mails: list[tuple[str, str, str]] = []
    said: list[str] = []
    state = {"ok": True}

    def send(to, subject, body, **kw):
        mails.append((to, subject, body))
        if state["ok"]:
            return types.SimpleNamespace(ok=True, error="", id=f"<{len(mails)}@test>")
        return types.SimpleNamespace(ok=False, error="rc=75 kind=tempfail: mail is not up yet", id=None)

    def stop_after_one_pass(*a, **k):
        raise Passed

    monkeypatch.setenv("DC_CALLERS_FILE", str(tmp_path / "callers.txt"))
    monkeypatch.setenv("DC_CHANNELS_FILE", str(tmp_path / "channels.txt"))
    monkeypatch.setattr(bot, "log", said.append)
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(bot.seatmail, "check", lambda *a, **k: None)
    monkeypatch.setattr(bot.seatmail, "setup", lambda *a, **k: "stub")
    monkeypatch.setattr(bot.seatmail, "seat", lambda *a, **k: "stub")
    monkeypatch.setattr(bot.seatmail, "send", send)
    monkeypatch.setattr(bot.seat, "have_box", lambda env, shell: bot.seat.Box("stub", "stub@h", "/box"))
    monkeypatch.setattr(bot.dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot.dc_env, "callers_problem", lambda *a, **k: None)
    monkeypatch.setattr(bot.dc_env, "callers_ignored", lambda *a, **k: None)
    monkeypatch.setattr(bot, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(bot.SPOOL, "counts", lambda: (0, 0))
    monkeypatch.setattr(bot.SPOOL, "drain", lambda: None)
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "server_choice", lambda: ("/venv/bin/deltachat-rpc-server", "venv", "test"))
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "INVITE", None)
    monkeypatch.setattr(bot, "IMAGE_TEXT", False)
    monkeypatch.setattr(bot.avatar, "at_start", lambda *a, **k: None)
    monkeypatch.setattr(bot.displayname, "at_start", lambda *a, **k: None)
    monkeypatch.setattr(bot, "xdc_rt_start", lambda account: None)
    monkeypatch.setattr(bot, "xdc_feed_start", lambda account, listing: None)
    monkeypatch.setattr(bot.jobs, "write_stamp", lambda *a, **k: "stub")
    monkeypatch.setattr(bot, "sweep", lambda account: None)
    monkeypatch.setattr(bot, "handle_events", lambda account: None)
    monkeypatch.setattr(bot, "Watchdog", lambda account: types.SimpleNamespace(tick=lambda: None))
    monkeypatch.setattr(bot, "speech_sweep", lambda: None)
    monkeypatch.setattr(bot, "alive_or_exit", stop_after_one_pass)
    if bot.calls is not None:
        monkeypatch.setattr(bot.calls, "settle_greetings", lambda display: None)

    def run(account, ok=True):
        state["ok"] = ok
        monkeypatch.setattr(bot, "DeltaChat",
                            lambda rpc: types.SimpleNamespace(get_all_accounts=lambda: [account]))
        with pytest.raises(Passed):
            bot.main()

    run.mails, run.said = mails, said
    run.callers, run.channels = tmp_path / "callers.txt", tmp_path / "channels.txt"
    return run


def _about_owner(mails):
    """The mails that name the owner's address: the owner-unpinned alert. (The
    channels list alert names no owner here: every list in these tests reads.)"""
    return [m for m in mails if OWNER in m[1] or OWNER in m[2]]


def _pinned_channels(start):
    """A channels.txt whose owner entry is already pinned (so only callers.txt
    is in question), marked: the migration has nothing to do there."""
    _write(start.channels, f"{channels.MARKER}\nowner  {OWNER}  key:{kf.FPR_K1}  # owner\n")


# ---- the rig's premise: passing before the fix ------------------------------


def test_the_rig_runs_the_real_migration_and_it_leaves_the_owner_unpinned(start):
    """The premise: the start migration runs, finds no key contact for the
    owner, pins nothing and marks the file. Today that is all, in the log."""
    _write(start.callers, CALLERS_OWNER)
    _pinned_channels(start)
    start(FakeAccount([]))
    assert keygate.is_marked(start.callers)
    assert keygate.read_caller_entries(start.callers)[0].key is None
    assert any(f"UNPINNED {OWNER}" in s for s in start.said), start.said
    assert any("nobody can ring" in s for s in start.said), start.said


def test_an_owner_the_migration_pins_mails_nothing(start):
    """The owner has his one key contact: pinned, and nobody need be told."""
    _write(start.callers, CALLERS_OWNER)
    _pinned_channels(start)
    start(FakeAccount([kf.Contact(10, OWNER, kf.K1)]))
    assert keygate.read_caller_entries(start.callers)[0].key == kf.FPR_K1
    assert _about_owner(start.mails) == [], start.mails


# ---- the bug: red before the fix ---------------------------------------------


def test_an_owner_left_unpinned_mails_the_agent_seat_once_with_both_fixes(start):
    """callers.txt: the owner line, no key contact for it, so nobody may ring.
    One mail to the agent seat, naming the address and both ways to fix it."""
    _write(start.callers, CALLERS_OWNER)
    _pinned_channels(start)
    start(FakeAccount([]))
    alerts = _about_owner(start.mails)
    assert len(alerts) == 1, f"want one mail naming {OWNER}, got {start.mails}"
    to, subject, body = alerts[0]
    assert to == bot.dc_env.MAILTO
    text = " ".join((subject + "\n" + body).split())
    assert "sessionphone link" in text, text                         # re-link: the owner scans
    assert keygate.PIN_HOW.format(addr=OWNER) in text, text          # or pin the key by hand
    assert "Encryption" in text, text                                # where FPR is read


def test_a_second_start_does_not_mail_again(start):
    """The file is marked after the first start, the alert was accepted:
    the next start is quiet, though the owner is still unpinned."""
    _write(start.callers, CALLERS_OWNER)
    _pinned_channels(start)
    start(FakeAccount([]))
    assert len(_about_owner(start.mails)) == 1, start.mails
    start(FakeAccount([]))
    assert len(_about_owner(start.mails)) == 1, start.mails


def test_a_failed_send_is_not_spent_a_later_start_mails(start):
    """The migration runs once, at a start, maybe before mail is up. A send
    that is refused must not be the only chance: the next start (the file is
    marked by then, so the migration does not run again) still mails."""
    _write(start.callers, CALLERS_OWNER)
    _pinned_channels(start)
    start(FakeAccount([]), ok=False)
    assert _about_owner(start.mails), "the first start tried no mail at all"
    tried = len(start.mails)
    start(FakeAccount([]), ok=True)
    later = _about_owner(start.mails[tried:])
    assert len(later) == 1, f"want one mail at the later start, got {start.mails[tried:]}"
    start(FakeAccount([]), ok=True)
    assert len(_about_owner(start.mails)) == tried + 1, "an accepted alert was sent again"


def test_a_channels_owner_left_unpinned_mails_the_agent_seat(start):
    """channels.txt: the owner entry, no key contact for it, so the bot writes
    to no 1:1 chat with the owner. callers.txt is already pinned: this mail is
    for channels.txt alone, with its own pin command."""
    _write(start.callers, f"{keygate.MARKER}\n{OWNER}  key:{kf.FPR_K1}  # owner\n")
    _write(start.channels, f"owner  {OWNER}  # owner\n")
    start(FakeAccount([]))
    assert channels.load().entries["owner"].key is None             # the premise: left unpinned
    alerts = _about_owner(start.mails)
    assert len(alerts) == 1, f"want one mail naming {OWNER}, got {start.mails}"
    to, subject, body = alerts[0]
    assert to == bot.dc_env.MAILTO
    text = " ".join((subject + "\n" + body).split())
    assert "sessionphone link" in text, text
    assert channels.PIN_HOW.format(name="owner") in text, text


# ---- the fix's edges -----------------------------------------------------------


def test_both_files_left_unpinned_at_one_start_make_one_mail_naming_both(start):
    """One owner, one lockout: callers.txt and channels.txt both left the owner
    unpinned at the same start. ONE mail, naming each file and its pin command."""
    _write(start.callers, CALLERS_OWNER)
    _write(start.channels, f"owner  {OWNER}  # owner\n")
    start(FakeAccount([]))
    alerts = _about_owner(start.mails)
    assert len(alerts) == 1, f"want one mail naming {OWNER}, got {start.mails}"
    to, subject, body = alerts[0]
    assert to == bot.dc_env.MAILTO
    assert len(subject) <= 69, subject
    text = " ".join((subject + "\n" + body).split())
    assert str(start.callers) in text and str(start.channels) in text, text
    assert keygate.PIN_HOW.format(addr=OWNER) in text, text
    assert channels.PIN_HOW.format(name="owner") in text, text
    assert "sessionphone link" in text, text
    start(FakeAccount([]))
    assert len(_about_owner(start.mails)) == 1, "a later start mailed again"


def test_an_owner_pinned_before_the_retry_is_not_mailed(start):
    """The first send was refused; before the next start the owner's key was
    pinned by hand. The next start re-reads the file and mails nothing."""
    _write(start.callers, CALLERS_OWNER)
    _pinned_channels(start)
    start(FakeAccount([]), ok=False)
    tried = len(start.mails)
    keygate.pin_entry(start.callers, OWNER, kf.FPR_K1)
    start(FakeAccount([]), ok=True)
    assert _about_owner(start.mails[tried:]) == [], start.mails[tried:]
    assert any("pinned now" in s for s in start.said), start.said


def test_the_alert_record_is_private_and_never_an_outbox_job(start):
    """The pending alert lives in the outbox, 0600, and not as *.json: the
    outbox loop takes every *.json as a job."""
    _write(start.callers, CALLERS_OWNER)
    _pinned_channels(start)
    start(FakeAccount([]), ok=False)
    rec = bot.OUTBOX / bot.ownerkey.RECORD
    assert rec.is_file()
    assert rec.stat().st_mode & 0o777 == 0o600
    assert not rec.name.endswith(".json")


def test_a_non_owner_line_left_unpinned_mails_nothing(start):
    """Only the owner line is pinned unattended; any other line is left unpinned
    by design and logged as before. No alert for it."""
    _write(start.callers, f"{CALLERS_OWNER}friend@example.org  # a friend\n")
    _pinned_channels(start)
    start(FakeAccount([kf.Contact(10, OWNER, kf.K1)]))
    assert keygate.read_caller_entries(start.callers)[0].key == kf.FPR_K1
    assert any("UNPINNED friend@example.org" in s for s in start.said), start.said
    assert [m for m in start.mails if "friend@example.org" in m[1] + m[2]] == [], start.mails


def test_the_relink_lines_name_the_bots_own_account_and_files(start):
    """`sessionphone link` and a pin without --key open the account. The shell
    they are run from may name another profile or root, so each re-link line
    names this bot's account folder (and link its two list files)."""
    _write(start.callers, CALLERS_OWNER)
    _write(start.channels, f"owner  {OWNER}  # owner\n")
    start(FakeAccount([]))
    (_to, _subject, body), = _about_owner(start.mails)
    acc = f"DC_ACCOUNTS={shlex.quote(str(bot.ACCOUNTS))} "
    lines = [ln.strip() for ln in body.splitlines()]
    link = next(ln for ln in lines if ln.endswith("sessionphone link"))
    assert link.startswith(acc), link
    assert f"DC_CALLERS_FILE={shlex.quote(str(start.callers))} " in link, link
    assert f"DC_CHANNELS_FILE={shlex.quote(str(start.channels))} " in link, link
    bare = [ln for ln in lines if ln.startswith("DC_") and " pin " in ln and "--key" not in ln]
    assert len(bare) == 2 and all(ln.startswith(acc) for ln in bare), bare
    hand = [ln for ln in lines if ln.startswith("DC_") and " pin " in ln and "--key FPR" in ln]
    assert len(hand) == 2 and not any("DC_ACCOUNTS" in ln for ln in hand), hand


def test_relink_lines_quote_a_path_with_a_space():
    items = [{"file": "callers", "path": "/a b/callers.txt", "addr": OWNER, "name": None}]
    got = bot.ownerkey.relink_lines(items, accounts="/a b/accounts")
    assert got[0] == "DC_ACCOUNTS='/a b/accounts' sessionphone link"
    assert got[1] == (f"DC_ACCOUNTS='/a b/accounts' DC_CALLERS_FILE='/a b/callers.txt' "
                      f"sessionphone callers pin {OWNER}")


def test_a_record_that_cannot_be_written_still_mails_at_this_start(start, monkeypatch):
    """The outbox will not take the record (OSError). The migration runs only
    once, so this start is the one chance: it logs the failure and mails all
    the same. A write failure is never silence."""
    def refuse(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(bot.ownerkey.listalert, "write_record", refuse)
    _write(start.callers, CALLERS_OWNER)
    _pinned_channels(start)
    start(FakeAccount([]))
    alerts = _about_owner(start.mails)
    assert len(alerts) == 1, start.mails
    assert keygate.PIN_HOW.format(addr=OWNER) in alerts[0][2]
    assert any("record not written" in s and "Read-only" in s for s in start.said), start.said
    assert any("OWNER KEY ALERT mailed to" in s for s in start.said), start.said
    assert not (bot.OUTBOX / bot.ownerkey.RECORD).exists()


@pytest.mark.parametrize("items", [[{"file": "callers"}], 5, "garbage",
                                   [{"file": "channels", "path": "/x", "addr": None,
                                     "name": "owner", "line": "x"}]])
def test_a_damaged_record_never_stops_the_start(start, items):
    """A record that is JSON but not the shape the bot writes (hand-edited,
    damaged): the start runs on through its loop, logs it, and mails nothing."""
    _write(start.callers, f"{keygate.MARKER}\n{OWNER}  key:{kf.FPR_K1}  # owner\n")
    _pinned_channels(start)
    bot.OUTBOX.mkdir(parents=True, exist_ok=True)
    (bot.OUTBOX / bot.ownerkey.RECORD).write_text(json.dumps({"ok": False, "items": items}))
    start(FakeAccount([]))      # raises Passed only once the start reached its loop
    assert _about_owner(start.mails) == [], start.mails
    assert any("names no owner line it can read" in s for s in start.said), start.said

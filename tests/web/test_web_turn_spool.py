"""PHONE-256 fix-002: the web server's own spool, its drain loop, and the line a lost turn is told by.

findings/rca-001.md, "the design for fix-002". A refused web turn is kept in
CallDesk.kept, a bridge/spool.Spool under <webdir>/undelivered/ (never the
bot's dir), resent From the WEB seat (turns.resender), drained by a loop on the
server's loop (CallDesk._drain_loop) between start() and stop(). The caller
hears calls.UNDELIVERED_LINE once per lost turn, by `mouth say --device call:N`.

Fakes only: conftest's fake `message` (DC_MESSAGE), a fake `mouth` on PATH, a
fake clock and an injected sleep (never a real wait for a retry), fake leg
servers (no sockets), a fake call object (no aiortc peer). Dates: 2030-06-09.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import types
from pathlib import Path

import pytest

from sessionphone.bridge import earleg, mouthleg, seatmail, spool
from sessionphone.bridge.calls import UNDELIVERED_LINE
from sessionphone.web import call as webcall
from sessionphone.web import turns
from sessionphone.web.call import CallDesk
from sessionphone.web.turns import seat_for

N = 1907204400                      # 14:00 Sun 2030-06-09 AEDT, the second the call began
HOST = "example.net"
IDS = "host"
SEAT = seat_for(f"alice@{HOST}", HOST, environ={})
T0 = 1907204400.0
AEDT = datetime.timezone(datetime.timedelta(hours=11))
DAY = "heard_2030-06-09.jsonl"


def mid(seq: int) -> str:
    return f"call-turn-web-{N}-{seq}@{IDS}"


def refused(kind, sent="no"):
    rc = 3 if kind in ("mta", "undeliverable") else 2
    return rc, {"sent": sent, "kind": kind, "message": f"refused: {kind}", "notices": []}


MTA = refused("mta")
OK = (0, {"sent": "yes", "notices": []})


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, AEDT).isoformat(timespec="milliseconds")


def turn(seq, text, *, t=T0 + 5):
    return {"ts": iso(t), "seq": seq, "kind": "turn", "text": text, "source": "call", "device": f"call:{N}",
            "session": f"call-ears-{N}", "agent": "ears", "final": True, "v": 1}


def write(d: Path, *recs):
    with (d / DAY).open("ab") as f:
        for r in recs:
            f.write(json.dumps(r).encode() + b"\n")


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def id_host(monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_HOST", IDS)


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """The leg servers, faked: start() binds nothing (a tmp path can pass sun_path)."""
    async def serve(manager, path=None, log=print):
        class S:
            def close(self):
                pass

            async def wait_closed(self):
                pass
        return S()
    monkeypatch.setattr(earleg, "serve", serve)
    monkeypatch.setattr(mouthleg, "serve", serve)


@pytest.fixture
def heard(tmp_path):
    d = tmp_path / "conversations"
    d.mkdir()
    write(d, turn(740, "an older turn, before the call"))
    return d


def fake_mouth(tmp_path, monkeypatch, rc=0, err="", hang=0.0):
    """A fake `mouth` first on PATH: each run's argv is one line in mouth.log; it
    waits HANG s (a mouth that gives no answer), then exits RC."""
    log = tmp_path / "mouth.log"
    exe = tmp_path / "mouth-bin" / "mouth"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text("#!/usr/bin/env python3\nimport json, sys, time\n"
                   f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                   f"time.sleep({hang!r}); sys.stderr.write({err!r}); sys.exit({rc})\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def mouth(tmp_path, monkeypatch):
    return fake_mouth(tmp_path, monkeypatch)


def no_mouth(monkeypatch):
    """No `mouth` on PATH, and nothing else changed. shutil is ONE module: a which()
    that answers None for every name, or an empty PATH, would also blind seatmail's
    lookup of the fake `message` (its `#!/usr/bin/env python3` too), so every send
    would fail kind exec and the test would be about something else."""
    real = webcall.shutil.which
    monkeypatch.setattr(webcall.shutil, "which",
                        lambda name, *a, **k: None if name == "mouth" else real(name, *a, **k))


def desk_for(tmp_path, heard, logs, **kw):
    return CallDesk(tmp_path / "web", legs=True, seat=SEAT, heard=heard, voicemode=None, spool=None,
                    log=logs.append, caller="owner", **kw)


def live_call(desk, *, live=True):
    """A fake call N, registered as on_call does, with its follower as the desk makes it."""
    c = types.SimpleNamespace(n=N, echo=None, pc=object() if live else None, turns=None)
    desk.registry.sessions[N] = c
    c.turns = desk.make_follower(c)
    c.turns.sleep = _no_sleep
    return c


async def _no_sleep(dt):
    await asyncio.sleep(0)


async def pump(f, *, settle=0.4):
    """One poll of the follower, then let any `mouth say` it started land."""
    for rec, new in f.read():
        await f.take(rec, new)
    end = asyncio.get_running_loop().time() + settle
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.02)


def go(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def said(mouth):
    return [a for a in mouth() if a[:3] == ["say", "--device", f"call:{N}"]]


# ---- where it is kept: the web's own dir, never the bot's ------------------------------------

def test_the_web_spool_is_under_the_web_dir_whatever_the_shell_carries(tmp_path, heard, fake_message,
                                                                        monkeypatch, undelivered_in_tmp, mouth):
    """A shell with the bot's DC_UNDELIVERED / DC_ROOT never moves the web spool:
    the root is passed explicitly (never spool.directory())."""
    bot_spool = tmp_path / "bot-undelivered"
    monkeypatch.setenv("DC_UNDELIVERED", str(bot_spool))
    monkeypatch.setenv("DC_ROOT", str(tmp_path / "bot-root"))
    logs = []
    desk = desk_for(tmp_path, heard, logs)
    assert desk.undelivered_dir() == tmp_path / "web" / "undelivered"
    assert desk.kept.root == desk.undelivered_dir()
    fake_message.answer(MTA)

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "kept where?"))
        await pump(c.turns)
    go(main())
    assert desk.kept.counts() == (1, 0)
    assert not bot_spool.exists() and list(undelivered_in_tmp.iterdir()) == []
    assert not (tmp_path / "bot-root").exists()


def test_echo_mode_builds_no_spool_no_drain_and_mails_nothing(tmp_path, heard):
    desk = CallDesk(tmp_path / "web", legs=False, seat=SEAT, heard=heard, voicemode=None, spool=None,
                    log=lambda m: None)
    assert desk.kept is None
    c = types.SimpleNamespace(n=N, echo=None, pc=object())
    assert desk.make_follower(c) is None

    async def main():
        await desk.start()
        assert desk.drainer is None
        await desk.stop()
    go(main())
    assert not (tmp_path / "web" / "undelivered").exists()


def test_legs_with_no_seat_builds_no_spool(tmp_path, heard):
    desk = CallDesk(tmp_path / "web", legs=True, heard=heard, voicemode=None, spool=None, log=lambda m: None)
    assert desk.kept is None


# ---- who drains it: the desk's own loop, on a fake clock, From the web seat ------------------

def test_a_kept_web_turn_is_retried_at_plus_5s_from_the_web_seat_never_the_bots(tmp_path, heard, fake_message,
                                                                                  monkeypatch, mouth):
    clock, asked, logs = Clock(), [], []
    monkeypatch.setenv("TM_AGENT_ADDRESS", "bot-seat")        # the shell's own seat: never the From
    monkeypatch.setenv("EMAIL", "bot-seat")

    def delivered(msgid, to, since=None):
        asked.append((msgid, to, clock.now))
        return False
    monkeypatch.setattr(seatmail, "delivered", delivered)

    async def sleep(dt):
        clock.now += dt                                      # the fake clock: no real wait
        await asyncio.sleep(0.01)

    desk = desk_for(tmp_path, heard, logs, clock=clock, sleep=sleep)
    fake_message.answer(MTA)                                 # the first try refused; the retry taken

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "retry me"))
        await pump(c.turns, settle=0.05)
        assert desk.kept.counts() == (1, 0)
        await desk.start()
        for _ in range(500):
            if desk.kept.counts() == (0, 0):
                break
            await asyncio.sleep(0.01)
        desk.registry.sessions.pop(N)                       # the fake call has no close()
        await desk.stop()
    go(main())
    assert desk.kept.counts() == (0, 0)
    assert asked == [(mid(741), SEAT.to, T0 + spool.CALL_TURN_FIRST_S)]   # +5 s, not +60 s
    first, retry = fake_message.calls()
    assert spool.message_id(retry["argv"]) == mid(741) == spool.message_id(first["argv"])
    for c in (first, retry):
        assert c["env"]["TM_AGENT_ADDRESS"] == c["env"]["EMAIL"] == "alice-chat"
        assert c["env"]["SESSIONMAIL_FROM_NAME"] == "Alice's web call"
    assert any(m.startswith("BRIDGE MAIL SENT on try 2") for m in logs if isinstance(m, str))


def test_the_resender_goes_from_the_seat_it_is_given(fake_message):
    send = turns.resender(SEAT)
    s = send(seatmail.args(SEAT.to, "s", msgid=mid(1)), "body")
    assert s.ok
    env = fake_message.env()
    assert env["TM_AGENT_ADDRESS"] == "alice-chat" and env["SESSIONMAIL_FROM_NAME"] == "Alice's web call"
    assert turns.sender(SEAT) == ("alice-chat", "Alice's web call")
    m = turns.turn_mail(N, 1, "hi", SEAT, "owner")
    assert (m.seat, m.from_name) == turns.sender(SEAT)


def test_stop_cancels_the_drain_loop(tmp_path, heard):
    desk = desk_for(tmp_path, heard, [])

    async def main():
        await desk.start()
        t = desk.drainer
        assert t is not None and not t.done()
        await desk.stop()
        assert desk.drainer is None and t.cancelled()
    go(main())


def test_a_drain_that_raises_is_logged_and_the_loop_goes_on(tmp_path, heard):
    logs, passes = [], []

    async def sleep(dt):
        await asyncio.sleep(0)

    desk = desk_for(tmp_path, heard, logs, sleep=sleep)

    def drain():
        passes.append(1)
        raise OSError("disk gone")
    desk.kept.drain = drain

    async def main():
        await desk.start()
        for _ in range(200):
            if len(passes) >= 2:
                break
            await asyncio.sleep(0.01)
        await desk.stop()
    go(main())
    assert len(passes) >= 2
    assert any("the drain failed: OSError: disk gone" in m for m in logs if isinstance(m, str))


def test_the_start_line_counts_the_web_spool(tmp_path, heard, fake_message):
    desk = desk_for(tmp_path, heard, [])
    *_, line = turns.start_lines(SEAT, heard, environ={}, kept=desk.kept)
    assert line == f"turns kept: 0 waiting, 0 dead ({tmp_path / 'web' / 'undelivered'})"
    fake_message.answer(MTA)

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "count me"))
        await pump(c.turns, settle=0.05)
    go(main())
    *_, line = turns.start_lines(SEAT, heard, environ={}, kept=desk.kept)
    assert line.startswith("turns kept: 1 waiting, 0 dead (")
    assert len(turns.start_lines(SEAT, heard, environ={})) == 2          # no spool given: no line


# ---- the tell: one line per lost turn, by the mouth -------------------------------------------

def test_a_lost_turn_is_said_once_by_the_mouth_and_logged(tmp_path, heard, fake_message, mouth):
    logs = []
    desk = desk_for(tmp_path, heard, logs)
    fake_message.answer(MTA)

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "lost"))
        await pump(c.turns)
    go(main())
    assert said(mouth) == [["say", "--device", f"call:{N}", UNDELIVERED_LINE]]
    assert f"UNDELIVERED call:{N} seq 741: said on the call" in logs


def test_two_lost_turns_are_two_lines_and_lost_mailed_lost_is_two(tmp_path, heard, fake_message, mouth):
    """Once per lost TURN, never per streak (what PHONE-249 shipped; the README's words)."""
    desk = desk_for(tmp_path, heard, [])
    fake_message.answer(MTA, MTA, MTA, OK, MTA)

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "one"), turn(742, "two"))
        await pump(c.turns)
        assert len(said(mouth)) == 2
        write(heard, turn(743, "three"), turn(744, "four"), turn(745, "five"))
        await pump(c.turns)
        return c.turns
    f = go(main())
    assert f.mailed == [mid(744)]
    assert len(said(mouth)) == 4
    assert desk.kept.counts() == (4, 0)


def test_a_closed_call_says_nothing_and_the_turn_is_still_kept(tmp_path, heard, fake_message, mouth):
    logs = []
    desk = desk_for(tmp_path, heard, logs)
    fake_message.answer(MTA, MTA)

    async def main():
        c = live_call(desk, live=False)                     # no peer: the call is over
        write(heard, turn(741, "after the end"))
        await pump(c.turns)
        desk.registry.sessions.pop(N)                       # WebCall.close pops it
        write(heard, turn(742, "after the pop"))
        await pump(c.turns)
    go(main())
    assert said(mouth) == []
    assert desk.kept.counts() == (2, 0)
    assert f"UNDELIVERED call:{N} seq 741: not said: the call has ended" in logs
    assert f"UNDELIVERED call:{N} seq 742: not said: the call has ended" in logs


def test_no_mouth_on_path_logs_not_said_and_keeps_the_turn(tmp_path, heard, fake_message, monkeypatch):
    no_mouth(monkeypatch)
    logs = []
    desk = desk_for(tmp_path, heard, logs)
    fake_message.answer(MTA)

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "nobody to say it"))
        await pump(c.turns, settle=0.05)
    go(main())
    assert f"UNDELIVERED call:{N} seq 741: not said: no mouth on PATH" in logs
    assert desk.kept.counts() == (1, 0)


def test_a_mouth_that_fails_is_logged_with_its_first_line(tmp_path, heard, fake_message, monkeypatch):
    mouth = fake_mouth(tmp_path, monkeypatch, rc=3, err="no call:N here\nmore\n")
    logs = []
    desk = desk_for(tmp_path, heard, logs)
    fake_message.answer(MTA)

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "x"))
        await pump(c.turns)
    go(main())
    assert len(said(mouth)) == 1
    assert f"UNDELIVERED call:{N} seq 741: not said: mouth exited 3: no call:N here" in logs


@pytest.mark.parametrize("road", ["missing", "exits-3", "times-out"])
def test_a_mouth_that_cannot_say_it_loses_only_the_line_never_the_turn(tmp_path, heard, fake_message,
                                                                       monkeypatch, road):
    """The super's MUST-HOLD (13:54): phone never needs voice installed. With no
    mouth on PATH, a mouth that exits non-zero, or one that gives no answer, the
    turn is STILL kept ONCE (one record, one try in it, no second spool entry)
    and retried; only the spoken line is lost, logged ONCE; nothing crashes (the
    follower mails the next turn)."""
    if road == "missing":
        no_mouth(monkeypatch)
        why = "not said: no mouth on PATH"
    elif road == "exits-3":
        fake_mouth(tmp_path, monkeypatch, rc=3, err="no such call\n")
        why = "not said: mouth exited 3: no such call"
    else:
        fake_mouth(tmp_path, monkeypatch, hang=30.0)
        monkeypatch.setattr(webcall, "MOUTH_TIMEOUT_S", 0.2)   # the fake mouth is killed, not waited out
        why = "not said: mouth gave no answer in 0 s"
    clock, logs = Clock(), []
    monkeypatch.setattr(seatmail, "delivered", lambda msgid, to, since=None: False)

    async def sleep(dt):
        clock.now += dt                                      # the fake clock: no real wait for the retry
        await asyncio.sleep(0.01)

    desk = desk_for(tmp_path, heard, logs, clock=clock, sleep=sleep)
    fake_message.answer(MTA)                                 # 741 refused; 742 and the retry taken

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "lost, and nobody can say so"))
        await pump(c.turns, settle=1.0)
        write(heard, turn(742, "the next turn still goes"))
        await pump(c.turns, settle=0.05)
        kept = sorted(desk.undelivered_dir().glob("*.json"))
        assert desk.kept.counts() == (1, 0) and len(kept) == 1
        rec = json.loads(kept[0].read_text())
        assert spool.message_id(rec["args"]) == mid(741) and len(rec["tries"]) == 1
        assert c.turns.mailed == [mid(742)]
        await desk.start()
        for _ in range(500):
            if desk.kept.counts() == (0, 0):
                break
            await asyncio.sleep(0.01)
        desk.registry.sessions.pop(N)
        await desk.stop()
    go(main())
    assert desk.kept.counts() == (0, 0)                      # retried, and gone from the spool
    ids = [spool.message_id(c["argv"]) for c in fake_message.calls()]
    assert ids == [mid(741), mid(742), mid(741)]             # one try, the next turn, ONE retry
    told = [m for m in logs if isinstance(m, str) and m.startswith(f"UNDELIVERED call:{N} ")]
    assert told == [f"UNDELIVERED call:{N} seq 741: {why}"]  # the line lost, logged once
    assert sum("BRIDGE MAIL FAILED" in m for m in logs if isinstance(m, str)) == 1


def test_a_mailed_turn_is_neither_kept_nor_said(tmp_path, heard, fake_message, mouth):
    desk = desk_for(tmp_path, heard, [])

    async def main():
        c = live_call(desk)
        write(heard, turn(741, "it goes"))
        await pump(c.turns, settle=0.1)
        return c.turns
    f = go(main())
    assert f.mailed == [mid(741)] and said(mouth) == [] and desk.kept.counts() == (0, 0)

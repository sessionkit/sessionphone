"""PHONE-256: a refused WEB call turn is kept, retried without a duplicate, and told.

repro-001 (a) pinned three defects as strict xfails; fix-002 built them
(findings/rca-001.md, "the design for fix-002"), and the markers are gone:

(a1) KEEP: TurnFollower.mail makes ONE try; any refusal (sent=no, sent=unknown)
     is kept at once in the web server's OWN spool, <webdir>/undelivered/
     (CallDesk.undelivered_dir()), under the turn's own Message-ID.
(a2) DEDUPE: the kept turn's retry asks the seat's box first (Spool._not_there,
     seatmail.delivered): there, nothing is sent; not there, sent once more.
(a3) TELL: the caller hears ONE line per lost turn, by the mouth
     (`mouth say --device call:N`, CallDesk._say_undelivered).

The seams are narrowed to what was built: a kept record is looked for under
<tmp_path>/web/undelivered only, and what the caller hears is counted at a fake
`mouth` on PATH only (the web process has no TTS of its own).

Fakes only: conftest's fake `message` (DC_MESSAGE), a scratch heard dir with the
two test dates, a fake call object (no aiortc peer), a fake `mouth`. No network,
no real ~/.sessionphone (PHONE-264's guard), no real mail.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import types
from pathlib import Path

import pytest

from sessionphone.bridge import spool
from sessionphone.web.call import CallDesk
from sessionphone.web.turns import seat_for

N = 1907204400                      # a web call's id: the second it began, 14:00 Sun 2030-06-09 AEDT
HOST = "example.net"
IDS = "host"                        # seatmail.id_host(): the ids' host
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
# every road a web turn is refused by: sent=no (a WORLD kind), and sent=unknown
# (it may have gone). Each is ONE try, then kept: the spool makes every retry.
LOST = {
    "sent-no": [MTA],
    "sent-unknown-mta": [refused("mta", sent="unknown")],
    "sent-unknown-undeliverable": [refused("undeliverable", sent="unknown")],
}


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, AEDT).isoformat(timespec="milliseconds")


def turn(seq, text, *, t=T0 + 5):
    """A finished caller turn of call N, in the ears' v1 heard-log shape."""
    return {"ts": iso(t), "seq": seq, "kind": "turn", "text": text, "source": "call", "device": f"call:{N}",
            "session": f"call-ears-{N}", "agent": "ears", "final": True, "v": 1}


def write(d: Path, *recs):
    with (d / DAY).open("ab") as f:
        for r in recs:
            f.write(json.dumps(r).encode() + b"\n")


@pytest.fixture(autouse=True)
def id_host(monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_HOST", IDS)


@pytest.fixture
def heard(tmp_path):
    d = tmp_path / "conversations"
    d.mkdir()
    write(d, turn(740, "an older turn, before the call"))
    return d


class SpyVoice:
    """WebCall.voice's duck type (the mouth leg's say/mix/cut): records what reaches the call."""

    def __init__(self):
        self.heard = []

    def say(self, pcm, *a, **k):
        self.heard.append(("say", pcm))

    def mix(self, pcm, *a, **k):
        self.heard.append(("mix", pcm))

    def cut(self):
        pass

    def drop(self):
        pass


@pytest.fixture
def mouth(tmp_path, monkeypatch):
    """A fake `mouth` first on PATH: each run's argv is one line in mouth.log."""
    log = tmp_path / "mouth.log"
    exe = tmp_path / "mouth-bin" / "mouth"
    exe.parent.mkdir()
    exe.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                   f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def rig(tmp_path, heard, mouth):
    """A --legs desk with a seat, ONE live fake call N on it, and that call's follower
    as the desk makes it (desk.make_follower). rig.heard_lines() counts what the
    caller could hear: each `mouth say` for call:N (the web's only voice)."""
    logs = []
    desk = CallDesk(tmp_path / "web", legs=True, seat=SEAT, heard=heard, voicemode=None, spool=None,
                    log=logs.append, caller="owner")
    call = types.SimpleNamespace(n=N, echo=None, voice=SpyVoice(), pc=object(), closed=False, turns=None)
    desk.registry.sessions[N] = call
    call.turns = f = desk.make_follower(call)
    f.sleep = _no_sleep

    def heard_lines():
        return len([a for a in mouth() if "say" in a and f"call:{N}" in " ".join(a)])
    return types.SimpleNamespace(desk=desk, call=call, f=f, logs=logs, heard_lines=heard_lines, mouth=mouth)


async def _no_sleep(dt):
    await asyncio.sleep(0)


def pump(rig, *, settle=0.3):
    """One poll of the follower, as run() does; then let any line it starts land."""
    async def main():
        for rec, new in rig.f.read():
            await rig.f.take(rec, new)
        end = asyncio.get_running_loop().time() + settle
        while asyncio.get_running_loop().time() < end:
            await asyncio.sleep(0.02)
    asyncio.run(asyncio.wait_for(main(), 20))


def kept_records(*roots: Path) -> list[tuple[Path, dict]]:
    """Every spool-shaped record (spool.py: `args` a list, `body` a string) under ROOTS.
    conftest's fake `message` writes `argv`, never `args`: its run logs never match."""
    out, seen = [], set()
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for p in Path(root).rglob("*.json"):
            if p in seen:
                continue
            seen.add(p)
            try:
                rec = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and isinstance(rec.get("args"), list) and isinstance(rec.get("body"), str):
                out.append((p, rec))
    return out


@pytest.fixture
def kept(tmp_path):
    """-> the kept records carrying Message-ID MSGID, in the web spool <tmp_path>/web/undelivered."""
    def find(msgid):
        return [(p, r) for p, r in kept_records(tmp_path / "web" / "undelivered")
                if spool.message_id(r["args"]) == msgid]
    return find


# ---- (a1) KEEP: a refused web turn is kept, under its own Message-ID, once -------------

@pytest.mark.parametrize("replies", LOST.values(), ids=LOST.keys())
def test_a_refused_web_turn_is_kept_with_its_message_id(rig, fake_message, kept, replies):
    fake_message.answer(*replies)
    write(rig.f.dir, turn(741, "can you check the build"))
    pump(rig)
    assert rig.f.mailed == []                                     # it did not go: the precondition
    found = kept(mid(741))
    assert len(found) == 1, f"no kept record for <{mid(741)}>: the turn is lost ({rig.logs})"
    (_, rec), = found
    assert spool.target(rec["args"]) == SEAT.to                    # the retry's dedupe reads this box
    assert "can you check the build" in rec["body"]


# ---- (a2) DEDUPE: the kept turn's retry never lands twice --------------------------------

@pytest.mark.parametrize("there", [True, False, None], ids=["in-the-box", "not-in-the-box", "box-uncheckable"])
def test_a_kept_web_turn_is_retried_without_a_duplicate(rig, fake_message, kept, there):
    """sent=unknown may have gone: its retry asks the box (Spool._not_there) first.
    There: removed, nothing sent. Not there: sent once more under the SAME id.
    The box cannot be checked (None): buried in dead/, never sent twice."""
    fake_message.answer(refused("mta", sent="unknown"))
    write(rig.f.dir, turn(741, "did it go?"))
    pump(rig)
    found = kept(mid(741))
    assert len(found) == 1, f"no kept record for <{mid(741)}>: nothing to retry ({rig.logs})"
    (path, rec), = found
    sends, asked, logs = [], [], []

    def send(args, body):
        sends.append(args)
        from sessionphone.bridge.seatmail import Sent
        return Sent(True, sent="yes", args=args)

    def delivered(msgid, to, since=None):
        asked.append((msgid, to))
        return there
    later = float(rec.get("next_try", 0)) + spool.CAP_S / 2
    spool.Spool(logs.append, root=path.parent, clock=lambda: later, send=send, delivered=delivered).drain()
    assert asked == [(mid(741), SEAT.to)]
    if there is None:
        assert sends == [] and not path.exists()
        (dead,) = (path.parent / spool.DEAD).glob("*.json")
        assert "may have gone (sent=unknown)" in json.loads(dead.read_text())["dead_why"]
        assert any(m.startswith("BRIDGE MAIL DEAD ") and "never sent twice" in m for m in logs)
    elif there:
        assert sends == [] and not path.exists()
    else:
        assert len(sends) == 1 and spool.message_id(sends[0]) == mid(741) and not path.exists()


# ---- (a3) TELL: the caller hears one line per lost turn -----------------------------------

@pytest.mark.parametrize("replies", LOST.values(), ids=LOST.keys())
def test_the_caller_hears_one_line_for_a_lost_web_turn(rig, fake_message, replies):
    fake_message.answer(*replies)
    write(rig.f.dir, turn(741, "can you check the build"))
    pump(rig)
    assert rig.f.mailed == []
    assert rig.heard_lines() == 1, f"the caller heard {rig.heard_lines()} lines for a lost turn ({rig.logs})"


def test_each_lost_web_turn_is_said_once(rig, fake_message):
    fake_message.answer(MTA, MTA)
    write(rig.f.dir, turn(741, "one"), turn(742, "two"))
    pump(rig)
    assert rig.f.mailed == []
    assert rig.heard_lines() == 2, f"the caller heard {rig.heard_lines()} lines for two lost turns"


# ---- guards: true today, and must stay true ---------------------------------------------

def test_a_mailed_web_turn_is_neither_kept_nor_said(rig, fake_message, kept):
    write(rig.f.dir, turn(741, "can you hear me"))
    pump(rig, settle=0.1)
    assert rig.f.mailed == [mid(741)]
    assert kept(mid(741)) == [] and rig.heard_lines() == 0


# ---- the instruments: what the tests above look with can see a kept turn and a said line ----

def test_the_kept_search_finds_a_spool_record_and_ignores_the_fake_messages_logs(fake_message, kept, tmp_path):
    from sessionphone.bridge import seatmail
    fake_message.answer(refused("mta", sent="unknown"))
    sent = seatmail.send(SEAT.to, "s", "body of it", msgid=mid(9), seat="alice-chat")
    assert not sent.ok and fake_message.calls()                   # the fake logged a run (argv, not args)
    assert kept(mid(9)) == []
    web_spool = tmp_path / "web" / "undelivered"
    spool.Spool(lambda m: None, root=web_spool).keep(sent, "body of it")
    (p, rec), = kept(mid(9))
    assert p.parent == web_spool and spool.target(rec["args"]) == SEAT.to


def test_the_ear_counts_a_mouth_say_for_this_call_only(rig):
    import subprocess
    subprocess.run(["mouth", "say", "--device", f"call:{N}", "hi"], check=True)
    subprocess.run(["mouth", "say", "--device", "call:7", "not this call"], check=True)
    assert rig.heard_lines() == 1

"""PHONE-158, the web leg: TurnFollower mails no heard-log turn with no real word.

ears writes a `turn` for any non-empty text after strip_annotations, so a bare
"Music." or "." reaches the heard log as a turn. The follower drops it, moves
its high-water mark past it (decided, never re-read), and logs one NOISE line
naming the call, the seq and the text, shaped like the ECHO drop.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sessionphone.bridge.seatmail import Sent
from sessionphone.web.turns import TurnFollower, seat_for

N = 1918213200
HOST = "example.net"
IDS = "host"                  # seatmail.id_host(): the ids' host (PHONE-72 part 2b)
SEAT = seat_for(f"alice@{HOST}", HOST, environ={})
DAY = "heard_2030-12-08.jsonl"


def turn(seq, text):
    return {"ts": "2030-12-08T23:34:56.000+11:00", "seq": seq, "kind": "turn", "text": text,
            "source": "call", "device": f"call:{N}", "agent": "ears", "final": True, "v": 1}


def write(d: Path, *recs):
    with (d / DAY).open("a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


async def _no_sleep(dt):
    await asyncio.sleep(0)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_HOST", IDS)
    d = tmp_path / "conversations"
    d.mkdir()
    logs, sent = [], []

    def send(mail):
        sent.append(mail)
        return Sent(True, sent="yes")
    f = TurnFollower(N, SEAT, d, caller="Ada", log=logs.append, send=send, sleep=_no_sleep)

    def pump():
        async def run():
            for rec, new in f.read():
                await f.take(rec, new)
        asyncio.run(asyncio.wait_for(run(), 20))
    return d, f, logs, sent, pump


def test_the_measured_no_word_turns_are_dropped_and_logged(rig):
    d, f, logs, sent, pump = rig
    write(d, turn(1, "."), turn(2, "Music."), turn(3, "Um"), turn(4, "Uh."))
    pump()
    assert sent == []
    assert [m for m in logs if m.startswith("NOISE ")] == [
        f"NOISE call:{N} seq 1: no real word | dropped: .",
        f"NOISE call:{N} seq 2: no real word | dropped: Music.",
        f"NOISE call:{N} seq 3: no real word | dropped: Um",
        f"NOISE call:{N} seq 4: no real word | dropped: Uh.",
    ]
    assert f.last_seq == 4 and f.mailed == []             # decided: never read again


@pytest.mark.parametrize("text", ["yes", "no", "go", "okay", "Hmm, yes"])
def test_a_one_word_answer_is_still_mailed(rig, text):
    d, f, logs, sent, pump = rig
    write(d, turn(7, text))
    pump()
    (m,) = sent
    assert m.body.startswith(f"(on the call) {text}")
    assert not any(l.startswith("NOISE ") for l in logs)
    assert f.mailed == [f"call-turn-web-{N}-7@{IDS}"]


def test_a_drop_between_turns_leaves_the_rest_mailed_in_order(rig):
    d, f, logs, sent, pump = rig
    write(d, turn(10, "can you check the build"), turn(11, "."), turn(12, "go"))
    pump()
    assert [m.body.split("\n", 1)[0] for m in sent] == [
        "(on the call) can you check the build", "(on the call) go"]
    assert f.mailed == [f"call-turn-web-{N}-10@{IDS}", f"call-turn-web-{N}-12@{IDS}"]
    pump()                                                 # a re-read mails nothing more
    assert len(sent) == 2 and sum(l.startswith("NOISE ") for l in logs) == 1

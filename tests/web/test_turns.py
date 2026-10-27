"""PHONE-138 build-001, build-002: a web call's finished turns, mailed to the seat (design.md §4, §5, §6).

Fakes only: a temporary heard dir, with lines in the exact v1 shape the ears
write (design §2); conftest's recording fake `message`
through DC_MESSAGE (PHONE-72 part 2b: a turn goes signed, by `message send
--message-id`); the call's own EchoGuard with no spool and no VoiceMode tail.
No real sendmail, message, keeper, mouth or browser.

T1-T6, T4b and T8's seat and host rules are here; T9 is tests/test_seatmail.py
(the pin) and tests/test_m5_golden.py (the Delta headers). T7 and T8's CLI and
start lines are in tests/web/test_call.py, beside the CallRig they need (build-002).
"""
from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path

import pytest

from sessionphone import settings
from sessionphone.bridge import bot, dc_env, echo, seatmail, spool
from sessionphone.bridge.echo import EchoGuard
from sessionphone.bridge.seatmail import Sent
from sessionphone.web import turns
from sessionphone.web.turns import Seat, TurnFollower, seat_for, turn_mail

N = 1918213200                       # a web call's id: ten digits, the second it began
HOST = "example.net"              # DC_HOST: the Reply-To
ARPA = "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa"   # the IPv6 loopback PTR
SH = {"SESSIONMAIL_HOST": "example"}   # a short host HOST sits under: host_part keeps HOST, asks no mail.json
IDS = "host"                           # seatmail.id_host(), sessionmail's own: the ids (PHONE-72 2b)
SEAT = seat_for(f"alice@{HOST}", HOST, environ={})
CALLER = "Ada"                       # who spoke: web/names.py's setting, handed down (PHONE-135)
T0 = 1918213200.0                    # the call's clock, epoch seconds
AEDT = datetime.timezone(datetime.timedelta(hours=11))
DAY = "heard_2030-06-09.jsonl"
NEXT_DAY = "heard_2030-12-08.jsonl"
SPOKEN = "Shall I commit the changes and push the branch now?"
# measured in the design review (design-001, build note 2)
MIXED = SPOKEN + " No, wait until I have read the whole diff myself."     # 0.68: mailed, echo included
SHORT = SPOKEN + " Yes."                                                  # 0.96: dropped whole


@pytest.fixture(autouse=True)
def id_host(monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_HOST", IDS)


def refused(kind, sent="no", rc=None):
    """A message send refusal, as --json gives it: for fake_message.answer."""
    rc = rc if rc is not None else (3 if kind in ("mta", "undeliverable") else 2)
    return rc, {"sent": sent, "kind": kind, "message": f"refused: {kind}", "notices": []}


def iso(t: float) -> str:
    """A heard-log ts: ISO 8601, milliseconds, with an offset (sv heard.py _now_iso)."""
    return datetime.datetime.fromtimestamp(t, AEDT).isoformat(timespec="milliseconds")


def turn(seq, text, *, n=N, t=T0 + 5, **over):
    """A turn line in the v1 shape (design §2)."""
    rec = {"ts": iso(t), "seq": seq, "kind": "turn", "text": text, "source": "call", "device": f"call:{n}",
           "session": f"call-ears-{n}", "agent": "ears", "final": True, "detector": "silero", "t0": 3.87,
           "t1": 6.07, "text_from": "redecode", "joined": text, "redecode_wait_s": 0.0, "speculative": True, "v": 1}
    rec.update(over)
    return {k: v for k, v in rec.items() if v is not None}


def event(seq, name, *, n=N, t=T0 + 5, **over):
    rec = {"ts": iso(t), "seq": seq, "kind": "event", "event": name, "source": "call", "device": f"call:{n}",
           "session": f"call-ears-{n}", "agent": "ears", "v": 1}
    rec.update(over)
    return rec


def write(d: Path, *recs, day=DAY, tail=b""):
    """Append whole lines (and an optional half line) to a heard day file."""
    with (d / day).open("ab") as f:
        for r in recs:
            f.write(json.dumps(r).encode() + b"\n")
        f.write(tail)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def heard(tmp_path):
    """A heard dir with today's file, holding lines older than the call."""
    d = tmp_path / "conversations"
    d.mkdir()
    write(d, turn(740, "an older turn of this same N, before the call"), event(739, "listen-started"))
    return d


def guard(clock=None):
    return EchoGuard(spool=None, voicemode=None, clock=clock or Clock())


def follower(heard, *, logs=None, **kw):
    kw.setdefault("sleep", _no_sleep)
    kw.setdefault("caller", CALLER)
    return TurnFollower(N, SEAT, heard, log=(logs.append if logs is not None else lambda m: None), **kw)


async def _no_sleep(dt):
    await asyncio.sleep(0)


async def pump(f: TurnFollower):
    """One poll: read what is new and decide each line, as run() does."""
    for rec, new in f.read():
        await f.take(rec, new)


def go(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


# ---- T1 (SC1): one scripted turn -> exactly one mail, header by header -----------------

def test_t1_one_turn_is_exactly_one_mail_to_the_seat(heard, fake_message):
    f = follower(heard)
    write(heard, turn(741, "Hey Alice, can you hear me?"))
    go(pump(f))
    (call,) = fake_message.calls()
    env = call["env"]                                     # From: the web seat, signed, its ids' host
    assert env["EMAIL"] == env["TM_AGENT_ADDRESS"] == "alice-chat"
    assert env["SESSIONMAIL_FROM_NAME"] == "Alice's web call"
    assert env["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1" and env["SESSIONMAIL_HOST"] == IDS
    (m,) = fake_message.mails()
    assert m["To"] == f"alice@{HOST}"
    assert m["Subject"] == f"[web {N}] Ada: Hey Alice, can you hear me?"
    assert m["X-Delta-Chat-Bridge"] == "1" and m["X-Delta-Chat-Kind"] == "call"
    assert m["Reply-To"] == f"calls+web-{N}@{HOST}"
    assert m["Message-ID"] == f"<call-turn-web-{N}-741@{IDS}>"
    assert m["In-Reply-To"] == m["References"] == f"<web-call-{N}@{IDS}>"
    assert m["Medium"] == "voice"
    argv = call["argv"]
    assert argv[argv.index("--message-id") + 1] == f"call-turn-web-{N}-741@{IDS}"
    assert argv[argv.index("-r") + 1] == f"<web-call-{N}@{IDS}>"
    assert {"--raw", "--no-shorten", "--json"} <= set(argv) and argv[argv.index("-f") + 1] == "-"
    # what the pager's built-in `wake to:$me` reads: To the seat, and no Priority
    assert "Priority" not in m
    for absent in ("X-Delta-Chat-Id", "X-Delta-Chat-Profile", "Cc", "Supersedes", "X-Delta-Chat-Sender"):
        assert absent not in m
    body = m.get_content()
    assert body.startswith("(on the call) Hey Alice, can you hear me?\n\n---\n")
    assert f"call: web (call {N})\nfrom: Ada\n" in body
    assert f'  Answer with: mouth say --device call:{N} "text"\n' in body
    assert f.mailed == [f"call-turn-web-{N}-741@{IDS}"] and f.last_seq == 741


def test_turn_mail_is_pure_and_flattens_the_subject():
    text = "run `make test` and $(echo hi)\nthen Ça va " + "x" * 80
    a = turn_mail(N, 7, text, SEAT, CALLER)
    assert a == turn_mail(N, 7, text, SEAT, CALLER)
    assert a.subject == f"[web {N}] Ada: run 'make test' and $ (echo hi) then ?a va xx"
    assert len(a.subject) <= 69 and a.subject.isascii()
    assert "\n" not in a.subject and not any("\n" in v for _, v in a.headers)
    assert [k for k, _ in a.headers] == ["X-Delta-Chat-Bridge", "X-Delta-Chat-Kind", "Reply-To", "Medium"]
    assert (a.msgid, a.thread, a.seat, a.from_name) == (
        f"call-turn-web-{N}-7@{IDS}", f"web-call-{N}@{IDS}", "alice-chat", "Alice's web call")
    assert a.body.startswith("(on the call) run `make test` and $(echo hi) then Ça va ")   # the body keeps the words


def test_the_thread_id_can_never_be_read_as_a_delta_chat():
    """The Delta mail leg's chat fallback (calls.py) must not find a chat in a web turn's ids.

    Since PHONE-155 the fallback is no pattern at all: only a root the bridge
    ISSUED for a chat, matched whole. A web turn's ids are never issued, so even
    with chat N holding a root, they find no chat."""
    from sessionphone.bridge import calls
    src = Path(calls.__file__).read_text()
    assert r"call-(?:[a-z]+-)?(\d+)-\d+@" not in src          # the pattern is gone
    issued = calls.IssuedRoots()
    issued.mint(N, f"call-{N}-1907238896@{HOST}")
    assert issued.chats({turns.thread_id(N, HOST), turns.message_id(N, 5, HOST)}) == set()


def test_seat_for_names_the_seat_not_the_shell():
    """§4.3 From, §11 A8: <agent> is DC_AGENT, else the To's local part cut at +."""
    assert seat_for("alice+call@example.net", HOST, environ={}) == Seat(
        "alice+call@example.net", f"alice-chat@{HOST}", "Alice", HOST)
    e = {"DC_AGENT": "Team", "DC_ADDR": "team-phone@relay.example", "DC_AGENT_NAME": "Team the cat",
         "CLAUDE_CODE_AGENT": "worker"}
    assert seat_for("alice@x.example", HOST, environ=e) == Seat(
        "alice@x.example", "team-phone@relay.example", "Team the cat", HOST)
    assert seat_for("Alice@x.example", HOST, environ={"CLAUDE_CODE_AGENT": "worker"}).addr == f"alice-chat@{HOST}"


def test_heard_dir_resolves_as_ears_mail_does(tmp_path):
    home = Path("~").expanduser()
    assert turns.heard_dir(tmp_path / "x", {"EARS_HEARD_DIR": "/e"}) == tmp_path / "x"
    assert turns.heard_dir(None, {"EARS_HEARD_DIR": "/e", "VOICEMODE_BASE_DIR": "/v"}) == Path("/e")
    assert turns.heard_dir(None, {"VOICEMODE_BASE_DIR": "/v"}) == Path("/v/logs/conversations")
    assert turns.heard_dir(None, {}) == home / ".voicemode" / "logs" / "conversations"


# ---- T2 (SC1): header parity with a Delta call turn -------------------------------------

def test_t2_the_web_turn_carries_the_delta_turns_headers(monkeypatch, fake_message):
    """Breaks on purpose when a header is added to Delta turns and not to web turns.
    Both go by the same `message send`: the argv is compared, header by header and flag by flag."""
    monkeypatch.setattr(dc_env, "ADDR", f"alice-chat@{HOST}")
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Alice")
    monkeypatch.setattr(dc_env, "MAILTO", SEAT.to)
    monkeypatch.setattr(bot, "PROFILE", "public")         # every Delta header there can be
    monkeypatch.setattr(bot, "log", lambda m: None)
    # exactly as CallSession._mail_turn calls it (calls.py)
    assert bot.mail_in("Team", 12, "Bob", "(on the call) push it", msgid=f"call-turn-p-12-1@{IDS}",
                       in_reply_to=f"call-p-12-0@{IDS}", subject="Bob: push it", reply_to=dc_env.ADDR,
                       cc=["guest@relay.example"], supersedes=f"prev-1@{IDS}", headers={"Medium": "voice"},
                       call=4242) is True
    assert turn_mail(N, 9, "push it", SEAT, CALLER).send().ok
    delta, web = fake_message.mails()
    gone = {"X-Delta-Chat-Id", "X-Delta-Chat-Profile", "Cc", "Supersedes"}
    assert gone <= set(delta.keys())
    assert set(web.keys()) == set(delta.keys()) - gone
    for h in ("To", "X-Delta-Chat-Kind", "X-Delta-Chat-Bridge", "Medium"):
        assert web[h] == delta[h], h
    # the order the headers go out in is the Delta order, less the four
    assert list(web.keys()) == [k for k in delta if k not in gone]
    # and the same signer and must-sign; the seat and the display name are each one's own
    d, w = (c["env"] for c in fake_message.calls())
    for k in ("CLAUDE_SESSION_AGENT_SOCK", "CLAUDE_SESSION_PUB", "SESSIONMAIL_REQUIRE_SIGNATURE", "SESSIONMAIL_HOST"):
        assert d[k] == w[k], k
    assert (d["SESSIONMAIL_FROM_NAME"], w["SESSIONMAIL_FROM_NAME"]) == ("Alice's Delta Chat bridge", "Alice's web call")


# ---- T3 (SC2): replay and re-read -> no further mail ---------------------------------------

def test_t3_a_replay_or_a_re_read_mails_nothing_more(heard, fake_message, tmp_path):
    f = follower(heard)
    write(heard, turn(741, "Hey Alice"))
    go(pump(f))
    assert len(fake_message.calls()) == 1
    # the same follower rewinds its offsets and re-reads every byte
    f.offsets = {p: 0 for p in f.offsets}
    go(pump(f))
    # a re-list: the same files, read again from where it was
    go(pump(f))
    # a second day's file appears, carrying a copy of the same line
    write(heard, turn(741, "Hey Alice"), day=NEXT_DAY)
    go(pump(f))
    assert len(fake_message.calls()) == 1
    # a second follower made now starts at the end: it never sees the turn
    g = follower(heard)
    go(pump(g))
    assert len(fake_message.calls()) == 1 and g.mailed == []


def test_start_at_the_end_reads_nothing_older_than_the_call(heard, fake_message):
    """§5.2: the old turn for this same N, written before the follower was made, is never mailed."""
    f = follower(heard)
    go(pump(f))
    assert fake_message.calls() == [] and f.last_seq == 0


def test_start_at_the_end_holds_for_both_of_two_day_files(tmp_path, fake_message):
    """The follower reads the last two day files; a turn already in either, older than the call, is never mailed."""
    d = tmp_path / "two-days"
    d.mkdir()
    write(d, turn(700, "an old turn of an old call", n=N), day=DAY)
    write(d, turn(740, "an older turn of this same N, before the call"), day=NEXT_DAY)
    f = follower(d)
    go(pump(f))
    assert fake_message.calls() == [] and f.last_seq == 0


def test_midnight_a_new_day_file_is_read_from_its_start(heard, fake_message):
    f = follower(heard)
    write(heard, turn(741, "before midnight"))
    write(heard, turn(742, "after midnight"), day=NEXT_DAY)
    go(pump(f))
    assert [m["Message-ID"] for m in fake_message.mails()] == [
        f"<call-turn-web-{N}-741@{IDS}>", f"<call-turn-web-{N}-742@{IDS}>"]


def test_a_counter_restart_is_said_once_loudly(heard, fake_message):
    logs = []
    f = follower(heard, logs=logs)
    write(heard, turn(741, "one"))
    go(pump(f))
    write(heard, turn(5, "the ears restarted with an empty log"), turn(6, "and again"))
    go(pump(f))
    assert len(fake_message.calls()) == 1
    went = [m for m in logs if "HEARD SEQ WENT BACK" in m]
    assert len(went) == 1 and "seq 5 after 741" in went[0] and f"call:{N}" in went[0]


# ---- T4: lines it must not mail ----------------------------------------------------------

def test_t4_lines_it_must_not_mail(heard, fake_message):
    logs = []
    f = follower(heard, logs=logs)
    with (heard / DAY).open("ab") as fh:
        fh.write(b"not json\n")
    write(heard,
          {**turn(741, "a partial"), "kind": "partial", "final": False},
          event(742, "heartbeat"),
          turn(743, "another call's turn", n=N + 1),
          turn(744, "the room mic", source="mic", device="mic"),
          turn(745, "not final", final=False),
          turn(746, "   "),
          turn(None, "a turn with no seq"),
          turn("747", "a string seq"),
          turn(True, "a bool seq"),
          {**turn(748, "a Delta-shaped device"), "device": f"call:{N}0"},
          tail=json.dumps(turn(749, "half a line")).encode())
    go(pump(f))
    assert fake_message.calls() == []
    assert sum("no integer seq" in m for m in logs) == 1
    # the half line is mailed once its newline lands
    with (heard / DAY).open("ab") as fh:
        fh.write(b"\n")
    go(pump(f))
    go(pump(f))
    (m,) = fake_message.mails()
    assert m["Message-ID"] == f"<call-turn-web-{N}-749@{IDS}>"


def test_a_turn_with_no_final_key_is_mailed(heard, fake_message):
    """`final` absent is not `final: false` (§4.2)."""
    f = follower(heard)
    write(heard, turn(741, "no final key", final=None))
    go(pump(f))
    assert len(fake_message.calls()) == 1


# ---- T4b: echo, per turn (§4.2, Q8) -------------------------------------------------------

def test_t4b_echo_is_dropped_logged_with_n_seq_and_score_and_the_rest_mailed(heard, fake_message):
    clock = Clock(T0)
    g = guard(clock)
    g.spoken(SPOKEN, 3.0, at=T0)                          # played T0..T0+3
    logs = []
    f = follower(heard, logs=logs, echo=g)
    write(heard,
          turn(741, SPOKEN, t=T0 + 5),                 # 2 s past playout: in the guard's 3 s tail
          turn(742, "Something else entirely, about lunch.", t=T0 + 5.5),
          turn(743, MIXED, t=T0 + 5.8))
    go(pump(f))
    ids = [m["Message-ID"] for m in fake_message.mails()]
    assert ids == [f"<call-turn-web-{N}-742@{IDS}>", f"<call-turn-web-{N}-743@{IDS}>"]
    (drop,) = [m for m in logs if m.startswith("ECHO ")]
    # build notes 1-2: call N, the seq and the score on ONE line, with the whole turn
    assert drop.startswith(f"ECHO call:{N} seq 741: echo of bridge at 1.00, 2.0s after")
    assert drop.endswith(f"| dropped: {SPOKEN}") and "\n" not in drop
    assert fake_message.mails()[1].get_content().startswith(f"(on the call) {MIXED}")   # echo included
    assert f.last_seq == 743


def test_t4b_the_measured_pair_sits_either_side_of_the_bar():
    """Build note 2: pin the mixed turn by its score, not by 'about as long'."""
    assert echo.score(MIXED, SPOKEN) < echo.NEAR_THRESHOLD < echo.THRESHOLD
    assert echo.score(SHORT, SPOKEN) >= echo.THRESHOLD


def test_t4b_an_echo_and_a_short_reply_drop_whole(heard, fake_message):
    g = guard()
    g.spoken(SPOKEN, 3.0, at=T0)
    logs = []
    f = follower(heard, logs=logs, echo=g)
    write(heard, turn(741, SHORT, t=T0 + 5))
    go(pump(f))
    assert fake_message.calls() == []
    (drop,) = [m for m in logs if m.startswith("ECHO ")]
    assert "seq 741" in drop and "at 0.96" in drop and drop.endswith(f"dropped: {SHORT}")


def test_t4b_checked_at_the_lines_time_not_the_followers(heard, fake_message):
    """A follower that reads late must not let an echo through: at = the line's ts."""
    clock = Clock(T0)
    g = guard(clock)
    g.spoken(SPOKEN, 3.0, at=T0)
    f = follower(heard, echo=g)
    write(heard, turn(741, SPOKEN, t=T0 + 5))
    clock.now = T0 + 60                                  # read a minute late
    go(pump(f))
    assert fake_message.calls() == [] and g.dropped == 1


def test_t4b_a_ts_that_will_not_parse_is_checked_at_the_clock(heard, fake_message):
    clock = Clock(T0 + 5)
    g = guard(clock)
    g.spoken(SPOKEN, 3.0, at=T0)
    logs = []
    f = follower(heard, logs=logs, echo=g)
    write(heard, turn(741, SPOKEN, ts="yesterday-ish"), turn(742, "Mine, and new.", ts=None))
    go(pump(f))
    assert [m["Message-ID"] for m in fake_message.mails()] == [f"<call-turn-web-{N}-742@{IDS}>"]
    assert any(m.startswith(f"ECHO call:{N} seq 741") for m in logs)
    assert not any("follower stopped" in m for m in logs)


# ---- T5: the drain at hang-up (§5.6) ------------------------------------------------------

def drive(heard, clock, *, script, logs=None, **kw):
    """run() the follower with a fake clock: each poll's sleep moves the clock;
    script(poll_no, follower) writes lines or ends the call between polls."""
    polls = []

    async def sleep(dt):
        polls.append(dt)
        script(len(polls), f)
        clock.now += dt
        await asyncio.sleep(0)

    f = follower(heard, logs=logs, clock=clock, sleep=sleep)

    async def main():
        f.start()
        await f.task
        return f

    return go(main()), polls


def test_t5_the_last_turn_after_hang_up_is_mailed_then_it_stops(heard, fake_message):
    clock = Clock(T0)

    def script(i, f):
        if i == 2:
            f.end()                                                       # hang-up at T0 + 0.2
        if i == 4:
            write(heard, event(741, "listen-stopped", t=T0 - 30, reason="died"))   # mid-call: does not count
        if i == 6:
            write(heard, turn(742, "my last words", t=T0 + 1.0), event(743, "listen-stopped", t=T0 + 1.1))
        assert i < 50, "never stopped"

    f, polls = drive(heard, clock, script=script)
    (m,) = fake_message.mails()
    assert m["Message-ID"] == f"<call-turn-web-{N}-742@{IDS}>"
    assert f.stopped_at is not None and len(polls) == 6


def test_t5_with_no_listen_stopped_it_stops_at_the_grace(heard, fake_message):
    clock = Clock(T0)

    def script(i, f):
        if i == 1:
            f.end()
        assert i < 200, "never stopped"

    f, polls = drive(heard, clock, script=script)
    assert fake_message.calls() == [] and f.stopped_at is None
    assert clock.now - f.ended_at >= turns.GRACE_S and len(polls) == pytest.approx(turns.GRACE_S / turns.POLL_S, abs=2)


def test_wait_is_bounded_and_cancels_a_follower_that_will_not_finish(heard):
    logs = []

    async def main():
        f = follower(heard, logs=logs, sleep=asyncio.sleep, poll=0.01)
        f.start()
        await f.wait(timeout=0.05)                # never ended: it cannot drain
        await asyncio.sleep(0)
        return f

    f = go(main())
    assert f.task.cancelled() and any("did not finish" in m for m in logs)


def test_a_follower_that_fails_says_so_once_and_goes_quiet(heard, fake_message):
    class Broken:
        def check(self, *a, **k):
            raise RuntimeError("boom")

    logs = []

    async def main():
        f = follower(heard, logs=logs, echo=Broken())
        write(heard, turn(741, "hello"))
        f.start()
        await f.task
        return f

    f = go(main())
    assert fake_message.calls() == [] and f.task.done() and not f.task.cancelled()
    (stopped,) = [m for m in logs if "follower stopped" in m]
    assert f"call:{N}" in stopped and "RuntimeError: boom" in stopped


# ---- T6: the send policy (§5.5; PHONE-256: one try, then the spool) -------------------------

MTA = refused("mta")
MID = f"call-turn-web-{N}-741@{IDS}"


@pytest.fixture
def kept(tmp_path):
    """The web server's own spool, as CallDesk builds it: <webdir>/undelivered, on a fake clock."""
    return spool.Spool(lambda m: None, root=tmp_path / "web" / "undelivered", clock=Clock(T0))


def records(k):
    return [json.loads(p.read_text()) for p in k.waiting()] + \
        [json.loads(p.read_text()) for p in sorted((k.root / spool.DEAD).glob("*.json"))]


def test_t6_a_refused_send_is_tried_once_and_kept_under_its_message_id(heard, fake_message, kept):
    """No in-process retry (the foreman's ruling 3): one try, then kept; the spool retries."""
    fake_message.answer(MTA, MTA, MTA)
    waits, logs, told = [], [], []

    async def sleep(dt):
        waits.append(dt)

    f = follower(heard, logs=logs, sleep=sleep, kept=kept, tell=told.append)
    write(heard, turn(741, "nobody takes it"))
    go(pump(f))
    go(pump(f))
    assert len(fake_message.calls()) == 1 and waits == []
    (no,) = [m for m in logs if "NOT mailed" in m]
    assert no == f"TURN call:{N} seq 741 NOT mailed to alice@{HOST}: rc=3 kind=mta: refused: mta, taken=no"
    assert f.last_seq == 741 and f.mailed == [] and told == [741]
    (rec,) = records(kept)
    assert spool.message_id(rec["args"]) == MID and spool.target(rec["args"]) == SEAT.to
    assert rec["next_try"] == T0 + spool.CALL_TURN_FIRST_S        # fix-001: a call turn, first retry at +5 s
    assert "nobody takes it" in rec["body"]


@pytest.mark.parametrize("kind", seatmail.WORLD_KINDS)
def test_t6_a_world_kind_that_sent_nothing_is_kept_for_the_spool(heard, fake_message, kind, kept):
    fake_message.answer(refused(kind))
    f = follower(heard, kept=kept)
    write(heard, turn(741, "again"))
    go(pump(f))
    assert len(fake_message.calls()) == 1 and f.mailed == []
    assert len(kept.waiting()) == 1 and kept.counts() == (1, 0)


@pytest.mark.parametrize("kind", ["usage", "subject", "address", "thread-id", "body", "header", "message-id", None])
def test_t6_an_input_kind_is_kept_dead_and_never_retried(heard, fake_message, kind, kept):
    """It would only fail again: one try, logged taken=no, kept in dead/ (counted, never lost silently)."""
    fake_message.answer((2, {"sent": "no", "kind": kind, "message": "bad"} if kind else {}))
    logs, told = [], []
    f = follower(heard, logs=logs, kept=kept, tell=told.append)
    write(heard, turn(741, "bad input"))
    go(pump(f))
    assert len(fake_message.calls()) == 1 and f.mailed == []
    (no,) = [m for m in logs if "NOT mailed" in m]
    assert no.endswith(", taken=no") and (kind is None or f"kind={kind}" in no)
    assert kept.counts() == (0, 1) and told == [741]


@pytest.mark.parametrize("reply", [refused("mta", sent="unknown"), refused("undeliverable", sent="unknown"),
                                   (3, {"sent": "unknown", "message": "sendmail exited 75"})])
def test_t6_sent_unknown_is_tried_once_and_kept(heard, fake_message, reply, kept):
    """An MTA that failed after the hand-off: the mail may have gone. Never twice here:
    it is kept, and the spool asks the box before any retry."""
    fake_message.answer(reply)
    logs, told = [], []
    f = follower(heard, logs=logs, kept=kept, tell=told.append)
    write(heard, turn(741, "did it go?"))
    go(pump(f))
    assert len(fake_message.calls()) == 1
    (u,) = [m for m in logs if "sent=unknown" in m]
    assert u.startswith(f"TURN call:{N} seq 741 NOT confirmed to alice@{HOST}: rc=3")
    assert u.endswith(f"<{MID}>") and "taken=no" not in u
    assert told == [741] and sum(kept.counts()) == 1


def test_t6_a_timeout_is_tried_once_logged_sent_unknown_and_kept(heard, kept):
    tries, logs = [], []

    def send(mail):
        tries.append(mail)
        return Sent(False, "rc=- kind=timeout sent=unknown: no answer in 30s", kind="timeout", sent="unknown")

    f = follower(heard, logs=logs, send=send, kept=kept)
    write(heard, turn(741, "did it go?"))
    go(pump(f))
    f.offsets = {p: 0 for p in f.offsets}
    go(pump(f))
    assert len(tries) == 1
    (u,) = [m for m in logs if "sent=unknown" in m]
    assert u.startswith(f"TURN call:{N} seq 741 NOT confirmed to alice@{HOST}") and "taken=no" not in u
    assert "timeout" in u
    (rec,) = records(kept)                       # kept once, though the lines were read twice
    assert rec["sent"] == "unknown" and rec["kind"] == "timeout"


def test_t6_a_timeout_through_seatmail_is_sent_unknown(heard, fake_message, monkeypatch):
    """The real TurnMail.send: message send ran past its bound, so sent=unknown."""
    import subprocess

    def hang(argv, **k):
        raise subprocess.TimeoutExpired(argv, seatmail.TIMEOUT_S)
    monkeypatch.setattr(seatmail.subprocess, "run", hang)
    logs = []
    f = follower(heard, logs=logs)
    write(heard, turn(741, "slow"))
    go(pump(f))
    (u,) = [m for m in logs if "NOT confirmed" in m]
    assert "kind=timeout sent=unknown" in u


def test_t6_a_send_that_cannot_be_run_is_kept_with_the_turns_full_args(heard, kept):
    """_send's exec fallback answers args=[]: the record's args are the TurnMail's own."""
    tries, logs = [], []

    def send(mail):
        tries.append(mail)
        raise FileNotFoundError(2, "No such file", "/nope/message")

    f = follower(heard, logs=logs, send=send, kept=kept)
    write(heard, turn(741, "x"))
    go(pump(f))
    assert len(tries) == 1 and any("FileNotFoundError" in m and "kind=exec" in m and "taken=no" in m for m in logs)
    (rec,) = records(kept)
    assert rec["args"] == tries[0].args() and rec["args"][:2] == ["send", SEAT.to]
    assert spool.message_id(rec["args"]) == MID and rec["kind"] == "exec"


def test_t6_a_keep_that_fails_is_logged_and_never_the_followers_end(heard, fake_message):
    class Full:
        def keep(self, sent, body):
            raise OSError(28, "No space left on device")
    fake_message.answer(MTA)
    logs = []
    f = follower(heard, logs=logs, kept=Full())
    write(heard, turn(741, "x"), turn(742, "y"))
    go(pump(f))
    (nk,) = [m for m in logs if "NOT KEPT" in m]
    assert nk.startswith(f"TURN call:{N} <{MID}> NOT KEPT: OSError") and "heard log only" in nk
    assert f.mailed == [f"call-turn-web-{N}-742@{IDS}"]          # the next turn still goes


def test_t6_a_tell_that_fails_is_logged_and_never_the_followers_end(heard, fake_message):
    def tell(seq):
        raise RuntimeError("no loop")
    fake_message.answer(MTA)
    logs = []
    f = follower(heard, logs=logs, tell=tell)
    write(heard, turn(741, "x"), turn(742, "y"))
    go(pump(f))
    assert any(m.startswith(f"UNDELIVERED call:{N} seq 741: not said: RuntimeError") for m in logs)
    assert f.mailed == [f"call-turn-web-{N}-742@{IDS}"]


def test_a_send_cancelled_in_flight_still_logs_when_it_lands(heard):
    """§5.5: shielded, as _mail_turn's is. A stop must not lose the send's log line."""
    import threading
    gate, logs = threading.Event(), []

    def send(mail):
        gate.wait(5)
        return Sent(True, id=f"<{mail.msgid}>", sent="yes")

    async def main():
        f = follower(heard, logs=logs, send=send)
        write(heard, turn(741, "slow"))
        t = asyncio.ensure_future(pump(f))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        gate.set()
        for _ in range(100):
            if f.mailed:
                break
            await asyncio.sleep(0.01)
        return f

    f = go(main())
    assert f.mailed == [f"call-turn-web-{N}-741@{IDS}"]
    assert any("(late)" in m and "mailed to" in m for m in logs)


def test_f1_a_send_cancelled_in_flight_that_lands_refused_is_kept_once_and_never_said(heard, kept):
    """The late path (CallDesk.stop()'s bound): the send's own thread keeps it;
    _landed_late only logs, and says nothing (the call is gone)."""
    import threading
    gate, logs, told = threading.Event(), [], []

    def send(mail):
        gate.wait(5)
        return Sent(False, "rc=3 kind=mta: no MTA", kind="mta")

    async def main():
        f = follower(heard, logs=logs, send=send, kept=kept, tell=told.append)
        write(heard, turn(741, "refused, then stopped"))
        t = asyncio.ensure_future(pump(f))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        gate.set()
        for _ in range(200):
            if any("(late)" in m for m in logs):
                break
            await asyncio.sleep(0.01)
        return f

    f = go(main())
    assert f.mailed == [] and told == []
    (no,) = [m for m in logs if "NOT mailed" in m]
    assert no == f"TURN call:{N} seq 741 NOT mailed to alice@{HOST} (late): rc=3 kind=mta: no MTA, taken=no"
    (rec,) = records(kept)
    assert spool.message_id(rec["args"]) == MID


# ---- T8: who the turns go to, and when --legs is refused (§4.5, §11 A1, A4, A8) -------------------

def test_t8_no_seat_named_is_refused_even_with_claude_code_agent():
    seat, why = turns.named_seat(None, environ={"CLAUDE_CODE_AGENT": "worker", **SH}, fqdn=HOST)
    assert seat is None and "no seat is named" in why and "--mail-to" in why and "CLAUDE_CODE_AGENT" in why
    assert turns.named_seat("  ", environ=SH, fqdn=HOST)[0] is None          # blank is not a name


@pytest.mark.parametrize("flag,environ,to", [
    ("alice@example.net", {"DC_MAILTO": "carol@example.net", "DC_AGENT": "team"}, "alice@example.net"),
    (None, {"DC_MAILTO": "carol@example.net", "DC_AGENT": "team"}, "carol@example.net"),
    (None, {"DC_AGENT": "Team"}, "team"),                                # bare: no host baked in (PHONE-248)
    (None, {"PIP_CHAT_MAILTO": "old@example.net"}, "old@example.net"),     # the deprecated alias
    ("alice", {"DC_MAILTO": "carol@example.net"}, "alice"),                # a bare --mail-to is kept
    (None, {"DC_MAILTO": "alice"}, "alice"),                              # a bare DC_MAILTO is kept
])
def test_t8_the_seat_is_mail_to_else_dc_mailto_else_bare_dc_agent(flag, environ, to):
    seat, why = turns.named_seat(flag, environ={"CLAUDE_CODE_AGENT": "worker", **SH, **environ}, fqdn=HOST)
    assert why == "" and seat.to == to and seat.host == HOST
    assert "worker" not in seat.addr                                    # never the shell's agent (A8)


@pytest.mark.parametrize("flag,environ", [
    ("alice@", {}), ("@example.net", {}), (" @ ", {}), ("@", {}),
    (None, {"DC_MAILTO": "alice@"}),
])
def test_t8_a_to_that_is_no_mail_address_is_refused(flag, environ):
    """A To with no box, or an `@` and no host, names no box: refused. A bare box
    (`alice`) is kept: it is the box on this host (PHONE-248)."""
    seat, why = turns.named_seat(flag, environ={**SH, **environ}, fqdn=HOST)
    assert seat is None and "is not a mail address" in why and "BOX or BOX@HOST" in why


def test_t8_the_from_follows_the_seat_not_the_shell():
    seat, _ = turns.named_seat("alice+phone@example.net", environ={"CLAUDE_CODE_AGENT": "worker", **SH}, fqdn=HOST)
    assert (seat.addr, seat.name) == (f"alice-chat@{HOST}", "Alice")
    seat, _ = turns.named_seat(None, environ={"DC_AGENT": "team", "DC_ADDR": "team-phone@example.net", **SH},
                                fqdn=HOST)
    assert seat.addr == "team-phone@example.net"


@pytest.fixture
def no_mail_json(monkeypatch):
    """No SESSIONMAIL_HOST, and sessionmail's mail.json gives no `host`."""
    monkeypatch.delenv("SESSIONMAIL_HOST", raising=False)
    monkeypatch.delenv("SESSION_MAIL_HOST", raising=False)
    monkeypatch.setattr(settings, "mail_text", lambda *a, **k: None)


# What host_part still gives that is no mail host: none, localhost, or a .arpa name
# a variable names. PHONE-98's refusal stays the last resort for exactly these.
JUNK_PART = [   # (environ, hostname, getfqdn, what the refusal says of the host)
    ({}, "", "x.example.net", "is empty"),                              # no short host at all
    ({}, "localhost", "localhost", "'localhost', which names no machine"),
    ({"SESSIONMAIL_HOST": "x.ip6.arpa"}, "stranger", ARPA, "'x.ip6.arpa', a reverse-DNS name"),
]


@pytest.mark.parametrize("environ,hostname,fqdn,said", JUNK_PART, ids=["none", "localhost", "arpa-variable"])
def test_t8_a_junk_host_is_refused_even_with_mail_to_and_dc_addr(no_mail_json, environ, hostname, fqdn, said):
    """§11 A4: host_problem() passes any host when DC_ADDR and DC_MAILTO are both
    named; the web turn still builds its Reply-To on host_part()'s answer."""
    full = {"DC_ADDR": "alice-chat@example.net", "DC_MAILTO": "alice@example.net"}
    assert dc_env.host_problem(full, host=hostname) is None               # the bot's rule would pass it
    for flag, env in (("alice@example.net", environ), (None, {**full, **environ})):
        seat, why = turns.named_seat(flag, environ=env, fqdn=fqdn, hostname=hostname)
        assert seat is None and said in why and "DC_HOST=" in why and "SESSIONMAIL_HOST" in why
        assert "Message-ID" not in why                    # the ids are seatmail.id_host()'s, not this host


def test_t8_the_refusal_names_where_the_host_came_from(no_mail_json):
    _, why = turns.named_seat("alice@example.net", environ={"SESSIONMAIL_HOST": "x.ip6.arpa"},
                              fqdn=ARPA, hostname="stranger")
    assert "(from SESSIONMAIL_HOST)" in why
    _, why = turns.named_seat("alice@example.net", environ={}, fqdn="localhost", hostname="localhost")
    assert "(from sessionmail's mail.json `host`, else this machine's name)" in why


# PHONE-217: with no DC_HOST, the host is dc_env.host_part (sessiontools ST-217's rule),
# never a raw getfqdn(). Each row is (hostname, getfqdn, the seat's host).
HOST_PART = [
    ("stranger.local", ARPA, "stranger"),                       # macOS 27: the loopback PTR -> the short host
    ("stranger", "stranger.example-tail.ts.net", "stranger.example-tail.ts.net"),   # a tailnet name stays
    ("stranger", "mac.example.net", "stranger"),                # CHANGED on purpose: an unrelated FQDN is not ours
    ("Stranger", "Stranger.Example-Tail.ts.net", "stranger.example-tail.ts.net"),   # CHANGED on purpose: lowercased
    ("macbook", "macbook", "macbook"),                          # a dotless short host is a mail host: no refusal
    ("macbook", "", "macbook"),                                 # no DNS name at all
]


@pytest.mark.parametrize("hostname,fqdn,host", HOST_PART, ids=["arpa", "tailnet", "unrelated", "mixed-case",
                                                               "dotless", "no-fqdn"])
def test_t8_no_dc_host_defaults_through_host_part_not_getfqdn(no_mail_json, hostname, fqdn, host):
    seat, why = turns.named_seat("ada@example.net", environ={}, fqdn=fqdn, hostname=hostname)
    assert why == "" and seat.host == host and seat.addr == f"ada-chat@{host}"
    seat, why = turns.named_seat(None, environ={"DC_AGENT": "ada"}, fqdn=fqdn, hostname=hostname)
    assert why == "" and seat.to == "ada" and seat.host == host     # the To is bare (PHONE-248); the host is not in it


def test_t8_the_arpa_regression_through_the_socket_calls(no_mail_json, monkeypatch):
    """The bug as found (findings/repro.md (a)): getfqdn() is the IPv6 loopback's PTR
    name. --legs was refused, asking for DC_HOST by hand; now it takes the short host."""
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: ARPA)
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger.local")
    seat, why = turns.named_seat("ada@stranger")
    assert why == "" and seat.host == "stranger"


def test_t8_no_dns_lookup_when_dc_host_is_named(monkeypatch):
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: pytest.fail("asked DNS with DC_HOST set"))
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: pytest.fail("asked the hostname with DC_HOST set"))
    seat, why = turns.named_seat("ada@example.net", environ={"DC_HOST": "example.net"})
    assert why == "" and seat.host == "example.net"


def test_t8_a_named_dc_host_is_trusted_and_used():
    seat, why = turns.named_seat("alice@example.net", environ={"DC_HOST": "localhost"}, fqdn="1.0.ip6.arpa")
    assert why == "" and seat.host == "localhost"
    seat, why = turns.named_seat(None, environ={"DC_HOST": "example.net", "DC_AGENT": "alice"}, fqdn="")
    assert why == "" and seat.to == "alice" and seat.host == "example.net"     # DC_HOST: Reply-To, never the To


def test_t8_the_start_lines_name_the_to_the_from_and_the_reply_to(tmp_path):
    seat, _ = turns.named_seat("alice@example.net", environ=SH, fqdn=HOST)
    a, b = turns.start_lines(seat, tmp_path, environ={})       # the suite's carrier: an MTA
    assert a == ("turns: each finished turn of a web call mailed to alice@example.net (From seat alice-chat), "
                 f"Reply-To calls+web-<N>@{HOST} (M5, signed (carrier /usr/sbin/sendmail is an MTA, "
                 "from sessionmail's default))")
    assert b == f"heard log: {tmp_path}"                                   # there: no mark


def test_t8_the_start_lines_name_the_mode_signing_rules_never_a_fixed_signed(tmp_path):
    """The web's turns are signed as seatmail.signing() rules, as the bot's mail
    is: on the drop with no signer they go unsigned, and the start says so and why."""
    seat, _ = turns.named_seat("alice@example.net", environ=SH, fqdn=HOST)
    a, _ = turns.start_lines(seat, tmp_path, environ={"SESSIONMAIL_SENDMAIL": "drop"})
    assert a.endswith(", unsigned (carrier drop: this bot writes into the box itself, "
                      "from SESSIONMAIL_SENDMAIL))")
    keyed = {"SESSIONMAIL_SENDMAIL": "drop", "DC_SIGNER_SOCK": "/s", "DC_SIGNER_PUB": "/p"}
    a, _ = turns.start_lines(seat, tmp_path, environ=keyed)
    assert a.endswith(", signed (a signer is given in DC_SIGNER_SOCK and DC_SIGNER_PUB, on carrier drop))")
    assert not a.endswith(", signed)")                          # never the old fixed word


def test_t8_a_missing_heard_dir_is_marked_at_start_never_silent(tmp_path):
    """build-002 review N1: a heard dir that is not there means no turn reaches the
    seat, the DEMO's failure shape, so the start line says so."""
    seat, _ = turns.named_seat("alice@example.net", environ=SH, fqdn=HOST)
    _, b = turns.start_lines(seat, tmp_path / "nope")
    assert b == (f"heard log: {tmp_path / 'nope'}  (missing: no turn reaches the seat until the ears "
                 "write here; check --heard-dir)")


# ---- a long turn reaches the seat whole -------------------------------------------------

LONG_WORDS = [f"w{i:04d}" for i in range(2500)]
LONG = " ".join(LONG_WORDS)                      # 14,999 characters: a turn of minutes


def test_a_long_web_turn_reaches_the_seat_whole(heard, fake_message):
    """Every heard word is mailed, in order, once: in the turn mail, or in
    continuation mail threaded under it. A split is marked; a plain '...' never
    stands for words that were not sent."""
    import re
    f = follower(heard)
    write(heard, turn(741, LONG))
    go(pump(f))
    mails = fake_message.mails()
    turn_id = f"<call-turn-web-{N}-741@{IDS}>"
    ids, parts = {turn_id}, []
    for m in mails:
        if m["Message-ID"] == turn_id or m["In-Reply-To"] in ids:
            ids.add(m["Message-ID"])
            parts.append(m.get_content().split("\n\n---\n", 1)[0])
    assert parts, "no turn mail"
    got = re.findall(r"\bw\d{4}\b", " ".join(parts))
    assert got == LONG_WORDS, (f"{len(got)} of {len(LONG_WORDS)} words reached the seat, in "
                               f"{len(parts)} mail(s); the last ends {parts[-1][-40:]!r}")
    for p in parts:
        assert not p.rstrip().endswith("..."), f"a plain '...' stands for words not sent: {p[-40:]!r}"
    for p in parts[:-1]:
        assert "[continued" in p, f"a split that is not marked: {p[-80:]!r}"

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

from sessionphone.bridge import bot, dc_env, echo, seatmail
from sessionphone.bridge.echo import EchoGuard
from sessionphone.bridge.seatmail import Sent
from sessionphone.web import turns
from sessionphone.web.turns import Seat, TurnFollower, seat_for, turn_mail

N = 1918213200                       # a web call's id: ten digits, the second it began
HOST = "example.net"              # DC_HOST: the Reply-To
IDS = "host"                           # seatmail.id_host(), sessionmail's own: the ids (PHONE-72 2b)
SEAT = seat_for(f"alice@{HOST}", HOST, environ={})
CALLER = "Ada"                       # who spoke: web/names.py's setting, handed down (PHONE-135)
T0 = 1918213200.0                    # the call's clock, epoch seconds
AEDT = datetime.timezone(datetime.timedelta(hours=11))
DAY = "heard_2030-10-15.jsonl"
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
    """A heard dir with yesterday's file and today's, each with lines older than the call."""
    d = tmp_path / "conversations"
    d.mkdir()
    write(d, turn(700, "an old turn of an old call", n=N), day="heard_2030-10-14.jsonl")
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
    write(heard, turn(741, "Hey Alice"), day="heard_2030-10-16.jsonl")
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


def test_midnight_a_new_day_file_is_read_from_its_start(heard, fake_message):
    f = follower(heard)
    write(heard, turn(741, "before midnight"))
    write(heard, turn(742, "after midnight"), day="heard_2030-10-16.jsonl")
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


# ---- T6: the send policy (§5.5) ------------------------------------------------------------

MTA = refused("mta")


def test_t6_a_refused_send_is_retried_with_the_same_message_id(heard, fake_message):
    fake_message.answer(MTA, MTA)
    waits, logs = [], []

    async def sleep(dt):
        waits.append(dt)

    f = follower(heard, logs=logs, sleep=sleep)
    write(heard, turn(741, "third time lucky"))
    go(pump(f))
    mails = fake_message.mails()
    assert len(mails) == 3 and {m["Message-ID"] for m in mails} == {f"<call-turn-web-{N}-741@{IDS}>"}
    assert waits == [1.0, 2.0]
    assert f.mailed == [f"call-turn-web-{N}-741@{IDS}"]
    assert any(m.startswith(f"TURN call:{N} seq 741 mailed to alice@") for m in logs)


def test_t6_refused_every_time_is_logged_taken_no_and_never_re_sent(heard, fake_message):
    fake_message.answer(MTA, MTA, MTA, MTA)
    logs = []
    f = follower(heard, logs=logs)
    write(heard, turn(741, "nobody takes it"))
    go(pump(f))
    go(pump(f))
    assert len(fake_message.calls()) == turns.TRIES == 3
    (no,) = [m for m in logs if "NOT mailed" in m]
    assert no == f"TURN call:{N} seq 741 NOT mailed to alice@{HOST}: rc=3 kind=mta: refused: mta, taken=no"
    assert f.last_seq == 741 and f.mailed == []


@pytest.mark.parametrize("kind", seatmail.WORLD_KINDS)
def test_t6_only_a_world_kind_that_sent_nothing_is_retried(heard, fake_message, kind):
    """The super's ruling (b): retry ONLY on sent=no with a world kind."""
    fake_message.answer(refused(kind), refused(kind))
    f = follower(heard)
    write(heard, turn(741, "again"))
    go(pump(f))
    assert len(fake_message.calls()) == 3 and f.mailed == [f"call-turn-web-{N}-741@{IDS}"]


@pytest.mark.parametrize("kind", ["usage", "subject", "address", "thread-id", "body", "header", "message-id", None])
def test_t6_an_input_kind_is_never_retried(heard, fake_message, kind):
    """It would only fail again: one try, logged taken=no."""
    fake_message.answer((2, {"sent": "no", "kind": kind, "message": "bad"} if kind else {}))
    logs = []
    f = follower(heard, logs=logs)
    write(heard, turn(741, "bad input"))
    go(pump(f))
    assert len(fake_message.calls()) == 1 and f.mailed == []
    (no,) = [m for m in logs if "NOT mailed" in m]
    assert no.endswith(", taken=no") and (kind is None or f"kind={kind}" in no)


@pytest.mark.parametrize("reply", [refused("mta", sent="unknown"), refused("undeliverable", sent="unknown"),
                                   (3, {"sent": "unknown", "message": "sendmail exited 75"})])
def test_t6_sent_unknown_is_never_retried(heard, fake_message, reply):
    """An MTA that failed after the hand-off: the mail may have gone. Never twice."""
    fake_message.answer(reply)
    logs = []
    f = follower(heard, logs=logs)
    write(heard, turn(741, "did it go?"))
    go(pump(f))
    assert len(fake_message.calls()) == 1
    (u,) = [m for m in logs if "sent=unknown" in m]
    assert u.startswith(f"TURN call:{N} seq 741 NOT confirmed to alice@{HOST}: rc=3")
    assert u.endswith(f"<call-turn-web-{N}-741@{IDS}>") and "taken=no" not in u


def test_t6_a_timeout_is_tried_once_and_logged_sent_unknown(heard):
    tries, logs = [], []

    def send(mail):
        tries.append(mail)
        return Sent(False, "rc=- kind=timeout sent=unknown: no answer in 30s", kind="timeout", sent="unknown")

    f = follower(heard, logs=logs, send=send)
    write(heard, turn(741, "did it go?"))
    go(pump(f))
    f.offsets = {p: 0 for p in f.offsets}
    go(pump(f))
    assert len(tries) == 1
    (u,) = [m for m in logs if "sent=unknown" in m]
    assert u.startswith(f"TURN call:{N} seq 741 NOT confirmed to alice@{HOST}") and "taken=no" not in u
    assert "timeout" in u


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


def test_t6_a_send_that_cannot_be_run_is_nothing_sent_and_retried(heard):
    tries, logs = [], []

    def send(mail):
        tries.append(mail)
        raise FileNotFoundError(2, "No such file", "/nope/message")

    f = follower(heard, logs=logs, send=send, sleep=lambda dt: asyncio.sleep(0))
    write(heard, turn(741, "x"))
    go(pump(f))
    assert len(tries) == 3 and any("FileNotFoundError" in m and "kind=exec" in m and "taken=no" in m for m in logs)


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


def test_f1_a_refused_turn_stopped_in_the_back_off_is_still_logged(heard):
    """build-001 review F1: refused, then the follower is cancelled between tries
    (CallDesk.stop()'s bound): nothing went, and the turn's own line says so."""
    tries, logs = [], []

    def send(mail):
        tries.append(mail)
        return Sent(False, "rc=3 kind=mta: no MTA", kind="mta")

    async def main():
        f = follower(heard, logs=logs, send=send, sleep=asyncio.sleep, poll=0.01, backoff=(5.0, 5.0))
        write(heard, turn(741, "refused, then stopped"))
        f.start()
        for _ in range(200):
            if tries:
                break
            await asyncio.sleep(0.01)
        f.end()
        await f.wait(timeout=0.1)
        await asyncio.sleep(0)
        return f

    f = go(main())
    assert len(tries) == 1 and f.task.cancelled()
    (no,) = [m for m in logs if "NOT mailed" in m]
    assert no == f"TURN call:{N} seq 741 NOT mailed to alice@{HOST}: rc=3 kind=mta: no MTA, taken=no (stopped)"
    assert any("did not finish" in m for m in logs)


# ---- T8: who the turns go to, and when --legs is refused (§4.5, §11 A1, A4, A8) -------------------

def test_t8_no_seat_named_is_refused_even_with_claude_code_agent():
    seat, why = turns.named_seat(None, environ={"CLAUDE_CODE_AGENT": "worker"}, fqdn=HOST)
    assert seat is None and "no seat is named" in why and "--mail-to" in why and "CLAUDE_CODE_AGENT" in why
    assert turns.named_seat("  ", environ={}, fqdn=HOST)[0] is None          # blank is not a name


@pytest.mark.parametrize("flag,environ,to", [
    ("alice@example.net", {"DC_MAILTO": "carol@example.net", "DC_AGENT": "team"}, "alice@example.net"),
    (None, {"DC_MAILTO": "carol@example.net", "DC_AGENT": "team"}, "carol@example.net"),
    (None, {"DC_AGENT": "Team"}, f"team@{HOST}"),
    (None, {"PIP_CHAT_MAILTO": "old@example.net"}, "old@example.net"),     # the deprecated alias
])
def test_t8_the_seat_is_mail_to_else_dc_mailto_else_dc_agent_at_host(flag, environ, to):
    seat, why = turns.named_seat(flag, environ={"CLAUDE_CODE_AGENT": "worker", **environ}, fqdn=HOST)
    assert why == "" and seat.to == to and seat.host == HOST
    assert "worker" not in seat.addr                                    # never the shell's agent (A8)


@pytest.mark.parametrize("flag,environ", [
    ("alice", {}), ("alice@", {}), ("@example.net", {}), (" @ ", {}),
    (None, {"DC_MAILTO": "alice"}),
])
def test_t8_a_to_that_is_no_mail_address_is_refused(flag, environ):
    """build-002 review N3: a To with no box or no host goes to a local user, not the seat."""
    seat, why = turns.named_seat(flag, environ=environ, fqdn=HOST)
    assert seat is None and "is not a mail address" in why and "BOX@HOST" in why


def test_t8_the_from_follows_the_seat_not_the_shell():
    seat, _ = turns.named_seat("alice+phone@example.net", environ={"CLAUDE_CODE_AGENT": "worker"}, fqdn=HOST)
    assert (seat.addr, seat.name) == (f"alice-chat@{HOST}", "Alice")
    seat, _ = turns.named_seat(None, environ={"DC_AGENT": "team", "DC_ADDR": "team-phone@example.net"}, fqdn=HOST)
    assert seat.addr == "team-phone@example.net"


@pytest.mark.parametrize("fqdn", ["1.0.0.0.ip6.arpa", "localhost", "macbook", ""])
def test_t8_a_junk_host_is_refused_even_with_mail_to_and_dc_addr(fqdn):
    """§11 A4: host_problem() passes any host when DC_ADDR and DC_MAILTO are both
    named; the web turn still builds its Reply-To and Message-ID on its DNS name."""
    full = {"DC_ADDR": "alice-chat@example.net", "DC_MAILTO": "alice@example.net"}
    assert dc_env.host_problem(full, host=fqdn) is None                    # the bot's rule would pass it
    for flag, environ in (("alice@example.net", {}), (None, full)):
        seat, why = turns.named_seat(flag, environ=environ, fqdn=fqdn)
        assert seat is None and "DC_HOST" in why and repr(fqdn) in why


def test_t8_a_named_dc_host_is_trusted_and_used():
    seat, why = turns.named_seat("alice@example.net", environ={"DC_HOST": "localhost"}, fqdn="1.0.ip6.arpa")
    assert why == "" and seat.host == "localhost"
    seat, why = turns.named_seat(None, environ={"DC_HOST": "example.net", "DC_AGENT": "alice"}, fqdn="")
    assert why == "" and seat.to == "alice@example.net" and seat.host == "example.net"


def test_t8_the_start_lines_name_the_to_the_from_and_the_reply_to(tmp_path):
    seat, _ = turns.named_seat("alice@example.net", environ={}, fqdn=HOST)
    a, b = turns.start_lines(seat, tmp_path, environ={})       # the suite's carrier: an MTA
    assert a == ("turns: each finished turn of a web call mailed to alice@example.net (From seat alice-chat), "
                 f"Reply-To calls+web-<N>@{HOST} (M5, signed (carrier /usr/sbin/sendmail is an MTA, "
                 "from sessionmail's default))")
    assert b == f"heard log: {tmp_path}"                                   # there: no mark


def test_t8_the_start_lines_name_the_mode_signing_rules_never_a_fixed_signed(tmp_path):
    """The web's turns are signed as seatmail.signing() rules, as the bot's mail
    is: on the drop with no signer they go unsigned, and the start says so and why."""
    seat, _ = turns.named_seat("alice@example.net", environ={}, fqdn=HOST)
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
    seat, _ = turns.named_seat("alice@example.net", environ={}, fqdn=HOST)
    _, b = turns.start_lines(seat, tmp_path / "nope")
    assert b == (f"heard log: {tmp_path / 'nope'}  (missing: no turn reaches the seat until the ears "
                 "write here; check --heard-dir)")

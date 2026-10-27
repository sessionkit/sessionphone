"""PHONE-256 fix-001: a kept call turn is first retried within seconds.

PHONE-249 keeps a refused call turn (a call-turn mail under a bot-minted Message-ID,
`call-turn-<chat>-<ms>@<id_host>`; a web call's is `call-turn-web-<N>-<seq>@...`)
in the undelivered spool. Before this fix, Spool.keep stamped next_try = now +
backoff(1) = +60 s whatever the kind of mail, so the agent heard a live caller's
turn a minute after it was refused (repro-001 (b)).

Now spool.backoff(n, call_turn) gives a call turn (spool.call_turn: its
--message-id starts spool.CALL_TURN_PREFIX) a first retry of CALL_TURN_FIRST_S,
then today's ladder from its first rung: 5, 60, 120 ... 3600. On a FAKE clock,
these tests pin: the first retry within ~5 s, the later gaps never shorter,
doubling, capped at BACKOFF_MAX_S, and the exact schedule.

The guard pins what must NOT change: a mail that is not a call turn keeps
today's first retry, BACKOFF_S. The README asks for a faster first retry for a
CALL TURN only.

Fakes only: a fake clock, a fake send, a fake box check (delivered), the
scratch spool dir (conftest's undelivered_in_tmp). No real message, no sleep.
"""
from itertools import pairwise

import pytest

from sessionphone.bridge import seatmail, spool

FIRST_RETRY_S = 5.0        # the README: "a call turn's first retry should come within seconds" (~5 s)
T = 1_907_204_400.0         # the kept turn's refusal, on the fake clock: Sun 2030-06-09
TO = "ada@example.net"
TURNS = {
    "delta": "call-turn-12-1907204400000@host",
    "web": "call-turn-web-1907204400-741@host",
}


class Clock:
    def __init__(self, now=T):
        self.now = now

    def __call__(self):
        return self.now


class Rig:
    """A Spool over the scratch spool dir, on a fake clock, whose send answers from
    a script (refused until told otherwise) and whose box check says 'not there'."""

    def __init__(self, root):
        self.clock, self.logs, self.sends, self.ok = Clock(), [], [], False
        self.spool = spool.Spool(self.logs.append, root=root, clock=self.clock, send=self.send,
                                 delivered=lambda msgid, to, since=None: False)

    def send(self, args, body):
        self.sends.append(self.clock.now)
        if self.ok:
            return seatmail.Sent(True, sent="yes", args=args)
        return seatmail.Sent(False, "rc=2 kind=identity: refused", kind="identity", sent="no", args=args)

    def keep(self, msgid):
        a = seatmail.args(TO, "[Team] Caller: can you check the build", msgid=msgid,
                          headers={"X-Delta-Chat-Kind": "call"})
        return self.spool.keep(seatmail.Sent(False, "rc=2 kind=identity: refused", kind="identity",
                                             sent="no", args=a), "(on the call) can you check the build")


@pytest.fixture
def rig(undelivered_in_tmp):
    return Rig(undelivered_in_tmp)


def _rec(path):
    return spool._read(path)


@pytest.mark.parametrize("msgid", TURNS.values(), ids=TURNS.keys())
def test_a_kept_call_turn_is_retried_within_seconds(rig, msgid):
    path = rig.keep(msgid)
    rig.ok = True
    rig.clock.now = T + FIRST_RETRY_S
    rig.spool.drain()
    assert rig.sends == [T + FIRST_RETRY_S], (
        f"not retried {FIRST_RETRY_S:.0f} s after the refusal: next_try is "
        f"+{_rec(path)['next_try'] - T:.0f} s")
    assert not path.exists()


@pytest.mark.parametrize("msgid", TURNS.values(), ids=TURNS.keys())
def test_after_a_fast_first_retry_a_call_turn_still_backs_off_as_now(rig, msgid):
    """Step the fake clock to each next_try; every try is refused. The gaps between
    tries: the first within seconds, then never shorter, doubling, at most
    BACKOFF_MAX_S, and reaching it (never a tight loop on a dead box)."""
    path = rig.keep(msgid)
    gaps, last = [], T
    for _ in range(12):
        due = float(_rec(path)["next_try"])
        gaps.append(due - last)
        rig.clock.now = last = due
        rig.spool.drain()
        assert rig.sends[-1] == due                     # tried exactly when due, and refused again
    assert gaps[0] <= FIRST_RETRY_S, f"first retry at +{gaps[0]:.0f} s: {gaps}"
    later = gaps[1:]
    assert all(b >= a for a, b in pairwise(later)), gaps
    assert all(b == min(2 * a, spool.BACKOFF_MAX_S) for a, b in pairwise(later)), gaps
    assert max(gaps) == later[-1] == spool.BACKOFF_MAX_S, gaps


@pytest.mark.parametrize("msgid", TURNS.values(), ids=TURNS.keys())
def test_a_kept_call_turn_runs_the_schedule_5_60_120_to_an_hour(rig, msgid):
    """The exact schedule (foreman ruling 1): only the first gap changes. The
    gaps between tries, every try refused: 5, then 60, 120 ... capped at 3600.
    The log line names each next wait."""
    path = rig.keep(msgid)
    assert _rec(path)["next_try"] - T == spool.CALL_TURN_FIRST_S == 5.0
    gaps, last = [], T
    for _ in range(10):
        due = float(_rec(path)["next_try"])
        gaps.append(due - last)
        rig.clock.now = last = due
        rig.spool.drain()
    assert gaps == [5, 60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]
    retries = [m for m in rig.logs if m.startswith("BRIDGE MAIL RETRY ")]
    assert "next try in 60s" in retries[0] and "next try in 120s" in retries[1], retries


def test_a_kept_call_turn_is_not_tried_before_its_first_retry(rig):
    """Not early either: one tick before CALL_TURN_FIRST_S, nothing is sent."""
    path = rig.keep(TURNS["delta"])
    rig.ok = True
    rig.clock.now = T + spool.CALL_TURN_FIRST_S - 1
    rig.spool.drain()
    assert rig.sends == [] and path.exists()


@pytest.mark.parametrize(("msgid", "expected"), [
    ("call-turn-12-1907204400000@host", True),             # a Delta call's turn
    ("call-turn-p-12-1907204400000@host", True),           # under a profile
    ("call-turn-web-1907204400-741@host", True),           # a web call's turn
    ("<call-turn-12-1907204400000@host>", True),           # bracketed: message_id() bares it
    ("call-preview-12-1907204400000@host", False),         # a preview: never kept, never hurried
    ("call-12-1907204400000@host", False),                 # the ringing root
    ("x-call-turn-12@host", False),                        # the prefix, not a substring
    (None, False),                                         # an M1-M4: no --message-id
], ids=["delta", "profile", "web", "bracketed", "preview", "root", "infix", "no-id"])
def test_call_turn_reads_the_message_id_prefix(msgid, expected):
    assert spool.call_turn(seatmail.args(TO, "[Team] Caller: hi", msgid=msgid)) is expected


def test_call_turn_on_empty_args_is_false():
    """An exec-fallback Sent carries args=[]: no id, not a call turn, no crash."""
    assert spool.call_turn([]) is False


def test_backoff_for_a_call_turn_and_for_any_other_mail():
    assert [spool.backoff(n, call_turn=True) for n in range(1, 10)] == \
        [5, 60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert [spool.backoff(n) for n in range(1, 9)] == [spool.backoff(n, call_turn=False) for n in range(1, 9)] == \
        [60, 120, 240, 480, 960, 1920, 3600, 3600]


@pytest.mark.parametrize("msgid", [None, "call-12-1907204400000@host"], ids=["chat-no-id", "call-ringing"])
def test_a_mail_that_is_not_a_call_turn_keeps_todays_first_retry(rig, msgid):
    """Guard: only a call turn is hurried. A chat mail (no --message-id) or another
    call mail (the RINGING root) is first retried at BACKOFF_S, as now."""
    path = rig.keep(msgid)
    assert _rec(path)["next_try"] - T == spool.BACKOFF_S
    rig.clock.now = T + FIRST_RETRY_S
    rig.spool.drain()
    assert rig.sends == [] and path.exists()

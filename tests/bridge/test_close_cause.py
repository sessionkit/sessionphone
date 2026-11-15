"""The call-close line names WHICH path closed the call.

`closing msg N: <why>` used to carry only the raw reason the close path
passed: `connection failed`, `connection closed`, `CallEnded event`. Drops with
different causes printed the same line. Now the line names the cause:

- consent failed after N misses: aioice's consent check closed the ICE
  connection after N missed checks (N read from aioice, never hard-coded);
- remote closed: the far side ended the call (the CallEnded event, or a
  connection `closed` that neither consent loss nor this side caused);
- remote track ended (MediaStreamError) with ICE still <state>: a listen loop
  saw the far side's track end, and the close that follows says so;
- hung up by <agent>: unchanged.

Logging only. The `why` a close path passes is unchanged everywhere else it
goes (the missed-call mail, the ear leg's end frame). The consent test drives
aioice's REAL consent check on a real Connection, so the log record this reads
is proven against the pinned aioice, not against a fake of it.
"""
import asyncio
import types

import aioice.ice
import aioice.stun
import pytest
from aiortc.mediastreams import MediaStreamError

from sessionphone.bridge import calls


class FakePC:
    """A peer connection as far as close() and the cause read it."""

    def __init__(self, connection=None, ice_state="connected"):
        self.iceConnectionState = ice_state
        self.closed = False
        ice = types.SimpleNamespace(_connection=connection)
        dtls = types.SimpleNamespace(transport=ice)
        self._tx = [types.SimpleNamespace(receiver=types.SimpleNamespace(transport=dtls))]
        self.sctp = types.SimpleNamespace(transport=dtls)       # bundled: the same transport

    def getTransceivers(self):
        return self._tx

    async def close(self):
        self.closed = True


@pytest.fixture
def logs(monkeypatch):
    out = []
    monkeypatch.setattr(calls, "log", out.append)
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls.earleg, "end", lambda s, why: out.append(f"earleg end: {why}"))
    return out


def session(pc, tmp_path):
    """A CallSession whose close() runs for real, with the parts it calls
    that are not under test stubbed out."""
    s = object.__new__(calls.CallSession)
    s.pc, s.msg_id, s.chat_id, s.dir = pc, 7, 12, tmp_path / "call"
    s.manager = types.SimpleNamespace(forget=lambda _s: None)
    s.tasks, s.turn_pending, s.turn_parts, s.connected = [], [], [], False
    for name in ("_roots_end", "_live_clear", "_turn_cancel", "_noreply_cancel", "_rec_end"):
        setattr(s, name, lambda *a: None)
    s._unanswered = lambda *a: None
    return s


def closing(logs):
    lines = [line for line in logs if line.startswith("closing msg")]
    assert len(lines) == 1, logs
    return lines[0]


def run(coro):
    return asyncio.run(coro)


# ---- remote closed

def test_the_call_ended_event_says_remote_closed(logs, tmp_path):
    s = session(FakePC(), tmp_path)
    run(s.close("CallEnded event"))
    assert closing(logs) == "closing msg 7: remote closed (CallEnded event)"
    # the why itself is unchanged where it goes on: the ear leg, s.closed
    assert "earleg end: CallEnded event" in logs and s.closed == "CallEnded event"


def test_a_connection_closed_not_by_consent_says_remote_closed(logs, tmp_path):
    s = session(FakePC(), tmp_path)
    run(s.close("connection closed"))
    assert closing(logs) == "closing msg 7: remote closed (connection closed)"


def test_a_plain_connection_failed_is_left_as_it_is(logs, tmp_path):
    s = session(FakePC(), tmp_path)
    run(s.close("connection failed"))
    assert closing(logs) == "closing msg 7: connection failed"


# ---- hung up by the agent: unchanged

def test_hung_up_by_the_agent_is_unchanged(logs, tmp_path):
    s = session(FakePC(), tmp_path)
    run(s.close("hung up by Pip"))
    assert closing(logs) == "closing msg 7: hung up by Pip"


# ---- consent failed after N misses: aioice's real consent check

class _Pair:
    """A nominated pair whose every consent check goes unanswered."""
    component = 1
    remote_addr = ("192.0.2.1", 9)

    def __init__(self):
        self.protocol = types.SimpleNamespace(request=self._request)

    async def _request(self, *a, **k):
        raise aioice.stun.TransactionError()


def consent_lost(monkeypatch, misses):
    """A real aioice Connection whose real query_consent() gives up after
    `misses` unanswered checks, closing the connection, as it does in a call."""
    monkeypatch.setattr(aioice.ice, "CONSENT_INTERVAL", 0.001)
    monkeypatch.setattr(aioice.ice, "CONSENT_FAILURES", misses)
    conn = aioice.ice.Connection(ice_controlling=True)
    conn.remote_username, conn.remote_password = "ru", "rp"
    conn._nominated[1] = _Pair()

    async def go():
        await asyncio.wait_for(conn.query_consent(), 5)
    run(go())
    assert conn._query_consent_task is None, "the consent check did not give up"
    return conn


@pytest.mark.parametrize("misses", [6, 3])
def test_consent_loss_says_consent_failed_after_n_misses(logs, tmp_path, monkeypatch, misses):
    conn = consent_lost(monkeypatch, misses)
    s = session(FakePC(conn), tmp_path)
    run(s.close("connection closed"))
    assert closing(logs) == f"closing msg 7: consent failed after {misses} misses (connection closed)"


def test_consent_loss_on_another_call_is_not_this_calls_cause(logs, tmp_path, monkeypatch):
    consent_lost(monkeypatch, 6)
    other = aioice.ice.Connection(ice_controlling=True)
    s = session(FakePC(other), tmp_path)
    run(s.close("connection closed"))
    assert closing(logs) == "closing msg 7: remote closed (connection closed)"


def test_consent_loss_before_a_hang_up_is_named_after_it(logs, tmp_path, monkeypatch):
    conn = consent_lost(monkeypatch, 6)
    s = session(FakePC(conn), tmp_path)
    run(s.close("hung up by Pip"))
    assert closing(logs) == "closing msg 7: hung up by Pip; consent failed after 6 misses"


def test_the_consent_watch_keeps_aioice_info_records_from_other_handlers(monkeypatch):
    # watching aioice's INFO records must not start printing them anywhere
    import logging
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())
    root = logging.getLogger()
    h = H(level=logging.NOTSET)
    root.addHandler(h)
    try:
        consent_lost(monkeypatch, 2)
    finally:
        root.removeHandler(h)
    assert not [m for m in seen if "Consent to send expired" in m], seen


# ---- remote track ended (MediaStreamError) with the ICE state

class EndedTrack:
    kind = "audio"

    async def recv(self):
        raise MediaStreamError


def listened(s):
    s._rec_start = lambda: types.SimpleNamespace(keep=False)
    run(s._listen(EndedTrack()))


def test_a_remote_track_end_names_the_ice_state_on_the_close_line(logs, tmp_path):
    pc = FakePC(ice_state="connected")
    s = session(pc, tmp_path)
    listened(s)
    assert any(line.startswith("listen ended: MediaStreamError") for line in logs), logs
    pc.iceConnectionState = "checking"        # it is the state AT the track end that counts
    run(s.close("connection failed"))
    line = closing(logs)
    assert line.startswith("closing msg 7: connection failed; "
                           "remote track ended (MediaStreamError) with ICE still connected, "), line
    assert line.endswith(" s earlier"), line


def test_a_remote_track_end_then_the_call_ended_event(logs, tmp_path):
    s = session(FakePC(ice_state="completed"), tmp_path)
    listened(s)
    run(s.close("CallEnded event"))
    assert closing(logs).startswith(
        "closing msg 7: remote closed (CallEnded event); "
        "remote track ended (MediaStreamError) with ICE still completed, ")


def test_a_track_end_after_the_pc_is_gone_says_closed(logs, tmp_path):
    s = session(FakePC(), tmp_path)
    s.pc = None
    listened(s)
    assert s.track_end[:2] == ("MediaStreamError", "closed")


# ---- a log line must never break a call

def test_a_pc_that_cannot_be_read_still_closes_the_call(logs, tmp_path):
    class Broken(FakePC):
        def getTransceivers(self):
            raise RuntimeError("gone")
    pc = Broken()
    s = session(pc, tmp_path)
    run(s.close("CallEnded event"))
    assert closing(logs).startswith("closing msg 7: CallEnded event (cause unknown: RuntimeError)")
    assert pc.closed, "the call was not closed"


# ---- a call this side ended: the CallEnded echo can win the race
#
# hangup (go and decline) and stop run the core's end_call, and end_call itself
# emits CallEnded. That echo's close("CallEnded event") can reach close() before
# the bot's own close; close() runs once, so the line must not then say the far
# side ended a call this side ended. The bot-ended paths mark the session
# `bot_ends` before they call end_call.

@pytest.mark.parametrize("why", calls.REMOTE_WHYS)
def test_a_remote_looking_why_on_a_call_this_side_ended_is_not_remote_closed(logs, tmp_path, why):
    s = session(FakePC(), tmp_path)
    s.bot_ends = True
    run(s.close(why))
    line = closing(logs)
    assert "remote closed" not in line, line
    assert line == f"closing msg 7: ended by this side ({why})"
    # the why itself is unchanged where it goes on
    assert s.closed == why


def test_the_bots_own_close_winning_still_reads_hung_up(logs, tmp_path):
    s = session(FakePC(), tmp_path)
    s.bot_ends = True
    run(s.close("hung up by Pip"))
    assert closing(logs) == "closing msg 7: hung up by Pip"


@pytest.mark.parametrize("why", calls.REMOTE_WHYS)
def test_bot_ends_false_still_reads_remote_closed(logs, tmp_path, why):
    s = session(FakePC(), tmp_path)
    s.bot_ends = False
    run(s.close(why))
    assert closing(logs) == f"closing msg 7: remote closed ({why})"


def test_a_session_with_no_bot_ends_attribute_reads_as_before(logs, tmp_path):
    s = session(FakePC(), tmp_path)
    assert "bot_ends" not in vars(s)   # never set: the class default (PHONE-315) answers
    run(s.close("CallEnded event"))
    assert closing(logs) == "closing msg 7: remote closed (CallEnded event)"


def test_a_call_this_side_ended_keeps_the_track_end_note(logs, tmp_path):
    s = session(FakePC(ice_state="completed"), tmp_path)
    listened(s)
    s.bot_ends = True
    run(s.close("CallEnded event"))
    assert closing(logs).startswith(
        "closing msg 7: ended by this side (CallEnded event); "
        "remote track ended (MediaStreamError) with ICE still completed, ")


def test_consent_loss_still_names_itself_on_a_call_this_side_ended(logs, tmp_path, monkeypatch):
    conn = consent_lost(monkeypatch, 6)
    s = session(FakePC(conn), tmp_path)
    s.bot_ends = True
    run(s.close("connection closed"))
    assert closing(logs) == "closing msg 7: consent failed after 6 misses (connection closed)"

"""Every INCOMING_CALL is logged at dispatch, first, before any await
(PHONE-240).

A missed call left no line in bot.log when core raised IncomingCall and the
answer path stalled: every other INCOMING_CALL branch of handle_events logs,
but the answer path hands the event to calls.incoming(), whose session logs
its first line, `CALL INCOMING`, only after it has awaited the names (rpc
reads). So "no line" could not tell a late delivery from core apart from a
stall in the session start.

handle_events now logs `CALL EVENT IncomingCall msg N chat C video=B` for
every INCOMING_CALL, before the elif chain picks a branch. These tests feed a
fake INCOMING_CALL through the real handle_events, on each branch (answer,
refused, no media pack, calls off, and an answer that raises), and assert the
line is logged first, with no caller address. Before the fix (origin/master
92e8534) the answer path logged nothing at dispatch and the refusal path
logged only its REFUSED line.

Fakes only: no account, no rpc-server, no media, no mail.
"""
import pytest

from sessionphone.bridge import bot

CALLER = "pat@relay.example.org"
MSG, CHAT = 77, 12
EVENT_LINE = f"CALL EVENT IncomingCall msg {MSG} chat {CHAT} video=False"


class Done(Exception):
    """Raised by the fake account when its events run out: handle_events()
    loops forever and does not catch around wait_for_event()."""


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Account:
    """Hands out its events, then Done. Any rpc read is recorded, so a test
    can show the event line came before it."""

    def __init__(self, events, logged):
        self.events, self.logged = list(events), logged

    def wait_for_event(self):
        if not self.events:
            raise Done
        return self.events.pop(0)

    def get_message_by_id(self, mid):
        self.logged.append(f"<rpc read msg {mid}>")
        sender = Obj(get_snapshot=lambda: Obj(address=CALLER, display_name="Pat"))
        return Obj(id=mid, get_snapshot=lambda: Obj(chat_id=CHAT, sender=sender))


def ring(**kw):
    fields = {"kind": bot.EventType.INCOMING_CALL, "msg_id": MSG, "chat_id": CHAT,
              "place_call_info": "v=0\r\n", "has_video": False}
    return Obj(**{**fields, **kw})


class FakeCalls:
    """bot.calls on the answer path. incoming() reads the caller (an rpc read,
    as calls.incoming does) and logs, so the order is on the record."""

    def __init__(self, logged, *, raises=False):
        self.logged, self.raises, self.answered = logged, raises, []

    def incoming(self, account, event):
        account.get_message_by_id(event.msg_id)
        if self.raises:
            raise RuntimeError("the session could not start")
        self.answered.append(event.msg_id)
        self.logged.append(f"<calls.incoming msg {event.msg_id}>")


@pytest.fixture
def logged(monkeypatch):
    out = []
    monkeypatch.setattr(bot, "log", out.append)
    monkeypatch.setattr(bot, "CALLS", True)
    monkeypatch.setattr(bot, "calls_refusal", lambda starts=True: None)
    return out


def run(events, logged):
    with pytest.raises(Done):
        bot.handle_events(Account(events, logged))


def assert_first_and_clean(logged):
    assert logged, "nothing logged"
    assert logged[0] == EVENT_LINE, logged
    assert sum(line.startswith("CALL EVENT") for line in logged) == 1, logged
    assert CALLER not in "\n".join(line for line in logged if line.startswith("CALL EVENT"))


def test_the_answer_path_logs_the_event_before_the_session_is_handed_it(logged, monkeypatch):
    calls = FakeCalls(logged)
    monkeypatch.setattr(bot, "calls", calls)
    run([ring()], logged)
    assert calls.answered == [MSG]                       # still answered: no change in behaviour
    assert_first_and_clean(logged)
    assert logged == [EVENT_LINE, f"<rpc read msg {MSG}>", f"<calls.incoming msg {MSG}>"]


def test_the_refusal_path_logs_the_event_before_the_refusal(logged, monkeypatch):
    calls = FakeCalls(logged)
    monkeypatch.setattr(bot, "calls", calls)
    monkeypatch.setattr(bot, "calls_refusal",
                        lambda starts=True: "refused: the media pack is gone (run bot install)")
    run([ring()], logged)
    assert calls.answered == []                          # still refused
    assert_first_and_clean(logged)
    assert len(logged) == 2 and logged[1].startswith(f"REFUSED incoming call in chat {CHAT}"), logged


def test_the_no_media_pack_path_logs_the_event_first(logged, monkeypatch):
    monkeypatch.setattr(bot, "calls", None)
    run([ring()], logged)
    assert_first_and_clean(logged)
    assert logged[1:] == [f"CALL event ignored (no media pack): {bot.EventType.INCOMING_CALL}"], logged


def test_the_calls_off_path_logs_the_event_first(logged, monkeypatch):
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "calls", None)
    run([ring()], logged)
    assert_first_and_clean(logged)
    assert logged[1:] == [f"CALL event ignored (calls off on this profile): {bot.EventType.INCOMING_CALL}"]


def test_an_answer_that_raises_still_has_its_event_line(logged, monkeypatch):
    monkeypatch.setattr(bot, "calls", FakeCalls(logged, raises=True))
    run([ring()], logged)
    assert_first_and_clean(logged)
    assert logged[-1].startswith("INCOMING_CALL handler failed:"), logged


def test_video_is_reported_and_a_short_event_never_stops_the_loop(logged, monkeypatch):
    """has_video as the event says it; an event without the fields logs `?`
    and the loop goes on to the next event."""
    calls = FakeCalls(logged)
    monkeypatch.setattr(bot, "calls", calls)
    short = Obj(kind=bot.EventType.INCOMING_CALL)
    run([ring(has_video=True), short, ring(msg_id=78)], logged)
    events = [line for line in logged if line.startswith("CALL EVENT")]
    assert events == [f"CALL EVENT IncomingCall msg {MSG} chat {CHAT} video=True",
                      "CALL EVENT IncomingCall msg ? chat ? video=?",
                      f"CALL EVENT IncomingCall msg 78 chat {CHAT} video=False"], logged
    assert 78 in calls.answered


def test_other_call_events_get_no_event_line(logged, monkeypatch):
    """Only the ring: an accepted, ended or answered-elsewhere call keeps
    its own line and gets no CALL EVENT."""
    monkeypatch.setattr(bot, "calls", Obj(ended=lambda mid: None, accepted=lambda ev: None))
    run([Obj(kind=bot.EventType.CALL_ENDED, msg_id=MSG),
         Obj(kind=bot.EventType.INCOMING_CALL_ACCEPTED, msg_id=MSG, from_this_device=False)], logged)
    assert not any(line.startswith("CALL EVENT") for line in logged), logged

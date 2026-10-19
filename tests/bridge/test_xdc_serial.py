"""A restart must never replay a webxdc app's history (PHONE-22, gap 2).

The bridge used to fetch status updates from XDC_SEEN.get(mid, 0), and
XDC_SEEN lives in memory, so the first tap after every restart fetched from
serial 0 and mailed the whole history: one bot replayed 48 and then 51 taps
on 09-26. It now fetches from the event's own status_update_serial - 1, and
an event with no usable serial fetches nothing.

Fakes only: no account, no rpc-server, nothing real.
"""
import pytest

from sessionphone.bridge import bot


class FakeMessage:
    """A webxdc message holding N updates, answering like the core does:
    get_webxdc_status_updates(n) returns the updates with serial > n."""

    def __init__(self, updates, chat_id=7):
        self.updates = updates
        self.chat_id = chat_id
        self.asked = []

    def get_webxdc_status_updates(self, last_known_serial=0):
        self.asked.append(last_known_serial)
        return [u for u in self.updates if u["serial"] > last_known_serial]

    def get_snapshot(self):
        return type("Snap", (), {"chat_id": self.chat_id})()


class FakeAccount:
    def __init__(self, msg):
        self.msg = msg

    def get_message_by_id(self, mid):
        return self.msg


class Event:
    """Stands in for the client's AttrDict event."""

    def __init__(self, msg_id, **fields):
        self.kind = "WebxdcStatusUpdate"
        self.msg_id = msg_id
        for k, v in fields.items():
            setattr(self, k, v)


def _updates(n, first=1):
    return [{"serial": s, "payload": {"from": "xdc", "tap": s}, "info": ""}
            for s in range(first, first + n)]


@pytest.fixture
def fresh(monkeypatch):
    """Fresh module state, as after a restart: both in-memory maps empty.
    Records every mail_in and log line instead of sending or writing.
    The sink is pinned to mail with no filter: these tests count mails, and
    bot.py reads DC_XDC_IN / DC_XDC_MATCH at import, so a shell that exports
    them (a `bot install` shell carries DC_*) would turn this suite red, or
    append these fake taps to a real xdc-in.jsonl that an applier tails."""
    monkeypatch.setattr(bot, "XDC_SEEN", {})
    monkeypatch.setattr(bot, "XDC_SENT", {})
    monkeypatch.setattr(bot, "XDC_IN", None)
    monkeypatch.setattr(bot, "XDC_MATCH", [])
    mailed, logged = [], []
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: mailed.append(a))
    monkeypatch.setattr(bot, "log", logged.append)
    return mailed, logged


@pytest.mark.parametrize("n_prior", [0, 1, 48, 20897])
def test_one_new_update_after_restart_yields_one_record(fresh, n_prior):
    mailed, _ = fresh
    new = n_prior + 1
    msg = FakeMessage(_updates(n_prior + 1))
    bot.xdc_update(FakeAccount(msg), Event(844, status_update_serial=new))
    assert len(mailed) == 1, f"{len(mailed)} records for one update ({n_prior} prior)"
    assert f'"tap": {new}' in mailed[0][3]
    assert msg.asked == [new - 1]


def test_serial_counts_across_messages_do_not_matter(fresh):
    # Serials are account-wide row ids: this message's history can sit at
    # 5..9 with the new one at 1000 (other apps took the serials between).
    mailed, _ = fresh
    msg = FakeMessage(_updates(5, first=5) + _updates(1, first=1000))
    bot.xdc_update(FakeAccount(msg), Event(844, status_update_serial=1000))
    assert len(mailed) == 1 and mailed[0][3].endswith('{"from": "xdc", "tap": 1000}')


@pytest.mark.parametrize("fields", [
    {},                                   # the field is missing altogether
    {"status_update_serial": None},
    {"status_update_serial": 0},
    {"status_update_serial": -3},
    {"status_update_serial": "not-a-number"},
    {"status_update_serial": True},
])
def test_no_usable_serial_replays_nothing(fresh, fields):
    mailed, logged = fresh
    msg = FakeMessage(_updates(51))
    bot.xdc_update(FakeAccount(msg), Event(844, **fields))
    assert mailed == []
    assert msg.asked == [], "fetched with no usable serial: that is the replay"
    assert any("no usable status_update_serial" in line for line in logged)


def test_never_fetches_from_zero_when_history_exists(fresh):
    # Every event on a message with history must ask from above 0.
    _, _ = fresh
    msg = FakeMessage(_updates(40))
    for s in range(2, 41):
        bot.xdc_update(FakeAccount(msg), Event(844, status_update_serial=s))
    assert 0 not in msg.asked


def test_updates_older_than_asked_are_dropped(fresh):
    # A core that ignored last_known_serial must still not replay history.
    mailed, _ = fresh

    class Sloppy(FakeMessage):
        def get_webxdc_status_updates(self, last_known_serial=0):
            self.asked.append(last_known_serial)
            return list(self.updates)

    msg = Sloppy(_updates(30))
    bot.xdc_update(FakeAccount(msg), Event(844, status_update_serial=30))
    assert len(mailed) == 1


def test_within_one_process_a_batch_is_not_mailed_twice(fresh):
    # Two updates land before either event is read. The first event's fetch
    # returns both; the second event must not mail its update again.
    mailed, _ = fresh
    msg = FakeMessage(_updates(12))
    acct = FakeAccount(msg)
    bot.xdc_update(acct, Event(844, status_update_serial=11))
    bot.xdc_update(acct, Event(844, status_update_serial=12))
    assert len(mailed) == 2
    assert msg.asked == [10, 12]


def test_echo_of_our_push_is_still_not_mailed(fresh):
    mailed, logged = fresh
    push = {"from": "bot", "state": "green"}
    msg = FakeMessage(_updates(3) + [{"serial": 4, "payload": push, "info": ""}])
    bot.xdc_remember(844, push)
    bot.xdc_update(FakeAccount(msg), Event(844, status_update_serial=4))
    assert mailed == []
    assert any("XDC ECHO" in line for line in logged)


def test_echo_pushed_before_restart_is_not_replayed(fresh):
    # The bot pushed #4 and died before reading its echo event. After the
    # restart XDC_SENT is empty, and the next tap is #5. The old fetch-from-0
    # mailed #1-#4 as taps, the echo among them; now only #5 is mailed.
    mailed, _ = fresh
    push = {"from": "bot", "state": "green"}
    msg = FakeMessage(_updates(3) + [{"serial": 4, "payload": push, "info": ""}]
                      + _updates(1, first=5))
    bot.xdc_update(FakeAccount(msg), Event(844, status_update_serial=5))
    assert len(mailed) == 1 and mailed[0][3].endswith('{"from": "xdc", "tap": 5}')


def test_handle_events_routes_the_event_to_xdc_update():
    # The loop is a `while True`; pin the branch to the helper by source.
    import inspect
    src = inspect.getsource(bot.handle_events)
    branch = src[src.index("EventType.WEBXDC_STATUS_UPDATE"):]
    assert "xdc_update(account, event)" in branch[:600]
    assert "since = XDC_SEEN.get(mid, 0)" not in inspect.getsource(bot)

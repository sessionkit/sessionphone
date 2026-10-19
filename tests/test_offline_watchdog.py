"""An offline bot is noticed, loudly (PHONE-98, gap 3).

A bot that is running but has no connection to its relay looks healthy: the
process is there, launchd shows it, and nothing comes in or goes out. Nothing
noticed. Now a watchdog asks the core for its connectivity; when the bot has
been offline (not connected, or stuck "connecting") past a bound it logs a
FATAL line naming why and how long, and the bot EXITS NON-ZERO, so launchd
(KeepAlive true) restarts it with a fresh rpc-server. It does not try to
reconnect in place: the restart is the reconnect.

The event thread dying is the other silent stop: the bot used to log it and
return, exiting 0. It now exits 1.

Fakes only: no account, no rpc-server, no relay, nothing real.
"""
import pytest

from sessionphone.bridge import bot

NOT_CONNECTED, CONNECTING, WORKING, CONNECTED = 1000, 2000, 3000, 4000


class FakeRpc:
    """Answers get_connectivity from a script: one value per call, the last
    one repeated. An Exception in the script is raised, as a dead rpc-server
    would."""

    def __init__(self, script):
        self.script = list(script)
        self.asked = []

    def get_connectivity(self, account_id):
        self.asked.append(account_id)
        v = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(v, Exception):
            raise v
        return v


class FakeAccount:
    def __init__(self, script, account_id=1):
        self.id = account_id
        self.manager = type("Manager", (), {})()
        self.manager.rpc = FakeRpc(script)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(bot, "log", lines.append)
    return lines


def dog(script, limit=300.0, every=10.0):
    clock = FakeClock()
    return bot.Watchdog(FakeAccount(script), limit_s=limit, every_s=every, clock=clock), clock


def run(d, clock, seconds, step=1.0):
    """Tick once a second, as the outbox loop does. -> the first exit code, or None."""
    end = clock.t + seconds
    while clock.t < end:
        code = d.tick()
        if code is not None:
            return code
        clock.t += step
    return None


def test_a_connected_bot_is_left_alone(logged):
    d, clock = dog([CONNECTED])
    assert run(d, clock, 3600) is None
    assert not [line for line in logged if "OFFLINE" in line]


def test_working_counts_as_online(logged):
    d, clock = dog([WORKING])
    assert run(d, clock, 3600) is None


@pytest.mark.parametrize("state", [NOT_CONNECTED, CONNECTING])
def test_offline_past_the_bound_exits_non_zero_loudly(logged, state):
    d, clock = dog([state], limit=300)
    start = clock.t
    code = run(d, clock, 3600)
    assert code is not None and code != 0, "an offline bot must exit non-zero, so launchd restarts it"
    assert code == bot.OFFLINE_EXIT
    assert 300 <= clock.t - start <= 300 + 10, f"exited after {clock.t - start}s, bound 300s, poll 10s"
    # loud: the first sighting is logged, and the exit names why, how long and what next
    first = [line for line in logged if line.startswith("OFFLINE")]
    assert first, "the first sighting is not logged"
    assert ("not connected" if state == NOT_CONNECTED else "connecting") in first[0].lower()
    fatal = [line for line in logged if line.startswith("FATAL")]
    assert len(fatal) == 1
    assert "OFFLINE" in fatal[0] and "300s" in fatal[0]
    assert f"exit {bot.OFFLINE_EXIT}" in fatal[0] and "launchd" in fatal[0]


def test_a_blip_shorter_than_the_bound_is_logged_and_forgiven(logged):
    # offline for ~100 s, then back, then offline again: the timer starts over
    d, clock = dog([CONNECTED] + [NOT_CONNECTED] * 10 + [CONNECTED] * 5 + [NOT_CONNECTED], limit=300)
    start = clock.t
    code = run(d, clock, 3600)
    assert code == bot.OFFLINE_EXIT
    assert any(line.startswith("ONLINE again") for line in logged)
    # the second outage began at ~160 s in, so the exit is ~300 s after THAT
    assert clock.t - start >= 160 + 300 - 10


def test_a_dead_rpc_server_counts_as_offline(logged):
    d, clock = dog([BrokenPipeError("rpc-server gone")], limit=60)
    assert run(d, clock, 3600) == bot.OFFLINE_EXIT
    assert any("BrokenPipeError" in line for line in logged if line.startswith("FATAL"))


def test_one_slow_poll_at_start_is_not_news(logged):
    # a core still "connecting" at its first poll after a start says nothing
    d, clock = dog([CONNECTING, CONNECTED])
    assert run(d, clock, 100) is None
    assert logged == []


def test_the_core_is_asked_only_every_so_often():
    d, clock = dog([CONNECTED], every=10)
    run(d, clock, 100)
    assert len(d.account.manager.rpc.asked) == 10


class Thread:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


def test_the_loop_exits_non_zero_when_the_bot_goes_offline(logged):
    d, _ = dog([NOT_CONNECTED], limit=0, every=0)
    with pytest.raises(SystemExit) as e:
        bot.alive_or_exit(Thread(True), d)
    assert e.value.code == bot.OFFLINE_EXIT != 0


def test_the_loop_exits_non_zero_when_the_event_thread_dies(logged):
    # it used to `return` out of main(): exit 0, and with KeepAlive
    # {SuccessfulExit: False}, down for good, silently
    d, _ = dog([CONNECTED])
    with pytest.raises(SystemExit) as e:
        bot.alive_or_exit(Thread(False), d)
    assert e.value.code not in (0, None)
    assert any("FATAL" in line and "event thread" in line for line in logged)


def test_the_loop_carries_on_while_all_is_well(logged):
    d, _ = dog([CONNECTED])
    assert bot.alive_or_exit(Thread(True), d) is None

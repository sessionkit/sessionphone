"""A bot run in a child process, on fakes, up to its outbox loop: the rig for
tests/test_stale_call_live.py.

Run as `python _stopped_bot.py MODE OUT`. The parent sets DC_ROOT and HOME to
tmp dirs before this starts, so every path the bot binds at import (its root,
call-live.json, bot.pid, bot.log, the home lock) is under tmp: the real
module-level paths, with no attribute patched. Everything the bot does before
and around its account is stubbed as tests/test_bot_online_as.py stubs it,
plus the steps after "online as" up to the loop. No rpc-server, relay, mail,
speech server or launchd job is touched.

MODE
  start   run main() into the outbox loop; print "ready" on its first pass,
          then keep looping (a bot that is up).
  call    the same, and on the first pass a call is up: a call session with a
          peer connection sits in the call manager, and call-live.json is
          written by the bot's own writer, as a connected call writes it.
          The fake account's end_call() writes OUT/end_call-<msg id>.

Every log line goes to OUT/log.txt, one per line, flushed.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time
import types

MODE, OUT = sys.argv[1], pathlib.Path(sys.argv[2])
CALL_ID, CHAT_ID = 5531, 12

from sessionphone.bridge import bot  # after argv: DC_ROOT is the parent's

calls = bot.calls
LOG = OUT / "log.txt"


def log(msg):
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{msg}\n")


class FakeMessage:
    def __init__(self, msg_id):
        self.msg_id = msg_id

    def end_call(self):
        (OUT / f"end_call-{self.msg_id}").write_text("ended\n")


class FakeAccount:
    def is_configured(self):
        return True

    def list_transports(self):
        return [{"addr": "bot1x2y3z@relay.example.org"}]

    def get_config(self, key):
        return {"displayname": "Sam"}.get(key)

    def start_io(self):
        pass

    def get_message_by_id(self, msg_id):
        return FakeMessage(msg_id)


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePc:
    """A peer connection that closes at once."""
    async def close(self):
        pass


ACCOUNT = FakeAccount()


def up_call():
    """A connected call, as far as the disk and the manager can tell: the
    session in the manager with a pc, and call-live.json from its writer."""
    s = calls.CallSession.__new__(calls.CallSession)
    s.manager, s.account = calls._manager, ACCOUNT
    s.msg_id, s.chat_id, s.caller = CALL_ID, CHAT_ID, "owner"
    s.dir = calls.CALLS_DIR / "call-x"
    s.pc, s.connected, s.started = FakePc(), True, time.time()
    calls._manager.sessions[CALL_ID] = s
    s._live_write()


ready = False


def drain():
    global ready
    if not ready:
        ready = True
        if MODE == "call":
            up_call()
        print("ready", flush=True)


def forever(*a, **k):
    threading.Event().wait()


def stub():
    s = bot
    s.log = log
    s.check_root = lambda: None
    s.check_stt = lambda: None
    s.seatmail.check = lambda *a, **k: None
    s.seatmail.setup = lambda *a, **k: "stub"
    s.seatmail.seat = lambda *a, **k: "stub"
    s.seatmail.signing = lambda *a, **k: ("stub", "stub")      # these three would ask the mail tools
    s.seatmail.carrier = lambda *a, **k: "stub"
    s.seatmail.id_host = lambda *a, **k: "relay.example.org"
    s.seat.have_box = lambda env, shell: s.seat.Box("stub", "stub@h", "/box")
    s.dc_env.HOST_PROBLEM = None
    s.dc_env.callers_problem = lambda *a, **k: None
    s.dc_env.callers_ignored = lambda *a, **k: None
    s.SPOOL.counts = lambda: (0, 0)
    s.SPOOL.drain = drain
    s.relink = lambda: None
    s.server_choice = lambda: ("/venv/bin/deltachat-rpc-server", "venv", "test")
    s.Rpc = FakeRpc
    s.DeltaChat = lambda rpc: types.SimpleNamespace(get_all_accounts=lambda: [ACCOUNT])
    s.keygate.migrate = lambda *a, **k: None
    s.channels.migrate = lambda *a, **k: None
    s.avatar.at_start = lambda *a, **k: None
    s.displayname.at_start = lambda *a, **k: None
    s.xdc_rt_start = lambda account: None
    s.xdc_feed_start = lambda account, listing: None
    s.listalert.ListAlert = lambda *a, **k: types.SimpleNamespace(judge=lambda listing: None, note=lambda: "")
    s.ChannelsWatch = lambda: types.SimpleNamespace(refresh=lambda: None, last=None)
    s.jobs.write_stamp = lambda *a, **k: "stub"
    s.imgtext.start = lambda *a, **k: None
    s.sweep = lambda account: None
    s.handle_events = forever           # the event thread: alive, quiet
    s.Watchdog = lambda account: types.SimpleNamespace(tick=lambda: None)
    s.speech_sweep = lambda: None
    if calls is not None:
        calls.settle_greetings = lambda display: None
        # the calls loop, without the legs (mail, mouth, ears) setup() starts;
        # its hooks as setup() sets them, so a CALL line goes to the log
        def setup(log_, transcribe, mail_in, *a, **k):
            calls._hooks = calls.Hooks(log_, transcribe, mail_in)
            calls._manager.thread.start()
        calls.setup = setup


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    assert calls is not None, "calls are off or the media pack is missing: no call can be up"
    stub()
    bot.main()

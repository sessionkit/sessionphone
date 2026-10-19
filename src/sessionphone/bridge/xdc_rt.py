"""xdc_rt - webxdc realtime: the ONE thread that makes every realtime rpc.

A webxdc app can join a realtime channel (webxdc spec, joinRealtimeChannel):
peers that have the app open get each datum at once, over iroh, and anyone not
connected at that moment gets nothing. The core's calls are
Message.send_webxdc_realtime_advertisement() and
Message.send_webxdc_realtime_data(bytes). Both block until the rpc returns, and
the first one in a process may set up the iroh endpoint and relay
(UNMEASURED).

So no realtime rpc ever runs on the outbox loop. The loop validates and gates a
`webxdc_realtime` or `webxdc_realtime_advertise` job, then hands it here. Sender
is one daemon thread that runs each handed item in order: it makes the rpc, then
writes the job's ack when the rpc returns. A slow or stuck send delays only the
items queued behind it, never a message, a tap or a call. The feed
(bridge/xdc_feed.py) hands its sends to the same thread.

A dead sender does not take the bot down. It is NOT watched by alive_or_exit.
When it dies it logs one loud line, DIED, and every item still queued, and
every later realtime or feed job, is refused with that reason. Everything
else keeps running. A restart brings realtime back.

Standard library only.
"""
from __future__ import annotations

import contextlib
import json
import queue
import threading
from collections.abc import Callable

MAX_BYTES = 128000      # per datum, after encoding (webxdc spec, joinRealtimeChannel)
DIED = "XDC FEED thread died: {why}; feeds off until restart"    # the one loud line


def encode(payload) -> bytes:
    """A job's payload as the bytes the app reads:
    JSON.parse(new TextDecoder().decode(bytes)). Compact, and UTF-8 as is."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Sender:
    """The realtime sender thread. submit(work, ack) queues WORK, a callable
    that makes the rpc and returns the ack's detail. The thread calls it, then
    writes ok with that detail to ACK. If WORK raises an Exception, the ack is
    not ok and names it, and the thread goes on to the next item.

    refusal() is why a job must not be handed here, or None: the thread died,
    `off` is set (the account has realtime off), or it was never started.

    log(msg) and ack_write(path, ok, detail) are the bot's own. Both are looked
    up when they are called, so a test may swap them."""

    def __init__(self, log: Callable[[str], None], ack_write: Callable[[object, bool, str], None],
                 name: str = "XdcRealtime"):
        self.log, self.ack_write, self.name = log, ack_write, name
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.died: str | None = None    # set once, by the thread, as it dies
        self.off: str | None = None     # set by the bot at start: why realtime is off
        self.sent = self.failed = 0

    def start(self) -> Sender:
        self.thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        """Finish what is queued, then end the thread (for tests: the bot never stops it)."""
        if self.thread is not None:
            self.q.put(None)
            self.thread.join(timeout=5)

    def refusal(self) -> str | None:
        """Why no realtime job may be handed to this sender now, or None."""
        if self.died is not None:
            return f"the realtime sender thread died ({self.died}); feeds off until restart"
        if self.off is not None:
            return self.off
        if self.thread is None:
            return "the realtime sender thread was never started"
        if not self.thread.is_alive():
            return "the realtime sender thread is not running; feeds off until restart"
        return None

    def submit(self, work: Callable[[], str], ack=None, label: str = "realtime") -> bool:
        """Queue WORK for the thread. -> True if queued. False if the sender
        cannot take it: ACK is then answered not ok, with the reason, at once."""
        with self.lock:     # _die drains under the same lock, so no item is left unanswered
            why = self.refusal()
            if why is None:
                self.q.put((work, ack, label))
                return True
        self.log(f"XDC RT REFUSED {label}: {why}")
        self.ack_write(ack, False, f"REFUSED: {why}; nothing was sent.")
        return False

    def _do(self, work, ack, label) -> None:
        try:
            detail = work()
        except Exception as e:      # noqa: BLE001 - one failed send is that job's answer, not the thread's end
            self.failed += 1
            why = f"{type(e).__name__}: {e}"
            self.log(f"XDC RT FAILED {label}: {why}")
            self.ack_write(ack, False, why)
            return
        self.sent += 1
        self.ack_write(ack, True, detail or "done")

    def _run(self) -> None:
        item = None
        try:
            while True:
                item = self.q.get()
                if item is None:
                    return
                self._do(*item)
                item = None
        except BaseException as e:      # noqa: BLE001 - say so once, refuse the rest, keep the bot up
            self._die(e, item)

    def _die(self, exc: BaseException, inflight) -> None:
        why = f"{type(exc).__name__}: {exc}"
        with self.lock:
            self.died = why
            left = [inflight] if inflight is not None else []
            while True:
                try:
                    it = self.q.get_nowait()
                except queue.Empty:
                    break
                if it is not None:
                    left.append(it)
        with contextlib.suppress(BaseException):    # the log itself may be what failed
            self.log(DIED.format(why=why))
        for _work, ack, _label in left:
            with contextlib.suppress(BaseException):
                self.ack_write(ack, False, f"REFUSED: the realtime sender thread died ({why}); "
                                           "feeds off until restart; whether it was sent is unknown.")

"""xdc_feed - the heard log, streamed live into one bound webxdc app.

A feed binds ONE webxdc msg (the Guide Trail, say) to the heard log:
VoiceMode's ``heard_<day>.jsonl``, found through ``web/turns.heard_dir()``.
Each new line becomes one realtime payload, in the three shapes the app
reads:

    kind partial, source mic             -> {"partial": TEXT}   a chunk: the app appends it
    kind turn, source mic, a real word   -> {"final": TEXT}     replaces the live line
    kind saying, agent == DC_AGENT       -> {"lines": [{"who": "agent", "name": DC_AGENT_NAME, "text": TEXT}]}

Call lines (``source: call``), other agents' sayings and noise turns are not
fed. Text is flattened and cut to 4000 characters. That cut is display only:
the app shows a live line to the owner, and no agent reads this feed. The
heard log it reads keeps every word.

A binding is made by an outbox job (``webxdc_realtime_feed``), which the bot
validates and gates first. Only a chat listed in ``DC_XDC_FEED_CHATS`` may be
fed: unset means no feed can bind. At most ``DC_XDC_FEEDS_MAX``
(default 2) at once. Bindings are written whole to ``xdc-feeds.json`` (0600)
and resumed at start, each from the END of the heard log: nothing is
replayed. A binding is dropped when its instance is deleted, after 3 failed
sends in a row, or when its msg is gone (or no longer passes the gate) at
resume.

THREADS. ``XdcFeed`` is its own daemon thread. It polls the heard files every
0.1 s and queues payloads per binding; it makes no rpc itself. Every send is
HANDED to the bot's one realtime sender thread (``xdc_rt.Sender``), at most
one in flight per binding, so the sender's queue never grows behind a slow
peer: the feed's own bounded queue sits in front of it. The event loop and
the outbox loop only bind, unbind and set a flag, each under a lock, so a
stuck send can only delay the feed.

THE QUEUE. Heard partials are CHUNKS, not snapshots. So pending partials are
joined in order, with one space, into one ``{"partial"}`` per send (up to
4000 characters, the rest waits for the next send); a ``{"final"}`` discards
the partials still pending before it, because the app replaces the live line
with it; finals and lines are never coalesced and go in order. The queue
holds at most 100 payloads (a chunk counts as one). On overflow the oldest
partial chunk is dropped first, and a final or line only when no partial is
left; drops are counted and logged at most once per 10 s. At most 10 sends
per second per feed.

ADVERTISING. A binding advertises once when made. When the app is opened and
the core reports ``WEBXDC_REALTIME_ADVERTISEMENT_RECEIVED`` for a bound msg,
the event loop only sets a flag; this thread advertises back, at most once per
60 s per msg, and skips silently otherwise.

A DEAD THREAD. This thread is NOT watched by alive_or_exit. A send that
raises is that send's failure, counted toward the 3 strikes. Any other
Exception in a poll is logged once per kind per 60 s, and the loop goes on.
Only a BaseException ends it: it logs one loud line, ``XDC FEED thread died:
<exc>; feeds off until restart``, and every later feed job is refused with
that reason. The bot keeps running; the bindings file is left for the restart.

Standard library only, plus web/turns (light: it imports no media pack).
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sessionphone.bridge import xdc_rt
from sessionphone.noise import no_real_word
from sessionphone.web import turns

FILE = "xdc-feeds.json"     # beside the outbox: each profile's bot has its own
FEEDS = ("heard",)          # what a feed can carry; one kind today
POLL_S = 0.1                # how often the heard files are read
RATE = 10.0                 # sends per second per feed, at most
QMAX = 100                  # pending payloads per feed (a partial chunk counts as one)
MAX_TEXT = 4000             # display only: characters per text, and per joined partial
STRIKES = 3                 # consecutive failed sends that drop a binding
ADVERTISE_BACK_S = 60.0     # at most one advertise-back per msg per this
DROP_LOG_S = 10.0           # a full queue is logged at most this often per feed
ERR_LOG_S = 60.0            # a failure of one kind is logged at most this often


class Refused(Exception):
    """A feed job the feed will not take; str() is the reason, for the ack."""


def chats(raw: str | None) -> frozenset[int]:
    """DC_XDC_FEED_CHATS as chat ids: comma-separated, spaces ignored, anything
    that is not an integer skipped. Unset or empty -> none, so no feed binds."""
    out = set()
    for part in (raw or "").replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return frozenset(out)


def cut(text, n: int = MAX_TEXT) -> str:
    """One line, at most N characters (the heard text may hold line breaks).
    Display only: what the app shows, never what an agent acts on."""
    return " ".join(str(text or "").split())[:n]


def payload_for(rec: dict, agent: str, name: str):
    """One heard record -> ("partial", TEXT), ("final", PAYLOAD), ("lines", PAYLOAD),
    or None when it is not fed."""
    kind, source = rec.get("kind"), rec.get("source")
    text = cut(rec.get("text"))
    if not text:
        return None
    if kind == "partial" and source == "mic":
        return ("partial", text)
    if kind == "turn" and source == "mic":
        if rec.get("final") is False or no_real_word(text):   # '.', 'Um' are not a turn
            return None
        return ("final", {"final": text})
    if kind == "saying" and str(rec.get("agent") or "").strip().lower() == agent:
        return ("lines", {"lines": [{"who": "agent", "name": name, "text": text}]})
    return None


class HeardReader:
    """Whole new lines from the last two heard day files, shaped like
    turns.TurnFollower.read(): an offset per file, a half line waits for its
    newline, a file that shrank is read again from 0, and midnight's new file
    is read from its start. It starts at the END of every file there is when
    it is made, so a binding replays nothing."""

    def __init__(self, directory: pathlib.Path,
                 files: Callable[[pathlib.Path], list[pathlib.Path]] = turns.heard_files):
        self.dir, self.files = pathlib.Path(directory), files
        self.offsets: dict[pathlib.Path, int] = {}
        for p in self.files(self.dir):
            with contextlib.suppress(OSError):
                self.offsets[p] = p.stat().st_size

    def read(self) -> list[dict]:
        out = []
        for p in self.files(self.dir):
            at = self.offsets.get(p, 0)
            try:
                if p.stat().st_size < at:      # truncated or replaced: from 0
                    at = 0
                with p.open("rb") as f:
                    f.seek(at)
                    chunk = f.read()
            except OSError:
                continue
            whole = chunk[: chunk.rfind(b"\n") + 1]
            for raw in whole.split(b"\n")[:-1]:
                at += len(raw) + 1
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
            self.offsets[p] = at
        return out


class Pending:
    """One feed's queue: bounded, partial chunks joined, finals and lines in order.
    Each item is ["partial", [chunk, ...]] or [kind, payload]."""

    def __init__(self, cap: int = QMAX):
        self.cap, self.items, self.dropped = cap, deque(), 0

    def __len__(self) -> int:
        return sum(len(v) if k == "partial" else 1 for k, v in self.items)

    def add(self, kind: str, value) -> int:
        """Queue one payload; -> how many were dropped to make room."""
        if kind == "partial":
            if self.items and self.items[-1][0] == "partial":
                self.items[-1][1].append(value)
            else:
                self.items.append(["partial", [value]])
        else:
            if kind == "final":     # the app replaces the live line: the chunks before it are moot
                self.items = deque(it for it in self.items if it[0] != "partial")
            self.items.append([kind, value])
        dropped = 0
        while len(self) > self.cap:
            for it in self.items:   # the oldest partial chunk first
                if it[0] == "partial":
                    it[1].pop(0)
                    if not it[1]:
                        self.items.remove(it)
                    break
            else:                   # no partial left: the oldest final or line
                self.items.popleft()
            dropped += 1
        self.dropped += dropped
        return dropped

    def take(self):
        """The next payload to send, or None. A run of chunks is joined, up to
        MAX_TEXT characters; what does not fit stays first in the queue."""
        if not self.items:
            return None
        kind, value = self.items[0]
        if kind != "partial":
            self.items.popleft()
            return value
        text = value.pop(0)
        while value and len(text) + 1 + len(value[0]) <= MAX_TEXT:
            text += " " + value.pop(0)
        if not value:
            self.items.popleft()
        return {"partial": text}


@dataclass(eq=False)
class Binding:
    mid: int
    chat_id: int
    msg: object
    reader: HeardReader
    pending: Pending
    since: str = ""
    failures: int = 0
    inflight: bool = False
    advertise: bool = True          # due: a binding advertises once when made
    peer: bool = False              # the app advertised; the event loop set this
    adv_at: float = float("-inf")
    sent_at: float = float("-inf")
    sent: int = 0
    said: dict = field(default_factory=dict)    # log key -> when it was last said


class XdcFeed:
    """Every feed in this bot, and the thread that pumps them.

    sender is the bot's xdc_rt.Sender. send(msg, mid, data) and
    advertise(msg, mid) make the rpc, ON THE SENDER THREAD. vet(mid) -> (msg,
    chat_id) checks a binding at resume as a bind job is checked (the msg is
    there, a webxdc app, and its chat passes the gate) and raises if not."""

    def __init__(self, *, sender: xdc_rt.Sender, send: Callable, advertise: Callable,
                 log: Callable[[str], None], path: pathlib.Path, directory: pathlib.Path,
                 agent: str, name: str, allowed: Iterable[int] = (), max_feeds: int = 2,
                 files: Callable[[pathlib.Path], list[pathlib.Path]] = turns.heard_files,
                 clock: Callable[[], float] = time.monotonic, poll: float = POLL_S,
                 rate: float = RATE, cap: int = QMAX):
        self.sender, self.send, self.advertise, self.log = sender, send, advertise, log
        self.path, self.dir, self.files = pathlib.Path(path), pathlib.Path(directory), files
        self.agent, self.name = agent.strip().lower(), name
        self.allowed, self.max = frozenset(allowed), max(0, int(max_feeds))
        self.clock, self.poll, self.gap, self.cap = clock, poll, 1.0 / rate, cap
        self.feeds: dict[int, Binding] = {}
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.halt = threading.Event()   # stop(): for tests; the bot never stops it
        self.died: str | None = None
        self.said: dict = {}

    # ---- the outbox loop's and the event loop's side: a lock and a dict ----

    def refusal(self) -> str | None:
        """Why no feed job may be taken now, or None."""
        if self.died is not None:
            return f"the realtime feed thread died ({self.died}); feeds off until restart"
        why = self.sender.refusal()
        if why is not None:
            return why
        if self.thread is None:
            return "the realtime feed thread was never started"
        if not self.thread.is_alive():
            return "the realtime feed thread is not running; feeds off until restart"
        return None

    def bound(self) -> list[int]:
        with self.lock:
            return sorted(self.feeds)

    def bind(self, mid: int, chat_id: int, msg, feed: str = "heard") -> str:
        """Bind MID (already gated, a webxdc app in CHAT_ID). -> the ack's
        detail. Raises Refused. Binding a bound msg is ok: 'already feeding'."""
        why = self.refusal()
        if why is not None:
            raise Refused(why)
        if feed not in FEEDS:
            raise Refused(f"feed {feed!r} is not one this bot has (known: {', '.join(FEEDS)})")
        if chat_id not in self.allowed:
            raise Refused(f"chat {chat_id} is not in DC_XDC_FEED_CHATS, so the heard log may not be "
                          f"read out into it (list only your own 1:1 chat)")
        with self.lock:
            if mid in self.feeds:
                return f"already feeding msg {mid}"
            if len(self.feeds) >= self.max:
                raise Refused(f"{len(self.feeds)} feeds are bound already, the most DC_XDC_FEEDS_MAX "
                              f"({self.max}) allows: msg {', '.join(map(str, sorted(self.feeds)))}; "
                              f"stop one first")
            self.feeds[mid] = self._binding(mid, chat_id, msg, time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            saved = self._save()
        self.log(f"XDC FEED msg {mid}: bound, chat {chat_id}, feed {feed}, heard log {self.dir}")
        return (f"feeding the heard log into msg {mid} (chat {chat_id}) from now on; realtime is "
                f"ephemeral: only peers with the app open get it, and nothing confirms delivery"
                + ("" if saved else "; NOT saved, so it ends at the next restart"))

    def unbind(self, mid: int, why: str = "stopped", only: Binding | None = None) -> bool:
        """Drop MID's binding. -> whether it was bound. ONLY: drop it only if it
        is still that binding (a late failure of an old one leaves a new one be)."""
        with self.lock:
            b = self.feeds.get(mid)
            if b is None or (only is not None and b is not only):
                return False
            del self.feeds[mid]
            self._save()
        self.log(f"XDC FEED msg {mid}: {why}, feed stopped ({b.sent} sent, {b.pending.dropped} dropped)")
        return True

    def stop_job(self, mid: int) -> str:
        return f"stopped feeding msg {mid}" if self.unbind(mid) else f"not feeding msg {mid}"

    def instance_deleted(self, mid: int) -> bool:
        """WebxdcInstanceDeleted: the app msg is gone."""
        return self.unbind(mid, "instance deleted")

    def peer_advertised(self, mid: int) -> bool:
        """WEBXDC_REALTIME_ADVERTISEMENT_RECEIVED: only a flag; this thread acts on it."""
        with self.lock:
            b = self.feeds.get(mid)
            if b is not None:
                b.peer = True
            return b is not None

    # ---- the bindings file ----

    def _binding(self, mid, chat_id, msg, since) -> Binding:
        return Binding(mid, chat_id, msg, HeardReader(self.dir, self.files), Pending(self.cap), since)

    def _save(self) -> bool:
        """Write every binding, whole: beside the file, then os.replace, 0600."""
        rec = {"v": 1, "feeds": [{"msg_id": b.mid, "chat_id": b.chat_id, "feed": "heard", "since": b.since}
                                 for b in sorted(self.feeds.values(), key=lambda b: b.mid)]}
        part = self.path.with_name(f"{self.path.name}.{os.getpid()}.part")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, (json.dumps(rec, indent=1) + "\n").encode("utf-8"))
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            os.replace(part, self.path)
            return True
        except OSError as e:
            with contextlib.suppress(OSError):
                part.unlink()
            self.log(f"XDC FEED bindings NOT SAVED to {self.path}: {e}")
            return False

    def load(self) -> list[dict]:
        """The saved bindings; [] when there is no file. A file that will not
        parse is logged and read as none."""
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            self.log(f"XDC FEED bindings file {self.path} unreadable ({type(e).__name__}: {e}); none resumed")
            return []
        feeds = rec.get("feeds") if isinstance(rec, dict) else None
        return [f for f in feeds if isinstance(f, dict)] if isinstance(feeds, list) else []

    def resume(self, vet: Callable[[int], tuple]) -> list[int]:
        """At start, before the thread: every saved binding, vetted again, from
        the END of the heard log. One whose msg is gone, or that no longer
        passes the gate or DC_XDC_FEED_CHATS, is dropped and said. The file is
        rewritten. -> the msg ids resumed."""
        with self.lock:
            for f in self.load():
                mid = f.get("msg_id")
                if isinstance(mid, bool) or not isinstance(mid, int) or mid in self.feeds:
                    continue
                try:
                    msg, chat_id = vet(mid)
                except Exception as e:      # noqa: BLE001 - gone, refused or broken: not resumed
                    self.log(f"XDC FEED msg {mid}: not resumed, feed stopped ({type(e).__name__}: {e})")
                    continue
                if chat_id not in self.allowed:
                    self.log(f"XDC FEED msg {mid}: not resumed, feed stopped (chat {chat_id} is not in "
                             f"DC_XDC_FEED_CHATS)")
                    continue
                if len(self.feeds) >= self.max:
                    self.log(f"XDC FEED msg {mid}: not resumed, feed stopped (DC_XDC_FEEDS_MAX is {self.max})")
                    continue
                self.feeds[mid] = self._binding(mid, chat_id, msg, str(f.get("since") or ""))
                self.log(f"XDC FEED msg {mid}: resumed, chat {chat_id}, from the end of the heard log")
            if self.path.exists() or self.feeds:
                self._save()
            return sorted(self.feeds)

    # ---- the thread ----

    def start(self) -> XdcFeed:
        self.thread = threading.Thread(target=self.run, name="XdcFeed", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        """End the thread after its current poll (for tests: the bot never stops it)."""
        self.halt.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def run(self) -> None:
        try:
            while not self.halt.is_set():
                try:
                    self.poll_once()
                except Exception as e:      # noqa: BLE001 - one bad poll is not the feed's end
                    self._once(self.said, f"poll {type(e).__name__}",
                               f"XDC FEED poll failed: {type(e).__name__}: {e}")
                self.halt.wait(self.poll)
        except BaseException as e:          # noqa: BLE001 - say so once, refuse the rest, keep the bot up
            self.died = f"{type(e).__name__}: {e}"
            with contextlib.suppress(BaseException):
                self.log(xdc_rt.DIED.format(why=self.died))

    def poll_once(self) -> None:
        """Read each binding's new heard lines, queue them, and hand at most one
        send per binding to the sender."""
        with self.lock:
            feeds = list(self.feeds.values())
        for b in feeds:
            dropped = 0
            for rec in b.reader.read():
                p = payload_for(rec, self.agent, self.name)
                if p is not None:
                    dropped += b.pending.add(*p)
            if dropped:
                self._once(b.said, "drop", f"XDC FEED msg {b.mid}: queue full ({self.cap}): {dropped} dropped "
                           f"now, {b.pending.dropped} so far, oldest partial text first", DROP_LOG_S)
            self._pump(b)

    def _pump(self, b: Binding) -> None:
        if b.inflight or self.sender.refusal() is not None:
            return
        now = self.clock()
        if b.peer and not b.advertise and now - b.adv_at < ADVERTISE_BACK_S:
            b.peer = False      # advertised back within the minute: skipped, silently
        if b.advertise or b.peer:
            b.advertise = b.peer = False
            b.adv_at = now
            self._hand(b, lambda: self.advertise(b.msg, b.mid), "advertise")
            return
        if now - b.sent_at < self.gap:
            return
        payload = b.pending.take()
        if payload is None:
            return
        data = xdc_rt.encode(payload)
        b.sent_at = now
        self._hand(b, lambda: self.send(b.msg, b.mid, data), "data")

    def _hand(self, b: Binding, rpc: Callable[[], object], what: str) -> None:
        """Hand one rpc for B to the sender thread; B has nothing else in flight
        until it returns. Its failure is counted here, not raised to the sender."""
        def work():
            try:
                rpc()
            except Exception as e:      # noqa: BLE001 - counted toward the strikes
                self._failed(b, what, e)
                return f"feed msg {b.mid}: {what} failed"
            finally:
                b.inflight = False
            b.failures = 0
            if what == "data":
                b.sent += 1
            return f"feed msg {b.mid}: {what} sent"
        b.inflight = True
        if not self.sender.submit(work, None, f"feed msg {b.mid} {what}"):
            b.inflight = False

    def _failed(self, b: Binding, what: str, e: Exception) -> None:
        b.failures += 1
        why = f"{type(e).__name__}: {e}"
        if b.failures >= STRIKES:
            self.unbind(b.mid, f"{STRIKES} sends failed in a row (last: {what}, {why})", only=b)
        else:
            self._once(b.said, f"fail {type(e).__name__}",
                       f"XDC FEED msg {b.mid}: {what} failed ({why}); {b.failures} of {STRIKES} in a row")

    def _once(self, said: dict, key: str, line: str, every: float = ERR_LOG_S) -> None:
        now = self.clock()
        if now - said.get(key, float("-inf")) >= every:
            said[key] = now
            self.log(line)

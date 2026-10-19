"""spool - a mail to the agent that fails is kept, not lost.

Before this, a refused send was logged and the message was gone: remember()
and the mark-seen had already run, so nothing ever retried it. Now bot.mail_in
hands every failed seat send (bridge/seatmail.py) to keep():

    <DC_ROOT>/undelivered/<epoch>-<n>.json    mode 0600, the dir 0700
    {"v": 1, "args": [...message's argv, the binary not included...],
     "body": "...", "created": EPOCH, "attempts": N, "next_try": EPOCH,
     "kind": ..., "sent": ..., "error": "rc=N kind=K: ...", "tries": [...]}

The env is never stored: a retry rebuilds it (seatmail.child_env), so a signer
fixed meanwhile is the one used. The message itself is still remember()ed, so
the Delta sweep never mails it again: the spool is the queue.

WHICH FAILURES ARE RETRIED: by the refusal's KIND, never its exit code. Exit 2
mixes a dead signer agent (heals when it comes back) with a refused subject
(never heals). Retried: RETRIED below. Every other kind (usage, subject,
address, header, body, thread-id, ...) is a bot bug or bad input that a retry
can only repeat: it goes to undelivered/dead/ at once, logged BRIDGE MAIL DEAD.

- No mail blocks another: each record keeps its own attempts and next_try,
  backing off from BACKOFF_S (doubling, at most BACKOFF_MAX_S). drain() sends
  every mail that is due, oldest first; one that fails again is rescheduled,
  and the rest still go.
- Except when the MTA itself is down:
  drain() runs on the outbox thread, BEFORE the agent's dc-say jobs, and a
  hung MTA costs every send seatmail.TIMEOUT_S. So a pass STOPS at the first
  refusal of a kind in STOP_KINDS (timeout, mta): that mail is rescheduled as
  any other, and the mails behind it are not tried this pass. Their next_try
  is unchanged, so they are still due on the next pass. One BRIDGE MAIL HELD
  line per pass says so. A pass therefore holds the outbox for at most one
  timeout; any other refusal (identity, exec, ...) never stops it.
- A retry can send twice: kind "mta" with sent "unknown" means the MTA started
  and then failed, so the mail may have gone. A duplicate beats a loss; the
  retry is logged with sent=unknown, so a duplicate can be explained.
- Past CAP_S (24 h) since the first failure, a mail goes to dead/. Nothing is
  ever deleted from dead/; a mail that finally goes is removed from the spool.
  A record that cannot be read goes to dead/ at once, as it was: it can never
  be sent, and the outbox loop would otherwise log it every second.
- counts() is what `dc-live` and `bin/bridge-check` report: waiting and dead.

The call mails (M5) are never spooled here: each goes under a Message-ID the
bot minted, and sessionmail has no duplicate check;
the channels alert keeps its own retry (listalert.ALERT_RETRY_S).
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from collections.abc import Callable

from sessionphone.bridge import dc_env, seatmail

# message send's kinds that heal, plus the bot's own two (seatmail: exec, timeout)
RETRIED = frozenset({"identity", "mta", "undeliverable", "exec", "timeout"})
# the MTA is down or hanging: every mail behind this one would wait as long
STOP_KINDS = frozenset({"timeout", "mta"})
BACKOFF_S = 60.0
BACKOFF_MAX_S = 3600.0
CAP_S = 24 * 3600.0
DEAD = "dead"

DIR: pathlib.Path | None = None     # tests point it at tmp; None = DC_UNDELIVERED / <DC_ROOT>/undelivered


def directory() -> pathlib.Path:
    return DIR if DIR is not None else dc_env.path("UNDELIVERED", "undelivered")


def backoff(attempts: int) -> float:
    """Seconds before the next try, after ATTEMPTS failures: 60, 120, 240 ... 3600."""
    return min(BACKOFF_S * 2 ** max(attempts - 1, 0), BACKOFF_MAX_S)


def _mkdir(d: pathlib.Path) -> pathlib.Path:
    """D, 0700 whatever the umask: it holds mail bodies. -> D."""
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if d.stat().st_mode & 0o077:
        d.chmod(0o700)
    return d


def _read(path: pathlib.Path) -> dict:
    """The record at PATH. Raises ValueError/TypeError/KeyError if it is not one
    _one() can act on: JSON, an object, args a list of strings, body a string,
    and created/next_try/attempts numbers where present."""
    rec = json.loads(path.read_text())
    if not isinstance(rec, dict):
        raise TypeError(f"a {type(rec).__name__}, not an object")
    if not isinstance(rec["args"], list) or not all(isinstance(a, str) for a in rec["args"]):
        raise TypeError("args is not a list of strings")
    if not isinstance(rec["body"], str):
        raise TypeError("body is not a string")
    # exactly as _one() reads them: attempts by int() AFTER the send and before the
    # unlink, so an "attempts" int() refuses would re-send a mail that went, every pass
    for k in ("created", "next_try"):
        if k in rec:
            float(rec[k])
    if "attempts" in rec:
        int(rec["attempts"])
    if "tries" in rec and not isinstance(rec["tries"], list):
        raise TypeError("tries is not a list")
    return rec


def _write_new(d: pathlib.Path, rec: dict, now: float) -> pathlib.Path:
    """Write REC as <epoch>-<n>.json in D, 0600, never over another: it appears whole
    (a tmp file linked into place), so a drain() on another thread never reads half."""
    _mkdir(d)
    data = json.dumps(rec, indent=1, ensure_ascii=True) + "\n"
    tmp = d / f".{os.getpid()}-{time.monotonic_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:          # a failed write (disk full) leaves no tmp behind
            fh.write(data)
        for n in range(1, 10_000):
            path = d / f"{int(now)}-{n}.json"
            try:
                os.link(tmp, path)
            except FileExistsError:
                continue
            return path
        raise FileExistsError(f"no free name for {int(now)}-N.json in {d}")
    finally:
        tmp.unlink(missing_ok=True)


def _move_free(path: pathlib.Path, d: pathlib.Path) -> pathlib.Path:
    """Move PATH into D under its own name, or the next free <epoch>-<n>.json, NEVER
    over another: keep() numbers dead/ and the spool apart, so a mail kept dead at
    once can already hold this name, and os.replace would delete it."""
    head, _, n = path.stem.partition("-")
    first = int(n) if n.isdigit() else 1
    for m in range(first, first + 10_000):
        dest = d / (path.name if m == first else f"{head}-{m}.json")
        try:
            os.link(path, dest)
        except FileExistsError:
            continue
        path.unlink()
        return dest
    raise FileExistsError(f"no free name for {path.name} in {d}")


def _rewrite(path: pathlib.Path, rec: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(rec, indent=1, ensure_ascii=True) + "\n")
    os.replace(tmp, path)


def _try(sent: seatmail.Sent, now: float) -> dict:
    return {"at": now, "kind": sent.kind, "sent": sent.sent, "error": sent.error}


class Spool:
    """The undelivered spool under ROOT (default: directory(), read when used)."""

    def __init__(self, log: Callable[[str], None], root: pathlib.Path | None = None,
                 clock: Callable[[], float] = time.time,
                 send: Callable[..., seatmail.Sent] | None = None):
        self.log, self._root, self.clock, self._send = log, root, clock, send

    @property
    def root(self) -> pathlib.Path:
        return self._root if self._root is not None else directory()

    def keep(self, sent: seatmail.Sent, body: str) -> pathlib.Path:
        """A send that failed: write it to the spool, or to dead/ if its kind never
        heals. Logs BRIDGE MAIL FAILED <error>, kept at PATH. -> the path."""
        now = self.clock()
        rec = {"v": 1, "args": list(sent.args), "body": body, "created": now, "attempts": 1,
               "next_try": now + backoff(1), "kind": sent.kind, "sent": sent.sent,
               "error": sent.error, "tries": [_try(sent, now)]}
        retried = sent.kind in RETRIED
        _mkdir(self.root)                   # the spool itself 0700, even when only dead/ is written
        path = _write_new(self.root if retried else self.root / DEAD, rec, now)
        self.log(f"BRIDGE MAIL FAILED {sent.error}, kept at {path}")
        if not retried:
            self.log(f"BRIDGE MAIL DEAD {path}: kind {sent.kind or 'unknown'} is never retried")
        return path

    def waiting(self) -> list[pathlib.Path]:
        """The spooled mails, oldest first (by name: <epoch>-<n>, numerically)."""
        def key(p: pathlib.Path):
            head, _, n = p.stem.partition("-")
            return (int(head) if head.isdigit() else 0, int(n) if n.isdigit() else 0, p.name)
        try:
            return sorted((p for p in self.root.glob("*.json") if p.is_file()), key=key)
        except OSError:
            return []

    def counts(self) -> tuple[int, int]:
        """(waiting, dead)."""
        try:
            dead = sum(1 for p in (self.root / DEAD).glob("*.json") if p.is_file())
        except OSError:
            dead = 0
        return len(self.waiting()), dead

    def _bury(self, path: pathlib.Path, rec: dict, why: str) -> None:
        rec["dead_at"], rec["dead_why"] = self.clock(), why
        _rewrite(path, rec)
        d = self.root / DEAD
        _mkdir(d)
        dest = _move_free(path, d)
        self.log(f"BRIDGE MAIL DEAD {dest}: {why}")

    def drain(self) -> None:
        """Send every mail that is due, oldest first. One that fails again is
        rescheduled (or buried); it never stops the ones behind it, unless its
        refusal's kind is in STOP_KINDS: then the MTA is down, and the pass stops
        there (the rest keep their next_try and go on a later pass), so the
        outbox thread is held for one timeout at most, never one per mail.
        A record that cannot be read can never be sent: it goes to dead/ as it
        was, once, rather than being logged on every pass (the outbox loop runs
        each second)."""
        paths = self.waiting()
        for i, path in enumerate(paths):
            try:
                rec = _read(path)
            except FileNotFoundError:
                continue
            except (ValueError, TypeError, KeyError, OverflowError) as e:  # JSONDecodeError, UnicodeDecodeError: ValueErrors; int(Infinity): Overflow
                try:
                    dest = _move_free(path, _mkdir(self.root / DEAD))
                    self.log(f"BRIDGE MAIL DEAD {dest}: unreadable record, kept as it was "
                             f"({type(e).__name__}: {e})")
                except Exception as e2:  # noqa: BLE001 - one bad record never stops the rest
                    self.log(f"BRIDGE MAIL RETRY {path}: unreadable and not moved: {type(e2).__name__}: {e2}")
                continue
            except Exception as e:  # noqa: BLE001 - one bad record never stops the rest
                self.log(f"BRIDGE MAIL RETRY {path}: {type(e).__name__}: {e}")
                continue
            try:
                sent = self._one(path, rec)
            except Exception as e:  # noqa: BLE001 - one bad record never stops the rest
                self.log(f"BRIDGE MAIL RETRY {path}: {type(e).__name__}: {e}")
                continue
            if sent is not None and not sent.ok and sent.kind in STOP_KINDS:
                rest = len(paths) - i - 1
                if rest:
                    self.log(f"BRIDGE MAIL HELD {rest} more not tried this pass: {path.name} "
                             f"got kind {sent.kind}, so the MTA looks down; they keep their "
                             f"next try and wait for the next pass")
                return

    def _one(self, path: pathlib.Path, rec: dict) -> seatmail.Sent | None:
        """Try PATH's mail if it is due. -> the send's answer, or None if nothing
        was sent (not due yet, or buried past CAP_S)."""
        now = self.clock()
        if now < float(rec.get("next_try", 0)):
            return None
        if now - float(rec.get("created", now)) >= CAP_S:
            self._bury(path, rec, f"still failing after {CAP_S / 3600:.0f} h "
                                  f"({rec.get('attempts')} tries, last: {rec.get('error')})")
            return None
        sent = (self._send or seatmail.send_args)(rec["args"], rec["body"])
        n = int(rec.get("attempts", 1)) + 1
        if sent.ok:
            path.unlink()
            self.log(f"BRIDGE MAIL SENT on try {n} (first failed "
                     f"{time.strftime('%H:%M:%S', time.localtime(float(rec.get('created', now))))}"
                     f", last sent={rec.get('sent')}): {path.name} {sent.id or ''}".rstrip())
            return sent
        rec.update(attempts=n, kind=sent.kind, sent=sent.sent, error=sent.error)
        rec.setdefault("tries", []).append(_try(sent, now))
        if sent.kind not in RETRIED:
            self._bury(path, rec, f"try {n}: kind {sent.kind or 'unknown'} is never retried ({sent.error})")
            return sent
        rec["next_try"] = now + backoff(n)
        _rewrite(path, rec)
        self.log(f"BRIDGE MAIL RETRY {n} FAILED {sent.error}; next try in "
                 f"{backoff(n):.0f}s: {path}")
        return sent

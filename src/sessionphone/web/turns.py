"""A web call's finished turns, mailed to the seat.

A Delta call's turns reach the seat as mail from the bot (calls.py
CallSession._mail_turn into bot.mail_in). A web call has no transcript of its
own: the ears do the STT on `call:N` and write each finished turn to the heard
log, and ears-mail `--to device` skips call turns. So the web server follows
the heard log for its OWN call:N and mails each finished turn, once, with the
Delta turn's headers, To the seat, Reply-To the call's box.

    turn_mail(...)  -> TurnMail   pure: one turn's mail, header by header
    TurnFollower                  one per call: reads, filters, checks echo, sends

EXACTLY ONCE: one writer (this process); start at the end (the heard files'
sizes are taken when the follower is MADE, synchronously, so build the follower
just before the call registers); key on the heard log's `seq` with a high-water
mark; a deterministic Message-ID, <call-turn-web-N-SEQ@IDHOST>; and a send that
may have gone (sent=unknown: a timeout, or an MTA that failed after the
hand-off) is never retried. A follower that fails logs once and goes quiet: the
call carries on (heard_sink's rule). It never touches the audio.

ECHO: each turn is checked, whole, against the call's own EchoGuard, at
the turn line's own time. A hit is logged with the call, the seq and the
score, and never mailed; the heard log keeps the turn.

TRANSPORT: M5, SIGNED, by seatmail's `message send
--message-id`, the one path every mail takes. It goes From the web seat (the
local part of Seat.addr) as "<Name>'s web call", under the bot's DC_SIGNER_*
key; `sessionphone web --legs` refuses to start when that key is another
seat's, or when `message` has no --message-id (seatmail.check). The ids are
minted at seatmail.id_host(), sessionmail's own host; the Reply-To keeps
Seat.host (DC_HOST, else dc_env.host_part()), the box a reply is spoken from.

RETRY: only a refusal that sent NOTHING (sent=no) of a WORLD kind
(seatmail.WORLD_KINDS: exec, identity, mta, undeliverable) is tried again, at
most TRIES times, with the same id. An input kind (usage, subject, address,
thread-id, body, header, message-id) would only fail again; sent=unknown may
have gone. Neither is retried.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple

from sessionphone.bridge import dc_env, seatmail
from sessionphone.bridge.echo import _epoch
from sessionphone.bridge.seatmail import Sent, clean_subject
from sessionphone.noise import no_real_word

POLL_S = 0.2              # a plain poll of the heard files
GRACE_S = 20.0            # the drain's bound after hang-up: ears-mail's LAG_MAX_S (15 s) and a re-decode
TRIES = 3                 # a send refused by the world (sent=no) is tried at most this often, same id
BACKOFF_S = (1.0, 2.0, 4.0)   # the wait before each retry; only the first TRIES - 1 are used
HEARD_GLOB = "heard_????-??-??.jsonl"


def one_line(text, n=400):
    """calls.one_line, imported when first used, not with this module: the
    bot's heard feed (bridge/xdc_feed.py) reads heard_dir() and heard_files()
    here on a chat-only bot, which never imports calls.py and the media pack
    it loads."""
    from sessionphone.bridge.calls import one_line as flat
    return flat(text, n)


# ---- where the heard log is, as ears-mail resolves it ------------------------

def heard_dir(flag: str | os.PathLike | None = None, environ: Mapping[str, str] | None = None) -> Path:
    """--heard-dir, else $EARS_HEARD_DIR, else $VOICEMODE_BASE_DIR/logs/conversations,
    else ~/.voicemode/logs/conversations (sv keeper/ears-mail.py, --logdir)."""
    e = os.environ if environ is None else environ
    if flag:
        return Path(flag).expanduser()
    if e.get("EARS_HEARD_DIR"):
        return Path(e["EARS_HEARD_DIR"]).expanduser()
    base = os.path.expanduser(e.get("VOICEMODE_BASE_DIR") or "~/.voicemode")
    return Path(base) / "logs" / "conversations"


def heard_files(d: Path) -> list[Path]:
    """The last two day files, re-listed on every poll so midnight's new one is read."""
    return [Path(p) for p in sorted(glob.glob(os.path.join(glob.escape(str(d)), HEARD_GLOB)))[-2:]]


# ---- who the mail is from and to ----------------------------------------------

class Seat(NamedTuple):
    """The seat a web call's turns go to, and the agent's own phone address."""
    to: str          # the seat's box: what wakes it (sessionmail's `wake to:$me`)
    addr: str        # whose seat it goes From: DC_ADDR, else <agent>-chat@HOST (its local part)
    name: str        # the From's display name: DC_AGENT_NAME, else <agent> capitalised
    host: str        # the web server's own host: the Reply-To (the ids are seatmail.id_host's)


def seat_for(to: str, host: str, environ: Mapping[str, str] | None = None) -> Seat:
    """The From for mail To `to`. <agent> is DC_AGENT, else the To's local part,
    cut at any `+`, lower-cased: the web process is not the bot's, so
    dc_env.ADDR would name whoever started the server."""
    e = os.environ if environ is None else environ
    agent = (dc_env._get(e, "AGENT") or to.split("@", 1)[0].split("+", 1)[0]).strip().lower()
    addr = dc_env._get(e, "ADDR") or f"{agent}-chat@{host}"
    name = dc_env._get(e, "AGENT_NAME") or agent.capitalize()
    return Seat(to, addr, name, host)


def named_seat(mail_to: str | None = None, environ: Mapping[str, str] | None = None,
               fqdn: str | None = None, hostname: str | None = None) -> tuple[Seat | None, str]:
    """-> (the Seat, '') for `sessionphone web --legs`, or (None, why it must refuse).

    The To is --mail-to, else DC_MAILTO, else <DC_AGENT>@<HOST>, with NO further
    fallback: dc_env.MAILTO's last one, CLAUDE_CODE_AGENT else `claude`, names
    the shell that started the server, not the seat a Delta turn wakes, and an
    address nobody reads is worse than no start.

    The host is DC_HOST, else dc_env.host_part(): the rule the mail drop uses to
    qualify a bare From. That is this machine's DNS name when it is the short
    host or a name under it (a tailnet name), else the short host, never a
    reverse-DNS name. fqdn and hostname stand in for socket.getfqdn() and
    socket.gethostname() in tests; with DC_HOST set neither is asked. When
    host_part gives no host, localhost, or a .arpa name (SESSIONMAIL_HOST can
    name one), --legs is refused even with --mail-to: the Reply-To is built
    from it. A dotless short host is a mail host and is accepted."""
    e = os.environ if environ is None else environ
    named = dc_env._get(e, "HOST")
    derived = None if named else dc_env.host_part(
        e, resolve_fqdn=None if fqdn is None else (lambda: fqdn),
        resolve_hostname=None if hostname is None else (lambda: hostname))
    host = named or derived or "localhost"
    to = (mail_to or "").strip() or dc_env._get(e, "MAILTO")
    if not to and dc_env._get(e, "AGENT"):
        to = f"{dc_env._get(e, 'AGENT').strip().lower()}@{host}"
    if not to:
        return None, ("--legs mails each finished turn of a web call to the seat, and no seat is named: "
                      "give --mail-to ADDR, or set DC_MAILTO or DC_AGENT (CLAUDE_CODE_AGENT is not taken: "
                      "it names the shell that started this server, not the seat)")
    box, _, domain = to.partition("@")
    if not box.strip() or not domain.strip():   # `--mail-to alice` would go to a local user, not the seat
        return None, (f"--legs mails each finished turn of a web call to the seat, and {to!r} is not a mail "
                      "address: name the seat's box as BOX@HOST (--mail-to, or DC_MAILTO)")
    what = None if named else dc_env.host_part_problem(derived)
    if what:
        where = next((var for var in dc_env.MAIL_HOST_VARS if (e.get(var) or "").strip()),
                     "sessionmail's mail.json `host`, else this machine's name")
        return None, (f"--legs: no DC_HOST is set and this machine's mail host (from {where}) {what}, "
                      f"so each turn's Reply-To would be built on it. Set DC_HOST to this "
                      f"machine's mail domain (DC_HOST=mac.example.net), or SESSIONMAIL_HOST to its host")
    return seat_for(to, host, e), ""


def start_lines(seat: Seat, directory: Path, environ: Mapping[str, str] | None = None) -> list[str]:
    """What `sessionphone web --legs` says at start about the turns: where they
    go, and whether they are signed and why, as seatmail.signing() rules for
    ENVIRON (the server's own, the one each turn's `message send` gets).
    A heard dir that is not there is marked (missing), never left silent: the
    follower reads nothing from it, and that is the DEMO's failure shape, turns
    that reach no one. It is still followed, so a dir the ears make later is read."""
    mode, reason = seatmail.signing(environ)
    missing = "" if Path(directory).is_dir() else (
        "  (missing: no turn reaches the seat until the ears write here; check --heard-dir)")
    return [(f"turns: each finished turn of a web call mailed to {seat.to} (From seat "
             f"{seatmail.local_part(seat.addr)}), Reply-To calls+web-<N>@{seat.host} "
             f"(M5, {mode} ({reason}))"),
            f"heard log: {directory}{missing}"]


# ---- the mail -------------------------------------------------------------------

def message_id(n: int, seq: int, host: str) -> str:
    """Deterministic: a replay of the same turn is the same message.
    host is seatmail.id_host(): `message send --message-id` takes no other."""
    return f"call-turn-web-{n}-{seq}@{host}"


def thread_id(n: int, host: str) -> str:
    """One thread per call. Deliberately NOT `call-...`: the Delta mail leg's
    chat fallback, call-(?:[a-z]+-)?(\\d+)-\\d+@, must never read a chat out of it."""
    return f"web-call-{n}@{host}"


def subject_for(n: int, caller: str, text: str) -> str:
    """bot.mail_in's Subject, the room `web` and N where the chat id would be:
    flattened, ASCII, cleaned, then cut to 69."""
    tail = f"{caller}: {text[:44]}"
    tail = " ".join(tail.split()).encode("ascii", "replace").decode("ascii")
    return clean_subject(f"[web {n}] {tail}")[:69]


class TurnMail(NamedTuple):
    """One finished web-call turn, before it is sent: what seatmail.send takes.
    msgid and thread are bare ids (no <>), minted at seatmail.id_host()."""
    to: str
    subject: str
    headers: tuple[tuple[str, str], ...]   # (name, value) in order, as --header
    msgid: str                             # --message-id
    thread: str                            # -r: In-Reply-To and References
    seat: str                              # the From's seat: Seat.addr's local part
    from_name: str                         # the From's display name
    body: str

    def send(self) -> Sent:
        """Sign and send it as the web seat. -> seatmail.Sent; never raises for a send."""
        return seatmail.send(self.to, self.subject, self.body, headers=dict(self.headers),
                             msgid=self.msgid, in_reply_to=self.thread,
                             seat=self.seat, from_name=self.from_name)


def turn_mail(n: int, seq: int, text: str, seat: Seat, caller: str) -> TurnMail:
    """One finished web-call turn as an M5 mail. Pure, given the id host.
    `caller` is who spoke it: web/names.py's one setting, handed down.

    The Delta call turn's header set (bot.mail_in on a call turn) less the
    three that would name a Delta chat that does not exist (X-Delta-Chat-Id,
    -Profile, Cc) and Supersedes (a web call mails no previews). To the seat,
    no Priority: what wakes it. Reply-To the call's box: a plain reply is
    spoken into the call."""
    text = one_line(text, 4000).strip()
    body = (
        f"(on the call) {text}\n\n---\n"
        f"call: web (call {n})\nfrom: {caller}\n\n"
        "To answer on the call:\n"
        f'  Answer with: mouth say --device call:{n} "text"\n'
        "  Or reply to this mail: the call's box speaks it.\n"
    )
    ids = seatmail.id_host()
    return TurnMail(
        to=seat.to,
        subject=subject_for(n, caller, text),
        headers=(
            ("X-Delta-Chat-Bridge", "1"),
            ("X-Delta-Chat-Kind", "call"),
            ("Reply-To", f"calls+web-{n}@{seat.host}"),
            ("Medium", "voice"),
        ),
        msgid=message_id(n, seq, ids),
        thread=thread_id(n, ids),
        seat=seatmail.local_part(seat.addr),
        from_name=f"{seat.name}'s web call",
        body=body,
    )


# ---- the follower -----------------------------------------------------------------

class TurnFollower:
    """Follows the heard log for ONE web call's `call:N` and mails each finished turn.

    Make it just BEFORE the call registers: the constructor records every heard
    file's size, and nothing below that is ever read. Then start() it on
    the server's loop; end() at hang-up, and it drains; wait() awaits that.

    caller is the name each turn's mail gives its speaker (web/names.py, handed
    down by CallDesk). echo is the call's EchoGuard (WebCall.echo), or None for no check.
    send stands in for TurnMail.send (a test's fake; None sends): it takes the
    TurnMail and answers a seatmail.Sent. clock is time.time."""

    def __init__(self, n: int, seat: Seat, directory: Path, *, caller: str, echo=None,
                 log: Callable[[str], None] = print, send: Callable[[TurnMail], Sent] | None = None,
                 clock: Callable[[], float] = time.time, poll: float = POLL_S, grace: float = GRACE_S,
                 tries: int = TRIES, backoff: tuple[float, ...] = BACKOFF_S,
                 sleep: Callable[[float], object] = asyncio.sleep):
        self.n, self.seat, self.dir, self.echo, self.caller = n, seat, Path(directory), echo, caller
        self.device = f"call:{n}"
        self.log, self.send, self.clock = log, send, clock
        self.poll, self.grace, self.tries, self.backoff, self.sleep = poll, grace, tries, backoff, sleep
        # start at the end: every heard file there is now is read from its size
        self.offsets: dict[Path, int] = {}
        for p in sorted(self.dir.glob(HEARD_GLOB)):
            try:
                self.offsets[p] = p.stat().st_size
            except OSError:
                pass
        self.seen: dict[Path, int] = dict(self.offsets)   # the furthest byte ever read, per file
        self.last_seq = 0                                  # the high-water mark
        self.ended_at: float | None = None
        self.stopped_at: float | None = None               # a listen-stopped for call:N after the end
        self.task: asyncio.Task | None = None
        self.said_no_seq = self.said_went_back = False
        self.mailed: list[str] = []                        # Message-IDs the MTA took, in order

    # ---- life

    def start(self) -> asyncio.Task:
        self.task = asyncio.ensure_future(self.run())
        return self.task

    def end(self, at: float | None = None) -> None:
        """The call ended: keep reading call:N until its listen-stopped, or the grace."""
        if self.ended_at is None:
            self.ended_at = self.clock() if at is None else at

    def finished(self) -> bool:
        return self.ended_at is not None and (
            self.stopped_at is not None or self.clock() - self.ended_at >= self.grace)

    async def wait(self, timeout: float | None = None) -> None:
        """Await the drain, bounded (CallDesk.stop()). A follower still running at
        the bound is cancelled; a send in flight is shielded and logs when it lands."""
        if self.task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self.task), self.grace + 1.0 if timeout is None else timeout)
        except TimeoutError:
            self.task.cancel()
            self.log(f"TURNS {self.device}: the drain did not finish; stopped")

    async def run(self) -> None:
        try:
            while True:
                for rec, new in self.read():
                    await self.take(rec, new)
                if self.finished():
                    break
                await self.sleep(self.poll)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never the call's problem: say so once, go quiet
            self.log(f"TURNS {self.device}: the follower stopped: {type(e).__name__}: {one_line(str(e), 200)}; "
                     "later turns of this call stay in the heard log only")

    # ---- reading

    def read(self) -> list[tuple[dict, bool]]:
        """Whole new lines from the last two heard files -> [(record, new_bytes)].
        new_bytes is False for a line read before (an offset rewound, or a file
        that shrank and was read from 0 again). A half line waits for its \\n."""
        out = []
        for p in heard_files(self.dir):
            at = self.offsets.get(p, 0)
            try:
                size = p.stat().st_size
                if size < at:               # truncated or replaced: from 0, the mark guards
                    at = 0
                with p.open("rb") as f:
                    f.seek(at)
                    chunk = f.read()
            except OSError:
                continue
            whole = chunk[: chunk.rfind(b"\n") + 1]
            for raw in whole.split(b"\n")[:-1]:
                at += len(raw) + 1
                new = at > self.seen.get(p, 0)
                if new:
                    self.seen[p] = at
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append((rec, new))
            self.offsets[p] = at
        return out

    # ---- deciding one line

    def wanted(self, rec: dict) -> bool:
        """A finished caller turn of THIS call, with words in it."""
        return (rec.get("kind") == "turn" and rec.get("source") == "call" and rec.get("device") == self.device
                and rec.get("final") is not False and bool(str(rec.get("text") or "").strip()))

    async def take(self, rec: dict, new: bool) -> None:
        if (rec.get("kind") == "event" and rec.get("event") == "listen-stopped"
                and rec.get("device") == self.device and self.ended_at is not None):
            at = _epoch(rec.get("ts"))
            if at is not None and at >= self.ended_at:   # a capture that died mid-call does not count
                self.stopped_at = at
            return
        if not self.wanted(rec):
            return
        seq = rec.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            if not self.said_no_seq:
                self.said_no_seq = True
                self.log(f"TURNS {self.device}: a turn with no integer seq is not mailed "
                         f"(said once): {one_line(str(rec.get('text')), 120)}")
            return
        if seq <= self.last_seq:
            if new and not self.said_went_back:
                self.said_went_back = True
                self.log(f"TURNS {self.device}: HEARD SEQ WENT BACK: seq {seq} after {self.last_seq} in new "
                         "lines (the ears' counter restarted?). No turn at or below the mark is mailed: "
                         "later turns of this call may reach the heard log only")
            return
        text = one_line(str(rec["text"]), 4000).strip()
        if no_real_word(text):           # '.', 'Music.', 'Um' never wake the seat
            self.last_seq = seq
            self.log(f"NOISE {self.device} seq {seq}: no real word | dropped: {one_line(text)}")
            return
        # echo: on the loop (the mouth leg changes the guard here), at the line's own time
        if self.echo is not None:
            why = self.echo.check(text, at=_epoch(rec.get("ts")))
            if why:
                self.last_seq = seq
                self.log(f"ECHO {self.device} seq {seq}: {why} | dropped: {one_line(text)}")
                return
        await self.mail(seq, text)
        self.last_seq = seq            # decided: mailed, refused or unknown

    # ---- sending

    async def mail(self, seq: int, text: str) -> None:
        mail = turn_mail(self.n, seq, text, self.seat, self.caller)
        mid = mail.msgid
        who = f"TURN {self.device} seq {seq}"
        loop = asyncio.get_running_loop()
        for i in range(max(1, self.tries)):
            fut = loop.run_in_executor(None, self._send, mail)
            try:
                sent = await asyncio.shield(fut)
            except asyncio.CancelledError:
                fut.add_done_callback(lambda f: self._landed_late(who, mid, f))
                raise
            if sent.ok:
                self.mailed.append(mid)
                self.log(f"{who} mailed to {self.seat.to} <{mid}>")
                return
            if sent.sent != "no":
                # it may have gone (a timeout, or an MTA that failed after the
                # hand-off): never twice wins, here only
                self.log(f"{who} NOT confirmed to {self.seat.to}: {sent.error} <{mid}>")
                return
            if sent.kind not in seatmail.WORLD_KINDS:
                break                   # the input's fault: the same mail would fail again
            if i + 1 < self.tries:
                try:
                    await self.sleep(self.backoff[min(i, len(self.backoff) - 1)])
                except asyncio.CancelledError:   # stopped between tries: nothing went, and say so
                    self.log(f"{who} NOT mailed to {self.seat.to}: {sent.error}, taken=no (stopped)")
                    raise
        self.log(f"{who} NOT mailed to {self.seat.to}: {sent.error}, taken=no")

    def _send(self, mail: TurnMail) -> Sent:
        """In the executor: `message send` blocks. Never raises: a send that
        could not even be tried is nothing sent, kind exec, as seatmail says it."""
        try:
            return self.send(mail) if self.send is not None else mail.send()
        except Exception as e:  # noqa: BLE001 - a fake's or our own fault: nothing went
            return Sent(False, f"rc=- kind=exec: {type(e).__name__}: {one_line(str(e), 120)}", kind="exec")

    def _landed_late(self, who: str, mid: str, f: asyncio.Future) -> None:
        """A send whose waiter was cancelled has landed: log it as mail() would."""
        if f.cancelled():
            return
        sent = f.result()          # _send never raises
        if sent.ok:
            self.mailed.append(mid)
            self.log(f"{who} mailed to {self.seat.to} (late) <{mid}>")
        elif sent.sent != "no":
            self.log(f"{who} NOT confirmed to {self.seat.to} (late): {sent.error} <{mid}>")
        else:
            self.log(f"{who} NOT mailed to {self.seat.to} (late): {sent.error}, taken=no")

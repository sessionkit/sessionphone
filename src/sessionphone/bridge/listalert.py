"""listalert - a list that refuses every send tells the owner's agent, once.

On one install a bot root had no channels.txt after the allow-list landed. Every
dc-* send exited 3 at the tools' pre-check, so no job ever reached the bot, and
nobody was told for 3.5 h. Now the BOT judges its own list at start and once
per outbox pass, jobs or not, and when the list is missing, unreadable or loose
it MAILS the owner's agent (dc_env.MAILTO: DC_MAILTO, else
<agent>@<host>). The alert is a mail, never a Delta send, so the closed gate
cannot block it. It goes by `message send`, signed by the bot's own seat,
which mints its Message-ID: the record keeps the id it printed.

The rate limit. The key is (list path, kind). One mail per key per bot start,
and again when the key changes (a new reason, or a list that was fixed and
broke again). Never one per refused job: jobs do not drive the alert, the
pass does. Across restarts a floor holds: a start does not re-mail a key the
record shows was mailed OK within ALERT_FLOOR_S, since launchd restarts an
offline bot about every 5.5 minutes (so a restart loop is
not ~11 mails an hour). A send that fails is retried every ALERT_RETRY_S; only an
ACCEPTED send spends the key.

The record, <OUTBOX>/.channels-alert, mode 0600, is what the dc-* tools read
to say on stderr whether the owner's agent was mailed (tool_line). Its `pid`
is the bot that last judged the key, never necessarily the one that sent the
mail: a start skipped by the floor re-stamps it.

This module imports no Delta Chat code: the tools import tool_line through
sessionphone.channels with whatever python they find.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import time
from collections.abc import Callable

from sessionphone.bridge import dc_env

ALERT_FLOOR_S = 3600.0      # a restart loop mails a key at most once an hour
ALERT_RETRY_S = 300.0       # a refused send is tried again after this
RECORD = ".channels-alert"  # no .json suffix: the bot's OUTBOX.glob("*.json") never takes it
KINDS = ("missing", "unreadable", "loose")

# (subject, body, headers) -> (accepted, error, the Message-ID the send minted)
Send = Callable[[str, str, "dict[str, str]"], "tuple[bool, str, str | None]"]
Log = Callable[[str], None]


def record_path(outbox: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(outbox) / RECORD


def read_record(outbox: pathlib.Path) -> dict | None:
    """The record, or None if there is none or it is not a JSON object."""
    try:
        rec = json.loads(record_path(outbox).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def write_record(outbox: pathlib.Path, rec: dict) -> None:
    """REC to the record, 0600, whole: written beside it, then os.replace. Raises OSError."""
    path = record_path(outbox)
    part = path.with_name(f"{RECORD}.{os.getpid()}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(rec, ensure_ascii=True, indent=1) + "\n").encode("ascii"))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.replace(part, path)


def pid_alive(pid: object) -> bool:
    """Is PID a running process? A PermissionError (someone else's) counts as alive."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def now() -> float:
    """The alert's clock: the epoch now. A module function, so a test can move it."""
    return time.time()


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


def fix_command(kind: str, path: pathlib.Path, owner: str | None,
                callers: pathlib.Path | None = None) -> list[str]:
    """The exact command(s) that fix KIND, first line first. The list's path is
    always named (DC_CHANNELS_FILE=...), so the line is exact for any profile or root.
    A missing list with an owner line in CALLERS also gets install's own seed,
    `sessionphone channels seed`, which reads that same owner line."""
    q = shlex.quote(str(path))
    if kind == "missing" and owner:
        cf = f"DC_CALLERS_FILE={shlex.quote(str(callers))} " if callers else ""
        return [f"DC_CHANNELS_FILE={q} sessionphone channels add owner {shlex.quote(owner)}",
                f"or, the same as install: DC_CHANNELS_FILE={q} {cf}sessionphone channels seed",
                f"and each group it posts to: DC_CHANNELS_FILE={q} sessionphone channels add NAME chat:N"]
    if kind == "missing":
        return [("sessionphone link (the first to join becomes the owner), or "
                 f"DC_CHANNELS_FILE={q} sessionphone channels add owner ADDR")]
    if kind == "loose":
        return [f"read it first (cat {q}), then chmod 600 {q}"]
    return [f"ls -l {q}: it must be a regular file that you own, mode 0600 (chmod 600 {q})"]


def _short(kind: str, path: pathlib.Path) -> str:
    return {"missing": f"no {path.name}", "loose": f"{path.name} is writable by others"}.get(
        kind, f"cannot read {path.name}")


def _ascii(text: str) -> str:
    return " ".join(text.split()).encode("ascii", "replace").decode("ascii")


def build_mail(listing, fix: list[str], *, profile: str, account: str, pid: int, started: str,
               since: str) -> tuple[str, str, dict[str, str]]:
    """-> (subject, body, headers) for LISTING's refusal. Headers ASCII, the subject 69 at most.
    From, To and Message-ID are the send's own (`message send`, the bot's seat)."""
    room = f"{profile} channels" if profile else "channels"
    subject = _ascii(f"[{room}] REFUSING EVERY SEND: {_short(listing.kind, listing.path)}")[:69]
    body = (
        f"Your Delta Chat bot {dc_env.AGENT_NAME} (profile {profile or 'default'}, {account})\n"
        "is REFUSING EVERY SEND. Nothing goes out: no dc-* send, no Desk update, no\n"
        "in-call chat text.\n\n"
        f"list:    {listing.path}\n"
        f"reason:  {listing.refusal}\n"
        f"since:   {since} (bot pid {pid}, started {started})\n\n"
        "Fix it:\n" + "".join(f"  {line}\n" for line in fix) + "\n"
        "The bot re-reads the list on every job, so no restart is needed.\n"
        "This is the only mail about it until the reason changes or the bot restarts\n"
        f"(and not again within the hour). Log: {dc_env.path('LOG', 'bot.log')}\n"
    )
    headers = {"X-Delta-Chat-Bridge": "1", "X-Delta-Chat-Alert": f"channels-{listing.kind}"}
    if profile:
        headers["X-Delta-Chat-Profile"] = profile
    return subject, body, headers


class ListAlert:
    """The bot's alert state, held beside its ChannelsWatch. judge() each pass."""

    def __init__(self, outbox: pathlib.Path, send: Send, log: Log, *,
                 owner: Callable[[], str | None] = lambda: None,
                 callers: pathlib.Path | None = None, account: str = "",
                 profile: str = "", clock: Callable[[], float] | None = None,
                 floor_s: float | None = None, retry_s: float | None = None,
                 pid: int | None = None):
        self.outbox, self.send, self.log, self.owner = pathlib.Path(outbox), send, log, owner
        self.account, self.profile, self.callers = account, profile, callers
        self.clock = clock or now
        self.floor_s = ALERT_FLOOR_S if floor_s is None else floor_s
        self.retry_s = ALERT_RETRY_S if retry_s is None else retry_s
        self.pid = os.getpid() if pid is None else pid
        self.started = _stamp(self.clock())
        self.spent: tuple | None = None     # the key mailed (or found mailed) this start
        self.failed: tuple | None = None    # the key whose last send failed...
        self.retry_at = 0.0                 # ...and when it may be tried again
        self.seen_ok = False                # a list that read OK: any later refusal is a new outage
        self.rec: dict | None = None        # what this bot last wrote to the record

    def judge(self, listing) -> None:
        """Mail the owner's agent if LISTING refuses every send and this key is not spent."""
        if not listing.refusal:
            self.spent = self.failed = None
            self.seen_ok = True
            return
        key = (str(listing.path), listing.kind)
        if key == self.spent:
            return
        now = self.clock()
        if key == self.failed and now < self.retry_at:
            return
        if not self.seen_ok and self._floor(key, now):
            return
        self._mail(listing, key, now)

    def _floor(self, key: tuple, now: float) -> bool:
        """At a start: was KEY mailed OK within the floor? Then re-stamp the record, mail nothing."""
        rec = read_record(self.outbox)
        if not rec or (rec.get("path"), rec.get("kind")) != key or rec.get("ok") is not True:
            return False
        at = rec.get("at")
        if not isinstance(at, (int, float)) or not 0 <= now - at < self.floor_s:
            return False
        rec = dict(rec, pid=self.pid, started=self.started)
        self._write(rec)
        self.spent, self.failed = key, None
        self.log(f"channels: alert for {key[1]} already mailed to {rec.get('to')} at {rec.get('ts')}, "
                 f"not again before {_stamp(at + self.floor_s)}")
        return True

    def _mail(self, listing, key: tuple, now: float) -> None:
        try:
            owner = self.owner()
        except Exception:   # noqa: BLE001 - an unreadable callers file only loses the address
            owner = None
        fix = fix_command(listing.kind, listing.path, owner, self.callers)
        since = _stamp(now)
        subject, body, headers = build_mail(listing, fix, profile=self.profile, account=self.account,
                                            pid=self.pid, started=self.started, since=since)
        msgid = None
        try:
            ok, error, msgid = self.send(subject, body, headers)
        except Exception as e:  # noqa: BLE001 - a send that raises is a failed send, not a crash
            ok, error = False, f"{type(e).__name__}: {e}"
        msgid = msgid if ok and msgid else None   # message send's own id, as it printed it
        to = dc_env.MAILTO
        if ok:
            self.spent, self.failed = key, None
            self.log(f"CHANNELS ALERT mailed to {to}: {subject} {msgid or '(no id printed)'}")
        else:
            self.failed, self.retry_at = key, now + self.retry_s
            self.log(f"CHANNELS ALERT NOT MAILED to {to}: {error}")
        self._write({"ts": since, "at": now, "path": key[0], "kind": key[1],
                     "reason": listing.refusal, "to": to, "ok": bool(ok),
                     "error": None if ok else (error or "the send failed"),
                     "message_id": msgid,
                     "pid": self.pid, "started": self.started, "fix": fix[0]})

    def _write(self, rec: dict) -> None:
        self.rec = rec
        try:
            write_record(self.outbox, rec)
        except OSError as e:
            self.log(f"CHANNELS ALERT record not written to {record_path(self.outbox)}: {e}")

    def note(self) -> str:
        """What a list-refused job's ack adds: was the owner's agent mailed, or why not."""
        rec = self.rec
        if rec and rec.get("ok"):
            return f"; the owner's agent {rec.get('to')} was mailed at {rec.get('ts')}"
        if rec:
            return f"; the owner's agent was NOT mailed: {rec.get('error')}"
        return "; the owner's agent was NOT mailed: the bot has not judged this list yet"


def tool_line(outbox: pathlib.Path, path: pathlib.Path, kind: str | None, tool: str) -> str:
    """The line a dc-* tool prints under a list-wide REFUSED: was the owner's agent
    mailed about THIS refusal (PATH, KIND), and if not, why not."""
    rec = read_record(outbox)
    no_bot = ("no running bot has reported this (no bot running, or one too old to "
              "report it: restart it)")
    if not rec:
        return f"{tool}: the owner's agent has NOT been mailed: {no_bot}."
    fix = f" Fix: {rec['fix']}" if rec.get("fix") else ""
    if (rec.get("path"), rec.get("kind")) != (str(path), kind):
        return (f"{tool}: the owner's agent has NOT been mailed about this: the bot last reported "
                f"{rec.get('kind')} for {rec.get('path')} at {rec.get('ts')}, not {kind} for {path} "
                f"(the bot reads its own list).{fix}")
    alive = pid_alive(rec.get("pid"))
    if rec.get("ok"):
        if alive:
            return (f"{tool}: the owner's agent {rec.get('to')} was mailed about this at "
                    f"{rec.get('ts')} ({rec.get('message_id')}).")
        # the mail went; say so, and that no bot is watching now
        return (f"{tool}: the owner's agent {rec.get('to')} was mailed about this at {rec.get('ts')} "
                f"({rec.get('message_id')}), but no bot is running now (pid {rec.get('pid')}): restart it.")
    if not alive:
        return f"{tool}: the owner's agent has NOT been mailed: {no_bot}.{fix}"
    return f"{tool}: the owner's agent {rec.get('to')} was NOT mailed: {rec.get('error')}.{fix}"

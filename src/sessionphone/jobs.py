"""jobs - the job keys a running bot reads, so a dc-* tool never queues one it would misread.

On one Mac the public bot PROCESS ran code from before the allow-list while the
dc-* tools on disk were newer. A `dc-say NAME` job (`{"chat": "sessionkit", ...}`)
crashed it with `KeyError: 'chat_id'`, and nothing said the tools and the bot
disagreed. A version string cannot tell them apart (many builds share one version string), so
the bot stamps the JOB KEYS it reads, and a tool checks its job against them
before it writes anything.

- KEYS: every job key this code's bot reads. A drift guard
  (tests/test_jobs.py) greps bot.py and fails until a new key is added here.
- LEGACY_KEYS: what a bot with no stamp is assumed to read (the d6529e2 set,
  from before the allow-list). So a plain numeric job to an old bot still
  goes out; only a job the old bot would misread (`chat`, `voice_message`) is
  refused.
- The stamp, <OUTBOX>/.bot, mode 0600: written by the bot at start. No .json
  suffix, so the bot's OUTBOX.glob("*.json") never takes it for a job.
- check(outbox, job): the refusal text, or None. Exit 3 and "restart the bot"
  from `python -m sessionphone.jobs check`, and nothing is queued.

Imports no Delta Chat code and no dc_env: a tool runs it with whatever python
it finds, as it runs sessionphone.channels.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

STAMP = ".bot"      # no .json suffix: the bot's OUTBOX.glob("*.json") never takes it

# Every job key bridge/bot.py reads (job["k"], job.get("k"), "k" in job,
# chat_ref(job, "k")). tests/test_jobs.py pins this set against bot.py.
KEYS = frozenset({
    "ack", "answer", "call", "call_id", "call_to", "chat", "chat_id", "delete", "dm", "file",
    "greeting", "group", "hangup", "html", "info", "invite", "members", "msg_id", "no_speak",
    "payload", "reaction", "summary", "text", "voice", "voice_message", "webxdc_update", "who",
    "avatar",       # sessionphone avatar
    "webxdc_realtime", "webxdc_realtime_advertise",    # dc-xdc-realtime
    "webxdc_realtime_feed", "feed", "stop",            # a heard feed into an app
})

# What a bot that wrote no stamp reads: bot.py at d6529e2, the last master
# before the allow-list (no `chat` NAME, no `voice_message`).
LEGACY_KEYS = frozenset({
    "ack", "answer", "call", "call_id", "call_to", "chat_id", "dm", "file",
    "greeting", "hangup", "html", "info", "invite", "msg_id", "no_speak", "payload",
    "reaction", "summary", "text", "voice", "webxdc_update", "who",
})

# Kickstart first: a bot `bot install` did not make (a hand-written launchd job,
# as in one field case) gets a SECOND launchd job for the same account from a re-run
# of install, as `channels seed`'s refusal says.
RESTART = ("restart the bot (kickstart its launchd job: `launchctl kickstart -k gui/UID/LABEL`; "
           "`sessionphone bot install` again only for a bot install made); nothing was queued")


def stamp_path(outbox: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(outbox) / STAMP


def _version() -> str:
    """Informational only: many builds share one version string."""
    try:
        from importlib.metadata import version
        return version("sessionphone")
    except Exception:   # noqa: BLE001 - not installed as a package (a checkout run)
        return "unknown"


def write_stamp(outbox: pathlib.Path, list_path: pathlib.Path | None = None,
                pid: int | None = None) -> pathlib.Path:
    """The bot's stamp: who it is and the job KEYS it reads, 0600, written whole
    (beside it, then os.replace). LIST_PATH is the channels list the bot reads,
    for a reader to see. Raises OSError."""
    path = stamp_path(outbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"pid": os.getpid() if pid is None else pid,
           "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "version": _version(), "keys": sorted(KEYS),
           "list": str(list_path) if list_path is not None else None}
    part = path.with_name(f"{STAMP}.{os.getpid()}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(rec, ensure_ascii=True, indent=1) + "\n").encode("ascii"))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.replace(part, path)
    return path


def read_stamp(outbox: pathlib.Path) -> dict | None:
    """The stamp, or None if there is none, or it is not a JSON object with a keys list."""
    try:
        rec = json.loads(stamp_path(outbox).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("keys"), list):
        return None
    return rec


def pid_alive(pid: object) -> bool:
    """Is PID a running process? A PermissionError (someone else's) counts as alive.
    (listalert.pid_alive's twin: that module imports dc_env, which this one must not.)"""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _keys(names) -> str:
    return ", ".join(f"'{k}'" for k in sorted(names))


def check(outbox: pathlib.Path, job: dict) -> str | None:
    """-> the refusal text, or None if the bot on OUTBOX reads every key in JOB.

    A stamp whose pid is alive: JOB's keys must be in its keys. No stamp, or
    its pid is dead (a bot older than the stamp, or none running): JOB's keys
    must be in LEGACY_KEYS, so a job an old bot reads as meant still goes."""
    rec = read_stamp(outbox)
    if rec is not None and pid_alive(rec.get("pid")):
        unread = set(job) - {str(k) for k in rec["keys"]}
        if not unread:
            return None
        return (f"REFUSED: the running bot (pid {rec['pid']}, started {rec.get('started', '?')}) "
                f"does not read job key(s) {_keys(unread)}: it is older than this dc-* tool. "
                f"Its stamp: {stamp_path(outbox)}. To send it, {RESTART}.")
    unread = set(job) - LEGACY_KEYS
    if not unread:
        return None
    why = (f"the bot that stamped {stamp_path(outbox)} (pid {rec['pid']}) is not running"
           if rec is not None else f"no bot has stamped {stamp_path(outbox)}")
    return (f"REFUSED: {why}, so the bot reading this outbox is too old to stamp its job "
            f"keys (or none is running), and an older bot does not read job key(s) {_keys(unread)}: "
            f"it would crash on the job, or misread it. To send it, {RESTART}.")


def main(argv: list[str] | None = None) -> int:
    """check --outbox DIR [--tool NAME] JOB_JSON

    For the dc-* tools, before a job is written. Exit 0: the bot on DIR reads
    every key in JOB_JSON. Exit 3: it would not, the refusal on stderr (it says
    "restart the bot"), and the tool queues nothing. Exit 2: usage, or JOB_JSON
    is not a JSON object.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="python -m sessionphone.jobs")
    sub = ap.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("check")
    c.add_argument("--outbox", type=pathlib.Path, required=True)
    c.add_argument("--tool", default="jobs")
    c.add_argument("job")
    a = ap.parse_args(argv)
    try:
        job = json.loads(a.job)
    except ValueError as exc:
        print(f"{a.tool}: the job is not JSON ({exc}); nothing was queued", file=sys.stderr)
        return 2
    if not isinstance(job, dict):
        print(f"{a.tool}: the job is not a JSON object; nothing was queued", file=sys.stderr)
        return 2
    refusal = check(a.outbox, job)
    if refusal:
        print(f"{a.tool}: {refusal}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

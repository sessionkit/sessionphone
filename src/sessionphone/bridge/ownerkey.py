"""ownerkey - an owner the start migration left unpinned tells the owner's agent, once.

At the first start after an update, the one-time key migration (keygate.migrate
for callers.txt, channels.migrate for channels.txt) pins the owner line to the
one key contact with its address. With none, or with two or more, it pins
nothing. From then on the owner cannot ring (callers.txt) and the bot writes
to no 1:1 chat with the owner (channels.txt). The migration said so only in
the bot's log, and it runs once, so that line was the only notice.

Now the bot reads the migration's decisions at that start. An owner line left
unpinned in either file is recorded, and the owner's agent (dc_env.MAILTO) is
mailed, by the same path as the list alert (bot.send_alert, signed by the
bot's seat), with the fix in words. Both files at one start make ONE mail.

Durable once. The record, <OUTBOX>/.owner-key-alert, mode 0600, keeps the
alert until a send is ACCEPTED. A refused send is tried again at the next
start; the migration does not run again, the record does. An accepted send
spends it, and no later start mails. At a retry each file is read again, and
a line pinned since then is dropped from the mail (all of them: no mail).
"""
from __future__ import annotations

import pathlib
import shlex
import time
from collections.abc import Callable

from sessionphone import channels, keygate
from sessionphone.bridge import dc_env, listalert

RECORD = ".owner-key-alert"  # no .json suffix: the bot's OUTBOX.glob("*.json") never takes it

Log = Callable[[str], None]


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


def _left(decisions, file: str, path: pathlib.Path) -> list[dict]:
    """The owner decisions in DECISIONS (a migration's return: a list, or None)
    that did not pin, as record items."""
    return [{"file": file, "path": str(path), "addr": d.addr, "name": d.name,
             "kind": d.kind, "line": d.line}
            for d in decisions or () if d.owner and d.kind != "pinned"]


def _items(rec: dict | None) -> list[dict]:
    """The well-formed items of a record: a hand-edited or damaged record
    drops what it cannot use, so the bot's start never stops on it."""
    got = (rec or {}).get("items")
    return [i for i in got if isinstance(i, dict)
            and i.get("file") in ("callers", "channels")
            and all(isinstance(i.get(k), str) for k in ("path", "addr", "line"))
            and isinstance(i.get("name"), (str, type(None)))] if isinstance(got, list) else []


def note(outbox: pathlib.Path, callers_path: pathlib.Path, callers_decisions,
         channels_path: pathlib.Path, channels_decisions, log: Log) -> dict | None:
    """Record a pending alert for each owner line this start's migrations left
    unpinned. A pending alert not yet sent keeps its other file's items; one
    already sent is replaced. -> the pending alert, or None if there is none.
    It is returned even when the record cannot be written, so that flush still
    mails it at this start: a write failure is logged, never silence."""
    items = (_left(callers_decisions, "callers", callers_path)
             + _left(channels_decisions, "channels", channels_path))
    if not items:
        return None
    rec = listalert.read_record(outbox, RECORD)
    files = {i["file"] for i in items}
    keep = [i for i in _items(rec)
            if i["file"] not in files] if rec and rec.get("ok") is not True else []
    now = time.time()
    pending = {"ts": _stamp(now), "at": now, "items": keep + items, "ok": False,
               "error": None, "message_id": None, "to": None}
    if not _write(outbox, pending, log):
        log("OWNER KEY ALERT not recorded: it is mailed at this start anyway, but a "
            "refused send is not tried again")
    return pending


def still_unpinned(item: dict) -> bool:
    """Is ITEM's owner line still unpinned in its file? A file that cannot be
    read counts as unpinned: better a mail than silence."""
    try:
        path = pathlib.Path(item["path"])
        if item["file"] == "callers":
            return not any(e.addr == item["addr"] and e.key for e in keygate.read_caller_entries(path))
        entry = channels.load(path, log=lambda _m: None).entries.get(item.get("name") or "")
        return entry is None or not entry.key
    except Exception:  # noqa: BLE001 - see the docstring
        return True


def fix_lines(items: list[dict]) -> list[str]:
    """The commands that fix ITEMS, each with the file it writes named, so the
    line is exact for any profile or root."""
    pins = []
    for i in items:
        q = shlex.quote(i["path"])
        if i["file"] == "callers":
            pins.append(f"DC_CALLERS_FILE={q} {keygate.PIN_HOW.format(addr=i['addr'])}")
        else:
            pins.append(f"DC_CHANNELS_FILE={q} {channels.PIN_HOW.format(name=i['name'])}")
    return pins


def relink_lines(items: list[dict], *, accounts: str = "", callers: str = "",
                 chans: str = "") -> list[str]:
    """The re-link fix: `sessionphone link`, then each pin without --key. Both
    open the account, so each line names the bot's own account folder (ACCOUNTS)
    and the link line its two list files (CALLERS, CHANS) as well: run from any
    shell, for any profile or root, they reach this bot, not another one."""
    acc = f"DC_ACCOUNTS={shlex.quote(accounts)} " if accounts else ""
    files = "".join(f"{var}={shlex.quote(val)} " for var, val in
                    (("DC_CALLERS_FILE", callers), ("DC_CHANNELS_FILE", chans)) if val)
    return ([f"{acc}{files}sessionphone link"]
            + [acc + line.replace(" --key FPR", "") for line in fix_lines(items)])


def build_mail(items: list[dict], *, profile: str, account: str, accounts: str = "",
               callers: str = "", chans: str = "") -> tuple[str, str, dict[str, str]]:
    """-> (subject, body, headers). Headers ASCII, the subject 69 at most.
    ACCOUNTS, CALLERS and CHANS are the bot's own account folder and list files,
    named in the re-link lines (relink_lines)."""
    addrs = sorted({i["addr"] for i in items})
    room = f"{profile} keys" if profile else "keys"
    subject = " ".join(f"[{room}] OWNER LOCKED OUT, no key pinned: {', '.join(addrs)}".split())
    subject = subject.encode("ascii", "replace").decode("ascii")[:69]
    what = []
    for i in items:
        if i["file"] == "callers":
            what.append(f"  {i['path']}\n    {i['addr']} cannot ring this agent: calls from the owner are refused.\n")
        else:
            what.append(f"  {i['path']}\n    {i['name']} ({i['addr']}): the bot writes to no 1:1 chat with the owner.\n")
        what.append(f"    log: {i['line']}\n")
    pins = fix_lines(items)
    relink = relink_lines(items, accounts=accounts, callers=callers, chans=chans)
    body = (
        f"Your Delta Chat bot {dc_env.AGENT_NAME} (profile {profile or 'default'}, {account})\n"
        "pinned its key files once, at its first start after an update, and could\n"
        "NOT pin the owner's key. Until a key is pinned:\n\n"
        + "".join(what) + "\n"
        "Fix it, either way:\n\n"
        "1. Pin the key by hand, from the owner's phone. FPR is the owner's key\n"
        f"   ({keygate.SHOWS}).\n"
        "   This only writes the file, so it works while the bot runs:\n"
        + "".join(f"     {line}\n" for line in pins) + "\n"
        "2. Or re-link: the owner scans the invite again (`sessionphone link`), so the\n"
        "   account holds the owner's key contact, then pin that one key without\n"
        "   --key. Both open the account, so run them while the bot is stopped:\n"
        + "".join(f"     {line}\n" for line in relink) + "\n"
        "This is the only mail about it: the migration does not run again.\n"
        f"Log: {dc_env.log_file(agent=dc_env.AGENT, profile_name=dc_env.PROFILE, root=dc_env.ROOT)}\n"
    )
    headers = {"X-Delta-Chat-Bridge": "1", "X-Delta-Chat-Alert": "owner-unpinned"}
    if profile:
        headers["X-Delta-Chat-Profile"] = profile
    return subject, body, headers


def flush(outbox: pathlib.Path, send: listalert.Send, log: Log, *, profile: str = "",
          account: str = "", accounts: str = "", callers: str = "", chans: str = "",
          pending: dict | None = None) -> None:
    """Mail the pending alert, if there is one and it is not spent. Only an
    accepted send spends it; a refused one waits for the next start. PENDING is
    what note returned at this start, mailed even if its record was not written;
    without it, the record is read. ACCOUNTS, CALLERS and CHANS: as build_mail."""
    rec = pending if pending is not None else listalert.read_record(outbox, RECORD)
    if not rec or rec.get("ok") is True:
        return
    items = _items(rec)
    if not items:
        log(f"OWNER KEY ALERT not mailed: {listalert.record_path(outbox, RECORD)} names no "
            "owner line it can read")
        return
    items = [i for i in items if still_unpinned(i)]
    now = time.time()
    if not items:
        log("OWNER KEY ALERT not mailed: the owner's key is pinned now")
        _write(outbox, dict(rec, ok=True, resolved=_stamp(now)), log)
        return
    subject, body, headers = build_mail(items, profile=profile, account=account, accounts=accounts,
                                        callers=callers, chans=chans)
    msgid = None
    try:
        ok, error, msgid = send(subject, body, headers)
    except Exception as e:  # noqa: BLE001 - a send that raises is a failed send, not a crash
        ok, error = False, f"{type(e).__name__}: {e}"
    to = dc_env.MAILTO
    if ok:
        log(f"OWNER KEY ALERT mailed to {to}: {subject} {msgid or '(no id printed)'}")
    else:
        log(f"OWNER KEY ALERT NOT MAILED to {to}: {error}; tried again at the next start")
    _write(outbox, dict(rec, items=items, ok=bool(ok), to=to, sent=_stamp(now),
                        error=None if ok else (error or "the send failed"),
                        message_id=msgid if ok else None), log)


def _write(outbox: pathlib.Path, rec: dict, log: Log) -> bool:
    """REC to the record. -> False, having logged why, if it was not written."""
    try:
        listalert.write_record(outbox, rec, RECORD)
    except OSError as e:
        log(f"OWNER KEY ALERT record not written to {listalert.record_path(outbox, RECORD)}: {e}")
        return False
    return True

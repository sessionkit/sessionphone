"""bot.pid - how `dc-live` knows a bot is running for its root.

Only the bot knows its own pid. A search of every process's argv for the
bot's name is not identity: any argv that carries the string counts (a
`tail -f` of a file named after it, a grep, another agent whose name starts
with this one's), and the foreground `sessionphone bot run` never carries it.
So the bot says who it is, in its root.

Once the bot holds the account (so a second bot, refused by the account
lock, never writes it), it writes DC_PID_FILE (<root>/bot.pid), four lines:

    12345                                       the bot's pid
    Thu Oct 22 02:19:57 2026                    its start time: ps -o lstart=
    /venv/bin/python -m sessionphone.bridge.bot claude    its command line: ps -o command=
    /Users/<you>/.sessionkit/phone/state        its root, symlinks resolved

`ps` is asked with TZ=UTC0 and LC_ALL=C, so the start time reads the same in
the bot's launchd job and in any shell. `dc-live` reads the file and says the
bot is running only if that pid is alive (kill -0), `ps` gives the same start
time and command line for it NOW, the command line names sessionphone, and
the root is the one it was asked about. A dead pid, or one the system has
given another process since (its start time differs), is no bot: a stale file
reads NOT running, so a bot killed without its clean exit leaves no lie.

At a clean exit the bot removes the file, if it still names the bot's own pid.
Writing it never stops a bot: a failure is logged, one line, and the bot runs
on (dc-live then says it is not running, which the line explains).

For a launchd bot the pid here is the one `sessionphone bot status` reads from
launchd: the job runs the bot module itself, no wrapper between.

`started` is dc-live's rule in Python: `bot install` and `bot uninstall` ask it
whether the bot behind a call-live.json still runs, and since when.
"""
import calendar
import os
import pathlib
import subprocess
import time

# ps reads the same in every environment: the start time in UTC, in C's words
PS_ENV = {"TZ": "UTC0", "LC_ALL": "C"}
# How ps is run: its own seam, bound here, so a rig that stubs subprocess.run
# to record the bot's mail calls never sees these. A test passes run=.
RUN = subprocess.run
NAME = "sessionphone"     # the bot's command line names it, in either form


def ps(pid, field, run=None):
    """`ps -o FIELD= -p PID`, stripped, as dc-live asks it. Raises OSError when
    ps gives nothing (no such process, or no ps)."""
    run = RUN if run is None else run
    r = run(["ps", "-ww", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True,
            env={**os.environ, **PS_ENV}, timeout=10, check=False)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise OSError(f"`ps -o {field}= -p {pid}` gave nothing (exit {r.returncode})")
    return out


def record(root, pid=None, run=None):
    """-> the file's text for PID (this process) on ROOT: pid, start time,
    command line, root (realpath)."""
    pid = os.getpid() if pid is None else pid
    lines = [str(pid), ps(pid, "lstart", run), ps(pid, "command", run), os.path.realpath(root)]
    return "".join(" ".join(line.split("\n")) + "\n" for line in lines)


def write(path, root, log, pid=None, run=None):
    """Write the bot.pid record at PATH, whole (a temp file renamed in), mode
    0600. -> PATH, or None when it could not be written: then LOG gets ONE
    line saying so, and the caller carries on. Never raises."""
    path = pathlib.Path(path)
    part = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        text = record(root, pid, run)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(part, path)
        return path
    except Exception as e:      # a pid file never stops the bot
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        log(f"bot.pid NOT WRITTEN at {path}: {e}; dc-live will say this bot is not running")
        return None


def owner(path):
    """-> the pid the file at PATH names, or None (no file, or no pid in it)."""
    try:
        first = pathlib.Path(path).read_text(encoding="utf-8").split("\n", 1)[0].strip()
    except OSError:
        return None
    return int(first) if first.isdigit() else None


def clear(path, pid=None):
    """At a clean exit: remove PATH if it still names PID (this process).
    A file another bot has written since is left alone. -> removed or not.
    Never raises."""
    pid = os.getpid() if pid is None else pid
    if owner(path) != pid:
        return False
    try:
        pathlib.Path(path).unlink()
        return True
    except OSError:
        return False


# >>> live-call-guard
def started(path, root, run=None):
    """dc-live's rule (bin/dc-live, dc_bot_why), in Python: -> (start, why).
    START is the bot's start time, seconds since the epoch, when the file at
    PATH names ROOT's running bot: that pid is alive, `ps` gives the same
    start time and command line for it NOW, the command line names
    sessionphone, and the root line is ROOT (realpath). Then WHY is "".
    Otherwise START is None and WHY says why not, in words. Never raises."""
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").split("\n")
    except OSError:
        return None, f"no {path}: no bot has started on this root"
    pid, lstart, cmd, where = (lines + ["", "", "", ""])[:4]
    pid = pid.strip()
    if not pid.isdigit() or pid.startswith("0") or pid == "1":
        return None, f"{path} names no bot's pid ({pid or 'nothing'})"
    try:
        os.kill(int(pid), 0)
    except OSError:
        return None, f"{path} is stale: pid {pid} is not running"
    try:
        now = ps(pid, "lstart", run)
    except (OSError, subprocess.SubprocessError):
        now = ""
    if not lstart or now != lstart:
        return None, f"{path} is stale: pid {pid} is another process now (started {now or '?'}, not {lstart or '?'})"
    try:
        now = ps(pid, "command", run)
    except (OSError, subprocess.SubprocessError):
        now = ""
    if not cmd or now != cmd:
        return None, f"{path} is stale: pid {pid} runs another command now"
    if NAME not in cmd:
        return None, f"{path} names pid {pid}, which is no sessionphone bot ({cmd})"
    if where != os.path.realpath(root):
        return None, f"{path} is another root's: its bot runs on {where or '?'}, not {os.path.realpath(root)}"
    try:   # ps's lstart in C's words and UTC (PS_ENV); a day under 10 is space-padded
        return float(calendar.timegm(time.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y"))), ""
    except ValueError:
        return None, f"{path}: its start time {lstart!r} cannot be read"
# <<< live-call-guard

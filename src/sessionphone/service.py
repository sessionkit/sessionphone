"""The resident bot as this user's launchd job: install, uninstall, status.

Calls need a process that is always there: a phone rings for about two
minutes, and the invite handshake needs a live peer. So the bot runs under
launchd, not inside a Claude Code session. The job runs the interpreter
this CLI runs under (the plugin's own venv, which `uv run` built), so a
plugin update means running `sessionphone bot install` again. It runs that
interpreter by its role name, <venv>/libexec/session-phone, so ps and top
show "session-phone", not "Python" (procname.py).
"""
from __future__ import annotations

import os
import pathlib
import plistlib
import shlex
import subprocess
import sys
import time

from sessionphone import channels, displayname, paths, seat, settings
from sessionphone.bridge import dc_env, seatmail, stt
from sessionphone.procname import named_python

# every launchd label the kit ships is dev.sessionkit.<package>.<job>: the kit's label scheme
LABEL_PREFIX = "dev.sessionkit.phone.bot"
# the label sessionphone 0.1.0 installed under: read ONLY to find and retire that
# one old job for the same agent (install, uninstall, status). Never written.
LEGACY_LABEL_PREFIX = "com.sessionphone.bot"
# the legacy plist's EnvironmentVariables keys install ALWAYS regenerates from
# this host, so a differing value there is no carried setting moving (the
# carry-over guard). Every other key, dropped or changed, refuses the install.
REGENERATED_KEYS = {
    # launchd's bare PATH: always BASE_PATH, where the job finds its tools
    "PATH": "always written as BASE_PATH, the job's tool lookup",
}
# NOT exempt, on purpose: DC_EARS_SRC picks WHICH ears code the bot imports, and
# an operator may hand-set it (a checkout), so a changed one refuses like any key.
# launchd starts jobs with a bare PATH: no Homebrew (ffmpeg), no ~/.local/bin
BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# where `uv tool install` puts a tool's shims, in uv's own order:
# UV_TOOL_BIN_DIR, XDG_BIN_HOME, XDG_DATA_HOME/../bin, ~/.local/bin
UV_BIN_DIRS = ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME")
EARS_SHIM = "ears"
EARS_PROBE_S = 10.0
# install: how long to wait for a booted-out job to go, how many bootstraps to
# try, and how long a bootstrapped job has to show up in `launchctl print`
BOOTOUT_WAIT_S = 10.0
BOOTSTRAP_TRIES = 3
VERIFY_WAIT_S = 3.0
POLL_S = 0.2
# the clock and the sleep, as names the tests can replace
_clock = time.monotonic
_sleep = time.sleep
# sessionmail's MAIL-ROUTE settings, carried into the job from the installing
# shell, because the environment is sessionmail's one seam today (its setup says
# to set SESSIONMAIL_SENDMAIL=drop "in every agent's starting environment
# (launchd services included)"), and launchd gives a job no other environment.
# Why a named allowlist: these say HOW and WHERE this host's mail goes; the other
# SESSIONMAIL_* are the installing SEAT's (FROM_NAME, SUBSCRIBE, PAGE_RULES,
# PAGER_REQUIRED ...) and must not reach the bot.
# Each was read in sessionmail 148dd21: SESSIONMAIL_SENDMAIL picks the carrier
# (_drop.selected, _sender); RELAY_DOMAIN is general delivery's relay
# (_sender.preflight); HOME is the host's seat-files root (_seat.home); ROOT is
# the maildir root the drop writes to (_drop.mail_root); HOST is the sender's
# host (_drop, _sender, and our own seatmail.id_host). None is seat-scoped.
# NOT carried: SESSIONMAIL_ORIGIN (ORIGIN_VAR). It is the domain a signature is
# made on, and the reader verifies with its own: a value only the bot's job
# holds makes every mail verify bad. Signer and reader on one host read the
# same settings, so it needs no pin; a wanted one goes in sessionmail's
# settings key postfix.origin, which both sides read.
MAIL_KEYS = ("SESSIONMAIL_SENDMAIL", "SESSIONMAIL_RELAY_DOMAIN",
             "SESSIONMAIL_HOME", "SESSIONMAIL_ROOT", "SESSIONMAIL_HOST")
ORIGIN_VAR = "SESSIONMAIL_ORIGIN"
ORIGIN_NOT_CARRIED = (f"{ORIGIN_VAR} is set in this shell and NOT carried: a value the reader "
                      "lacks makes every mail verify bad; set it in sessionmail's settings key "
                      "postfix.origin, which both sides read")


def agent() -> str:
    """Who the bot speaks for, picked as install picks it (dc_env.agent_from):
    DC_AGENT, else this session's durable seat, else the one bot phone.json
    records, else "claude"."""
    return dc_env.agent_from()[0]


def label(name: str | None = None) -> str:
    return f"{LABEL_PREFIX}.{name or agent()}"


def plist_path(name: str | None = None) -> pathlib.Path:
    return pathlib.Path.home() / "Library" / "LaunchAgents" / f"{label(name)}.plist"


def legacy_label(name: str | None = None) -> str:
    """The label sessionphone 0.1.0 gave this agent's job: the one exact old
    form install and uninstall retire, never a pattern."""
    return f"{LEGACY_LABEL_PREFIX}.{name or agent()}"


def legacy_plist_path(name: str | None = None) -> pathlib.Path:
    """The old job's plist, beside the new one in the same LaunchAgents."""
    return plist_path(name).with_name(f"{legacy_label(name)}.plist")


def uv_bin_dir() -> pathlib.Path:
    """The directory `uv tool install` links its shims into, read from this
    shell the way uv reads it."""
    for k in UV_BIN_DIRS:
        if os.environ.get(k):
            return pathlib.Path(os.environ[k])
    if os.environ.get("XDG_DATA_HOME"):
        return pathlib.Path(os.environ["XDG_DATA_HOME"]).parent / "bin"
    return pathlib.Path.home() / ".local" / "bin"


def ears_src() -> str | None:
    """The site-packages of an ears installed as a uv tool, for the job's
    DC_EARS_SRC; None if there is none.

    The bot runs the plugin venv's interpreter, and a uv tool is a venv of its
    own, so the bot cannot import it: PATH does not help (imports read
    sys.path), and PYTHONPATH puts the tool's compiled dependencies (numpy)
    ahead of the bot's, which crashes the bot when the two venvs run different
    Pythons. DC_EARS_SRC is added to sys.path by bridge/calls.py after the
    bot's own imports. The tool is found by its shim, not by its name (the
    distribution is `sessionvoice`, not `ears`): the shim in uv's bin dir is a
    link to <tool>/bin/ears, beside the tool's own python, which names its
    site-packages. A shim that is no link, or a site-packages with no ears
    package in it, carries nothing."""
    shim = uv_bin_dir() / EARS_SHIM
    if not shim.is_symlink():
        return None
    python = shim.resolve().parent / "python"
    if not python.exists():
        return None
    try:
        r = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                           capture_output=True, text=True, timeout=EARS_PROBE_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    site = r.stdout.strip()
    if r.returncode != 0 or not site or not (pathlib.Path(site) / "ears").is_dir():
        return None
    return site


def carried_mail(environ=None) -> dict[str, str]:
    """The SESSIONMAIL_* settings the job carries: each of MAIL_KEYS set, and
    not empty, in ENVIRON (this shell). Nothing else is carried by this rule:
    no seat setting, and not the legacy SESSION_MAIL_* spelling."""
    e = os.environ if environ is None else environ
    return {k: e[k] for k in MAIL_KEYS if e.get(k)}


def mail_line(env, shell=None, probe=None) -> str:
    """What `bot install` says of the job's mail, judged on ENV, the job's own
    environment read back from its plist: which SESSIONMAIL_* it carried (by
    name; SESSIONMAIL_SENDMAIL with its value), and the carrier the bot's
    `message send` will use. With SESSIONMAIL_SENDMAIL unset it says how to get
    the mail drop, since a Mac's default MTA (seatmail.DEFAULT_MTA) is always
    there but sends nowhere without a configured postfix. When SHELL (the
    installing shell) sets SESSIONMAIL_ORIGIN, it says that was not carried,
    and where it belongs: a statement, never a refusal.
    PROBE is settings.mail_file's answer: when the installed sessionmail reads
    its own mail.json, a job that carries no SESSIONMAIL_SENDMAIL sends by the
    carrier sessionmail reads there (or its default), and the line says so.
    The carrier's words are seatmail.carrier_said's, the bot's own start line's."""
    carried = carried_mail(env)
    names = [f"{k}={v}" if k == seatmail.CARRIER_VAR else k for k, v in sorted(carried.items())]
    said = f"carried {', '.join(names)}" if names else "carried no SESSIONMAIL_* setting"
    mail_file = settings.shown(settings.MAIL_FILE)
    # the one speaker for the carrier; the advice around it is install's own
    kind, words = seatmail.carrier_said(env, probe)
    if kind == seatmail.NOT_KNOWN:
        line = f"{said}; the bot's mail carrier is {words}. Fix that file, then run `{INSTALL}` again"
    elif kind == seatmail.NOWHERE:
        line = f"{said}; the bot's mail goes {words}. Fix it, then run `{INSTALL}` again"
    else:
        line = f"{said}; the bot's mail goes by {words}"
        filed = (probe or {}).get(seatmail.CARRIER_KEY)
        if seatmail.CARRIER_VAR in carried:
            pass                        # the variable wins: sessionmail never reads the file
        elif filed is None:
            line += (f". With no MTA configured (no postfix) that mail reaches no one: export "
                     f"{seatmail.CARRIER_VAR}={seatmail.DROP}, then run `{INSTALL}` again")
        elif filed[0] != seatmail.DROP:
            line += (f". With no MTA configured (no postfix) that mail reaches no one: write "
                     f'"sendmail": "{seatmail.DROP}" in {mail_file}')
    if shell is not None and shell.get(ORIGIN_VAR):
        line += f". {ORIGIN_NOT_CARRIED}"
    return line


def job(name: str | None = None, python: str | None = None, given=None) -> dict:
    """The launchd job, as a dict. DC_* set in GIVEN are carried into it, so
    `DC_MESSAGE=... sessionphone bot install` configures the bot, and so are
    the mail-route SESSIONMAIL_* (MAIL_KEYS, carried_mail), so
    `SESSIONMAIL_SENDMAIL=drop sessionphone bot install` makes the bot's
    `message send` use the mail drop on a Mac with no MTA. Carried, never
    chosen here: the carrier is sessionmail's to pick.
    GIVEN is what install resolved (settings.resolve: this shell's values
    over the settings file's), else this shell; its SESSIONPHONE_ROOT is the
    root.
    An ears installed as a uv tool is carried as DC_EARS_SRC (ears_src), unless
    GIVEN sets DC_EARS_SRC itself."""
    name = name or agent()
    if given is None:
        given, root = os.environ, paths.root()
    else:
        root = pathlib.Path(given.get("SESSIONPHONE_ROOT") or paths.DEFAULT_ROOT).expanduser()
    env = {"PATH": BASE_PATH, "SESSIONPHONE_ROOT": str(root), "DC_AGENT": name,
           "PYTHONUNBUFFERED": "1"}
    env.update(carried_mail(given))
    env.update({k: v for k, v in given.items() if k.startswith("DC_") and v})
    env["DC_AGENT"] = name
    if "DC_EARS_SRC" not in env:
        src = ears_src()
        if src:
            env["DC_EARS_SRC"] = src
    log = str(root / "bot.launchd.log")
    return {
        "Label": label(name),
        # the agent in argv, so `ps` tells two bots apart (dc-live reads the bot's bot.pid)
        "ProgramArguments": [python or sys.executable, "-m", "sessionphone.bridge.bot", name],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        # restart it whenever it exits, CLEAN EXITS TOO: {SuccessfulExit: False}
        # left a bot that exited 0 down for good, and nothing said so. The bot
        # exits non-zero when it is offline too long (bot.py Watchdog), and
        # launchd's restart is its reconnect. ThrottleInterval keeps a bot that
        # refuses to start (exit 2: no account yet, an unsafe root, a junk mail
        # host) from spinning: one try every 30 s, each one logging its refusal
        # to bot.log.
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def domain() -> str:
    return f"gui/{os.getuid()}"


# the seat step's refusals (seat.py), here beside install's others
NoSigner = seat.NoSigner         # no usable key: exit 2, naming the steps
SeatFailed = seat.SeatFailed     # `message seat agent --install` failed: exit 1, naming the recovery
NoBox = seat.NoBox               # the agent's box is not on this host: exit 2


class NoMailHost(Exception):
    """The job would mail <agent>@<junk host>: set DC_HOST. Exit code 2."""


class NoSTTModel(Exception):
    """The job's DC_STT_URL names no model: its kind is unknown or has no
    default, and DC_STT_MODEL is unset. The bot would refuse to start
    (bot.check_stt), so install refuses first. Exit code 2, naming both
    settings."""


class NoCallers(Exception):
    """Nobody may ring the bot: no address in the callers file, and no --owner.
    Or DC_CALLERS is set, which grants nothing. Exit code 2, naming the
    file."""


class NoChannels(Exception):
    """The bot could send nowhere: channels.txt is missing or empty and there
    is no owner to seed it from (no --owner, no owner line in the callers
    file), or the file is there but the bot would refuse it. Exit code 2,
    naming the file."""


class LegacyCarryOver(Exception):
    """The legacy job's plist carries a setting the new job would drop or
    change: a rename must never silently move a bot's calls dir or its
    recording switch. Refused before anything is written. Exit code 2, naming
    each key old -> new and the fix."""


class LegacyStillLoaded(RuntimeError):
    """The legacy job was booted out but launchd still lists it after
    BOOTOUT_WAIT_S: the new job is not started beside it (never two bots on one
    account). Exit code 1, saying what is on disk and to re-run install."""


class RivalStillLoaded(RuntimeError):
    """A bot --replace retires was booted out but launchd still lists it after
    BOOTOUT_WAIT_S: the new job is not started beside it (never two bots on one
    account). Exit code 1, saying what is on disk and to re-run install."""


class NotLoaded(RuntimeError):
    """`bot install` wrote the plist but launchd does not have the job loaded,
    so the bot is not running. Never reported as installed."""


def _loaded(target: str) -> bool:
    return _launchctl("print", target).returncode == 0


def _wait(target: str, loaded: bool, wait_s: float) -> bool:
    """Poll `launchctl print` until the job's loaded state is `loaded`, for
    at most wait_s. -> whether it got there."""
    deadline = _clock() + wait_s
    while True:
        if _loaded(target) == loaded:
            return True
        if _clock() >= deadline:
            return False
        _sleep(POLL_S)


def _bootstrap(p: pathlib.Path, target: str) -> None:
    """Bootstrap the plist once the old job has gone, then check that it is
    loaded. Raises NotLoaded, with launchctl's reason, if it is not.

    bootout returns before launchd has removed the job, and a bootstrap in
    that window fails "5: Input/output error" (or races, leaving nothing
    loaded). So: wait, bounded, for `print` to stop finding the old job; then
    bootstrap, retrying error 5 with a growing pause; then VERIFY.
    The same wait lives in sessionvoice's ears/services.py:154 (_bootstrap);
    it is the shared piece to adopt when it is shared, not copied here."""
    _wait(target, loaded=False, wait_s=BOOTOUT_WAIT_S)
    why = ""
    for attempt in range(1, BOOTSTRAP_TRIES + 1):
        r = _launchctl("bootstrap", domain(), str(p))
        if r.returncode == 0:
            why = ""
            break
        why = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
        if r.returncode != 5 or attempt == BOOTSTRAP_TRIES:
            break
        _sleep(float(attempt))      # 1 s, then 2 s: the window can outlast print's answer
    if why:
        raise NotLoaded(f"{target} is NOT loaded: launchctl bootstrap {domain()} {p} failed"
                        f" ({attempt} {'try' if attempt == 1 else 'tries'}): {why}."
                        " The bot is not running. Run `sessionphone bot install` again.")
    if not _wait(target, loaded=True, wait_s=VERIFY_WAIT_S):
        raise NotLoaded(f"{target} is NOT loaded: launchctl bootstrap answered 0, but"
                        f" `launchctl print {target}` does not find the job."
                        " The bot is not running. Run `sessionphone bot install` again.")


def carry_over(legacy_env: dict, new_env: dict) -> list[tuple[str, str, str | None]]:
    """Every key of LEGACY_ENV (the old plist's EnvironmentVariables) that the
    new job's NEW_ENV drops or gives another value, except REGENERATED_KEYS:
    -> [(key, old, new or None)], sorted by key."""
    return [(k, str(v), str(new_env[k]) if k in new_env else None)
            for k, v in sorted(legacy_env.items())
            if k not in REGENERATED_KEYS and (k not in new_env or str(new_env[k]) != str(v))]


def _carried(key: str) -> bool:
    """Would `job()` carry KEY from the installing shell? DC_*, MAIL_KEYS, and
    SESSIONPHONE_ROOT (paths.root() reads it), so an old root is kept by
    re-running with it in the env, never by uninstalling first."""
    return key.startswith("DC_") or key in MAIL_KEYS or key == "SESSIONPHONE_ROOT"


def check_carry_over(name: str, new_env: dict) -> None:
    """Refuse (LegacyCarryOver) when the legacy job's plist holds a setting the
    new job would drop or change, naming each key old -> new and the fix.
    No legacy plist: nothing to judge here (check_legacy_orphan asks launchd).
    An unreadable one refuses: what it carries cannot be known. Reads files
    only, never launchctl."""
    old = legacy_plist_path(name)
    if not old.exists():
        return
    retire = (f"or, if the change is meant, `sessionphone bot uninstall` first (it removes "
              f"{legacy_label(name)}), then `{INSTALL}`.")
    try:
        with open(old, "rb") as fh:
            legacy_env = plistlib.load(fh).get("EnvironmentVariables") or {}
        if not isinstance(legacy_env, dict):
            raise TypeError("EnvironmentVariables is not a dictionary")
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException, AttributeError) as exc:
        raise LegacyCarryOver(
            f"the legacy launchd job {legacy_label(name)} has a plist {old} that could not be "
            f"read ({exc}), so what it carries is not known and nothing was written. Fix or "
            f"remove it, {retire}") from exc
    moved = carry_over(legacy_env, new_env)
    if not moved:
        return
    lines = []
    for k, was, now in moved:
        how = "dropped" if now is None else repr(now)
        fix = ("the install name: see the last line" if k == "DC_AGENT"
               else f"re-run with {k}={shlex.quote(was)} in the env" if _carried(k)
               else ("install no longer carries it: a value the reader lacks makes every mail "
                     "verify bad; set it in sessionmail's settings key postfix.origin")
               if k == ORIGIN_VAR
               else f"install never carries {k}")
        lines.append(f"  {k}: {was!r} -> {how} ({fix})")
    agent_was = next((was for k, was, _ in moved if k == "DC_AGENT"), None)
    if agent_was is not None:
        # job() always sets DC_AGENT to the install name, so the env cannot keep the old
        # one; the name is install's pick, DC_AGENT first (cli: no name option).
        # A DC_AGENT=<old> install alone would leave this legacy job running beside it.
        # Both ways uninstall first, which removes the plist this guard compares, so each
        # install line carries the other moved keys install takes from the shell.
        keep, env = agent_was.strip().lower(), "".join(
            f"{k}={shlex.quote(was)} " for k, was, _ in moved if k != "DC_AGENT" and _carried(k))
        lost = [k for k, _, _ in moved if not _carried(k)]
        fix = (f"This legacy job's DC_AGENT is {agent_was}, not {name}: install as {keep} "
               f"(`sessionphone bot uninstall` first, it removes {legacy_label(name)}, then "
               f"`DC_AGENT={shlex.quote(agent_was)} {env}{INSTALL}`: install's name is "
               f"DC_AGENT, else this session's seat, else the one bot phone.json records, "
               f"else claude), or `sessionphone bot "
               f"uninstall` first to accept the change, then `{env}{INSTALL}`."
               if keep and keep != name else
               f"This legacy job's DC_AGENT is {agent_was!r}, which install keeps as no name "
               f"but {name} (it lowercases the name; an empty one is no name): `sessionphone "
               f"bot uninstall` first (it removes {legacy_label(name)}), then `{env}{INSTALL}`.")
        if lost:
            fix += f" Either way {', '.join(lost)} is dropped: install never carries it."
    elif all(_carried(k) for k, _, _ in moved):
        fix = f"Re-run `{INSTALL}` with the old values in the env, {retire}"
    else:   # a key install never carries cannot be kept by the env: only dropped, on purpose
        fix = (f"A key install never carries cannot be kept by the new job: if dropping it is "
               f"meant, `sessionphone bot uninstall` first (it removes {legacy_label(name)}), "
               f"then `{INSTALL}`.")
    raise LegacyCarryOver(
        f"the legacy launchd job {legacy_label(name)} ({old}) carries "
        f"{len(moved)} setting{'s' if len(moved) != 1 else ''} the new job "
        f"{label(name)} would drop or change, so nothing was written and nothing was "
        "booted out:\n" + "\n".join(lines) + "\n" + fix)


def check_legacy_orphan(name: str) -> None:
    """Refuse (LegacyCarryOver) when launchd has the legacy job loaded but its
    plist is gone: its env cannot be compared, so the carry-over guard cannot
    clear it. Asks launchd, so install runs it last, just before its first
    write: a refusal that comes earlier still makes no launchctl call."""
    if legacy_plist_path(name).exists() or not _loaded(f"{domain()}/{legacy_label(name)}"):
        return
    raise LegacyCarryOver(
        f"legacy job {legacy_label(name)} is loaded but its plist is gone: its env cannot be "
        f"compared, so nothing was written and nothing was booted out. "
        f"`sessionphone bot uninstall` first (it boots out {legacy_label(name)}), then `{INSTALL}`.")


def _retire_legacy(name: str, say) -> list[str]:
    """Boot out the legacy job if launchd has it, remove its plist if it
    exists, for this one agent's exact old label. -> one line per act, each
    also given to SAY. Raises LegacyStillLoaded, its plist kept, if launchd
    still lists the old job BOOTOUT_WAIT_S after the bootout: a re-run, which
    is safe, retires it then."""
    old_label = legacy_label(name)
    target = f"{domain()}/{old_label}"
    old = legacy_plist_path(name)
    said = []
    if _loaded(target):
        _launchctl("bootout", target)
        if not _wait(target, loaded=False, wait_s=BOOTOUT_WAIT_S):
            kept = f"its plist {old} is kept" if old.exists() else "it has no plist"
            raise LegacyStillLoaded(
                f"the legacy launchd job {old_label} is still going: launchd lists it "
                f"{BOOTOUT_WAIT_S:g} s after its bootout, so the new job {label(name)} was NOT "
                f"started and its plist was not written ({kept}). Re-run `{INSTALL}`: it is "
                "safe to repeat.")
        said.append(f"retired legacy launchd job {old_label} (booted out)")
    if old.exists():
        old.unlink()
        said.append(f"retired legacy launchd job {old_label} (plist removed)")
    for line in said:
        if say is not None:
            say(line)
    return said


def _retire_rivals(replace, say) -> list[str]:
    """Retire each bot `--replace` names (settings.Resolved.replace: bot ->
    the root it shared): boot out its job if launchd has it, remove its
    plist, and retire its legacy job too (_retire_legacy). -> one line per
    act, each also given to SAY. phone.json forgets it later, in
    settings.save, once the new job is loaded. Raises RivalStillLoaded, its
    plist kept, if launchd still lists it BOOTOUT_WAIT_S after the bootout:
    a re-run, which is safe, retires it then."""
    said = []
    for old, root in replace.items():
        target = f"{domain()}/{label(old)}"
        p = plist_path(old)
        acts = []
        if _loaded(target):
            _launchctl("bootout", target)
            if not _wait(target, loaded=False, wait_s=BOOTOUT_WAIT_S):
                kept = f"its plist {p} is kept" if p.exists() else "it has no plist"
                raise RivalStillLoaded(
                    f"bot {old}'s launchd job {label(old)} is still going: launchd lists it "
                    f"{BOOTOUT_WAIT_S:g} s after its bootout, so the new job was NOT started "
                    f"({kept}). Re-run `{INSTALL} --replace {old}`: it is safe to repeat.")
            acts.append("booted out")
        if p.exists():
            p.unlink()
            acts.append("plist removed")
        line = (f"replaced bot {old}, on the same root ({root}): launchd job {label(old)} "
                f"{', '.join(acts) if acts else 'was not installed'}")
        said.append(line)
        if say is not None:
            say(line)
        said += _retire_legacy(old, say)
    return said


def _record_replaced(res, say) -> None:
    """After --replace: write the retired bots' names under the root
    (displayname.record_replaced), so the new bot's start takes the old
    agent's name as a default nobody chose. A failed write is said, not raised:
    the bot then keeps that name as the user's, and install's `name:` line
    gives the DC_NAME line that changes it."""
    if not res.replace:
        return
    try:
        displayname.record_replaced(dc_env.home(res.env, res.root), res.replace)   # the bot's own home
    except OSError as exc:
        if say is not None:
            say(f"warning: could not record the replaced bot under {res.root} ({exc.strerror or exc}); "
                "the bot keeps its current name at its start")


def install(name: str | None = None, owner: str | None = None,
            seeded=None, say=None, seated=None, shell=None, forget=(),
            settled=None, registry: str | None = None, replace=()) -> pathlib.Path:
    """Write the plist (0600) and (re)load it, and check that launchd has it
    loaded. Refuses an unconfigured or unsafe root, a junk mail host, an STT
    URL with no model, and a bot nobody may ring, before it touches the old
    job. Raises NotLoaded if the job is not loaded.

    owner: the owner's Delta Chat address, approved in the callers file the
    job will read (DC_CALLERS_FILE, else <root>/callers.txt). Without it the
    owner is whoever `sessionphone link` wrote there: there is nowhere else it
    is known, and the account cannot be opened while the old bot holds it.
    Nothing is read from the kit's operator setting: that is a mail address,
    and the owner must be a Delta Chat contact. Refused while DC_CALLERS is
    set: it grants nothing, and the job would carry it.

    The bot's allow-list is seeded here too, before launchd (plan_channels,
    write_channels), and judged before anything is written, so a refused install
    writes nothing: seeded, if given, is called with the channels.Entry written,
    if one was.

    A legacy job for the same agent (legacy_label: sessionphone 0.1.0's) is
    retired before the new plist is written: booted out if loaded, its plist
    removed if it exists, each act given to SAY as one line. Refused first
    (LegacyCarryOver, writing nothing) if its plist carries a setting the new
    job would drop or change (check_carry_over), or if it is loaded with its
    plist gone (check_legacy_orphan). Raises LegacyStillLoaded, the new job
    neither written nor started, if launchd still lists it after its bootout.

    Another bot phone.json records on the same root (one Delta Chat account)
    is refused (settings.Rival, exit 2, writing nothing), naming it, unless
    REPLACE (`--replace NAME`) names it. Then, where the legacy job is
    retired, its job is booted out and its plist removed (_retire_rivals),
    and phone.json forgets it once the new job is loaded. Raises
    RivalStillLoaded, the new job neither written nor started, if launchd
    still lists it after its bootout.

    The bot's seat (seat.py), after every refusal above and before install's
    first write of its own: DC_MESSAGE is pinned to the installing shell's
    `message`. Whether a signer is wanted is seatmail.signing()'s ruling on
    the job's environment (the carrier this bot's own `message send` will
    use), never install's. On the mail drop with no signer given or saved the
    seat step is skipped, and so is the signer half of the start check: no
    key, no registry, no agent. A carrier sessionmail refuses, or one that
    cannot be known, refuses (seat.NoCarrier, a NoSigner), writing nothing.
    DC_SIGNER_* given (this shell's, or saved) are used as they are, on any
    carrier. REGISTRY (`--registry DIR`) asks for a key on any carrier, the
    drop included: with no DC_SIGNER_* given, install runs the seat's agent
    and, only if sessionmail says the seat has no key yet, first mints it
    into REGISTRY (seat.key: `message seat init --registry DIR <seat>`); a
    key already there is never minted again. The signer it prints is then
    SAVED (settings.Resolved.keyed), so later installs and starts sign
    without --registry. Else, with neither, and a sessionmail whose `message seat agent --help`
    says `Capability: install`, install runs `message seat agent --install
    <seat>` and the job carries the socket and .pub it prints (derived on
    each run, so never saved); a seat not yet minted, or any other refusal,
    is NoSigner and a failure SeatFailed, with nothing of the bot written.
    With only one set, or a sessionmail without that line, install refuses
    (NoSigner) with the bot's own start-up refusal, naming the steps, and
    writes nothing. Then the bot's own start check (seatmail.check) runs on
    the job's environment, and the box Delta text goes to is made and proved
    there (`message setup --box`, `message path`), so a job that would refuse
    to start, or mail no box, is never loaded. SEATED, if given, is
    called with (seat.Seat, seat.Box) once the job is loaded. SHELL is the
    installing shell's environment (os.environ).

    What the job carries is resolved FIRST (settings.resolve), the bot's name
    and root before anything else: this shell's values, then the settings
    file's (settings.FILE), then the default. A file that cannot be used, two
    bots recorded and no name, a locked key this shell changes, or a saved
    signer path that is gone refuses (settings.Bad, exit 2), writing nothing;
    when this sessionmail can run the seat's agent (seat.runs_agent), that
    refusal also names the --forget that lets install run it.
    FORGET names keys to drop from the file. The file is written only once
    the job is loaded (settings.save), so a refused or failed install leaves
    the last settings that worked; SETTLED, if given, is then called with the
    settings.Resolved, its env the job's. A save that fails still calls
    SEATED and SETTLED (the job is loaded), then raises settings.NotSaved,
    its `plist` the job's plist."""
    shell = os.environ if shell is None else shell
    res = settings.resolve(shell, name=name, forget=forget,
                           runs_agent=lambda given: seat.runs_agent(given, shell), replace=replace)
    name = res.name
    paths.ensure_private(at=res.root)
    j = job(name, python=named_python(), given=res.given)
    env = j["EnvironmentVariables"]
    # judged on the environment the job will carry, so the bot it starts would not refuse
    why = dc_env.host_problem(env)
    if why:
        raise NoMailHost(why)
    # the bot's own start check (bot.check_stt), reused so the two cannot drift
    why = stt.check(dc_env.stt_url(env), dc_env.stt_model(env))
    if why:
        raise NoSTTModel(why)
    why = dc_env.callers_ignored(env)
    if why:     # before anything is written: the job would carry a setting that does nothing
        raise NoCallers(f"{why} `sessionphone bot install` refuses while it is set: unset it, "
                        "then run it again.")
    # before anything is written: a rename must not move a carried setting
    check_carry_over(name, env)
    if owner:
        # a refused install writes NOTHING: judge the
        # list before add_owner touches the callers file
        plan = plan_channels(env, owner)
    else:
        why = dc_env.callers_problem(env)
        if why:
            raise NoCallers(why)
        plan = plan_channels(env)     # nothing written yet: the callers refusal comes first
    me = seat.seat_name(env, name)
    # writes nothing: no signer it can get refuses here. --registry asks for a key on any carrier
    given = seat.plan(env, me, shell, keyed=registry is not None)
    check_legacy_orphan(name)          # the last refusal before the first write
    if given:
        made = given
    elif registry is not None:         # sessionmail's writes: the seat's key if absent, its agent
        made = seat.key(env, me, shell, registry)
        res.keyed = {k: env[k] for k in seat.SIGNER_VARS}   # saved: later installs and starts sign
    else:
        made = seat.run_agent(env, me, shell)   # sessionmail's write: the seat's agent
    why = seatmail.check(env, seat=me)           # the bot's own start check, on the job's env
    if why:
        raise NoSigner(why)
    box = seat.ensure_box(env, shell)
    if owner:
        add_owner(owner, dc_env.callers_file(env))
        why = dc_env.callers_problem(env)
        if why:
            raise NoCallers(why)
    entry = write_channels(plan)
    if entry is not None and seeded is not None:
        seeded(entry)
    try:
        _retire_rivals(res.replace, say)   # never two bots on one account: --replace NAME
        _record_replaced(res, say)     # the bot's start renames a default it carried
        _retire_legacy(name, say)      # never two bots on one account
    except (LegacyStillLoaded, RivalStillLoaded) as exc:   # say what this run did write
        wrote = [f"{owner.strip().lower()} approved in {dc_env.callers_file(env)}"] if owner else []
        if entry is not None:
            wrote.append(f"channel {entry.name} {entry.target} listed in {dc_env.channels_file(env)}")
        if not wrote:
            raise
        raise type(exc)(f"{exc} Kept from this run: {'; '.join(wrote)}.") from None
    p = plist_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    target = f"{domain()}/{label(name)}"
    _launchctl("bootout", target)      # a reinstall replaces the old job
    tmp = p.with_suffix(".plist.part")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
        plistlib.dump(j, fh)
    tmp.replace(p)
    _bootstrap(p, target)
    res.env = dict(env)
    unsaved = None
    try:
        settings.save(res)             # only once the job is loaded
    except settings.NotSaved as exc:   # the job runs: say all of it, then this
        unsaved = exc
    if seated is not None:
        seated(made, box)
    if settled is not None:
        settled(res)
    if unsaved is not None:
        unsaved.plist = p
        raise unsaved
    return p


def add_owner(addr: str, path: pathlib.Path) -> bool:
    """Approve addr as the owner in the callers file at path (made 0600, its
    directory 0700 if new). -> False if it was already approved. Raises
    ValueError, writing nothing, if addr is not an address."""
    # account starts no rpc on import, but it is heavy: only when an owner is named
    from sessionphone.account import add_caller
    if "@" not in addr.strip():
        raise ValueError(f"--owner {addr!r} is not an address (want name@domain)")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return add_caller(addr, f"owner, named by `sessionphone bot install --owner`, {stamp}", path)


INSTALL = "sessionphone bot install"     # plan_channels' default RETRY: install's own wording


def plan_channels(env: dict, owner: str | None = None, retry: str = INSTALL) -> tuple | None:
    """Judge the list dc_env.channels_file(env), writing NOTHING.

    -> None if it already has entries (it is left alone), else (path, addr,
    note): the owner line to seed, from OWNER (--owner), else from the callers
    file's owner line. Raises NoChannels, naming the file, when there is no one
    to seed it with, or when the file is there but the bot would refuse it
    (loose, unreadable): install never tightens it quietly, which would lift
    the bot's refusal of a loose list. Raises ValueError if OWNER is not an address. So every
    refusal comes before install writes anything.
    RETRY is the command a loose or unreadable list's refusal says to run again
    once it is fixed: install's own, unless `channels seed` calls it,
    whose bots install did not make (re-running install there would write a
    second launchd job for the same account). For the same reason, a seed with
    no owner line leads with `channels add owner ADDR`, never `bot install --owner`."""
    path = dc_env.channels_file(env)
    listing = channels.load(path, log=lambda _msg: None)
    if listing.refusal and path.exists():
        raise NoChannels(f"the bot would send NOWHERE: {listing.refusal}. Fix it, then run "
                         f"`{retry}` again.")
    if listing.entries:
        return None
    callers = dc_env.callers_file(env)
    if owner:
        addr, came = owner.strip().lower(), "named by `sessionphone bot install --owner`"
        norm = channels.parse_target(addr)
        if norm is None or norm.startswith("chat:"):
            raise ValueError(f"--owner {owner!r} is not an address (want name@domain)")
    else:
        # account starts no rpc on import, but it is heavy: only when needed
        from sessionphone.account import read_owner
        addr, came = read_owner(callers), f"the owner line of {callers}"
    if not addr:
        what = "has no channel in it" if path.exists() else "does not exist"
        if retry == INSTALL:
            name = (f"Name the owner: run `sessionphone link` and let them scan the invite (the "
                    f"first to join becomes the owner), or `sessionphone bot install --owner ADDR` "
                    f"(writes `owner ADDR` into {path}), or `sessionphone channels add owner ADDR`; "
                    f"{dc_env.DELTA_ADDR}.")
        else:   # `channels seed`: its bots were not made by install, so never lead with it
            name = (f"Name the owner: `DC_CHANNELS_FILE={shlex.quote(str(path))} sessionphone "
                    f"channels add owner ADDR` (writes `owner ADDR` into {path}), or run "
                    f"`sessionphone link` and let them scan the invite (the first to join becomes "
                    f"the owner), then run `{retry}` again; {dc_env.DELTA_ADDR}.")
        raise NoChannels(
            f"the bot would send NOWHERE: {path} {what}, and there is no owner to list in it "
            f"(no --owner, and no owner line in {callers}), so every send would be refused. "
            + name)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return path, addr, f"owner, {came}, {stamp}"


def write_channels(plan: tuple | None) -> channels.Entry | None:
    """Write what plan_channels judged: -> the Entry written, or None (nothing to do,
    or the address is already listed). Raises NoChannels if the write is refused."""
    if plan is None:
        return None
    path, addr, note = plan
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        return channels.seed_owner(addr, note, path)
    except ValueError as exc:
        raise NoChannels(f"the bot would send NOWHERE: {exc}") from exc


def uninstall(name: str | None = None) -> list[str]:
    """Stop the job and remove its plist, and the same for a legacy job of
    this agent (legacy_label). -> one line per job removed, saying what was
    done to it; empty if neither job was there."""
    name = name or agent()
    said = []
    for lab, p, what in ((label(name), plist_path(name), "removed launchd job"),
                         (legacy_label(name), legacy_plist_path(name), "retired legacy launchd job")):
        acts = []
        if _launchctl("bootout", f"{domain()}/{lab}").returncode == 0:
            acts.append("booted out")
        if p.exists():
            p.unlink()
            acts.append("plist removed")
        if acts:
            said.append(f"{what} {lab} ({', '.join(acts)})")
    return said


def _plist(p: pathlib.Path) -> dict:
    """The plist at P as a dict; {} when it is missing or unreadable."""
    try:
        with open(p, "rb") as fh:
            j = plistlib.load(fh)
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def log_paths(name: str | None = None) -> tuple[pathlib.Path, pathlib.Path]:
    """(the bot's own log, launchd's log for its job), as the installed plist
    names them: DC_LOG, else <DC_ROOT, else SESSIONPHONE_ROOT, else the root>/bot.log;
    and the job's StandardErrorPath, else <root>/bot.launchd.log."""
    j = _plist(plist_path(name))
    env = j.get("EnvironmentVariables") or {}
    env = env if isinstance(env, dict) else {}
    r = dc_env._get(env, "ROOT") or env.get("SESSIONPHONE_ROOT")
    root = pathlib.Path(r).expanduser() if r else paths.root()
    log = dc_env._get(env, "LOG")
    bot_log = pathlib.Path(log).expanduser() if log else root / "bot.log"
    err = j.get("StandardErrorPath")
    launchd_log = pathlib.Path(err) if isinstance(err, str) and err else paths.root() / "bot.launchd.log"
    return bot_log, launchd_log


def last_line(p: pathlib.Path, tail: int = 65536) -> str | None:
    """The last non-empty line of the file at P (its last TAIL bytes), or None."""
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else None


def last_log(name: str | None = None) -> tuple[pathlib.Path, str | None, bool]:
    """-> (the log, its last line or None, whether it is launchd's log). The bot's
    own log first; when it has no line yet (a bot that refused at start writes
    only to stderr), launchd's log for the job, which holds that refusal."""
    bot_log, launchd_log = log_paths(name)
    line = last_line(bot_log)
    if line is not None:
        return bot_log, line, False
    line = last_line(launchd_log)
    if line is not None:
        return launchd_log, line, True
    return bot_log, None, False


def legacy_status(name: str | None = None) -> dict:
    """{label, plist, installed, loaded} of the legacy job; never raises."""
    p = legacy_plist_path(name)
    return {"label": legacy_label(name), "plist": str(p), "installed": p.exists(),
            "loaded": _loaded(f"{domain()}/{legacy_label(name)}")}


def status(name: str | None = None) -> dict:
    """{label, installed, loaded, pid, last_exit, plist, legacy}; never raises.
    legacy is legacy_status(), or None when no legacy job is loaded and no
    legacy plist exists."""
    p = plist_path(name)
    out = {"label": label(name), "plist": str(p), "installed": p.exists(), "loaded": False,
           "pid": None, "last_exit": None, "legacy": None}
    old = legacy_status(name)
    if old["installed"] or old["loaded"]:
        out["legacy"] = old
    r = _launchctl("print", f"{domain()}/{label(name)}")
    if r.returncode == 0:
        out["loaded"] = True
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("pid = "):
                out["pid"] = int(line.split("=", 1)[1])
            elif line.startswith("last exit code = "):
                out["last_exit"] = line.split("=", 1)[1].strip()
    return out

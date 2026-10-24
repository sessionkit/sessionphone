"""settings - what `bot install` remembers between runs: the phone tool's host file.

The file is ``~/.sessionkit/phone/settings/phone.json`` (FILE): the kit's host
layer for the tool ``phone`` in the package ``phone``, one file per tool. It
holds one map per bot, because two agents on one Mac are two bots:

    {"bots": {"ada": {"DC_HOST": "mac.example.net", "SESSIONPHONE_ROOT": "~/ada"}}}

The keys are the settings' own variable names. What the reader does, as the
kit's settings specification says a host layer is read:

- ``phone.json`` first, then ``phone.d/*.json`` in name order. A later file
  wins, and each value's origin is the one file that set it.
- ``_locked``, in any of them, is a list of dotted paths
  (``bots.<agent>.<KEY>``, or ``bots.<agent>`` for every key that file sets).
  A locked key is ignored in every file read after the one that locked it,
  and a different value from this shell is refused, naming that file.

Each value resolves in the family's order: this shell, then the file, then
the default (or a value derived on every run, which is never saved). After
the job is loaded, install writes back the values this shell gave, into
``phone.json`` only. A drop-in is never written.

Which keys are kept (savable): every ``DC_*`` but DC_AGENT (the map's own key)
and DC_CALLERS (install refuses while it is set), and the names in SAVED.
One derived value is kept too: the signer `bot install --registry` got from
`message seat agent --install` (Resolved.keyed), so the bot keeps its key.
sessionmail's mail-route keys are NOT kept here: sessionmail reads them from
its own ``mail.json``, and a second copy would be a second source. A key the
shell sets is still carried into the job for that run. mail_file() asks the
installed sessionmail whether it reads ``mail.json``, so install can say,
for each one, whether it is kept and how to keep it.

Install is not the only reader. ``bot status``, ``bot uninstall``, ``bot
run``, paths.root(), bridge/dc_env.py and bin/dc-env take the bot and the
root the same way (bot_name, kept): this shell, then the file, then the
default. Each asks sessionmail for this session's seat, as install does
(shell_name's ASK), once per process; paths.root() asks only when the file
records a bot, since with none the name changes nothing it reads. A reader
refuses nothing a file locks: only install, which writes, enforces a lock.

One root is one Delta Chat account. Install refuses a second bot recorded
on the same root (rivals, Rival), naming it, unless it is told to replace
it (``--replace NAME``): then the old bot's job is retired and phone.json
forgets it.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sessionphone import paths, reach

TOOL = "phone"
FILE = paths.kit_settings(TOOL, f"{TOOL}.json")
DROPINS = f"{TOOL}.d"
LOCKED = "_locked"
BOTS = "bots"
DEFAULT_AGENT = "claude"
ROOT_KEY = "SESSIONPHONE_ROOT"
# Kept besides DC_*: the ONE list that widens what install remembers.
SAVED = (ROOT_KEY,)
# DC_* never kept: the map's own key, and a setting install refuses while set.
NEVER = ("DC_AGENT", "DC_CALLERS")
# Kept, and refused when the saved path has gone: a stale signer is never carried.
MUST_EXIST = ("DC_SIGNER_SOCK", "DC_SIGNER_PUB")

# sessionmail's mail-route variables and the mail.json key that holds each.
# SESSIONMAIL_HOME has no key: it is read from the environment only.
MAIL_FILE = paths.kit_settings("mail", "mail.json")
# sessionmail's INSTALLED template, the layer under mail.json (source "template"):
# sessionmail's default, written at its install, never the user's own file.
MAIL_TEMPLATE = paths.kit_settings("mail", "mail.template.json")
MAIL_KEYS = {"SESSIONMAIL_SENDMAIL": "sendmail", "SESSIONMAIL_ROOT": "root",
             "SESSIONMAIL_HOST": "host", "SESSIONMAIL_RELAY_DOMAIN": "postfix.relay_domain"}
MAIL_HOME = "SESSIONMAIL_HOME"
MAIL_PROBE_S = 10.0
# Run by the installed sessionmail's own interpreter: exit 3 when it has no
# key table for these keys (it reads the environment only), else each key's
# value and source with no variable set, which is what a job that carries
# none of them gets.
_MAIL_PROBE = """
import json, sys
try:
    from sessionmail import _settings as s
except Exception:
    sys.exit(3)
keys = sys.argv[1:]
table = getattr(s, "KEYS", None)
if not isinstance(table, dict) or not all(k in table for k in keys) or not hasattr(s, "setting"):
    sys.exit(3)
out = {}
for k in keys:
    got = s.setting(k, {})
    out[k] = [got.value, got.source]
print(json.dumps(out))
"""
# The carrier, asked of sessionmail's own resolver (`_settings.carrier`, the one
# `message send` acts on), in a process whose environment holds only the job's
# SESSIONMAIL_SENDMAIL, if it carries one: argv[1] is "1" and argv[2] the value.
_CARRIER_PROBE = """
import json, os, sys
try:
    from sessionmail import _settings as s
except Exception:
    sys.exit(3)
fn = getattr(s, "carrier", None)
if not callable(fn):
    sys.exit(3)
env = {s.CARRIER_VAR: sys.argv[2]} if sys.argv[1:2] == ["1"] else {}
c = fn(env)
print(json.dumps({"kind": c.kind, "path": c.path, "source": c.source, "why": c.why}))
"""
CARRIER_KINDS = ("drop", "mta", "refused")
FILE_SOURCES = ("template", "host")
TEMPLATE_SOURCE, HOST_SOURCE = FILE_SOURCES
# sessionmail could not read its own file: it names no value, and refuses to pick a carrier.
UNREADABLE = "unreadable"


class Bad(Exception):
    """phone.json (or a drop-in) cannot be used, or this run conflicts with
    it: unreadable, not the shape above, a locked key changed, a saved signer
    path gone, two bots and no name. Exit code 2, naming the file; nothing
    is written."""


class Rival(Bad):
    """Another bot phone.json records keeps the same root (one Delta Chat
    account), and this install was not told to replace it. Exit code 2,
    naming it and the ways past it; nothing is written."""


class NotSaved(RuntimeError):
    """The job was loaded, but phone.json could not be written. Exit code 1.

    ``plist`` is the job file that was loaded; install sets it before it
    re-raises, so the caller can still say every installed line."""

    plist: pathlib.Path | None = None


def shown(p: str | os.PathLike) -> str:
    """P with this account's home shown as ``~``."""
    s, home = str(p), str(pathlib.Path.home())
    return "~" + s[len(home):] if home and (s == home or s.startswith(home + os.sep)) else s


def file_path() -> pathlib.Path:
    return pathlib.Path(FILE).expanduser()


def savable(key: str) -> bool:
    return key in SAVED or (key.startswith("DC_") and key not in NEVER)


def given_by(shell: Mapping[str, str]) -> dict[str, str]:
    """What this shell gives the job, as install has always carried it: each
    DC_*, SESSIONPHONE_ROOT, mail-route key and SESSIONMAIL_HOME set and not
    empty (an empty value is no value). Only the savable ones are saved."""
    return {k: v for k, v in shell.items()
            if v and (k.startswith("DC_") or savable(k) or k in MAIL_KEYS or k == MAIL_HOME)}


INSTALL_VERB = "bot install"


def _again(verb: str | None) -> str:
    """How a refusal names the run to repeat: `sessionphone VERB`, or, for a
    reader that serves many verbs (the root), the run itself."""
    return f"run `sessionphone {verb}` again" if verb else "run it again"


def _read(p: pathlib.Path, verb: str | None = INSTALL_VERB) -> dict:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise Bad(f"{shown(p)} could not be read ({exc.strerror or exc}): nothing was written. "
                  f"Fix or remove it, then {_again(verb)}.") from exc
    except ValueError as exc:
        raise Bad(f"{shown(p)} is not JSON ({exc}): nothing was written. Fix or remove it, "
                  f"then {_again(verb)}.") from exc
    why = _shape(data)
    if why:
        raise Bad(f"{shown(p)} {why}: nothing was written. Fix it, then {_again(verb)}.")
    return data


def _shape(data) -> str | None:
    if not isinstance(data, dict):
        return "is not a JSON object"
    bots = data.get(BOTS, {})
    if not isinstance(bots, dict):
        return f"has a `{BOTS}` that is not an object"
    for name, keys in bots.items():
        if not isinstance(keys, dict):
            return f"has `{BOTS}.{name}`, which is not an object"
        for k, v in keys.items():
            if not isinstance(v, str):
                return f"has `{BOTS}.{name}.{k}`, which is not a string"
    locked = data.get(LOCKED, [])
    if not isinstance(locked, list) or not all(isinstance(x, str) for x in locked):
        return f"has a `{LOCKED}` that is not a list of key paths"
    return None


def layers(verb: str | None = INSTALL_VERB) -> list[tuple[pathlib.Path, dict]]:
    """phone.json, then each phone.d/*.json in name order: [(path, data)].
    A file that is not there is no layer. Raises Bad for one that cannot be
    used, naming VERB as the run to repeat once it is fixed."""
    main = file_path()
    out = []
    if paths.exists(main):
        out.append((main, _read(main, verb)))
    d = main.with_name(DROPINS)
    if paths.is_dir(d):
        for p in sorted(d.glob("*.json")):
            out.append((p, _read(p, verb)))
    return out


def names(found: list[tuple[pathlib.Path, dict]]) -> list[str]:
    """Every bot any layer records, sorted."""
    return sorted({n for _p, data in found for n in data.get(BOTS, {})})


def _locks(data: dict, name: str) -> tuple[set[str], bool]:
    """-> (the keys of bot NAME this file locks by name, whether it locks them all)."""
    keys, every = set(), False
    for lp in data.get(LOCKED, []):
        if lp in (BOTS, f"{BOTS}.{name}"):
            every = True
        elif lp.startswith(f"{BOTS}.{name}."):
            keys.add(lp[len(f"{BOTS}.{name}."):])
    return keys, every


@dataclass
class Resolved:
    """What install resolved before it built the job."""
    name: str
    name_from: str                  # where the name came from, in words
    given: dict[str, str]           # what job() reads: this shell's over the file's
    origin: dict[str, str]          # key -> "shell" or the file that set it
    file: dict[str, str]            # the file layers' values for this bot
    file_origin: dict[str, pathlib.Path]
    locked: dict[str, pathlib.Path]  # key -> the file that locked it
    forget: tuple[str, ...] = ()
    forgot: dict[str, str] = field(default_factory=dict)   # key -> the value removed
    ignored: dict[str, pathlib.Path] = field(default_factory=dict)  # file keys not kept -> file
    env: dict[str, str] = field(default_factory=dict)      # the job's environment, once built
    saved: bool | None = None       # phone.json written (True), unchanged (False), not yet (None)
    keyed: dict[str, str] = field(default_factory=dict)    # the signer `--registry` gave: saved
    not_saved: bool = False         # the write was tried and failed (NotSaved)
    replace: dict[str, str] = field(default_factory=dict)  # bot -> its root: retired, then forgotten

    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(self.given.get(ROOT_KEY) or paths.DEFAULT_ROOT).expanduser()


NAME_VARS = ("DC_AGENT", "CLAUDE_CODE_AGENT")
SEAT_FROM = "this session's seat (`message subscription list`)"


def named_by_shell(came: str) -> bool:
    """Did the shell's own variables (DC_AGENT, CLAUDE_CODE_AGENT) name the
    bot, by bot_name's CAME? Then it is what was asked for; else (the seat,
    the file, the default) a reader says which it picked."""
    return came.endswith(" in this shell")


def task_seat(shell: Mapping[str, str]) -> bool:
    """Is this shell a task seat, whose EMAIL is a plus address
    (`project+task.role...@host`)? Its CLAUDE_CODE_AGENT is then a role
    (worker, foreman), not a box anyone hears."""
    return "+" in (shell.get("EMAIL") or "").partition("@")[0]


def shell_name(shell: Mapping[str, str], ask: bool = False) -> tuple[str, str] | None:
    """The bot this shell names, with where it came from; None when it names
    none. DC_AGENT first. With ASK (install, `bot status`, `bot uninstall`),
    sessionmail is then asked what reaches this session (reach.asked): its
    seat names the bot when it is DURABLE, one a later session can be
    started as (`session start NAME`); a plus address or a session id names
    none. Else, and when sessionmail gives no answer, CLAUDE_CODE_AGENT,
    unless this is a task seat (task_seat: it holds a role, not a box)."""
    raw = (shell.get("DC_AGENT") or "").strip().lower()
    if raw:
        return raw, "DC_AGENT in this shell"
    if ask:
        r = reach.asked(shell)
        if r.answered:
            return (r.durable, SEAT_FROM) if r.durable else None
    raw = (shell.get("CLAUDE_CODE_AGENT") or "").strip().lower()
    if raw and not task_seat(shell):
        return raw, "CLAUDE_CODE_AGENT in this shell"
    return None


def pick_name(shell: Mapping[str, str], recorded: list[str],
              verb: str | None = INSTALL_VERB, ask: bool = False) -> tuple[str, str]:
    """Which bot: DC_AGENT, then this session's durable seat (shell_name),
    then the one bot the file records, then "claude". Two or more recorded and no name
    from this shell is refused: guessing would act on the wrong bot. The
    refusal names VERB (`sessionphone VERB`) as the run to repeat. ASK:
    shell_name asks sessionmail for this session's seat."""
    named = shell_name(shell, ask)
    if named:
        return named
    if len(recorded) == 1:
        return recorded[0], f"{shown(file_path())}, the one bot it records"
    if len(recorded) > 1:
        how = (f"`DC_AGENT={recorded[0]} sessionphone {verb}`" if verb else
               f"set DC_AGENT (`export DC_AGENT={recorded[0]}`), then run it again")
        raise Bad(f"{shown(file_path())} records {len(recorded)} bots ({', '.join(recorded)}) and "
                  f"this shell names none: nothing was written. Name one: {how}.")
    return DEFAULT_AGENT, "the default"


def bot_name(shell: Mapping[str, str] | None = None,
             verb: str | None = INSTALL_VERB, ask: bool = False) -> tuple[str, str]:
    """Which bot a reader acts on, as install picks it (pick_name), with
    where the name came from. A name this shell gives is taken before any
    file is read, so a broken phone.json never stops `DC_AGENT=NAME ...`.
    Raises Bad for a file that cannot be used, or two bots and no name.
    ASK, as shell_name: `bot status` and `bot uninstall` ask, as install does."""
    shell = os.environ if shell is None else shell
    return shell_name(shell, ask) or pick_name(shell, names(layers(verb)), verb, ask)


def merged(found: list[tuple[pathlib.Path, dict]], name: str
           ) -> tuple[dict[str, str], dict[str, pathlib.Path], dict[str, pathlib.Path]]:
    """The layers' values for bot NAME, merged as the host layer is read: a
    later file wins, and a key an earlier file locks is ignored after it.
    -> (values, key -> the file that set it, key -> the file that locked it).
    A lock is recorded whether or not any file gives the key a value."""
    values: dict[str, str] = {}
    origin: dict[str, pathlib.Path] = {}
    locked: dict[str, pathlib.Path] = {}
    for p, data in found:
        mine = data.get(BOTS, {}).get(name, {})
        for k, v in mine.items():
            if k in locked:
                continue        # held by an earlier file's lock
            values[k], origin[k] = v, p
        keys, every = _locks(data, name)
        for k in keys | (set(mine) if every else set()):
            locked.setdefault(k, p)
    return values, origin, locked


def kept(key: str, shell: Mapping[str, str] | None = None,
         verb: str | None = None) -> tuple[str | None, pathlib.Path | None]:
    """What the file keeps for KEY, for the bot install would pick
    (bot_name, asking sessionmail as install does): -> (value, the file that set it),
    or (None, None) when no file keeps it. The FILE step of a reader's
    order; the reader takes this shell's value first. Writes nothing and
    refuses no lock (install alone enforces one). Raises Bad as bot_name."""
    shell = os.environ if shell is None else shell
    if not savable(key):
        return None, None
    found = layers(verb)
    recorded = names(found)
    if not recorded:            # no bot recorded: no name finds a value, so ask nothing
        return None, None
    name, _from = pick_name(shell, recorded, verb, ask=True)
    values, origin, _locked = merged(found, name)
    v = values.get(key)
    return (v, origin[key]) if v else (None, None)


def place(values: Mapping[str, str]) -> tuple[str, str]:
    """(the home, the account folder) of a bot whose job carries VALUES, as
    dc_env reads them there: DC_ROOT, else SESSIONPHONE_ROOT, else the
    default, plus /<DC_PROFILE> when a profile is named (dc_env.home);
    DC_ACCOUNTS, else <home>/accounts. Two profiles on one root are two
    homes, so two accounts. Real paths: two spellings of one folder are one
    folder."""
    root = os.path.expanduser(values.get("DC_ROOT") or values.get(ROOT_KEY) or paths.DEFAULT_ROOT)
    profile = (values.get("DC_PROFILE") or "").strip()
    home = os.path.join(root, profile) if profile else root
    accounts = values.get("DC_ACCOUNTS") or os.path.join(home, "accounts")
    return (os.path.realpath(home),
            os.path.realpath(os.path.expanduser(accounts)))


def rivals(found: list[tuple[pathlib.Path, dict]], name: str,
           given: Mapping[str, str]) -> dict[str, str]:
    """Every OTHER bot the layers record on bot NAME's home or account folder
    (place), GIVEN being what NAME's job will carry: one home (a root, or a
    profile under it) is one Delta Chat account, and two bots on it is a broken state, the old one still
    mailing its own box. -> {bot: the root it shares, shown}, by name."""
    root, accounts = place(given)
    out = {}
    for other in names(found):
        if other == name:
            continue
        theirs = place(merged(found, other)[0])
        if theirs[0] == root or theirs[1] == accounts:
            out[other] = shown(theirs[0] if theirs[0] == root else theirs[1])
    return out


def _replacing(found: list[tuple[pathlib.Path, dict]], name: str, given: Mapping[str, str],
               replace: tuple[str, ...]) -> dict[str, str]:
    """The bots this install retires: each one --replace names, when it is a
    rival (rivals) phone.json alone records, unlocked. -> {bot: root}. Raises
    Bad for a --replace that cannot be done, and Rival for a rival left
    unnamed, naming it and the ways past it. Writes nothing."""
    main = file_path()
    near = rivals(found, name, given)
    for other in replace:
        if other == name:
            raise Bad(f"--replace {other}: that is the bot this install is: nothing was written.")
        if other not in near:
            raise Bad(f"--replace {other}: {shown(main)} records no other bot {other} on this bot's "
                      f"root ({shown(place(given)[0])}): nothing was written.")
        for p, data in found:
            keys, every = _locks(data, other)
            if keys or every:
                raise Bad(f"--replace {other}: {shown(p)} locks it: nothing was written.")
            if p != main and other in data.get(BOTS, {}):
                raise Bad(f"--replace {other}: it is recorded in {shown(p)}, which install does not "
                          "write. Remove it there: nothing was written.")
    left = [o for o in near if o not in replace]
    if left:
        them = ", ".join(left)
        bots_ = "bot" if len(left) == 1 else "bots"
        flags = " ".join(f"--replace {o}" for o in left)
        keep = (f"install it instead (`DC_AGENT={left[0]} sessionphone bot install`), or "
                if len(left) == 1 else "")
        raise Rival(f"{shown(main)} records {bots_} {them} on the same root as bot {name} "
                    f"({near[left[0]]}). One root is one Delta Chat account, and two bots on it "
                    f"is a broken state: the old one keeps mailing its own box ({them}), so its "
                    f"calls and messages never reach {name}. Nothing was written. To replace "
                    f"it: `sessionphone bot install {flags}` (its launchd job is booted out, its "
                    f"plist removed, and {shown(main)} forgets it). To keep it, {keep}give bot "
                    f"{name} its own root (`SESSIONPHONE_ROOT=DIR sessionphone bot install`).")
    return {o: near[o] for o in replace}


def _agent_instead(values: dict[str, str], origin: dict[str, pathlib.Path],
                   locked: dict[str, pathlib.Path], forgot: dict[str, str],
                   shell_gives: dict[str, str],
                   runs_agent: Callable[[Mapping[str, str]], bool] | None) -> str:
    """The dead-signer refusal's last sentence, or "": when forgetting every
    saved signer key would leave the job with none, and this sessionmail can
    run the seat's agent (RUNS_AGENT), the one command that lets install run
    it. Said only when that command would work: no DC_SIGNER_* from this
    shell, and each saved one in the file install writes, unlocked."""
    if runs_agent is None or any(k in shell_gives for k in MUST_EXIST):
        return ""
    main = file_path()
    keys = [k for k in MUST_EXIST if k in values or k in forgot]
    if any(k in values and (k in locked or origin.get(k) != main) for k in keys):
        return ""
    given = dict(values)
    given.update(shell_gives)
    if not runs_agent(given):
        return ""
    forgets = " ".join(f"--forget {k}" for k in keys)
    return (f" This sessionmail can run the seat's agent itself (`Capability: install`): "
            f"`sessionphone bot install {forgets}` lets install run "
            "`message seat agent --install` for it.")


def resolve(shell: Mapping[str, str], name: str | None = None,
            forget: tuple[str, ...] | list[str] = (),
            runs_agent: Callable[[Mapping[str, str]], bool] | None = None,
            replace: tuple[str, ...] | list[str] = ()) -> Resolved:
    """This shell over the file, for one bot. Writes nothing. Raises Bad,
    naming the file, for a file that cannot be used, two bots and no name, a
    locked key this shell changes, a --forget that cannot be done, a
    saved signer path that is gone, or a --replace that cannot be done; and
    Rival for another bot recorded on this bot's root that REPLACE does not
    name (_replacing). RUNS_AGENT, asked only for that last
    refusal, with the values the job would carry: True when the installed
    sessionmail can run the seat's agent itself, so the refusal also names
    the --forget that lets install do so."""
    found = layers()
    if name:
        name, name_from = name.strip().lower(), "given"
    else:
        name, name_from = pick_name(shell, names(found), ask=True)
    values, origin, locked = merged(found, name)
    ignored = {k: origin[k] for k in sorted(values) if not savable(k)}
    for k in ignored:
        values.pop(k)
        origin.pop(k)
    forget = tuple(dict.fromkeys(k.strip() for k in forget if k.strip()))
    main = file_path()
    forgot = {}
    for k in forget:
        if not savable(k):
            raise Bad(f"--forget {k}: install keeps no such setting (it keeps DC_* but "
                      f"{', '.join(NEVER)}, and {', '.join(SAVED)}): nothing was written.")
        if shell.get(k):
            raise Bad(f"--forget {k} while this shell sets {k}: unset it, or drop --forget "
                      "(a value given in the shell replaces the saved one): nothing was written.")
        if k in locked:
            raise Bad(f"--forget {k}: {shown(locked[k])} locks it: nothing was written.")
        if k in origin and origin[k] != main:
            raise Bad(f"--forget {k}: it is set in {shown(origin[k])}, which install does not "
                      "write. Remove it there: nothing was written.")
        if k in values:
            forgot[k] = values.pop(k)
            origin.pop(k)
    shell_gives = given_by(shell)
    for k, v in shell_gives.items():
        if k in locked and k not in values:
            raise Bad(f"{k} is locked by {shown(locked[k])}, which gives it no value (the bot "
                      f"takes its default), and this shell sets {v!r}: nothing was written. "
                      f"Unset {k}, or set it (or drop the lock) in {shown(locked[k])}.")
        if k in locked and values.get(k) != v:
            raise Bad(f"{k} is locked by {shown(locked[k])} at {values.get(k)!r}, and this "
                      f"shell sets {v!r}: nothing was written. Unset {k}, or change it in "
                      f"{shown(locked[k])}.")
    for k in MUST_EXIST:
        if k in values and k not in shell_gives and not paths.exists(os.path.expanduser(values[k])):
            raise Bad(f"{k} in {shown(origin[k])} names {values[k]}, which is not there any more, "
                      f"so the bot could not sign: nothing was written. Give it again "
                      f"(`{k}=PATH sessionphone bot install`), or `--forget {k}`."
                      + _agent_instead(values, origin, locked, forgot, shell_gives, runs_agent))
    given = dict(values)
    given.update(shell_gives)
    replace = tuple(dict.fromkeys(n.strip().lower() for n in replace if n.strip()))
    replacing = _replacing(found, name, given, replace)
    where = {k: str(origin[k]) for k in values}
    where.update({k: "shell" for k in shell_gives})
    return Resolved(name=name, name_from=name_from, given=given, origin=where, file=values,
                    file_origin=origin, locked=locked, forget=forget, forgot=forgot,
                    ignored=ignored, replace=replacing)


def to_save(r: Resolved) -> dict[str, str]:
    """The values phone.json gets for this bot: each savable one this shell
    gave, and the signer `bot install --registry` got (r.keyed), unless a
    drop-in sets it (a drop-in is read after phone.json and would win, so the
    saved copy would never be read)."""
    main = file_path()
    out = {k: v for k, v in r.given.items()
           if r.origin.get(k) == "shell" and savable(k)
           and r.file_origin.get(k, main) == main}
    out.update({k: v for k, v in r.keyed.items() if r.file_origin.get(k, main) == main})
    return out


def save(r: Resolved) -> bool:
    """Write this shell's values for bot r.name into phone.json, drop
    each --forget key, and forget each bot --replace retired (r.replace),
    keeping everything else in the file. Atomic, 0600, in
    a directory made 0700. -> whether the file changed. Raises NotSaved."""
    p = file_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if paths.exists(p) else {}
        if _shape(data):
            raise ValueError(_shape(data))
        bot = dict(data.get(BOTS, {}).get(r.name, {}))
        new = dict(bot)
        new.update(to_save(r))
        for k in r.forget:
            new.pop(k, None)
        gone = [o for o in r.replace if o in data.get(BOTS, {})]
        if r.name in data.get(BOTS, {}) and new == bot and not gone:
            r.saved = False
            return False
        for o in gone:
            del data[BOTS][o]
        data.setdefault(BOTS, {})[r.name] = dict(sorted(new.items()))
        _mkdirs(p.parent)
        tmp = p.with_name(p.name + ".part")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                  encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        tmp.replace(p)
    except (OSError, ValueError) as exc:
        r.not_saved = True
        raise NotSaved(f"the bot is installed and loaded, but its settings were NOT saved to "
                       f"{shown(p)} ({exc}): the next install will not remember them. Fix it, "
                       "then run `sessionphone bot install` again.") from exc
    r.saved = True
    return True


def _mkdirs(d: pathlib.Path) -> None:
    """Make D and each missing parent 0700; a folder already there is left as it is."""
    missing = []
    while not paths.exists(d):
        missing.append(d)
        d = d.parent
    for m in reversed(missing):
        m.mkdir(mode=0o700)
        m.chmod(0o700)


# ---- sessionmail's own file, asked of the installed sessionmail --------------

def _interpreter(exe: str) -> str | None:
    """The Python a `message` script runs under: its #! line, when that names
    an absolute python. None for anything else (a shell script, a binary)."""
    try:
        with open(os.path.realpath(exe), "rb") as fh:
            first = fh.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    parts = first[2:].decode("utf-8", "replace").split()
    if not parts:
        return None
    py = parts[0]
    if not os.path.isabs(py) or not os.path.basename(py).startswith("python"):
        return None
    return py if os.access(py, os.X_OK) else None


# mail_file's answers, by the realpath of the `message` asked: a process asks
# each sessionmail once (a bot asks on every send). A mail.json edit is read at
# the next start, as every other setting a job carries is. forget_mail_file()
# empties it.
_MAIL_ASKED: dict[str, dict[str, tuple[object, str]] | None] = {}


def forget_mail_file() -> None:
    """Forget every answer mail_file and mail_carrier have kept: the next call asks again."""
    _MAIL_ASKED.clear()
    _CARRIER_ASKED.clear()


def mail_file(exe: str | None) -> dict[str, tuple[object, str]] | None:
    """Does the sessionmail behind EXE (the job's `message`) read its keys
    from mail.json? Asked of that sessionmail itself, by its own interpreter,
    never by a version string. -> {mail.json key: (value, source)} as it
    reads them with no variable set (source: template, host or default), or
    None when it does not read them, or this cannot tell. Reads only.
    Asked once per `message` (its realpath) for the life of the process."""
    if not exe:
        return None
    where = os.path.realpath(exe)
    if where not in _MAIL_ASKED:
        _MAIL_ASKED[where] = _ask_mail_file(exe)
    got = _MAIL_ASKED[where]
    return None if got is None else dict(got)


# mail_carrier's answers, by (the realpath of the `message` asked, the job's
# SESSIONMAIL_SENDMAIL or None); emptied by forget_mail_file() too.
_CARRIER_ASKED: dict[tuple[str, str | None], dict[str, object] | None] = {}


def mail_carrier(exe: str | None, sendmail_value: str | None = None) -> dict[str, object] | None:
    """The carrier the sessionmail behind EXE (the job's `message`) sends by,
    asked of its own resolver (`_settings.carrier`) with sendmail_value as
    the job's SESSIONMAIL_SENDMAIL (None: the job carries none). -> {kind: drop |
    mta | refused, path, source, why}, as sessionmail answers it, or None when
    it cannot be asked (no `message`, not a Python script, a sessionmail with
    no such resolver, no answer). It never looks for an MTA on this host:
    sessionmail's own default decides. Reads only. Asked once per `message`
    and value for the life of the process."""
    if not exe:
        return None
    key = (os.path.realpath(exe), sendmail_value)
    if key not in _CARRIER_ASKED:
        _CARRIER_ASKED[key] = _ask_mail_carrier(exe, sendmail_value)
    got = _CARRIER_ASKED[key]
    return None if got is None else dict(got)


def _ask_mail_carrier(exe: str, sendmail: str | None) -> dict[str, object] | None:
    """mail_carrier's question, asked now (no cache)."""
    py = _interpreter(exe)
    if py is None:
        return None
    try:
        r = subprocess.run([py, "-I", "-c", _CARRIER_PROBE, "1" if sendmail is not None else "0",
                            sendmail or ""],
                           capture_output=True, text=True, timeout=MAIL_PROBE_S, check=False,
                           stdin=subprocess.DEVNULL,
                           env={"PATH": os.defpath, "HOME": str(pathlib.Path.home())})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        got = json.loads(r.stdout)
    except ValueError:
        return None
    if not isinstance(got, dict) or not isinstance(got.get("kind"), str):
        return None             # a kind outside CARRIER_KINDS is kept: signing() names it
    return {k: got.get(k) for k in ("kind", "path", "source", "why")}


def _ask_mail_file(exe: str) -> dict[str, tuple[object, str]] | None:
    """mail_file's question, asked now (no cache)."""
    py = _interpreter(exe)
    if py is None:
        return None
    try:
        r = subprocess.run([py, "-I", "-c", _MAIL_PROBE, *MAIL_KEYS.values()],
                           capture_output=True, text=True, timeout=MAIL_PROBE_S, check=False,
                           stdin=subprocess.DEVNULL,
                           env={"PATH": os.defpath, "HOME": str(pathlib.Path.home())})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        got = json.loads(r.stdout)
        return {k: (v[0], str(v[1])) for k, v in got.items() if k in MAIL_KEYS.values()}
    except (ValueError, TypeError, IndexError, AttributeError):
        return None


def _mail_value(var: str, value: str) -> str:
    """The mail.json value for VAR's VALUE: `root`'s variable names the
    agents folder, and the key holds its parent."""
    return str(pathlib.Path(value).parent) if var == "SESSIONMAIL_ROOT" else value


def mail_text(probe: dict | None, key: str) -> str | None:
    """The value sessionmail's own file gives KEY, as sessionmail itself takes
    it (its file_text): only from the file (FILE_SOURCES), a non-empty string,
    stripped. None when the file gives none, or PROBE is None (a sessionmail
    that reads the environment only, or one that could not be asked)."""
    filed = (probe or {}).get(key)
    if filed is None or filed[1] not in FILE_SOURCES or not isinstance(filed[0], str):
        return None
    return filed[0].strip() or None


def mail_from(key: str, source: object) -> str:
    """Where sessionmail's settings gave KEY, in words, by SOURCE: its mail.json
    (`host`) only when the key is set THERE; its installed template (`template`)
    is sessionmail's default, never the user's mail.json; `default` is the
    key's own default. Any other source is named as it is."""
    if source == HOST_SOURCE:
        return f"sessionmail's {shown(MAIL_FILE)} `{key}`"
    if source == TEMPLATE_SOURCE:
        return f"sessionmail's installed default, {shown(MAIL_TEMPLATE)} `{key}`"
    if source == "default":
        return "sessionmail's default"
    return f"sessionmail's {source}"


def mail_note(var: str, value: str | None, probe: dict | None) -> str | None:
    """What the settings block says of one mail-route variable: VALUE is
    what the job carries (this shell's), None when it carries none. None
    means there is nothing to say."""
    if var == MAIL_HOME:
        return (None if value is None else
                "this shell (NOT kept: sessionmail has no settings key for it; export it again "
                "at each install)")
    key = MAIL_KEYS[var]
    filed = probe.get(key) if probe else None
    in_file = filed is not None and filed[1] in FILE_SOURCES
    if value is None:
        if not in_file:
            return None
        return f"{mail_from(key, filed[1])} (not carried: the bot's `message` reads it)"
    want = _mail_value(var, value)
    write = f'write "{key}": {json.dumps(shown(want))} in {shown(MAIL_FILE)}'
    if probe is None:
        return (f"this shell (NOT kept: this sessionmail reads it from the environment only; "
                f"to keep it, upgrade sessionmail, then {write})")
    if in_file and os.path.expanduser(str(filed[0])) == os.path.expanduser(want):
        return f"this shell (not saved here: {mail_from(key, filed[1])} holds it too)"
    # "it" is mail.json, the file the advice writes: a template value is not in it
    has = ("it could not be read" if filed is not None and filed[1] == UNREADABLE else
           f"it holds {json.dumps(filed[0])}" if in_file and filed[1] == HOST_SOURCE else
           f"it has no such key; {mail_from(key, filed[1])} is {json.dumps(filed[0])}" if in_file else
           "it has no such key")
    return f"this shell (NOT kept: sessionmail reads it from its own file; to keep it, {write}; {has})"


# ---- the settings block --------------------------------------------------------

FIXED = ("PATH", "PYTHONUNBUFFERED")


def block(r: Resolved, probe: dict | None) -> list[str]:
    """`bot install`'s settings block: the file, then one line per setting
    the job carries (its value, and where it came from), then what was
    forgotten or ignored. Judged on r.env, the job as written."""
    env, main = r.env, file_path()
    rows: list[tuple[str, str, str]] = []
    keep = to_save(r)

    def source(k: str) -> str:
        if r.origin.get(k) == "shell":
            if k in keep:
                was = r.file.get(k)
                return ("this shell (saved)" if was is None else
                        "this shell (as saved)" if was == r.given[k] else
                        f"this shell (saved; was {shown(was)})")
            if savable(k):
                return (f"this shell (not saved: {shown(r.file_origin[k])} sets it and is read "
                        "after phone.json; change it there)")
            return "this shell"
        if k in r.origin:
            lock = " (locked)" if k in r.locked else ""
            return f"{shown(r.origin[k])}{lock}"
        if k == "DC_MESSAGE":
            return "pinned from this shell's PATH (not saved)"
        if k in MUST_EXIST and k in r.keyed:
            return ("from `message seat agent --install`, for --registry (saved)" if k in keep else
                    f"from `message seat agent --install`, for --registry (not saved: "
                    f"{shown(r.file_origin[k])} sets it and is read after phone.json)")
        if k in MUST_EXIST:
            return "from `message seat agent --install` (not saved: asked again at each install)"
        if k == "DC_EARS_SRC":
            return "derived from the ears uv tool (not saved)"
        if k == ROOT_KEY:
            return "the default"
        return "install"

    for k in sorted(env):
        if k in FIXED or k in MAIL_KEYS or k == MAIL_HOME:
            continue
        v = env[k]
        how = r.name_from if k == "DC_AGENT" else source(k)
        rows.append((k, shown(v), how))
    for var in (*MAIL_KEYS, MAIL_HOME):
        note = mail_note(var, env.get(var), probe)
        if note is None:
            continue
        if var in env:
            v = shown(env[var])
        else:
            filed = probe[MAIL_KEYS[var]]
            v = shown(str(filed[0]))
        rows.append((var, v, note))
    width_k = max((len(k) for k, _v, _h in rows), default=0)
    width_v = min(max((len(v) for _k, v, _h in rows), default=0), 40)
    state = ("NOT saved" if r.not_saved else
             {True: "saved", False: "unchanged", None: "not written"}[r.saved])
    out = [f"settings:  {shown(main)} ({state}), bot {r.name}"]
    out += [f"  {k:<{width_k}}  {v:<{width_v}}  {h}" for k, v, h in rows]
    for k, was in r.forgot.items():
        out.append(f"  forgot {k} (was {shown(was)}): removed from {shown(main)}")
    for k in r.forget:
        if k not in r.forgot:
            out.append(f"  forget {k}: {shown(main)} did not hold it")
    for k, p in r.ignored.items():
        out.append(f"  ignored {k} in {shown(p)}: install does not keep it, so the job does not carry it")
    for o, root in r.replace.items():
        done = "removed from" if r.saved else "NOT removed from" if r.not_saved else "still in"
        out.append(f"  replaced bot {o} (it kept the same root, {root}): {done} {shown(main)}")
    return out

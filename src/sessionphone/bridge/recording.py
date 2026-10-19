"""recording - the call recorder's switch: is a call's audio kept on disk?

Off by default. With nothing set, a call keeps no audio: no ``full-in.wav`` or
``full-out.wav``, no ``caller-call.wav``, no ``caller-NNN.wav`` once the call
is over. The switch is the one the ears use (their ``ears.recording``), read
the same way here, so one setting covers the mic and the calls. ears need not
be installed: this module imports nothing of theirs.

Two settings, each read from the first layer that is SET:

=================  ============================  ====================  ===============  =======
setting            CLI (the bot's own argv)      environment           ears.json key    default
=================  ============================  ====================  ===============  =======
``record``         ``--record``/``--no-record``  ``EARS_RECORD``       ``"record"``     off
``record_days``    (none)                        ``EARS_RECORD_DAYS``  ``"record_days"``  7
=================  ============================  ====================  ===============  =======

Two more say, at each call's connect, whether the call is kept. Neither has a
CLI flag, and each is read on its own (either can be off without the other):

========================  ==============================  ==========================  =======
setting                   environment                     ears.json key               default
========================  ==============================  ==========================  =======
``record_notice``         ``EARS_RECORD_NOTICE``          ``"record_notice"``         on
``record_spoken_notice``  ``EARS_RECORD_SPOKEN_NOTICE``   ``"record_spoken_notice"``  on
========================  ==============================  ==========================  =======

``record_notice`` posts one line into the call's chat on every call
(:func:`notice_text`). ``record_spoken_notice`` speaks :data:`SPOKEN_NOTICE`
before the greeting, and only on a call that is kept. Their values are read as
``record``'s, and rules 1-3 and 7-8 hold for them, but an INVALID value FAILS
TO ON, with a warning naming the layer: a garbled setting never hides the
status.

ears.json is ``$EARS_CONF`` when it is set and not empty, else
:data:`EARS_CONF_DEFAULT`. It is one JSON object shared with the other ears
settings; keys this module does not know are ignored.

Up to 0.1.0 the file lived at :data:`LEGACY_EARS_CONF`. That path is never
read, written or fallen back to. When ``$EARS_CONF`` is unset or empty, the
default file is absent and the old one exists (a stat, nothing more), the
warning for the missing file names both paths instead (:func:`stranded_warning`).

Accepted values. ``EARS_RECORD``: ``1``, ``true``, ``on`` or ``0``, ``false``,
``off``, any case, whitespace trimmed. The file's ``record``: a JSON boolean.
``EARS_RECORD_DAYS``: a decimal integer, 0 or more. The file's ``record_days``:
a JSON integer, 0 or more (not a boolean). 0 days means never prune.

The edge rules, the same on both sides:

1. An env var that is unset or empty is NOT SET.
2. No file, an unreadable file, bad JSON, or a top-level value that is not an
   object: the file layer is NOT SET for both keys (said once).
3. A key missing from the file is NOT SET.
4. An invalid ``record`` at any layer FAILS CLOSED: that layer wins, with OFF,
   and a warning names the layer and the value. A garbled switch never records.
5. An invalid ``record_days`` at a layer is skipped with a warning; the next
   layer (or 7) is used. Retention never becomes "forever" by accident.
6. The CLI flag is the process's own argument; the last one given wins.
7. The CLI and env are fixed for the life of the process. The file is followed:
   :class:`Switch` re-reads it when its stat (mtime, size, inode) changes.
8. The switch is checked when a unit of audio is about to be kept, never once
   at start-up.

The winning source is one of ``cli``, ``env``, ``file`` or ``default``.

THE CALL DIR. What a call keeps is written down in the dir itself, so a later
pass (hang-up, a crash sweep at start, the retention prune) can tell kept audio
from audio that was only needed while the call was live:

- ``recording.json`` (:data:`KEPT_FILE`): the switch as the call saw it and the
  names of the files kept under it. Only what it names is kept; only what it
  names is ever pruned.
- ``caller-call.transient`` (:data:`TRANSIENT_MARK`): this dir holds audio that
  must not outlive the call. :func:`clean` removes every audio file the dir
  holds that ``recording.json`` does not name, then the marker.

A call dir with neither file (every call made before this switch) is never
touched by anything here.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from sessionphone import paths

ENV_RECORD = "EARS_RECORD"
ENV_DAYS = "EARS_RECORD_DAYS"
KEY_RECORD = "record"
KEY_DAYS = "record_days"
ENV_NOTICE = "EARS_RECORD_NOTICE"
KEY_NOTICE = "record_notice"
ENV_SPOKEN = "EARS_RECORD_SPOKEN_NOTICE"
KEY_SPOKEN = "record_spoken_notice"
DEFAULT_NOTICE = True
DEFAULT_RECORD = False
DEFAULT_DAYS = 7

ON_WORDS = frozenset({"1", "true", "on"})
OFF_WORDS = frozenset({"0", "false", "off"})

SOURCES = ("cli", "env", "file", "default")

CLI_ON, CLI_OFF = "--record", "--no-record"

#: The spoken notice (rule 11): said before the greeting on a call that is kept.
SPOKEN_NOTICE = "This call is being recorded and kept."

#: What the docs and the log say: the switch keeps every voice the call carries.
ALL_VOICES = "every voice the call carries is kept, guests included"

#: What "off" means for the whole-call caller-call.wav. True (built): it is
#: written at its usual path DURING the call, because the speaker side-car reads
#: it live (its speaker names feed the call's barge-in gate), and deleted after
#: hang-up plus RECORD_DRAIN. False: it is never opened while the switch is
#: off, and barge-in by voice on a call stops working while recording is off.
#: STT chunk files are transient either way: STT needs a file to read.
TRANSIENT_DURING_CALL = True

#: Seconds after hang-up (ears closed, the last turn mailed) before a call's
#: transient audio is removed. The side-car waits at most 5 s per line.
RECORD_DRAIN = 10.0

KEPT_FILE = "recording.json"
TRANSIENT_MARK = "caller-call.transient"
#: Files of a call dir that are audio (or only describe audio) and so follow the
#: switch. Text (transcript.md, the .sdp files, ear-taps.jsonl) never does.
AUDIO_GLOBS = ("*.wav", "caller-call.json")
#: How often, at most, a call's end runs the retention prune.
PRUNE_EVERY = 3600.0

_DAYS_RE = re.compile(r"^[0-9]+$")
#: The ears.json read when ``$EARS_CONF`` is unset or empty: the voice
#: package's, built by :func:`sessionphone.paths.kit_settings`.
EARS_CONF_DEFAULT = paths.kit_settings("voice", "ears.json")
#: Where ears.json lived up to 0.1.0. Only stat-ed, to warn an upgrader whose
#: file was left there: never opened, never written, never a fallback.
LEGACY_EARS_CONF = "~/.settings/ears/ears.json"


def conf_path() -> str:
    """ears.json: ``$EARS_CONF`` if set and not empty, else :data:`EARS_CONF_DEFAULT`."""
    return os.environ.get("EARS_CONF") or os.path.expanduser(EARS_CONF_DEFAULT)


@dataclass(frozen=True)
class Record:
    """The ``record`` switch: ``on``, the layer that set it, and whether that layer's
    value was invalid (then ``on`` is False: rule 4). ``value`` is the raw value."""

    on: bool
    source: str
    invalid: bool = False
    value: object = None

    def as_dict(self) -> dict:
        return {"on": self.on, "source": self.source, "invalid": self.invalid}

    @property
    def word(self) -> str:
        return "on" if self.on else "off"


@dataclass(frozen=True)
class Days:
    """``record_days``: retention in days (0 = never prune) and the layer that set it.
    ``invalid`` is always False: an invalid layer is skipped (rule 5)."""

    value: int
    source: str
    invalid: bool = False

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "invalid": self.invalid}

    @property
    def words(self) -> str:
        if self.value == 0:
            return "kept until deleted (0 = never prune)"
        return f"{self.value} day{'' if self.value == 1 else 's'}"


@dataclass(frozen=True)
class FileLayer:
    """What ears.json gave: ``data`` is its object, or None when the file layer is
    NOT SET for both keys (rule 2), with ``why`` saying which case it was."""

    path: str
    data: Optional[dict]
    why: Optional[str] = None


def _stat(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def read_file(path: Optional[str] = None) -> FileLayer:
    """ears.json as a :class:`FileLayer`. Never raises."""
    p = path or conf_path()
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return FileLayer(p, None, "no file")
    except (OSError, UnicodeDecodeError) as exc:
        return FileLayer(p, None, f"unreadable ({type(exc).__name__})")
    try:
        d = json.loads(text)
    except ValueError:
        return FileLayer(p, None, "not JSON")
    if not isinstance(d, dict):
        return FileLayer(p, None, f"top level is a {type(d).__name__}, not an object")
    return FileLayer(p, d, None)


def _env_value(env: Mapping[str, str], name: str) -> Optional[str]:
    v = env.get(name)
    return v if v else None  # rule 1: unset or empty is NOT SET


def record_from(cli: Optional[bool], env: Mapping[str, str], file: FileLayer,
                warn: Callable[[str], None]) -> Record:
    """The ``record`` switch from its layers (rules 1-4)."""
    if cli is not None:
        return Record(bool(cli), "cli", False, bool(cli))
    raw = _env_value(env, ENV_RECORD)
    if raw is not None:
        word = raw.strip().lower()
        if word in ON_WORDS:
            return Record(True, "env", False, raw)
        if word in OFF_WORDS:
            return Record(False, "env", False, raw)
        warn(f"{ENV_RECORD}={raw!r} is not one of 1/true/on or 0/false/off: recording OFF (fails closed)")
        return Record(False, "env", True, raw)
    if file.data is not None and KEY_RECORD in file.data:
        v = file.data[KEY_RECORD]
        if isinstance(v, bool):
            return Record(v, "file", False, v)
        warn(f"{file.path}: \"record\": {json.dumps(v)} is not true or false: recording OFF (fails closed)")
        return Record(False, "file", True, v)
    return Record(DEFAULT_RECORD, "default", False, None)


def days_from(env: Mapping[str, str], file: FileLayer, warn: Callable[[str], None]) -> Days:
    """``record_days`` from its layers (rules 1-3, 5). There is no CLI layer."""
    raw = _env_value(env, ENV_DAYS)
    if raw is not None:
        if _DAYS_RE.match(raw):
            return Days(int(raw), "env")
        warn(f"{ENV_DAYS}={raw!r} is not a whole number of days (0 or more): ignored")
    if file.data is not None and KEY_DAYS in file.data:
        v = file.data[KEY_DAYS]
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return Days(v, "file")
        warn(f"{file.path}: \"record_days\": {json.dumps(v)} is not a whole number of days (0 or more): ignored")
    return Days(DEFAULT_DAYS, "default")


@dataclass(frozen=True)
class Notice:
    """``record_notice`` or ``record_spoken_notice``: ``on``, the layer that set it,
    and whether that layer's value was invalid (then ``on`` is True: it fails to
    ON). ``value`` is the raw value."""

    on: bool
    source: str
    invalid: bool = False
    value: object = None

    def as_dict(self) -> dict:
        return {"on": self.on, "source": self.source, "invalid": self.invalid}

    @property
    def word(self) -> str:
        return "on" if self.on else "off"


def notice_from(env_name: str, key: str, env: Mapping[str, str], file: FileLayer,
                warn: Callable[[str], None]) -> Notice:
    """A notice setting from its layers (rules 1-3, and 10-11: invalid fails to ON)."""
    raw = _env_value(env, env_name)
    if raw is not None:
        word = raw.strip().lower()
        if word in ON_WORDS:
            return Notice(True, "env", False, raw)
        if word in OFF_WORDS:
            return Notice(False, "env", False, raw)
        warn(f"{env_name}={raw!r} is not one of 1/true/on or 0/false/off: {key} ON (fails to on)")
        return Notice(True, "env", True, raw)
    if file.data is not None and key in file.data:
        v = file.data[key]
        if isinstance(v, bool):
            return Notice(v, "file", False, v)
        warn(f"{file.path}: \"{key}\": {json.dumps(v)} is not true or false: {key} ON (fails to on)")
        return Notice(True, "file", True, v)
    return Notice(DEFAULT_NOTICE, "default", False, None)


def notice_text(on: bool, days: int) -> str:
    """The chat line (rule 10), true under "off = transient": with recording off the
    caller's audio exists only while the call is live, so it is not "kept"."""
    if not on:
        return "This call is not being kept: audio is deleted when the call ends."
    if days == 0:
        return "This call is being recorded and kept."
    return f"This call is being recorded and kept for {days} day{'' if days == 1 else 's'}."


def stranded_warning(file: FileLayer) -> Optional[str]:
    """The one line for an ears.json left in the old folder, or None.

    It is said only when ``$EARS_CONF`` is unset or empty, the file read was the
    default one and it is absent, and :data:`LEGACY_EARS_CONF` exists. The old
    file is stat-ed, never opened: nothing is read from it."""
    if file.why != "no file" or os.environ.get("EARS_CONF"):
        return None
    new = os.path.expanduser(EARS_CONF_DEFAULT)
    if file.path != new:
        return None
    old = os.path.expanduser(LEGACY_EARS_CONF)
    if not os.path.exists(old):
        return None
    return (f"{new}: no file, but {old} is there: that old file is not read, "
            f"so the file sets nothing; the kit's host step moves it")


def _file_warning(file: FileLayer) -> Optional[str]:
    if file.why is None:
        return None
    stranded = stranded_warning(file)
    if stranded is not None:
        return stranded
    return f"{file.path}: {file.why}: the file sets nothing"


def resolve_all(cli: Optional[bool] = None, *, env: Optional[Mapping[str, str]] = None,
                path: Optional[str] = None) -> tuple[Record, Days, list[str]]:
    """Both settings, read now, and every warning the read raised."""
    env = os.environ if env is None else env
    file = read_file(path)
    warnings: list[str] = []
    fw = _file_warning(file)
    if fw is not None:
        warnings.append(fw)
    return record_from(cli, env, file, warnings.append), days_from(env, file, warnings.append), warnings


def resolve_notices(*, env: Optional[Mapping[str, str]] = None,
                    path: Optional[str] = None) -> tuple[Notice, Notice, list[str]]:
    """``(record_notice, record_spoken_notice, warnings)``, read now."""
    env = os.environ if env is None else env
    file = read_file(path)
    warnings: list[str] = []
    fw = _file_warning(file)
    if fw is not None:
        warnings.append(fw)
    return (notice_from(ENV_NOTICE, KEY_NOTICE, env, file, warnings.append),
            notice_from(ENV_SPOKEN, KEY_SPOKEN, env, file, warnings.append), warnings)


def cli_flag(argv: Iterable[str]) -> tuple[list[str], Optional[bool]]:
    """``(argv without the flags, the CLI layer)``: True for ``--record``, False for
    ``--no-record``, None for neither; the last one given wins (rule 6)."""
    rest, flag = [], None
    for a in argv:
        if a == CLI_ON:
            flag = True
        elif a == CLI_OFF:
            flag = False
        else:
            rest.append(a)
    return rest, flag


class Switch:
    """The switch a long-running process holds (rule 7).

    The CLI flag and the env are taken once, here. ears.json is followed: each
    :meth:`record` or :meth:`days` stats it and re-reads it only when its mtime,
    size or inode changed. Each distinct warning goes to ``log`` once.
    """

    def __init__(self, cli: Optional[bool] = None, *, env: Optional[Mapping[str, str]] = None,
                 path: Optional[str] = None, log: Optional[Callable[[str], None]] = None) -> None:
        src = os.environ if env is None else env
        self.cli = cli
        self._env = {k: src[k] for k in (ENV_RECORD, ENV_DAYS, ENV_NOTICE, ENV_SPOKEN) if k in src}
        self._path = path
        self._log = log if log is not None else (
            lambda m: print(f"recording: {m}", file=sys.stderr, flush=True))
        self._said: set[str] = set()
        self._stamp: object = object()  # never equal to a stat: the first call reads
        self._record: Optional[Record] = None
        self._days: Optional[Days] = None
        self._notice: Optional[Notice] = None
        self._spoken: Optional[Notice] = None

    @property
    def path(self) -> str:
        return self._path or conf_path()

    def env_value(self, name: str) -> str:
        return self._env.get(name, "")

    def _warn(self, msg: str) -> None:
        if msg not in self._said:
            self._said.add(msg)
            self._log(msg)

    def _refresh(self) -> None:
        p = self.path
        stamp = (p, _stat(p))
        if stamp == self._stamp and self._record is not None:
            return
        file = read_file(p)
        fw = _file_warning(file)
        if fw is not None:
            self._warn(fw)
        self._record = record_from(self.cli, self._env, file, self._warn)
        self._days = days_from(self._env, file, self._warn)
        self._notice = notice_from(ENV_NOTICE, KEY_NOTICE, self._env, file, self._warn)
        self._spoken = notice_from(ENV_SPOKEN, KEY_SPOKEN, self._env, file, self._warn)
        self._stamp = stamp

    def record(self) -> Record:
        """The switch now (the file re-read if it changed)."""
        self._refresh()
        return self._record  # type: ignore[return-value]

    def days(self) -> Days:
        """The retention now (the file re-read if it changed)."""
        self._refresh()
        return self._days  # type: ignore[return-value]

    def notice(self) -> Notice:
        """``record_notice`` now: the per-call chat line (the file re-read if it changed)."""
        self._refresh()
        return self._notice  # type: ignore[return-value]

    def spoken_notice(self) -> Notice:
        """``record_spoken_notice`` now: the spoken notice on a kept call."""
        self._refresh()
        return self._spoken  # type: ignore[return-value]

    def __call__(self) -> bool:
        return self.record().on


class Fixed:
    """A switch that never changes: for a test, or a caller that decides itself."""

    def __init__(self, on: bool, days: int = DEFAULT_DAYS, notice: bool = DEFAULT_NOTICE,
                 spoken: bool = DEFAULT_NOTICE) -> None:
        self._record = Record(bool(on), "cli", False, bool(on))
        self._days = Days(days, "default")
        self._notice = Notice(bool(notice), "default")
        self._spoken = Notice(bool(spoken), "default")
        self.cli = bool(on)
        self.path = "(fixed)"

    def env_value(self, name: str) -> str:
        return ""

    def record(self) -> Record:
        return self._record

    def days(self) -> Days:
        return self._days

    def notice(self) -> Notice:
        return self._notice

    def spoken_notice(self) -> Notice:
        return self._spoken

    def __call__(self) -> bool:
        return self._record.on


def _source_words(x, sw, name: Optional[str] = None) -> str:
    if x.source == "env":
        name = name or (ENV_RECORD if isinstance(x, Record) else ENV_DAYS)
        return f"env {name}={sw.env_value(name)!r}" if x.invalid else f"env {name}={sw.env_value(name)}"
    if x.source == "file":
        return f"file {sw.path}"
    return x.source


def line(sw) -> str:
    """The call-start log line's tail: ``off (default), retention 7 days (default)``;
    an invalid layer says so: ``off (env EARS_RECORD='yes' invalid, fails closed)``."""
    rec, days = sw.record(), sw.days()
    bad = " invalid, fails closed" if rec.invalid else ""
    return (f"{rec.word} ({_source_words(rec, sw)}{bad}), "
            f"retention {days.words} ({_source_words(days, sw)})")


def notice_line(sw) -> str:
    """The log's word on the two notices: ``chat line on (default), spoken notice
    off (env EARS_RECORD_SPOKEN_NOTICE=0)``; an invalid layer says it fails to on."""
    out = []
    for what, n, name in (("chat line", sw.notice(), ENV_NOTICE),
                          ("spoken notice", sw.spoken_notice(), ENV_SPOKEN)):
        bad = " invalid, fails to on" if n.invalid else ""
        out.append(f"{what} {n.word} ({_source_words(n, sw, name)}{bad})")
    return ", ".join(out)


# -- the call dir -------------------------------------------------------------------------


def _audio(d: Path) -> list[Path]:
    seen, out = set(), []
    for g in AUDIO_GLOBS:
        for f in sorted(d.glob(g)):
            if f.is_file() and f.name not in seen:
                seen.add(f.name)
                out.append(f)
    return out


def read_kept(d: Path) -> Optional[dict]:
    """The dir's ``recording.json``, or None (none, or not a JSON object)."""
    try:
        v = json.loads((Path(d) / KEPT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _write_json(path: Path, d: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(d, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def keep(d: Path, names: Iterable[str], rec: Record, days: Days, now: Optional[float] = None) -> None:
    """Name ``names`` as kept under the switch in ``d/recording.json``."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    k = read_kept(d) or {"started": now if now is not None else time.time(), "kept": []}
    kept = list(k.get("kept") or [])
    for n in names:
        if n not in kept:
            kept.append(n)
    k.update(kept=kept, record=rec.as_dict(), record_days=days.as_dict())
    _write_json(d / KEPT_FILE, k)


def unkeep(d: Path, names: Iterable[str]) -> None:
    """Take ``names`` back off ``recording.json`` (the switch went off mid-call)."""
    d = Path(d)
    k = read_kept(d)
    if k is None:
        return
    drop = set(names)
    k["kept"] = [n for n in (k.get("kept") or []) if n not in drop]
    _write_json(d / KEPT_FILE, k)


def mark_transient(d: Path, why: str) -> None:
    """Say, in the dir, that it holds audio which must not outlive the call."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    m = d / TRANSIENT_MARK
    if not m.exists():
        m.write_text(f"call {d.name}: {why}. Recording is off: this audio is deleted "
                     f"when the call ends; a bot that crashed deletes it at its next start.\n",
                     encoding="utf-8")


def clean(d: Path, log: Optional[Callable[[str], None]] = None) -> list[str]:
    """Remove every audio file of a TRANSIENT-marked dir that ``recording.json`` does
    not name as kept, then the marker; ``recording.json`` goes too if it names
    nothing. A dir with no marker is left as it is. Returns the names removed."""
    d = Path(d)
    # paths.exists, not Path.exists: on 3.14 an unsearchable dir would read "unmarked"
    # and its transient audio would outlive the call in silence; this way it raises
    if not paths.exists(d / TRANSIENT_MARK):
        return []
    k = read_kept(d)
    kept = set((k or {}).get("kept") or [])
    gone = []
    for f in _audio(d):
        if f.name in kept:
            continue
        try:
            f.unlink()
            gone.append(f.name)
        except FileNotFoundError:
            pass
        except OSError as e:
            if log:
                log(f"could not delete {d.name}/{f.name}: {e}")
    if k is not None and not kept:
        (d / KEPT_FILE).unlink(missing_ok=True)
    (d / TRANSIENT_MARK).unlink(missing_ok=True)
    return gone


def sweep(calls_dir: Path, live: Iterable[str] = (), log: Optional[Callable[[str], None]] = None) -> list[str]:
    """At start: :func:`clean` every call dir a crash left marked TRANSIENT, except a
    live call's (``live``: dir names). Unmarked dirs are never touched. Returns
    the dir names cleaned."""
    root, skip, done = Path(calls_dir), set(live), []
    if not paths.is_dir(root):      # not Path.is_dir: see clean()
        return done
    for m in sorted(root.glob(f"*/{TRANSIENT_MARK}")):
        d = m.parent
        if d.name in skip:
            continue
        gone = clean(d, log)
        done.append(d.name)
        if log:
            log(f"crash sweep removed {len(gone)} audio file(s) from {d.name} (recording was off)")
    return done


def prune(calls_dir: Path, rec: Record, days: int, now: Optional[float] = None,
          log: Optional[Callable[[str], None]] = None, live: Iterable[str] = ()) -> list[str]:
    """Remove the kept audio of every call dir whose ``recording.json`` is older than
    ``days``. It runs ONLY while ``rec`` is on and ``days`` is more than 0: with
    recording off, or 0 days (never prune), it deletes nothing. Only the files
    ``recording.json`` names; text stays, and a dir without ``recording.json``
    (every call made before the switch) is never read or touched. Returns the
    dir names pruned."""
    if not rec.on or days <= 0:
        return []
    root, skip, done = Path(calls_dir), set(live), []
    if not paths.is_dir(root):      # not Path.is_dir: see clean()
        return done
    now = time.time() if now is None else now
    for kf in sorted(root.glob(f"*/{KEPT_FILE}")):
        d = kf.parent
        if d.name in skip:
            continue
        k = read_kept(d)
        if k is None or not k.get("kept"):
            continue
        started = k.get("started")
        if not isinstance(started, (int, float)) or now - started < days * 86400:
            continue
        for n in k["kept"]:
            if "/" in n or n.startswith("."):
                continue    # only a plain name in this dir is ever deleted
            (d / n).unlink(missing_ok=True)
        k.update(kept=[], pruned_at=now)
        _write_json(kf, k)
        done.append(d.name)
        if log:
            log(f"pruned the kept audio of {d.name} (older than {days} days)")
    return done

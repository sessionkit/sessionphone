"""The dc-* console scripts: one target for every tool (PHONE-314).

`uv tool install X` links only X's own executables, never a dependency's, so
the dc-* tools reach a kit install's PATH only as entry points the kit can
re-declare (wheel data scripts cannot be re-exported). Each dc-* entry point
in pyproject.toml names this one function. It reads the name it was run as
(the sessionview pattern), refuses any name that is not a shipped tool, and
execs the real tool: the file of that name in dcbin/, shipped as package data
beside its dc-env (dcbin/ is the allow-list: relative symlinks into the
checkout's bin/, which the build follows).

The tool is told this interpreter as DC_PYTHON, so dc-env (and
dc-log) run their package code in the env that holds sessionphone, wherever
the console script was linked to. A Python tool (dc-log, dc-log-web) is
started on this interpreter directly, never on the first python3 on PATH.

on_path() and status_lines() are the other half (PHONE-302): a shell runs
the FIRST dc-* of a name on PATH, so an older, foreign copy ahead of this
install's answers instead of it, and wrongly ("the bot is NOT RUNNING", no
NAME, "unknown arg"). This code cannot run in its place, so it can only say
so where a stranger already looks: `sessionphone bot status`.
"""
import os
import shutil
import sys
import sysconfig
from pathlib import Path
from typing import NamedTuple

DIR = Path(__file__).resolve().parent / "dcbin"
# shipped beside the tools, sourced by them: a library, never run by name
LIBRARY = frozenset({"dc-env"})
PYTHON_ENV = "DC_PYTHON"


def tools():
    """The names a dc-* console script may run: every file in dcbin/ but dc-env."""
    return frozenset(p.name for p in DIR.iterdir() if p.name.startswith("dc-")) - LIBRARY


def command(name, args):
    """The argv to exec for tool NAME with ARGS. The path is resolved, so a
    checkout (an editable install) runs bin/'s own file, exactly as before."""
    tool = os.path.realpath(DIR / name)
    with open(tool, "rb") as f:
        shebang = f.readline()
    if shebang.startswith(b"#!") and b"python" in shebang:
        return [sys.executable, tool, *args]
    return [tool, *args]


def homes():
    """The folders that hold this install's own copy of each tool, both routes:
    the scripts folder of the interpreter running this code, where the console
    scripts are (PHONE-314; a `uv tool` link in ~/.local/bin resolves into it),
    and dcbin/, whose files resolve into the checkout's bin/ on an editable
    install."""
    return [Path(sysconfig.get_path("scripts")), DIR]


class Hit(NamedTuple):
    """The first file named NAME on PATH, and whether it is this install's."""
    name: str
    found: str               # the file on PATH: its folder is the PATH entry
    resolves: str            # where that file resolves, links followed
    mine: bool               # it resolves to this install's own copy
    ours: str | None         # this install's copy (not resolved), None when it has none


def _ours(name, dirs):
    """-> (this install's copy of NAME as the first home holds it, every one's realpath)."""
    have = [d / name for d in dirs if (d / name).is_file()]
    return (str(have[0]) if have else None), {os.path.realpath(p) for p in have}


def on_path(path=None, dirs=None):
    """A Hit for each shipped tool PATH has, in name order. A name absent from
    PATH has none: absent is not foreign. PATH defaults to this process's,
    DIRS to homes(). A hit is MINE when its realpath is the realpath of the
    tool in any of DIRS, so a link to this install's copy is mine too."""
    dirs = homes() if dirs is None else [Path(d) for d in dirs]
    hits = []
    for name in sorted(tools()):
        found = shutil.which(name, path=path)
        if found is None:
            continue
        ours, real = _ours(name, dirs)
        resolves = os.path.realpath(found)
        hits.append(Hit(name, found, resolves, resolves in real, ours))
    return hits


def foreign(hits):
    """The hits that are not this install's: what a shell runs instead of it."""
    return [h for h in hits if not h.mine]


HEAD = "dc tools: "


def status_lines(hits, total=None):
    """`bot status`'s `dc tools:` lines for HITS (on_path()): one ok line when
    every tool on PATH is this install's; else, per PATH folder, the foreign
    ones, where they resolve, this install's copies and the fix."""
    total = len(tools()) if total is None else total
    if not hits:
        return [f"{HEAD}none of the {total} dc-* tools is on PATH"]
    bad = foreign(hits)
    if not bad:
        where = " ".join(dict.fromkeys(os.path.dirname(h.found) for h in hits))
        some = "all" if len(hits) == total else f"{len(hits)} of"
        return [f"{HEAD}ok, {some} {total} on PATH are this install's ({where})"]
    lines = [(f"{HEAD}{len(bad)} of the dc-* tools on PATH are NOT this install's, so a "
              "shell runs them instead of it and their answers can be wrong:")]
    groups = {}
    for h in bad:
        key = (os.path.dirname(h.found), os.path.dirname(h.resolves))
        groups.setdefault(key, []).append(h.name)
    for (entry, into), names in groups.items():
        to = "" if into == entry else f", links into {into}"
        lines.append(f"  {' '.join(names)}: in {entry}{to}")
    mine = list(dict.fromkeys(os.path.dirname(h.ours) for h in bad if h.ours))
    if mine:
        lines.append(f"  this install's copies are in {' and '.join(mine)}")
    entries = " and ".join(dict.fromkeys(e for e, _ in groups))
    first = f" or put {mine[0]} ahead of {entries} on PATH" if mine else ""
    lines.append(f"  Fix: remove or repoint those in {entries}{first}.")
    return lines


def main():
    name = os.path.basename(sys.argv[0])
    if name not in tools():
        print(f"sessionphone.dctools: '{name}' is not a dc-* tool this package ships "
              f"(known: {' '.join(sorted(tools()))})", file=sys.stderr)
        return 2
    argv = command(name, sys.argv[1:])
    os.environ[PYTHON_ENV] = sys.executable
    os.execv(argv[0], argv)


if __name__ == "__main__":
    sys.exit(main())

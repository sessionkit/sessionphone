"""The real home never changes under the suite (PHONE-264): the snapshot and its check.

A test, or a child process a test started, once wrote ~/.sessionphone/.bot.lock in the
real home of the Mac running the suite. tests/conftest.py now moves HOME (and
SESSIONPHONE_ROOT) to tmp before any sessionphone import, and again for every test. This
module is the backstop behind that move: conftest snapshots the real home's sessionphone
paths when it loads, and again when the run ends, and the run FAILS if they differ.

What is watched (watched()): the default root, ~/.sessionphone; the phone's kit folder,
~/.sessionkit/phone (phone.json lives under it); and the bot's launchd plists in
~/Library/LaunchAgents, today's label and the legacy one. A snapshot keeps, for each
path under a watched one, its mtime, size and mode, never its bytes: it reads nothing
and writes nothing, so the guard itself never touches what it guards.

The home is an argument, never read here: a test of the guard points it at a fake home
(tests/test_home_guard.py), so proving the guard fires never writes the real one.
"""
from __future__ import annotations

import fnmatch
import os
import pathlib

# The bot's launchd job, as service.plist_path names it (service.LABEL_PREFIX and
# service.LEGACY_LABEL_PREFIX). Spelt here, not imported: this module loads before
# any sessionphone import, so the seat's environment is dropped first.
PLIST_GLOBS = ("dev.sessionkit.phone.bot*.plist", "com.sessionphone.bot*.plist")


def watched(home) -> list[tuple[pathlib.Path, tuple[str, ...] | None]]:
    """-> (path, globs) for each place under HOME the suite must never change.
    globs None: the whole tree under path. Otherwise only path's direct entries
    whose name matches one of the globs."""
    home = pathlib.Path(home)
    return [(home / ".sessionphone", None),
            (home / ".sessionkit" / "phone", None),
            (home / "Library" / "LaunchAgents", PLIST_GLOBS)]


def _stat(p: str):
    try:
        st = os.lstat(p)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_mode)


def snapshot(home) -> dict[str, tuple[int, int, int]]:
    """{path: (mtime_ns, size, mode)} for every path watched() names under HOME
    that is there now, the watched paths themselves included. Symlinks are not
    followed. Something that vanishes mid-walk is simply not in it."""
    seen: dict[str, tuple[int, int, int]] = {}
    for top, globs in watched(home):
        top = os.fspath(top)
        if globs is not None:
            try:
                names = os.listdir(top)
            except OSError:
                continue
            for n in names:
                if any(fnmatch.fnmatch(n, g) for g in globs):
                    st = _stat(os.path.join(top, n))
                    if st is not None:
                        seen[os.path.join(top, n)] = st
            continue
        st = _stat(top)
        if st is None:
            continue
        seen[top] = st
        for dp, dns, fns in os.walk(top):      # os.walk never follows a symlinked dir
            for n in dns + fns:
                p = os.path.join(dp, n)
                st = _stat(p)
                if st is not None:
                    seen[p] = st
    return seen


def changes(before: dict, after: dict) -> list[str]:
    """Each path that differs, one line each: `+ path` made, `- path` gone,
    `~ path (what changed)` changed. Empty: nothing changed."""
    out = []
    for p in sorted(set(before) | set(after)):
        b, a = before.get(p), after.get(p)
        if b == a:
            continue
        if b is None:
            out.append(f"+ {p}")
        elif a is None:
            out.append(f"- {p}")
        else:
            what = [k for k, x, y in zip(("mtime", "size", "mode"), b, a) if x != y]
            out.append(f"~ {p} ({', '.join(what)})")
    return out


def report(home, lines: list[str]) -> str:
    """The failure the run ends with, naming each change."""
    return (f"the suite changed the REAL home's sessionphone paths under {home} (PHONE-264).\n"
            "No test may reach them: HOME and SESSIONPHONE_ROOT are tmp for every test, so a\n"
            "test, or a process it started, named the real home some other way (an env\n"
            "without HOME, a path bound before conftest moved it, pwd's home dir).\n"
            "Changed:\n  " + "\n  ".join(lines) + "\n"
            "Do NOT clean these up from a test: find the test (run its files one by one) and\n"
            "point it at tmp. A bot running on this Mac during the run changes them too.")

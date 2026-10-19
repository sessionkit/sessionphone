"""The venv's interpreter under a role name, so ps and top show it.

Without this the phone bots show up as "Python". The name is
session-<role>, with the role after the hyphen
where it stands out. So it is session-phone here, and session-ears and
session-mouth in sessionvoice. Keep a name to 15 characters or fewer, or
macOS cuts it short.

macOS names a process after the file it executes, so a symlink would still
show the target's name. That is why the launchd job runs <venv>/libexec/<name>,
a HARD link to the venv's real interpreter. It lives in libexec/, so bin/ and
its entry points are left alone. Python still finds pyvenv.cfg one
level up, so sys.prefix is still the venv.

A hard link pins the inode it was made from. After a venv rebuild, a
`uv sync` that swaps the interpreter, or a uv Python patch upgrade,
bin/python resolves to a new binary while the link keeps the old one over the
new stdlib. So each call compares inodes and re-links when they differ.

One function, on purpose: voicemode (mlx-audio) and sessionvoice
want the same thing, and this is the whole of it.
"""
from __future__ import annotations

import os
import pathlib
import sys


def named_python(name: str = "session-phone", python: str | None = None) -> str:
    """-> <venv>/libexec/<name>, a hard link to the venv's real interpreter,
    made or re-made as needed. `python` is any interpreter path in the venv
    (default sys.executable, which may itself be the link).

    Never costs the caller a working interpreter: when the link cannot be made
    (not a venv, or the interpreter is on another filesystem), it returns
    `python` unchanged."""
    python_path = pathlib.Path(python or sys.executable)
    venv = python_path.parent.parent
    if not (venv / "pyvenv.cfg").exists():
        return str(python_path)
    bin_python = venv / "bin" / "python"
    real = (bin_python if bin_python.exists() else python_path).resolve()
    link = venv / "libexec" / name
    try:
        if link.exists() and link.stat().st_ino == real.stat().st_ino:
            return str(link)
        link.parent.mkdir(exist_ok=True)
        part = link.with_name(f".{name}.part")
        part.unlink(missing_ok=True)
        os.link(real, part)
        os.replace(part, link)        # atomic: a bot starting now sees the old link or the new one
    except OSError:
        return str(python_path)
    return str(link)

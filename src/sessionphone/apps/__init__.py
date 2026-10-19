"""The webxdc apps sessionphone ships, and their build into a `.xdc`.

Each app is a directory here, package data, so a wheel install has it without
a checkout. A `.xdc` is a zip with `index.html` at its root. `build` makes it
deterministically: the names sorted, one fixed mtime and mode, so the same
tree gives the same bytes.

`webxdc.js` is never shipped: the messenger provides it at run time, and a
copy inside the zip would shadow it.

    sessionphone app list
    sessionphone app build guide-trail [-o PATH]

Send the built file with `dc-send-file CHAT_ID PATH`. The ack names the new
message's id, and that id is what `dc-xdc-realtime` takes.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import zipfile

APPS_DIR = pathlib.Path(__file__).resolve().parent

# The webxdc runtime's own script: never inside a .xdc.
RUNTIME = "webxdc.js"
# One timestamp for every entry: the earliest a zip can carry.
EPOCH = (1980, 1, 1, 0, 0, 0)
MODE = 0o644


class UnknownApp(Exception):
    """No app of that name ships here. Exit code 2."""


def names() -> list[str]:
    """The apps that ship, sorted: each directory here holding an index.html."""
    return sorted(p.name for p in APPS_DIR.iterdir() if p.is_dir() and (p / "index.html").is_file())


def app_dir(name: str) -> pathlib.Path:
    """The directory of the app NAME, or UnknownApp naming the ones that ship."""
    if name not in names():
        shipped = ", ".join(names()) or "none"
        raise UnknownApp(f"no app {name!r}; the apps are: {shipped}")
    return APPS_DIR / name


def files(name: str) -> list[str]:
    """The app's files, as zip names relative to its directory, sorted.

    Hidden files, caches and the runtime's webxdc.js are left out."""
    root = app_dir(name)
    out = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if not path.is_file() or any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        if rel.name == RUNTIME:
            continue
        out.append(rel.as_posix())
    return sorted(out)


def default_path(name: str, root: pathlib.Path) -> pathlib.Path:
    """Where `build` writes when no -o is given: ROOT/apps/NAME.xdc."""
    return root / "apps" / f"{name}.xdc"


def build(name: str, out: pathlib.Path) -> pathlib.Path:
    """Zip the app NAME into OUT (a .xdc) and return OUT.

    Written beside OUT, then renamed over it, so a reader never sees half a
    file. The parent directory is made if it is missing."""
    root = app_dir(name)
    out = pathlib.Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in files(name):
                info = zipfile.ZipInfo(rel, date_time=EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100000 | MODE) << 16   # a regular file, rw-r--r--
                info.create_system = 3                         # unix, so the mode above is read
                zf.writestr(info, (root / rel).read_bytes())
        os.chmod(tmp, MODE)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return out

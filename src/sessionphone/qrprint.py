"""One QR printer: half-block text for a terminal, and a PNG.

`sessionphone invite --qr` and the web enrol QR print through `text`, so the
two QRs a person sees behave the same. It lives outside `web/` and
`account.py` so the invite never imports the web package or the rpc client.
segno is a core dependency; it is imported when a QR is made.

The text print is Unicode half-blocks, two module rows a line, with the
4-module quiet zone, and no ANSI escape: it reads the same in a terminal and
in a tool result. No plain-text print is the right way round on every theme,
because a space is the background and a block is the foreground:

- default (segno's own compact print): a DARK module is a space. True on a
  dark-theme terminal: dark modules on a light quiet zone.
- `invert=True`: a dark module is a block. True on a light-theme terminal.

Error level "m" throughout, as `account.qr_svg` uses.
"""
from __future__ import annotations

import io
import pathlib

ERROR = "m"
BORDER = 4          # the quiet zone, in modules
PNG_SCALE = 12      # px a module: a ~57-module invite is ~680 px, scannable at arm's length

# (top, bottom) -> character, 1 is a dark module. The default is segno's map.
_INVERTED = {(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}


def _make(data: str):
    """Always a full QR, never a Micro QR (which a phone's Delta Chat scanner
    is not promised to read). segno picks Micro only for data of 13 bytes or
    less at level "m", which no invite or enrol URL is, so the default print
    stays segno's own, byte for byte."""
    import segno

    return segno.make(data, error=ERROR, micro=False)


def text(data: str, invert: bool = False) -> str:
    """The QR for `data` as half-block text, one line per two module rows,
    each line ending in a newline.

    The default is segno's compact terminal print, byte for byte. Inverted, a
    dark module is a full block, and an odd last row is padded with LIGHT (a
    space), so the bottom edge never prints a stray bar."""
    qr = _make(data)
    if not invert:
        buf = io.StringIO()
        qr.terminal(out=buf, compact=True)   # segno's quiet zone: 4 for a QR
        return buf.getvalue()
    rows = [[int(bool(v)) for v in row] for row in qr.matrix_iter(border=BORDER)]
    if len(rows) % 2:
        rows.append([0] * len(rows[0]))
    return "".join("".join(_INVERTED[pair] for pair in zip(top, bottom)) + "\n"
                   for top, bottom in zip(rows[::2], rows[1::2]))


def png(data: str, path: pathlib.Path, scale: int = PNG_SCALE) -> pathlib.Path:
    """Write the QR for `data` to `path` as a PNG, dark on white, with the
    quiet zone; overwrite allowed. Returns the resolved path. An OSError (a
    missing parent, no permission) is the caller's to report."""
    path = pathlib.Path(path)
    with path.open("wb") as f:
        _make(data).save(f, kind="png", scale=scale, border=BORDER)
    return path.resolve()

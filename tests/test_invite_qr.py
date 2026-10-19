"""`sessionphone invite --qr / --invert / --png`: the invite as a QR (PHONE-164).

The scan proof needs no decoder: the printed half-block text, in either
polarity, is read back into its module matrix and compared with segno's own
matrix for the EXACT saved link (the method of PHONE-136's
tests/web/test_enrol_qr.py, with the block map as a parameter). The PNG is
decoded with the standard library alone (zlib and the PNG filter bytes), one
pixel sampled per module.

Fakes only: a temp SESSIONPHONE_ROOT at 0700 holding an invite.txt. No
account is opened and no relay is dialled.
"""
from __future__ import annotations

import struct
import zlib

import pytest
import segno
from click.testing import CliRunner

from sessionphone import qrprint
from sessionphone.cli import main
from sessionphone.web import enrol_qr

# The shape of a real invite (about 165 characters); every value is made up.
LINK = ("https://i.delta.chat/#0123456789ABCDEF0123456789ABCDEF01234567&v=3"
        "&i=AbCdEfGhIjKlMnOpQrStUvWx&s=ZyXwVuTsRqPoNmLkJiHgFeDc"
        "&a=bot%40relay.example&n=Test+Agent")

# character -> (top, bottom), 1 is a dark module
DEFAULT = {" ": (1, 1), "▀": (0, 1), "▄": (1, 0), "█": (0, 0)}   # segno's compact print
INVERTED = {" ": (0, 0), "▀": (1, 0), "▄": (0, 1), "█": (1, 1)}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A set-up root: 0700, with the invite `setup` would have saved."""
    r = tmp_path / "phone"
    r.mkdir(mode=0o700)
    r.chmod(0o700)
    (r / "invite.txt").write_text(LINK + "\n")
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    return r


def run(*args: str):
    return CliRunner().invoke(main, ["invite", *args])


def want_matrix(data: str) -> list[list[int]]:
    """segno's matrix for `data`, with the 4-module quiet zone."""
    return [[int(bool(v)) for v in row] for row in segno.make(data, error="m", micro=False).matrix_iter(border=4)]


def printed_matrix(lines: list[str], blocks: dict) -> list[list[int]]:
    """Read half-block text back into module rows, two rows a line."""
    rows = []
    for line in lines:
        assert set(line) <= set(blocks), f"not a QR line: {line!r}"
        rows.append([blocks[c][0] for c in line])
        rows.append([blocks[c][1] for c in line])
    return rows


def assert_text_is(lines: list[str], data: str, blocks: dict, pad: int) -> None:
    """The printed QR is `data`'s matrix; an odd height pads the last line's
    lower half with `pad` (dark in segno's print, light inverted)."""
    got, want = printed_matrix(lines, blocks), want_matrix(data)
    if len(want) % 2:
        assert got[-1] == [pad] * len(want[0]), "the padding half is not the background"
        got = got[:-1]
    assert got == want, "the printed QR is not the saved link"


def split_qr(out: str) -> tuple[list[str], list[str]]:
    """(the QR's lines, the lines after the blank line)."""
    lines = out.split("\n")
    blank = lines.index("")
    return lines[:blank], lines[blank + 1:]


# ---- a stdlib PNG reader: greyscale or palette, any bit depth to 8 --------

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def read_png(path) -> list[list[int]]:
    """The PNG's pixels as rows of luminance-ish values (0 black .. 255 white)."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    i, idat, palette = 8, b"", None
    while i < len(data):
        (n,), kind = struct.unpack(">I", data[i:i + 4]), data[i + 4:i + 8]
        body = data[i + 8:i + 8 + n]
        if kind == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = [body[j:j + 3] for j in range(0, n, 3)]
        elif kind == b"IDAT":
            idat += body
        i += 12 + n
    assert colour in (0, 3) and depth <= 8 and interlace == 0, (colour, depth, interlace)
    raw = zlib.decompress(idat)
    stride = (width * depth + 7) // 8
    bpp = 1                                     # bytes a pixel, rounded up: always 1 here
    prev, pixels, at = bytearray(stride), [], 0
    for _ in range(height):
        f, line = raw[at], bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
            line[x] = (line[x] + (0, a, b, (a + b) // 2, _paeth(a, b, c))[f]) & 0xFF
        prev = line
        bits = "".join(f"{byte:08b}" for byte in line)
        vals = [int(bits[k * depth:(k + 1) * depth], 2) for k in range(width)]
        if colour == 3:
            vals = [palette[v][0] for v in vals]
        else:
            vals = [v * 255 // ((1 << depth) - 1) for v in vals]
        pixels.append(vals)
    return pixels


def png_matrix(path, scale: int = qrprint.PNG_SCALE) -> list[list[int]]:
    """One pixel from the middle of each module: 1 is dark."""
    px = read_png(path)
    assert len(px) % scale == 0 and len(px[0]) % scale == 0
    mid = scale // 2
    return [[int(row[x] < 128) for x in range(mid, len(row), scale)] for row in px[mid::scale]]


# ---- 1. no flags: unchanged ----------------------------------------------

def test_no_flags_is_the_link_and_nothing_else(root):
    r = run()
    assert r.exit_code == 0, r.output
    assert r.stdout == LINK + "\n"


# ---- 2. --qr: the saved link, scannable, no ANSI -------------------------

def test_qr_decodes_to_the_saved_link_then_a_blank_then_the_link(root):
    r = run("--qr")
    assert r.exit_code == 0, r.output
    qr, after = split_qr(r.stdout)
    assert_text_is(qr, LINK, DEFAULT, pad=1)
    assert after == [LINK, ""]                # the link, then the final newline
    assert "\x1b" not in r.stdout


def test_qr_is_segnos_own_compact_print(root):
    import io

    buf = io.StringIO()
    segno.make(LINK, error="m").terminal(out=buf, compact=True)
    assert run("--qr").stdout == buf.getvalue() + "\n" + LINK + "\n"


# ---- 3. --qr --invert: the same matrix, the other polarity ---------------

def test_inverted_qr_decodes_to_the_same_link(root):
    r = run("--qr", "--invert")
    assert r.exit_code == 0, r.output
    qr, after = split_qr(r.stdout)
    assert_text_is(qr, LINK, INVERTED, pad=0)
    assert after == [LINK, ""]
    assert "\x1b" not in r.stdout
    assert r.stdout != run("--qr").stdout


@pytest.mark.parametrize("data", ["A", "https://example.org/x", LINK, "x" * 400])
def test_both_polarities_decode_at_every_size(data):
    """Version 1 up to a large version. A QR is 4k+21 modules a side, so with
    the quiet zone the height is always ODD: the last line's lower half is
    padding, dark in segno's print and light inverted (no stray bar)."""
    assert len(want_matrix(data)) % 2 == 1
    for invert, blocks, pad in ((False, DEFAULT, 1), (True, INVERTED, 0)):
        out = qrprint.text(data, invert=invert)
        assert out.endswith("\n") and "\x1b" not in out
        lines = out.rstrip("\n").split("\n")
        assert len({len(x) for x in lines}) == 1, "ragged lines"
        assert_text_is(lines, data, blocks, pad)


def test_short_data_is_never_a_micro_qr():
    """segno alone would make a Micro QR (2-module quiet zone) for one letter."""
    assert segno.make("A", error="m").is_micro
    assert len(qrprint.text("A").rstrip("\n").split("\n")) == (21 + 8 + 1) // 2


# ---- 4. --png: a PNG of the same matrix, its path last -------------------

def test_png_decodes_to_the_saved_link_and_prints_only_its_path(root, tmp_path):
    out = tmp_path / "invite.png"
    r = run("--png", str(out))
    assert r.exit_code == 0, r.output
    assert r.stdout == f"{out.resolve()}\n"   # the path only: no link, no QR
    assert png_matrix(out) == want_matrix(LINK)
    side = len(want_matrix(LINK)) * qrprint.PNG_SCALE
    assert len(read_png(out)) == side >= 600   # big enough for a phone at arm's length


def test_png_overwrites(root, tmp_path):
    out = tmp_path / "invite.png"
    out.write_bytes(b"not a png")
    assert run("--png", str(out)).exit_code == 0
    assert png_matrix(out) == want_matrix(LINK)


def test_png_relative_path_prints_the_absolute_one(root, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = run("--png", "rel.png")
    assert r.exit_code == 0, r.output
    last = r.stdout.rstrip("\n").split("\n")[-1]
    assert last == str((tmp_path / "rel.png").resolve()) and last.startswith("/")


# ---- 5. --png into a missing dir, or a write error: exit 1, one line -----

def test_png_into_a_missing_directory_is_exit_1_one_line(root, tmp_path):
    out = tmp_path / "nope" / "invite.png"
    r = run("--png", str(out))
    assert r.exit_code == 1
    assert r.stdout == ""
    assert r.stderr.count("\n") == 1 and "no such directory" in r.stderr and "Traceback" not in r.stderr
    assert not out.exists()


def test_png_parent_is_a_file_says_not_a_directory_exit_1(root, tmp_path):
    """A parent that exists but is a file is named as such, not as missing."""
    f = tmp_path / "afile"
    f.write_text("x")
    r = run("--png", str(f / "invite.png"))
    assert r.exit_code == 1
    assert r.stdout == ""
    assert r.stderr.count("\n") == 1 and "not a directory" in r.stderr and "Traceback" not in r.stderr
    assert f.read_text() == "x"


def test_png_write_error_is_exit_1_one_line(root, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        r = run("--png", str(ro / "invite.png"))
    finally:
        ro.chmod(0o700)
    assert r.exit_code == 1
    assert r.stdout == ""
    assert r.stderr.count("\n") == 1 and "could not write" in r.stderr and "Traceback" not in r.stderr


def test_png_onto_a_directory_is_a_write_error_exit_1_not_a_usage_error(root, tmp_path):
    """Exit 2 means not set up or unsafe: a directory given as FILE is a
    write error, exit 1 and one plain line, not click's usage error (exit 2)."""
    d = tmp_path / "adir"
    d.mkdir()
    r = run("--png", str(d))
    assert r.exit_code == 1
    assert r.stdout == ""
    assert r.stderr.count("\n") == 1 and "could not write" in r.stderr and "Usage" not in r.stderr
    assert d.is_dir()


# ---- 6. not set up / unsafe: exit 2, nothing written ---------------------

def test_not_set_up_is_exit_2_and_writes_no_png(root, tmp_path):
    (root / "invite.txt").unlink()
    out = tmp_path / "invite.png"
    for args in ((), ("--qr",), ("--png", str(out)), ("--qr", "--png", str(out))):
        r = run(*args)
        assert r.exit_code == 2, (args, r.output)
        assert r.stdout == ""
    assert not out.exists()


def test_unsafe_root_is_exit_2_and_writes_no_png(root, tmp_path):
    root.chmod(0o755)
    out = tmp_path / "invite.png"
    r = run("--qr", "--png", str(out))
    assert r.exit_code == 2
    assert "must be 0700" in r.stderr
    assert not out.exists()


# ---- 7. --invert without --qr: exit 1 ------------------------------------

@pytest.mark.parametrize("args", [("--invert",), ("--invert", "--png", "x.png")])
def test_invert_without_qr_is_exit_1(root, tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    r = run(*args)
    assert r.exit_code == 1
    assert r.stdout == ""
    assert r.stderr == "sessionphone: --invert needs --qr\n"
    assert not (tmp_path / "x.png").exists()


# ---- 8. --qr --png together: QR, blank, link, path -----------------------

@pytest.mark.parametrize("invert", [False, True])
def test_qr_and_png_together(root, tmp_path, invert):
    out = tmp_path / "invite.png"
    r = run("--qr", *(["--invert"] if invert else []), "--png", str(out))
    assert r.exit_code == 0, r.output
    qr, after = split_qr(r.stdout)
    assert_text_is(qr, LINK, INVERTED if invert else DEFAULT, pad=0 if invert else 1)
    assert after == [LINK, str(out.resolve()), ""]
    assert png_matrix(out) == want_matrix(LINK)       # the PNG is never inverted


# ---- 9. the web enrol QR prints through the same printer, unchanged -------

def test_enrol_qr_text_is_the_shared_printer_and_segnos_print():
    import io

    url = "https://host.example:8775/#code=ABCDEFGHJK"
    buf = io.StringIO()
    segno.make(url, error="m").terminal(out=buf, compact=True)
    assert enrol_qr.qr_text(url) == qrprint.text(url) == buf.getvalue()


def test_invite_never_opens_the_account(root, monkeypatch):
    """The bot holds the account: the invite reads the saved file only."""
    from sessionphone import account

    def boom(*a, **k):
        raise AssertionError("the account was opened")

    monkeypatch.setattr(account, "_open", boom)
    monkeypatch.setattr(account, "Rpc", boom)
    assert run("--qr", "--png", str(root.parent / "i.png")).exit_code == 0
    assert not (root / "accounts").exists()


def test_the_decoders_tell_one_link_from_another(root, tmp_path):
    """CONTROL: the proof is not vacuous. One changed character in the auth
    token is a different matrix, in the text and in the PNG."""
    other = LINK.replace("s=Z", "s=Y")
    qr, _ = split_qr(run("--qr").stdout)
    assert printed_matrix(qr, DEFAULT)[:-1] != want_matrix(other)
    out = tmp_path / "invite.png"
    run("--png", str(out))
    assert png_matrix(out) != want_matrix(other)

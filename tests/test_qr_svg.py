"""The connect page's QR: `account.qr_svg` and the one page it goes into.

The code must scan wherever the page is shown, so:

- the SVG has a `viewBox` and no fixed size, so CSS scales the whole code to
  its box (with a fixed size and no viewBox, a box smaller than the drawing
  crops it, finder patterns included);
- the light modules and the 4-module quiet zone are painted white, not left
  transparent, so the code is dark-on-white on a dark page too;
- the page, `src/sessionphone/pages/qr-page.html` (package data, so a checkout,
  a wheel and a kit install print the same one), keeps a white card behind the
  code in dark mode, scales the SVG, and says what to do when the phone waits
  at the handshake.

The scan proof needs no decoder: the SVG's dark path is read back into its
module matrix and compared with segno's own matrix for the exact link, the
method of tests/test_invite_qr.py. A real decoder round-trip runs too when
zxing-cpp and Pillow are importable, and skips cleanly when they are not.
"""
from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import pytest
import segno

from sessionphone import account

# The shape of a real invite (about 165 characters); every value is made up.
LINK = ("https://i.delta.chat/#0123456789ABCDEF0123456789ABCDEF01234567&v=3"
        "&i=AbCdEfGhIjKlMnOpQrStUvWx&s=ZyXwVuTsRqPoNmLkJiHgFeDc"
        "&a=bot%40relay.example&n=Test+Agent")
BORDER = 4


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "phone"
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    return r


def want_matrix(data: str) -> list[list[int]]:
    """segno's matrix for `data`, with the 4-module quiet zone."""
    return [[int(bool(v)) for v in row]
            for row in segno.make(data, error="m", micro=False).matrix_iter(border=BORDER)]


def root_tag(svg: str) -> str:
    m = re.match(r"\s*(<svg\b[^>]*>)", svg)
    assert m, svg[:80]
    return m.group(1)


def side(svg: str) -> int:
    """The code's side in modules, from the white fill that covers it."""
    m = re.search(r'<path fill="#fff" d="M0 0h(\d+)v(\d+)h-(\d+)z"', svg)
    assert m, "no white fill under the code"
    w, h, back = map(int, m.groups())
    assert w == h == back
    return w


def svg_matrix(svg: str) -> list[list[int]]:
    """Read segno's dark path back into module rows: 1 is a dark module.

    The path is runs of `h` (a dark run along a row) joined by `m` moves,
    in module units, each row drawn at its half-module centre line."""
    d = re.search(r'<path class="qrline" stroke="#000" d="([^"]+)"', svg)
    assert d, "no dark-module path"
    n = side(svg)
    grid = [[0] * n for _ in range(n)]
    x = y = 0.0
    for op, a, b in re.findall(r"([Mmh])(-?\d+(?:\.\d+)?)(?: (-?\d+(?:\.\d+)?))?", d.group(1)):
        a = float(a)
        if op == "M":
            x, y = a, float(b)
        elif op == "m":
            x, y = x + a, y + float(b)
        else:
            row = int(y)
            for col in range(int(x), int(x + a)):
                grid[row][col] = 1
            x += a
    return grid


# ---- the SVG ----------------------------------------------------------------

def test_svg_scales_to_its_box():
    tag = root_tag(account.qr_svg(LINK))
    assert "viewBox=" in tag
    assert not re.search(r"\s(width|height)=", tag), tag


def test_svg_viewbox_covers_the_whole_code():
    svg = account.qr_svg(LINK)
    n = len(want_matrix(LINK))
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', root_tag(svg))
    assert m and m.group(1) == m.group(2)
    scale = int(m.group(1)) / n
    assert scale == int(scale) and f"scale({int(scale)})" in svg


def test_svg_paints_the_light_modules_and_quiet_zone_white():
    svg = account.qr_svg(LINK)
    assert side(svg) == len(want_matrix(LINK))       # the fill spans code + quiet zone
    assert 'stroke="#000"' in svg


def test_svg_quiet_zone_is_four_modules():
    grid = svg_matrix(account.qr_svg(LINK))
    n = len(grid)
    assert n == len(segno.make(LINK, error="m", micro=False).matrix) + 2 * BORDER
    edge = grid[:BORDER] + grid[-BORDER:] + [row[:BORDER] + row[-BORDER:] for row in grid]
    assert not any(any(row) for row in edge), "a dark module inside the quiet zone"
    assert grid[BORDER][BORDER] == 1                  # the finder starts right after it


@pytest.mark.parametrize("data", ["A", "https://example.org/x", LINK, "x" * 400],
                         ids=["v1", "short-url", "invite", "large"])
def test_svg_reads_back_to_segnos_matrix(data):
    """Version 1 up to a large version, and never a Micro QR."""
    assert svg_matrix(account.qr_svg(data)) == want_matrix(data)


def test_svg_decodes_with_a_real_decoder():
    zxingcpp = pytest.importorskip("zxingcpp")
    image = pytest.importorskip("PIL.Image")
    grid = svg_matrix(account.qr_svg(LINK))
    px = 8
    im = image.new("L", (len(grid) * px, len(grid) * px), 255)
    for y, row in enumerate(grid):
        for x, dark in enumerate(row):
            if dark:
                im.paste(0, (x * px, y * px, (x + 1) * px, (y + 1) * px))
    assert [r.text for r in zxingcpp.read_barcodes(im)] == [LINK]


# ---- the page ---------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
PAGE_FILE = ROOT / "src" / "sessionphone" / "pages" / "qr-page.html"


def page_text() -> str:
    return account.default_page()


def test_the_page_is_package_data_read_by_importlib_resources():
    """The page the package reads is the file in src/, byte for byte."""
    assert account.QR_PAGE == "pages/qr-page.html"
    assert resources.files("sessionphone").joinpath(account.QR_PAGE).read_bytes() == PAGE_FILE.read_bytes()
    assert account.default_page() == PAGE_FILE.read_text(encoding="utf-8")


def test_there_is_one_page_and_no_second_copy():
    """A second copy (a page string in the code, or one in the skill) drifts."""
    assert not hasattr(account, "BUILTIN_QR_PAGE")
    assert not hasattr(account, "default_template")
    copies = [p for p in ROOT.glob("**/qr-page.html")
              if not {".venv", ".git", "dist", "build"} & set(p.relative_to(ROOT).parts)]
    assert copies == [PAGE_FILE]
    source = (ROOT / "src" / "sessionphone" / "account.py").read_text(encoding="utf-8")
    assert "<!doctype html>" not in source.lower()
    assert "parents[" not in source


def css_rules(page: str) -> tuple[dict[str, str], dict[str, str]]:
    """(rules outside any @media, rules inside the dark-mode @media)."""
    style = re.search(r"<style>(.*?)</style>", page, re.DOTALL).group(1)
    dark: dict[str, str] = {}
    m = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{(.*)\}\s*$", style, re.DOTALL)
    if m:
        style = style[:m.start()]
        dark = {sel.strip(): body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", m.group(1))}
    plain = {sel.strip(): body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style)}
    return plain, dark


def background(body: str) -> str | None:
    m = re.search(r"background(?:-color)?\s*:\s*([^;]+)", body)
    return m.group(1).strip().lower() if m else None


def test_page_keeps_a_white_card_behind_the_code_in_dark_mode():
    plain, dark = css_rules(page_text())
    assert background(plain[".qr"]) in ("#fff", "#ffffff", "white")
    for sel, body in dark.items():                    # dark mode must not undo it
        if ".qr" in sel:
            assert background(body) in (None, "#fff", "#ffffff", "white"), sel


def luminance(hex_colour: str) -> float:
    """WCAG relative luminance of a #rgb or #rrggbb colour."""
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    def chan(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_page_link_is_readable_in_dark_mode():
    """The link is the second way to connect: the browser's default blue on
    a near-black page is too dim to read, so dark mode gives links a light
    colour (WCAG AA, 4.5:1, against the dark background)."""
    _, dark = css_rules(page_text())
    page_bg = background(dark["body"])
    m = re.search(r"(?:^|;|\s)color\s*:\s*(#[0-9a-fA-F]{3,6})", dark.get("a", ""))
    assert m, "dark mode leaves links the browser's default colour"
    assert contrast(m.group(1), page_bg) >= 4.5, (m.group(1), page_bg)


def test_page_scales_the_svg_rather_than_fixing_its_size():
    plain, _ = css_rules(page_text())
    body = plain[".qr svg"]
    assert re.search(r"width\s*:\s*100%", body), body
    assert re.search(r"height\s*:\s*auto", body), body


def test_page_says_what_to_do_when_the_handshake_waits():
    text = re.sub(r"<[^>]+>", "", page_text())
    assert "Establishing connection, please wait" in text
    assert "sessionphone bot status" in text
    assert "sessionphone link" in text
    assert "running" in text


def test_qr_page_carries_the_scalable_white_svg(root):
    page = account.qr_page("Test Agent", link=LINK)
    assert "{{" not in page
    svg = page[page.index("<svg"):page.index("</svg>") + len("</svg>")]
    assert "viewBox=" in root_tag(svg)
    assert svg_matrix(svg) == want_matrix(LINK)
    assert page.startswith(PAGE_FILE.read_text(encoding="utf-8").split("{{", 1)[0])

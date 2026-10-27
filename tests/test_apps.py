"""The webxdc apps sessionphone ships, and `sessionphone app` (PHONE-165 part 4).

The Guide Trail is package data under src/sessionphone/apps/guide-trail/,
built into a .xdc by `sessionphone app build`: a zip with index.html at its
root, no webxdc.js (the messenger provides it), the same bytes from the same
tree. These tests pin the build, the app's one rendering rule (C3: payload
strings reach the page through textContent only) and that nothing private or
dead (C4: the spike's trail.py readers) ships with it.

Fakes only: scratch roots and scratch app trees, no Delta Chat.
"""
from __future__ import annotations

import re
import shutil
import stat
import struct
import tomllib
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from sessionphone import apps
from sessionphone.cli import main

ROOT = Path(__file__).resolve().parent.parent
TRAIL = ROOT / "src" / "sessionphone" / "apps" / "guide-trail"
INDEX = TRAIL / "index.html"
# The Guide Trail recipe is in the page the skill's "What reaches you" links.
REACHES = ROOT / "docs" / "what-reaches-you.md"
README = ROOT / "README.md"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A scratch DC_ROOT; nothing under the real one is touched."""
    r = tmp_path / "root"
    monkeypatch.setenv("DC_ROOT", str(r))
    return r


def run(*args):
    return CliRunner().invoke(main, list(args))


def zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


# ---- what ships ------------------------------------------------------------

def test_the_guide_trail_ships_exactly_its_three_files():
    # C4: no trail.py (its heard-maildir and mouth/audio readers), no webxdc.js
    assert sorted(p.name for p in TRAIL.iterdir() if not p.name.startswith(".")) == [
        "icon.png", "index.html", "manifest.toml"]


def test_the_manifest_names_the_app():
    manifest = tomllib.loads((TRAIL / "manifest.toml").read_text())
    assert manifest["name"] == "Guide Trail"


def png_chunks(data: bytes) -> list[bytes]:
    out, i = [], 8
    while i < len(data):
        n, = struct.unpack(">I", data[i:i + 4])
        out.append(data[i + 4:i + 8])
        i += 12 + n
    return out


def test_the_icon_is_a_square_png_carrying_no_text():
    # the spike's own icon (64x64); a PNG text chunk could carry a path or a name
    data = (TRAIL / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    w, h = struct.unpack(">II", data[16:24])
    assert w == h and w >= 64
    assert not {b"tEXt", b"zTXt", b"iTXt", b"eXIf"} & set(png_chunks(data))


def test_the_app_still_loads_the_runtime_script_it_does_not_ship():
    html = INDEX.read_text()
    assert '<script src="webxdc.js"></script>' in html


# ---- C3: payload strings through textContent only ---------------------------

# Every DOM door that parses a string as markup.
MARKUP_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                "createContextualFragment", "srcdoc", "eval(", "new Function")


@pytest.mark.parametrize("sink", MARKUP_SINKS)
def test_c3_the_app_never_parses_a_string_as_markup(sink):
    assert sink not in INDEX.read_text(), f"{sink} in {INDEX.name}: render payload strings with textContent"


def test_c3_text_reaches_the_page_through_textcontent():
    script = INDEX.read_text().split("<script>", 1)[1]
    assert "textContent" in script
    # every assignment into the page is a textContent one, or a fixed className
    for target in re.findall(r"\.(\w+)\s*=(?!=)", script):
        assert target in {"textContent", "className"}, f"an assignment to .{target} in the app's script"


def test_the_app_draws_roles_not_people():
    html = INDEX.read_text()
    assert ".user{" in html and ".agent{" in html
    assert "GUIDE TRAIL" in html
    # the role decides the colour; the label is the payload's name, else the role
    assert "who === 'agent' ? 'agent' : 'user'" in html
    assert "toUpperCase()" in html


# ---- nothing private in the shipped tree ------------------------------------
#
# These pins name no person, host or path: a list of the words to keep out
# would put those very words into the tree. Instead they hold the app to what
# it is allowed to say (its classes, ids, string literals and visible text),
# and look for the SHAPES a private word comes in (an address, a dotted host
# name, a home path). release-scan, with its private list, is the real gate.

def _script() -> str:
    """The app's script with its comments stripped."""
    script = INDEX.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    return re.sub(r"(?m)^\s*//.*$", "", script)


def test_the_app_styles_roles_and_parts_never_people():
    css = INDEX.read_text().split("<style>", 1)[1].split("</style>", 1)[0]
    assert set(re.findall(r"(?:^|[}\s,])\.([A-Za-z][\w-]*)\s*\{", css)) == {"user", "agent", "who", "cur"}
    assert set(re.findall(r"#([\w-]+)\s*\{", css)) == {"t", "log", "st"}


# A JS string literal in any of its three quotes: 'x', "x" or `x` (a template
# may span lines). A pin that read only one quote would let a name in by another.
LITERAL = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\])*)\1""")


def _literals(script: str) -> set[str]:
    return {body for _quote, body in LITERAL.findall(script)}


def test_the_literal_reader_sees_every_quote():
    assert _literals("""a('x'); b("y"); c(`z ${n}`); d('it\\'s');""") == {"x", "y", "z ${n}", "it\\'s"}


def test_the_app_script_says_only_its_own_fixed_strings():
    # every string literal the script holds: element names, ids, class names,
    # its role words, its status words. A name or a host would be a new one.
    literals = _literals(_script())
    assert literals == {"", "agent", "user", "div", "span", "who", "cur", "log", "st", "object", "\u2588",
                        " ", "realtime ", "realtime joined", "no realtime: "}, sorted(literals)


def test_the_app_page_shows_only_its_title_and_status():
    html = INDEX.read_text()
    body = re.sub(r"(?s)<style>.*?</style>|<script>.*?</script>|<!--.*?-->", "", html)
    shown = {t.strip() for t in re.findall(r">([^<]+)<", body) if t.strip()}
    assert shown == {"Guide Trail", "GUIDE TRAIL", "starting"}


def test_the_manifest_holds_only_the_name():
    assert tomllib.loads((TRAIL / "manifest.toml").read_text()) == {"name": "Guide Trail"}


ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*")
DOTTED_HOST = re.compile(r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:net|com|org|io|dev|local|lan|internal)\b")
HOME_PATH = re.compile(r"~/|(?<![<\w.:])/[A-Za-z][\w.-]*/")
# C4: the spike's readers of the heard log, the mail tree and the mouth
DEAD_READER = re.compile(r"heard_|maildir|\.jsonl|mouth|\.wav\b")


@pytest.mark.parametrize("pattern", [ADDRESS, DOTTED_HOST, HOME_PATH, DEAD_READER], ids=lambda p: p.pattern[:24])
@pytest.mark.parametrize("name", ["index.html", "manifest.toml"])
def test_the_shipped_app_carries_no_address_host_home_path_or_dead_reader(name, pattern):
    text = (TRAIL / name).read_text()
    assert not pattern.findall(text), f"{name}: {pattern.findall(text)}"


@pytest.mark.parametrize("text", ["x@y.example", "see host-1.example.net", "~/.config", "/opt/app/x",
                                  "heard_2030-06-09.jsonl", "maildir"])
def test_the_shape_pins_would_catch_what_they_look_for(text):
    assert any(p.search(text) for p in (ADDRESS, DOTTED_HOST, HOME_PATH, DEAD_READER))


# ---- sessionphone app list ---------------------------------------------------

def test_app_list_names_the_guide_trail():
    for args in (["app"], ["app", "list"]):
        r = run(*args)
        assert r.exit_code == 0, r.output
        assert r.output.splitlines() == ["guide-trail"]


def test_names_skips_a_directory_with_no_index(tmp_path, monkeypatch):
    (tmp_path / "half").mkdir()
    (tmp_path / "whole").mkdir()
    (tmp_path / "whole" / "index.html").write_text("<p>")
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path)
    assert apps.names() == ["whole"]


# ---- sessionphone app build --------------------------------------------------

def test_build_writes_the_xdc_under_the_root_and_prints_its_path(root):
    r = run("app", "build", "guide-trail")
    assert r.exit_code == 0, r.output
    out = root / "apps" / "guide-trail.xdc"
    assert r.output == f"{out}\n"
    assert zip_names(out) == ["icon.png", "index.html", "manifest.toml"]


def test_build_makes_a_missing_root_private(root):
    assert not root.exists()
    assert run("app", "build", "guide-trail").exit_code == 0
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_build_honours_dash_o(root, tmp_path):
    out = tmp_path / "elsewhere" / "trail.xdc"
    r = run("app", "build", "guide-trail", "-o", str(out))
    assert r.exit_code == 0, r.output
    assert r.output == f"{out}\n"
    assert out.is_file()
    assert not root.exists()     # -o writes nothing under the root


def test_build_has_index_html_and_the_manifest_at_the_zip_root(tmp_path):
    out = apps.build("guide-trail", tmp_path / "a.xdc")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "index.html" in names and "manifest.toml" in names
        assert all("/" not in n for n in names)
        assert zf.read("index.html") == INDEX.read_bytes()
        assert zf.testzip() is None


def test_build_is_byte_identical_across_builds(tmp_path):
    a = apps.build("guide-trail", tmp_path / "a.xdc")
    b = apps.build("guide-trail", tmp_path / "b.xdc")
    assert a.read_bytes() == b.read_bytes()


def test_build_does_not_depend_on_file_times(tmp_path, monkeypatch):
    tree = tmp_path / "apps"
    shutil.copytree(TRAIL, tree / "guide-trail")
    monkeypatch.setattr(apps, "APPS_DIR", tree)
    first = apps.build("guide-trail", tmp_path / "1.xdc").read_bytes()
    import os
    for p in (tree / "guide-trail").iterdir():
        os.utime(p, (1_907_238_896, 1_907_238_896))
        p.chmod(0o600)
    assert apps.build("guide-trail", tmp_path / "2.xdc").read_bytes() == first


def test_build_never_ships_webxdc_js_or_hidden_files(tmp_path, monkeypatch):
    tree = tmp_path / "apps"
    app = tree / "demo"
    (app / "img").mkdir(parents=True)
    (app / "index.html").write_text("<p>")
    (app / "webxdc.js").write_text("// the runtime's")
    (app / ".DS_Store").write_bytes(b"\0")
    (app / "img" / "a.png").write_bytes(b"png")
    (app / "__pycache__").mkdir()
    (app / "__pycache__" / "x.pyc").write_bytes(b"\0")
    monkeypatch.setattr(apps, "APPS_DIR", tree)
    names = zip_names(apps.build("demo", tmp_path / "demo.xdc"))
    assert names == ["img/a.png", "index.html"]


def test_build_entries_are_plain_files_with_one_mode_and_time(tmp_path):
    with zipfile.ZipFile(apps.build("guide-trail", tmp_path / "a.xdc")) as zf:
        for info in zf.infolist():
            assert info.date_time == apps.EPOCH
            assert stat.S_IMODE(info.external_attr >> 16) == 0o644
            assert stat.S_ISREG(info.external_attr >> 16)


def test_build_replaces_an_old_file_and_leaves_no_temp(tmp_path):
    out = tmp_path / "a.xdc"
    out.write_bytes(b"stale")
    apps.build("guide-trail", out)
    assert out.read_bytes()[:2] == b"PK"
    assert [p.name for p in tmp_path.iterdir()] == ["a.xdc"]


def test_build_of_an_unknown_app_exits_2_naming_the_apps(root):
    r = run("app", "build", "nope")
    assert r.exit_code == 2
    assert "no app 'nope'" in r.output and "guide-trail" in r.output
    assert not root.exists()


def test_a_failed_write_leaves_no_temp_and_exits_1(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(apps.zipfile.ZipFile, "writestr", boom)
    r = run("app", "build", "guide-trail", "-o", str(tmp_path / "a.xdc"))
    assert r.exit_code == 1
    assert "disk full" in r.output
    assert list(tmp_path.iterdir()) == []


def test_app_help_names_the_send_recipe():
    r = run("app", "build", "--help")
    assert r.exit_code == 0
    assert "dc-send-file" in r.output


# ---- the docs ----------------------------------------------------------------

def test_skill_has_the_send_then_feed_recipe():
    text = REACHES.read_text()
    assert "sessionphone app build guide-trail" in text
    send = text.index("dc-send-file")
    assert "dc-xdc-realtime --feed" in text[send:]


def test_readme_documents_the_app_and_its_icon():
    text = README.read_text()
    assert "sessionphone app build guide-trail" in text
    assert "sessionphone app list" in text
    assert "icon.png" in text

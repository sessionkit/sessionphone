"""The configuration docs keep up with the code.

docs/settings.md lists every setting the package reads, so a new DC_* in
src/ that the page does not name fails here. docs/configuration.md and
docs/settings.md link each other and the README relatively, other repos by a
full URL, and their own headings by anchor. The README
carries the template's `## Configuration` and `## Logs`, and the log files
and the Health command they name are the ones the code uses.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from sessionphone import cli, paths, service

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sessionphone"
BIN = ROOT / "bin"
DOCS = ROOT / "docs"

# A setting read through dc_env: env("X"), path("X", ...), number("X", ...)
# and _get(e, "X") all name DC_X (PIP_CHAT_X is its deprecated alias).
VIA_DC_ENV = re.compile(r"\b(?:env|path|number|_get)\((?:e, )?\"([A-Z][A-Z0-9_]*)\"")
# A setting read by its full name: os.environ.get("DC_X"), e.get("EARS_X"), ...
LITERAL = re.compile(r"\"((?:DC|EARS|ECHO|SESSIONMAIL|SESSIONPHONE)_[A-Z0-9_]+)\"")
# Names in src/ that are not settings a user sets.
NOT_SETTINGS = {
    "DC_CALLERS",                     # read only to say it is IGNORED; the page says so
    "SESSIONMAIL_FROM_NAME",          # set FOR the `message send` child, never read
    "SESSIONMAIL_REQUIRE_SIGNATURE",  # the same
    "SESSIONMAIL_MHS_ORACLE",         # stripped from a verify child, never honoured
}
# A setting a dc-* tool reads with a default: ${DC_X:-...} or os.environ.get("DC_X")
BIN_READ = re.compile(r"\$\{((?:DC|SESSIONPHONE)_[A-Z0-9_]+):-|environ\.get\(\"((?:DC|SESSIONPHONE)_[A-Z0-9_]+)\"")
BIN_NOT_SETTINGS = {
    "DC_VOICE_MESSAGE",               # dc-send-file sets it for its own child, from --voice-message
}


def _src_settings() -> set[str]:
    names: set[str] = set()
    for f in SRC.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        names |= {f"DC_{n}" for n in VIA_DC_ENV.findall(text)}
        names |= set(LITERAL.findall(VIA_DC_ENV.sub("", text)))   # env("EARS_SRC") is DC_EARS_SRC
    return names - NOT_SETTINGS


def _bin_settings() -> set[str]:
    names: set[str] = set()
    for f in BIN.iterdir():
        if f.is_file():
            names |= {a or b for a, b in BIN_READ.findall(f.read_text(encoding="utf-8"))}
    return names - BIN_NOT_SETTINGS


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation out, spaces to -."""
    return re.sub(r"[^a-z0-9 _-]", "", heading.strip().lower()).replace(" ", "-")


def _section(text: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, f"no `## {title}` section"
    return m.group(1)


def test_the_scan_finds_settings():
    found = _src_settings()
    # a proven non-zero: the scan sees names read each way
    assert {"DC_AGENT", "DC_STT_URL", "DC_BARGE_IN", "EARS_RECORD", "SESSIONPHONE_ROOT"} <= found


def test_settings_page_names_every_setting_src_reads():
    page = _read("settings.md")
    missing = sorted(n for n in _src_settings() if f"`{n}`" not in page)
    assert missing == [], f"docs/settings.md does not name: {missing}"


def test_settings_page_names_every_setting_the_tools_read():
    found = _bin_settings()
    assert {"DC_TAKEN_WAIT", "DC_WEB_OUT", "DC_XDC_SUMMARY"} <= found   # the scan sees them
    page = _read("settings.md")
    missing = sorted(n for n in found if f"`{n}`" not in page)
    assert missing == [], f"docs/settings.md does not name: {missing}"


def test_settings_page_says_where_bot_install_remembers_its_settings():
    page = _read("settings.md")
    head = " ".join(page.split("\n## ")[0].split())     # the point, above the first section
    assert "reads its `DC_*` settings from its environment" in head
    assert "does not read the kit's settings cascade" not in head
    assert f"`{paths.kit_settings('phone', 'phone.json')}`" in head
    assert "## What bot install remembers" in page
    label = re.search(r"\[the kit's settings guide\]\[([^\]]+)\]", head)
    assert label, "the lead does not link the kit's settings guide"
    assert "`docs/settings.md` in the sessionkit repo" in head, "offline, the lead names no path"
    assert re.search(rf"^\[{re.escape(label.group(1))}\]: "
                     r"https://github\.com/sessionkit/sessionkit/blob/HEAD/docs/settings\.md$",
                     page, re.MULTILINE)


def test_bot_install_carries_only_what_the_docs_say(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path))
    for k in service.MAIL_KEYS:                       # this shell's own, out of the way
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DC_VOICE", "af_test")
    monkeypatch.setenv("EARS_RECORD", "1")
    monkeypatch.setenv("SESSIONMAIL_HOST", "mail.example")
    monkeypatch.setenv("SESSIONMAIL_SENDMAIL", "drop")
    monkeypatch.setenv("SESSIONMAIL_FROM_NAME", "the installing seat")
    monkeypatch.setenv("SESSION_MAIL_HOST", "old.example")
    monkeypatch.setattr(service, "ears_src", lambda: None)
    env = service.job("tester", python="/bin/true")["EnvironmentVariables"]
    assert env["DC_VOICE"] == "af_test"
    assert env["SESSIONMAIL_HOST"] == "mail.example" and env["SESSIONMAIL_SENDMAIL"] == "drop"
    for k in ("EARS_RECORD", "SESSIONMAIL_FROM_NAME", "SESSION_MAIL_HOST"):
        assert k not in env
    assert set(env) - {k for k in env if k.startswith("DC_")} == {
        "PATH", "SESSIONPHONE_ROOT", "PYTHONUNBUFFERED", "SESSIONMAIL_HOST", "SESSIONMAIL_SENDMAIL"}
    conf = " ".join(_read("configuration.md").split())
    assert "Nothing else in your shell reaches the bot." in conf
    assert "`EARS_*` is not carried" in conf
    bot = _section(_read("configuration.md"), "The bot")
    route = _section(_read("settings.md"), "Mail route")
    for k in service.MAIL_KEYS:                       # every carried key, on both pages
        assert f"`{k}`" in bot and f"`{k}`" in route, k


@pytest.mark.parametrize("name", ["configuration.md", "settings.md"])
def test_docs_links_are_absolute_relative_or_anchors_that_resolve(name):
    """A relative link is checked where it lands by test_public_links.py."""
    text = _read(name)
    targets = re.findall(r"^\[[^\]]+\]:\s*(\S+)", text, re.MULTILINE)
    targets += re.findall(r"\]\(\s*<?([^)\s>]+)", text)
    assert targets, "no links found: the scan is broken"
    slugs = {_slug(h) for h in re.findall(r"^#+ (.+)$", text, re.MULTILINE)}
    relative = [t for t in targets if not re.match(r"#|[a-z][a-z0-9+.-]*:", t)]
    assert relative, "no relative links found: the scan is broken"
    bad = [t for t in targets
           if not (t.startswith("https://") or t in relative or t[1:] in slugs and t.startswith("#"))]
    assert bad == [], f"{name}: links that are not absolute, relative or a heading here: {bad}"


def test_configuration_settings_section_links_the_settings_page():
    assert "\n[settings]: settings.md\n" in _read("configuration.md")
    assert "](#settings)" not in _section(_read("configuration.md"), "Settings")


def test_readme_configuration_links_both_pages_relatively():
    sec = _section((ROOT / "README.md").read_text(encoding="utf-8"), "Configuration")
    assert "](docs/configuration.md)" in sec
    assert "](docs/settings.md)" in sec


def test_readme_logs_name_the_files_the_code_writes(monkeypatch, tmp_path):
    sec = _section((ROOT / "README.md").read_text(encoding="utf-8"), "Logs")
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path))
    monkeypatch.setattr(service, "ears_src", lambda: None)
    j = service.job("tester", python="/bin/true")
    assert Path(j["StandardOutPath"]).name in sec          # bot.launchd.log
    assert Path(j["StandardErrorPath"]) == Path(j["StandardOutPath"])
    assert "bot.log" in sec and "transcript.md" in sec
    # the folder both logs are written in, as the code spells it, per bot and per profile
    folder = paths.kit_dir("phone", "log")
    assert Path(j["StandardOutPath"]) == Path(folder).expanduser() / "tester" / "bot.launchd.log"
    assert f"`{folder}/<agent>/bot.log`" in sec and f"`{folder}/<agent>/bot.launchd.log`" in sec
    assert f"`{folder}/<agent>/<profile>/`" in sec
    assert "~/.sessionphone/bot.log" not in sec and "~/.sessionphone/bot.launchd.log" not in sec


def test_readme_health_is_a_real_command():
    sec = _section((ROOT / "README.md").read_text(encoding="utf-8"), "Logs")
    m = re.search(r"\*\*Health:\*\* `sessionphone ([a-z ]+)`", sec)
    assert m, "no `**Health:** `sessionphone ...`` line"
    r = CliRunner().invoke(cli.main, [*m.group(1).split(), "--help"])
    assert r.exit_code == 0, r.output
    assert "Exit 0 running, 1 not" in r.output

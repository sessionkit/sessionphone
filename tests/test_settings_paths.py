"""The settings paths the code reads or names are each held in one constant,
and every reader, test and doc goes through it.

- ``recording.conf_path()``: ``$EARS_CONF`` when set and not empty, else
  ``recording.EARS_CONF_DEFAULT`` (``~`` expanded). There is no other variable
  and no second, fallback path.
- ``calls.REGISTRY_OFF``, the mail leg's OFF banner, names ``calls.MAIL_CONF``.
- Both are built by ``paths.kit_settings(package, name)``, the one place the
  kit's settings layout is spelled: ears.json is the voice package's,
  mail.json the mail package's. No folder carries a "session" prefix.
- The shipped tree names a superseded settings root on EXACTLY the allowed
  lines (:data:`ALLOWED`), each by file:line: ``recording.LEGACY_EARS_CONF``,
  the one HOME=tmp test of the stranded-file warning, this file's own
  pattern, and one "Upgrading from 0.1.0" note in the docs. Any other hit
  fails, and so does an allowed line that no longer hits.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from sessionphone import paths
from sessionphone.bridge import calls, recording

# what `bot install` remembers: settings.FILE as shipped (conftest moves it per test)
PHONE_CONF = paths.kit_settings("phone", "phone.json")


def test_ears_conf_wins_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("EARS_CONF", str(tmp_path / "x.json"))
    assert recording.conf_path() == str(tmp_path / "x.json")


def test_unset_or_empty_ears_conf_reads_the_one_default(monkeypatch):
    want = os.path.expanduser(recording.EARS_CONF_DEFAULT)
    monkeypatch.delenv("EARS_CONF", raising=False)
    assert recording.conf_path() == want
    monkeypatch.setenv("EARS_CONF", "")
    assert recording.conf_path() == want


ROOT = Path(__file__).resolve().parents[1]


def test_the_defaults_are_built_by_the_one_layout():
    assert recording.EARS_CONF_DEFAULT == paths.kit_settings("voice", "ears.json")
    assert calls.MAIL_CONF == paths.kit_settings("mail", "mail.json")


def test_the_layout_is_package_first_under_the_kit():
    """``~/.sessionkit/<package>/settings/<file>``: package folders carry no
    "session" prefix, and the shared files agree with their owners'."""
    assert paths.kit_settings("voice", "ears.json") == "~/.sessionkit/voice/settings/ears.json"


# A superseded settings root: the old home-directory folder, or the
# kind-first layout under the kit. Spelled plainly, so this line is a hit too.
SUPERSEDED = re.compile(r"\.settings|sessionkit/settings/")

# Every line of the shipped tree that may hit SUPERSEDED, by file:line. The
# gate fails on any other hit, and on any of these that no longer hits.
ALLOWED = {
    "src/sessionphone/bridge/recording.py:145",  # LEGACY_EARS_CONF: stat-ed for the warning only
    "tests/test_stranded_ears_conf.py:23",       # OLD: the HOME=tmp test of the warning
    "tests/test_settings_paths.py:61",           # SUPERSEDED: this gate's own pattern
    "docs/configuration.md:230",                 # the "Upgrading from 0.1.0" note
}

# Never shipped, or not ours: version control, caches, virtualenvs, builds.
_NOT_SHIPPED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
                ".mypy_cache", "node_modules", "dist", "build"}


def _git(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], input=stdin, capture_output=True,
                          check=True, text=True).stdout


def _in_checkout() -> bool:
    if shutil.which("git") is None:
        return False
    try:
        return Path(_git("rev-parse", "--show-toplevel").strip()).resolve() == ROOT.resolve()
    except subprocess.CalledProcessError:
        return False


def shipped_files() -> list[str]:
    """The files a release archive carries, relative to ROOT. In a git checkout:
    tracked and new (not ignored) files, less those marked export-ignore, as
    they are on disk now. Outside one (an export, an sdist): every file under
    ROOT but caches, virtualenvs and builds."""
    if _in_checkout():
        names = [n for n in _git("ls-files", "-z", "--cached", "--others",
                                 "--exclude-standard").split("\0") if n]
        attrs = _git("check-attr", "-z", "--stdin", "export-ignore",
                     stdin="\0".join(names) + "\0").split("\0")
        ignored = {attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "set"}
        return sorted(n for n in set(names) - ignored if (ROOT / n).is_file())
    out = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if p.is_file() and not (set(rel.parts) & _NOT_SHIPPED) \
                and not any(part.endswith(".egg-info") for part in rel.parts):
            out.append(rel.as_posix())
    return sorted(out)


def superseded_hits() -> set[str]:
    hits = set()
    for name in shipped_files():
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits |= {f"{name}:{n}" for n, line in enumerate(text.splitlines(), 1)
                 if SUPERSEDED.search(line)}
    return hits


def test_the_shipped_tree_names_a_superseded_root_only_on_the_allowed_lines():
    hits = superseded_hits()
    assert sorted(hits - ALLOWED) == [], "a superseded settings root outside the allowed lines"
    assert sorted(ALLOWED - hits) == [], "an allowed line no longer names it: update ALLOWED"


def test_the_docs_name_the_old_path_once_in_the_upgrading_note():
    """README, docs, skills and the CHANGELOG: one hit, under its heading."""
    docs = {h for h in superseded_hits()
            if h.startswith(("README.md:", "docs/", "skills/", "CHANGELOG.md:"))}
    assert len(docs) == 1, sorted(docs)
    name, n = docs.pop().rsplit(":", 1)
    above = (ROOT / name).read_text(encoding="utf-8").splitlines()[: int(n)]
    heading = next(line for line in reversed(above) if line.startswith("#"))
    assert heading == "### Upgrading from 0.1.0"


def test_the_off_banner_names_the_mail_settings_file():
    assert f'"registry" in {calls.MAIL_CONF}' in calls.REGISTRY_OFF


KIT_PATH = re.compile(r"~/\.sessionkit/[^\s`'\")]+")


def test_every_kit_path_in_the_docs_is_one_the_code_builds():
    """Docs cannot import, so each names a kit path in exactly the form the
    code builds: a layout change in the code fails here until the docs follow."""
    built = {recording.EARS_CONF_DEFAULT, calls.MAIL_CONF, PHONE_CONF}
    docs = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    seen, odd = set(), []
    for p in docs:
        for n, line in enumerate(p.read_text().splitlines(), 1):
            for m in KIT_PATH.findall(line):
                seen.add(m)
                if m not in built:
                    odd.append(f"{p.relative_to(ROOT)}:{n}: {m}")
    assert odd == []
    assert seen == built

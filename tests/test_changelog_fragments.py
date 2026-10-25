"""The live changelog.d/: every pending fragment is one the release can collate.

A change writes its entry to changelog.d/<TASK-ID>-<slug>.md, and
scripts/release.py moves it into CHANGELOG.md at the release. These checks
read this repo's own fragments, so a bad one goes red on the branch that adds
it, not at the release:

- every fragment parses as the release parses it (its own parse_fragment);
- no fragment holds a relative link. A link resolves from the file that holds
  it, so one written for changelog.d/ breaks once the entry sits in
  CHANGELOG.md, and one written for CHANGELOG.md is broken here. "Relative"
  is the link gate's own is_relative, so the two cannot drift;
- every file git tracks under changelog.d/, at any depth, is
  changelog.d/README.md or a fragment changelog.d/<TASK-ID>-<slug>.md. It
  compares paths, so a nested README.md is a stray too, and a file the
  release would not read is caught here, not left behind at the release;
- no two fragments share a task id, the ids compared as numbers. One task
  writes one fragment, and edits it rather than adding a second.

Each check is a function over its input, run on the live tree and on a
planted bad case, so each is shown to go red.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import _changelog
import pytest
import test_public_links as public_links  # its is_relative: the link gate's own rule
import test_pypi_readme as pypi_readme  # its link_targets: the prose's links, code left out

ROOT = _changelog.ROOT
README = "changelog.d/README.md"
# changelog.d/<TASK-ID>-<slug>.md: an upper-case tracker prefix, a number,
# then a lower-case slug of words joined by single hyphens.
FRAGMENT = re.compile(r"changelog\.d/(?P<id>[A-Z][A-Z0-9]*-\d+)-[a-z0-9]+(?:-[a-z0-9]+)*\.md")


def parse_faults(paths: list[Path]) -> list[str]:
    """What the release's own parse_fragment refuses, one line per fragment."""
    faults = []
    for path in paths:
        try:
            sections = _changelog.release().parse_fragment(path)
        except SystemExit as e:
            faults.append(str(e.code))
            continue
        if not sections:  # parse_fragment refuses this itself; kept as a floor
            faults.append(f"{path}: no sections")
    return faults


def relative_links(paths: list[Path]) -> list[str]:
    """`name: target` for each relative link in the prose of each fragment."""
    return [f"{path.name}: {target}"
            for path in paths
            for target in pypi_readme.link_targets(path.read_text(encoding="utf-8"))
            if public_links.is_relative(target)]


def name_faults(tracked: list[str]) -> list[str]:
    """Each tracked path under changelog.d/ that is neither its README.md nor
    a fragment the release collects."""
    return [p for p in tracked if p != README and not FRAGMENT.fullmatch(p)]


def task_id(path: str) -> tuple[str, int] | None:
    """A fragment's task id as (prefix, number), so PHONE-07 is PHONE-7."""
    m = FRAGMENT.fullmatch(path)
    if not m:
        return None
    prefix, _, number = m.group("id").rpartition("-")
    return prefix, int(number)


def shared_ids(paths: list[str]) -> list[str]:
    """The fragments that share a task id, grouped, in path order."""
    seen: dict[tuple[str, int], list[str]] = {}
    for p in sorted(paths):
        key = task_id(p)
        if key is not None:
            seen.setdefault(key, []).append(p)
    return [", ".join(group) for group in seen.values() if len(group) > 1]


def _repo(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _plant(tmp_path: Path, name: str, text: str) -> Path:
    d = tmp_path / "changelog.d"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


GOOD = "### A headline\n\n- An entry, naming `README.md` in a code span.\n"


def in_checkout() -> bool:
    if shutil.which("git") is None:
        return False
    out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=False)
    return out.returncode == 0 and Path(out.stdout.strip()).resolve() == ROOT.resolve()


needs_checkout = pytest.mark.skipif(
    not in_checkout(), reason="not a git checkout (an sdist?): no tracked files to read")


def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "changelog.d"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


# ---- the live tree ----------------------------------------------------------------


def test_the_checks_read_the_live_fragments():
    """A proven non-zero beside the zeros below, while an entry is pending.

    The checks read this repo's own changelog.d/, where its README.md is.
    Right after a release that directory holds no fragment, and that is its
    right state, so this never fails for an empty changelog.d/. It skips,
    and says so: a check with nothing to read never passes quietly."""
    assert (_changelog.FRAGMENTS / "README.md").is_file(), "the checks read the live changelog.d/"
    if not _changelog.fragments():
        pytest.skip("no pending fragment: changelog.d/ holds only its README.md, as right after a release")


def test_every_live_fragment_parses_as_the_release_parses_it():
    assert parse_faults(_changelog.fragments()) == []


def test_no_live_fragment_holds_a_relative_link():
    assert relative_links(_changelog.fragments()) == []


@needs_checkout
def test_every_tracked_path_under_changelog_d_is_its_readme_or_a_fragment():
    paths = tracked()
    assert README in paths, "changelog.d/README.md is tracked"
    assert name_faults(paths) == []


@needs_checkout
def test_git_and_the_collector_agree_on_the_fragments():
    """Every fragment git tracks is one the release collects: the name rule
    and release.fragment_paths say the same."""
    named = {p for p in tracked() if p != README}
    assert named - {_repo(p) for p in _changelog.fragments()} == set()


def test_no_two_live_fragments_share_a_task_id():
    assert shared_ids([_repo(p) for p in _changelog.fragments()]) == []


# ---- each check goes red on a planted bad case ---------------------------------------


@pytest.mark.parametrize("text, says", [
    ("Loose text first.\n\n### A headline\n\n- an entry\n", "text outside a ### section"),
    ("## [9.9.9] - 2000-01-01\n\n- an entry\n", "a fragment holds only"),
    ("#### Too deep\n\n- an entry\n", "a fragment holds only"),
    ("### A headline\n", "no entries"),
    ("", "no entries"),
])
def test_the_parse_check_refuses_a_planted_bad_fragment(tmp_path, text, says):
    good = _plant(tmp_path, "PHONE-1-good.md", GOOD)
    bad = _plant(tmp_path, "PHONE-2-bad.md", text)
    assert parse_faults([good]) == []
    faults = parse_faults([good, bad])
    assert len(faults) == 1 and says in faults[0] and "PHONE-2-bad.md" in faults[0], faults


@pytest.mark.parametrize("link", [
    "[the README](README.md)",
    "[the README](../README.md)",
    "[a section](../README.md#status)",
    "[the how-to](./README.md)",
    "<a href=\"../CHANGELOG.md\">the changelog</a>",
    "[setup]\n\n[setup]: ../docs/setup.md",
])
def test_the_link_check_refuses_a_planted_relative_link(tmp_path, link):
    bad = _plant(tmp_path, "PHONE-3-link.md", f"### A headline\n\n- See {link}.\n")
    assert relative_links([bad]), link


@pytest.mark.parametrize("text", [
    "- See [a page](https://example.com/page).",
    "- See [the top](#a-headline).",
    "- Name the file in a code span: `[x](README.md)`.",
    "- Mail [us](mailto:someone@example.com).",
])
def test_the_link_check_passes_what_is_not_a_relative_link(tmp_path, text):
    ok = _plant(tmp_path, "PHONE-4-ok.md", f"### A headline\n\n{text}\n")
    assert relative_links([ok]) == []


@pytest.mark.parametrize("path", [
    "changelog.d/PHONE-9-x.markdown",
    "changelog.d/PHONE-9-x.txt",
    "changelog.d/sub/PHONE-9-x.md",
    "changelog.d/sub/README.md",
    "changelog.d/readme.md",
    "changelog.d/notes.md",
    "changelog.d/PHONE-9.md",
    "changelog.d/phone-9-x.md",
    "changelog.d/PHONE-9-Some-Slug.md",
    "changelog.d/PHONE-9-x--y.md",
    "changelog.d/PHONE-9-x-.md",
    "changelog.d/.DS_Store",
])
def test_the_name_check_refuses_a_planted_stray(path):
    assert name_faults([README, "changelog.d/PHONE-1-good.md", path]) == [path]


@pytest.mark.parametrize("path", [
    "changelog.d/PHONE-9-x.md",
    "changelog.d/PHONE-239-changelog-fragments.md",
    "changelog.d/SK-1000-a-b-c.md",
    "changelog.d/PHONE-12-fix-2fa.md",
])
def test_the_name_check_passes_a_fragment(path):
    assert name_faults([README, path]) == []


def test_every_name_the_check_passes_is_one_the_release_collects(tmp_path):
    for name in ("PHONE-9-x.md", "SK-1000-a-b-c.md"):
        _plant(tmp_path, name, GOOD)
    collected = [p.name for p in _changelog.fragments(tmp_path / "changelog.d")]
    assert sorted(collected) == ["PHONE-9-x.md", "SK-1000-a-b-c.md"]
    assert name_faults([f"changelog.d/{n}" for n in collected]) == []


@pytest.mark.parametrize("paths, shared", [
    (["changelog.d/PHONE-9-x.md", "changelog.d/PHONE-9-y.md"],
     ["changelog.d/PHONE-9-x.md, changelog.d/PHONE-9-y.md"]),
    (["changelog.d/PHONE-9-x.md", "changelog.d/PHONE-09-y.md"],
     ["changelog.d/PHONE-09-y.md, changelog.d/PHONE-9-x.md"]),
])
def test_the_id_check_refuses_two_planted_fragments_for_one_task(paths, shared):
    assert shared_ids(["changelog.d/PHONE-1-good.md", *paths]) == shared


def test_the_id_check_passes_distinct_ids():
    """PHONE-9 and PHONE-90 are different tasks; so are PHONE-9 and SK-9."""
    assert shared_ids(["changelog.d/PHONE-9-x.md", "changelog.d/PHONE-90-x.md",
                       "changelog.d/SK-9-x.md"]) == []


def test_the_live_checks_hold_right_after_a_release(tmp_path, monkeypatch):
    """A release collates every fragment and leaves changelog.d/ holding only
    its README.md. No live check may go red on that tree: the release commit
    runs the same suite."""
    d = tmp_path / "changelog.d"
    d.mkdir()
    (d / "README.md").write_text("# changelog.d\n", encoding="utf-8")
    real = _changelog.fragments
    monkeypatch.setattr(_changelog, "FRAGMENTS", d)
    monkeypatch.setattr(_changelog, "fragments", lambda directory=d: real(directory))
    assert _changelog.fragments() == []
    with pytest.raises(pytest.skip.Exception, match="no pending fragment"):
        test_the_checks_read_the_live_fragments()
    # The live tests themselves, reading the README-only directory.
    test_every_live_fragment_parses_as_the_release_parses_it()
    test_no_live_fragment_holds_a_relative_link()
    test_no_two_live_fragments_share_a_task_id()
    # The tracked-path checks read git, not the directory: their functions on
    # what git would list there.
    assert parse_faults([]) == []
    assert relative_links([]) == []
    assert name_faults([README]) == []
    assert shared_ids([]) == []


# ---- entry(): one change's own entry, pending or released ---------------------------

PLANTED_CHANGELOG = """# Changelog

## [Unreleased]

## [9.9.9] - 2030-12-08

### Before it

- before

### The planted headline

- the planted entry
- its second line

### After it

- after

## [9.9.8] - 2030-06-09

### Older

- older
"""


def test_entry_reads_the_fragment_while_it_is_pending(tmp_path):
    d = tmp_path / "changelog.d"
    d.mkdir()
    (d / "PHONE-9-x.md").write_text("### The planted headline\n\n- the fragment's own text\n", encoding="utf-8")
    log = tmp_path / "CHANGELOG.md"
    log.write_text(PLANTED_CHANGELOG, encoding="utf-8")
    text = _changelog.entry("PHONE-9-x.md", "The planted headline", d, log)
    assert text == "### The planted headline\n\n- the fragment's own text\n"


def test_entry_reads_the_released_section_once_the_fragment_is_gone(tmp_path):
    d = tmp_path / "changelog.d"
    d.mkdir()
    log = tmp_path / "CHANGELOG.md"
    log.write_text(PLANTED_CHANGELOG, encoding="utf-8")
    text = _changelog.entry("PHONE-9-x.md", "The planted headline", d, log)
    assert text == "### The planted headline\n\n- the planted entry\n- its second line\n\n"
    assert "before" not in text and "after" not in text and "older" not in text


def test_entry_takes_the_last_section_of_the_file_to_its_end(tmp_path):
    log = tmp_path / "CHANGELOG.md"
    log.write_text(PLANTED_CHANGELOG, encoding="utf-8")
    assert _changelog.entry("PHONE-9-x.md", "Older", tmp_path, log) == "### Older\n\n- older\n"


def test_entry_refuses_when_neither_holds_it(tmp_path):
    log = tmp_path / "CHANGELOG.md"
    log.write_text(PLANTED_CHANGELOG, encoding="utf-8")
    with pytest.raises(LookupError, match="PHONE-9-x.md"):
        _changelog.entry("PHONE-9-x.md", "No such headline", tmp_path, log)
    with pytest.raises(LookupError):  # a headline is matched whole, never as a prefix
        _changelog.entry("PHONE-9-x.md", "The planted", tmp_path, log)

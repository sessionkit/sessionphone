"""PHONE-168, PHONE-172, PHONE-336: the release archive leaves CLAUDE.md,
design/ and openspec/ out.

CLAUDE.md is agent context for this checkout, not for a plugin user, and the
repo root is the plugin root. design/ holds the maintainers' internal notes.
openspec/ holds config.yaml, a pointer to the project's private OpenSpec store
(the kit's rule, SK-223, copied). `.gitattributes` marks all three export-ignore, so `git archive` (and the GitHub
tarball) drops them while they stay in the tree. A shipped doc must then not
point into design/: that guard reads files only, so it runs in an archive too.

`git check-attr` reads the working-tree `.gitattributes`, so an edit that drops
the rule fails before it is committed. Outside a git checkout (an sdist, say)
the checks skip, with a reason.

A git clone of the export is a checkout too, but the export already dropped
CLAUDE.md, design/ and openspec/. So a test about an export-ignored input keys on that
input being in the tree, never on .git alone: absent, it skips with a reason.
In the private checkout all are present, so every test here runs.
"""
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          check=True, text=True).stdout


def in_checkout():
    if shutil.which("git") is None:
        return False
    try:
        top = git("rev-parse", "--show-toplevel").strip()
    except subprocess.CalledProcessError:
        return False
    return Path(top).resolve() == ROOT.resolve()


needs_checkout = pytest.mark.skipif(
    not in_checkout(), reason="not a git checkout (an sdist?): no export rules to read")


def needs_present(path):
    """Skip unless the export-ignored `path` is in this tree. An export (and a
    clone of it) leaves it out, which is the rule working, not a failure."""
    return pytest.mark.skipif(
        not (ROOT / path).exists(),
        reason=f"{path} is not in this tree: an export dropped it (export-ignore)")


def export_ignore(path):
    """The export-ignore value git reports for `path`: set, unset or unspecified."""
    return git("check-attr", "export-ignore", "--", path).rsplit(": ", 1)[1].strip()


@needs_checkout
@needs_present("CLAUDE.md")
def test_claude_md_is_export_ignored():
    assert (ROOT / "CLAUDE.md").is_file(), "CLAUDE.md stays in the tree"
    assert export_ignore("CLAUDE.md") == "set"


@needs_checkout
@pytest.mark.parametrize("path", ["README.md", "AGENTS.md", "CHANGELOG.md", "LICENSE",
                                  ".claude-plugin/plugin.json",
                                  "changelog.d/README.md",
                                  "skills/sessionphone/SKILL.md",
                                  "skills/sessionphone/CLAUDE.md",
                                  "skills/sessionphone/design/notes.md",
                                  "src/design/notes.md",
                                  "skills/sessionphone/openspec/config.yaml"])
def test_the_rule_is_anchored_to_the_root(path):
    """Only the root CLAUDE.md, design/ and openspec/ are dropped: the rest
    still ships, and a nested CLAUDE.md, design/ or openspec/ would not be
    caught by the anchored `/CLAUDE.md`, `/design/` and `/openspec` lines."""
    assert export_ignore(path) == "unspecified"


def design_files():
    return git("ls-files", "-z", "--", "design").split("\0")[:-1]


@needs_checkout
@needs_present("design")
def test_design_notes_are_export_ignored():
    """design/ stays in the tree, and the directory and every tracked file in
    it are export-ignore `set`: `/design/` drops the tree from the archive,
    and `/design/**` marks each file, which is what check-attr reports."""
    files = design_files()
    assert files, "design/ stays in the tree"
    assert export_ignore("design/") == "set"
    for path in files:
        assert export_ignore(path) == "set", path


def openspec_files():
    return git("ls-files", "-z", "--", "openspec").split("\0")[:-1]


@needs_checkout
@needs_present("openspec")
def test_openspec_pointer_is_export_ignored():
    """openspec/ stays in the tree, and the directory and every tracked file
    in it (config.yaml, the store pointer) are export-ignore `set`: `/openspec`
    drops the directory entry, and `/openspec/**` marks each file."""
    files = openspec_files()
    assert "openspec/config.yaml" in files, "openspec/config.yaml stays in the tree"
    assert export_ignore("openspec") == "set"
    for path in files:
        assert export_ignore(path) == "set", path


def archive_names():
    """The entries `git archive` writes for HEAD under the working-tree rules,
    so an edit to `.gitattributes` is tested before it is committed."""
    out = subprocess.run(["git", "-C", str(ROOT), "archive", "--worktree-attributes", "HEAD"],
                         capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(out)) as tar:
        return set(tar.getnames())


def under(name, top):
    return name == top or name.startswith(top + "/")


@needs_checkout
@needs_present("openspec")
def test_the_archive_drops_openspec():
    """The archive holds no openspec/ entry at all: not the file, and not an
    empty directory left behind."""
    assert not [n for n in archive_names() if under(n.rstrip("/"), "openspec")]


@needs_checkout
def test_the_archive_drops_design_and_nothing_else_new():
    """The archive of the working-tree rules holds no design/ entry at all,
    and every other tracked file is still in it, bar the root CLAUDE.md and
    openspec/."""
    names = archive_names()
    assert not [n for n in names if under(n.rstrip("/"), "design")]
    in_head = set(git("ls-tree", "-r", "-z", "--name-only", "HEAD").split("\0")[:-1])
    shipped = {n for n in in_head if n != "CLAUDE.md"
               and not under(n, "design") and not under(n, "openspec")}
    assert shipped - names == set()


# changelog.d/ holds the pending entries the release moves into CHANGELOG.md.
SHIPPED_DOCS = ["README.md", "AGENTS.md", "CHANGELOG.md", "changelog.d", "skills"]


def shipped_doc_files():
    for name in SHIPPED_DOCS:
        path = ROOT / name
        yield from (sorted(path.rglob("*.md")) if path.is_dir() else [path])


def test_shipped_docs_do_not_point_into_design():
    """design/ is not in a release archive, so a shipped doc that cites it
    sends the reader to a file they do not have. State the rule inline."""
    hits = [f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
            for path in shipped_doc_files()
            for n, line in enumerate(path.read_text().splitlines(), 1)
            if "design/" in line]
    assert hits == []

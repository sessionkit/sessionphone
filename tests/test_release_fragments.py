"""CHANGELOG fragments: each change writes changelog.d/<TASK-ID>-<slug>.md and
scripts/release.py collates them into CHANGELOG.md at release, so two
branches stop clashing on CHANGELOG.md.

The unit tests load scripts/release.py as a module. The end-to-end tests run
it in a scratch git repo with a stand-in `uv` first on PATH (it bumps
pyproject's version and nothing else, and refuses a version call without
--frozen), so no test touches the network, this checkout's version, or the
real uv's lock and venv. tests/test_release_frozen.py runs the real uv.

sessionphone's shape is SECTIONS = None: one "### <headline>" section per
entry, kept whole. The Keep a Changelog cases set SECTIONS to the tuple, the
shape another repo copying the collector would run."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release.py"
KAC = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")

_spec = importlib.util.spec_from_file_location("sessionphone_release", SCRIPT)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def frag(d: Path, name: str, text: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text)
    return p


def today() -> str:
    return release.datetime.now().astimezone().date().isoformat()


@pytest.fixture
def kac(monkeypatch):
    monkeypatch.setattr(release, "SECTIONS", KAC)


def test_sessionphone_runs_the_headline_shape():
    assert release.SECTIONS is None


def test_stamp_keeps_its_two_argument_call():
    """PHONE-227's guard calls stamp(version, dry=False): fragments defaults to none."""
    import inspect

    sig = inspect.signature(release.stamp)
    assert list(sig.parameters) == ["version", "dry", "fragments"]
    assert tuple(sig.parameters["fragments"].default) == ()


# ---- which files are fragments, and their order --------------------------------


def test_absent_dir_is_no_fragments(tmp_path):
    assert release.fragment_paths(tmp_path / "changelog.d") == []


def test_readme_and_non_markdown_are_not_fragments(tmp_path):
    d = tmp_path / "changelog.d"
    frag(d, "README.md", "# how to\n")
    frag(d, ".gitkeep", "")
    frag(d, "notes.txt", "### X\n\n- x\n")
    frag(d, "PHONE-9-x.markdown", "### X\n\n- x\n")
    frag(d / "sub", "PHONE-9-x.md", "### X\n\n- x\n")
    assert release.fragment_paths(d) == []


def test_order_is_by_name_with_the_id_compared_as_a_number(tmp_path):
    d = tmp_path / "changelog.d"
    names = (
        "PHONE-1000-k.md",
        "ATN-3-c.md",
        "PHONE-138-b.md",
        "PHONE-999-z.md",
        "PHONE-20-z.md",
        "PHONE-9-a.md",
        "PHONE-138-a.md",
    )
    for name in names:
        frag(d, name, "### X\n\n- x\n")
    assert [p.name for p in release.fragment_paths(d)] == [
        "ATN-3-c.md",
        "PHONE-9-a.md",
        "PHONE-20-z.md",
        "PHONE-138-a.md",
        "PHONE-138-b.md",
        "PHONE-999-z.md",
        "PHONE-1000-k.md",
    ]


def test_order_does_not_depend_on_creation_order(tmp_path):
    a, b = tmp_path / "a" / "changelog.d", tmp_path / "b" / "changelog.d"
    names = ["PHONE-2-x.md", "PHONE-10-x.md", "PHONE-1-x.md"]
    for n in names:
        frag(a, n, "### X\n\n- x\n")
    for n in reversed(names):
        frag(b, n, "### X\n\n- x\n")
    assert [p.name for p in release.fragment_paths(a)] == [p.name for p in release.fragment_paths(b)]


# ---- reading one fragment: the headline shape (SECTIONS = None) ---------------------


def test_headline_parse_keeps_each_section_whole_and_in_order(tmp_path):
    p = frag(
        tmp_path,
        "PHONE-1-x.md",
        "\n### A turn reaches the agent whole\n\n- one\n- two\n  more\n\n### Speech waits once\n- three\n\n",
    )
    assert release.parse_fragment(p) == [
        ("A turn reaches the agent whole", "- one\n- two\n  more"),
        ("Speech waits once", "- three"),
    ]


def test_headline_parse_merges_nothing_within_a_fragment(tmp_path):
    p = frag(tmp_path, "PHONE-1-x.md", "### Same\n\n- one\n\n### Other\n\n- o\n\n### Same\n\n- two\n")
    assert release.parse_fragment(p) == [("Same", "- one"), ("Other", "- o"), ("Same", "- two")]


def test_headline_parse_drops_an_empty_section(tmp_path):
    p = frag(tmp_path, "PHONE-1-x.md", "### Empty\n\n### Full\n\n- f\n")
    assert release.parse_fragment(p) == [("Full", "- f")]


def test_headline_parse_accepts_a_keep_a_changelog_name_as_a_headline(tmp_path):
    p = frag(tmp_path, "PHONE-1-x.md", "### Fixed\n\n- f\n")
    assert release.parse_fragment(p) == [("Fixed", "- f")]


def test_a_heading_inside_a_code_fence_is_not_a_section(tmp_path):
    body = "### Shows a fence\n\n- shows\n\n  ```md\n  ### Nonsense\n  ## [Unreleased]\n  ```\n"
    p = frag(tmp_path, "PHONE-1-x.md", body)
    assert release.parse_fragment(p) == [
        ("Shows a fence", "- shows\n\n  ```md\n  ### Nonsense\n  ## [Unreleased]\n  ```")
    ]


@pytest.mark.parametrize(
    "text, says",
    [
        ("- loose\n\n### Headline\n\n- x\n", "outside a ### section"),
        ("### Headline\n\n- x\n\n## [1.0.0]\n", "## [1.0.0]"),
        ("# Title\n\n### Headline\n\n- x\n", "# Title"),
        ("### Headline\n\n#### Sub\n\n- x\n", "#### Sub"),
        ("###\n\n- x\n", "'###'"),
        ("### \n\n- x\n", "'### '"),
        ("\n\n", "no entries"),
        ("### One\n\n### Two\n", "no entries"),
    ],
)
def test_headline_parse_refuses_a_bad_fragment(tmp_path, text, says):
    p = frag(tmp_path, "PHONE-1-x.md", text)
    with pytest.raises(SystemExit) as e:
        release.parse_fragment(p)
    msg = str(e.value)
    assert msg.startswith("release: ") and "PHONE-1-x.md" in msg and says in msg


def test_a_heading_refusal_names_the_line(tmp_path):
    p = frag(tmp_path, "PHONE-1-x.md", "### Headline\n\n- x\n\n## [1.0.0]\n")
    with pytest.raises(SystemExit) as e:
        release.parse_fragment(p)
    assert "PHONE-1-x.md:5:" in str(e.value) and "### <headline> sections" in str(e.value)


# ---- reading one fragment: Keep a Changelog (SECTIONS = a tuple) --------------------


def test_kac_parse_reads_sections(tmp_path, kac):
    p = frag(tmp_path, "ATN-1-x.md", "\n### Added\n\n- one\n- two\n  more\n\n### Fixed\n- three\n\n")
    assert dict(release.parse_fragment(p)) == {"Added": "- one\n- two\n  more", "Fixed": "- three"}


def test_kac_parse_merges_a_repeated_section_within_one_fragment(tmp_path, kac):
    p = frag(tmp_path, "ATN-1-x.md", "### Added\n\n- one\n\n### Fixed\n\n- f\n\n### Added\n\n- two\n")
    assert release.parse_fragment(p) == [("Added", "- one\n- two"), ("Fixed", "- f")]


def test_kac_parse_drops_an_empty_section(tmp_path, kac):
    p = frag(tmp_path, "ATN-1-x.md", "### Added\n\n### Fixed\n\n- f\n")
    assert release.parse_fragment(p) == [("Fixed", "- f")]


@pytest.mark.parametrize(
    "text, says",
    [
        ("### Addded\n\n- typo\n", "### Addded"),
        ("### A headline\n\n- x\n", "### A headline"),
        ("- loose\n\n### Added\n\n- x\n", "outside a ### section"),
        ("### Added\n\n- x\n\n## [1.0.0]\n", "## [1.0.0]"),
        ("# Title\n\n### Added\n\n- x\n", "# Title"),
        ("\n\n", "no entries"),
        ("### Added\n\n### Fixed\n", "no entries"),
    ],
)
def test_kac_parse_refuses_a_bad_fragment(tmp_path, kac, text, says):
    p = frag(tmp_path, "ATN-1-x.md", text)
    with pytest.raises(SystemExit) as e:
        release.parse_fragment(p)
    msg = str(e.value)
    assert msg.startswith("release: ") and "ATN-1-x.md" in msg and says in msg


# ---- merging fragments ------------------------------------------------------------


def test_collect_nothing_is_empty():
    assert release.collect([]) == ""


def test_headline_collect_keeps_every_section_whole_in_fragment_order(tmp_path):
    d = tmp_path / "changelog.d"
    a = frag(d, "PHONE-9-a.md", "### Calls ring once\n\n- a1\n\n### Fixed\n\n- a2\n")
    b = frag(d, "PHONE-20-b.md", "### Fixed\n\n- b1\n")
    c = frag(d, "PHONE-1000-c.md", "### Calls ring once\n\n- c1\n  more\n")
    paths = release.fragment_paths(d)
    assert paths == [a, b, c]
    assert release.collect(paths) == (
        "### Calls ring once\n\n- a1\n\n"
        "### Fixed\n\n- a2\n\n"
        "### Fixed\n\n- b1\n\n"
        "### Calls ring once\n\n- c1\n  more"
    )


def test_kac_collect_merges_same_named_sections_in_keep_a_changelog_order(tmp_path, kac):
    d = tmp_path / "changelog.d"
    a = frag(d, "ATN-1-a.md", "### Fixed\n\n- a-fix\n\n### Added\n\n- a-add\n")
    b = frag(d, "ATN-2-b.md", "### Security\n\n- b-sec\n\n### Added\n\n- b-add\n")
    c = frag(d, "ATN-3-c.md", "### Deprecated\n\n- c-dep\n\n### Changed\n\n- c-chg\n\n### Removed\n\n- c-rm\n")
    assert release.collect([a, b, c]) == (
        "### Added\n\n- a-add\n- b-add\n\n"
        "### Changed\n\n- c-chg\n\n"
        "### Deprecated\n\n- c-dep\n\n"
        "### Removed\n\n- c-rm\n\n"
        "### Fixed\n\n- a-fix\n\n"
        "### Security\n\n- b-sec"
    )


# ---- stamping CHANGELOG.md ----------------------------------------------------------

LOG = """# Changelog

Header.

## [Unreleased]

### A legacy entry

- old-one

## [0.0.1] - 2026-01-02

### Born

- born
"""

KAC_LOG = """# Changelog

Header.

## [Unreleased]

### Added

- old-add

### Fixed

- old-fix

## [0.0.1] - 2026-01-02

### Added

- born
"""


def test_stamp_without_fragments_is_the_old_stamp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    release.stamp("0.0.2", dry=False)
    assert Path("CHANGELOG.md").read_text() == LOG.replace(
        "## [Unreleased]", f"## [Unreleased]\n\n## [0.0.2] - {today()}", 1
    )


def test_stamp_with_an_empty_fragment_list_is_the_old_stamp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    release.stamp("0.0.2", dry=False, fragments=[])
    assert Path("CHANGELOG.md").read_text() == LOG.replace(
        "## [Unreleased]", f"## [Unreleased]\n\n## [0.0.2] - {today()}", 1
    )


def test_stamp_puts_fragments_then_the_unreleased_body_under_the_version(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    d = Path("changelog.d")
    frag(d, "PHONE-10-b.md", "### Ten\n\n- ten\n")
    frag(d, "PHONE-9-a.md", "### Nine\n\n- nine\n\n### Nine again\n\n- nine-2\n")
    release.stamp("0.0.2", dry=False, fragments=release.fragment_paths(d))
    assert Path("CHANGELOG.md").read_text() == f"""# Changelog

Header.

## [Unreleased]

## [0.0.2] - {today()}

### Nine

- nine

### Nine again

- nine-2

### Ten

- ten

### A legacy entry

- old-one

## [0.0.1] - 2026-01-02

### Born

- born
"""


def test_kac_stamp_merges_fragments_then_the_unreleased_body(tmp_path, monkeypatch, kac):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(KAC_LOG)
    a = frag(Path("changelog.d"), "ATN-5-a.md", "### Fixed\n\n- frag-fix\n\n### Added\n\n- frag-add\n")
    release.stamp("0.0.2", dry=False, fragments=[a])
    assert Path("CHANGELOG.md").read_text() == f"""# Changelog

Header.

## [Unreleased]

## [0.0.2] - {today()}

### Added

- frag-add

### Fixed

- frag-fix

### Added

- old-add

### Fixed

- old-fix

## [0.0.1] - 2026-01-02

### Added

- born
"""


def test_stamp_with_an_empty_unreleased_and_no_older_version(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    a = frag(Path("changelog.d"), "PHONE-5-a.md", "### Frag\n\n- frag\n")
    release.stamp("1.0.0", dry=False, fragments=[a])
    assert Path("CHANGELOG.md").read_text() == (
        f"# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - {today()}\n\n### Frag\n\n- frag\n"
    )


def test_stamp_keeps_the_fresh_unreleased_directly_above_the_version(tmp_path, monkeypatch):
    """PHONE-227's pin: an empty [Unreleased] heading, then the new version."""
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    a = frag(Path("changelog.d"), "PHONE-5-a.md", "### Frag\n\n- frag\n")
    release.stamp("9.9.9", dry=False, fragments=[a])
    assert "## [Unreleased]\n\n## [9.9.9] - " in Path("CHANGELOG.md").read_text()


def test_stamp_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    a = frag(Path("changelog.d"), "PHONE-5-a.md", "### Frag headline\n\n- frag\n")
    release.stamp("0.0.2", dry=True, fragments=[a])
    assert Path("CHANGELOG.md").read_text() == LOG
    assert a.exists()
    out = capsys.readouterr().out
    assert "changelog.d/PHONE-5-a.md" in out and "Frag headline" in out


def test_stamp_with_fragments_refuses_an_unreleased_that_is_not_its_own_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased] - soon\n\n### X\n\n- x\n")
    a = frag(Path("changelog.d"), "PHONE-5-a.md", "### Frag\n\n- frag\n")
    with pytest.raises(SystemExit) as e:
        release.stamp("1.0.0", dry=False, fragments=[a])
    assert "exactly '## [Unreleased]'" in str(e.value)
    assert Path("CHANGELOG.md").read_text().startswith("# Changelog\n\n## [Unreleased] - soon\n")


# ---- the preview ------------------------------------------------------------------


def test_preview_collates_into_unreleased_ahead_of_its_body(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    d = Path("changelog.d")
    frag(d, "PHONE-10-b.md", "### Ten\n\n- ten\n")
    frag(d, "PHONE-9-a.md", "### Nine\n\n- nine\n")
    assert release.preview(release.fragment_paths(d)) == (
        "## [Unreleased]\n\n### Nine\n\n- nine\n\n### Ten\n\n- ten\n\n### A legacy entry\n\n- old-one\n"
    )
    assert Path("CHANGELOG.md").read_text() == LOG


def test_preview_reads_as_the_stamp_writes(tmp_path, monkeypatch):
    """One merge for both: the preview's [Unreleased] body is the stamped version's body."""
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    d = Path("changelog.d")
    frag(d, "PHONE-9-a.md", "### Nine\n\n- nine\n\n```\n### not a section\n```\n")
    frag(d, "PHONE-1000-b.md", "### Thousand\n\n- k\n")
    paths = release.fragment_paths(d)
    previewed = release.preview_text(paths)
    release.stamp("0.0.2", dry=False, fragments=paths)
    stamped = Path("CHANGELOG.md").read_text()
    assert stamped == previewed.replace("## [Unreleased]", f"## [Unreleased]\n\n## [0.0.2] - {today()}", 1)


def test_preview_with_no_fragments_is_the_unreleased_section_as_it_is(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    assert release.preview([]) == "## [Unreleased]\n\n### A legacy entry\n\n- old-one\n"
    assert release.preview_text([]) == LOG


def test_preview_refuses_a_bad_fragment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CHANGELOG.md").write_text(LOG)
    a = frag(Path("changelog.d"), "PHONE-5-a.md", "loose text\n")
    with pytest.raises(SystemExit) as e:
        release.preview([a])
    assert "PHONE-5-a.md:1:" in str(e.value)


def test_preview_of_this_repo_holds_every_live_fragment():
    """The live tree, read as `--preview` reads it, from the repo root."""
    paths = release.fragment_paths(ROOT / "changelog.d")
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        out = release.preview(paths)
    finally:
        os.chdir(cwd)
    assert out.startswith("## [Unreleased]\n")
    for p in paths:
        for name, body in release.parse_fragment(p):
            assert f"### {name}\n\n{body}" in out, p.name


# ---- end to end: the script in a scratch repo ----------------------------------------

FAKE_UV = '''
import re, sys
from pathlib import Path
a = sys.argv[1:]
p = Path("pyproject.toml")
t = p.read_text()
old = re.search(r'^version = "([^"]+)"', t, re.M).group(1)
if a == ["version", "--short"]:
    print(old); sys.exit(0)
if "--frozen" not in a:
    sys.exit("fake uv: a version write without --frozen would re-lock")
dry = "--dry-run" in a
a = [x for x in a if x not in ("--dry-run", "--frozen")]
if a[:2] == ["version", "--bump"]:
    M, m, P = map(int, old.split("."))
    new = {"patch": f"{M}.{m}.{P + 1}", "minor": f"{M}.{m + 1}.0", "major": f"{M + 1}.0.0"}[a[2]]
else:
    new = a[1]
if not dry:
    p.write_text(t.replace(f'version = "{old}"', f'version = "{new}"', 1))
print(f"demo {old} => {new}")
'''

PYPROJECT = '[project]\nname = "demo"\nversion = "0.0.1"\n'
LOCK = 'version = 1\n\n[[package]]\nname = "demo"\nversion = "0.0.1"\nsource = { editable = "." }\n'


@pytest.fixture
def repo(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    uv = bin_ / "uv"
    uv.write_text(f"#!{sys.executable}\n{FAKE_UV}")
    uv.chmod(0o755)
    r = tmp_path / "repo"
    r.mkdir()
    # The scratch repo sees no git config of this machine's: not the files, and
    # not the command-line config a seat's environment carries (signing on).
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("GIT_CONFIG_", "GIT_DIR", "GIT_WORK_TREE"))},
        "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    (r / "pyproject.toml").write_text(PYPROJECT)
    (r / "uv.lock").write_text(LOCK)
    (r / "CHANGELOG.md").write_text(LOG)
    frag(r / "changelog.d", "README.md", "How to write a fragment.\n")
    frag(r / "changelog.d", "PHONE-9-nine.md", "### Nine lands\n\n- nine\n")
    frag(r / "changelog.d", "PHONE-10-ten.md", "### Ten lands\n\n- ten\n\n### Ten again\n\n- ten-2\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=r, env=env, capture_output=True, text=True, check=True).stdout

    def run(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=r, env=env, capture_output=True, text=True, check=False
        )

    git("init", "-q", "-b", "master")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return r, git, run


def assert_untouched(r, git):
    """Refused before the bump: nothing written, nothing committed, nothing tagged."""
    assert (r / "pyproject.toml").read_text() == PYPROJECT
    assert (r / "uv.lock").read_text() == LOCK
    assert (r / "CHANGELOG.md").read_text() == LOG
    assert (r / "changelog.d" / "PHONE-9-nine.md").exists()
    assert "chore: release" not in git("log", "--format=%s")
    assert git("tag", "-l") == ""


def test_release_collects_deletes_and_commits_the_deletions(repo):
    r, git, run = repo
    out = run("patch")
    assert out.returncode == 0, out.stderr
    log = (r / "CHANGELOG.md").read_text()
    assert (
        f"## [Unreleased]\n\n## [0.0.2] - {today()}\n\n"
        "### Nine lands\n\n- nine\n\n### Ten lands\n\n- ten\n\n### Ten again\n\n- ten-2\n\n"
        "### A legacy entry\n\n- old-one\n\n## [0.0.1] - 2026-01-02\n"
    ) in log
    assert sorted(p.name for p in (r / "changelog.d").iterdir()) == ["README.md"]
    assert git("ls-files", "changelog.d") == "changelog.d/README.md\n"
    assert git("status", "--porcelain") == ""
    changed = git("show", "--name-status", "--format=%s", "HEAD").split("\n")
    assert changed[0] == "chore: release v0.0.2"
    assert "D\tchangelog.d/PHONE-10-ten.md" in changed and "D\tchangelog.d/PHONE-9-nine.md" in changed
    assert "M\tCHANGELOG.md" in changed and "M\tpyproject.toml" in changed and "M\tuv.lock" in changed
    assert (r / "uv.lock").read_text() == LOCK.replace('version = "0.0.1"', 'version = "0.0.2"')
    assert git("tag", "-l") == "v0.0.2\n"


def test_release_orders_past_999_by_number(repo):
    r, git, run = repo
    frag(r / "changelog.d", "PHONE-1000-k.md", "### Thousand lands\n\n- k\n")
    frag(r / "changelog.d", "PHONE-999-n.md", "### Nine-ninety-nine lands\n\n- n\n")
    git("add", "-A")
    git("commit", "-q", "-m", "two more")
    out = run("patch")
    assert out.returncode == 0, out.stderr
    log = (r / "CHANGELOG.md").read_text()
    order = ["Nine lands", "Ten lands", "Nine-ninety-nine lands", "Thousand lands", "A legacy entry"]
    assert [log.index(h) for h in order] == sorted(log.index(h) for h in order)


def test_release_dry_run_lists_the_fragments_and_touches_nothing(repo):
    r, git, run = repo
    head = git("rev-parse", "HEAD")
    out = run("patch", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "changelog.d/PHONE-9-nine.md" in out.stdout and "changelog.d/PHONE-10-ten.md" in out.stdout
    assert "git rm" in out.stdout
    assert out.stdout.index("PHONE-9-nine") < out.stdout.index("PHONE-10-ten")
    assert git("status", "--porcelain") == "" and git("rev-parse", "HEAD") == head
    assert_untouched(r, git)


def test_release_without_changelog_d_is_the_old_stamp_byte_for_byte(repo):
    r, git, run = repo
    git("rm", "-q", "-r", "changelog.d")
    git("commit", "-q", "-m", "no fragments")
    out = run("minor")
    assert out.returncode == 0, out.stderr
    assert (r / "CHANGELOG.md").read_text() == LOG.replace(
        "## [Unreleased]", f"## [Unreleased]\n\n## [0.1.0] - {today()}", 1
    )
    assert git("tag", "-l") == "v0.1.0\n"


def test_release_with_only_the_readme_is_the_old_stamp_byte_for_byte(repo):
    r, git, run = repo
    git("rm", "-q", "changelog.d/PHONE-9-nine.md", "changelog.d/PHONE-10-ten.md")
    git("commit", "-q", "-m", "readme only")
    out = run("patch")
    assert out.returncode == 0, out.stderr
    assert (r / "CHANGELOG.md").read_text() == LOG.replace(
        "## [Unreleased]", f"## [Unreleased]\n\n## [0.0.2] - {today()}", 1
    )
    assert git("ls-files", "changelog.d") == "changelog.d/README.md\n"
    assert git("status", "--porcelain") == ""


def test_release_refuses_a_bad_fragment_before_it_bumps(repo):
    r, git, run = repo
    frag(r / "changelog.d", "PHONE-11-bad.md", "loose\n\n### Headline\n\n- x\n")
    git("add", "-A")
    git("commit", "-q", "-m", "bad fragment")
    out = run("patch")
    assert out.returncode != 0
    assert "PHONE-11-bad.md:1:" in out.stderr and "outside a ### section" in out.stderr
    assert_untouched(r, git)
    assert git("status", "--porcelain") == ""


def test_release_refuses_a_fragment_git_ignores_before_it_bumps(repo):
    """N8: a *.md hidden by .git/info/exclude passes the clean-tree check; the
    two-way check refuses it before anything is written."""
    r, git, run = repo
    (r / ".git" / "info").mkdir(exist_ok=True)
    (r / ".git" / "info" / "exclude").write_text("changelog.d/PHONE-12-hidden.md\n")
    frag(r / "changelog.d", "PHONE-12-hidden.md", "### Hidden\n\n- h\n")
    assert git("status", "--porcelain") == ""
    for args in (("patch",), ("patch", "--dry-run")):
        out = run(*args)
        assert out.returncode != 0, args
        assert "changelog.d/PHONE-12-hidden.md" in out.stderr and "does not track" in out.stderr
        assert "uv version" not in out.stdout
        assert_untouched(r, git)


@pytest.mark.parametrize(
    "path",
    ["changelog.d/PHONE-13-x.markdown", "changelog.d/sub/PHONE-13-x.md", "changelog.d/sub/README.md", "changelog.d/notes.txt"],
)
def test_release_refuses_a_tracked_stray_before_it_bumps(repo, path):
    """N8: a tracked file the collector would not read refuses, named."""
    r, git, run = repo
    p = r / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("### Stray\n\n- s\n")
    git("add", "-A")
    git("commit", "-q", "-m", "stray")
    for args in (("patch",), ("patch", "--dry-run")):
        out = run(*args)
        assert out.returncode != 0, args
        assert path in out.stderr and "not a fragment the release reads" in out.stderr
        assert "uv version" not in out.stdout
        assert_untouched(r, git)


def test_release_passes_an_ignored_file_that_is_not_a_fragment(repo):
    """Finder's .DS_Store: ignored, not *.md, so neither side of the check sees it."""
    r, git, run = repo
    (r / ".git" / "info" / "exclude").write_text(".DS_Store\n")
    (r / "changelog.d" / ".DS_Store").write_bytes(b"\0\1")
    out = run("patch")
    assert out.returncode == 0, out.stderr
    assert git("tag", "-l") == "v0.0.2\n"


@pytest.mark.parametrize(
    "setup, says",
    [
        ("dirty", "not clean"),
        ("untracked-fragment", "not clean"),
        ("no-unreleased", "no '## [Unreleased]'"),
        ("no-changelog", "CHANGELOG.md not found"),
        ("tag", "already exists"),
    ],
)
def test_the_old_refusals_still_hold(repo, setup, says):
    r, git, run = repo
    if setup == "dirty":
        (r / "stray").write_text("x")
    elif setup == "untracked-fragment":
        frag(r / "changelog.d", "PHONE-14-new.md", "### New\n\n- n\n")
    elif setup == "no-unreleased":
        (r / "CHANGELOG.md").write_text(LOG.replace("## [Unreleased]", "## [Next]"))
        git("commit", "-q", "-am", "x")
    elif setup == "no-changelog":
        git("rm", "-q", "CHANGELOG.md")
        git("commit", "-q", "-m", "x")
    elif setup == "tag":
        git("tag", "v0.0.2")
    out = run("patch")
    assert out.returncode != 0 and says in out.stderr
    assert (r / "changelog.d" / "PHONE-9-nine.md").exists()
    assert "chore: release" not in git("log", "--format=%s")


def test_check_left_refuses_a_fragment_still_tracked(repo, monkeypatch):
    """The post-rm self-check reads git: a tracked fragment left behind refuses."""
    r, git, _run = repo
    monkeypatch.chdir(r)
    with pytest.raises(SystemExit) as e:
        release.check_left()
    assert "changelog.d/PHONE-10-ten.md" in str(e.value) and "changelog.d/PHONE-9-nine.md" in str(e.value)
    git("rm", "-q", "changelog.d/PHONE-9-nine.md", "changelog.d/PHONE-10-ten.md")
    release.check_left()  # only README.md left: passes


def test_check_left_reads_git_not_the_glob(repo, monkeypatch):
    """A tracked file the glob never chose (a nested one) still fails it."""
    r, git, _run = repo
    monkeypatch.chdir(r)
    frag(r / "changelog.d" / "sub", "PHONE-15-x.md", "### X\n\n- x\n")
    git("add", "-A")
    git("rm", "-q", "changelog.d/PHONE-9-nine.md", "changelog.d/PHONE-10-ten.md")
    assert release.fragment_paths(r / "changelog.d") == []
    with pytest.raises(SystemExit) as e:
        release.check_left()
    assert "changelog.d/sub/PHONE-15-x.md" in str(e.value)


def test_preview_needs_no_uv_no_git_and_writes_nothing(tmp_path):
    """Not a git repo, a dirty draft, a PATH with neither uv nor git on it."""
    r = tmp_path / "plain"
    r.mkdir()
    (r / "CHANGELOG.md").write_text(LOG)
    frag(r / "changelog.d", "README.md", "How to.\n")
    frag(r / "changelog.d", "PHONE-10-ten.md", "### Ten lands\n\n- ten\n")
    frag(r / "changelog.d", "PHONE-9-nine.md", "### Nine lands\n\n- nine\n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    before = {p: p.read_bytes() for p in r.rglob("*") if p.is_file()}
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--preview"],
        cwd=r,
        env={"PATH": str(empty), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == (
        "## [Unreleased]\n\n### Nine lands\n\n- nine\n\n### Ten lands\n\n- ten\n\n### A legacy entry\n\n- old-one\n"
    )
    assert {p: p.read_bytes() for p in r.rglob("*") if p.is_file()} == before


def test_preview_in_a_repo_leaves_git_as_it_was(repo):
    r, git, run = repo
    head = git("rev-parse", "HEAD")
    out = run("--preview")
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("## [Unreleased]\n\n### Nine lands\n")
    assert git("status", "--porcelain") == "" and git("rev-parse", "HEAD") == head
    assert_untouched(r, git)

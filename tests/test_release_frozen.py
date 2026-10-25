"""scripts/release.py never re-locks, and refuses an existing tag before it writes.

A plain `uv version X` re-locks the project, and the release commits
uv.lock, so a cut before the deps are on PyPI failed, or committed a
local-path lock. These tests pin the fix:

- `uv version` carries --frozen, on the real path and the dry path alike;
- a REAL release, in a throwaway git clone, leaves every line of uv.lock as
  it was except the root `sessionphone` version line, and `uv lock --check`
  (offline) passes after it;
- the root-line edit fails closed: no root block, two, or two version lines
  in it, and the release refuses before `uv version` writes anything;
- an existing tag for the version the release would make is refused before
  `uv version` writes, so the tree is left clean;
- `make release` runs the script with python3, never a re-locking `uv run`.

A dry run cannot catch the re-lock (`uv version --dry-run` never locks), so
the release tests are real ones. Every uv call here runs offline
(UV_OFFLINE=1) against an EMPTY cache of its own, so any resolve fails: an
offline re-lock from a warm cache writes the same bytes as the fix when the
deps are unchanged, and would pass unseen. One case also floors a dependency
at a version no index has, the family-cut case the bug broke.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release.py"
UV = shutil.which("uv")
GIT = shutil.which("git")
MAKE = shutil.which("make")
# The working-tree files a release reads or writes: the clone gets these as
# they are on disk, so the test checks the code under test, not HEAD's.
OVERLAY = ("scripts/release.py", "pyproject.toml", "uv.lock", "CHANGELOG.md", ".claude-plugin", "changelog.d")
# What a refused release must leave exactly as it was.
WATCHED = ("pyproject.toml", "uv.lock", "CHANGELOG.md", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json")

needs_uv_git = pytest.mark.skipif(UV is None or GIT is None, reason="uv and git are needed")


def load_release():
    spec = importlib.util.spec_from_file_location("release_frozen_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env(cwd: Path) -> dict[str, str]:
    """No UV_* from the host (a UV_FROZEN would hide the bug), offline, an
    empty uv cache beside the clone, and a git with no user or system config
    (no signing, no hooks), named here."""
    out = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "GIT_", "VIRTUAL_ENV"))}
    out.update(
        UV_OFFLINE="1",
        UV_CACHE_DIR=str(cwd.parent / "uv-cache"),
        UV_PYTHON_DOWNLOADS="never",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="release test",
        GIT_AUTHOR_EMAIL="release-test@example.invalid",
        GIT_COMMITTER_NAME="release test",
        GIT_COMMITTER_EMAIL="release-test@example.invalid",
    )
    return out


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env(cwd), capture_output=True, text=True, check=False)


def git(cwd: Path, *args: str) -> str:
    out = run(cwd, "git", *args)
    assert out.returncode == 0, out.stderr
    return out.stdout


def release(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(cwd, sys.executable, "scripts/release.py", *args)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A git clone of this repo with the working tree's release files laid
    over it and committed, so the tree is clean for release.py."""
    repo = tmp_path / "repo"
    git(tmp_path, "clone", "-q", "--no-hardlinks", str(ROOT), str(repo))
    for rel in OVERLAY:
        src, dst = ROOT / rel, repo / rel
        if src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)
    git(repo, "add", "-A", "--", *[r for r in OVERLAY if (repo / r).exists()])
    if git(repo, "status", "--porcelain").strip():
        git(repo, "commit", "-q", "-m", "test: the working tree's release files")
    for tag in git(repo, "tag", "-l").split():
        git(repo, "tag", "-d", tag)
    return repo


def snapshot(repo: Path) -> dict[str, bytes]:
    return {f: (repo / f).read_bytes() for f in WATCHED if (repo / f).exists()}


def version_of(repo: Path) -> str:
    return tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]


def root_line(lock: str) -> str:
    m = re.search(r'^\[\[package\]\]\nname = "sessionphone"\n(version = "[^"]*")\n', lock, re.MULTILINE)
    assert m, "no root sessionphone block in uv.lock"
    return m.group(1)


def with_root(lock: str, old: str, new: str) -> str:
    """`lock` with the root sessionphone version line, and only it, moved to `new`."""
    head = '[[package]]\nname = "sessionphone"\nversion = '
    assert lock.count(f'{head}"{old}"\n') == 1
    return lock.replace(f'{head}"{old}"\n', f'{head}"{new}"\n')


def next_patch(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


# --- the uv call carries --frozen --------------------------------------------


@pytest.mark.parametrize("spec", ["patch", "minor", "major", "9.8.7"])
@pytest.mark.parametrize("dry", [False, True])
def test_uv_version_is_frozen_on_both_paths(monkeypatch, spec: str, dry: bool) -> None:
    rel = load_release()
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="sessionphone 0.3.0 => 9.8.7\n", stderr="")

    monkeypatch.setattr(rel.subprocess, "run", fake_run)
    assert rel.bump(spec, dry) == "9.8.7"
    (argv,) = calls
    assert argv[:2] == ["uv", "version"]
    assert "--frozen" in argv
    assert ("--dry-run" in argv) is dry
    # The dry argv is the real argv plus --dry-run: a rehearsal runs the same call.
    assert [a for a in argv if a != "--dry-run"] == (
        ["uv", "version", "--bump", spec, "--frozen"]
        if spec in ("patch", "minor", "major")
        else ["uv", "version", spec, "--frozen"]
    )


# --- a real release ------------------------------------------------------------


@needs_uv_git
def test_real_release_changes_only_the_root_lock_line(clone: Path) -> None:
    before = (clone / "uv.lock").read_text()
    old = version_of(clone)
    new = next_patch(old)

    out = release(clone, "patch")
    assert out.returncode == 0, out.stdout + out.stderr

    assert version_of(clone) == new
    after = (clone / "uv.lock").read_text()
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    assert len(a) == len(b), "uv.lock gained or lost lines: something re-locked"
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    assert changed == [(f'version = "{old}"\n', f'version = "{new}"\n')]
    assert root_line(after) == f'version = "{new}"'  # the changed line IS the root line
    # Nothing resolved: no uv.lock written by uv, so nothing else moved either.
    assert after == with_root(before, old, new)

    # The release committed it and tagged it, and left the tree clean.
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "log", "-1", "--format=%s").strip() == f"chore: release v{new}"
    assert git(clone, "tag", "-l", f"v{new}").strip() == f"v{new}"
    assert root_line(git(clone, "show", f"v{new}:uv.lock")) == f'version = "{new}"'
    assert not (clone / ".venv").exists(), "something synced an environment"


@needs_uv_git
def test_real_set_release_changes_only_the_root_lock_line(clone: Path) -> None:
    before = (clone / "uv.lock").read_text()
    old = version_of(clone)

    out = release(clone, "9.8.7")
    assert out.returncode == 0, out.stdout + out.stderr

    assert version_of(clone) == "9.8.7"
    assert (clone / "uv.lock").read_text() == with_root(before, old, "9.8.7")
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "tag", "-l").strip() == "v9.8.7"


@needs_uv_git
def test_release_with_a_floor_no_index_has(clone: Path) -> None:
    """The family cut: a dependency floored at a release not yet on any
    index. A re-lock is unsatisfiable here; the release must not need one."""
    pyproject = clone / "pyproject.toml"
    text = pyproject.read_text()
    floored = re.sub(r'"segno>=[^"]+"', '"segno>=99.0.0"', text, count=1)
    assert floored != text
    pyproject.write_text(floored)
    git(clone, "commit", "-q", "-am", "test: floor a dep at an unreleased version")
    before = (clone / "uv.lock").read_text()
    old = version_of(clone)
    new = next_patch(old)

    out = release(clone, "patch")
    assert out.returncode == 0, out.stdout + out.stderr

    assert (clone / "uv.lock").read_text() == with_root(before, old, new)
    assert root_line((clone / "uv.lock").read_text()) == f'version = "{new}"'
    assert git(clone, "status", "--porcelain").strip() == ""
    assert not (clone / ".venv").exists(), "something synced an environment"


@needs_uv_git
def test_lock_check_passes_offline_after_the_release(clone: Path) -> None:
    pre = run(clone, "uv", "lock", "--check")
    assert pre.returncode == 0, f"uv lock --check fails offline before the release: {pre.stderr.strip()}"
    out = release(clone, "patch")
    assert out.returncode == 0, out.stdout + out.stderr
    check = run(clone, "uv", "lock", "--check")
    assert check.returncode == 0, check.stderr
    assert git(clone, "status", "--porcelain").strip() == ""


@needs_uv_git
def test_dry_run_reports_the_lock_edit_and_writes_nothing(clone: Path) -> None:
    before = snapshot(clone)
    head = git(clone, "rev-parse", "HEAD")
    old = version_of(clone)
    new = next_patch(old)

    out = release(clone, "patch", "--dry-run")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "--frozen" in out.stdout
    assert f"uv.lock: root version {old} -> {new}" in out.stdout
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "tag", "-l").strip() == ""


# --- an existing tag is refused before anything is written ---------------------


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("how", ["set", "bump"])
@needs_uv_git
def test_an_existing_tag_is_refused_and_the_tree_left_clean(clone: Path, how: str, dry: bool) -> None:
    new = next_patch(version_of(clone))
    git(clone, "tag", f"v{new}")
    before = snapshot(clone)
    head = git(clone, "rev-parse", "HEAD")

    out = release(clone, new if how == "set" else "patch", *(["--dry-run"] if dry else []))

    assert out.returncode != 0
    assert f"tag v{new} already exists" in out.stderr
    assert "nothing was written" in out.stderr
    # Only the dry probe ran: the version came from it, and no write followed.
    uv_calls = [line for line in out.stdout.splitlines() if line.startswith("  $ uv version")]
    assert len(uv_calls) == 1 and uv_calls[0].endswith("--frozen --dry-run"), out.stdout
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "tag", "-l").strip() == f"v{new}"


@needs_uv_git
def test_a_real_release_probes_then_writes_with_the_same_call(clone: Path) -> None:
    out = release(clone, "patch")
    assert out.returncode == 0, out.stdout + out.stderr
    uv_calls = [line for line in out.stdout.splitlines() if line.startswith("  $ uv version")]
    assert uv_calls == ["  $ uv version --bump patch --frozen --dry-run", "  $ uv version --bump patch --frozen"]


def test_a_real_bump_that_disagrees_with_its_dry_run_is_refused(tmp_path: Path, monkeypatch) -> None:
    rel = load_release()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.0.1"\n')
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "demo"\nversion = "0.0.1"\nsource = { editable = "." }\n')
    monkeypatch.setattr(sys, "argv", ["release.py", "patch"])
    monkeypatch.setattr(rel, "fragment_paths", list)
    monkeypatch.setattr(rel, "check_tracked", lambda fragments: None)

    def fake_run(args, **kw):
        if list(args[:2]) == ["uv", "version"]:
            new = "0.0.2" if "--dry-run" in args else "0.0.3"
            return subprocess.CompletedProcess(args, 0, stdout=f"demo 0.0.1 => {new}\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(rel.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="made 0.0.3, its dry run said 0.0.2"):
        rel.main()


# --- the root-line edit fails closed -------------------------------------------

ROOT_BLOCK = re.compile(
    r'^\[\[package\]\]\nname = "sessionphone"\n.*?(?=^\[\[package\]\]$|\Z)', re.MULTILINE | re.DOTALL
)


def no_root(lock: str) -> str:
    return ROOT_BLOCK.sub("", lock, count=1)


def two_roots(lock: str) -> str:
    block = ROOT_BLOCK.search(lock).group(0)
    return lock + "\n" + block


def two_versions(lock: str) -> str:
    return re.sub(
        r'^(name = "sessionphone"\nversion = "[^"]*"\n)', r'\1version = "0.0.0"\n', lock, count=1, flags=re.MULTILINE
    )


def not_the_root_source(lock: str) -> str:
    """The sessionphone block present, but sourced from an index: not the project."""
    return re.sub(
        r'^(name = "sessionphone"\nversion = "[^"]*"\n)source = \{ editable = "\." \}$',
        r'\1source = { registry = "https://pypi.org/simple" }',
        lock,
        count=1,
        flags=re.MULTILINE,
    )


@pytest.mark.parametrize(
    "mangle, says",
    [
        (no_root, "has 0 root [[package]] blocks"),
        (two_roots, "has 2 root [[package]] blocks"),
        (two_versions, "has 2 version lines"),
        (not_the_root_source, "has 0 root [[package]] blocks"),
    ],
)
@pytest.mark.parametrize("dry", [False, True])
@needs_uv_git
def test_a_bad_lock_is_refused_before_anything_is_written(clone: Path, mangle, says: str, dry: bool) -> None:
    lock = clone / "uv.lock"
    mangled = mangle(lock.read_text())
    assert mangled != lock.read_text()
    lock.write_text(mangled)
    git(clone, "commit", "-q", "-am", "test: a lock release.py must refuse")
    before = snapshot(clone)
    head = git(clone, "rev-parse", "HEAD")

    out = release(clone, "patch", *(["--dry-run"] if dry else []))

    assert out.returncode != 0
    assert says in out.stderr
    assert "nothing was written" in out.stderr
    assert "uv version" not in out.stdout  # refused BEFORE uv ran
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "tag", "-l").strip() == ""


def test_lock_root_finds_the_one_version_value() -> None:
    rel = load_release()
    lock = (ROOT / "uv.lock").read_text()
    start, end = rel.lock_root(lock, "sessionphone")
    assert lock[start:end] == version_of(ROOT)
    assert lock[:start].endswith('[[package]]\nname = "sessionphone"\nversion = "')


def test_lock_root_takes_a_virtual_root() -> None:
    rel = load_release()
    lock = 'version = 1\n\n[[package]]\nname = "demo"\nversion = "1.2.3"\nsource = { virtual = "." }\n'
    start, end = rel.lock_root(lock, "demo")
    assert lock[start:end] == "1.2.3"


def test_missing_lock_is_refused(tmp_path: Path, monkeypatch) -> None:
    rel = load_release()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["release.py", "patch"])
    real_run = subprocess.run
    monkeypatch.setattr(
        rel.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if list(args[:2]) in (["git", "status"], ["git", "ls-files"])
        else real_run(args, **kw),
    )
    with pytest.raises(SystemExit, match="uv.lock not found"):
        rel.main()


# --- make release ----------------------------------------------------------------


def release_recipe() -> list[str]:
    lines = (ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("release:"))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    return recipe


def test_make_release_recipe_has_no_relocking_uv_run() -> None:
    recipe = release_recipe()
    assert recipe, "Makefile has no release: recipe"
    for line in recipe:
        assert not re.search(r"\buv\s+run\b(?!.*--no-project)", line), line
    assert any("scripts/release.py" in line for line in recipe)


@pytest.mark.skipif(MAKE is None, reason="make is not installed")
def test_make_release_expands_to_python3() -> None:
    out = subprocess.run(
        [str(MAKE), "-C", str(ROOT), "--no-print-directory", "-n", "release", "VERSION=9.9.9"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "python3 scripts/release.py 9.9.9"


def test_the_fragment_readme_rehearses_without_uv_run() -> None:
    text = (ROOT / "changelog.d" / "README.md").read_text()
    assert "uv run python scripts/release.py" not in text
    assert "`python3 scripts/release.py patch --dry-run`" in text


# --- a python3 older than 3.11 (no tomllib) ------------------------------------
# make changelog runs `python3 scripts/release.py --preview`, and a stock macOS
# python3 is 3.9. Only the release reads pyproject with tomllib, so only the
# release may need 3.11+; it refuses before it writes. Simulated: tomllib is
# blocked in sys.modules, so `import tomllib` raises as it does on 3.9.

NO_TOMLLIB = (
    "import runpy, sys; sys.modules['tomllib'] = None; "
    "sys.argv = ['scripts/release.py', *sys.argv[1:]]; "
    "runpy.run_path('scripts/release.py', run_name='__main__')"
)


def test_preview_runs_without_tomllib() -> None:
    out = subprocess.run(
        [sys.executable, "-c", NO_TOMLLIB, "--preview"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("## [Unreleased]")


@needs_uv_git
def test_a_release_without_tomllib_is_refused_before_anything_is_written(clone: Path) -> None:
    before = snapshot(clone)
    head = git(clone, "rev-parse", "HEAD")

    out = run(clone, sys.executable, "-c", NO_TOMLLIB, "patch")

    assert out.returncode != 0
    assert "a release needs Python 3.11+ (tomllib)" in out.stderr
    assert "nothing was written" in out.stderr
    assert "uv version" not in out.stdout
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "rev-parse", "HEAD") == head

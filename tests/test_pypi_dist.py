"""`make pypi-dist`: the dists PyPI gets, with the README's links pinned to the tag.

README.md keeps RELATIVE links so a clone can follow them, and README.md is
also the PyPI page, where relative links 404. `make pypi-dist` builds from the
release tag (never the checkout), rewrites the staged README's relative links
to absolute URLs pinned to that tag, and checks both the wheel and the sdist
for any relative link left. `make publish` uploads only what it built. Before
any of that it runs the kit's `tag-check`: the local tag and the public one
must be the SAME commit, or the pinned links would name other files.

The rewriter is sessionkit's one scripts/pypi_readme.py. This repo carries no
copy of it: KIT_DIR names a sessionkit checkout, or PYPI_README the command.

Everything here is offline. The Makefile runs in a throwaway clone with a LOCAL
bare repo as PUBLIC_REMOTE. The end-to-end case swaps `uv build` for a stub
that writes a wheel and an sdist carrying the staged README, so no backend is
fetched; it needs a sessionkit checkout whose script has `tag-check` (KIT_DIR
in the environment, else ../sessionkit beside this repo), and skips without one.
Every other run uses a fake kit, so no test here reaches the network.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
GIT = shutil.which("git")
MAKE = shutil.which("make")
UV = shutil.which("uv")
# Split, so no shipped file spells this repo's public URL in full but the Makefile.
REPO = "sessionkit/session" + "phone"

needs_git_make = pytest.mark.skipif(GIT is None or MAKE is None, reason="git and make are needed")
needs_tools = pytest.mark.skipif(GIT is None or MAKE is None or UV is None,
                                 reason="git, make and uv are needed")


def kit_dir() -> Path | None:
    """A sessionkit checkout whose scripts/pypi_readme.py has `tag-check`, or None.

    A kit from before SK-289 has the script but not the verb pypi-dist now runs."""
    for cand in (os.environ.get("KIT_DIR"), ROOT.parent / "sessionkit"):
        script = Path(cand) / "scripts" / "pypi_readme.py" if cand else None
        if script and script.is_file() and "tag-check" in script.read_text(encoding="utf-8"):
            return Path(cand).resolve()
    return None


# --- the Makefile, as text ---------------------------------------------------


def test_publish_builds_through_pypi_dist() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^publish: pypi-dist\s+##", text, re.MULTILINE)
    assert re.search(r"^pypi-dist:.*## ", text, re.MULTILINE)  # in `make help`
    phony = re.search(r"^\.PHONY: (.*)$", text, re.MULTILINE)
    assert phony and "pypi-dist" in phony.group(1).split()


def test_the_rewriter_is_the_kits_one_copy() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^KIT_DIR \?= \.\./sessionkit$", text, re.MULTILINE)
    assert re.search(r"^PYPI_README \?= python3 \$\(KIT_DIR\)/scripts/pypi_readme\.py$", text, re.MULTILINE)
    assert re.search(rf"^PUBLIC_REMOTE \?= https://github\.com/{REPO}\.git$", text, re.MULTILINE)
    assert f"--repo {REPO} " in text
    # no fork: the script lives in the kit, never here
    copies = [p for d in ("scripts", "src", "bin") for p in (ROOT / d).rglob("pypi_readme*.py")]
    assert copies == [], f"a copy of the kit's pypi_readme.py is in this repo: {copies}"


def test_the_tag_check_is_the_kits_tag_check() -> None:
    """The public tag is checked by the kit's `tag-check` (same commit both sides),
    not by a bare `git ls-remote`, which only asked whether the tag existed."""
    text = MAKEFILE.read_text(encoding="utf-8")
    recipe = re.search(r"^pypi-dist:.*?(?=^\S)", text, re.MULTILINE | re.DOTALL).group(0)
    assert '$(PYPI_README) tag-check --tag "$$v" --remote "$(PUBLIC_REMOTE)"' in recipe
    assert "ls-remote" not in recipe
    # it runs before anything is staged or built
    assert recipe.index("tag-check") < recipe.index("git archive") < recipe.index(" rewrite ")


# --- the Makefile, run -------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run([str(GIT), "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                           *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, timeout=120).stdout


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, Path, str]:
    """A throwaway clone carrying this working tree's Makefile, a local tag for
    its version, and an EMPTY bare repo as the public one."""
    repo = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--no-hardlinks", str(ROOT), str(repo))
    shutil.copy2(MAKEFILE, repo / "Makefile")
    git(repo, "add", "Makefile")
    git(repo, "commit", "-q", "--allow-empty", "-m", "overlay: the code under test")
    version = re.search(r'^version = "([^"]+)"', (repo / "pyproject.toml").read_text(), re.MULTILINE).group(1)
    git(repo, "tag", "-f", f"v{version}")
    bare = tmp_path / "public.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    return repo, bare, version


# A stand-in for the kit's pypi_readme.py: it records each call's argv, one JSON
# line per call. `rewrite` and `check` do nothing more. `tag-check` keeps the
# kit's contract (SK-289): exit 0 only when the local tag and the public one,
# peeled, are the same commit; else one line on stderr and exit 1.
FAKE_KIT = """import json, subprocess, sys
args = sys.argv[1:]
with open({calls!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[0] != "tag-check":
    sys.exit(0)
tag, remote = args[args.index("--tag") + 1], args[args.index("--remote") + 1]
git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True)
here = git("rev-parse", "--verify", "--quiet", f"refs/tags/{{tag}}^{{{{commit}}}}").stdout.strip()
refs = dict(l.split("\\t")[::-1] for l in git("ls-remote", remote, f"refs/tags/{{tag}}",
                                               f"refs/tags/{{tag}}^{{{{}}}}").stdout.splitlines())
there = refs.get(f"refs/tags/{{tag}}^{{{{}}}}") or refs.get(f"refs/tags/{{tag}}")
if not here:
    sys.exit(print(f"fake tag-check: tag {{tag}} is not in this repo", file=sys.stderr) or 1)
if not there:
    sys.exit(print(f"fake tag-check: tag {{tag}} is not on {{remote}}", file=sys.stderr) or 1)
if here != there:
    sys.exit(print(f"fake tag-check: tag {{tag}} differs: local {{here}}, public {{there}}",
                   file=sys.stderr) or 1)
"""


@pytest.fixture
def fake_kit(tmp_path: Path) -> tuple[Path, Path]:
    """A kit folder whose pypi_readme.py records its calls (see FAKE_KIT)."""
    kit = tmp_path / "kit"
    (kit / "scripts").mkdir(parents=True)
    calls = tmp_path / "kit-calls.jsonl"
    (kit / "scripts" / "pypi_readme.py").write_text(FAKE_KIT.format(calls=str(calls)),
                                                    encoding="utf-8")
    return kit, calls


def calls(log: Path) -> list[list[str]]:
    """The fake kit's calls, in order: each one's argv after the script."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def verbs(log: Path) -> list[str]:
    return [c[0] for c in calls(log)]


def make(repo: Path, *args: str, path: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "UV_OFFLINE": "1"}
    env.pop("KIT_DIR", None)  # each test names its own
    if path is not None:
        env["PATH"] = f"{path}{os.pathsep}{env['PATH']}"
    return subprocess.run([str(MAKE), "-C", str(repo), "--no-print-directory", "pypi-dist", *args],
                          env=env, capture_output=True, text=True, timeout=300, check=False)


@needs_git_make
def test_pypi_dist_refuses_without_the_kits_script(clone, tmp_path: Path) -> None:
    repo, bare, version = clone
    git(repo, "push", "-q", str(bare), f"refs/tags/v{version}")  # the tag is not the reason
    missing = tmp_path / "no-kit-here"
    result = make(repo, f"KIT_DIR={missing}", f"PUBLIC_REMOTE={bare}")
    assert result.returncode != 0
    assert f"pypi-dist: no scripts/pypi_readme.py in KIT_DIR={missing}" in result.stderr
    assert "point KIT_DIR= at a sessionkit checkout" in result.stderr
    assert not (repo / "dist").exists()  # nothing built


@needs_tools
def test_pypi_dist_asks_the_kits_tag_check_first(clone, fake_kit) -> None:
    """The recipe hands tag-check the tag v<version> and PUBLIC_REMOTE, first."""
    repo, bare, version = clone
    kit, log = fake_kit
    git(repo, "push", "-q", str(bare), "HEAD:refs/heads/master")  # the branch, not the tag
    result = make(repo, f"KIT_DIR={kit}", f"PUBLIC_REMOTE={bare}")
    assert calls(log)[0] == ["tag-check", "--tag", f"v{version}", "--remote", str(bare)]
    assert result.returncode != 0
    # tag-check's own line is the error, passed through as it is
    assert f"fake tag-check: tag v{version} is not on {bare}" in result.stderr
    assert verbs(log) == ["tag-check"]  # refused before the rewrite
    assert not (repo / "dist").exists()  # nothing built


@needs_tools
def test_pypi_dist_refuses_a_public_tag_on_another_commit(clone, fake_kit) -> None:
    """The tag is on the public repo, but at a different commit: refused.

    The bare `git ls-remote` check this replaced let this through."""
    repo, bare, version = clone
    kit, log = fake_kit
    git(repo, "push", "-q", str(bare), f"HEAD~1:refs/tags/v{version}")
    here = git(repo, "rev-parse", f"v{version}^{{commit}}").strip()
    there = git(repo, "rev-parse", "HEAD~1").strip()
    assert here != there
    result = make(repo, f"KIT_DIR={kit}", f"PUBLIC_REMOTE={bare}")
    assert result.returncode != 0
    assert f"tag v{version} differs: local {here}, public {there}" in result.stderr
    assert verbs(log) == ["tag-check"]  # refused before the rewrite
    assert not (repo / "dist").exists()  # nothing built


@needs_tools
def test_pypi_dist_takes_a_named_rewriter_without_the_kit(clone, fake_kit, tmp_path: Path) -> None:
    """PYPI_README set by hand: KIT_DIR is not looked at, and the tag check still runs."""
    repo, bare, version = clone
    kit, log = fake_kit
    result = make(repo, f"KIT_DIR={tmp_path / 'no-kit-here'}",
                  f"PYPI_README=python3 {kit / 'scripts' / 'pypi_readme.py'}", f"PUBLIC_REMOTE={bare}")
    assert result.returncode != 0
    assert "no scripts/pypi_readme.py" not in result.stderr
    assert verbs(log) == ["tag-check"]
    assert f"fake tag-check: tag v{version} is not on {bare}" in result.stderr


# A stand-in for `uv build`: writes a wheel and an sdist whose long description
# is the README in the folder it runs in, as a real backend would. `uv version`
# goes to the real uv, which only reads pyproject.toml.
STUB_UV = """#!{python}
import io, os, sys, tarfile, tomllib, zipfile
args = sys.argv[1:]
if args[:1] == ["version"]:
    os.execv({uv!r}, [{uv!r}, *args])
assert args[0] == "build" and args[1] == "--out-dir", args
out = args[2]; os.makedirs(out, exist_ok=True)
v = tomllib.load(open("pyproject.toml", "rb"))["project"]["version"]
meta = "Metadata-Version: 2.4\\nName: sessionphone\\nVersion: " + v + "\\n\\n" + open("README.md").read()
with zipfile.ZipFile(os.path.join(out, f"sessionphone-{{v}}-py3-none-any.whl"), "w") as z:
    z.writestr(f"sessionphone-{{v}}.dist-info/METADATA", meta)
with tarfile.open(os.path.join(out, f"sessionphone-{{v}}.tar.gz"), "w:gz") as t:
    data = meta.encode(); info = tarfile.TarInfo(f"sessionphone-{{v}}/PKG-INFO"); info.size = len(data)
    t.addfile(info, io.BytesIO(data))
print("stub uv build:", os.getcwd())
"""


@pytest.fixture
def stub_uv(tmp_path: Path) -> Path:
    """A folder holding the STUB_UV `uv`, to put first on PATH."""
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "uv").write_text(STUB_UV.format(python=sys.executable, uv=UV), encoding="utf-8")
    (stub / "uv").chmod(0o755)
    return stub


@needs_tools
@pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
def test_pypi_dist_proceeds_when_the_public_tag_is_the_same_commit(
        clone, fake_kit, stub_uv, annotated: bool) -> None:
    """The same commit on both sides: tag-check passes, then rewrite, build, check."""
    repo, bare, version = clone
    kit, log = fake_kit
    if annotated:  # the public side is peeled, so an annotated tag matches too
        git(repo, "tag", "-f", "-a", "-m", "release", f"v{version}", "HEAD")
    git(repo, "push", "-q", str(bare), f"refs/tags/v{version}")
    result = make(repo, f"KIT_DIR={kit}", f"PUBLIC_REMOTE={bare}", path=stub_uv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert verbs(log) == ["tag-check", "rewrite", "check"]
    assert (repo / "dist" / f"sessionphone-{version}-py3-none-any.whl").exists()


@needs_tools
def test_the_kits_tag_check_refuses_a_public_tag_on_another_commit(clone, tmp_path: Path) -> None:
    """The same refusal through the kit's real script, where a checkout has it."""
    kit = kit_dir()
    if kit is None:
        pytest.skip("no sessionkit checkout whose pypi_readme.py has tag-check (set KIT_DIR)")
    repo, bare, version = clone
    git(repo, "push", "-q", str(bare), f"HEAD~1:refs/tags/v{version}")
    here = git(repo, "rev-parse", f"v{version}^{{commit}}").strip()
    there = git(repo, "rev-parse", "HEAD~1").strip()
    result = make(repo, f"KIT_DIR={kit}", f"PUBLIC_REMOTE={bare}")
    assert result.returncode != 0
    assert f"tag v{version} differs: local {here}, public {there}" in result.stderr
    assert not (repo / "dist").exists()  # nothing built


@needs_tools
def test_pypi_dist_builds_from_the_tag_and_checks_both_dists(clone, stub_uv) -> None:
    kit = kit_dir()
    if kit is None:
        pytest.skip("no sessionkit checkout whose pypi_readme.py has tag-check (set KIT_DIR)")
    repo, bare, version = clone
    git(repo, "push", "-q", str(bare), f"refs/tags/v{version}")
    stub = stub_uv
    # An uncommitted edit in the checkout must not reach the dists.
    readme = repo / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nCHECKOUT-ONLY [x](nope.md)\n",
                      encoding="utf-8")
    before = readme.read_bytes()
    status = git(repo, "status", "--porcelain")

    result = make(repo, f"KIT_DIR={kit}", f"PUBLIC_REMOTE={bare}", path=stub)

    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"[1-9][0-9]* link\(s\) rewritten", result.stdout + result.stderr)
    assert result.stdout.count(": 0 relative link(s)") == 2  # the wheel and the sdist
    assert readme.read_bytes() == before  # the checkout's README is never rewritten
    assert git(repo, "status", "--porcelain") == status  # dist/ is ignored; nothing else moved
    whl = repo / "dist" / f"sessionphone-{version}-py3-none-any.whl"
    with zipfile.ZipFile(whl) as z:
        body = z.read(f"sessionphone-{version}.dist-info/METADATA").decode()
    blob = f"https://github.com/{REPO}/blob/v{version}"
    assert "CHECKOUT-ONLY" not in body  # built from the tag
    assert f"]({blob}/docs/configuration.md)" in body
    assert f"[mini-apps]: {blob}/docs/mini-apps.md\n" in body  # a reference definition too
    assert "](docs/" not in body and "]: docs/" not in body  # no relative docs link left
    assert (repo / "dist" / f"sessionphone-{version}.tar.gz").exists()

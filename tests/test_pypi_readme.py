"""The README is the PyPI description.

pyproject's [project] names `readme = "README.md"`, so the wheel and the sdist
carry it as the long description (content type text/markdown, inferred from
the .md suffix). Without the field the PyPI page has no description and
`twine check --strict` fails on both artifacts. Published metadata can never be
changed, so a release without it costs a new version.

The README also opens where the repo is: in an editor, offline, from a clone.
So a link into this repo is relative, and every other link is an in-page
anchor or an absolute URL to another site or repo. A link to another
sessionkit repo names `HEAD` or a `v*` tag, never a branch. test_public_links.py checks
that each relative link resolves in the export, and that no shipped file links
this repo by a full URL. PyPI's renderer keeps in-page anchors working: it
gives each heading a `user-content-` id and rewrites `#x` links to match.

Known gap: PyPI renders the README away from the repository, so a relative
link does not work on the PyPI page. That gap is carried by the release
(SK-88), not worked around here.

The artifacts are built once per module with uv; nothing is uploaded.
"""
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
# A link into another sessionkit repo, and the ref it names.
SESSIONKIT_REF = re.compile(r"https?://github\.com/sessionkit/[\w.-]+/(?:blob|tree|raw)/([\w.-]+)")

UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(UV is None, reason="uv is not on PATH (make test runs under uv)")


def prose(text):
    """The README with fenced code blocks and inline code spans removed."""
    out, fence = [], None
    for line in text.splitlines():
        m = re.match(r"\s*(`{3,}|~{3,})", line)
        if m:
            if fence is None:
                fence = m.group(1)[0] * len(m.group(1))
            elif m.group(1).startswith(fence):
                fence = None
            continue
        if fence is None:
            out.append(re.sub(r"(`+).*?\1", "", line))
    return "\n".join(out)


def link_targets(text):
    """Every markdown link/image target and HTML href/src in the prose."""
    body = prose(text)
    targets = re.findall(r"\]\(\s*<?([^)\s>]+)", body)
    targets += re.findall(r"^\s*\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", body, re.MULTILINE)
    targets += re.findall(r"""(?:href|src)\s*=\s*["']([^"']+)""", body, re.IGNORECASE)
    return targets


def test_pyproject_names_the_readme():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project.get("readme") == "README.md"
    assert README.is_file()


# A relative README link does not work on the PyPI page: the module docstring's
# known gap (SK-88).
def test_readme_links_are_anchors_absolute_or_into_this_repo():
    targets = link_targets(README.read_text(encoding="utf-8"))
    assert targets, "no links found: the link scan is broken"
    other = [t for t in targets if re.match(r"[a-z][a-z0-9+.-]*:", t)
             and not re.match(r"(https://|http://|mailto:)", t)]
    assert other == [], f"links that are neither absolute, an anchor nor relative: {other}"
    relative = [t for t in targets if not re.match(r"#|[a-z][a-z0-9+.-]*:", t)]
    assert relative, "no relative link found: the README links its docs relatively"
    on_branch = [t for t in targets
                 if (m := SESSIONKIT_REF.match(t)) and not re.fullmatch(r"HEAD|v[0-9][\w.-]*", m[1])]
    assert on_branch == [], f"sessionkit links on a branch, not HEAD or a v* tag: {on_branch}"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("see [a](#x) and [b](https://e.example/y)", ["#x", "https://e.example/y"]),
        ("![pic](docs/a.png)", ["docs/a.png"]),
        ("[ref]: ./LICENSE", ["./LICENSE"]),
        ('<img src="img/x.png">', ["img/x.png"]),
        ("`[not](a/link)` and [yes](b.md)", ["b.md"]),
        ("```\n[not](a/link)\n```\n[yes](c.md)", ["c.md"]),
    ],
)
def test_link_scan_finds_relative_targets(text, expected):
    assert link_targets(text) == expected


def uv(*args):
    r = subprocess.run([UV, *args], cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)
    assert r.returncode == 0, f"uv {' '.join(args)}\n{r.stdout}\n{r.stderr}"


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    d = tmp_path_factory.mktemp("dist")
    uv("build", "--out-dir", str(d))
    sdist, = d.glob("*.tar.gz")
    wheel, = d.glob("*.whl")
    return {"sdist": sdist, "wheel": wheel}


@needs_uv
def test_wheel_metadata_carries_the_readme(dist):
    with zipfile.ZipFile(dist["wheel"]) as z:
        name, = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        meta = Parser().parsestr(z.read(name).decode("utf-8"))
    assert meta["Description-Content-Type"] == "text/markdown"
    assert meta.get_payload().strip() == README.read_text(encoding="utf-8").strip()


@needs_uv
def test_sdist_ships_the_readme(dist):
    with tarfile.open(dist["sdist"]) as t:
        top = t.getnames()[0].split("/")[0]
        readme = t.extractfile(f"{top}/README.md").read().decode("utf-8")
        meta = Parser().parsestr(t.extractfile(f"{top}/PKG-INFO").read().decode("utf-8"))
    assert readme == README.read_text(encoding="utf-8")
    assert meta["Description-Content-Type"] == "text/markdown"
    assert meta.get_payload().strip() == readme.strip()

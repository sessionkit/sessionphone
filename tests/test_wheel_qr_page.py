"""A wheel install prints the same connect page as a checkout.

The page (`src/sessionphone/pages/qr-page.html`) is package data, read with
importlib.resources, so it travels in the wheel. A wheel is what `pip install`,
`uv tool install` and a kit install all put on disk. These tests build the
wheel the way a release does, from the tree and from the sdist. They check it
holds the page byte for byte, install it into a fresh venv, and have that
venv's interpreter print the page. The result must be the same bytes the
checkout prints.

Fakes only: a made-up invite link, no Delta Chat account. uv builds the wheel
once per module and installs it `--offline` from its cache (`uv sync` filled it).
"""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from sessionphone import account

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "src" / "sessionphone" / "pages" / "qr-page.html"
MEMBER = "sessionphone/pages/qr-page.html"
LINK = ("https://i.delta.chat/#0123456789ABCDEF0123456789ABCDEF01234567&v=3"
        "&i=AbCdEfGhIjKlMnOpQrStUvWx&s=ZyXwVuTsRqPoNmLkJiHgFeDc"
        "&a=bot%40relay.example&n=Test+Agent")

UV = shutil.which("uv")
pytestmark = pytest.mark.skipif(UV is None, reason="uv is not on PATH (make test runs under uv)")


def uv(*args, cwd=ROOT):
    r = subprocess.run([UV, *args], cwd=cwd, capture_output=True, text=True, timeout=300, check=False)
    assert r.returncode == 0, f"uv {' '.join(args)}\n{r.stdout}\n{r.stderr}"
    return r


@pytest.fixture(scope="module")
def wheels(tmp_path_factory):
    """{"direct": wheel from the tree, "sdist": wheel built FROM the sdist}."""
    d = tmp_path_factory.mktemp("dist")
    uv("build", "--sdist", "--out-dir", str(d / "sdist"))
    uv("build", "--wheel", "--out-dir", str(d / "direct"))
    sdist, = (d / "sdist").glob("*.tar.gz")
    uv("build", "--wheel", "--out-dir", str(d / "from-sdist"), str(sdist))
    direct, = (d / "direct").glob("*.whl")
    from_sdist, = (d / "from-sdist").glob("*.whl")
    return {"direct": direct, "sdist": from_sdist}


@pytest.fixture(scope="module")
def venv_python(wheels, tmp_path_factory):
    """The direct wheel, with its dependencies, in a fresh venv: an install."""
    venv = tmp_path_factory.mktemp("install") / "venv"
    uv("venv", "--quiet", str(venv))
    python = venv / "bin" / "python"
    uv("pip", "install", "--quiet", "--offline", "--python", str(python), str(wheels["direct"]))
    return python


def run_installed(python, code):
    """CODE in the installed venv: isolated (-I), from a directory outside the
    tree, so `sessionphone` can only come from the venv's site-packages."""
    r = subprocess.run([str(python), "-I", "-c", code], cwd=python.parent, capture_output=True,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode()
    return r.stdout


@pytest.mark.parametrize("how", ["direct", "sdist"])
def test_the_wheel_ships_the_page_byte_for_byte(wheels, how):
    with zipfile.ZipFile(wheels[how]) as z:
        assert z.read(MEMBER) == PAGE.read_bytes(), f"the {how} wheel's page differs from src/"
        assert not [n for n in z.namelist() if n.endswith("qr-page.html") and n != MEMBER]


def test_the_installed_package_is_not_the_checkout(venv_python):
    where = run_installed(venv_python, "import sessionphone; print(sessionphone.__file__)").decode().strip()
    assert Path(where).is_relative_to(venv_python.parent.parent), where
    assert not Path(where).is_relative_to(ROOT), where


def test_the_installed_package_reads_the_same_page(venv_python):
    got = run_installed(venv_python, "import sys; from sessionphone import account; "
                                     "sys.stdout.buffer.write(account.default_page().encode())")
    assert got == PAGE.read_bytes()


def test_the_installed_package_prints_the_same_connect_page(venv_python):
    """THE BUG: a wheel had no page of its own path to find, so it printed a
    second, hand-kept page that had drifted from the first."""
    got = run_installed(venv_python, "import sys; from sessionphone import account; "
                                     f"sys.stdout.buffer.write(account.qr_page('Test Agent', link={LINK!r}).encode())")
    assert got == account.qr_page("Test Agent", link=LINK).encode()

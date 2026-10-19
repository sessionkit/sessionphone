"""No `.python-version` ships, so first contact uses a Python the user has (PHONE-146).

The repo shipped a `.python-version` pinning 3.13, a scaffold default. uv
honours that file over `requires-python`, so on a Mac with Homebrew 3.14 and
no 3.13 the first `uv sync` fetched CPython 3.13 (25.3 MB), and with
downloads off it exited 2 ("No interpreter found for Python 3.13"). Without
the file, uv picks an installed Python inside `requires-python`.

The floor, `>=3.11`, is measured: the suite passed on 3.11, 3.12, 3.13 and
3.14 (PHONE-146 findings/python-floor.md). The ceiling, `<3.15`, keeps an
install off Python 3.15, which is not yet verified (PHONE-191). Moving either
end means measuring again and updating the docs that name it, so the test
below names it too.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_no_python_version_file_ships():
    pin = ROOT / ".python-version"
    assert not pin.exists(), (
        f"{pin} pins {pin.read_text(encoding='utf-8').strip()!r}: uv would use that "
        "over requires-python and fetch it on a Mac that already has a Python in range. "
        "Remove it (PHONE-146)."
    )


def test_requires_python_is_the_measured_range():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.11,<3.15"


def test_docs_no_longer_say_a_mac_without_313_fetches_python():
    for rel in ("README.md", "skills/sessionphone/SKILL.md", "CHANGELOG.md"):
        text = _flat((ROOT / rel).read_text(encoding="utf-8"))
        assert "no Python 3.13" not in text, f"{rel} still says a Mac with no Python 3.13 fetches Python"
        assert "Python 3.13 (about 25 MB)" not in text, rel


def test_docs_say_python_is_fetched_only_below_the_floor():
    readme = _flat((ROOT / "README.md").read_text(encoding="utf-8"))
    skill = _flat((ROOT / "skills" / "sessionphone" / "SKILL.md").read_text(encoding="utf-8"))
    assert "uv uses a Python already on the Mac when it is 3.11 to 3.14" in readme
    assert "25.3 MB, measured for CPython 3.13" in readme
    assert "uv uses a Python already on the Mac when it is 3.11 to 3.14" in skill

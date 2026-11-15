"""The launchd verbs on a host without launchd.

`bot install`, `bot uninstall` and `bot status` drive this user's launchd
job, so they need launchd: macOS, with `launchctl` where the job runner
looks for it (PATH). On any other host each refuses at its top with one
plain line, exit 2: it is macOS only, and the bot runs in the foreground
with `sessionphone bot run`. No traceback, no `[Errno 2]`, and install
writes nothing: the refusal comes before the media pack check and before
every write. `bot run` needs no launchd and is never refused.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest
from click.testing import CliRunner

from sessionphone import cli as cli_mod
from sessionphone import media, service

# the real check, kept at import: conftest's autouse has_launchd replaces it per test
REAL_LAUNCHD = service.launchd

LAUNCHD_VERBS = [["bot", "install"], ["bot", "install", "--owner", "a@b.example"],
                 ["bot", "uninstall"], ["bot", "status"], ["bot"]]


def _tree(root: pathlib.Path) -> dict[str, int]:
    """Every file under root, with its size: what a refusal must leave as found."""
    return {str(p.relative_to(root)): p.stat().st_size for p in sorted(root.rglob("*")) if p.is_file()}


def _no_launchctl(*args):
    raise AssertionError(f"launchctl was run on a host without launchd: {args}")


def _no_install(**kw):
    raise AssertionError("service.install ran on a host without launchd")


@pytest.fixture
def linux(monkeypatch, tmp_path):
    """A Linux host: sys.platform is linux and no launchctl is on PATH. Any
    launchctl call, install step or media check fails the test loudly."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setattr(service, "launchd", REAL_LAUNCHD)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(service, "_launchctl", _no_launchctl)
    monkeypatch.setattr(service, "install", _no_install)
    monkeypatch.setattr(media, "missing", lambda: list(media.MODULES))   # the pack check would refuse first
    return tmp_path


@pytest.mark.parametrize("argv", LAUNCHD_VERBS, ids=" ".join)
def test_a_launchd_verb_on_linux_is_one_plain_line_and_exit_2(linux, argv):
    home = pathlib.Path(os.environ["HOME"])
    before = _tree(home)
    r = CliRunner().invoke(cli_mod.main, argv)
    assert r.exit_code == 2, r.output
    verb = " ".join(argv[:2]) if argv != ["bot"] else "bot status"     # bare `bot` is `bot status`
    assert r.stdout == "", "a refusal says nothing on stdout"
    assert r.stderr == (f"sessionphone {verb}: macOS only (launchd). "
                        "On Linux, run the bot with `sessionphone bot run`.\n")
    for word in ("Traceback", "Errno", "media pack"):
        assert word not in r.output
    assert _tree(home) == before, "a refusal on a host without launchd wrote a file"


def test_bot_run_is_not_refused_on_linux(linux, monkeypatch):
    """bot run needs no launchd: it goes on to its own checks (here the media
    pack, which the fixture reports missing), never to the launchd refusal."""
    r = CliRunner().invoke(cli_mod.main, ["bot", "run"])
    assert "macOS only" not in r.output
    assert "media pack" in r.output


def test_launchd_needs_macos_and_launchctl_on_path(monkeypatch, tmp_path):
    has = tmp_path / "has"
    has.mkdir()
    tool = has / "launchctl"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    none = tmp_path / "none"
    none.mkdir()
    for platform, path, want in (("darwin", has, True), ("darwin", none, False),
                                 ("linux", has, False), ("linux", none, False)):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setenv("PATH", str(path))
        assert REAL_LAUNCHD() is want, (platform, path.name)


def test_a_mac_with_launchctl_off_path_says_so(monkeypatch, tmp_path):
    """macOS has launchd, but a PATH without /bin hides launchctl: the line says
    that, not "macOS only", and still points at `bot run`."""
    monkeypatch.setattr(service, "launchd", REAL_LAUNCHD)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(service, "_launchctl", _no_launchctl)
    r = CliRunner().invoke(cli_mod.main, ["bot", "status"])
    assert r.exit_code == 2, r.output
    assert r.stderr == ("sessionphone bot status: launchctl is not on PATH (macOS keeps it in /bin). "
                        "Put /bin on PATH, or run the bot with `sessionphone bot run`.\n")
    assert "Traceback" not in r.output


def test_macos_with_launchctl_passes_the_check(monkeypatch, launchd, tmp_path):
    """A Mac with launchctl on PATH: the real check passes, and status goes on to launchd."""
    tool = tmp_path / "launchctl"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setattr(service, "launchd", REAL_LAUNCHD)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    r = CliRunner().invoke(cli_mod.main, ["bot", "status"])
    assert "macOS only" not in r.output
    assert launchd.verbs(), "status never asked launchd"

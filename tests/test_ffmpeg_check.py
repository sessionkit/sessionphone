"""bot install on a host with no ffmpeg (PHONE-230).

The bot turns each spoken reply into call audio with ffmpeg
(bridge/calls.py: FFMPEG, the TTS decode; firstcall.py: ffmpeg_decode). With
no ffmpeg, every spoken reply fails and goes to the caller as text. A fresh
install printed `hearing:   yes` and nothing about ffmpeg, so the first call
was the first sign. The install's report must name ffmpeg, say that spoken
replies fall back to text, and give the one fix, shown as a command on a
line of its own.

"No ffmpeg" here means every place the bot looks: no DC_FFMPEG (or its
deprecated alias), none on PATH, and neither Homebrew path
(/opt/homebrew/bin/ffmpeg, /usr/local/bin/ffmpeg). The `no_ffmpeg` fixture
hides ffmpeg from each lookup a check could make (shutil.which, os.path,
os.access, pathlib, a PATH directory holding one), and resets the bot's own
module-level FFMPEG to the bare name a host without ffmpeg resolves to. Tests
use fakes: no real launchd job, STT server or ffmpeg is run.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat

import pytest
from test_hearing import FakeCurl, _flat, _install, rig  # noqa: F401 - rig is a fixture

from sessionphone import hearing

FIX = "brew install ffmpeg"     # the one fix: Homebrew's ffmpeg


def _is_ffmpeg(p) -> bool:
    return os.path.basename(os.fspath(p)) == "ffmpeg"


@pytest.fixture
def no_ffmpeg(monkeypatch):
    """A host with no ffmpeg anywhere the bot looks (see the module doc)."""
    for k in ("DC_FFMPEG", "PIP_CHAT_FFMPEG", "FFMPEG"):
        monkeypatch.delenv(k, raising=False)
    path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", os.pathsep.join(
        d for d in path.split(os.pathsep) if d and not os.path.exists(os.path.join(d, "ffmpeg"))))

    which, exists, isfile, access = shutil.which, os.path.exists, os.path.isfile, os.access
    p_exists, p_is_file = pathlib.Path.exists, pathlib.Path.is_file
    monkeypatch.setattr(shutil, "which",
                        lambda cmd, *a, **k: None if _is_ffmpeg(cmd) else which(cmd, *a, **k))
    monkeypatch.setattr(os.path, "exists", lambda p: False if _is_ffmpeg(p) else exists(p))
    monkeypatch.setattr(os.path, "isfile", lambda p: False if _is_ffmpeg(p) else isfile(p))
    monkeypatch.setattr(os, "access",
                        lambda p, *a, **k: False if _is_ffmpeg(p) else access(p, *a, **k))
    monkeypatch.setattr(pathlib.Path, "exists",
                        lambda self, *a, **k: False if _is_ffmpeg(self) else p_exists(self, *a, **k))
    monkeypatch.setattr(pathlib.Path, "is_file",
                        lambda self, *a, **k: False if _is_ffmpeg(self) else p_is_file(self, *a, **k))
    try:   # the bot's own lookup ran at import, on this host: put it where no ffmpeg leaves it
        from sessionphone.bridge import calls
    except Exception:  # noqa: BLE001 - no media pack: nothing imported it, nothing to reset
        return
    monkeypatch.setattr(calls, "FFMPEG", "ffmpeg")


@pytest.fixture
def an_ffmpeg(monkeypatch, tmp_path):
    """A host whose ffmpeg is the one DC_FFMPEG names (a stand-in, never run)."""
    f = tmp_path / "bin" / "ffmpeg"
    f.parent.mkdir()
    f.write_text("#!/bin/sh\nexit 0\n")
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("DC_FFMPEG", str(f))
    try:
        from sessionphone.bridge import calls
    except Exception:  # noqa: BLE001
        return f
    monkeypatch.setattr(calls, "FFMPEG", str(f))
    return f


def _report(output: str) -> list[str]:
    """What bot install reported once the job was in: from `installed:` on."""
    lines = output.splitlines()
    return lines[next(i for i, ln in enumerate(lines) if ln.startswith("installed:")):]


def _about_ffmpeg(report: list[str]) -> str:
    """The flattened words around the report's first mention of ffmpeg."""
    text = _flat(report)
    m = re.search(r"\bffmpeg\b", text)
    assert m, "the install's report never names ffmpeg:\n" + "\n".join(report)
    return text[max(0, m.start() - 300):m.end() + 400]


@pytest.mark.usefixtures("rig")
def test_hearing_yes_with_no_ffmpeg_names_ffmpeg_text_replies_and_the_fix(monkeypatch, no_ffmpeg):
    """The reported case: the STT answers, so hearing is yes, and there is no
    ffmpeg. The report names ffmpeg, says spoken replies go as text, and shows
    the fix as a command of its own."""
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    report = _report(r.output)
    assert any(ln.startswith("hearing:   yes.") for ln in report), r.output   # the STT did answer
    near = _about_ffmpeg(report)
    assert re.search(r"\bspoken\b|\bspeak", near, re.IGNORECASE), near
    assert re.search(r"\btext\b", near), near
    assert any(ln.strip() == FIX for ln in report), r.output


@pytest.mark.usefixtures("rig")
def test_no_ffmpeg_is_said_when_hearing_is_no_too(monkeypatch, no_ffmpeg):
    """No STT and no ffmpeg: the ffmpeg warning is not lost behind the NO."""
    r, _ = _install(monkeypatch, FakeCurl(rc=7))
    assert r.exit_code == 0, r.output
    report = _report(r.output)
    near = _about_ffmpeg(report)
    assert re.search(r"\btext\b", near), near
    assert any(ln.strip() == FIX for ln in report), r.output


@pytest.mark.usefixtures("rig")
def test_an_ffmpeg_on_the_host_raises_no_warning(monkeypatch, an_ffmpeg):
    """The control: with ffmpeg found, the report says nothing about it."""
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    report = _report(r.output)
    assert not any(ln.strip() == FIX for ln in report), r.output
    assert "fall back to text" not in _flat(report) and "as text" not in _flat(report)
    assert hearing.HEAD.strip() in r.output


def test_the_fixture_hides_ffmpeg_from_every_lookup(no_ffmpeg):
    """The rig itself: a check that passes under no_ffmpeg found no ffmpeg."""
    assert shutil.which("ffmpeg") is None
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        assert not os.path.exists(p) and not os.path.isfile(p) and not os.access(p, os.X_OK)
        assert not pathlib.Path(p).exists() and not pathlib.Path(p).is_file()
    assert not any(os.path.basename(d) and os.listdir(d).count("ffmpeg")
                   for d in os.environ["PATH"].split(os.pathsep) if os.path.isdir(d))
    assert not any(os.environ.get(k) for k in ("DC_FFMPEG", "PIP_CHAT_FFMPEG", "FFMPEG"))
    from sessionphone.bridge import calls
    assert calls.FFMPEG == "ffmpeg"

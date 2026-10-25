"""The `speaking:` line: can the bot speak its replies? (PHONE-230)

On the speech-server route the bot turns each reply into call audio with
ffmpeg. hearing.ffmpeg_for looks where the bot looks (bridge/calls.py
FFMPEG), on the JOB's environment: DC_FFMPEG, else the job's PATH, else the
Homebrew paths. `bot install` and `bot status` both say it. The repro
(tests/test_ffmpeg_check.py) pins the install's NO; these pin the rest.
Tests use fakes: no real launchd job, speech server or ffmpeg is run.
"""
from __future__ import annotations

import os
import plistlib
import stat

import pytest
from click.testing import CliRunner
from test_ffmpeg_check import (  # noqa: F401 - fixtures
    FIX,
    _report,
    an_ffmpeg,
    no_ffmpeg,
)
from test_hearing import FakeCurl, _flat, _install, rig  # noqa: F401 - rig is a fixture

from sessionphone import hearing, service
from sessionphone.cli import main as cli


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _speaking(output: str) -> list[str]:
    lines = output.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("speaking:"))
    j = next((k for k in range(i + 1, len(lines)) if not lines[k].startswith(" ")), len(lines))
    return lines[i:j]


# ---- ffmpeg_for: where the bot looks, on the job's environment ------------

def test_dc_ffmpeg_wins_over_the_path(tmp_path):
    set_ = _exe(tmp_path / "set" / "ffmpeg")
    on_path = _exe(tmp_path / "bin" / "ffmpeg")
    ff = hearing.ffmpeg_for({"DC_FFMPEG": str(set_), "PATH": str(on_path.parent)})
    assert (ff.path, ff.setting, ff.ok) == (str(set_), "DC_FFMPEG", True)


def test_a_dc_ffmpeg_that_names_nothing_is_not_ok(tmp_path):
    """The bot runs what DC_FFMPEG names, even when an ffmpeg is on the PATH."""
    on_path = _exe(tmp_path / "bin" / "ffmpeg")
    ff = hearing.ffmpeg_for({"DC_FFMPEG": str(tmp_path / "gone"), "PATH": str(on_path.parent)})
    assert (ff.path, ff.setting, ff.ok) == (str(tmp_path / "gone"), "DC_FFMPEG", False)


def test_a_dc_ffmpeg_that_cannot_run_is_not_ok(tmp_path):
    f = tmp_path / "ffmpeg"
    f.write_text("not a program")
    f.chmod(0o600)
    assert not hearing.ffmpeg_for({"DC_FFMPEG": str(f)}).ok


def test_a_bare_name_in_dc_ffmpeg_is_found_on_the_jobs_path(tmp_path, monkeypatch):
    """DC_FFMPEG=ffmpeg: the bot's subprocess looks the name up on its own
    PATH, so the check does too, on the job's PATH and not the shell's."""
    job_bin = _exe(tmp_path / "job" / "ffmpeg").parent
    monkeypatch.setenv("PATH", str(_exe(tmp_path / "shell" / "ffmpeg").parent))
    ff = hearing.ffmpeg_for({"DC_FFMPEG": "ffmpeg", "PATH": str(job_bin)})
    assert (ff.path, ff.setting, ff.ok) == (str(job_bin / "ffmpeg"), "DC_FFMPEG", True)
    assert _flat(hearing.speaking_lines({"DC_FFMPEG": "ffmpeg", "PATH": str(job_bin)})).startswith(
        f"speaking: yes. ffmpeg at {job_bin / 'ffmpeg'} (DC_FFMPEG)")


def test_a_bare_name_in_dc_ffmpeg_off_the_jobs_path_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(_exe(tmp_path / "shell" / "ffmpeg").parent))
    env = {"DC_FFMPEG": "ffmpeg", "PATH": str(tmp_path / "empty")}
    ff = hearing.ffmpeg_for(env)
    assert (ff.path, ff.setting, ff.ok) == ("ffmpeg", "DC_FFMPEG", False)
    text = _flat(hearing.speaking_lines(env))
    assert text.startswith(f"speaking: NO. DC_FFMPEG is ffmpeg, which is not on the job's PATH "
                           f"({tmp_path / 'empty'})")
    assert "chat text" in text


def test_the_deprecated_alias_is_read_as_the_bot_reads_it(tmp_path):
    set_ = _exe(tmp_path / "ffmpeg")
    ff = hearing.ffmpeg_for({"PIP_CHAT_FFMPEG": str(set_)})
    assert (ff.path, ff.setting, ff.ok) == (str(set_), "PIP_CHAT_FFMPEG", True)


def test_found_on_the_jobs_path_not_the_shells(tmp_path, monkeypatch):
    """An ffmpeg on the job's PATH is found; one only on this shell's PATH is
    not: the bot runs under launchd, with the job's PATH."""
    job_bin = _exe(tmp_path / "job" / "ffmpeg").parent
    shell_bin = _exe(tmp_path / "shell" / "ffmpeg").parent
    monkeypatch.setenv("PATH", str(shell_bin))
    monkeypatch.setattr(hearing.os.path, "exists",
                        lambda p, real=os.path.exists: p not in hearing.FFMPEG_HOMEBREW and real(p))
    ff = hearing.ffmpeg_for({"PATH": str(job_bin)})
    assert (ff.path, ff.ok, ff.came) == (str(job_bin / "ffmpeg"), True, "on the job's PATH")
    ff = hearing.ffmpeg_for({"PATH": str(tmp_path / "empty")})
    assert (ff.path, ff.ok) == (None, False)


def test_a_homebrew_path_is_found_off_the_jobs_path(monkeypatch, tmp_path):
    """A hand-edited plist PATH with no Homebrew: the bot still finds
    /usr/local/bin/ffmpeg (calls.py's fallback), and so does the check."""
    monkeypatch.setattr(hearing.os.path, "exists", lambda p: p == "/usr/local/bin/ffmpeg")
    ff = hearing.ffmpeg_for({"PATH": str(tmp_path)})
    assert (ff.path, ff.ok, ff.came) == ("/usr/local/bin/ffmpeg", True, "the Homebrew path")


def test_a_job_with_no_path_is_looked_up_on_launchds(monkeypatch):
    seen = {}

    def which(cmd, path=None):
        seen["path"] = path
    monkeypatch.setattr(hearing.shutil, "which", which)
    monkeypatch.setattr(hearing.os.path, "exists", lambda p: False)
    ff = hearing.ffmpeg_for({})
    assert seen["path"] == hearing.LAUNCHD_PATH and not ff.ok and ff.job_path == hearing.LAUNCHD_PATH


# ---- speaking_lines: what is said ------------------------------------------

def test_no_ffmpeg_says_no_names_it_text_and_the_fix(monkeypatch, tmp_path):
    monkeypatch.setattr(hearing.os.path, "exists", lambda p: False)
    out = hearing.speaking_lines({"PATH": str(tmp_path)})
    assert out[0].startswith("speaking:  NO. No ffmpeg was found where the bot looks")
    text = _flat(out)
    assert str(tmp_path) in text and "/opt/homebrew/bin/ffmpeg" in text and "/usr/local/bin/ffmpeg" in text
    assert "every spoken reply will fail and go to the caller as chat text" in text
    assert [ln.strip() for ln in out[-2:]] == [FIX, "sessionphone bot install"]
    assert "macOS say" in text         # auto: with no server, say speaks


def test_server_mode_has_no_say_to_fall_back_on(monkeypatch, tmp_path):
    monkeypatch.setattr(hearing.os.path, "exists", lambda p: False)
    text = _flat(hearing.speaking_lines({"PATH": str(tmp_path), "DC_TTS": "server"}))
    assert text.startswith("speaking: NO.") and "macOS say" not in text


def test_a_bad_dc_ffmpeg_names_the_setting(tmp_path):
    out = hearing.speaking_lines({"DC_FFMPEG": str(tmp_path / "gone")})
    text = _flat(out)
    assert text.startswith(f"speaking: NO. DC_FFMPEG is {tmp_path / 'gone'}, which is not a file")
    assert "chat text" in text and "Point DC_FFMPEG at ffmpeg, or unset it" in text
    assert any(ln.strip() == FIX for ln in out)


def test_apple_tts_needs_no_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(hearing.os.path, "exists", lambda p: False)
    out = hearing.speaking_lines({"PATH": str(tmp_path), "DC_TTS": " Apple "})
    assert out == ["speaking:  yes. DC_TTS=apple: replies are spoken by macOS say, which needs no ffmpeg."]


def test_found_says_yes_and_where(tmp_path):
    f = _exe(tmp_path / "ffmpeg")
    out = hearing.speaking_lines({"DC_FFMPEG": str(f)})
    assert _flat(out) == (f"speaking: yes. ffmpeg at {f} (DC_FFMPEG) turns the speech server's "
                          "replies into call audio.")


def test_the_head_is_the_callers(tmp_path):
    f = _exe(tmp_path / "ffmpeg")
    out = hearing.speaking_lines({"DC_FFMPEG": str(f)}, head="speaking: ")
    assert out[0].startswith("speaking: yes.") and all(ln.startswith(" " * 10) for ln in out[1:])


# ---- bot install: after hearing, judged on the plist -----------------------

@pytest.mark.usefixtures("rig", "no_ffmpeg")
def test_install_says_speaking_last_after_hearing(monkeypatch):
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    report = _report(r.output)
    heard = next(i for i, ln in enumerate(report) if ln.startswith("hearing:"))
    said = next(i for i, ln in enumerate(report) if ln.startswith("speaking:"))
    assert heard < said and report[-1].strip() == "sessionphone bot install"


@pytest.mark.usefixtures("rig", "no_ffmpeg")
def test_install_judges_the_plists_env_not_the_shell(monkeypatch, tmp_path):
    """DC_FFMPEG set in the shell is carried into the job (DC_*), and the line
    reads it back from the plist: a plist rewritten with a bad one says NO."""
    good = _exe(tmp_path / "ok" / "ffmpeg")
    monkeypatch.setenv("DC_FFMPEG", str(good))
    real = service.install

    def install(**kw):
        p = real(**kw)
        j = plistlib.loads(p.read_bytes())
        j["EnvironmentVariables"]["DC_FFMPEG"] = str(tmp_path / "gone")
        p.write_bytes(plistlib.dumps(j))
        return p
    monkeypatch.setattr(service, "install", install)
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    assert _flat(_speaking(r.output)).startswith(f"speaking: NO. DC_FFMPEG is {tmp_path / 'gone'}")


@pytest.mark.usefixtures("rig", "no_ffmpeg")
def test_install_with_apple_tts_says_yes_with_no_ffmpeg(monkeypatch):
    monkeypatch.setenv("DC_TTS", "apple")
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    assert _speaking(r.output) == [
        "speaking:  yes. DC_TTS=apple: replies are spoken by macOS say, which needs no ffmpeg."]
    assert not any(ln.strip() == FIX for ln in r.output.splitlines())


# ---- bot status: the same line, the same exit code -------------------------

@pytest.mark.usefixtures("rig", "no_ffmpeg")
def test_status_says_no_ffmpeg_and_keeps_its_exit_code(monkeypatch):
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    st = CliRunner().invoke(cli, ["bot", "status"])
    block = _speaking(st.output)
    text = _flat(block)
    assert block[0].startswith("speaking: NO. No ffmpeg was found where the bot looks")
    assert "chat text" in text and any(ln.strip() == FIX for ln in block)
    running = service.status()["pid"] is not None
    assert st.exit_code == (0 if running else 1), st.output
    lines = st.output.splitlines()
    assert lines.index(block[0]) > next(i for i, ln in enumerate(lines) if ln.startswith("running:"))
    assert lines[-1].startswith("last")      # the log's last line stays last


@pytest.mark.usefixtures("rig", "an_ffmpeg")
def test_status_says_yes_with_an_ffmpeg(monkeypatch):
    ff = os.environ["DC_FFMPEG"]     # the stand-in an_ffmpeg made
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    st = CliRunner().invoke(cli, ["bot", "status"])
    assert _flat(_speaking(st.output)) == (f"speaking: yes. ffmpeg at {ff} (DC_FFMPEG) turns "
                                           "the speech server's replies into call audio.")


@pytest.mark.usefixtures("rig", "no_ffmpeg")
def test_status_with_no_job_says_nothing_about_speaking(monkeypatch):
    st = CliRunner().invoke(cli, ["bot", "status"])
    assert "(not installed)" in st.output and "speaking:" not in st.output, st.output

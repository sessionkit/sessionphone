"""The RINGING mail names commands its reader can run (PHONE-56).

The mail says "Pick up: dc-answer CHAT". The package installs one entry
point, `sessionphone`; no dc-* tool reaches any PATH, so a reader whose PATH
lacks the repo's bin/ got `command not found` and the call went to
voicemail. The bridge runs from its checkout, so calls.py names the
checkout's bin/dc-answer and bin/dc-hangup by absolute path, shell-quoted,
and falls back to the bare name only when the file is not there.

Each line the mail prints is taken apart as a shell would (shlex) and run
with a bare PATH (/usr/bin:/bin), no repo bin/ on it.
"""
import os
import shlex
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from sessionphone.bridge import calls

ROOT = Path(__file__).resolve().parent.parent
BARE_PATH = "/usr/bin:/bin"


def ringing_mail(monkeypatch):
    """What _mail_ringing sends for chat 12: (body, subject)."""
    sent = []
    monkeypatch.setattr(calls, "log", lambda *_a: None)
    monkeypatch.setattr(calls, "_hooks", types.SimpleNamespace(
        mail_in=lambda *a, **k: sent.append((a, k))))
    s = object.__new__(calls.CallSession)
    s.chat_id, s.msg_id, s.chat_name, s.caller = 12, 7, "Bob Team", "Bob Team"
    s.root_id = "call-12-1@test"
    s._mail_ringing()
    (args, kw), = sent
    return args[3], kw["subject"]


def mail_line(body, label):
    """The shell words after LABEL ('Pick up:' / 'Decline:') in the body."""
    line, = [ln for ln in body.splitlines() if ln.strip().startswith(label)]
    return shlex.split(line.split(label, 1)[1])


def runnable_without_repo_bin(word):
    """Would a shell with PATH=/usr/bin:/bin find WORD? Absolute + executable,
    or found on that bare PATH."""
    if os.path.isabs(word):
        return os.path.isfile(word) and os.access(word, os.X_OK)
    return shutil.which(word, path=BARE_PATH) is not None


@pytest.mark.parametrize("label,tool", [("Pick up:", "dc-answer"), ("Decline:", "dc-hangup")])
def test_the_mail_names_a_command_runnable_with_no_repo_bin_on_path(monkeypatch, label, tool):
    body, _subject = ringing_mail(monkeypatch)
    words = mail_line(body, label)
    assert words[-1] == "12"
    assert Path(words[0]).name == tool
    assert runnable_without_repo_bin(words[0]), (
        f"the RINGING mail says {label} {shlex.join(words)}, and {words[0]!r} is "
        f"not runnable with PATH={BARE_PATH}")


@pytest.mark.parametrize("label", ["Pick up:", "Decline:"])
def test_the_named_command_really_runs_from_a_bare_shell(monkeypatch, tmp_path, label):
    """Run it, not just look at it: `<cmd> --help` under bash with a bare
    PATH and a scratch HOME/DC_ROOT/TMPDIR/cwd. --help exits 0 and writes
    nothing (test_bin_help); a missing tool is bash's 127."""
    body, _subject = ringing_mail(monkeypatch)
    cmd = mail_line(body, label)[0]
    dirs = {k: tmp_path / k.lower() for k in ("HOME", "DC_ROOT", "TMPDIR", "CWD")}
    for d in dirs.values():
        d.mkdir()
    env = {"PATH": BARE_PATH, "HOME": str(dirs["HOME"]),
           "DC_ROOT": str(dirs["DC_ROOT"]), "TMPDIR": str(dirs["TMPDIR"])}
    r = subprocess.run(["/bin/bash", "-c", f"{shlex.quote(cmd)} --help"], env=env, cwd=dirs["CWD"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, f"{cmd} --help: exit {r.returncode}: {r.stderr.strip()[-200:]}"
    assert Path(cmd).name in r.stdout


def test_the_subject_names_the_same_pick_up(monkeypatch):
    body, subject = ringing_mail(monkeypatch)
    assert f"{calls.ANSWER_CMD} 12" in subject
    assert mail_line(body, "Pick up:") == shlex.split(f"{calls.ANSWER_CMD} 12")


def test_the_commands_are_this_checkouts_bin():
    assert calls.TOOL_BIN == ROOT / "bin"
    assert shlex.split(calls.ANSWER_CMD)[0] == str(ROOT / "bin" / "dc-answer")
    assert shlex.split(calls.HANGUP_CMD)[0] == str(ROOT / "bin" / "dc-hangup")


def test_profile_rides_after_the_path(tmp_path):
    tool = tmp_path / "dc-answer"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    assert shlex.split(calls.tool_cmd("dc-answer", "public", tmp_path)) == \
        [str(tool), "--profile", "public"]
    assert calls.tool_cmd("dc-answer", "", tmp_path) == shlex.quote(str(tool))


def test_a_path_with_a_space_is_quoted(tmp_path):
    d = tmp_path / "my code" / "bin"
    d.mkdir(parents=True)
    tool = d / "dc-hangup"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    cmd = calls.tool_cmd("dc-hangup", "public", d)
    assert shlex.split(cmd) == [str(tool), "--profile", "public"]


@pytest.mark.parametrize("make", ["absent", "not-executable"])
def test_falls_back_to_the_bare_name(tmp_path, make):
    if make == "not-executable":
        (tmp_path / "dc-answer").write_text("#!/bin/sh\n")
        (tmp_path / "dc-answer").chmod(0o644)
    assert calls.tool_cmd("dc-answer", "", tmp_path) == "dc-answer"
    assert calls.tool_cmd("dc-answer", "public", tmp_path) == "dc-answer --profile public"

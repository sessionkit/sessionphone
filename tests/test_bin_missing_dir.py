"""No bin/ tool resolves a path through a failed cd (PHONE-127 SC3).

dc-send-file built its absolute path as
`FILE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"`. That assignment takes
the status of the LAST substitution, basename's, so `set -e` never fired. A
missing directory then became `/<basename>`. If that file existed, the tool
queued it.

Every path-taking tool is walked here, and each one is held to the same rule:
a missing directory exits non-zero, names the path AS GIVEN, and queues or
writes nothing. The decoy is a regular file that already sits at `/` (macOS
ships `/.file`). The test only reads it; nothing here writes outside tmp.

Walked, by reading bin/ at the base:
  dc-send-file PATH        fixed (the failed cd)
  dc-send-file --html FILE already safe ([ -s ] on the path as given)
  dc-say --html FILE       already safe ([ -s ] on the path as given)
  dc-call-mix CALL_DIR     fixed: it refused, but did not name the path
Not paths at the bin layer: dc-ring VOICE and dc-call --voice are names passed
to the bot as-is; dc-xdc-update PAYLOAD_JSON is JSON. sessionphone qr
--template is a click.Path(exists=True). bin/dc-env, bin/sessionphone and
bin/bridge-check cd only to their own directory.
"""
import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def root_decoy():
    """A regular file directly under /, so the old bug's `/<basename>` would exist."""
    try:
        names = sorted(os.listdir("/"))
    except OSError:
        return None
    for n in names:
        p = Path("/") / n
        if p.is_file() and not p.is_symlink():
            return n
    return None


DECOY = root_decoy()
NAMES = ["x.ogg"] + ([DECOY] if DECOY else [])


def run(tool, root, *args, **extra):
    root.mkdir(parents=True, exist_ok=True)
    allow = root / "channels.txt"
    if not allow.exists():
        allow.write_text("thirteen  chat:13\n")
        allow.chmod(0o600)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DC_", "PIP_CHAT_")) and k not in ("SESSIONPHONE_ROOT", "CDPATH")}
    env.update(HOME=str(root / "home"), DC_ROOT=str(root), DC_TAKEN_WAIT="0", **extra)
    return subprocess.run([str(BIN / tool), *args], env=env, cwd=root, capture_output=True,
                          text=True, timeout=30, check=False)


def nothing_queued(root):
    return not [p for p in root.rglob("*.json") if "home" not in p.parts]


@pytest.mark.parametrize("name", NAMES)
def test_dc_send_file_missing_dir_names_the_path_as_given(tmp_path, name):
    root = tmp_path / "bot"
    given = str(tmp_path / "nonexistent-dir" / name)
    r = run("dc-send-file", root, "13", given)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert given in r.stderr
    assert f": /{name}" not in r.stderr          # never the old bug's /<basename>
    assert nothing_queued(root)


@pytest.mark.parametrize("name", NAMES)
def test_dc_send_file_missing_relative_dir_names_the_path_as_given(tmp_path, name):
    root = tmp_path / "bot"
    given = f"nonexistent-dir/{name}"
    r = run("dc-send-file", root, "13", given)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert f"no such file: {given}" in r.stderr
    assert nothing_queued(root)


def test_dc_send_file_voice_message_missing_dir_names_it_and_the_suffixes(tmp_path):
    root = tmp_path / "bot"
    given = str(tmp_path / "nonexistent-dir" / "x.ogg")
    r = run("dc-send-file", root, "--voice-message", "13", given)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert given in r.stderr and ".ogg" in r.stderr
    assert nothing_queued(root)


def test_dc_send_file_path_through_a_file_is_refused(tmp_path):
    """dirname is a FILE, not a directory: cd fails the same way."""
    root = tmp_path / "bot"
    (tmp_path / "plain").write_text("not a dir")
    given = str(tmp_path / "plain" / "x.ogg")
    r = run("dc-send-file", root, "13", given)
    assert r.returncode == 1
    assert f"no such file: {given}" in r.stderr
    assert nothing_queued(root)


def test_dc_send_file_ignores_cdpath(tmp_path):
    """A relative dir is found from the cwd, never through CDPATH, and cd prints nothing into the path."""
    root = tmp_path / "bot"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "elsewhere/sub").mkdir(parents=True)
    (tmp_path / "elsewhere/sub/x.ogg").write_text("decoy")
    r = run("dc-send-file", root, "13", "sub/x.ogg", CDPATH=str(tmp_path / "elsewhere"))
    assert r.returncode == 1
    assert "no such file: " in r.stderr and "elsewhere" not in r.stderr
    assert nothing_queued(root)


@pytest.mark.parametrize("tool", ["dc-send-file", "dc-say"])
def test_html_in_a_missing_dir_names_the_path_as_given(tmp_path, tool):
    root = tmp_path / "bot"
    real = tmp_path / "real.txt"
    real.write_text("a file")
    given = str(tmp_path / "nonexistent-dir" / "page.html")
    args = ["--html", given, "13", str(real)] if tool == "dc-send-file" else ["--html", given, "13", "hi"]
    r = run(tool, root, *args)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert given in r.stderr
    assert nothing_queued(root)


@pytest.mark.parametrize("given", ["{tmp}/nonexistent-dir/call", "nonexistent-dir/call"])
def test_dc_call_mix_missing_dir_names_the_path_as_given(tmp_path, given):
    root = tmp_path / "bot"
    given = given.format(tmp=tmp_path)
    r = run("dc-call-mix", root, given)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert f"no call dir: {given}" in r.stderr
    assert not list(tmp_path.rglob("stereo.wav"))


def test_dc_call_mix_with_no_calls_names_where_it_looked(tmp_path):
    root = tmp_path / "bot"
    r = run("dc-call-mix", root)
    assert r.returncode == 1
    assert str(root / "calls") in r.stderr

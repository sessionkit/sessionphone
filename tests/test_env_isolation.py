"""The seat's environment never reaches the suite (PHONE-131).

bridge/dc_env.py reads DC_AGENT / CLAUDE_CODE_AGENT and DC_ROOT /
SESSIONPHONE_ROOT at import, so a test run from a seat's shell used to see
that seat's name and live root: with CLAUDE_CODE_AGENT=alice,
test_mail_leg_posts_once_to_a_listed_chat failed, because that agent's mail became a
known voice. tests/conftest.py drops every such variable before any sessionphone
import. These tests fail if that isolation is removed or comes too late.
"""
import json
import os
import pathlib
import subprocess
import sys

from sessionphone import paths
from sessionphone.bridge import calls, dc_env

REPO = pathlib.Path(__file__).resolve().parents[1]
PROBES = ("tests/test_env_isolation.py::test_import_time_settings_are_a_bare_install",
          "tests/test_env_isolation.py::test_no_seat_variable_survives_into_a_test")
LEAKY = "tests/bridge/test_incall_posts.py::test_mail_leg_posts_once_to_a_listed_chat"


def test_import_time_settings_are_a_bare_install() -> None:
    # what dc_env and calls froze at import, whatever the shell that ran pytest held
    assert dc_env.AGENT == "claude"
    assert dc_env.VOICE == "af_sky"
    assert calls.DEFAULT_VOICE_MAP == {"claude": "af_sky"}
    assert dc_env.ROOT == paths.root()
    assert calls.VOICE_MAP_FILE == dc_env.ROOT / "voices.json"


def test_no_seat_variable_survives_into_a_test() -> None:
    # the autouse tripwires set DC_SENDMAIL, DC_MESSAGE, DC_SIGNER_* and DC_STT_URL per test. And
    # importing bridge/bot.py fills an unset DC_AGENT from sys.argv[1] (the launchd
    # job names the agent there); under pytest that is pytest's own argument, never
    # the seat's. Nothing else may be set.
    tripwires = {"DC_SENDMAIL", "DC_MESSAGE", "DC_SIGNER_SOCK", "DC_SIGNER_PUB", "DC_STT_URL"}
    left = {k: v for k, v in os.environ.items()
            if (k in ("CLAUDE_CODE_AGENT", "SESSIONPHONE_ROOT") or k.startswith(("DC_", "PIP_CHAT_")))
            and k not in tripwires}
    if len(sys.argv) > 1 and left.get("DC_AGENT") == sys.argv[1]:
        del left["DC_AGENT"]
    assert left == {}


def test_a_hostile_seat_env_does_not_reach_the_suite(tmp_path) -> None:
    """A child pytest, run as a seat would run it: named alice, with a root whose
    voices.json knows alice. Without the conftest isolation all three fail."""
    root = tmp_path / "seat-root"
    root.mkdir()
    (root / "voices.json").write_text(json.dumps({"alice": "x"}))
    env = dict(os.environ, CLAUDE_CODE_AGENT="alice", DC_AGENT="alice", PIP_CHAT_AGENT="alice",
               DC_ROOT=str(root), DC_VOICE="x", SESSIONPHONE_ROOT=str(tmp_path / "sp-root"))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *PROBES, LEAKY],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "3 passed" in r.stdout, r.stdout[-3000:]
    assert json.loads((root / "voices.json").read_text()) == {"alice": "x"}   # the seat root, untouched


def test_a_root_bot_install_kept_does_not_reach_the_suite(tmp_path) -> None:
    """A child pytest on a machine where `bot install` has run: its phone.json
    keeps a root, which paths.root() reads. dc_env.ROOT is bound at import,
    before any per-test stub, so without the conftest isolation the suite
    freezes that live root into it."""
    home = tmp_path / "home"
    kept = home / ".sessionkit" / "phone" / "settings" / "phone.json"
    kept.parent.mkdir(parents=True)
    kept.write_text(json.dumps({"bots": {"alice": {"SESSIONPHONE_ROOT": str(tmp_path / "kept-root")}}}))
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *PROBES],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "2 passed" in r.stdout, r.stdout[-3000:]

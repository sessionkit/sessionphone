"""The bot's side of the media guard (PHONE-145): media.gone(), the calls-only
verbs in media.require(), and bridge/ack.py.

- gone() sees a pack removed under a LIVE process, which missing() cannot:
  find_spec returns a loaded module's cached spec and never looks at disk
  (rca-001, findings/rca-001.md in the task dir). Both ways a pack goes are
  tested: the suite's meta-path blocker, and a real deletion of a package
  that was already imported.
- `bot install` and `bot run` pass require() with DC_CALLS=off: the bot they
  start never imports the pack. A calls-on bot keeps PHONE-10's refusal. The
  call sites in cli.py are unchanged; the condition is in media.py.
- ack.write answers every job without calls.py, and never raises.

New tests live here, not in test_media_guard.py (another task's diff holds it);
the bot install probe below borrows that file's PROBE, read-only.
"""
from __future__ import annotations

import importlib
import importlib.abc
import json
import os
import plistlib
import shutil
import subprocess
import sys

import pytest
import test_media_guard as guard  # its PROBE and LINK only; nothing in it is changed

from sessionphone import media
from sessionphone.bridge import ack


class Block(importlib.abc.MetaPathFinder):
    """What the suite calls a removed pack: any import of these names raises."""

    def __init__(self, names):
        self.names, self.attempts = set(names), []

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.names:
            self.attempts.append(name)
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)


# ---- gone(): what a fresh import would find --------------------------------------


def test_gone_is_empty_with_the_pack_here():
    assert media.gone() == []


def test_gone_sees_a_blocked_pack_that_missing_cannot(monkeypatch):
    """Load the pack as a live calls-on bot has, then block it. missing()
    still finds every loaded module (the cached spec); gone() asks the
    finders, as a first import would."""
    for name in ("numpy", "av", "aioice", "cryptography"):
        importlib.import_module(name)
    monkeypatch.setattr(sys, "meta_path", [Block(media.MODULES), *sys.meta_path])
    loaded = [n for n in media.MODULES if n in sys.modules]
    assert len(loaded) >= 4, loaded
    assert set(media.missing()).isdisjoint(loaded)     # why gone() exists
    assert media.gone() == list(media.MODULES)


def test_gone_names_one_blocked_module_alone(monkeypatch):
    monkeypatch.setattr(sys, "meta_path", [Block({"av"}), *sys.meta_path])
    assert media.gone() == ["av"]


def test_gone_sees_a_loaded_package_deleted_from_disk(tmp_path, monkeypatch):
    """The real removal: a package imported, then its files deleted. The
    module stays in sys.modules, so missing() says present; gone() says gone,
    and the lazy import a live call would make fails the same way."""
    pkg = tmp_path / "fakepack145"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("X = 1\n")
    (pkg / "lazy.py").write_text("Y = 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(media, "MODULES", ("fakepack145",))
    importlib.invalidate_caches()
    importlib.import_module("fakepack145")
    try:
        assert media.gone() == []
        shutil.rmtree(pkg)
        assert media.missing() == []                    # the cached spec hides it
        assert media.gone() == ["fakepack145"]
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("fakepack145.lazy")
    finally:
        sys.modules.pop("fakepack145", None)
        sys.modules.pop("fakepack145.lazy", None)


def test_gone_imports_nothing(monkeypatch):
    """Like missing(): it looks, and loads none of what it looks for."""
    monkeypatch.setattr(media, "MODULES", ("sessionphone_never_a_module_145", "json"))
    before = set(sys.modules)
    assert media.gone() == ["sessionphone_never_a_module_145"]
    assert set(sys.modules) - before == set()


def test_calls_refused_names_the_pack_the_fix_and_the_restart():
    text = media.calls_refused(["numpy", "av"])
    assert text.startswith("calls need the media pack")
    assert "(missing: numpy, av)" in text
    assert "bot install" in text and "restart the bot" in text and "Chat still works." in text


# ---- require(): bot install and bot run are calls-only -------------------------------


@pytest.fixture
def no_pack(monkeypatch):
    monkeypatch.setattr(media, "missing", lambda: ["numpy"])
    monkeypatch.delenv("DC_CALLS", raising=False)
    monkeypatch.delenv("PIP_CHAT_CALLS", raising=False)
    return monkeypatch


@pytest.mark.parametrize("verb", media.CALLS_ONLY)
def test_a_calls_only_verb_passes_with_calls_off(no_pack, verb):
    no_pack.setenv("DC_CALLS", "off")
    media.require(verb)                 # no raise: the bot it starts never imports the pack


@pytest.mark.parametrize("verb", media.CALLS_ONLY)
@pytest.mark.parametrize("calls", [None, "", "on", "OFF", "no"])
def test_a_calls_only_verb_still_refuses_unless_calls_are_exactly_off(no_pack, verb, calls):
    """Only "off" is off, as the bot reads DC_CALLS: unset, empty and anything
    else leave calls on, so PHONE-10's refusal stands."""
    if calls is not None:
        no_pack.setenv("DC_CALLS", calls)
    with pytest.raises(media.Missing):
        media.require(verb)


@pytest.mark.parametrize("verb", media.CALLS_ONLY)
def test_the_deprecated_alias_does_not_pass_the_guard(no_pack, verb):
    """`bot install` carries DC_* into the job and nothing else, so a
    PIP_CHAT_CALLS=off install would start a calls-ON bot: refuse it."""
    no_pack.setenv("PIP_CHAT_CALLS", "off")
    with pytest.raises(media.Missing):
        media.require(verb)


@pytest.mark.parametrize("verb", ["web", "web enrol"])
def test_web_needs_the_pack_whatever_dc_calls_says(no_pack, verb):
    no_pack.setenv("DC_CALLS", "off")
    with pytest.raises(media.Missing):
        media.require(verb)


def test_bot_install_with_calls_off_and_no_pack_writes_a_chat_only_job(tmp_path):
    """End to end, the pack blocked: `DC_CALLS=off sessionphone bot install`
    writes the job, carries DC_CALLS=off into it (what keeps the bot it starts
    off the pack), and loads none of the pack. Without DC_CALLS=off the same
    command refuses (test_media_guard's test_bot_install_refuses_first...)."""
    root, home, out = tmp_path / "root", tmp_path / "home", tmp_path / "probe.json"
    home.mkdir()
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    (root / "invite.txt").write_text(guard.LINK + "\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_AGENT", "SESSIONPHONE_ROOT", "PYTHONPATH")
           and not k.startswith(("DC_", "PIP_CHAT_"))}
    env.update(HOME=str(home), SESSIONPHONE_ROOT=str(root), DC_HOST="host.example", DC_AGENT="probe",
               DC_CALLS="off", PROBE_BLOCK=json.dumps(sorted(media.MODULES)), PROBE_OUT=str(out),
               PROBE_LINK=guard.LINK, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", guard.PROBE, "bot", "install", "--owner", "owner@example.org"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False,
                          stdin=subprocess.DEVNULL)
    assert out.exists(), proc.stderr[-3000:]
    rec = json.loads(out.read_text())
    assert rec["rc"] == 0, proc.stderr
    assert "media pack" not in proc.stderr, proc.stderr
    assert rec["heavy"] == [] and rec["attempts"] == [], rec
    plists = sorted((home / "Library" / "LaunchAgents").glob("*.plist"))
    assert len(plists) == 1, plists
    job = plistlib.loads(plists[0].read_bytes())
    assert job["EnvironmentVariables"]["DC_CALLS"] == "off"
    assert job["ProgramArguments"][1:3] == ["-m", "sessionphone.bridge.bot"]


# ---- ack.write: every job's answer, without calls.py ---------------------------------


def test_ack_write_writes_ok_and_detail(tmp_path):
    p = tmp_path / "acks" / "00.json"
    ack.write(str(p), True, "sent to chat 12, msg 78")
    assert json.loads(p.read_text()) == {"ok": True, "detail": "sent to chat 12, msg 78"}
    assert not p.with_name("00.json.part").exists()


def test_ack_write_with_no_path_writes_nothing(tmp_path):
    ack.write(None, True, "x")
    ack.write("", False, "x")
    assert list(tmp_path.iterdir()) == []


def test_ack_write_logs_a_failure_and_never_raises(tmp_path):
    """A calls-off bot used to crash here: calls.ack_write logged through
    calls.log, whose hooks only calls.setup() sets (rca-001 item 1)."""
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    seen = []
    ack.write(str(blocker / "00.json"), True, "x", log=seen.append)
    assert len(seen) == 1 and seen[0].startswith(f"ack write failed for {blocker / '00.json'}")


def test_ack_write_survives_a_log_that_raises(tmp_path, capsys):
    blocker = tmp_path / "a-file"
    blocker.write_text("")

    def broken(msg):
        raise AttributeError("'NoneType' object has no attribute 'log'")
    ack.write(str(blocker / "00.json"), True, "x", log=broken)
    assert "ack write failed" in capsys.readouterr().err


def test_ack_module_is_light():
    """bridge/ack.py imports the standard library only: a chat-only bot
    imports it on the light core."""
    code = ("import sys, json; import sessionphone.bridge.ack; "
            f"print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in {list(media.MODULES)!r})))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(r.stdout) == []


def test_calls_ack_write_is_the_light_one(tmp_path):
    """calls.ack_write stays for its callers and writes the same file."""
    from sessionphone.bridge import calls
    p = tmp_path / "01.json"
    calls.ack_write(str(p), False, "no live call in chat 12")
    assert json.loads(p.read_text()) == {"ok": False, "detail": "no live call in chat 12"}

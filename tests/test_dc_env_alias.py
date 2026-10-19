"""PIP_CHAT_<NAME> is a deprecated alias of DC_<NAME> (PHONE-132 design-001 (b)).

A live bot's launchd job still sets ten PIP_CHAT_* keys with no DC_ twin, so
the alias is kept: it is still read, and the first read of each name from the
alias says so on stderr, once per process. A DC_ value wins and says nothing.
bin/dc-env and bin/dc-log mirror dc_env, so they warn the same way. Every
subprocess here runs with HOME pointed at a tmp dir and no DC_/PIP_CHAT_
variables inherited.
"""
import os
import subprocess
from pathlib import Path

import pytest

from sessionphone.bridge import dc_env

BIN = Path(__file__).resolve().parent.parent / "bin"
WARN = "PIP_CHAT_{0} is deprecated; rename it DC_{0}"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """A process that has warned about nothing yet, and no inherited settings."""
    monkeypatch.setattr(dc_env, "_warned", set())
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)


# --- dc_env.env and dc_env._get ----------------------------------------------

def test_the_alias_is_read_and_warns(monkeypatch, capsys):
    monkeypatch.setenv("PIP_CHAT_PROFILE", "public")
    assert dc_env.env("PROFILE") == "public"
    err = capsys.readouterr().err
    assert err.count(WARN.format("PROFILE")) == 1


def test_a_second_read_of_the_same_name_does_not_warn(monkeypatch, capsys):
    monkeypatch.setenv("PIP_CHAT_PROFILE", "public")
    dc_env.env("PROFILE")
    capsys.readouterr()
    assert dc_env.env("PROFILE") == "public"
    assert capsys.readouterr().err == ""


def test_each_name_warns_once_on_its_own(monkeypatch, capsys):
    monkeypatch.setenv("PIP_CHAT_PROFILE", "public")
    monkeypatch.setenv("PIP_CHAT_STUN_RTO", "2")
    dc_env.env("PROFILE")
    dc_env.env("STUN_RTO")
    dc_env.env("PROFILE")
    err = capsys.readouterr().err
    assert err.count(WARN.format("PROFILE")) == 1
    assert err.count(WARN.format("STUN_RTO")) == 1


def test_dc_wins_over_the_alias_and_says_nothing(monkeypatch, capsys):
    monkeypatch.setenv("DC_PROFILE", "dc")
    monkeypatch.setenv("PIP_CHAT_PROFILE", "alias")
    assert dc_env.env("PROFILE") == "dc"
    assert capsys.readouterr().err == ""


def test_an_empty_dc_falls_through_to_the_alias_and_warns(monkeypatch, capsys):
    monkeypatch.setenv("DC_PROFILE", "")
    monkeypatch.setenv("PIP_CHAT_PROFILE", "alias")
    assert dc_env.env("PROFILE") == "alias"
    assert WARN.format("PROFILE") in capsys.readouterr().err


def test_an_empty_alias_is_unset_and_says_nothing(monkeypatch, capsys):
    monkeypatch.setenv("PIP_CHAT_PROFILE", "")
    assert dc_env.env("PROFILE", "default") == "default"
    assert capsys.readouterr().err == ""


def test_neither_set_gives_the_default_and_says_nothing(capsys):
    assert dc_env.env("PROFILE", "default") == "default"
    assert dc_env.env("PROFILE") is None
    assert capsys.readouterr().err == ""


def test_get_on_a_mapping_reads_the_alias_and_warns_once(capsys):
    e = {"PIP_CHAT_CALLS": "off"}
    assert dc_env._get(e, "CALLS") == "off"
    assert dc_env._get(e, "CALLS") == "off"
    assert capsys.readouterr().err.count(WARN.format("CALLS")) == 1


def test_get_on_a_mapping_prefers_dc_and_says_nothing(capsys):
    assert dc_env._get({"DC_CALLS": "on", "PIP_CHAT_CALLS": "off"}, "CALLS") == "on"
    assert dc_env._get({}, "CALLS") is None
    assert capsys.readouterr().err == ""


def test_path_reads_the_alias(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PIP_CHAT_SEEN", str(tmp_path / "seen"))
    assert dc_env.path("SEEN", "seen.txt") == tmp_path / "seen"
    assert WARN.format("SEEN") in capsys.readouterr().err


# --- the bin/ mirrors ----------------------------------------------------------

def clean_env(home, **extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    e["HOME"] = str(home)
    e.update(extra)
    return e


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def dc_channels(home, **extra):
    """bin/dc-env's dc_channels, sourced as the tools source it."""
    return subprocess.run(["bash", "-c", f'source "{BIN / "dc-env"}"; dc_channels'],
                          env=clean_env(home, DC_ROOT=str(home / "root"), **extra),
                          capture_output=True, text=True, timeout=30, check=False)


def test_dc_env_reads_the_channels_alias_and_warns(home):
    r = dc_channels(home, PIP_CHAT_CHANNELS_FILE="/lists/alias.txt")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "/lists/alias.txt"
    assert r.stderr.count(WARN.format("CHANNELS_FILE")) == 1


def test_dc_env_prefers_dc_channels_file_and_says_nothing(home):
    r = dc_channels(home, DC_CHANNELS_FILE="/lists/dc.txt",
                    PIP_CHAT_CHANNELS_FILE="/lists/alias.txt")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "/lists/dc.txt"
    assert r.stderr == ""


def test_dc_env_with_neither_uses_the_root_and_says_nothing(home):
    r = dc_channels(home)
    assert r.stdout == str(home / "root" / "channels.txt")
    assert r.stderr == ""


def dc_log(home, **extra):
    return subprocess.run([str(BIN / "dc-log"), "--raw"], env=clean_env(home, **extra),
                          cwd=home, capture_output=True, text=True, timeout=30, check=False)


def test_dc_log_reads_the_alias_and_warns_once(home):
    log = home / "alias.log"
    log.write_text("FROM THE ALIAS\n")
    r = dc_log(home, PIP_CHAT_LOG=str(log))
    assert r.returncode == 0, r.stderr
    assert "FROM THE ALIAS" in r.stdout
    assert r.stderr.count(WARN.format("LOG")) == 1


def test_dc_log_prefers_dc_and_says_nothing(home):
    (home / "dc.log").write_text("FROM DC\n")
    (home / "alias.log").write_text("FROM THE ALIAS\n")
    r = dc_log(home, DC_LOG=str(home / "dc.log"), PIP_CHAT_LOG=str(home / "alias.log"))
    assert r.returncode == 0, r.stderr
    assert "FROM DC" in r.stdout and "FROM THE ALIAS" not in r.stdout
    assert "deprecated" not in r.stderr

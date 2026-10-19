"""bin/sessionphone fetches the media pack only for the media verbs (PHONE-10).

Every `uv run` the launcher makes passes --no-dev, so no user gets the dev
tools. Only the media verbs (bot install, bot run, web, web enrol) add
`--extra media`; setup, qr, link and the rest run on the four light deps. The
first media verb finds the pack missing, prints ONE plain line saying what it
fetches and why, runs `uv sync --inexact --no-dev --extra media`, and only then
runs the verb. A failed fetch is a plain line and exit 2. Help, wherever -h or
--help sits, never reaches uv at all.

Nothing here touches the network or installs anything: a fake `uv`, first on
PATH, records each argv and exits. The launcher is copied into a scratch
plugin root, so the venv it looks at is a fake one too: a script standing in
for `.venv/bin/python` says whether the pack is there. Two tests use a REAL
interpreter for the launcher's find_spec probe: this suite's own (which has
the pack) through a wrapper, and an empty `python -m venv --without-pip` in
tmp (which has none of it).

The table test reads the launcher's own media-verb list and holds it EQUAL to
the CLI's media.require() call sites (the AST walk in test_media_guard.py). A
verb the CLI guards but the launcher does not fetch for would loop a user on
exit 2 forever (decompose review m1).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_media_guard import _commands, require_sites

from sessionphone import media

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "sessionphone"
SEP, REC = "\x1f", "\x1e"     # between args, after each call

# The fake uv: one record per call, its argv joined by \x1f, ended by \x1e
# (the probe's -c body has newlines). `uv sync` exits
# FAKE_UV_SYNC_RC; a successful sync leaves the marker the fake venv python
# reads, as a real fetch would leave the pack.
FAKE_UV = r'''#!/usr/bin/env bash
{ printf '%s\x1f' "$@"; printf '\x1e'; } >> "$FAKE_UV_LOG"
if [ "${1:-}" = sync ]; then
  rc="${FAKE_UV_SYNC_RC:-0}"
  [ "$rc" = 0 ] && [ -n "${FAKE_PACK:-}" ] && : > "$FAKE_PACK"
  [ "$rc" = 0 ] || echo "error: Failed to download av==17.1.0 (fake uv)" >&2
  exit "$rc"
fi
exit 0
'''

# The fake venv python: it records its argv and has the pack iff the marker is there.
FAKE_PY = r'''#!/usr/bin/env bash
{ printf '%s\x1f' "$@"; printf '\x1e'; } >> "$FAKE_PY_LOG"
[ -e "$FAKE_PACK" ]
'''

# The fake venv entry point, for help: it records that it ran.
FAKE_ENTRY = r'''#!/usr/bin/env bash
{ printf '%s\x1f' "$@"; printf '\x1e'; } >> "$FAKE_ENTRY_LOG"
echo "Usage: sessionphone [OPTIONS] COMMAND [ARGS]..."
'''


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class Plugin:
    """A scratch plugin root holding a copy of the launcher, and a fake uv on PATH."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "plugin"
        self.tool = self.root / "bin" / "sessionphone"
        self.tool.parent.mkdir(parents=True)
        shutil.copy2(LAUNCHER, self.tool)
        self.fakebin = tmp / "fakebin"
        _script(self.fakebin / "uv", FAKE_UV)
        self.home = tmp / "home"
        self.home.mkdir()
        self.pack = self.root / ".venv" / "pack-marker"
        self.logs = {k: tmp / f"{k}.log" for k in ("uv", "py", "entry")}

    def venv(self, *, pack: bool) -> Plugin:
        _script(self.root / ".venv" / "bin" / "python", FAKE_PY)
        if pack:
            self.pack.write_text("")
        return self

    def run(self, *args: str, sync_rc: int = 0, path: str | None = None) -> subprocess.CompletedProcess:
        env = {"PATH": path if path is not None else f"{self.fakebin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
               "HOME": str(self.home), "TMPDIR": str(self.tmp),
               "FAKE_UV_LOG": str(self.logs["uv"]), "FAKE_PY_LOG": str(self.logs["py"]),
               "FAKE_ENTRY_LOG": str(self.logs["entry"]), "FAKE_PACK": str(self.pack),
               "FAKE_UV_SYNC_RC": str(sync_rc)}
        return subprocess.run([str(self.tool), *args], env=env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=False)

    def calls(self, which: str = "uv") -> list[list[str]]:
        f = self.logs[which]
        if not f.exists():
            return []
        return [rec.split(SEP)[:-1] for rec in f.read_text().split(REC)[:-1]]

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:1] == ["run"]]

    def syncs(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:1] == ["sync"]]


def _uv_options(call: list[str]) -> list[str]:
    """A `uv run` call's own options: everything before the `sessionphone` it runs."""
    return call[:call.index("sessionphone")]


@pytest.fixture
def plugin(tmp_path):
    return Plugin(tmp_path)


# ---- the table: the launcher's media verbs ARE the CLI's guarded verbs -------------

def _bash_array(name: str) -> list[str]:
    m = re.search(rf"^{name}=\((.*)\)$", LAUNCHER.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, f"bin/sessionphone has no `{name}=(...)` line"
    return shlex.split(m.group(1))


def test_the_launchers_media_verbs_equal_the_clis_require_call_sites():
    launcher = _bash_array("media_verbs")
    sites = require_sites()
    assert len(launcher) == len(set(launcher)), f"a verb is listed twice: {launcher}"
    assert sites, "the AST walk found no media.require call: it is broken, not empty"
    assert set(launcher) == set(sites), (
        f"guarded in the CLI, never fetched for by the launcher: {set(sites) - set(launcher)}; "
        f"fetched for by the launcher, never guarded in the CLI: {set(launcher) - set(sites)}")


def test_the_launchers_modules_are_the_packs_modules():
    assert tuple(_bash_array("media_modules")) == media.MODULES


VERBS = [v for v, _ in _commands() if v]


def test_the_cli_walk_finds_the_verbs():
    """A broken walk must not pass the parametrised test below vacuously."""
    assert {"setup", "qr", "link", "bot", "bot install", "bot status", "web", "web enrol"} <= set(VERBS)


@pytest.mark.parametrize("verb", VERBS)
def test_every_cli_verb_gets_the_extra_iff_the_cli_guards_it(plugin, verb):
    """Run the launcher for every verb cli.py defines: the guarded ones get
    `--no-dev --extra media`, every other one gets `--no-dev` and no extra."""
    plugin.venv(pack=True)
    r = plugin.run(*verb.split())
    assert r.returncode == 0, r.stderr
    assert plugin.syncs() == []
    [call] = plugin.runs()
    opts = _uv_options(call)
    assert "--no-dev" in opts
    if verb in require_sites():
        assert opts.count("--extra") == 1 and opts[opts.index("--extra") + 1] == "media"
    else:
        assert "--extra" not in opts
    assert call[call.index("sessionphone") + 1:] == verb.split()


# ---- the light verbs --------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["setup", "--name", "Claude"], ["qr"], ["link"], ["invite"], ["callers"], ["channels", "list"],
    ["bot"], ["bot", "status"], ["bot", "uninstall"], [],
], ids=lambda a: " ".join(a) or "bare")
def test_light_verbs_run_without_the_extra_and_never_look_for_the_pack(plugin, argv):
    """No venv at all, the first run's case: still no fetch, no probe, no extra."""
    r = plugin.run(*argv)
    assert r.returncode == 0, r.stderr
    assert plugin.calls() == [["run", "--quiet", "--no-dev", "--project", str(plugin.root), "sessionphone", *argv]]
    assert plugin.calls("py") == []
    assert "media pack" not in r.stderr


def test_a_verb_word_later_in_the_line_is_not_a_media_verb(plugin):
    """`setup --name web` is setup: only the first one or two words pick the verb."""
    plugin.run("setup", "--name", "web")
    [call] = plugin.runs()
    assert "--extra" not in _uv_options(call)


# ---- the media verbs --------------------------------------------------------------

MEDIA_ARGV = [["bot", "install"], ["bot", "install", "--owner", "a@b.example"], ["bot", "run"],
              ["web"], ["web", "--port", "9000"], ["web", "enrol"], ["web", "--port", "9000", "enrol"],
              ["web", "--root", "/x", "enrol", "--root", "/x"]]


@pytest.mark.parametrize("argv", MEDIA_ARGV, ids=" ".join)
def test_media_verb_with_the_pack_present_runs_it_with_the_extra_and_no_fetch(plugin, argv):
    plugin.venv(pack=True)
    r = plugin.run(*argv)
    assert r.returncode == 0, r.stderr
    assert plugin.calls() == [["run", "--quiet", "--no-dev", "--extra", "media", "--project", str(plugin.root),
                               "sessionphone", *argv]]
    assert r.stderr == ""


@pytest.mark.parametrize("argv", MEDIA_ARGV, ids=" ".join)
def test_media_verb_with_the_pack_missing_says_so_once_then_fetches_then_runs(plugin, argv):
    plugin.venv(pack=False)
    r = plugin.run(*argv)
    assert r.returncode == 0, r.stderr
    assert plugin.calls() == [
        ["sync", "--inexact", "--no-dev", "--extra", "media", "--project", str(plugin.root)],
        ["run", "--quiet", "--no-dev", "--extra", "media", "--project", str(plugin.root), "sessionphone", *argv]]
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, r.stderr
    line = lines[0]
    assert line.startswith("sessionphone: ")
    for word in ("media pack", "aiortc", "av", "numpy", "about 31 MB", "once"):
        assert word in line, f"the fetch line does not say {word!r}: {line}"
    assert "setup, qr and link do not" in line          # why: the light verbs never need it
    assert "Traceback" not in r.stderr


def test_the_pack_is_fetched_once_and_never_again(plugin):
    plugin.venv(pack=False)
    plugin.run("bot", "install")
    plugin.run("bot", "run")
    plugin.run("web")
    assert len(plugin.syncs()) == 1
    assert len(plugin.runs()) == 3


def test_no_venv_yet_counts_as_missing(plugin):
    """The first run of all might be a media verb: no .venv, so the sync builds it."""
    r = plugin.run("bot", "install")
    assert r.returncode == 0, r.stderr
    assert [c[0] for c in plugin.calls()] == ["sync", "run"]
    assert "media pack" in r.stderr


def test_the_probe_asks_the_venvs_python_for_exactly_the_six_isolated(plugin):
    plugin.venv(pack=True)
    plugin.run("bot", "run")
    [probe] = plugin.calls("py")
    assert probe[0] == "-I" and probe[1] == "-c"
    assert "find_spec" in probe[2]
    assert tuple(probe[3:]) == media.MODULES


@pytest.mark.parametrize("argv", [["bot", "install"], ["web", "enrol"]], ids=" ".join)
def test_a_failed_fetch_is_a_plain_line_and_exit_2_and_runs_nothing(plugin, argv):
    plugin.venv(pack=False)
    r = plugin.run(*argv, sync_rc=1)
    assert r.returncode == 2
    assert plugin.runs() == [], "the verb ran after its pack failed to download"
    assert len(plugin.syncs()) == 1
    assert "Failed to download av" in r.stderr          # uv's own reason stays visible, above
    last = r.stderr.strip().splitlines()[-1]
    assert last.startswith("sessionphone: the media pack did not download")
    for word in ("Nothing else changed", "setup, qr and link still work", "again", "better link"):
        assert word in last, f"the failure line does not say {word!r}: {last}"
    assert "Traceback" not in r.stderr
    assert not plugin.pack.exists()


def test_after_a_failed_fetch_the_next_run_tries_again(plugin):
    plugin.venv(pack=False)
    assert plugin.run("bot", "install", sync_rc=1).returncode == 2
    assert plugin.run("bot", "install").returncode == 0
    assert len(plugin.syncs()) == 2
    assert len(plugin.runs()) == 1


def test_a_failed_fetch_leaves_the_light_verbs_working(plugin):
    plugin.venv(pack=False)
    plugin.run("bot", "install", sync_rc=1)
    r = plugin.run("qr")
    assert r.returncode == 0
    assert plugin.calls()[-1] == ["run", "--quiet", "--no-dev", "--project", str(plugin.root), "sessionphone", "qr"]


def test_every_uv_call_passes_no_dev(plugin):
    plugin.venv(pack=False)
    for argv in (["setup", "--name", "X"], ["bot", "install"], ["web"], ["bot", "status"], ["qr"]):
        plugin.run(*argv)
    calls = plugin.calls()
    assert len(calls) == 6
    assert all("--no-dev" in c for c in calls)


# ---- the probe on a real interpreter ----------------------------------------------

def test_real_probe_finds_the_pack_in_this_suites_env(plugin):
    """This suite's interpreter has the pack (the dev group carries it), so the
    launcher's own find_spec probe through it says present: no fetch."""
    assert media.missing() == [], "this suite's env lacks the pack; the test cannot say anything"
    _script(plugin.root / ".venv" / "bin" / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    r = plugin.run("bot", "run")
    assert r.returncode == 0, r.stderr
    assert plugin.syncs() == []
    assert "--extra" in _uv_options(plugin.runs()[0])


def test_real_probe_finds_no_pack_in_an_empty_venv(plugin):
    """An empty venv (no pip, nothing installed) has none of the six: the probe
    says missing, and the launcher fetches."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(plugin.root / ".venv")],
                   check=True, capture_output=True, timeout=120)
    r = plugin.run("bot", "install")
    assert r.returncode == 0, r.stderr
    assert len(plugin.syncs()) == 1
    assert "media pack" in r.stderr


# ---- help never fetches -----------------------------------------------------------

HELP_ARGV = [["-h"], ["--help"], ["bot", "install", "--help"], ["bot", "run", "-h"], ["web", "-h"],
             ["web", "--port", "9000", "enrol", "--help"], ["setup", "--help"], ["bot", "--help", "install"]]


@pytest.mark.parametrize("argv", HELP_ARGV, ids=" ".join)
def test_help_anywhere_goes_to_the_venvs_entry_point_and_never_to_uv(plugin, argv):
    plugin.venv(pack=False)
    _script(plugin.root / ".venv" / "bin" / "sessionphone", FAKE_ENTRY)
    r = plugin.run(*argv)
    assert r.returncode == 0, r.stderr
    assert plugin.calls() == [], "help reached uv: it could sync, fetch the pack and fill uv's cache"
    assert plugin.calls("py") == []
    assert plugin.calls("entry") == [argv]
    assert "media pack" not in r.stderr


@pytest.mark.parametrize("argv", HELP_ARGV, ids=" ".join)
def test_help_anywhere_with_no_venv_prints_usage_and_needs_no_uv(plugin, argv):
    """No venv and no uv on the PATH at all: help still answers, exit 0."""
    r = plugin.run(*argv, path="/usr/bin:/bin")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("usage: sessionphone")
    assert plugin.calls() == []
    assert sorted(p.name for p in plugin.home.rglob("*")) == []


def test_a_help_flag_after_double_dash_is_an_argument_not_help(plugin):
    """After `--` the words are the command's, so the launcher does not answer them."""
    plugin.run("callers", "add", "--", "--help")
    [call] = plugin.runs()
    assert call[-2:] == ["--", "--help"]


def test_no_uv_on_the_path_is_a_plain_exit_2(plugin):
    r = plugin.run("bot", "install", path="/usr/bin:/bin")
    assert r.returncode == 2
    assert "uv is not on the PATH" in r.stderr

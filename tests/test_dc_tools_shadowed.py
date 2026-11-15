"""PHONE-302: say so when a dc-* tool on PATH is not this install's.

A shell runs the FIRST dc-* of a name on PATH. On one host ~/.local/bin/dc-*
were links into an older private checkout's bin/, first on PATH, so a bare
`dc-live` said "the bot is NOT RUNNING" while every bot ran, `dc-say NAME`
raised, and `dc-log 12` said "unknown arg". The kit's code never ran, so it
could not correct the answer; it can only say so where a stranger already
looks: `sessionphone bot status`'s `dc tools:` lines (sessionphone.dctools).

This install's copy has two legitimate routes, and neither is an alarm: the
console script in the running interpreter's scripts folder (PHONE-314; a
`uv tool` link in ~/.local/bin resolves into it), and the checkout's bin/
(dcbin/'s files resolve into it). A name absent from PATH is not foreign.
Every PATH here is a tmp dir: no tool is run, nothing is installed.
"""
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_hearing import rig  # noqa: F401 - rig is a fixture

from sessionphone import dctools, service
from sessionphone.cli import main as cli

BIN = Path(__file__).resolve().parent.parent / "bin"
STALE = "#!/bin/sh\necho 'the bot is NOT RUNNING ... nothing can start one' >&2\nexit 2\n"
CONSOLE = "#!/venv/bin/python\nfrom sessionphone.dctools import main\nmain()\n"


def _tool(path, body=STALE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _link(path, to):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(to)
    return path


@pytest.fixture
def kit(tmp_path):
    """This install's scripts folder (the console-script route), holding a
    console script for every shipped tool, as a wheel install has."""
    scripts = tmp_path / "uv" / "tools" / "sessionkit" / "bin"
    for name in dctools.tools():
        _tool(scripts / name, CONSOLE)
    return scripts


def _path(*dirs):
    return os.pathsep.join(str(d) for d in dirs)


# ---- the check: sessionphone.dctools.on_path ---------------------------------

def test_a_foreign_dc_live_ahead_on_path_is_named(tmp_path, kit):
    """The reported shape: ~/.local/bin/dc-live, first on PATH, links into another
    checkout's bin/. It is named: the PATH entry, where it resolves, and this
    install's copy."""
    stale = _tool(tmp_path / "old-checkout" / "bin" / "dc-live")
    local = _link(tmp_path / "home" / ".local" / "bin" / "dc-live", stale)
    hits = dctools.on_path(_path(local.parent, kit), dirs=[kit])
    bad = dctools.foreign(hits)
    assert [(h.name, h.found, h.resolves, h.ours) for h in bad] == [
        ("dc-live", str(local), os.path.realpath(stale), str(kit / "dc-live"))]
    # every other tool resolves to this install's copy, further down PATH
    assert {h.name for h in hits if h.mine} == dctools.tools() - {"dc-live"}

    lines = dctools.status_lines(hits)
    text = "\n".join(lines)
    assert lines[0].startswith("dc tools: 1 of the dc-* tools on PATH are NOT this install's")
    assert f"dc-live: in {local.parent}, links into {os.path.dirname(os.path.realpath(stale))}" in text
    assert f"this install's copies are in {kit}" in text
    assert lines[-1] == (f"  Fix: remove or repoint those in {local.parent} "
                         f"or put {kit} ahead of {local.parent} on PATH.")


def test_a_foreign_file_that_is_no_link_is_named_where_it_is(tmp_path, kit):
    """A plain copy, not a link: its folder is named, with no `links into`."""
    stale = _tool(tmp_path / "old" / "dc-say")
    bad = dctools.foreign(dctools.on_path(_path(stale.parent, kit), dirs=[kit]))
    assert [b.name for b in bad] == ["dc-say"]
    text = "\n".join(dctools.status_lines(dctools.on_path(_path(stale.parent, kit), dirs=[kit])))
    assert f"  dc-say: in {stale.parent}\n" in text and "links into" not in text


def test_foreign_tools_are_grouped_by_their_path_entry(tmp_path, kit):
    """Fourteen stale links into one folder are one line, not fourteen."""
    old = tmp_path / "old-checkout" / "bin"
    local = tmp_path / "local"
    for name in ("dc-live", "dc-log", "dc-say"):
        _link(local / name, _tool(old / name))
    lines = dctools.status_lines(dctools.on_path(_path(local, kit), dirs=[kit]))
    assert lines[0].startswith("dc tools: 3 of the dc-* tools on PATH")
    assert lines[1] == f"  dc-live dc-log dc-say: in {local}, links into {os.path.realpath(old)}"


def test_a_foreign_tool_with_no_copy_of_ours_still_gives_a_fix(tmp_path):
    """No home holds this install's copy (a broken install): the stale one
    is still named, and the fix says to remove it; no folder to put first."""
    stale = _tool(tmp_path / "old" / "dc-live")
    lines = dctools.status_lines(dctools.on_path(_path(stale.parent), dirs=[tmp_path / "none"]))
    assert not any("this install's copies" in ln for ln in lines)
    assert lines[-1] == f"  Fix: remove or repoint those in {stale.parent}."


# ---- no false alarm -----------------------------------------------------------

def test_no_alarm_for_the_console_script_route(kit):
    """The scripts folder itself on PATH: every tool is this install's."""
    hits = dctools.on_path(_path(kit), dirs=[kit])
    assert {h.name for h in hits} == dctools.tools() and not dctools.foreign(hits)
    assert dctools.status_lines(hits) == [
        f"dc tools: ok, all {len(dctools.tools())} on PATH are this install's ({kit})"]


def test_no_alarm_for_a_uv_tool_link_into_the_scripts_folder(tmp_path, kit):
    """`uv tool install` links ~/.local/bin/dc-* to the tool env's bin/: the
    link resolves to this install's console script, so it is mine."""
    local = tmp_path / "home" / ".local" / "bin"
    for name in dctools.tools():
        _link(local / name, kit / name)
    hits = dctools.on_path(_path(local), dirs=[kit])
    assert not dctools.foreign(hits) and len(hits) == len(dctools.tools())
    assert dctools.status_lines(hits)[0].startswith("dc tools: ok, all ")


def test_no_alarm_for_the_checkout_bin_route(tmp_path):
    """The checkout's bin/ on PATH: dcbin/'s files resolve into it, so the
    default homes() count it as this install's."""
    hits = dctools.on_path(_path(BIN))
    assert {h.name for h in hits} == dctools.tools() and not dctools.foreign(hits)


def test_no_alarm_for_a_link_into_the_checkout_bin(tmp_path):
    """A hand-made link to the checkout's bin/dc-live is the checkout's copy."""
    local = _link(tmp_path / "local" / "dc-live", BIN / "dc-live")
    hits = dctools.on_path(_path(local.parent), dirs=[tmp_path / "none", dctools.DIR])
    assert [(h.name, h.mine) for h in hits] == [("dc-live", True)]


def test_the_default_homes_are_this_interpreters_scripts_and_dcbin():
    import sysconfig
    assert dctools.homes() == [Path(sysconfig.get_path("scripts")), dctools.DIR]


def test_a_name_absent_from_path_is_no_alarm(tmp_path, kit):
    """Nothing on PATH: no hit, and the line says so plainly, with no fix."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert dctools.on_path(_path(empty), dirs=[kit]) == []
    assert dctools.status_lines([]) == [
        f"dc tools: none of the {len(dctools.tools())} dc-* tools is on PATH"]


def test_some_names_absent_and_the_rest_ours_is_ok(tmp_path, kit):
    part = tmp_path / "part"
    for name in ("dc-live", "dc-say"):
        _link(part / name, kit / name)
    lines = dctools.status_lines(dctools.on_path(_path(part), dirs=[kit]))
    assert lines == [f"dc tools: ok, 2 of {len(dctools.tools())} on PATH are this install's ({part})"]


def test_a_non_tool_dc_name_on_path_is_not_checked(tmp_path, kit):
    """dc-env is a library, never run by name, and bridge-check never ships:
    a copy of either on PATH is not this check's business."""
    other = tmp_path / "other"
    _tool(other / "dc-env")
    _tool(other / "dc-unknown")
    assert dctools.on_path(_path(other), dirs=[kit]) == []


# ---- the surface: sessionphone bot status ------------------------------------

@pytest.mark.usefixtures("rig")
def test_bot_status_names_a_foreign_dc_live_and_keeps_its_exit_code(tmp_path, kit, monkeypatch):
    stale = _tool(tmp_path / "old-checkout" / "bin" / "dc-live")
    local = _link(tmp_path / "local" / "dc-live", stale)
    monkeypatch.setenv("PATH", _path(local.parent, kit, "/usr/bin", "/bin"))
    monkeypatch.setattr(dctools, "homes", lambda: [kit])
    st = CliRunner().invoke(cli, ["bot", "status"])
    lines = st.output.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("dc tools:"))
    assert "NOT this install's" in lines[i]
    assert lines[i + 1] == f"  dc-live: in {local.parent}, links into {os.path.dirname(os.path.realpath(stale))}"
    running = service.status()["pid"] is not None
    assert st.exit_code == (0 if running else 1), st.output
    assert lines[-1].startswith("last")      # the log's last line stays last


@pytest.mark.usefixtures("rig")
def test_bot_status_says_ok_when_the_tools_on_path_are_ours(kit, monkeypatch):
    monkeypatch.setenv("PATH", _path(kit, "/usr/bin", "/bin"))
    monkeypatch.setattr(dctools, "homes", lambda: [kit])
    st = CliRunner().invoke(cli, ["bot", "status"])
    dc = [ln for ln in st.output.splitlines() if ln.startswith("dc tools:")]
    assert dc == [f"dc tools: ok, all {len(dctools.tools())} on PATH are this install's ({kit})"]
    assert "NOT this install's" not in st.output

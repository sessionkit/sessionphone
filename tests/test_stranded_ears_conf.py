"""An ears.json left in the old settings folder is named, never read.

Up to 0.1.0 ears.json lived in the home directory's old settings folder
(``recording.LEGACY_EARS_CONF``). The bot reads only
``recording.EARS_CONF_DEFAULT`` now. When ``$EARS_CONF`` is unset or empty,
the new file is absent and the old one exists, the warning for the missing
file is ONE line naming both paths, and saying the kit's host step moves it.
The old file is only stat-ed: never opened, never written, never a fallback.

Every case runs with HOME at a tmp dir, so no test sees the seat's files.
"""
from __future__ import annotations

import json
import os

import pytest

from sessionphone.bridge import recording

#: The old file, relative to HOME, spelled out here so the test checks the
#: constant rather than reusing it.
OLD = ".settings/ears/ears.json"
NEW = ".sessionkit/voice/settings/ears.json"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("EARS_CONF", raising=False)
    return h


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def tree(root):
    """Every file under root with its bytes: what a read must leave alone."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def all_warnings():
    """Every warning the three warning sites raise for one read: resolve_all,
    resolve_notices and a Switch (read twice, as a bot reads at each call)."""
    _, _, w1 = recording.resolve_all()
    _, _, w2 = recording.resolve_notices()
    said = []
    sw = recording.Switch(None, log=said.append)
    sw.record()
    sw.notice()
    sw.days()
    return w1, w2, said


def naming_old(lines, home):
    return [m for m in lines if str(home / OLD) in m]


def test_the_constant_is_the_old_path_and_the_default_the_new(home):
    assert os.path.expanduser(recording.LEGACY_EARS_CONF) == str(home / OLD)
    assert os.path.expanduser(recording.EARS_CONF_DEFAULT) == str(home / NEW)


def test_absent_and_absent_says_no_file_and_no_stranded_line(home):
    for lines in all_warnings():
        assert lines == [f"{home / NEW}: no file: the file sets nothing"]
        assert naming_old(lines, home) == []


def test_absent_new_and_present_old_is_one_line_naming_both(home):
    put(home / OLD, {"record": True, "record_days": 0})
    before = tree(home)
    w1, w2, said = all_warnings()
    for lines in (w1, w2, said):
        assert len(lines) == 1, lines
        line = lines[0]
        assert str(home / NEW) in line and str(home / OLD) in line
        assert "host step" in line
        assert "\n" not in line
    # the old file is never read: its "record": true does not reach the switch
    rec, days, _ = recording.resolve_all()
    assert (rec.on, rec.source, days.value, days.source) == (False, "default", 7, "default")
    # and nothing is written: no new file, the old one untouched
    assert tree(home) == before
    assert not (home / NEW).exists()


def test_a_switch_says_the_stranded_line_once_per_process(home):
    put(home / OLD, {"record": True})
    said = []
    sw = recording.Switch(None, log=said.append)
    for _ in range(5):
        sw()
        sw.days()
        sw.spoken_notice()
    # the old file changes: it is not followed, and nothing is said again
    put(home / OLD, {"record": False, "x": 1})
    sw()
    assert len(naming_old(said, home)) == 1
    assert len(said) == 1


def test_present_new_says_nothing(home):
    put(home / OLD, {"record": False})
    put(home / NEW, {"record": True})
    for lines in all_warnings():
        assert lines == []
    rec, _, _ = recording.resolve_all()
    assert (rec.on, rec.source) == (True, "file")


def test_a_broken_new_file_is_named_alone(home):
    """The new file is there but broken: its own warning, and no stranded line."""
    put(home / OLD, {"record": True})
    (home / NEW).parent.mkdir(parents=True)
    (home / NEW).write_text("{nope")
    for lines in all_warnings():
        assert lines == [f"{home / NEW}: not JSON: the file sets nothing"]


def test_ears_conf_set_means_no_stranded_line(home, tmp_path, monkeypatch):
    put(home / OLD, {"record": True})
    chosen = tmp_path / "chosen.json"
    monkeypatch.setenv("EARS_CONF", str(chosen))
    for lines in all_warnings():
        assert lines == [f"{chosen}: no file: the file sets nothing"]
        assert naming_old(lines, home) == []


def test_ears_conf_set_to_the_default_path_still_means_no_stranded_line(home, monkeypatch):
    """$EARS_CONF set at all is the user's choice, even when it names the default."""
    put(home / OLD, {"record": True})
    monkeypatch.setenv("EARS_CONF", str(home / NEW))
    for lines in all_warnings():
        assert naming_old(lines, home) == []


def test_empty_ears_conf_is_unset(home, monkeypatch):
    put(home / OLD, {"record": True})
    monkeypatch.setenv("EARS_CONF", "")
    for lines in all_warnings():
        assert len(lines) == 1 and len(naming_old(lines, home)) == 1


def test_an_old_folder_without_the_file_is_not_stranded(home):
    (home / OLD).parent.mkdir(parents=True)
    for lines in all_warnings():
        assert naming_old(lines, home) == []

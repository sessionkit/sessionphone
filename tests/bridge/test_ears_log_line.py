"""PHONE-152: setup() logs the ears-missing line only when it matters.

The `ears.previews` import in calls.py feeds turn PREVIEWS (DC_PREVIEWS) and
sits beside the in-process ears leg (DC_EARS); both are off by default.
By default the call is heard on the energy-chunk STT path. The old line, "ears is not
installed", was logged on every start whatever those flags said, and a trial
read it as "no hearing". Now: nothing about ears with both flags off; with
either on, a line that names the asked-for feature. With previews alone it says
hearing is unaffected; with DC_EARS=1 it says calls are NOT heard, since the
in-process leg needs ears to start at all. No call, no network: the
manager's start() is stubbed.
"""
import types

import pytest

from sessionphone.bridge import calls

MISSING = "No module named 'ears'"


def _setup(monkeypatch, tmp_path, *, previews, ears, missing):
    monkeypatch.setattr(calls, "PREVIEWS", previews)
    monkeypatch.setattr(calls, "EARS", ears)
    monkeypatch.setattr(calls, "EARS_MISSING", missing)
    monkeypatch.setattr(calls, "CALLS_DIR", tmp_path / "calls")
    monkeypatch.setattr(calls, "_manager", types.SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(calls, "_hooks", None)
    logged = []
    calls.setup(logged.append, lambda p: "", lambda *a, **k: None)
    # the recording switch's own start lines name ears.json (its file): not this line
    return [line for line in logged if "ears" in line.lower() and not line.startswith("CALL recording:")]


def test_defaults_with_ears_missing_log_no_ears_line(monkeypatch, tmp_path):
    assert _setup(monkeypatch, tmp_path, previews=False, ears=False, missing=MISSING) == []


def test_previews_on_with_ears_missing_names_previews(monkeypatch, tmp_path):
    lines = _setup(monkeypatch, tmp_path, previews=True, ears=False, missing=MISSING)
    assert len(lines) == 1
    line = lines[0]
    assert "previews" in line.lower()
    assert "DC_PREVIEWS" in line
    assert "unaffected" in line.lower() and "hearing" in line.lower()
    assert MISSING in line
    assert "DC_EARS=1" not in line          # only the asked-for feature is named


def test_ears_on_with_ears_missing_names_the_ears_leg(monkeypatch, tmp_path):
    lines = _setup(monkeypatch, tmp_path, previews=False, ears=True, missing=MISSING)
    assert len(lines) == 1
    line = lines[0]
    assert "DC_EARS=1" in line
    assert "in-process ears leg" in line
    assert "DC_PREVIEWS" not in line
    # DC_EARS=1 without ears really does stop the call being heard
    # (_listen_ears imports ears.cli), so the line must not say otherwise
    assert "unaffected" not in line.lower()
    assert "NOT heard" in line and "unset DC_EARS" in line


def test_both_on_with_ears_missing_names_both_once(monkeypatch, tmp_path):
    lines = _setup(monkeypatch, tmp_path, previews=True, ears=True, missing=MISSING)
    assert len(lines) == 1
    assert "DC_PREVIEWS" in lines[0] and "DC_EARS=1" in lines[0]
    assert " are off" in lines[0]
    assert "unaffected" not in lines[0].lower()   # the ears leg is on: hearing IS affected


def test_line_helper_matches_what_setup_logs(monkeypatch):
    monkeypatch.setattr(calls, "EARS_MISSING", MISSING)
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls, "PREVIEWS", False)
    assert calls.ears_missing_line() is None
    monkeypatch.setattr(calls, "PREVIEWS", True)
    assert calls.ears_missing_line().startswith(f"ears is not installed ({MISSING}), so turn previews")


@pytest.mark.parametrize("previews,ears", [(False, False), (True, False), (False, True), (True, True)])
def test_ears_present_logs_no_ears_line(monkeypatch, tmp_path, previews, ears):
    assert _setup(monkeypatch, tmp_path, previews=previews, ears=ears, missing=None) == []


def test_setup_still_starts_the_manager(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(calls, "PREVIEWS", False)
    monkeypatch.setattr(calls, "EARS", False)
    monkeypatch.setattr(calls, "EARS_MISSING", MISSING)
    monkeypatch.setattr(calls, "CALLS_DIR", tmp_path / "calls")
    monkeypatch.setattr(calls, "_manager", types.SimpleNamespace(start=lambda: started.append(1)))
    monkeypatch.setattr(calls, "_hooks", None)
    calls.setup(lambda m: None, lambda p: "", lambda *a, **k: None)
    assert started == [1]
    assert (tmp_path / "calls").is_dir()

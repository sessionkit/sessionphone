"""Webxdc taps go to a file, not mail, when DC_XDC_IN is set (PHONE-22, gap 1).

The bot writes each tap as one JSON line to xdc-in.jsonl and mails
nothing. Three readers follow that file, so the line must parse the way they
parse it. Their parse steps are copied below, verbatim in substance, with
file:line, so a change to the record shape breaks a test here:

  settings  the settings app, apply.py:870-887
  notify    the notify app, apply.py:220-236
  desk      the desk app, answer.py:26-33 (taps) and 55-64 (write_up)

With DC_XDC_IN unset (a bare install), taps are
mailed as before. DC_XDC_MATCH narrows what counts as a tap, for either sink.

Fakes only: no account, no rpc-server, nothing real.
"""
import json
import os
import subprocess
import sys

import pytest

from sessionphone.bridge import bot


class FakeMessage:
    """get_webxdc_status_updates(n) returns the updates with serial > n."""

    def __init__(self, updates, chat_id=7):
        self.updates = updates
        self.chat_id = chat_id

    def get_webxdc_status_updates(self, last_known_serial=0):
        return [u for u in self.updates if u["serial"] > last_known_serial]

    def get_snapshot(self):
        return type("Snap", (), {"chat_id": self.chat_id})()


class FakeAccount:
    def __init__(self, msg):
        self.msg = msg

    def get_message_by_id(self, mid):
        return self.msg


class Event:
    def __init__(self, msg_id, serial):
        self.kind = "WebxdcStatusUpdate"
        self.msg_id = msg_id
        self.status_update_serial = serial


SET_TAP = {"from": "xdc", "kind": "set", "path": ["ears", "listen"], "value": True, "by": "Bob"}
DESK_TAP = {"kind": "desk-answer", "type": "question", "ask": "1907238896.1001.1@example.org",
            "answer": {"text": "sessionvoice maybe?"}}
TM_BEAT = {"app": "tm", "at": 1907238896, "kind": "beat"}           # an app with no "from"
METER_STATE = {"kind": "state", "app": "notify", "global": {"allow": True}}


def _feed(payloads, first=100):
    """Updates carrying these payloads at serials first, first+1, ..."""
    return [{"serial": first + i, "payload": p, "info": ""} for i, p in enumerate(payloads)]


def _run(payloads, mid=1001, first=100):
    """Feed each update to xdc_update as its own event, the way the core does."""
    msg = FakeMessage(_feed(payloads, first))
    for i in range(len(payloads)):
        bot.xdc_update(FakeAccount(msg), Event(mid, first + i))


@pytest.fixture
def sink(monkeypatch, tmp_path):
    """Fresh module state with the file sink on and no filter."""
    path = tmp_path / "bot" / "xdc-in.jsonl"
    monkeypatch.setattr(bot, "XDC_SEEN", {})
    monkeypatch.setattr(bot, "XDC_SENT", {})
    monkeypatch.setattr(bot, "XDC_IN", path)
    monkeypatch.setattr(bot, "XDC_MATCH", [])
    mailed, logged = [], []
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: mailed.append(a))
    monkeypatch.setattr(bot, "log", logged.append)
    return path, mailed, logged


def _lines(path):
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


# --- the three readers' parse steps -------------------------------------------------


def settings_or_notify_reads(line, msgs):
    """settings/apply.py:870-887 == notify-xdc/apply.py:220-236: the tap it applies, or None."""
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    p = rec.get("payload") or {}
    if str(rec.get("msg_id")) not in msgs or p.get("kind") != "set":
        return None
    return {"serial": rec.get("serial"), "by": p.get("by"), "path": list(p.get("path") or []),
            "value": p.get("value")}


def desk_reads(text):
    """desk/answer.py:26-33 taps(), then the fields write_up() (55-64) indexes."""
    out = []
    for line in text.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("payload", {}).get("kind") == "desk-answer":
            p = j["payload"]
            out.append((j["ts"], j["serial"], p["ask"], p["type"], p["answer"]))
    return out


# --- file sink ------------------------------------------------------------------------


def test_a_tap_is_written_to_the_file_and_not_mailed(sink):
    path, mailed, _ = sink
    _run([SET_TAP], first=7)
    assert mailed == [], "DC_XDC_IN is set: a tap must never be mailed"
    [rec] = _lines(path)
    # v1: the four frozen keys first, then chat_id (contract §4.2); no info on this update
    assert list(rec) == ["ts", "msg_id", "serial", "payload", "chat_id"]
    assert rec["chat_id"] == 7
    assert rec["msg_id"] == 1001 and rec["serial"] == 7 and rec["payload"] == SET_TAP
    # the bot's ts: time.strftime("%Y-%m-%dT%H:%M:%S%z"), e.g. 2030-06-09T12:34:56+0000
    assert len(rec["ts"]) == 24 and rec["ts"][10] == "T" and rec["ts"][19] in "+-"


def test_the_settings_and_notify_readers_parse_it(sink):
    path, _, _ = sink
    _run([SET_TAP], first=7)
    [line] = path.read_text().splitlines()
    assert settings_or_notify_reads(line, ["1001"]) == {
        "serial": 7, "by": "Bob", "path": ["ears", "listen"], "value": True}
    assert settings_or_notify_reads(line, ["999"]) is None     # another app's file line


def test_the_desk_reader_parses_it(sink):
    path, _, _ = sink
    _run([DESK_TAP], first=27)
    [(ts, serial, ask, typ, answer)] = desk_reads(path.read_text())
    assert serial == 27 and ask == DESK_TAP["ask"] and typ == "question"
    assert answer == {"text": "sessionvoice maybe?"} and ts


def test_lines_append_one_per_tap_and_the_file_is_private(sink):
    path, mailed, _ = sink
    _run([SET_TAP, DESK_TAP, SET_TAP])
    assert [r["serial"] for r in _lines(path)] == [100, 101, 102]
    assert mailed == []
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o077) == 0


def test_an_echo_is_not_written(sink):
    path, _, logged = sink
    bot.xdc_remember(1001, METER_STATE)            # our own push
    _run([METER_STATE, SET_TAP])
    assert [r["payload"] for r in _lines(path)] == [SET_TAP]
    assert any("XDC ECHO" in ln for ln in logged)


def test_a_failed_write_is_logged_and_never_mailed(sink, monkeypatch, tmp_path):
    _, mailed, logged = sink
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(bot, "XDC_IN", blocker / "xdc-in.jsonl")
    _run([SET_TAP, DESK_TAP])
    assert mailed == []
    assert sum("FAILED" in ln for ln in logged) == 2   # the second tap is still tried


def test_a_large_tap_is_one_whole_line_from_one_write(sink, monkeypatch):
    # a reader tailing the file must never see half a record: the line goes
    # down in ONE os.write, even past a buffered file's 8K chunk
    path, mailed, _ = sink
    writes, real_write = [], os.write
    monkeypatch.setattr(bot.os, "write", lambda fd, b: (writes.append(len(b)), real_write(fd, b))[1])
    big = dict(SET_TAP, value="x" * 70_000)
    _run([big])
    assert len(writes) == 1 and writes[0] == path.stat().st_size
    assert [r["payload"] for r in _lines(path)] == [big] and mailed == []


def test_a_short_write_is_logged_as_failed_and_never_mailed(sink, monkeypatch):
    _, mailed, logged = sink
    monkeypatch.setattr(bot.os, "write", lambda fd, b: len(b) - 1)
    _run([SET_TAP])
    assert mailed == []
    assert any("FAILED" in ln and "short write" in ln for ln in logged)


def test_restart_then_one_tap_writes_one_line(sink):
    # gap 2 through the file sink: N prior updates, a fresh process, one tap
    path, mailed, _ = sink
    history = [SET_TAP] * 500
    msg = FakeMessage(_feed(history + [DESK_TAP], first=1))
    bot.xdc_update(FakeAccount(msg), Event(1001, 501))
    assert [r["serial"] for r in _lines(path)] == [501] and mailed == []


# --- the filter: DC_XDC_MATCH -------------------------------------------------------


def test_no_filter_writes_every_non_echo_update(sink):
    path, _, _ = sink
    _run([SET_TAP, TM_BEAT, "a bare string", [1, 2]])
    assert [r["payload"] for r in _lines(path)] == [SET_TAP, TM_BEAT, "a bare string", [1, 2]]


def test_a_filter_keeps_only_the_taps_and_desk_answers_it_names(sink, monkeypatch):
    path, _, logged = sink
    monkeypatch.setattr(bot, "XDC_MATCH", bot.xdc_parse_match("from=xdc,kind=desk-answer")[0])
    _run([SET_TAP, METER_STATE, DESK_TAP, TM_BEAT, "a bare string", [1, 2],
          {"from": "phone"}, {"kind": None}])
    assert [r["payload"] for r in _lines(path)] == [SET_TAP, DESK_TAP]
    # METER_STATE is rule 3's (XDC STATE), before the filter ever sees it
    assert sum("XDC SKIP" in ln for ln in logged) == 5
    assert sum("XDC STATE" in ln for ln in logged) == 1
    # every line is a JSON object payload, so no reader's payload.get() can break
    assert all(isinstance(r["payload"], dict) for r in _lines(path))


def test_the_filter_narrows_mail_too(monkeypatch):
    mailed = []
    monkeypatch.setattr(bot, "XDC_SEEN", {})
    monkeypatch.setattr(bot, "XDC_SENT", {})
    monkeypatch.setattr(bot, "XDC_IN", None)
    monkeypatch.setattr(bot, "XDC_MATCH", [("from", "xdc")])
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: mailed.append(a))
    monkeypatch.setattr(bot, "log", lambda *_: None)
    _run([TM_BEAT, SET_TAP])
    assert len(mailed) == 1 and '"from": "xdc"' in mailed[0][3]


@pytest.mark.parametrize("spec, pairs, bad", [
    (None, [], []),
    ("", [], []),
    ("from=xdc", [("from", "xdc")], []),
    (" from = xdc , kind=desk-answer ,", [("from", "xdc"), ("kind", "desk-answer")], []),
    ("from=xdc,oops,=x", [("from", "xdc")], ["oops", "=x"]),
    ("kind=", [("kind", "")], []),
    ("from=xdc,from=desk", [("from", "xdc"), ("from", "desk")], []),     # a key may repeat
])
def test_parse_match(spec, pairs, bad):
    assert bot.xdc_parse_match(spec) == (pairs, bad)


def test_a_repeated_key_matches_with_any(monkeypatch):
    # contract §4.3 rule 4: from=xdc,from=desk matches either value, not both at once
    monkeypatch.setattr(bot, "XDC_MATCH", bot.xdc_parse_match("from=xdc,from=desk")[0])
    assert bot.xdc_is_tap({"from": "xdc"}) and bot.xdc_is_tap({"from": "desk"})
    assert not bot.xdc_is_tap({"from": "phone"}) and not bot.xdc_is_tap({"kind": "set"})


def test_the_filter_compares_values_as_strings(monkeypatch):
    # string values compare by ==; non-strings match their Python spelling
    monkeypatch.setattr(bot, "XDC_MATCH", [("from", "xdc"), ("n", "1"), ("x", "None")])
    assert bot.xdc_is_tap({"from": "xdc"})
    assert not bot.xdc_is_tap({"from": "XDC"})
    assert bot.xdc_is_tap({"n": 1}) and bot.xdc_is_tap({"x": None})
    assert not bot.xdc_is_tap({"y": None})       # an absent key never matches


# --- no DC_XDC_IN: mail, exactly as before (a bare install) -------------------------------


def test_without_dc_xdc_in_every_tap_is_mailed_and_no_file_is_made(monkeypatch, tmp_path):
    mailed = []
    monkeypatch.setattr(bot, "XDC_SEEN", {})
    monkeypatch.setattr(bot, "XDC_SENT", {})
    monkeypatch.setattr(bot, "XDC_IN", None)
    monkeypatch.setattr(bot, "XDC_MATCH", [])
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: mailed.append(a))
    monkeypatch.setattr(bot, "log", lambda *_: None)
    monkeypatch.chdir(tmp_path)
    _run([TM_BEAT, SET_TAP])
    assert [m[:3] for m in mailed] == [("webxdc", 7, "webxdc")] * 2
    assert mailed[0][3] == f"(tapped in the app)  {json.dumps(TM_BEAT)}"
    assert list(tmp_path.iterdir()) == []


def _config_in_a_fresh_process(tmp_path, **env):
    """Import the bot the way launchd does, and report what it read."""
    full = {k: v for k, v in os.environ.items()
            if not k.startswith(("DC_", "PIP_CHAT_"))}
    full.update(DC_ROOT=str(tmp_path / "root"), DC_CALLS="off", **env)
    out = subprocess.run(
        [sys.executable, "-c",
         ("import json; from sessionphone.bridge import bot; "
          "print(json.dumps([None if bot.XDC_IN is None else str(bot.XDC_IN), bot.XDC_MATCH]))")],
        env=full, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_config_is_read_from_the_environment(tmp_path):
    # a plist that sets neither: taps stay mail
    assert _config_in_a_fresh_process(tmp_path) == [None, []]
    home = os.path.expanduser("~")
    assert _config_in_a_fresh_process(
        tmp_path, DC_XDC_IN="~/x/xdc-in.jsonl", DC_XDC_MATCH="from=xdc,kind=desk-answer") == [
        f"{home}/x/xdc-in.jsonl", [["from", "xdc"], ["kind", "desk-answer"]]]
    # the old PIP_CHAT_ spelling works as it does for every other setting
    assert _config_in_a_fresh_process(tmp_path, PIP_CHAT_XDC_IN="/t/in.jsonl")[0] == "/t/in.jsonl"
    # empty counts as unset (dc_env.env)
    assert _config_in_a_fresh_process(tmp_path, DC_XDC_IN="")[0] is None


# --- rule 3: state updates are dropped at source, on both sinks (contract §4.3) ----------

X_STATE = {"kind": "desk-state", "hash": "f1a6", "asks": []}        # the desk app's push
STATEFUL = {"kind": "stateful", "from": "xdc"}                         # a tap: not "-state"


@pytest.mark.parametrize("payload, state", [
    ({"kind": "state"}, True),
    ({"kind": "x-state"}, True),
    (METER_STATE, True),
    (X_STATE, True),
    ({"kind": "stateful"}, False),
    ({"kind": "State"}, False),             # exact, case-sensitive
    ({"kind": "statex-"}, False),
    ({"kind": "-states"}, False),
    ({"kind": None}, False),
    ({"kind": ["state"]}, False),           # only a string kind
    ({"type": "desk-state"}, False),        # the contract keys on "kind" only
    ({"app": "notify"}, False),
    ("state", False),                       # not an object
    (["kind", "state"], False),
    (None, False),
])
def test_what_is_a_state_update(payload, state):
    assert bot.xdc_is_state(payload) is state


def test_state_is_dropped_from_the_file_and_stateful_is_not(sink):
    path, mailed, logged = sink
    _run([{"kind": "state"}, {"kind": "x-state"}, STATEFUL, SET_TAP])
    assert [r["payload"] for r in _lines(path)] == [STATEFUL, SET_TAP]
    assert mailed == []
    assert sum("XDC STATE" in ln for ln in logged) == 2


def test_state_is_dropped_from_mail_and_stateful_is_not(monkeypatch):
    mailed, logged = [], []
    monkeypatch.setattr(bot, "XDC_SEEN", {})
    monkeypatch.setattr(bot, "XDC_SENT", {})
    monkeypatch.setattr(bot, "XDC_IN", None)
    monkeypatch.setattr(bot, "XDC_MATCH", [])
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: mailed.append(a))
    monkeypatch.setattr(bot, "chat_channel", lambda *a: None)
    monkeypatch.setattr(bot, "log", logged.append)
    _run([{"kind": "state"}, {"kind": "x-state"}, STATEFUL])
    assert len(mailed) == 1 and '"kind": "stateful"' in mailed[0][3]
    assert sum("XDC STATE" in ln for ln in logged) == 2


def test_a_state_update_this_process_never_pushed_is_dropped(sink):
    # another process or device on the account, or a push made before a restart:
    # not in XDC_SENT, so rule 2 cannot see it; rule 3 drops it anyway
    path, mailed, logged = sink
    assert bot.XDC_SENT == {}
    _run([X_STATE, METER_STATE, DESK_TAP])
    assert [r["payload"] for r in _lines(path)] == [DESK_TAP] and mailed == []
    assert not any("XDC ECHO" in ln for ln in logged)
    assert sum("XDC STATE" in ln for ln in logged) == 2


def test_our_own_state_push_is_still_an_echo_first(sink):
    # rule 2 runs before rule 3: a push this process made is logged XDC ECHO, once
    path, _, logged = sink
    bot.xdc_remember(1001, X_STATE)
    _run([X_STATE])
    assert _lines(path) == []
    xdc = [ln for ln in logged if ln.startswith("XDC ")]
    assert len(xdc) == 1 and xdc[0].startswith("XDC ECHO")      # never also XDC STATE


def test_no_match_setting_lets_a_state_update_through(sink, monkeypatch):
    # rule 3 runs BEFORE DC_XDC_MATCH and cannot be turned off
    path, _, logged = sink
    monkeypatch.setattr(bot, "XDC_MATCH", bot.xdc_parse_match("kind=desk-state,kind=state")[0])
    _run([X_STATE, METER_STATE])
    assert _lines(path) == []
    assert sum("XDC STATE" in ln for ln in logged) == 2
    assert not any("XDC SKIP" in ln or "XDC TAP" in ln for ln in logged)


def test_the_public_profile_logs_no_state_payload(sink, monkeypatch, tmp_path):
    _, _, logged = sink
    monkeypatch.setattr(bot, "PROFILE", "public")
    # never the real list: were rule 3 to regress, the inbound gate would record here
    monkeypatch.setenv("DC_CHANNELS_FILE", str(tmp_path / "public" / "channels.txt"))
    _run([dict(X_STATE, secret="a stranger's words")])
    [line] = [ln for ln in logged if "XDC STATE" in ln]
    assert "stranger" not in line and "desk-state" not in line
    assert not (tmp_path / "public").exists()       # dropped before the gate: nothing recorded


# --- line format v1: four frozen keys, chat_id and info added (contract §4.2) ---------------


def test_a_line_carries_chat_id_and_the_info_line_when_there_is_one(sink):
    path, _, _ = sink
    msg = FakeMessage([{"serial": 100, "payload": DESK_TAP, "info": "Bob answered"},
                       {"serial": 101, "payload": SET_TAP, "info": ""}], chat_id=43)
    for s in (100, 101):
        bot.xdc_update(FakeAccount(msg), Event(1001, s))
    first, second = _lines(path)
    assert list(first) == ["ts", "msg_id", "serial", "payload", "chat_id", "info"]
    assert first["chat_id"] == 43 and first["info"] == "Bob answered"
    assert list(second) == ["ts", "msg_id", "serial", "payload", "chat_id"]   # no info, no key


def test_a_chat_id_that_cannot_be_read_costs_the_key_not_the_tap(sink):
    path, mailed, logged = sink

    class NoSnap(FakeMessage):
        def get_snapshot(self):
            raise RuntimeError("core gone")

    bot.xdc_update(FakeAccount(NoSnap(_feed([SET_TAP]))), Event(1001, 100))
    [rec] = _lines(path)
    assert list(rec) == ["ts", "msg_id", "serial", "payload"] and rec["payload"] == SET_TAP
    assert mailed == [] and any("no chat id" in ln and "core gone" in ln for ln in logged)


def test_the_readers_parse_a_line_with_the_added_keys(sink):
    path, _, _ = sink
    msg = FakeMessage([{"serial": 7, "payload": SET_TAP, "info": "set ears.listen"},
                       {"serial": 8, "payload": DESK_TAP, "info": "answered"}], chat_id=43)
    for s in (7, 8):
        bot.xdc_update(FakeAccount(msg), Event(1001, s))
    text = path.read_text()
    assert all({"chat_id", "info"} <= set(r) for r in _lines(path))
    assert settings_or_notify_reads(text.splitlines()[0], ["1001"]) == {
        "serial": 7, "by": "Bob", "path": ["ears", "listen"], "value": True}
    [(_, serial, ask, typ, answer)] = desk_reads(text)
    assert serial == 8 and ask == DESK_TAP["ask"] and typ == "question"
    assert answer == {"text": "sessionvoice maybe?"}

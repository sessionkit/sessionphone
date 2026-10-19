"""Realtime data FROM a webxdc app, written to DC_XDC_RT_IN (PHONE-165 part 3).

The back channel: a WEBXDC_REALTIME_DATA event from an app this bot joined
becomes one JSON line in the sibling file DC_XDC_RT_IN, and never a mail.
xdc-in.jsonl (DC_XDC_IN) is not touched: its v1 line freezes `serial`, and a
realtime datum has none (design.md section 5; Q3 ruled: the sibling file).

Fakes only (AGENTS.md): no account, no rpc-server, no relay.
"""
import json
import os
import stat
import subprocess
import sys
import threading
import types

import pytest
import test_xdc_feed as feed_tests  # its Events loop and its main() rig
import test_xdc_realtime as rt_tests

from sessionphone import channels
from sessionphone.bridge import bot, xdc_feed, xdc_rt

MID = 844


# --- fakes -------------------------------------------------------------------

class Msg:
    """A webxdc msg: only its snapshot, counted (the one rpc the handler may make)."""

    def __init__(self, chat_id=42, fail=None):
        self.chat_id, self.fail, self.snaps = chat_id, fail, 0

    def get_snapshot(self):
        self.snaps += 1
        if self.fail is not None:
            raise self.fail
        return types.SimpleNamespace(chat_id=self.chat_id, view_type="Webxdc")


class Account:
    def __init__(self, msg=None):
        self.msg = msg if msg is not None else Msg()
        self.asked = []

    def get_message_by_id(self, mid):
        self.asked.append(mid)
        return self.msg


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def datum(payload, mid=MID):
    """The event as the client hands it: data is a list of uint8 (openrpc uint8[])."""
    raw = payload if isinstance(payload, (bytes, bytearray)) else xdc_rt.encode(payload)
    return types.SimpleNamespace(kind=bot.EventType.WEBXDC_REALTIME_DATA, msg_id=mid, data=list(raw))


@pytest.fixture
def rt_in(monkeypatch, tmp_path):
    """Fresh module state: the sink on, no filter, private profile, msg 844 joined."""
    path = tmp_path / "bot" / "xdc-rt-in.jsonl"
    xdc_in = tmp_path / "bot" / "xdc-in.jsonl"
    logged, mailed = [], []
    clock = Clock()
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: mailed.append(a))
    monkeypatch.setattr(bot, "XDC_RT_IN", path)
    monkeypatch.setattr(bot, "XDC_RT_IN_CLASH", None)
    monkeypatch.setattr(bot, "XDC_IN", xdc_in)
    monkeypatch.setattr(bot, "XDC_MATCH", [])
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setattr(bot, "XDC_RT_ADVERTISED", {MID: 0.0})
    for name in ("XDC_RT_SEQ", "XDC_RT_CHAT", "XDC_RT_GATE", "XDC_RT_RATE", "XDC_RT_NOTES"):
        monkeypatch.setattr(bot, name, {})
    monkeypatch.setattr(bot, "XDC_RT_CLOCK", clock)
    feed = bot.xdc_feed_make(tmp_path / xdc_feed.FILE, tmp_path / "heard", sender=feed_tests.SyncSender())
    monkeypatch.setattr(bot, "XDC_FEED", feed)
    return types.SimpleNamespace(path=path, xdc_in=xdc_in, logged=logged, mailed=mailed, clock=clock,
                                 feed=feed, account=Account())


def lines(path):
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def send(rt, *payloads, mid=MID):
    for p in payloads:
        bot.xdc_rt_in(rt.account, datum(p, mid))


# --- the line --------------------------------------------------------------------

def test_a_datum_from_a_joined_app_is_one_line_in_the_sibling_file(rt_in):
    send(rt_in, {"choice": "left"}, {"choice": "right"})
    got = lines(rt_in.path)
    assert [list(r) for r in got] == [["ts", "msg_id", "seq", "via", "payload", "chat_id"]] * 2
    assert [(r["msg_id"], r["seq"], r["via"], r["payload"], r["chat_id"]) for r in got] == [
        (MID, 1, "realtime", {"choice": "left"}, 42), (MID, 2, "realtime", {"choice": "right"}, 42)]
    assert all("serial" not in r for r in got), "a realtime datum has no serial: v1's frozen key stays out"
    assert rt_in.mailed == [], "realtime data is never mailed"
    assert not rt_in.xdc_in.exists(), "xdc-in.jsonl is untouched by realtime data"


def test_seq_counts_per_msg_and_the_snapshot_is_asked_once(rt_in, monkeypatch):
    monkeypatch.setitem(bot.XDC_RT_ADVERTISED, 845, 0.0)
    send(rt_in, {"n": 1}, {"n": 2})
    send(rt_in, {"n": 1}, mid=845)
    send(rt_in, {"n": 3})
    assert [(r["msg_id"], r["seq"]) for r in lines(rt_in.path)] == [(844, 1), (844, 2), (845, 1), (844, 3)]
    assert rt_in.account.msg.snaps == 2, "one chat-id snapshot per msg, cached"


def test_any_json_value_is_a_payload_and_utf8_is_kept(rt_in):
    send(rt_in, "kia ora", [1, 2], 7, None, {"text": "naïve café ✓"})
    assert [r["payload"] for r in lines(rt_in.path)] == ["kia ora", [1, 2], 7, None, {"text": "naïve café ✓"}]


def test_the_file_is_0600_in_a_0700_dir_one_write_per_line(rt_in, monkeypatch):
    writes = []
    real = os.write
    monkeypatch.setattr(bot.os, "write", lambda fd, b: writes.append(b) or real(fd, b))
    send(rt_in, {"big": "x" * 70000})
    assert len(writes) == 1 and writes[0].endswith(b"\n")
    assert stat.S_IMODE(rt_in.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(rt_in.path.parent.stat().st_mode) == 0o700


def test_the_line_has_no_chat_id_when_the_snapshot_fails_and_asks_again_a_minute_later(rt_in):
    rt_in.account.msg.fail = RuntimeError("gone")
    send(rt_in, {"a": 1}, {"a": 2})
    assert [list(r) for r in lines(rt_in.path)] == [["ts", "msg_id", "seq", "via", "payload"]] * 2
    assert rt_in.account.msg.snaps == 1, "a failed snapshot is asked again only after a minute"
    rt_in.account.msg.fail = None
    rt_in.clock.t += 60
    send(rt_in, {"a": 3})
    assert lines(rt_in.path)[-1]["chat_id"] == 42


def test_a_failed_write_is_logged_the_seq_is_not_spent_and_nothing_is_mailed(rt_in, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")
    monkeypatch.setattr(bot, "XDC_RT_IN", blocker / "xdc-rt-in.jsonl")
    send(rt_in, {"a": 1})
    assert [x for x in rt_in.logged if "FAILED" in x and "datum lost" in x]
    assert rt_in.mailed == [] and bot.XDC_RT_SEQ == {}
    monkeypatch.setattr(bot, "XDC_RT_IN", rt_in.path)
    send(rt_in, {"a": 2})
    assert lines(rt_in.path)[0]["seq"] == 1


# --- rule 1: only a msg this bot joined ---------------------------------------------

def test_a_msg_this_bot_never_joined_is_dropped_silently(rt_in):
    send(rt_in, {"move": "e4"}, {"move": "e5"}, mid=999)        # someone's game, in some chat
    assert not rt_in.path.exists()
    assert rt_in.logged == [], "no log line per datum for other apps' traffic"
    assert rt_in.account.asked == [], "no rpc for a msg that is not ours"


def test_a_msg_with_a_feed_bound_is_joined(rt_in, monkeypatch):
    monkeypatch.setattr(bot, "XDC_RT_ADVERTISED", {})
    send(rt_in, {"a": 1})
    assert not rt_in.path.exists()
    rt_in.feed.allowed = frozenset({42})
    rt_in.feed.thread = types.SimpleNamespace(is_alive=lambda: True)
    rt_in.feed.bind(MID, 42, Msg())
    send(rt_in, {"a": 2})
    assert [r["payload"] for r in lines(rt_in.path)] == [{"a": 2}]


def test_an_advertised_or_data_sent_msg_is_joined(rt_in, monkeypatch):
    """XDC_RT_ADVERTISED is how the bot knows: an advertise job sets it, and a
    data job advertises first (xdc_rt_send), so both mark the msg joined."""
    monkeypatch.setattr(bot, "XDC_RT_ADVERTISED", {})
    m = feed_tests.Msg(MID)
    bot.xdc_rt_send(m, MID, xdc_rt.encode({"hi": 1}))
    send(rt_in, {"back": 1})
    assert [r["payload"] for r in lines(rt_in.path)] == [{"back": 1}]


# --- rule 2: UTF-8 JSON only ----------------------------------------------------------

@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe\x00", b"", b"{\"a\": 1", b"[" * 200000])
def test_a_datum_that_is_not_utf8_json_is_skipped_naming_its_size_never_its_content(rt_in, raw):
    bot.xdc_rt_in(rt_in.account, datum(raw))
    assert not rt_in.path.exists()
    assert rt_in.logged == [f"XDC RT SKIP msg {MID}: not JSON ({len(raw)} bytes)"]


def test_an_event_with_no_data_is_skipped(rt_in):
    bot.xdc_rt_in(rt_in.account, types.SimpleNamespace(kind="x", msg_id=MID))
    assert rt_in.logged == [f"XDC RT SKIP msg {MID}: not JSON (0 bytes)"] and not rt_in.path.exists()


def test_skips_are_logged_once_per_10s_per_msg_and_reason_with_a_count(rt_in):
    for _ in range(5):
        bot.xdc_rt_in(rt_in.account, datum(b"nope"))
    assert len(rt_in.logged) == 1
    rt_in.clock.t += 10
    bot.xdc_rt_in(rt_in.account, datum(b"nope"))
    assert rt_in.logged[-1] == f"XDC RT SKIP msg {MID}: not JSON (4 bytes) (5 since this was last logged)"


# --- rule 3 and DC_XDC_MATCH, as for taps --------------------------------------------

def test_a_state_payload_is_dropped_and_its_content_not_logged(rt_in):
    send(rt_in, {"kind": "state", "secret": "s3"}, {"kind": "desk-state"}, {"kind": "stateful"})
    assert [r["payload"] for r in lines(rt_in.path)] == [{"kind": "stateful"}]
    assert any(x.startswith(f"XDC RT STATE msg {MID}") for x in rt_in.logged)
    assert not [x for x in rt_in.logged if "s3" in x]


def test_dc_xdc_match_filters_realtime_data_too(rt_in, monkeypatch):
    monkeypatch.setattr(bot, "XDC_MATCH", [("from", "xdc")])
    send(rt_in, {"from": "xdc", "n": 1}, {"from": "other", "n": 2}, "a bare string")
    assert [r["payload"] for r in lines(rt_in.path)] == [{"from": "xdc", "n": 1}]
    assert any(x.startswith(f"XDC RT SKIP msg {MID}: not a tap by DC_XDC_MATCH") for x in rt_in.logged)


# --- rule 4: the public profile's inbound gate, cached per msg -------------------------

def test_public_profile_gate_is_asked_once_per_minute_per_msg(rt_in, monkeypatch):
    asked = []
    verdict = {"ok": False}
    monkeypatch.setattr(bot, "PROFILE", "public")
    monkeypatch.setattr(bot, "inbound_ok", lambda account, chat_id, kind, *a, **k:
                        asked.append((chat_id, kind)) or verdict["ok"])
    send(rt_in, *({"n": i} for i in range(10)))
    assert asked == [(42, "webxdc-realtime")], "a rejection is asked and recorded once, not per datum"
    assert not rt_in.path.exists()
    verdict["ok"] = True
    rt_in.clock.t += 59
    send(rt_in, {"n": 10})
    assert len(asked) == 1 and not rt_in.path.exists(), "the cached rejection holds for the minute"
    rt_in.clock.t += 1
    send(rt_in, {"n": 11}, {"n": 12})
    assert len(asked) == 2
    assert [r["payload"] for r in lines(rt_in.path)] == [{"n": 11}, {"n": 12}]


def test_public_profile_unlisted_chat_is_recorded_once_in_rejected_jsonl(rt_in, monkeypatch, tmp_path):
    """The real inbound_ok, over a list that does not hold chat 42: one record, kind webxdc-realtime."""
    lst = rt_tests.listing(tmp_path, 7)
    monkeypatch.setattr(bot, "PROFILE", "public")
    monkeypatch.setattr(bot.channels, "load", lambda *a, **k: lst)
    send(rt_in, {"secret": "do not log me"}, {"secret": "nor me"})
    recs = [json.loads(ln) for ln in channels.rejected_path(lst.path).read_text().splitlines()]
    assert len(recs) == 1 and recs[0]["kind"] == "webxdc-realtime" and recs[0]["chat_id"] == 42
    assert not rt_in.path.exists() and rt_in.mailed == []
    assert not [x for x in rt_in.logged if "secret" in x or "do not log" in x]


def test_webxdc_realtime_is_a_named_inbound_kind():
    assert "webxdc-realtime" in channels.INBOUND_KINDS


# --- the sink unset: counted and dropped, never mailed -------------------------------

def test_with_the_sink_unset_nothing_is_written_or_mailed_and_the_log_is_bounded(rt_in, monkeypatch):
    monkeypatch.setattr(bot, "XDC_RT_IN", None)
    send(rt_in, *({"n": i} for i in range(30)))
    assert rt_in.mailed == [] and not rt_in.path.exists() and not rt_in.xdc_in.exists()
    assert len(rt_in.logged) == 1 and "DC_XDC_RT_IN is unset" in rt_in.logged[0]
    assert rt_in.account.asked == [], "no snapshot rpc when nothing can be written"
    rt_in.clock.t += 60
    send(rt_in, {"n": 30})
    assert rt_in.logged[-1].endswith("(30 since this was last logged)")


# --- rule 6: 20 a second per msg -------------------------------------------------------

def test_the_rate_cap_is_20_a_second_per_msg(rt_in, monkeypatch):
    monkeypatch.setitem(bot.XDC_RT_ADVERTISED, 845, 0.0)
    send(rt_in, *({"n": i} for i in range(25)))
    send(rt_in, {"other": 1}, mid=845)              # another msg has its own budget
    got = lines(rt_in.path)
    assert [r["payload"]["n"] for r in got if r["msg_id"] == MID] == list(range(20))
    assert [r["payload"] for r in got if r["msg_id"] == 845] == [{"other": 1}]
    assert [x for x in rt_in.logged if x.startswith("XDC RT DROP")] == [
        f"XDC RT DROP msg {MID}: over 20 realtime data a second"], "logged once, not per datum"
    rt_in.clock.t += 0.999
    send(rt_in, {"n": 25})
    assert len([r for r in lines(rt_in.path) if r["msg_id"] == MID]) == 20
    rt_in.clock.t += 0.001
    send(rt_in, {"n": 26})
    assert lines(rt_in.path)[-1]["payload"] == {"n": 26}


def test_the_rate_cap_runs_before_the_decode(rt_in, monkeypatch):
    """A flood of junk costs no parse and no log line each."""
    parsed = []
    real = bot.json.loads
    monkeypatch.setattr(bot.json, "loads", lambda s, *a, **k: parsed.append(1) or real(s, *a, **k))
    for _ in range(100):
        bot.xdc_rt_in(rt_in.account, datum(b"junk"))
    assert len(parsed) == 20
    assert len(rt_in.logged) == 2       # one SKIP, one DROP


# --- echo (design 5.7, UNMEASURED): pinned as built --------------------------------

def test_no_echo_filter_a_datum_equal_to_one_the_bot_sent_is_written(rt_in):
    """Whether the core echoes the bot's own realtime sends is unmeasured; this
    build has no echo filter. If a live bot shows echoes, this test changes with it."""
    bot.XDC_SENT[MID] = [bot.xdc_key({"final": "hello"})]
    try:
        send(rt_in, {"final": "hello"})
    finally:
        bot.XDC_SENT.pop(MID, None)
    assert [r["payload"] for r in lines(rt_in.path)] == [{"final": "hello"}]


# --- xdc-in.jsonl, through the factored writer, is unchanged ---------------------------

def test_taps_still_go_to_xdc_in_with_serial_through_the_shared_writer(rt_in, monkeypatch):
    monkeypatch.setattr(bot, "XDC_SEEN", {})
    monkeypatch.setattr(bot, "XDC_SENT", {})
    bot.xdc_in_write(1001, 7, {"kind": "set"}, 12, "set ears.listen")
    [rec] = lines(rt_in.xdc_in)
    assert list(rec) == ["ts", "msg_id", "serial", "payload", "chat_id", "info"]
    assert (rec["msg_id"], rec["serial"], rec["chat_id"]) == (1001, 7, 12)
    assert stat.S_IMODE(rt_in.xdc_in.stat().st_mode) == 0o600
    assert not rt_in.path.exists()


# --- the event loop and the start log ---------------------------------------------------

def test_the_event_loop_hands_realtime_data_to_the_handler_and_survives_its_failure(rt_in, monkeypatch):
    acct = feed_tests.Events(datum({"a": 1}), datum({"a": 2}))
    acct.get_message_by_id = lambda mid: Msg()
    calls = []

    def flaky(account, event):
        calls.append(event.msg_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
    monkeypatch.setattr(bot, "xdc_rt_in", flaky)
    threading.Thread(target=bot.handle_events, args=(acct,), daemon=True).start()
    assert acct.done.wait(5)
    assert calls == [MID, MID]
    assert [x for x in rt_in.logged if x.startswith(f"webxdc realtime data, msg {MID}: RuntimeError: boom")]


def test_the_event_loop_writes_a_line_for_a_real_event(rt_in):
    acct = feed_tests.Events(datum({"from": "app"}))
    acct.get_message_by_id = rt_in.account.get_message_by_id
    threading.Thread(target=bot.handle_events, args=(acct,), daemon=True).start()
    assert acct.done.wait(5)
    assert [r["payload"] for r in lines(rt_in.path)] == [{"from": "app"}]


@pytest.mark.parametrize("sink, says", [
    (None, "webxdc realtime data in: DC_XDC_RT_IN is unset, so it is counted and dropped, never mailed"),
    ("set", "webxdc realtime data in -> "),
    ("clash", "webxdc realtime data in: DC_XDC_RT_IN REFUSED: "),
])
def test_the_start_log_says_where_realtime_data_goes(monkeypatch, tmp_path, sink, says):
    feed = bot.xdc_feed_make(tmp_path / xdc_feed.FILE, tmp_path / "heard")
    monkeypatch.setattr(bot, "XDC_FEED", feed)
    monkeypatch.setattr(bot, "XDC_RT_IN", tmp_path / "rt.jsonl" if sink == "set" else None)
    monkeypatch.setattr(bot, "XDC_RT_IN_CLASH", tmp_path / "xdc-in.jsonl" if sink == "clash" else None)
    try:
        logged, sender = rt_tests.run_main_one_pass(monkeypatch, tmp_path, rt_tests.MainAccount(feed_tests.Msg(MID)), [])
    finally:
        feed.stop()
    sender.stop()
    assert [x for x in logged if x.startswith(says)], logged


def _config_in_a_fresh_process(tmp_path, cwd=None, **env):
    full = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    full.update(DC_ROOT=str(tmp_path / "root"), DC_CALLS="off", **env)
    out = subprocess.run(
        [sys.executable, "-c", ("import json; from sessionphone.bridge import bot; "
                                "print(json.dumps([None if p is None else str(p) for p in (bot.XDC_RT_IN, bot.XDC_IN)]))")],
        env=full, cwd=cwd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_dc_xdc_rt_in_is_off_by_default_and_read_from_the_environment(tmp_path):
    assert _config_in_a_fresh_process(tmp_path) == [None, None]
    home = os.path.expanduser("~")
    assert _config_in_a_fresh_process(tmp_path, DC_XDC_RT_IN="~/x/rt.jsonl") == [f"{home}/x/rt.jsonl", None]
    assert _config_in_a_fresh_process(tmp_path, DC_XDC_RT_IN="") == [None, None]


def test_dc_xdc_rt_in_naming_dc_xdc_in_s_own_file_is_refused(tmp_path):
    """A serial-less line must never land in xdc-in.jsonl (v1 keys on serial):
    the same file, however it is spelled, turns the sink off, not on."""
    sink = tmp_path / "bot" / "xdc-in.jsonl"
    for spelling in (str(sink), str(tmp_path / "bot" / ".." / "bot" / "xdc-in.jsonl")):
        assert _config_in_a_fresh_process(tmp_path, DC_XDC_IN=str(sink), DC_XDC_RT_IN=spelling) == [None, str(sink)]
    assert _config_in_a_fresh_process(tmp_path, DC_XDC_IN=str(sink), DC_XDC_RT_IN=str(tmp_path / "rt.jsonl")) == [
        str(tmp_path / "rt.jsonl"), str(sink)]


def test_the_refusal_sees_through_home_relative_and_linked_spellings(tmp_path):
    """The same file spelled with ~, relative to the bot's cwd, through a
    symlinked directory, or as a dangling symlink to the not-yet-written file:
    each is refused, before either file exists."""
    sink = tmp_path / "bot" / "xdc-in.jsonl"
    sink.parent.mkdir()
    (tmp_path / "linked").symlink_to(sink.parent, target_is_directory=True)
    (tmp_path / "dangling.jsonl").symlink_to(sink)
    for spelling, cwd in (("~/bot/xdc-in.jsonl", None),
                          ("xdc-in.jsonl", sink.parent),
                          ("../bot/xdc-in.jsonl", sink.parent),
                          (str(tmp_path / "linked" / "xdc-in.jsonl"), None),
                          (str(tmp_path / "dangling.jsonl"), None)):
        got = _config_in_a_fresh_process(tmp_path, cwd=cwd, HOME=str(tmp_path),
                                         DC_XDC_IN=str(sink), DC_XDC_RT_IN=spelling)
        assert got == [None, str(sink)], spelling
    assert not sink.exists()
    # and the other way round: DC_XDC_IN spelled with ~, DC_XDC_RT_IN in full
    got = _config_in_a_fresh_process(tmp_path, HOME=str(tmp_path),
                                     DC_XDC_IN="~/bot/xdc-in.jsonl", DC_XDC_RT_IN=str(sink))
    assert got == [None, str(sink)]
    # a sibling in the same directory, spelled relative, is NOT refused
    got = _config_in_a_fresh_process(tmp_path, cwd=sink.parent, DC_XDC_IN=str(sink), DC_XDC_RT_IN="rt.jsonl")
    assert got == ["rt.jsonl", str(sink)]


def test_same_file_sees_links_and_never_raises(tmp_path):
    a = tmp_path / "a.jsonl"
    a.write_text("")
    os.link(a, tmp_path / "hard.jsonl")
    (tmp_path / "soft.jsonl").symlink_to(a)
    assert bot.xdc_same_file(tmp_path / "hard.jsonl", a)
    assert bot.xdc_same_file(tmp_path / "soft.jsonl", a)
    assert not bot.xdc_same_file(tmp_path / "b.jsonl", a)
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    assert bot.xdc_same_file(tmp_path / "loop", tmp_path / "loop") is True
    assert bot.xdc_same_file(tmp_path / "loop", a) is False


def test_a_refused_sink_drops_and_says_why(rt_in, monkeypatch):
    monkeypatch.setattr(bot, "XDC_RT_IN", None)
    monkeypatch.setattr(bot, "XDC_RT_IN_CLASH", rt_in.xdc_in)
    send(rt_in, {"n": 1})
    assert not rt_in.xdc_in.exists() and not rt_in.path.exists() and rt_in.mailed == []
    assert rt_in.logged == [(f"XDC RT IN msg {MID}: realtime data dropped: DC_XDC_RT_IN is the same file as "
                             f"DC_XDC_IN, and realtime data is never mailed")]


# --- the docs say what the code does (AGENTS.md: the spec rule) ------------------------

ROOT = feed_tests.ROOT


def test_the_docs_name_the_sink_its_line_and_that_it_is_never_mailed():
    readme = (ROOT / "README.md").read_text()
    sec = readme.split("### Realtime data from an app: `DC_XDC_RT_IN`", 1)[1].split("\n### ", 1)[0]
    for need in ('"via": "realtime"', '"seq": 1,', "never mailed", "no `serial`", "20 a second",
                 "webxdc-realtime", "sibling of `DC_XDC_IN`"):
        assert need in sec, need
    assert "DC_XDC_RT_IN" in (ROOT / "skills" / "sessionphone" / "SKILL.md").read_text()
    unreleased = (ROOT / "CHANGELOG.md").read_text().split("## [0.1.0]", 1)[0]
    assert "`DC_XDC_RT_IN=PATH`" in unreleased

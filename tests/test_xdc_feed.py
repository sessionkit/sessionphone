"""The heard log, streamed live into one bound webxdc app (PHONE-165 part 2).

A feed job binds a webxdc msg; XdcFeed, its own thread, turns each new heard
line into a realtime payload and hands the send to the one realtime sender
thread. Most tests drive XdcFeed by hand (poll_once(), an injected clock, a
sender that runs each item at once), so nothing waits on a real thread; the
live-thread tests say so in their names.

Fakes only (AGENTS.md): no account, no rpc-server, no relay, no real heard log.
"""
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import types

import pytest
import test_xdc_realtime as rt_tests  # do-001's fakes and its one-pass main() rig

from sessionphone import channels, jobs
from sessionphone.bridge import ack, bot, dc_env, xdc_feed, xdc_rt

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "dc-xdc-realtime"
# The skill's Guide Trail recipe: in the page its "What reaches you" links.
SKILL = ROOT / "docs" / "what-reaches-you.md"
DAY = "2030-12-08"


# --- fakes -------------------------------------------------------------------

class Msg:
    """A webxdc msg as the feed sees it: its realtime calls, decoded.
    fail: raised by every rpc while set. block: an Event the data send waits on."""

    def __init__(self, mid=844, chat_id=42, view_type="Webxdc", fail=None, block=None):
        self.id, self.chat_id, self.view_type, self.fail, self.block = mid, chat_id, view_type, fail, block
        self.calls, self.threads = [], []

    def get_snapshot(self):
        return types.SimpleNamespace(chat_id=self.chat_id, view_type=self.view_type)

    def send_webxdc_realtime_advertisement(self):
        self.threads.append(threading.current_thread().name)
        if self.fail is not None:
            raise self.fail
        self.calls.append("advertise")

    def send_webxdc_realtime_data(self, data):
        self.threads.append(threading.current_thread().name)
        if self.block is not None:
            assert self.block.wait(timeout=10), "the blocked send was never released"
        if self.fail is not None:
            raise self.fail
        self.calls.append(json.loads(bytes(data).decode("utf-8")))

    def data(self):
        return [c for c in self.calls if c != "advertise"]


class SyncSender:
    """A sender that runs each item at once, on the caller's thread (poll_once tests)."""

    def __init__(self):
        self.dead, self.labels = None, []

    def refusal(self):
        return self.dead

    def submit(self, work, ack=None, label=""):
        if self.dead is not None:
            return False
        self.labels.append(label)
        work()
        return True


class HoldSender(SyncSender):
    """A sender that keeps each item until run() is called: a stuck send."""

    def __init__(self):
        super().__init__()
        self.q = []

    def submit(self, work, ack=None, label=""):
        self.labels.append(label)
        self.q.append(work)
        return True

    def run(self):
        while self.q:
            self.q.pop(0)()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s=0.1):
        self.t += s
        return self


def heard(d, *recs, day=DAY):
    """Append heard lines to D's day file, as the ears do: one whole line each."""
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"heard_{day}.jsonl").open("a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def partial(text, **kw):
    return {"kind": "partial", "source": "mic", "text": text, **kw}


def turn(text, **kw):
    return {"kind": "turn", "source": "mic", "text": text, **kw}


def saying(text, agent="probe", **kw):
    return {"kind": "saying", "agent": agent, "text": text, **kw}


def make(tmp_path, sender=None, allowed=(42,), max_feeds=2, clock=None, path=None, **kw):
    """An XdcFeed over tmp: its log lines in feed.logged."""
    logged = []
    feed = xdc_feed.XdcFeed(
        sender=sender or SyncSender(),
        send=lambda m, mid, data: m.send_webxdc_realtime_data(data),
        advertise=lambda m, mid: m.send_webxdc_realtime_advertisement(),
        log=logged.append, path=path or tmp_path / "root" / xdc_feed.FILE, directory=tmp_path / "heard",
        agent="Probe", name="Probe", allowed=allowed, max_feeds=max_feeds, clock=clock or Clock(), **kw)
    feed.thread = types.SimpleNamespace(is_alive=lambda: True)    # "started": poll_once is driven by hand
    feed.logged = logged
    return feed


def pump(feed, n=20, step=0.1):
    """n polls, the clock moving STEP between them."""
    for _ in range(n):
        feed.poll_once()
        feed.clock.tick(step)


# --- what is fed (design 3.3) -------------------------------------------------

def test_heard_lines_become_the_three_payloads(tmp_path):
    feed, m = make(tmp_path), Msg()
    feed.bind(844, 42, m)
    d = tmp_path / "heard"
    heard(d, partial("so the"))
    pump(feed, 2)
    heard(d, turn("so the trail goes on"), saying("It does."))
    pump(feed)
    assert m.calls == ["advertise", {"partial": "so the"}, {"final": "so the trail goes on"},
                       {"lines": [{"who": "agent", "name": "Probe", "text": "It does."}]}]


@pytest.mark.parametrize("rec", [
    {"kind": "partial", "source": "call", "text": "a caller"},             # a call has its own transcript (Q2)
    {"kind": "turn", "source": "call", "text": "a caller's turn"},
    saying("not ours", agent="team"),                                         # another agent's saying
    turn("Um."), turn("."), turn("Music."),                                  # PHONE-158 noise turns
    turn("half a turn", final=False),
    partial("   "), turn(""), {"kind": "said", "source": "mic", "text": "x"},
    {"kind": "event", "event": "listen-started", "source": "mic"},
])
def test_what_is_not_fed(rec):
    assert xdc_feed.payload_for(rec, "probe", "Probe") is None


def test_the_agent_is_matched_without_case():
    assert xdc_feed.payload_for(saying("hi", agent=" PROBE "), "probe", "Probe") is not None


def test_text_is_flattened_and_cut_to_4000_characters():
    kind, text = xdc_feed.payload_for(partial("a\nb  c\t" + "x" * 5000), "probe", "Probe")
    assert kind == "partial" and text.startswith("a b c x") and len(text) == 4000


def test_nothing_is_replayed_at_bind(tmp_path):
    d, feed, m = tmp_path / "heard", make(tmp_path), Msg()
    heard(d, turn("said before the bind"), partial("and this"))
    feed.bind(844, 42, m)
    pump(feed)
    assert m.data() == []
    heard(d, turn("after"))
    pump(feed)
    assert m.data() == [{"final": "after"}]


# --- the queue: chunks join, a final closes them (PASS block CORRECTION) -------

def test_pending_partials_join_in_order_into_one_partial(tmp_path):
    """Heard partials are CHUNKS: the app appends each one, so none may be lost."""
    d, feed, m = tmp_path / "heard", make(tmp_path), Msg()
    feed.bind(844, 42, m)
    pump(feed, 2)               # the bind's advertisement
    heard(d, partial("so"), partial("the trail"), partial("goes"))
    pump(feed)
    assert m.data() == [{"partial": "so the trail goes"}]


def test_a_final_discards_the_partials_pending_before_it():
    q = xdc_feed.Pending()
    q.add("partial", "so the")
    q.add("lines", {"lines": [1]})
    q.add("partial", "trail")
    q.add("final", {"final": "so the trail"})
    assert [q.take(), q.take(), q.take()] == [{"lines": [1]}, {"final": "so the trail"}, None]


def test_finals_and_lines_keep_their_order_and_are_never_coalesced():
    q = xdc_feed.Pending()
    for i in range(3):
        q.add("final", {"final": f"f{i}"})
        q.add("lines", {"lines": [i]})
    assert [q.take() for _ in range(7)] == [{"final": "f0"}, {"lines": [0]}, {"final": "f1"}, {"lines": [1]},
                                            {"final": "f2"}, {"lines": [2]}, None]


def test_partials_after_a_final_start_a_new_run():
    q = xdc_feed.Pending()
    q.add("final", {"final": "one"})
    q.add("partial", "two")
    q.add("partial", "three")
    assert [q.take(), q.take(), q.take()] == [{"final": "one"}, {"partial": "two three"}, None]


def test_a_joined_partial_stops_at_4000_characters_and_the_rest_waits():
    q = xdc_feed.Pending()
    for c in "abc":
        q.add("partial", c * 1999)
    first, second = q.take(), q.take()
    assert first == {"partial": "a" * 1999 + " " + "b" * 1999}        # 3999: a third would pass 4000
    assert second == {"partial": "c" * 1999} and q.take() is None
    assert len(xdc_rt.encode(first)) < xdc_rt.MAX_BYTES


def test_on_overflow_the_oldest_partial_chunk_goes_first():
    q = xdc_feed.Pending(cap=4)
    q.add("partial", "p1")
    q.add("lines", {"lines": ["l1"]})
    q.add("partial", "p2")
    q.add("partial", "p3")
    assert q.add("lines", {"lines": ["l2"]}) == 1       # p1 dropped, the line before it kept
    assert len(q) == 4 and q.dropped == 1
    assert [q.take() for _ in range(4)] == [{"lines": ["l1"]}, {"partial": "p2 p3"}, {"lines": ["l2"]}, None]


def test_with_no_partial_left_the_oldest_final_or_line_goes():
    q = xdc_feed.Pending(cap=2)
    q.add("final", {"final": "1"})
    q.add("lines", {"lines": [2]})
    assert q.add("final", {"final": "3"}) == 1
    assert [q.take(), q.take(), q.take()] == [{"lines": [2]}, {"final": "3"}, None]


def test_the_queue_holds_100_and_a_full_queue_is_logged_once_per_10_s(tmp_path):
    sender = HoldSender()
    d, feed, m = tmp_path / "heard", make(tmp_path, sender=sender), Msg()
    feed.bind(844, 42, m)
    feed.poll_once()            # the advertisement, held: nothing else goes until it returns
    heard(d, *[turn(f"turn {i}") for i in range(130)])
    feed.poll_once()
    b = feed.feeds[844]
    assert len(b.pending) == 100 and b.pending.dropped == 30
    full = [x for x in feed.logged if "queue full" in x]
    assert len(full) == 1 and "30 dropped now, 30 so far" in full[0]
    heard(d, turn("one more"))
    feed.clock.tick(5)
    feed.poll_once()
    assert len([x for x in feed.logged if "queue full" in x]) == 1      # inside 10 s: not said again
    heard(d, turn("and another"))
    feed.clock.tick(5)
    feed.poll_once()
    assert len([x for x in feed.logged if "queue full" in x]) == 2
    for _ in range(3):
        sender.run()
        pump(feed, 1)
    assert m.data()[0] == {"final": "turn 32"}       # the oldest 32 went, the rest are in order


def test_the_send_rate_is_capped_per_feed(tmp_path):
    """RATE sends a second at most. Measured at rate 4, whose gap (0.25 s) and
    the poll step (1/16 s) are exact in binary, so no float edge decides it."""
    assert xdc_feed.RATE == 10.0
    d, feed, m = tmp_path / "heard", make(tmp_path, rate=4.0), Msg()
    feed.bind(844, 42, m)
    feed.poll_once()            # the advertisement
    heard(d, *[turn(f"t{i}") for i in range(40)])
    pump(feed, 16, step=0.0625)     # 1 s of polls, 4 times faster than the cap
    assert m.data() == [{"final": f"t{i}"} for i in range(4)]


def test_one_send_in_flight_per_feed_so_the_sender_queue_never_grows(tmp_path):
    """The do-001 review's note: the shared sender's queue is unbounded, so the
    feed's own bounded queue sits in front of it. A stuck send holds ONE item."""
    sender = HoldSender()
    d, feed, m = tmp_path / "heard", make(tmp_path, sender=sender), Msg()
    feed.bind(844, 42, m)
    for i in range(50):
        heard(d, partial(f"w{i}"))
        feed.poll_once()
        feed.clock.tick(0.2)
    assert len(sender.q) == 1           # the advertisement, still in flight
    sender.run()
    feed.poll_once()
    sender.run()
    assert m.data() == [{"partial": " ".join(f"w{i}" for i in range(50))}]


# --- reading the heard log ----------------------------------------------------

def test_midnight_rollover_reads_the_new_day_file_from_its_start(tmp_path):
    d, feed, m = tmp_path / "heard", make(tmp_path), Msg()
    heard(d, turn("yesterday"), day="2030-06-09")
    feed.bind(844, 42, m)
    heard(d, turn("before midnight"), day="2030-06-09")
    heard(d, turn("after midnight"), day="2030-12-08")
    pump(feed)
    assert m.data() == [{"final": "before midnight"}, {"final": "after midnight"}]


def test_a_truncated_file_is_read_again_from_its_start(tmp_path):
    d, feed, m = tmp_path / "heard", make(tmp_path), Msg()
    heard(d, turn("an old long line that was here before the bind"))
    feed.bind(844, 42, m)
    (d / f"heard_{DAY}.jsonl").write_text(json.dumps(turn("new")) + "\n")      # shorter: replaced
    pump(feed)
    assert m.data() == [{"final": "new"}]


def test_a_half_line_waits_for_its_newline_and_junk_is_skipped(tmp_path):
    d, feed, m = tmp_path / "heard", make(tmp_path), Msg()
    feed.bind(844, 42, m)
    f = d / f"heard_{DAY}.jsonl"
    d.mkdir(parents=True, exist_ok=True)
    line = json.dumps(turn("whole"))
    f.write_text("not json\n[1]\n" + line[:10])
    pump(feed)
    assert m.data() == []
    with f.open("a") as fh:
        fh.write(line[10:] + "\n")
    pump(feed)
    assert m.data() == [{"final": "whole"}]


def test_no_heard_dir_is_no_lines_not_an_error(tmp_path):
    feed, m = make(tmp_path), Msg()
    feed.bind(844, 42, m)
    pump(feed)
    assert m.data() == [] and not [x for x in feed.logged if "failed" in x]


# --- binding: two consents, idempotent, bounded -------------------------------

def test_a_chat_not_in_dc_xdc_feed_chats_is_refused_and_nothing_is_sent(tmp_path):
    feed, m = make(tmp_path, allowed=(7,)), Msg(chat_id=42)
    with pytest.raises(xdc_feed.Refused, match="chat 42 is not in DC_XDC_FEED_CHATS"):
        feed.bind(844, 42, m)
    heard(tmp_path / "heard", turn("x"))
    pump(feed)
    assert m.calls == [] and feed.bound() == [] and not (tmp_path / "root" / xdc_feed.FILE).exists()


@pytest.mark.parametrize("raw,want", [(None, set()), ("", set()), (" 12, 13 ,x,,-4", {12, 13, -4})])
def test_dc_xdc_feed_chats_parses_ids_and_unset_is_none(raw, want):
    assert xdc_feed.chats(raw) == want


def test_unset_dc_xdc_feed_chats_means_no_feed_binds(tmp_path):
    feed = make(tmp_path, allowed=xdc_feed.chats(None))
    with pytest.raises(xdc_feed.Refused, match="DC_XDC_FEED_CHATS"):
        feed.bind(844, 42, Msg())


def test_the_settings_and_their_defaults(monkeypatch):
    assert dc_env.XDC_FEED_CHATS == "" and dc_env.XDC_FEEDS_MAX == 2     # conftest dropped every DC_*
    src = (ROOT / "bin" / "dc-env").read_text()
    assert 'DC_XDC_FEED_CHATS="${DC_XDC_FEED_CHATS:-}"' in src
    assert 'DC_XDC_FEEDS_MAX="${DC_XDC_FEEDS_MAX:-2}"' in src


def test_bind_twice_is_already_feeding_and_stop_twice_is_not_feeding(tmp_path):
    feed, m = make(tmp_path), Msg()
    assert feed.bind(844, 42, m).startswith("feeding the heard log into msg 844 (chat 42)")
    assert feed.bind(844, 42, m) == "already feeding msg 844"
    assert feed.stop_job(844) == "stopped feeding msg 844"
    assert feed.stop_job(844) == "not feeding msg 844"
    assert feed.bound() == []


def test_the_bind_ack_says_delivery_is_not_confirmed(tmp_path):
    assert "nothing confirms delivery" in make(tmp_path).bind(844, 42, Msg())


def test_at_most_dc_xdc_feeds_max_at_once(tmp_path):
    feed = make(tmp_path, max_feeds=2)
    feed.bind(1, 42, Msg(1))
    feed.bind(2, 42, Msg(2))
    with pytest.raises(xdc_feed.Refused, match=r"DC_XDC_FEEDS_MAX \(2\) allows: msg 1, 2"):
        feed.bind(3, 42, Msg(3))
    feed.stop_job(1)
    feed.bind(3, 42, Msg(3))
    assert feed.bound() == [2, 3]


def test_an_unknown_feed_is_refused(tmp_path):
    with pytest.raises(xdc_feed.Refused, match="feed 'video' is not one"):
        make(tmp_path).bind(844, 42, Msg(), feed="video")


def test_two_feeds_each_get_every_line(tmp_path):
    d, feed, a, b = tmp_path / "heard", make(tmp_path), Msg(1), Msg(2)
    feed.bind(1, 42, a)
    feed.bind(2, 42, b)
    heard(d, turn("both"))
    pump(feed)
    assert a.data() == b.data() == [{"final": "both"}]


# --- the bindings file: 0600, whole, resumed at the end -----------------------

def test_the_bindings_file_is_0600_and_written_whole(tmp_path):
    feed = make(tmp_path)
    feed.bind(844, 42, Msg())
    p = tmp_path / "root" / xdc_feed.FILE
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    rec = json.loads(p.read_text())
    assert rec["v"] == 1 and [(f["msg_id"], f["chat_id"], f["feed"]) for f in rec["feeds"]] == [(844, 42, "heard")]
    assert [x.name for x in p.parent.iterdir()] == [xdc_feed.FILE]     # no .part left behind
    feed.stop_job(844)
    assert json.loads(p.read_text())["feeds"] == []


def test_a_bind_that_cannot_be_saved_still_feeds_and_says_so(tmp_path):
    (tmp_path / "root").write_text("a file where the dir should be")
    feed = make(tmp_path)
    detail = feed.bind(844, 42, Msg())
    assert "NOT saved, so it ends at the next restart" in detail and feed.bound() == [844]
    assert any("bindings NOT SAVED" in x for x in feed.logged)


def test_bindings_survive_a_restart_and_resume_at_the_end(tmp_path):
    d, m = tmp_path / "heard", Msg()
    first = make(tmp_path)
    first.bind(844, 42, m)
    heard(d, turn("before the restart"))
    heard(d, turn("while the bot was down"))
    again = make(tmp_path)                  # a new process: the same file
    assert again.resume(lambda mid: (m, 42)) == [844]
    assert any("msg 844: resumed, chat 42, from the end of the heard log" in x for x in again.logged)
    pump(again)
    assert m.data() == []                   # nothing replayed
    heard(d, turn("after"))
    pump(again)
    assert m.data() == [{"final": "after"}]
    assert m.calls[0] == "advertise"        # a resumed binding advertises again


def test_resume_drops_a_msg_that_is_gone_and_rewrites_the_file(tmp_path):
    make(tmp_path).bind(844, 42, Msg())

    def gone(mid):
        raise KeyError(mid)
    again = make(tmp_path)
    assert again.resume(gone) == []
    assert any("msg 844: not resumed, feed stopped (KeyError" in x for x in again.logged)
    assert json.loads((tmp_path / "root" / xdc_feed.FILE).read_text())["feeds"] == []


def test_resume_drops_a_chat_no_longer_in_dc_xdc_feed_chats(tmp_path):
    make(tmp_path).bind(844, 42, Msg())
    again = make(tmp_path, allowed=())
    assert again.resume(lambda mid: (Msg(), 42)) == []
    assert any("not in DC_XDC_FEED_CHATS" in x for x in again.logged)


def test_resume_keeps_to_dc_xdc_feeds_max(tmp_path):
    feed = make(tmp_path)
    feed.bind(1, 42, Msg(1))
    feed.bind(2, 42, Msg(2))
    assert make(tmp_path, max_feeds=1).resume(lambda mid: (Msg(mid), 42)) == [1]


@pytest.mark.parametrize("body", ["{not json", "[]", '{"feeds": 3}', '{"feeds": [1, {"msg_id": true}]}'])
def test_a_bad_bindings_file_resumes_none(tmp_path, body):
    p = tmp_path / "root" / xdc_feed.FILE
    p.parent.mkdir()
    p.write_text(body)
    assert make(tmp_path).resume(lambda mid: (Msg(mid), 42)) == []


def test_no_bindings_file_writes_none_at_resume(tmp_path):
    assert make(tmp_path).resume(lambda mid: (Msg(mid), 42)) == []
    assert not (tmp_path / "root").exists()


# --- dropped: deleted, 3 strikes ----------------------------------------------

def test_webxdc_instance_deleted_drops_the_binding(tmp_path):
    feed = make(tmp_path)
    feed.bind(844, 42, Msg())
    assert feed.instance_deleted(844) is True and feed.bound() == []
    assert any("XDC FEED msg 844: instance deleted, feed stopped" in x for x in feed.logged)
    assert json.loads((tmp_path / "root" / xdc_feed.FILE).read_text())["feeds"] == []
    assert feed.instance_deleted(844) is False      # another app's deletion: nothing


def test_three_failed_sends_in_a_row_drop_the_binding(tmp_path):
    d, feed, m = tmp_path / "heard", make(tmp_path), Msg(fail=RuntimeError("msg gone"))
    feed.bind(844, 42, m)
    heard(d, *[turn(f"t{i}") for i in range(5)])
    pump(feed, 10)
    assert feed.bound() == []
    assert any("3 sends failed in a row (last: data, RuntimeError: msg gone)" in x for x in feed.logged)
    fails = [x for x in feed.logged if "failed (RuntimeError" in x]
    assert len(fails) == 1          # said once per kind per 60 s, not per send


def test_a_good_send_resets_the_strikes(tmp_path):
    d, feed, m = tmp_path / "heard", make(tmp_path), Msg()
    feed.bind(844, 42, m)
    pump(feed, 1)
    for i in range(4):
        m.fail = RuntimeError("blip")
        heard(d, turn(f"lost {i}"), turn(f"lost {i}b"))
        pump(feed, 3)
        m.fail = None
        heard(d, turn(f"kept {i}"))
        pump(feed, 3)
    assert feed.bound() == [844]
    assert m.data() == [{"final": f"kept {i}"} for i in range(4)]


def test_a_late_failure_of_an_old_binding_leaves_a_new_one_be(tmp_path):
    feed, m = make(tmp_path), Msg()
    feed.bind(844, 42, m)
    old = feed.feeds[844]
    feed.stop_job(844)
    feed.bind(844, 42, m)
    for _ in range(3):
        feed._failed(old, "data", RuntimeError("late"))
    assert feed.bound() == [844]


# --- advertising (design 3.5) -------------------------------------------------

def test_a_binding_advertises_once_when_made(tmp_path):
    d, feed, m = tmp_path / "heard", make(tmp_path), Msg()
    feed.bind(844, 42, m)
    heard(d, turn("a"), turn("b"))
    pump(feed)
    assert m.calls == ["advertise", {"final": "a"}, {"final": "b"}]


def test_advertise_back_at_most_once_a_minute_per_msg(tmp_path):
    feed, m = make(tmp_path), Msg()
    feed.bind(844, 42, m)
    pump(feed, 1)
    assert m.calls == ["advertise"]
    said = len(feed.logged)
    assert feed.peer_advertised(844) is True       # inside the minute of the bind's advertisement
    pump(feed, 5)
    assert m.calls == ["advertise"]                 # skipped, silently
    assert feed.logged[said:] == []
    feed.clock.tick(60)
    feed.peer_advertised(844)
    pump(feed, 1)
    assert m.calls == ["advertise", "advertise"]
    feed.peer_advertised(844)
    pump(feed, 2)
    assert m.calls == ["advertise", "advertise"]


def test_an_advertisement_for_an_unbound_msg_is_ignored(tmp_path):
    feed = make(tmp_path)
    assert feed.peer_advertised(999) is False


# --- C2: a dead thread is the feed's, never the bot's ---------------------------

def test_a_poll_that_raises_is_logged_and_the_loop_goes_on(tmp_path):
    calls = []

    def files(d):
        calls.append(1)
        if len(calls) in (2, 3):
            raise OSError("disk went away")
        return []
    feed = make(tmp_path, files=files, poll=0.001)
    feed.bind(844, 42, Msg())
    feed.thread = None
    feed.start()
    try:
        end = time.monotonic() + 5
        while len(calls) < 6:
            assert time.monotonic() < end
            time.sleep(0.01)
        assert feed.thread.is_alive() and feed.died is None
        assert [x for x in feed.logged if "XDC FEED poll failed: OSError" in x] == \
            ["XDC FEED poll failed: OSError: disk went away"]       # once per kind per 60 s
    finally:
        feed.stop()


def test_live_thread_a_send_that_raises_never_kills_the_loop(tmp_path):
    sender = xdc_rt.Sender(lambda m: None, lambda p, ok, d: None).start()
    d, m = tmp_path / "heard", Msg(fail=ValueError("nope"))
    feed = make(tmp_path, sender=sender, clock=time.monotonic, poll=0.01)
    feed.thread = None
    feed.start()
    try:
        feed.bind(844, 42, m)
        heard(d, turn("a"), turn("b"), turn("c"))
        end = time.monotonic() + 5
        while feed.bound():
            assert time.monotonic() < end, feed.logged
            time.sleep(0.01)
        assert feed.thread.is_alive() and sender.thread.is_alive() and feed.refusal() is None
    finally:
        feed.stop()
        sender.stop()


class Boom(BaseException):
    """Not an Exception: what ends a thread."""


def test_a_dead_feed_thread_logs_once_refuses_feed_and_realtime_jobs_and_the_bot_stays_up(
        tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    sender = xdc_rt.Sender(lambda m: bot.log(m), lambda p, ok, d: ack.write(p, ok, d, log=bot.log)).start()
    monkeypatch.setattr(bot, "XDC_RT", sender)
    feed = bot.xdc_feed_make(tmp_path / xdc_feed.FILE, tmp_path / "heard", sender=sender)
    feed.allowed = frozenset({42})
    monkeypatch.setattr(bot, "XDC_FEED", feed)

    def die():
        raise Boom("x")
    monkeypatch.setattr(feed, "poll_once", die)
    feed.start()
    feed.thread.join(timeout=5)
    try:
        assert not feed.thread.is_alive() and feed.died == "Boom: x"
        assert logged.count("XDC FEED thread died: Boom: x; feeds off until restart") == 1
        acct, lst = rt_tests.FakeAccount(rt_tests.FakeMsg(844)), rt_tests.listing(tmp_path, 42)
        with pytest.raises(channels.Refused, match=r"feed thread died \(Boom: x\); feeds off until restart"):
            bot.xdc_feed_job(acct, {"webxdc_realtime_feed": 844, "feed": "heard"}, lst)
        with pytest.raises(channels.Refused, match=r"feed thread died \(Boom: x\)"):
            bot.xdc_rt_job(acct, {"webxdc_realtime": 844, "payload": 1}, lst)
        assert sender.thread.is_alive()     # the sender, and so the bot, carry on
    finally:
        sender.stop()


def test_alive_or_exit_does_not_watch_the_feed():
    src = (ROOT / "src/sessionphone/bridge/bot.py").read_text()
    body = src[src.index("def alive_or_exit("):]
    body = body[:body.index("\n# <<<")]
    assert "XDC_FEED" not in body and "xdc_feed" not in body


def test_a_dead_sender_refuses_a_feed_bind(tmp_path):
    sender = SyncSender()
    sender.dead = "the realtime sender thread died (Boom: x); feeds off until restart"
    with pytest.raises(xdc_feed.Refused, match="sender thread died"):
        make(tmp_path, sender=sender).bind(844, 42, Msg())


def test_an_unstarted_feed_refuses(tmp_path):
    feed = make(tmp_path)
    feed.thread = None
    assert "never started" in feed.refusal()


# --- the bot: the job, the events, the start -----------------------------------

@pytest.fixture
def bot_feed(tmp_path, monkeypatch):
    """bot.XDC_FEED over tmp, chat 42 allowed, driven by hand over a SyncSender."""
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot, "XDC_RT_ADVERTISED", {})
    feed = bot.xdc_feed_make(tmp_path / "root" / xdc_feed.FILE, tmp_path / "heard", sender=SyncSender())
    feed.allowed, feed.clock = frozenset({42}), Clock()
    feed.thread = types.SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(bot, "XDC_FEED", feed)
    feed.logged = logged
    return feed


def test_the_feed_job_binds_a_gated_webxdc_msg(bot_feed, tmp_path):
    m = Msg(844)
    acct, lst = rt_tests.FakeAccount(m), rt_tests.listing(tmp_path, 42)
    done = bot.xdc_feed_job(acct, {"webxdc_realtime_feed": 844, "feed": "heard"}, lst)
    assert done.startswith("feeding the heard log into msg 844 (chat 42)")
    assert bot_feed.bound() == [844]
    heard(tmp_path / "heard", turn("hello"))
    pump(bot_feed, 2)
    assert m.calls == ["advertise", {"final": "hello"}]
    assert 844 in bot.XDC_RT_ADVERTISED       # the bot's own advertise, remembered for later data jobs
    assert not [x for x in bot_feed.logged if x.startswith("XDC RT DATA")]     # no log line per datum


def test_the_feed_job_takes_a_digit_string_and_feed_defaults_to_heard(bot_feed, tmp_path):
    acct, lst = rt_tests.FakeAccount(Msg(844)), rt_tests.listing(tmp_path, 42)
    bot.xdc_feed_job(acct, {"webxdc_realtime_feed": "844"}, lst)
    assert bot_feed.bound() == [844]


def test_the_feed_job_is_gated_on_the_channels_list(bot_feed, tmp_path):
    m = Msg(844, chat_id=13)
    with pytest.raises(channels.Refused, match="chat 13 is not on the allow-list"):
        bot.xdc_feed_job(rt_tests.FakeAccount(m), {"webxdc_realtime_feed": 844}, rt_tests.listing(tmp_path, 42))
    assert bot_feed.bound() == [] and m.calls == []


def test_the_feed_job_needs_both_consents(bot_feed, tmp_path):
    """Listed in channels.txt, but not in DC_XDC_FEED_CHATS: refused."""
    m = Msg(844, chat_id=43)
    with pytest.raises(channels.Refused, match="chat 43 is not in DC_XDC_FEED_CHATS"):
        bot.xdc_feed_job(rt_tests.FakeAccount(m), {"webxdc_realtime_feed": 844},
                         rt_tests.listing(tmp_path, 42, 43))
    assert bot_feed.bound() == [] and m.calls == []


def test_the_feed_job_refuses_a_msg_that_is_not_a_webxdc_app(bot_feed, tmp_path):
    with pytest.raises(channels.Refused, match="msg 844 is not a webxdc app"):
        bot.xdc_feed_job(rt_tests.FakeAccount(Msg(844, view_type="Text")), {"webxdc_realtime_feed": 844},
                         rt_tests.listing(tmp_path, 42))


@pytest.mark.parametrize("job,why", [
    ({"webxdc_realtime_feed": "x"}, 'must be a webxdc message id, not "x"'),
    ({"webxdc_realtime_feed": True}, "must be a webxdc message id, not true"),
    ({"webxdc_realtime_feed": 0}, "must be a webxdc message id, not 0"),
    ({"webxdc_realtime_feed": 844, "stop": "yes"}, '"stop" must be true or false'),
])
def test_a_bad_feed_job_is_refused(bot_feed, tmp_path, job, why):
    with pytest.raises(channels.Refused, match=re.escape(why)):
        bot.xdc_feed_job(rt_tests.FakeAccount(Msg(844)), job, rt_tests.listing(tmp_path, 42))


def test_a_stop_job_needs_no_rpc_and_no_gate(bot_feed, tmp_path):
    bot_feed.bind(844, 42, Msg(844))

    class NoRpc:
        def get_message_by_id(self, mid):
            raise AssertionError("a stop made an rpc")
    lst = rt_tests.listing(tmp_path)          # nothing listed
    assert bot.xdc_feed_job(NoRpc(), {"webxdc_realtime_feed": 844, "stop": True}, lst) == \
        "stopped feeding msg 844"
    assert bot.xdc_feed_job(NoRpc(), {"webxdc_realtime_feed": 844, "stop": True}, lst) == "not feeding msg 844"


def test_resume_vets_each_binding_through_the_gate(bot_feed, tmp_path):
    bot_feed.bind(844, 42, Msg(844))
    bot_feed.bind(845, 42, Msg(845, chat_id=42))
    fresh = bot.xdc_feed_make(bot_feed.path, tmp_path / "heard", sender=SyncSender())
    fresh.allowed = frozenset({42})
    acct = rt_tests.FakeAccount(Msg(844), Msg(845, view_type="Text"))
    assert fresh.resume(lambda mid: bot.xdc_feed_vet(acct, mid, rt_tests.listing(tmp_path, 42))) == [844]
    assert any("msg 845: not resumed" in x and "not a webxdc app" in x for x in bot_feed.logged)


class Events:
    """account.wait_for_event() over a list; then it blocks until released."""

    def __init__(self, *events):
        self.events, self.done = list(events), threading.Event()

    def wait_for_event(self):
        if self.events:
            return self.events.pop(0)
        self.done.set()
        threading.Event().wait()        # the loop's thread is a daemon: it parks here


def test_the_event_loop_drops_a_deleted_instance_and_flags_an_advertisement(bot_feed, monkeypatch):
    bot_feed.bind(844, 42, Msg(844))
    bot_feed.bind(845, 42, Msg(845))
    acct = Events(types.SimpleNamespace(kind=bot.EventType.WEBXDC_INSTANCE_DELETED, msg_id=844),
                  types.SimpleNamespace(kind=bot.EventType.WEBXDC_REALTIME_ADVERTISEMENT_RECEIVED, msg_id=845))
    threading.Thread(target=bot.handle_events, args=(acct,), daemon=True).start()
    assert acct.done.wait(5)
    assert bot_feed.bound() == [845] and bot_feed.feeds[845].peer is True
    assert bot_feed.feeds[845].msg.calls == []     # a flag; no rpc on the loop


def test_live_threads_a_stuck_feed_send_does_not_delay_handle_events(tmp_path, monkeypatch):
    """Success criterion 3: the feed's send blocks on the sender thread for ever;
    the event loop still handles a WEBXDC_STATUS_UPDATE, and an advertisement."""
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot, "XDC_RT_ADVERTISED", {844: 0.0})
    sender = xdc_rt.Sender(lambda m: bot.log(m), lambda p, ok, d: None).start()
    monkeypatch.setattr(bot, "XDC_RT", sender)
    feed = bot.xdc_feed_make(tmp_path / xdc_feed.FILE, tmp_path / "heard", sender=sender)
    feed.allowed, feed.poll = frozenset({42}), 0.01
    monkeypatch.setattr(bot, "XDC_FEED", feed)
    hold = threading.Event()
    m = Msg(844, block=hold)
    updates = []
    monkeypatch.setattr(bot, "xdc_update", lambda account, event: updates.append(event.msg_id))
    feed.start()
    try:
        feed.bind(844, 42, m)
        heard(tmp_path / "heard", turn("stuck"))
        end = time.monotonic() + 5
        while "XdcRealtime" not in m.threads or len(m.threads) < 2:     # the advertisement, then the stuck data
            assert time.monotonic() < end, m.threads
            time.sleep(0.01)
        acct = Events(types.SimpleNamespace(kind=bot.EventType.WEBXDC_REALTIME_ADVERTISEMENT_RECEIVED, msg_id=844),
                      types.SimpleNamespace(kind=bot.EventType.WEBXDC_STATUS_UPDATE, msg_id=900))
        t0 = time.monotonic()
        threading.Thread(target=bot.handle_events, args=(acct,), daemon=True).start()
        assert acct.done.wait(2), "handle_events waited on the feed's stuck send"
        assert time.monotonic() - t0 < 1.0
        assert updates == [900] and not hold.is_set()
        assert set(m.threads) == {"XdcRealtime"}        # every realtime rpc on the sender thread
    finally:
        hold.set()
        feed.stop()
        sender.stop()


def test_main_starts_the_feed_beside_the_event_thread_and_acks_a_feed_job(monkeypatch, tmp_path):
    feed = bot.xdc_feed_make(tmp_path / xdc_feed.FILE, tmp_path / "heard")
    feed.allowed = frozenset({42})
    monkeypatch.setattr(bot, "XDC_FEED", feed)
    real_make = xdc_rt.Sender

    def sender_too(*a, **k):    # run_main_one_pass makes the sender: the feed hands to that one
        s = real_make(*a, **k)
        feed.sender = s
        return s
    monkeypatch.setattr(xdc_rt, "Sender", sender_too)
    a = tmp_path / "acks" / "a.json"
    try:
        logged, _sender = rt_tests.run_main_one_pass(monkeypatch, tmp_path, rt_tests.MainAccount(Msg(844)), [
            {"webxdc_realtime_feed": 844, "feed": "heard", "ack": str(a)}])
        got = json.loads(a.read_text())
        assert got["ok"] is True and got["detail"].startswith("feeding the heard log into msg 844"), got
        assert feed.thread.is_alive() and feed.bound() == [844]
        assert "webxdc feed: chats 42 may be fed the heard log" in " ".join(logged)
        assert json.loads((tmp_path / xdc_feed.FILE).read_text())["feeds"][0]["msg_id"] == 844
    finally:
        feed.stop()
        feed.sender.stop()


def test_main_says_when_no_feed_can_bind(monkeypatch, tmp_path, xdc_feed_in_tmp):
    real_make = xdc_rt.Sender

    def sender_too(*a, **k):
        s = real_make(*a, **k)
        xdc_feed_in_tmp.sender = s
        return s
    monkeypatch.setattr(xdc_rt, "Sender", sender_too)
    a = tmp_path / "acks" / "a.json"
    logged, sender = rt_tests.run_main_one_pass(monkeypatch, tmp_path, rt_tests.MainAccount(Msg(844)), [
        {"webxdc_realtime_feed": 844, "ack": str(a)}])
    try:
        assert "webxdc feed: DC_XDC_FEED_CHATS is unset, so no feed can bind" in logged
        got = json.loads(a.read_text())
        assert got["ok"] is False and "DC_XDC_FEED_CHATS" in got["detail"]
    finally:
        sender.stop()


def test_main_hands_feed_jobs_to_the_feed_and_makes_no_realtime_rpc():
    src = (ROOT / "src/sessionphone/bridge/bot.py").read_text()
    main = src[src.index("def main():"):]
    branch = main[main.index('elif "webxdc_realtime_feed" in job'):]
    branch = branch[:branch.index("elif", 5)]
    assert "xdc_feed_job(account, job, listing)" in branch and "send_webxdc_realtime" not in branch
    feed_src = (ROOT / "src/sessionphone/bridge/xdc_feed.py").read_text()
    assert "send_webxdc_realtime" not in feed_src       # the feed thread itself makes no rpc


def test_the_feed_imports_no_media_pack():
    """A chat-only bot (no media pack, PHONE-145) imports the feed, and with it
    web/turns, without loading calls.py."""
    code = ("import sys\n"
            "for m in ('aiortc', 'aioice', 'av', 'numpy'): sys.modules[m] = None\n"
            "import sessionphone.bridge.xdc_feed\n"
            "assert 'sessionphone.bridge.calls' not in sys.modules\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr


def test_jobs_keys_know_the_feed_job():
    assert {"webxdc_realtime_feed", "feed", "stop"} <= jobs.KEYS


# --- the tool, and the SKILL recipe end to end (do-002 L1) ---------------------

class FeedBot(rt_tests.RtBot):
    """rt_tests.RtBot, plus a file job (acked as the bot words it, msg 844) and
    feed jobs through the real bot.xdc_feed_job, acked by the loop."""

    def loop(self):
        while not self.stop.is_set():
            for out in self.outs:
                for f in sorted(out.glob("*.json")) if out.exists() else []:
                    j = json.loads(f.read_text())
                    self.jobs.append(dict(j))
                    try:
                        if "file" in j:
                            done = f"sent {pathlib.Path(j['file']).name} to chat 42, msg 844"
                        else:
                            done = bot.xdc_feed_job(self.acct, j, self.lst)
                        ack.write(j.get("ack"), True, done)
                    except channels.Refused as e:
                        ack.write(j.get("ack"), False, str(e))
                    f.unlink()
            time.sleep(0.05)


def test_script_feed_and_stop_write_the_feed_jobs(bot_feed, tmp_path):
    rt_tests._allow(tmp_path)
    with FeedBot(tmp_path, rt_tests.FakeAccount(Msg(844))) as b:
        r1 = rt_tests._script(tmp_path, "--feed", "844")
        r2 = rt_tests._script(tmp_path, "--feed", "844", "--stop")
    assert r1.returncode == 0, r1.stderr
    assert r1.stdout.startswith("dc-xdc-realtime: feeding the heard log into msg 844"), r1.stdout
    assert r2.returncode == 0 and r2.stdout.strip() == "dc-xdc-realtime: stopped feeding msg 844", r2.stdout
    for j in b.jobs:
        j.pop("ack")
    assert b.jobs == [{"webxdc_realtime_feed": 844, "feed": "heard"}, {"webxdc_realtime_feed": 844, "stop": True}]


def test_script_feed_exits_3_when_the_chat_may_not_be_fed(bot_feed, tmp_path):
    rt_tests._allow(tmp_path)
    bot_feed.allowed = frozenset()
    with FeedBot(tmp_path, rt_tests.FakeAccount(Msg(844))):
        r = rt_tests._script(tmp_path, "--feed", "844")
    assert r.returncode == 3 and "DC_XDC_FEED_CHATS" in r.stderr, (r.stdout, r.stderr)


@pytest.mark.parametrize("args", [["--feed"], ["--feed", "844", "1"], ["--stop", "844"],
                                  ["--advertise", "--feed", "844"], ["844", "--stop"]])
def test_script_feed_usage_exits_1_and_queues_nothing(tmp_path, args):
    rt_tests._allow(tmp_path)
    r = rt_tests._script(tmp_path, *args)
    assert r.returncode == 1, (args, r.returncode, r.stderr)
    assert not (tmp_path / "outbox").exists() or not list((tmp_path / "outbox").iterdir())


def _recipe():
    body = SKILL.read_text()
    block = body[body.index("**The Guide Trail**"):]
    block = block[block.index("```bash") + len("```bash"):]
    block = block[:block.index("```")]
    return [line.split("#")[0].strip() for line in block.strip().splitlines()]


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("uv") is None,
                    reason="needs bash and uv on PATH (bin/sessionphone runs through uv; "
                           "the kit's INSTALL puts uv in ~/.local/bin)")
def test_the_skill_recipe_works_end_to_end_against_a_fake_bot(bot_feed, tmp_path):
    """do-002 L1: build the app, send it, feed it, hear a line arrive, stop."""
    send, feed_line, stop_line = _recipe()
    assert send == 'dc-send-file CHAT_ID "$(sessionphone app build guide-trail)"'
    assert feed_line == "dc-xdc-realtime --feed N" and stop_line == "dc-xdc-realtime --feed N --stop"
    rt_tests._allow(tmp_path)
    venv_bin = pathlib.Path(os.sys.executable).parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("DC_")}
    env.update(DC_ROOT=str(tmp_path / "root"), DC_OUTBOX=str(tmp_path / "outbox"),
               DC_OUTBOX_PUBLIC=str(tmp_path / "public"), DC_TAKEN_WAIT="5",
               PATH=f"{ROOT / 'bin'}:{venv_bin}:{env.get('PATH', '')}")

    def sh(line):
        return subprocess.run(["bash", "-c", line], env=env, capture_output=True, text=True, timeout=60,
                              check=False)
    m = Msg(844)
    with FeedBot(tmp_path, rt_tests.FakeAccount(m)) as b:
        r = sh(send.replace("CHAT_ID", "42"))
        assert r.returncode == 0, r.stderr
        n = re.search(r"msg (\d+)", r.stdout).group(1)
        assert b.jobs[0]["file"] == str(tmp_path / "root" / "apps" / "guide-trail.xdc")
        assert pathlib.Path(b.jobs[0]["file"]).is_file()
        r = sh(feed_line.replace("N", n))
        assert r.returncode == 0 and "feeding the heard log into msg 844" in r.stdout, (r.stdout, r.stderr)
        heard(tmp_path / "heard", partial("so the"), turn("so the trail"))
        pump(bot_feed, 3)
        r = sh(stop_line.replace("N", n))
        assert r.returncode == 0 and "stopped feeding msg 844" in r.stdout, (r.stdout, r.stderr)
    assert m.calls == ["advertise", {"final": "so the trail"}]
    assert bot_feed.bound() == []


# --- the app: a live row the 40-row cap evicted is not written to (do-002 L3) ----

APP_JS = r"""
// A DOM just big enough for the Guide Trail's script; then the scenario.
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.parent = null; this.textContent = ''; this.className = ''; }
  append(...cs) { for (const c of cs) { c.parent = this; this.children.push(c); } }
  remove() { if (this.parent) { const s = this.parent.children; s.splice(s.indexOf(this), 1); this.parent = null; } }
  get firstChild() { return this.children[0]; }
}
const els = {log: new El('div'), st: new El('span')};
globalThis.document = {getElementById: id => els[id], createElement: t => new El(t)};
let listener = null;
globalThis.window = {webxdc: {setUpdateListener() {}, joinRealtimeChannel() { return {setListener(f) { listener = f; }}; }}};
globalThis.setTimeout = f => f();     // the typewriter runs at once
eval(require('fs').readFileSync(process.argv[2], 'utf8'));
const send = p => listener(new TextEncoder().encode(JSON.stringify(p)));
const rows = () => els.log.children.map(d => d.children[0].textContent + ':' + d.children[1].textContent);
send({partial: 'so'}); send({partial: 'the trail'});
for (let i = 0; i < 45; i++) send({lines: [{who: 'agent', name: 'Probe', text: 'line ' + i}]});
send({partial: 'goes on'});
const after = rows();
send({final: 'so the trail goes on'});
console.log(JSON.stringify({after, end: rows()}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_app_starts_a_new_live_row_when_the_cap_evicted_the_old_one(tmp_path):
    html = (ROOT / "src/sessionphone/apps/guide-trail/index.html").read_text()
    script = html[html.index("<script>", html.index("webxdc.js")) + len("<script>"):html.rindex("</script>")]
    (tmp_path / "app.js").write_text(script)
    (tmp_path / "run.js").write_text(APP_JS)
    r = subprocess.run(["node", str(tmp_path / "run.js"), str(tmp_path / "app.js")], capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert len(got["after"]) == 40
    assert got["after"][-1] == "USER:goes on"         # visible, not written into an evicted row
    assert got["end"][-1] == "USER:so the trail goes on"
    assert not any(x.startswith("USER:so the trail") for x in got["end"][:-1])


# --- the docs (design 7: docs pinned) ------------------------------------------

def test_readme_has_the_feed_rows_and_the_two_consents():
    readme = (ROOT / "README.md").read_text()
    sec = readme[readme.index("### The heard feed: `dc-xdc-realtime --feed`"):]
    sec = sec[:sec.index("\n### ")]
    for row in ("| F1 | `kind: partial`", "| F2 | `kind: turn`", "| F3 | `kind: saying`"):
        assert row in sec
    for word in ("DC_XDC_FEED_CHATS", "DC_XDC_FEEDS_MAX", "list only your own 1:1 chat", "xdc-feeds.json",
                 "XDC FEED thread died", "--feed MSG_ID --stop"):
        assert word in sec, word


@pytest.mark.skipif(not (ROOT / "design").is_dir(),
                    reason="design/ is export-ignored (.gitattributes): a release archive has no design notes")
def test_the_design_note_keeps_the_decisions():
    note = (ROOT / "design" / "webxdc-realtime.md").read_text()
    for word in ("No realtime rpc on the outbox loop", "Partials are chunks", "Two consents",
                 "alive_or_exit", "DC_XDC_RT_IN", "Unmeasured"):
        assert word in note, word


def test_the_changelog_names_the_feed_under_unreleased_added():
    log = (ROOT / "CHANGELOG.md").read_text()
    at = log.index("## [0.2.0]")          # the feed shipped in 0.2.0: its section, not [Unreleased]
    unreleased = log[at:log.index("\n## [", at + 5)]
    added = unreleased[unreleased.index("### Added"):]
    assert "`dc-xdc-realtime --feed MSG_ID`" in added and "DC_XDC_FEED_CHATS" in added


# --- a long heard turn: what the feed carries, and what it drops ---------------

LONG_WORDS = [f"w{i:04d}" for i in range(2500)]
LONG = " ".join(LONG_WORDS)                  # 14,999 characters


def _words(payloads):
    text = " ".join(p["partial"] if "partial" in p else p["final"] for p in payloads)
    return re.findall(r"\bw\d{4}\b", text)


def test_a_long_run_of_partial_chunks_is_carried_whole_across_sends():
    """The queue keeps what does not fit for the next send: no chunk is lost."""
    q = xdc_feed.Pending()
    for i in range(25):
        q.add("partial", " ".join(LONG_WORDS[i * 100:(i + 1) * 100]))
    sent = []
    while (p := q.take()) is not None:
        sent.append(p)
    assert len(sent) > 1 and _words(sent) == LONG_WORDS and q.dropped == 0


def test_a_long_final_is_cut_for_display_only():
    """The feed's cut is a display cap: the app's live line is bounded at
    MAX_TEXT, in order, and the heard record it came from (what agents read)
    is left whole."""
    rec = turn(LONG)
    kind, payload = xdc_feed.payload_for(rec, "probe", "Probe")
    shown = _words([payload])
    assert kind == "final" and len(payload["final"]) == xdc_feed.MAX_TEXT
    assert 0 < len(shown) < len(LONG_WORDS) and shown == LONG_WORDS[:len(shown)]
    assert rec["text"] == LONG

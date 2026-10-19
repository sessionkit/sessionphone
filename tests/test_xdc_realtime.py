"""Realtime data into a webxdc app: the outbox jobs and the one sender thread (PHONE-165 part 1).

`webxdc_realtime` sends a JSON payload as bytes to a webxdc msg's realtime
channel (advertising first, once per msg per process); `webxdc_realtime_advertise`
advertises now. Both are validated and gated on the outbox loop like
`webxdc_update`, then HANDED to the realtime sender thread, which makes the rpc
and writes the ack when it returns (design.md C1). A dead sender logs one loud
line and refuses later realtime jobs, and does not take the bot down (C2).

Fakes only: no account, no rpc-server, no relay. The script tests run the real
bin/dc-xdc-realtime against a fake bot on a tmp outbox.
"""
import json
import os
import pathlib
import subprocess
import threading
import time
import types

import pytest

from sessionphone import channels, jobs
from sessionphone.bridge import ack, bot, xdc_rt

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "dc-xdc-realtime"
UNCONFIRMED = "delivery not confirmed: realtime is ephemeral"


class Boom(BaseException):
    """Not an Exception: what kills a thread a per-item `except Exception` cannot catch."""


class FakeMsg:
    """A webxdc message. Records each realtime call and the thread it ran on.
    block: an Event the data send waits for. fail: an exception the data send raises.
    in_data_send: set once a data send has started (its thread recorded), before it
    waits on block: so a test can wait for the sender thread to park there."""

    def __init__(self, mid, chat_id=42, view_type="Webxdc", block=None, fail=None, adv_fail=None):
        self.id, self.chat_id, self.view_type = mid, chat_id, view_type
        self.block, self.fail, self.adv_fail = block, fail, adv_fail
        self.calls, self.threads, self.updates = [], [], []
        self.in_data_send = threading.Event()

    def get_snapshot(self):
        return types.SimpleNamespace(chat_id=self.chat_id, view_type=self.view_type)

    def send_webxdc_realtime_advertisement(self):
        self.threads.append(threading.current_thread().name)
        if self.adv_fail is not None:
            raise self.adv_fail
        self.calls.append(("advertise",))

    def send_webxdc_realtime_data(self, data):
        self.threads.append(threading.current_thread().name)
        self.in_data_send.set()
        if self.block is not None:
            assert self.block.wait(timeout=10), "the blocked send was never released"
        if self.fail is not None:
            raise self.fail
        self.calls.append(("data", bytes(data)))

    def send_webxdc_status_update(self, update, description):
        self.updates.append((update, description))


class FakeAccount:
    def __init__(self, *msgs, config=None):
        self.msgs = {m.id: m for m in msgs}
        self.config = {"webxdc_realtime_enabled": "1"} if config is None else config

    def get_message_by_id(self, mid):
        return self.msgs[mid]

    def get_config(self, key):
        return self.config.get(key)


def listing(tmp_path, *chat_ids):
    return channels.Listing(tmp_path / "channels.txt",
                            {f"c{n}": channels.Entry(f"c{n}", f"chat:{n}") for n in chat_ids})


def wait_ack(path, timeout=5.0):
    path = pathlib.Path(path)
    end = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < end, f"no ack at {path}"
        time.sleep(0.01)
    return json.loads(path.read_text())


@pytest.fixture
def rt(monkeypatch):
    """A fresh, started sender as bot.XDC_RT; empty advertise memory; log lines recorded."""
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot, "XDC_RT_ADVERTISED", {})
    sender = xdc_rt.Sender(lambda m: bot.log(m), lambda p, ok, d: ack.write(p, ok, d, log=bot.log))
    monkeypatch.setattr(bot, "XDC_RT", sender)
    sender.start()
    sender.logged = logged
    yield sender
    sender.stop()


def job(tmp_path, name, **kw):
    return {**kw, "ack": str(tmp_path / "acks" / f"{name}.json")}


# --- the encoding ------------------------------------------------------------

def test_encode_is_compact_utf8_json():
    payload = {"partial": "so the trail", "lines": [{"who": "agent", "text": "naïve ✓"}]}
    data = xdc_rt.encode(payload)
    assert data == json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert "naïve ✓".encode() in data              # UTF-8 as is, not \\u escapes
    assert json.loads(data.decode("utf-8")) == payload  # what the app's JSON.parse(TextDecoder) reads


# --- the job: validated and gated on the loop, sent by the sender -----------

def test_data_job_advertises_once_then_sends_the_payload_bytes(rt, tmp_path):
    m = FakeMsg(844)
    acct, lst = FakeAccount(m), listing(tmp_path, 42)
    j1 = job(tmp_path, "1", webxdc_realtime=844, payload={"partial": "so the trail"})
    j2 = job(tmp_path, "2", webxdc_realtime=844, payload={"final": "so the trail goes"})
    bot.xdc_rt_job(acct, j1, lst)
    a1 = wait_ack(j1["ack"])
    bot.xdc_rt_job(acct, j2, lst)
    a2 = wait_ack(j2["ack"])
    assert m.calls == [("advertise",),
                       ("data", b'{"partial":"so the trail"}'),
                       ("data", b'{"final":"so the trail goes"}')]
    n1 = len(b'{"partial":"so the trail"}')
    assert a1 == {"ok": True, "detail": f"advertised, then sent realtime data to msg 844 ({n1} bytes); "
                                        f"{UNCONFIRMED}"}
    assert a2["ok"] and a2["detail"].startswith("sent realtime data to msg 844 (")
    assert UNCONFIRMED in a2["detail"]
    assert 844 in bot.XDC_RT_ADVERTISED


def test_the_rpc_runs_on_the_sender_thread_never_the_caller(rt, tmp_path):
    m = FakeMsg(844)
    j = job(tmp_path, "1", webxdc_realtime=844, payload=1)
    bot.xdc_rt_job(FakeAccount(m), j, listing(tmp_path, 42))
    wait_ack(j["ack"])
    assert m.threads == ["XdcRealtime", "XdcRealtime"]


def test_advertise_job_always_advertises(rt, tmp_path):
    m = FakeMsg(844)
    acct, lst = FakeAccount(m), listing(tmp_path, 42)
    bot.xdc_rt_job(acct, job(tmp_path, "d", webxdc_realtime=844, payload={}), lst)
    wait_ack(tmp_path / "acks" / "d.json")
    for n in ("a1", "a2"):
        j = job(tmp_path, n, webxdc_realtime_advertise=844)
        bot.xdc_rt_job(acct, j, lst)
        a = wait_ack(j["ack"])
        assert a["ok"] and "advertised realtime on msg 844" in a["detail"]
        assert "not confirmed" in a["detail"]
    assert m.calls == [("advertise",), ("data", b"{}"), ("advertise",), ("advertise",)]


def test_a_digit_string_msg_id_is_read_as_the_number(rt, tmp_path):
    m = FakeMsg(844)
    j = job(tmp_path, "1", webxdc_realtime="844", payload=[1])
    bot.xdc_rt_job(FakeAccount(m), j, listing(tmp_path, 42))
    assert wait_ack(j["ack"])["ok"]
    assert m.calls[-1] == ("data", b"[1]")


def test_the_limit_is_128000_bytes_encoded(rt, tmp_path):
    m = FakeMsg(844)
    acct, lst = FakeAccount(m), listing(tmp_path, 42)
    at = "x" * (xdc_rt.MAX_BYTES - 2)        # "..." quotes make it exactly the limit
    assert len(xdc_rt.encode(at)) == xdc_rt.MAX_BYTES
    j = job(tmp_path, "at", webxdc_realtime=844, payload=at)
    bot.xdc_rt_job(acct, j, lst)
    assert wait_ack(j["ack"])["ok"]
    with pytest.raises(channels.Refused) as e:
        bot.xdc_rt_job(acct, job(tmp_path, "over", webxdc_realtime=844, payload=at + "y"), lst)
    assert f"{xdc_rt.MAX_BYTES + 1} bytes" in str(e.value) and "nothing was sent" in str(e.value)
    assert "nothing was truncated" in str(e.value)
    assert [c for c in m.calls if c[0] == "data"] == [("data", xdc_rt.encode(at))]


def test_a_multibyte_payload_is_measured_in_bytes_not_characters(rt, tmp_path):
    m = FakeMsg(844)
    big = "é" * 64000            # 64000 characters, 128002 bytes with its quotes
    with pytest.raises(channels.Refused) as e:
        bot.xdc_rt_job(FakeAccount(m), job(tmp_path, "1", webxdc_realtime=844, payload=big),
                       listing(tmp_path, 42))
    assert "128002 bytes" in str(e.value)
    assert m.calls == [] and m.threads == []


def test_a_non_webxdc_msg_is_refused_and_nothing_sent(rt, tmp_path):
    m = FakeMsg(900, view_type="Text")
    for k in ({"webxdc_realtime": 900, "payload": 1}, {"webxdc_realtime_advertise": 900}):
        with pytest.raises(channels.Refused) as e:
            bot.xdc_rt_job(FakeAccount(m), job(tmp_path, "1", **k), listing(tmp_path, 42))
        assert "msg 900 is not a webxdc app" in str(e.value)
    assert m.calls == [] and m.threads == []


def test_the_gate_refuses_an_unlisted_chat_and_nothing_is_sent(rt, tmp_path):
    m = FakeMsg(844, chat_id=13)
    for k in ({"webxdc_realtime": 844, "payload": 1}, {"webxdc_realtime_advertise": 844}):
        with pytest.raises(channels.Refused) as e:
            bot.xdc_rt_job(FakeAccount(m), job(tmp_path, "1", **k), listing(tmp_path, 42))
        assert "chat 13 is not on the allow-list" in str(e.value)
    assert any("xdc-realtime" in line for line in rt.logged)
    assert m.calls == [] and m.threads == []


@pytest.mark.parametrize("bad", [True, None, "84x", 0, -3, 8.5, [844]])
def test_a_msg_id_that_is_not_a_number_is_refused(rt, tmp_path, bad):
    with pytest.raises(channels.Refused) as e:
        bot.xdc_rt_job(FakeAccount(), job(tmp_path, "1", webxdc_realtime=bad, payload=1), listing(tmp_path, 42))
    assert "must be a webxdc message id" in str(e.value)


def test_a_data_job_with_no_payload_is_refused(rt, tmp_path):
    with pytest.raises(channels.Refused) as e:
        bot.xdc_rt_job(FakeAccount(FakeMsg(844)), job(tmp_path, "1", webxdc_realtime=844), listing(tmp_path, 42))
    assert '"payload"' in str(e.value)


def test_a_send_that_raises_is_that_jobs_no_and_the_sender_goes_on(rt, tmp_path):
    m = FakeMsg(844, fail=RuntimeError("msg gone"))
    acct, lst = FakeAccount(m), listing(tmp_path, 42)
    j = job(tmp_path, "1", webxdc_realtime=844, payload=1)
    bot.xdc_rt_job(acct, j, lst)
    assert wait_ack(j["ack"]) == {"ok": False, "detail": "RuntimeError: msg gone"}
    assert rt.refusal() is None and rt.thread.is_alive()
    m.fail = None
    j = job(tmp_path, "2", webxdc_realtime=844, payload=2)
    bot.xdc_rt_job(acct, j, lst)
    assert wait_ack(j["ack"])["ok"]
    assert m.calls[-1] == ("data", b"2")


def test_a_failed_advertisement_is_not_remembered(rt, tmp_path):
    m = FakeMsg(844, adv_fail=RuntimeError("no iroh"))
    acct, lst = FakeAccount(m), listing(tmp_path, 42)
    j = job(tmp_path, "1", webxdc_realtime=844, payload=1)
    bot.xdc_rt_job(acct, j, lst)
    assert not wait_ack(j["ack"])["ok"]
    assert 844 not in bot.XDC_RT_ADVERTISED
    m.adv_fail = None
    j = job(tmp_path, "2", webxdc_realtime=844, payload=1)
    bot.xdc_rt_job(acct, j, lst)
    assert wait_ack(j["ack"])["detail"].startswith("advertised, then sent")


def test_the_log_names_the_size_never_the_payload(rt, tmp_path):
    j = job(tmp_path, "1", webxdc_realtime=844, payload={"final": "a private sentence"})
    bot.xdc_rt_job(FakeAccount(FakeMsg(844)), j, listing(tmp_path, 42))
    wait_ack(j["ack"])
    assert not any("private" in line for line in rt.logged), rt.logged
    assert any(line.startswith("XDC RT DATA msg 844: ") and line.endswith(" bytes") for line in rt.logged)


# --- realtime off on the account ---------------------------------------------

def test_webxdc_realtime_enabled_0_refuses_every_realtime_job(monkeypatch, tmp_path):
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot, "XDC_RT_ADVERTISED", {})
    sender = xdc_rt.Sender(lambda m: bot.log(m), lambda p, ok, d: ack.write(p, ok, d))
    monkeypatch.setattr(bot, "XDC_RT", sender)
    m = FakeMsg(844)
    acct = FakeAccount(m, config={"webxdc_realtime_enabled": "0"})
    bot.xdc_rt_start(acct)
    try:
        assert any("webxdc_realtime_enabled='0'" in x and "REFUSED" in x for x in logged), logged
        for k in ({"webxdc_realtime": 844, "payload": 1}, {"webxdc_realtime_advertise": 844}):
            with pytest.raises(channels.Refused) as e:
                bot.xdc_rt_job(acct, job(tmp_path, "1", **k), listing(tmp_path, 42))
            assert 'webxdc_realtime_enabled is "0"' in str(e.value)
        assert m.calls == [] and m.threads == []
    finally:
        sender.stop()


def test_the_start_log_names_webxdc_realtime_enabled(monkeypatch):
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    sender = xdc_rt.Sender(lambda m: bot.log(m), lambda p, ok, d: None)
    monkeypatch.setattr(bot, "XDC_RT", sender)
    bot.xdc_rt_start(FakeAccount())
    try:
        assert logged == ["webxdc realtime: webxdc_realtime_enabled='1'; sender thread started"]
        assert sender.refusal() is None
    finally:
        sender.stop()


# --- C2: a dead sender ---------------------------------------------------------

def test_a_dead_sender_logs_once_refuses_later_jobs_and_the_bot_stays_up(rt, tmp_path):
    hold = threading.Event()
    m = FakeMsg(844, block=hold, fail=Boom("iroh gone"))
    acct, lst = FakeAccount(m), listing(tmp_path, 42)
    j1, j2, j3 = (job(tmp_path, n, webxdc_realtime=844, payload=n) for n in ("1", "2", "3"))
    for j in (j1, j2, j3):          # 2 and 3 queue behind the one that will kill the thread
        bot.xdc_rt_job(acct, j, lst)
    hold.set()
    for j in (j1, j2, j3):
        a = wait_ack(j["ack"])
        assert not a["ok"] and "realtime sender thread died (Boom: iroh gone)" in a["detail"]
    rt.thread.join(timeout=5)
    assert not rt.thread.is_alive()
    loud = [x for x in rt.logged if x.startswith("XDC FEED thread died")]
    assert loud == ["XDC FEED thread died: Boom: iroh gone; feeds off until restart"]
    # every later realtime job is refused with that reason, and nothing is sent
    for k in ({"webxdc_realtime": 844, "payload": 4}, {"webxdc_realtime_advertise": 844}):
        with pytest.raises(channels.Refused) as e:
            bot.xdc_rt_job(acct, job(tmp_path, "4", **k), lst)
        assert "the realtime sender thread died (Boom: iroh gone); feeds off until restart" in str(e.value)
    assert [c for c in m.calls if c[0] == "data"] == []
    # the bot does not go down: alive_or_exit does not look at the sender
    alive = threading.Thread(target=threading.Event().wait, args=(5,), daemon=True)
    alive.start()
    bot.alive_or_exit(alive, types.SimpleNamespace(tick=lambda: None))     # no SystemExit
    # and the rest still runs: a webxdc_update goes out
    bot.xdc_push(acct, {"webxdc_update": 844, "payload": {"n": 1}})
    assert m.updates == [({"payload": {"n": 1}, "info": ""}, "")]
    assert len([x for x in rt.logged if x.startswith("XDC FEED thread died")]) == 1


def test_a_submit_after_death_is_answered_at_once(tmp_path):
    logged, acks = [], []
    s = xdc_rt.Sender(logged.append, lambda p, ok, d: acks.append((p, ok, d))).start()

    def boom():
        raise Boom("x")
    assert s.submit(boom, "a1")
    s.thread.join(timeout=5)
    assert not s.thread.is_alive(), "the sender thread did not die within 5s"
    assert not s.submit(lambda: "never", "a2", "later")
    assert acks[-1][0] == "a2" and acks[-1][1] is False and "thread died (Boom: x)" in acks[-1][2]
    assert logged.count("XDC FEED thread died: Boom: x; feeds off until restart") == 1


def test_alive_or_exit_does_not_watch_the_realtime_sender():
    """C2, by source: the sender is never one of the bot's must-live threads."""
    src = (ROOT / "src/sessionphone/bridge/bot.py").read_text()
    body = src[src.index("def alive_or_exit("):]
    body = body[:body.index("\n# <<<")]
    assert "XDC_RT" not in body and "xdc_rt" not in body


def test_an_unstarted_sender_refuses():
    s = xdc_rt.Sender(lambda m: None, lambda p, ok, d: None)
    assert "never started" in s.refusal()


# --- C1: the outbox loop never waits on a realtime rpc ------------------------

class Done(Exception):
    """Raised at the end of main()'s first outbox pass."""


class MainAccount(FakeAccount):
    def is_configured(self):
        return True

    def start_io(self):
        pass

    def list_transports(self):
        return [{"addr": "bot@relay.example.org"}]


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_main_one_pass(monkeypatch, tmp_path, account, jobs_in):
    """bot.main() over fakes, through ONE outbox pass of JOBS_IN; -> the log lines."""
    logged = []
    stop = threading.Event()
    out = tmp_path / "outbox"
    out.mkdir()
    for i, j in enumerate(jobs_in):
        (out / f"{i:04d}.json").write_text(json.dumps(j))
    lst = listing(tmp_path, 42)
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(bot.seatmail, "check", lambda *a, **k: None)
    monkeypatch.setattr(bot.seatmail, "setup", lambda *a, **k: "stub")
    monkeypatch.setattr(bot.seatmail, "seat", lambda *a, **k: "stub")
    monkeypatch.setattr(bot.dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot.dc_env, "callers_problem", lambda *a, **k: None)
    monkeypatch.setattr(bot.dc_env, "callers_ignored", lambda *a, **k: None)
    monkeypatch.setattr(bot, "OUTBOX", out)
    monkeypatch.setattr(bot.SPOOL, "counts", lambda: (0, 0))
    monkeypatch.setattr(bot.SPOOL, "drain", lambda: None)
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "server_choice", lambda: ("/venv/bin/deltachat-rpc-server", "venv", "test"))
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "DeltaChat", lambda rpc: types.SimpleNamespace(get_all_accounts=lambda: [account]))
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "INVITE", None)
    monkeypatch.setattr(bot, "IMAGE_TEXT", False)
    monkeypatch.setattr(bot.channels, "load", lambda *a, **k: lst)
    monkeypatch.setattr(bot.listalert, "ListAlert",
                        lambda *a, **k: types.SimpleNamespace(judge=lambda listing: None, note=lambda: ""))
    monkeypatch.setattr(bot, "sweep", lambda account: None)
    monkeypatch.setattr(bot, "handle_events", lambda account: stop.wait(10))
    monkeypatch.setattr(bot, "Watchdog", lambda account: types.SimpleNamespace(tick=lambda: None))

    def end_of_pass(t, dog):
        raise Done
    monkeypatch.setattr(bot, "alive_or_exit", end_of_pass)
    monkeypatch.setattr(bot, "XDC_RT_ADVERTISED", {})
    sender = xdc_rt.Sender(lambda m: bot.log(m), lambda p, ok, d: ack.write(p, ok, d, log=bot.log))
    monkeypatch.setattr(bot, "XDC_RT", sender)
    try:
        with pytest.raises(Done):
            bot.main()
    finally:
        stop.set()
    return logged, sender


def test_a_blocked_realtime_send_does_not_stop_the_outbox_loop(monkeypatch, tmp_path):
    """C1: the realtime job is handed off; the next job in the same pass is taken
    and answered while the realtime rpc is still blocked; the realtime ack comes
    when that rpc returns."""
    hold = threading.Event()
    rt_msg, up_msg = FakeMsg(844, block=hold), FakeMsg(845)
    acks = tmp_path / "acks"
    a, b = acks / "a.json", acks / "b.json"
    logged, sender = run_main_one_pass(monkeypatch, tmp_path, MainAccount(rt_msg, up_msg), [
        {"webxdc_realtime": 844, "payload": {"partial": "so the trail"}, "ack": str(a)},
        {"webxdc_update": 845, "payload": {"n": 1}, "ack": str(b)},
    ])
    try:
        # the pass is over; wait for the sender THREAD to park in the blocked data
        # send (it has advertised by then), so the asserts below on what it did are
        # ordered after it did it, not raced against it (PHONE-184)
        assert rt_msg.in_data_send.wait(timeout=5), "the sender thread never reached the data send"
        # the realtime rpc is still blocked
        assert json.loads(b.read_text()) == {"ok": True, "detail": "pushed an update to msg 845"}
        assert up_msg.updates == [({"payload": {"n": 1}, "info": ""}, "")]
        assert not a.exists(), "the realtime job was answered before its rpc returned"
        assert rt_msg.calls == [("advertise",)]
        assert list((tmp_path / "outbox").glob("*.json")) == []        # both taken
        assert "MainThread" not in rt_msg.threads
        assert any("handed to the realtime sender" in x for x in logged)
        assert "webxdc realtime: webxdc_realtime_enabled='1'; sender thread started" in logged
        hold.set()
        got = wait_ack(a)
        assert got["ok"] and UNCONFIRMED in got["detail"]
        assert rt_msg.calls[-1] == ("data", b'{"partial":"so the trail"}')
    finally:
        hold.set()
        sender.stop()


def test_main_acks_a_refused_realtime_job_on_the_loop(monkeypatch, tmp_path):
    """A no is the loop's to answer, at once, and nothing reaches the sender."""
    m = FakeMsg(844, chat_id=13)
    a = tmp_path / "acks" / "a.json"
    _logged, sender = run_main_one_pass(monkeypatch, tmp_path, MainAccount(m), [
        {"webxdc_realtime": 844, "payload": 1, "ack": str(a)}])
    try:
        got = json.loads(a.read_text())
        assert got["ok"] is False and "chat 13 is not on the allow-list" in got["detail"]
        assert m.calls == [] and m.threads == []
    finally:
        sender.stop()


def test_main_hands_realtime_jobs_off_and_makes_no_realtime_rpc():
    """C1, by source: main()'s outbox loop names no realtime rpc."""
    src = (ROOT / "src/sessionphone/bridge/bot.py").read_text()
    main = src[src.index("def main():"):]
    assert "send_webxdc_realtime" not in main
    branch = main[main.index('elif "webxdc_realtime" in job'):]
    branch = branch[:branch.index("elif", 5)]
    assert "xdc_rt_job(account, job, listing)" in branch and "continue" in branch


# --- the tool: bin/dc-xdc-realtime --------------------------------------------

def _allow(tmp_path):
    p = tmp_path / "channels.txt"
    p.write_text("family  chat:42\n")
    p.chmod(0o600)


def _script(tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DC_")}
    env.update(DC_ROOT=str(tmp_path / "root"), DC_OUTBOX=str(tmp_path / "outbox"),
               DC_OUTBOX_PUBLIC=str(tmp_path / "public"), DC_TAKEN_WAIT="5")
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True,
                          timeout=60, check=False)


class RtBot:
    """A fake bot on both outboxes, shaped like main()'s loop: each job goes to
    the real bot.xdc_rt_job over a FakeAccount; a refusal is acked here, and a
    handed job is acked by the real sender thread when its (slow) rpc returns."""

    def __init__(self, tmp_path, account, slow=0.0):
        self.outs = (tmp_path / "outbox", tmp_path / "public")
        self.acct, self.jobs, self.slow = account, [], slow
        self.lst = listing(tmp_path, 42)
        self.stop = threading.Event()
        self.t = threading.Thread(target=self.loop)

    def loop(self):
        while not self.stop.is_set():
            for out in self.outs:
                for f in sorted(out.glob("*.json")) if out.exists() else []:
                    j = json.loads(f.read_text())
                    self.jobs.append(dict(j))
                    try:
                        bot.xdc_rt_job(self.acct, j, self.lst)
                    except channels.Refused as e:
                        ack.write(j.get("ack"), False, str(e))
                    f.unlink()
            time.sleep(0.05)

    def __enter__(self):
        for out in self.outs:       # a running bot stamps the keys it reads (PHONE-129)
            jobs.write_stamp(out)
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()


class SlowMsg(FakeMsg):
    """A send that takes longer than the 1 s every other tool waits for its ack."""

    def send_webxdc_realtime_data(self, data):
        time.sleep(1.5)
        super().send_webxdc_realtime_data(data)


def test_script_sends_data_and_waits_for_the_late_ack(rt, tmp_path):
    _allow(tmp_path)
    m = SlowMsg(844)
    with RtBot(tmp_path, FakeAccount(m)) as b:
        r = _script(tmp_path, "844", '{"partial": "so the trail"}')
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("dc-xdc-realtime: advertised, then sent realtime data to msg 844 ("), r.stdout
    assert UNCONFIRMED in r.stdout
    [j] = b.jobs
    assert pathlib.Path(j.pop("ack")).parent == tmp_path / "root" / "acks"
    assert j == {"webxdc_realtime": 844, "payload": {"partial": "so the trail"}}
    assert m.calls == [("advertise",), ("data", b'{"partial":"so the trail"}')]


def test_script_advertise_writes_the_advertise_job(rt, tmp_path):
    _allow(tmp_path)
    m = FakeMsg(7)
    with RtBot(tmp_path, FakeAccount(m)) as b:
        r = _script(tmp_path, "--profile", "public", "--advertise", "7")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("on profile public")
    assert "advertised realtime on msg 7" in r.stdout
    [j] = b.jobs
    j.pop("ack")
    assert j == {"webxdc_realtime_advertise": 7}
    assert m.calls == [("advertise",)]


def test_script_exits_3_when_the_bot_refuses(rt, tmp_path):
    _allow(tmp_path)
    with RtBot(tmp_path, FakeAccount(FakeMsg(844, view_type="Text"))):
        r = _script(tmp_path, "844", "1")
    assert r.returncode == 3
    assert "FAILED" in r.stderr and "not a webxdc app" in r.stderr


def test_script_refuses_for_a_bot_that_does_not_read_the_keys(tmp_path):
    """No stamp: a bot older than PHONE-129, whose keys are LEGACY_KEYS, would
    take the job for a dc-say with no chat. Refused before anything is written."""
    _allow(tmp_path)
    r = _script(tmp_path, "844", "1")
    assert r.returncode == 3 and "'webxdc_realtime'" in r.stderr and "restart the bot" in r.stderr
    assert not list((tmp_path / "outbox").glob("*.json"))


@pytest.mark.parametrize("args", [[], ["844"], ["--advertise"], ["--advertise", "844", "1"],
                                  ["844", "1", "extra"], ["--bogus", "844", "1"], ["--profile"]])
def test_script_usage_exits_1_and_queues_nothing(tmp_path, args):
    _allow(tmp_path)
    r = _script(tmp_path, *args)
    assert r.returncode == 1, (args, r.returncode, r.stderr)
    assert "usage: dc-xdc-realtime" in r.stderr
    assert not (tmp_path / "outbox").exists() or not list((tmp_path / "outbox").iterdir())


@pytest.mark.parametrize("args,why", [(["84x", "1"], "MSG_ID must be a message number"),
                                      (["844", "{not json"], "PAYLOAD_JSON is not JSON")])
def test_script_bad_input_exits_1_and_queues_nothing(tmp_path, args, why):
    _allow(tmp_path)
    r = _script(tmp_path, *args)
    assert r.returncode == 1 and why in r.stderr, r.stderr
    assert not (tmp_path / "outbox").exists() or not list((tmp_path / "outbox").iterdir())


def test_wheel_bin_links_the_tool():
    p = ROOT / "wheel-bin" / "dc-xdc-realtime"
    assert p.is_symlink() and os.readlink(p) == "../bin/dc-xdc-realtime"

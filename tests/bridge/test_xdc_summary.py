"""The webxdc summary reaches the core when a job carries one (PHONE-22, gap 3).

The webxdc spec's `summary` is the short text Delta Chat shows beside the app
icon, so the app's state shows without opening it. The settings app sets it
through DC_XDC_SUMMARY (its apply.py:740), and the bridge this was ported
from passes it through (its bot.py:381-384). sessionphone dropped it: the
outbox built {payload, info} and nothing else, and bin/dc-xdc-update never
read DC_XDC_SUMMARY.

On the pinned core (rpc-server 2.62.0) send_webxdc_status_update takes the
update as one JSON object plus a description string, so the summary is a field
of that object.

Fakes only: no account, no rpc-server. The script tests write to a tmp outbox.
The tool waits for a bot to take its job (DC_TAKEN_WAIT, 10 s), and since
PHONE-102 it always waits for a job the bot gates: a msg id's chat is one only
the bot can read, so DC_TAKEN_WAIT=0 no longer leaves the job in place. The
tests that read the job's shape run a fake bot that keeps each job and acks it;
the end-to-end test runs one that hands it to the real xdc_push (PHONE-38).
Every script test lists a chat first: with no allow-list the tool refuses.
"""
import json
import os
import pathlib
import subprocess
import threading
import time

import pytest

from sessionphone.bridge import bot, calls

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "bin" / "dc-xdc-update"


class FakeMessage:
    def __init__(self):
        self.sent = []

    def send_webxdc_status_update(self, update, description):
        self.sent.append((update, description))


class FakeAccount:
    def __init__(self):
        self.msg = FakeMessage()
        self.asked = []

    def get_message_by_id(self, mid):
        self.asked.append(mid)
        return self.msg


@pytest.fixture
def fresh(monkeypatch):
    """Empty echo memory; log lines recorded, not written."""
    monkeypatch.setattr(bot, "XDC_SENT", {})
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    return logged


def _push(job):
    acct = FakeAccount()
    bot.xdc_push(acct, job)
    assert len(acct.msg.sent) == 1
    return acct, acct.msg.sent[0]


# --- the bridge: xdc_push --------------------------------------------------

def test_summary_reaches_send_webxdc_status_update(fresh):
    acct, (update, descr) = _push({"webxdc_update": 844, "payload": {"a": 1},
                                   "summary": "ears on · mouth default"})
    assert acct.asked == [844]
    assert update == {"payload": {"a": 1}, "info": "", "summary": "ears on · mouth default"}
    assert descr == ""


def test_no_summary_is_the_update_it_always_was(fresh):
    """Absent summary: the exact dict the bridge sent before gap 3."""
    _, (update, descr) = _push({"webxdc_update": "844", "payload": {"a": 1}})
    assert update == {"payload": {"a": 1}, "info": ""}
    assert "summary" not in update
    assert descr == ""


@pytest.mark.parametrize("empty", ["", None])
def test_empty_summary_is_left_out(fresh, empty):
    _, (update, _) = _push({"webxdc_update": 844, "payload": {}, "summary": empty})
    assert "summary" not in update


def test_summary_rides_beside_info(fresh):
    _, (update, descr) = _push({"webxdc_update": 844, "payload": {"n": 2},
                                "info": "2 votes", "summary": "Highscore: 123"})
    assert update == {"payload": {"n": 2}, "info": "2 votes", "summary": "Highscore: 123"}
    assert descr == "2 votes"


def test_summary_does_not_touch_the_echo_filter(fresh):
    """The echo key is the payload alone: a push with a summary still
    recognises its own echo, which carries the payload back."""
    _push({"webxdc_update": 844, "payload": {"a": 1}, "summary": "s"})
    assert bot.xdc_is_echo(844, {"a": 1})
    assert not bot.xdc_is_echo(844, {"a": 1})


def test_log_names_the_summary_only_when_sent(fresh):
    logged = fresh
    _push({"webxdc_update": 844, "payload": {"a": 1}})
    _push({"webxdc_update": 844, "payload": {"a": 1}, "summary": "ears on"})
    assert "summary" not in logged[0]
    assert "summary 'ears on'" in logged[1]


def test_outbox_loop_hands_webxdc_jobs_to_xdc_push():
    """The job dispatch lives inline in main(); guard the wiring."""
    src = (ROOT / "src/sessionphone/bridge/bot.py").read_text()
    main = src[src.index("def main():"):]
    branch = main[main.index('elif "webxdc_update" in job:'):]
    branch = branch[:branch.index("elif", 5)]
    # PHONE-102: the channels gate on the message's chat comes first (design row 4)
    assert "xdc_push(account, job)" in branch
    assert branch.index("gate(account,") < branch.index("xdc_push(account, job)")


# --- the tool: bin/dc-xdc-update -------------------------------------------

def _script(tmp_path, *args, summary=None, taken_wait=None):
    """Run the real script against a tmp outbox; return the finished process.
    taken_wait=None leaves DC_TAKEN_WAIT unset: the tool's own 10 s wait."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("DC_")}
    env.update(DC_ROOT=str(tmp_path / "root"), DC_OUTBOX=str(tmp_path / "outbox"),
               DC_OUTBOX_PUBLIC=str(tmp_path / "public"))
    if summary is not None:
        env["DC_XDC_SUMMARY"] = summary
    if taken_wait is not None:
        env["DC_TAKEN_WAIT"] = taken_wait
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True,
                          timeout=30, check=False)


def _allow(tmp_path):
    """The allow-list the tool's pre-check reads: the one beside its outbox. Both
    outboxes here (tmp/outbox, tmp/public) sit in tmp_path, so one file serves."""
    p = tmp_path / "channels.txt"
    p.write_text("family  chat:42\n")
    p.chmod(0o600)


class Keeper:
    """A fake bot on both outboxes: it keeps each job, acks it ok, unlinks it."""

    def __init__(self, *outboxes):
        self.outs, self.jobs, self.stop = outboxes, [], threading.Event()
        self.t = threading.Thread(target=self.loop)

    def loop(self):
        while not self.stop.is_set():
            for out in self.outs:
                for f in sorted(out.glob("*.json")) if out.exists() else []:
                    job = json.loads(f.read_text())
                    self.jobs.append(dict(job))
                    calls.ack_write(job.get("ack"), True, "kept")
                    f.unlink()
            time.sleep(0.05)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()


def _run(tmp_path, *args, summary=None):
    """Run the real script against a Keeper and return the one job it wrote,
    less the ack path every job names (tests/test_dc_ack.py's business). Not
    DC_TAKEN_WAIT=0: the tool waits for a gated job whatever it says (PHONE-102)."""
    _allow(tmp_path)
    with Keeper(tmp_path / "outbox", tmp_path / "public") as k:
        r = _script(tmp_path, *args, summary=summary, taken_wait="0")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("dc-xdc-update: kept"), r.stdout     # it waited for the answer
    assert len(k.jobs) == 1, k.jobs
    job = k.jobs[0]
    assert pathlib.Path(job.pop("ack")).parent == tmp_path / "root" / "acks"
    return job


class XdcBot:
    """A fake bot on one outbox, like tests/test_dc_ack.py's Bot: it takes each
    job, hands it to the real bot.xdc_push on a FakeAccount, answers in the
    job's ack as main()'s outbox loop does, and only then unlinks the job."""

    def __init__(self, outbox):
        self.out, self.acct, self.jobs = outbox, FakeAccount(), []
        self.out.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.t = threading.Thread(target=self.loop)

    def loop(self):
        while not self.stop.is_set():
            for f in sorted(self.out.glob("*.json")):
                job = json.loads(f.read_text())
                self.jobs.append(job)
                bot.xdc_push(self.acct, job)
                calls.ack_write(job.get("ack"), True,
                                f"pushed an update to msg {job['webxdc_update']}")
                f.unlink()
            time.sleep(0.1)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()


def test_script_puts_DC_XDC_SUMMARY_in_the_job(tmp_path):
    job = _run(tmp_path, "844", '{"a":1}', summary="ears on · mouth default")
    assert job == {"webxdc_update": 844, "payload": {"a": 1}, "summary": "ears on · mouth default"}


def test_script_without_DC_XDC_SUMMARY_writes_the_old_job(tmp_path):
    job = _run(tmp_path, "844", '{"a":1}', "an", "info")
    assert job == {"webxdc_update": 844, "payload": {"a": 1}, "info": "an info"}


def test_script_with_empty_DC_XDC_SUMMARY_writes_no_summary(tmp_path):
    job = _run(tmp_path, "844", '{"a":1}', summary="")
    assert "summary" not in job


def test_script_summary_with_profile_and_info(tmp_path):
    job = _run(tmp_path, "--profile", "public", "7", '{"b":2}', "hello", summary="2 votes")
    assert job == {"webxdc_update": 7, "payload": {"b": 2}, "info": "hello", "summary": "2 votes"}


def test_script_job_through_the_bridge_end_to_end(tmp_path, fresh):
    """What the settings app does: DC_XDC_SUMMARY in, summary at the core.
    The wait for a take stays ON (DC_TAKEN_WAIT unset): a bot must take the job."""
    _allow(tmp_path)
    with XdcBot(tmp_path / "outbox") as b:
        r = _script(tmp_path, "844", '{"a":1}', summary="ears off")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-xdc-update: pushed an update to msg 844"
    assert len(b.jobs) == 1 and not list((tmp_path / "outbox").glob("*.json"))  # taken
    assert pathlib.Path(b.jobs[0]["ack"]).exists()                               # answered
    assert b.acct.asked == [844]
    [(update, descr)] = b.acct.msg.sent
    assert update["summary"] == "ears off"
    assert update == {"payload": {"a": 1}, "info": "", "summary": "ears off"}
    assert descr == ""

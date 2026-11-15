"""bin/dc-fetch: fetch a file the bot mailed as "not downloaded" (PHONE-283, design.md section 7).

The real tool against a fake bot thread, as tests/test_dc_delete.py does. The
registry and the handler are the bot's (tests/bridge/test_fetch.py); here the
tool's own side is pinned: the id check, the {"fetch": N} job, the exit codes of the dc-*
family, the older-bot refusal. Fakes only: scratch DC_ROOT, never a Delta send.
"""
import os
import subprocess
import threading
import time

import pytest
from test_dc_channels import BIN, Bot, allow, run
from test_dc_shape import queued, stamp

from sessionphone import jobs

LIST = "twelve  chat:12\n"


def test_one_id_exits_0_and_queues_a_fetch_job(tmp_path):
    allow(tmp_path, LIST)
    with Bot(tmp_path / "outbox", lambda job: (True, "fetch started for msg 5531; it is mailed when it lands")) as b:
        r = run("dc-fetch", tmp_path, "5531")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-fetch: fetch started for msg 5531; it is mailed when it lands"
    assert b.jobs[0]["fetch"] == 5531 and isinstance(b.jobs[0]["fetch"], int)
    assert set(b.jobs[0]) == {"fetch", "ack"}


def test_leading_zeros_are_a_number_not_octal(tmp_path):
    allow(tmp_path, LIST)
    with Bot(tmp_path / "outbox", lambda job: (True, "mailed")) as b:
        r = run("dc-fetch", tmp_path, "0042")
    assert r.returncode == 0, r.stderr
    assert b.jobs[0]["fetch"] == 42


def test_a_refusal_exits_3_with_the_bots_words(tmp_path):
    allow(tmp_path, LIST)
    why = "msg 77 was not left undownloaded by this bot: nothing to fetch."
    with Bot(tmp_path / "outbox", lambda job: (False, why)):
        r = run("dc-fetch", tmp_path, "77")
    assert r.returncode == 3, r.stderr
    assert r.stdout == ""
    assert "dc-fetch: FAILED - " in r.stderr and why in r.stderr


def test_no_ack_is_not_confirmed_exit_4(tmp_path):
    allow(tmp_path, LIST)
    stamp(tmp_path / "outbox", jobs.KEYS)
    # a take with no ack: the job file is unlinked and nothing is written
    out = tmp_path / "outbox"
    stop = threading.Event()

    def eat():
        while not stop.is_set():
            for f in out.glob("*.json"):
                f.unlink()
            time.sleep(0.05)
    t = threading.Thread(target=eat)
    t.start()
    try:
        r = run("dc-fetch", tmp_path, "5531", wait="5")
    finally:
        stop.set()
        t.join()
    assert r.returncode == 4, r.stderr


def test_profile_public_goes_to_the_public_outbox(tmp_path):
    allow(tmp_path, LIST)
    allow(tmp_path / "public", LIST)
    with Bot(tmp_path / "public" / "outbox", lambda job: (True, "mailed")) as b:
        r = run("dc-fetch", tmp_path, "--profile", "public", "5531")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-fetch: mailed on profile public"
    assert b.jobs[0]["fetch"] == 5531
    assert not (tmp_path / "outbox").exists() or queued(tmp_path) == []


# ---- an older bot: refused at dc_shape, nothing queued ----

def test_a_bot_stamped_without_fetch_refuses_and_queues_nothing(tmp_path):
    allow(tmp_path, LIST)
    stamp(tmp_path / "outbox", jobs.KEYS - {"fetch"})        # live pid: a running, older bot
    r = run("dc-fetch", tmp_path, "5531", wait="5")
    assert r.returncode == 3, r.stderr
    assert "does not read job key(s) 'fetch'" in r.stderr and "restart the bot" in r.stderr
    assert queued(tmp_path) == []


def test_an_unstamped_bot_refuses_and_queues_nothing(tmp_path):
    allow(tmp_path, LIST)
    r = run("dc-fetch", tmp_path, "5531", wait="5")
    assert r.returncode == 3, r.stderr
    assert "no bot has stamped" in r.stderr and "'fetch'" in r.stderr
    assert queued(tmp_path) == []


def test_no_list_refuses_before_queueing(tmp_path):
    stamp(tmp_path / "outbox", jobs.KEYS)
    r = run("dc-fetch", tmp_path, "5531")
    assert r.returncode == 3 and "every send is refused" in r.stderr
    assert queued(tmp_path) == []


# ---- usage: exit 1, nothing queued (a non-integer id is refused loudly) ----

@pytest.mark.parametrize("args", [[], ["0"], ["00"], ["-5"], ["+5"], ["x"], ["5531", "x"], ["5531", "5532"],
                                  ["1.5"], [""], ["4294967296"], ["99999999999999999999999"],
                                  ["--profile", "public"]])
def test_a_bad_id_is_usage_exit_1(tmp_path, args):
    allow(tmp_path, LIST)
    stamp(tmp_path / "outbox", jobs.KEYS)
    r = run("dc-fetch", tmp_path, *args)
    assert r.returncode == 1, (args, r.stderr)
    assert "usage: dc-fetch [--profile public] MSG_ID" in r.stderr
    assert queued(tmp_path) == []


def test_a_non_integer_id_says_what_it_is(tmp_path):
    allow(tmp_path, LIST)
    r = run("dc-fetch", tmp_path, "abc")
    assert r.returncode == 1
    assert "dc-fetch: 'abc' is not a message id (a positive integer)" in r.stderr


def test_help_prints_the_header(tmp_path):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "DC_ROOT": str(tmp_path)}
    for flag in ("-h", "--help"):
        r = subprocess.run([str(BIN / "dc-fetch"), flag], env=env, capture_output=True, text=True,
                           timeout=30, check=False)
        assert r.returncode == 0
        assert r.stdout.startswith("dc-fetch [--profile public] MSG_ID - fetch a file this bot mailed")
        assert "Exit: 0" in r.stdout

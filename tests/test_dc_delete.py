"""bin/dc-delete: delete for everyone a message the bot sent (PHONE-122, design.md section 2).

The real tool against a fake bot thread whose answer is the bot's own
bridge/retract.delete_for_all over a fake rpc (tests/bridge/test_retract.py's),
so the tool's exit code and line are what a real refusal or delete gives.
Exit codes, the dc-* idiom: 0 the ack says it went; 1 usage; 3 refused by the
bot, or a bot that does not read `delete` (dc_shape), nothing queued or sent;
3 for core's own refusal too, which claims nothing about what core did.
Fakes only: scratch DC_ROOT, never a Delta send.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_dc_channels import BIN, Bot, allow, run
from test_dc_shape import queued, stamp

from sessionphone import channels, jobs
from sessionphone.bridge import bot, retract

sys.path.insert(0, str(Path(__file__).resolve().parent / "bridge"))
from test_retract import account  # the fake rpc under a real client Account

LIST = "twelve  chat:12\nthirteen  chat:13\n"


@pytest.fixture(autouse=True)
def quiet_gate(monkeypatch):
    monkeypatch.setattr(bot, "log", lambda line: None)


def retracting(root, acct):
    """The bot's answer to a delete job: retract.delete_for_all with the bot's gate
    over ROOT's list, acked ok with the done line, or ok:false with why."""
    def answer(job):
        try:
            return True, retract.delete_for_all(acct, job["delete"], channels.load(root / "channels.txt"),
                                                bot.gate)
        except channels.Refused as e:
            return False, str(e)
    return answer


def test_one_id_exits_0_and_prints_the_done_line(tmp_path):
    allow(tmp_path, LIST)
    a = account()
    with Bot(tmp_path / "outbox", retracting(tmp_path, a)) as b:
        r = run("dc-delete", tmp_path, "5531")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ("dc-delete: deleted msg 5531 here and asked every member of chat 12 "
                                "to delete it")
    assert b.jobs[0]["delete"] == [5531] and set(b.jobs[0]) == {"delete", "ack"}
    assert a._rpc.deletes == [("delete_messages_for_all", 3, [5531])]


def test_several_ids_one_job_one_call(tmp_path):
    allow(tmp_path, LIST)
    a = account()
    with Bot(tmp_path / "outbox", retracting(tmp_path, a)) as b:
        r = run("dc-delete", tmp_path, "5531", "5532")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ("dc-delete: deleted msgs 5531, 5532 here and asked every member of "
                                "chat 12 to delete them")
    assert [j["delete"] for j in b.jobs] == [[5531, 5532]]
    assert a._rpc.deletes == [("delete_messages_for_all", 3, [5531, 5532])]


@pytest.mark.parametrize("ids, why", [
    (["9999"], "msg 9999: no such message on this profile"),
    (["5533"], "msg 5533 was not sent by this account"),
    (["5534"], "msg 5534 is an info message"),
    (["5531", "6001"], "more than one chat"),
    (["7001"], "chat 14 is not on the allow-list"),
    (["5531", "9999"], "msg 9999: no such message"),          # mixed: the good one is not sent
])
def test_each_refusal_exits_3_and_sends_nothing(tmp_path, ids, why):
    allow(tmp_path, LIST)
    a = account()
    with Bot(tmp_path / "outbox", retracting(tmp_path, a)):
        r = run("dc-delete", tmp_path, *ids)
    assert r.returncode == 3, r.stderr
    assert r.stdout == ""
    assert "dc-delete: FAILED - REFUSED: " in r.stderr and why in r.stderr
    assert a._rpc.deletes == []


def test_cores_refusal_exits_3_verbatim(tmp_path):
    allow(tmp_path, LIST)
    a = account(core_error="Cannot request deletion of unencrypted message for others")
    with Bot(tmp_path / "outbox", retracting(tmp_path, a)):
        r = run("dc-delete", tmp_path, "5531")
    assert r.returncode == 3
    assert "Delta Chat core refused: Cannot request deletion of unencrypted message for others" in r.stderr


def test_profile_public_goes_to_the_public_outbox(tmp_path):
    allow(tmp_path, LIST)
    allow(tmp_path / "public", LIST)
    a = account()
    with Bot(tmp_path / "public" / "outbox", retracting(tmp_path / "public", a)) as b:
        r = run("dc-delete", tmp_path, "--profile", "public", "5531")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("to delete it on profile public")
    assert b.jobs[0]["delete"] == [5531]


# ---- an older bot: refused at dc_shape, nothing queued ----

def test_a_bot_stamped_without_delete_refuses_and_queues_nothing(tmp_path):
    allow(tmp_path, LIST)
    stamp(tmp_path / "outbox", jobs.KEYS - {"delete"})       # live pid: a running, older bot
    r = run("dc-delete", tmp_path, "5531", wait="5")
    assert r.returncode == 3, r.stderr
    assert "does not read job key(s) 'delete'" in r.stderr and "restart the bot" in r.stderr
    assert queued(tmp_path) == []


def test_an_unstamped_bot_refuses_and_queues_nothing(tmp_path):
    allow(tmp_path, LIST)
    r = run("dc-delete", tmp_path, "5531", wait="5")
    assert r.returncode == 3, r.stderr
    assert "no bot has stamped" in r.stderr and "'delete'" in r.stderr
    assert queued(tmp_path) == []


def test_no_list_refuses_before_queueing(tmp_path):
    stamp(tmp_path / "outbox", jobs.KEYS)
    r = run("dc-delete", tmp_path, "5531")
    assert r.returncode == 3 and "every send is refused" in r.stderr
    assert queued(tmp_path) == []


# ---- usage: exit 1, nothing queued ----

@pytest.mark.parametrize("args", [[], ["0"], ["00"], ["-5"], ["+5"], ["x"], ["5531", "x"], ["1.5"],
                                  ["4294967296"], ["99999999999999999999999"], ["--profile", "public"]])
def test_a_bad_id_is_usage_exit_1(tmp_path, args):
    allow(tmp_path, LIST)
    stamp(tmp_path / "outbox", jobs.KEYS)
    r = run("dc-delete", tmp_path, *args)
    assert r.returncode == 1, (args, r.stderr)
    assert "usage: dc-delete [--profile public] MSG_ID [MSG_ID...]" in r.stderr
    assert queued(tmp_path) == []


def test_help_prints_the_header(tmp_path):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "DC_ROOT": str(tmp_path)}
    for flag in ("-h", "--help"):
        r = subprocess.run([str(BIN / "dc-delete"), flag], env=env, capture_output=True, text=True,
                           timeout=30, check=False)
        assert r.returncode == 0
        assert r.stdout.startswith("dc-delete [--profile public] MSG_ID [MSG_ID...] - delete for everyone")
        assert "never a partial delete" in r.stdout and "Exit: 0" in r.stdout
        # the bot's checks send nothing; core's refusal claims nothing (retract.py's tail="", README)
        flat = " ".join(r.stdout.split())
        assert "The bot refuses, exit 3, nothing sent: an id with no message," in flat
        assert ("Core's own refusal is exit 3 too, its text verbatim: what core did before it "
                "refused is core's to say, and it is not measured.") in flat
        assert "core's own refusal, verbatim" not in flat
        assert ("3 the bot refused or no bot took it, nothing sent, or core refused "
                "(only its words say what it did);") in flat
    assert list(tmp_path.iterdir()) == []


def test_not_in_the_wheel():
    """Q2: bin/ only, like dc-react."""
    assert (BIN.parent / "wheel-bin").is_dir()
    assert not (BIN.parent / "wheel-bin" / "dc-delete").exists()

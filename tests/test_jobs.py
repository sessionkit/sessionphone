"""The bot stamps the job keys it reads; a job it would misread is refused (PHONE-129 (c)).

On one install the public bot PROCESS ran pre-PHONE-102 code and the dc-* tools on
disk were post-102: `dc-say NAME` queued `{"chat": ...}` and the bot crashed
with KeyError 'chat_id'. Now the bot writes <OUTBOX>/.bot with the keys it
reads, and sessionphone.jobs.check() refuses a job with a key the running bot
does not read (no stamp: the pre-PHONE-102 keys), "restart the bot", exit 3.

Fakes only: stamps under tmp_path, pids of this test or of a finished child.
"""
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from sessionphone import jobs

BOT = Path(__file__).resolve().parent.parent / "src" / "sessionphone" / "bridge" / "bot.py"


def bot_keys(src: str) -> set[str]:
    """Every job key SRC reads, by the forms bot.py uses: job["k"], job.get("k"),
    "k" in job, chat_ref(job, "k"), and any(k in job for k in ("a", "b"))."""
    q = r"""["']([a-z_]+)["']"""
    keys: set[str] = set()
    for pat in (rf"\bjob\[\s*{q}\s*\]", rf"\bjob\.get\(\s*{q}", rf"{q}\s+(?:not\s+)?in\s+job\b",
                rf"\bchat_ref\(\s*job\s*,\s*{q}"):
        keys |= set(re.findall(pat, src))
    for tup in re.findall(r"\w+\s+in\s+job\s+for\s+\w+\s+in\s+\(([^)]*)\)", src):
        keys |= set(re.findall(q, tup))
    return keys


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def stamp(out: Path, keys, pid=None) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    p = jobs.stamp_path(out)
    p.write_text(json.dumps({"pid": os.getpid() if pid is None else pid,
                             "started": "2030-12-08T23:34:56+1100", "keys": sorted(keys)}))
    return p


# ---- the key sets ----

def test_drift_guard_bot_keys_are_jobs_keys():
    """A new job key in bot.py cannot land without the stamp learning it. PHONE-129
    landed second, on PHONE-94, so KEYS carries PHONE-94's `group` and `members`."""
    found = bot_keys(BOT.read_text(encoding="utf-8"))
    assert found == set(jobs.KEYS), (f"bot.py reads {sorted(found - jobs.KEYS)} not in jobs.KEYS; "
                                     f"jobs.KEYS has {sorted(jobs.KEYS - found)} bot.py does not read")


def test_the_drift_guard_sees_every_form():
    src = '''job["a"] job.get('b') "c" in job "d" not in job chat_ref(job, "e")
if any(k in job for k in ("f", "g")): pass'''
    assert bot_keys(src) == set("abcdefg")


def test_legacy_keys_are_the_pre_102_bot():
    # bot.py at d6529e2 (the last master before PHONE-102), measured by the
    # forms above: `git show d6529e2:src/sessionphone/bridge/bot.py`
    assert jobs.LEGACY_KEYS == {
        "ack", "answer", "call", "call_id", "call_to", "chat_id", "dm", "file", "greeting",
        "hangup", "html", "info", "invite", "msg_id", "no_speak", "payload", "reaction",
        "summary", "text", "voice", "webxdc_update", "who"}
    # PHONE-102, PHONE-118, PHONE-94 (d6529e2 reads no group key: measured, 0 hits),
    # PHONE-122 (dc-delete: an older bot would take {"delete": ...} for a dc-say),
    # PHONE-126 (sessionphone avatar), PHONE-165 (dc-xdc-realtime: an older bot would
    # take {"webxdc_realtime": ...} for a dc-say with no chat; part 2's feed job too)
    assert jobs.KEYS - jobs.LEGACY_KEYS == {"chat", "voice_message", "group", "members", "delete", "avatar",
                                            "webxdc_realtime", "webxdc_realtime_advertise",
                                            "webxdc_realtime_feed", "feed", "stop"}


# ---- the stamp ----

def test_write_stamp_is_0600_not_a_job_and_holds_the_keys(tmp_path):
    out = tmp_path / "outbox"
    p = jobs.write_stamp(out, tmp_path / "channels.txt")
    assert p == out / ".bot" and not p.name.endswith(".json")
    assert list(out.glob("*.json")) == []                   # the bot's glob never takes it
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    rec = json.loads(p.read_text())
    assert rec["pid"] == os.getpid() and rec["keys"] == sorted(jobs.KEYS)
    assert rec["list"] == str(tmp_path / "channels.txt") and rec["started"] and rec["version"]
    assert [q.name for q in out.iterdir()] == [".bot"]       # no .part left behind


def test_write_stamp_replaces_a_wider_file_with_0600(tmp_path):
    out = tmp_path / "outbox"
    out.mkdir()
    (out / ".bot").write_text("{}")
    (out / ".bot").chmod(0o644)
    jobs.write_stamp(out)
    assert stat.S_IMODE((out / ".bot").stat().st_mode) == 0o600


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"pid": 1}', '{"pid": 1, "keys": "chat"}'])
def test_an_unusable_stamp_reads_as_none(tmp_path, text):
    (tmp_path / ".bot").write_text(text)
    assert jobs.read_stamp(tmp_path) is None


# ---- check(): the matrix ----

NAME_JOB = {"chat": "sessionkit", "text": "hi", "ack": "/x"}         # the field case
NUMBER_JOB = {"chat_id": 20, "text": "hi", "ack": "/x"}
VOICE_JOB = {"chat_id": 13, "file": "/n.ogg", "text": "", "voice_message": True, "ack": "/x"}


def test_a_live_stamp_passes_its_keys(tmp_path):
    stamp(tmp_path, jobs.KEYS)
    for job in (NAME_JOB, NUMBER_JOB, VOICE_JOB):
        assert jobs.check(tmp_path, job) is None


def test_a_live_stamp_refuses_a_key_it_does_not_read(tmp_path):
    stamp(tmp_path, jobs.KEYS - {"voice_message"})
    why = jobs.check(tmp_path, VOICE_JOB)
    assert why.startswith(f"REFUSED: the running bot (pid {os.getpid()}, started 2030-12-08T23:34:56+1100)")
    assert "does not read job key(s) 'voice_message'" in why and "older than this dc-* tool" in why
    assert "restart the bot" in why and "nothing was queued" in why
    assert jobs.check(tmp_path, NUMBER_JOB) is None


def test_no_stamp_passes_legacy_keys_and_refuses_chat_and_voice_message(tmp_path):
    assert jobs.check(tmp_path, NUMBER_JOB) is None          # a plain job to an old bot still goes
    why = jobs.check(tmp_path, NAME_JOB)
    assert f"no bot has stamped {tmp_path / '.bot'}" in why and "too old to stamp its job keys" in why
    assert "does not read job key(s) 'chat'" in why and "restart the bot" in why
    assert "'voice_message'" in jobs.check(tmp_path, VOICE_JOB)
    both = jobs.check(tmp_path, {"chat": "x", "voice_message": True})
    assert "job key(s) 'chat', 'voice_message'" in both


GROUP_JOB = {"group": "Book Club", "members": ["Bob"]}


def test_a_group_job_goes_to_a_current_bot_and_not_to_an_older_one(tmp_path):
    """PHONE-94's group job: a bot stamped with today's KEYS takes it; an unstamped
    bot (the pre-102 keys) and a live stamp that predates it are told to restart."""
    assert "does not read job key(s) 'group', 'members'" in jobs.check(tmp_path, GROUP_JOB)  # no stamp
    stamp(tmp_path, jobs.KEYS - {"group", "members"})
    assert "does not read job key(s) 'group', 'members'" in jobs.check(tmp_path, GROUP_JOB)
    stamp(tmp_path, jobs.KEYS)
    assert jobs.check(tmp_path, GROUP_JOB) is None


def test_a_dead_stamp_is_no_stamp(tmp_path):
    pid = dead_pid()
    stamp(tmp_path, jobs.KEYS, pid=pid)
    why = jobs.check(tmp_path, NAME_JOB)
    assert f"the bot that stamped {tmp_path / '.bot'} (pid {pid}) is not running" in why
    assert "restart the bot" in why
    assert jobs.check(tmp_path, NUMBER_JOB) is None


def test_someone_elses_live_pid_counts_as_alive(tmp_path, monkeypatch):
    def kill(pid, sig):
        raise PermissionError
    monkeypatch.setattr(jobs.os, "kill", kill)
    stamp(tmp_path, jobs.KEYS, pid=1)
    assert jobs.check(tmp_path, NAME_JOB) is None


# ---- main(): what bin/dc-env's dc_shape runs ----

def test_main_exits_0_3_2(tmp_path, capsys):
    assert jobs.main(["check", "--outbox", str(tmp_path), json.dumps(NUMBER_JOB)]) == 0
    assert jobs.main(["check", "--outbox", str(tmp_path), "--tool", "dc-say", json.dumps(NAME_JOB)]) == 3
    err = capsys.readouterr().err
    assert err.startswith("dc-say: REFUSED: no bot has stamped") and "restart the bot" in err
    assert jobs.main(["check", "--outbox", str(tmp_path), "{not json"]) == 2
    assert jobs.main(["check", "--outbox", str(tmp_path), "[]"]) == 2
    assert "nothing was queued" in capsys.readouterr().err


def test_the_module_runs_with_no_delta_chat_import(tmp_path):
    """A tool runs it with whatever python it finds: no deltachat, no dc_env (DNS)."""
    src = Path(jobs.__file__).resolve().parents[1]
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import sessionphone.jobs; "
            "bad = [m for m in sys.modules if m.startswith(('deltachat', 'sessionphone.bridge'))]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(src)], capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_restart_advice_leads_with_kickstart_not_install(tmp_path):
    """do-003 review: a bot `bot install` did not make (the field case runs
    under its own launchd label) gets a SECOND launchd job for the same account
    from a re-run of install. So the refusal names kickstart first, and install
    only for a bot install made; the same rule as `channels seed` (do-002 A1)."""
    for why in (jobs.check(tmp_path, NAME_JOB),                       # no stamp
                (stamp(tmp_path, jobs.KEYS - {"chat"}), jobs.check(tmp_path, NAME_JOB))[1]):
        advice = why.split("restart the bot (", 1)[1]
        assert advice.startswith("kickstart its launchd job: `launchctl kickstart -k gui/UID/LABEL`")
        assert advice.index("kickstart") < advice.index("bot install")
        assert "`sessionphone bot install` again only for a bot install made" in advice

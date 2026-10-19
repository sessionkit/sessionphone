"""The dc-* tools take a channel NAME and refuse before they queue (PHONE-102,
design/allow-list.md rules 2 and 3).

Rule 2: a tool's CHAT may be a number or a NAME. A number keeps today's field;
anything else goes into the job as "chat", JSON-quoted, never pasted raw.

Rule 3 (super ruling): NO REFUSED SEND EXITS 0. The tool asks
sessionphone.channels (the one parser) BEFORE it queues: a list that is
missing, unreadable or loose, or a NAME not on it, exits 3 with the list's
path on stderr and no job written, whatever DC_TAKEN_WAIT says. That
pre-check can only refuse. What it lets through, the bot judges against its
OWN list, and the tool waits for that answer even under DC_TAKEN_WAIT=0, so a
bot refusal exits 3 too.

Fakes only: a fake bot answers each job in its ack file, as the real one does
(tests/test_dc_ack.py). Never a Delta send.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sessionphone import jobs

BIN = Path(__file__).resolve().parent.parent / "bin"
OWNER = "bob@relay.example"
LIST = f"owner  {OWNER}  # owner\nfamily  chat:42  # the family group\n"


def allow(root, text=LIST, mode=0o600):
    root.mkdir(parents=True, exist_ok=True)
    p = root / "channels.txt"
    p.write_text(text)
    p.chmod(mode)
    return p


def run(tool, root, *args, wait="0", env=None, bin_dir=BIN):
    """Run a real tool against ROOT. DC_TAKEN_WAIT defaults to 0 here: the case
    where a refusal used to exit 0."""
    e = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    e.update(DC_ROOT=str(root), DC_TAKEN_WAIT=wait, **(env or {}))
    return subprocess.run([str(bin_dir / tool), *args], env=e, capture_output=True,
                          text=True, timeout=60, check=False)


def jobs_left(root):
    return [p for d in (root / "outbox", root / "public" / "outbox") if d.exists()
            for p in d.iterdir()]


class Bot:
    """A fake bot on one outbox: answer(job) -> (ok, detail), acked BEFORE the
    unlink, as the real bot does. It stamps the job keys it reads, as a running
    bot does since PHONE-129, so a `chat` NAME job may go to it."""

    def __init__(self, out, answer):
        self.out, self.answer, self.jobs = out, answer, []
        self.out.mkdir(parents=True, exist_ok=True)
        jobs.write_stamp(self.out)      # this test's pid: alive
        self.stop = threading.Event()
        self.t = threading.Thread(target=self.loop)

    def loop(self):
        while not self.stop.is_set():
            for f in sorted(self.out.glob("*.json")):
                job = json.loads(f.read_text())
                self.jobs.append(job)
                ok, detail = self.answer(job)
                Path(job["ack"]).write_text(json.dumps({"ok": ok, "detail": detail}))
                f.unlink()
            time.sleep(0.05)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()


def ok_bot(root, sub="outbox"):
    return Bot(root / sub, lambda job: (True, "done"))


def refusing_bot(root, why=None):
    """The real bot's refusal detail, naming ITS list (bot.py gate, channels.check)."""
    path = root / "channels.txt"
    return Bot(root / "outbox", lambda job: (False, why or (
        f"REFUSED: chat 13 is not on the allow-list {path}; nothing was sent. "
        "Add it: sessionphone channels add NAME chat:13")))


def strip(job):
    job = dict(job)
    job.pop("ack")
    return job


# ---- rule 2: a NAME where CHAT_ID went --------------------------------------


def test_dc_say_owner_writes_a_job_with_chat_owner(tmp_path):
    allow(tmp_path)
    with ok_bot(tmp_path) as bot:
        r = run("dc-say", tmp_path, "owner", "hi")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-say: done"            # it waited for the answer
    assert strip(bot.jobs[0]) == {"chat": "owner", "text": "hi"}


def test_dc_say_a_number_keeps_chat_id(tmp_path):
    allow(tmp_path, "twelve  chat:12\n")
    with ok_bot(tmp_path) as bot:
        r = run("dc-say", tmp_path, "12", "hi")
    assert r.returncode == 0, r.stderr
    assert strip(bot.jobs[0]) == {"chat_id": 12, "text": "hi"}


def test_every_tool_that_takes_a_chat_takes_a_name(tmp_path):
    allow(tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("x")
    with ok_bot(tmp_path) as bot:
        for tool, *args in (("dc-send-file", "family", str(f), "look"), ("dc-call", "family"),
                            ("dc-invite", "family", "guest@example.org"),
                            ("dc-hangup", "family"), ("dc-answer", "owner")):
            r = run(tool, tmp_path, *args)
            assert r.returncode == 0 and r.stdout.startswith(f"{tool}: done"), (tool, r.stderr)
    assert [strip(j) for j in bot.jobs] == [
        {"chat": "family", "file": str(f), "text": "look"},
        {"call": None, "chat": "family"},
        {"invite": "guest@example.org", "chat": "family"},
        {"hangup": None, "chat": "family"},
        {"answer": None, "chat": "owner"},
    ]


def test_every_tool_keeps_todays_field_for_a_number(tmp_path):
    allow(tmp_path, "family  chat:42\n")
    f = tmp_path / "a.txt"
    f.write_text("x")
    with ok_bot(tmp_path) as bot:
        for tool, *args in (("dc-send-file", "42", str(f)), ("dc-call", "42"),
                            ("dc-invite", "42", "guest@example.org"),
                            ("dc-hangup", "42"), ("dc-answer", "42")):
            r = run(tool, tmp_path, *args, wait="5")    # hangup/answer by number: no forced wait
            assert r.returncode == 0, (tool, r.stderr)
    assert [strip(j) for j in bot.jobs] == [
        {"chat_id": 42, "file": str(f), "text": ""},
        {"call": 42},
        {"invite": "guest@example.org", "chat_id": 42},
        {"hangup": 42},
        {"answer": 42},
    ]


NASTY = 'x", "chat_id": 13, "y": "\\'


def test_a_name_with_quotes_and_backslashes_cannot_break_the_json(tmp_path):
    """dc-hangup and dc-answer pasted $1 raw into JSON. Now nothing does: a
    nasty NAME is refused cleanly by the pre-check (it is not on the list), and
    a nasty contact name for dc-dm arrives in the job byte for byte."""
    allow(tmp_path)
    for tool in ("dc-hangup", "dc-answer", "dc-say", "dc-call"):
        args = (NASTY, "hi") if tool == "dc-say" else (NASTY,)
        r = run(tool, tmp_path, *args)
        assert r.returncode == 3 and "REFUSED: no channel" in r.stderr, (tool, r.stderr)
        assert jobs_left(tmp_path) == []
    with ok_bot(tmp_path) as bot:
        r = run("dc-dm", tmp_path, NASTY, "hi")
    assert r.returncode == 0, r.stderr
    assert strip(bot.jobs[0]) == {"dm": NASTY, "text": "hi"}


@pytest.mark.parametrize("tool", ["dc-say", "dc-send-file", "dc-call", "dc-invite",
                                  "dc-hangup", "dc-answer"])
def test_usage_says_chat_is_a_number_or_a_channel_name(tmp_path, tool):
    r = run(tool, tmp_path)
    assert r.returncode == 1 and "CHAT" in r.stderr and "a chat number or a channel name" in r.stderr
    assert "CHAT_ID" not in r.stderr
    head = (BIN / tool).read_text().split("\nset -euo")[0]
    assert "channel NAME" in head


def test_dc_dm_usage_says_a_group_channel_posts_into_the_group(tmp_path):
    """FOREMAN (do-002 review advisory 4): 'dm' alone undersells it."""
    r = run("dc-dm", tmp_path)
    assert "a group channel posts into the group" in r.stderr
    assert "posts INTO THE GROUP" in (BIN / "dc-dm").read_text()


# ---- rule 3: the pre-check refuses before anything is queued -----------------


GATED = [("dc-say", ("42", "hi",)), ("dc-send-file", ("42", "FILE",)), ("dc-call", ("42",)),
         ("dc-invite", ("42", "guest@example.org",)), ("dc-react", ("5", "+1",)),
         ("dc-xdc-update", ("5", "{}",)), ("dc-dm", ("Sam", "hi",)), ("dc-ring", ("Sam",))]


def _args(tmp_path, args):
    f = tmp_path / "a.txt"
    f.write_text("x")
    return [str(f) if a == "FILE" else a for a in args]


@pytest.mark.parametrize("tool, args", GATED)
@pytest.mark.parametrize("why, make", [
    ("NO channels file", lambda root: None),
    ("cannot read", lambda root: allow(root, mode=0)),
    ("writable by group or world", lambda root: allow(root, mode=0o660)),
])
def test_an_unusable_list_refuses_with_exit_3_and_writes_no_job(tmp_path, tool, args, why, make):
    if why == "cannot read" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    make(tmp_path)
    r = run(tool, tmp_path, *_args(tmp_path, args), wait="0")
    assert r.returncode == 3, r.stderr
    assert f"{tool}: REFUSED" in r.stderr and why in r.stderr
    assert str(tmp_path / "channels.txt") in r.stderr          # names the list
    assert r.stdout == ""
    assert jobs_left(tmp_path) == [] and not (tmp_path / "acks").exists()


@pytest.mark.parametrize("tool, args", [("dc-say", ("nobody", "hi",)), ("dc-call", ("nobody",)),
                                         ("dc-invite", ("nobody", "g@example.org",)),
                                         ("dc-hangup", ("nobody",)), ("dc-answer", ("nobody",))])
def test_an_unknown_name_refuses_with_exit_3_and_writes_no_job(tmp_path, tool, args):
    p = allow(tmp_path)
    r = run(tool, tmp_path, *args, wait="0")
    assert r.returncode == 3
    assert f"{tool}: REFUSED: no channel 'nobody' in {p}; nothing was sent." in r.stderr
    assert jobs_left(tmp_path) == []


def test_a_number_no_entry_could_pass_is_refused_before_queueing(tmp_path):
    p = allow(tmp_path, "family  chat:42\n")             # no address: only 42 can pass
    r = run("dc-say", tmp_path, "13", "x", wait="0")
    assert r.returncode == 3
    assert f"dc-say: REFUSED: chat 13 is not on the allow-list {p}" in r.stderr
    assert jobs_left(tmp_path) == []


def test_an_all_digit_argument_is_always_a_chat_id(tmp_path):
    """FOREMAN ruling: '12' is chat 12, never a NAME; a NAME '12' is ignored, and said so."""
    allow(tmp_path, "12  chat:42\n")
    r = run("dc-say", tmp_path, "12", "x", wait="0")
    assert r.returncode == 3
    assert "REFUSED: chat 12 is not on the allow-list" in r.stderr and "all digits" in r.stderr


# ---- rule 3: what the pre-check lets through, the bot judges; the tool waits --


def test_dc_say_13_with_a_refusing_bot_exits_3_naming_the_list(tmp_path):
    """The success criterion, verbatim: dc-say 13 x, a fake bot that refuses."""
    p = allow(tmp_path)                     # owner's address is listed: 13 may be its 1:1
    with refusing_bot(tmp_path):
        r = run("dc-say", tmp_path, "13", "x", wait="10")
    assert r.returncode == 3
    assert f"dc-say: FAILED - REFUSED: chat 13 is not on the allow-list {p}" in r.stderr
    assert r.stdout == ""


@pytest.mark.parametrize("tool, args", [("dc-say", ("13", "x",)), ("dc-react", ("5", "+1",)),
                                         ("dc-xdc-update", ("5", "{}",)), ("dc-dm", ("Sam", "hi",)),
                                         ("dc-ring", ("Sam",)), ("dc-call", ("13",)),
                                         ("dc-hangup", ("owner",)), ("dc-answer", ("owner",))])
def test_an_undecidable_target_waits_under_wait_zero_and_a_refusal_exits_3(tmp_path, tool, args):
    """A raw number that may be a listed address's 1:1, a msg id, a contact's
    name, an address channel the bot must open: only the bot can decide, so the
    tool waits for its answer even with DC_TAKEN_WAIT=0."""
    p = allow(tmp_path)
    with refusing_bot(tmp_path) as bot:
        r = run(tool, tmp_path, *args, wait="0")
    assert len(bot.jobs) == 1                                # it was queued ...
    assert r.returncode == 3, (tool, r.stdout, r.stderr)     # ... and the refusal is heard
    assert f"{tool}: FAILED - REFUSED" in r.stderr and str(p) in r.stderr
    assert "not waited for" not in r.stdout


def test_a_list_the_tool_reads_can_never_widen_the_bots(tmp_path):
    """Review fix 1: the bot reads the list from its own environment. A tool
    pointed at a list that names chat 13 (DC_CHANNELS_FILE) gets past the
    pre-check, and still exits 3 when the bot refuses, under DC_TAKEN_WAIT=0."""
    allow(tmp_path, "family  chat:42\n")               # the bot's list: no 13
    mine = tmp_path / "mine.txt"
    mine.write_text("thirteen  chat:13\n")
    mine.chmod(0o600)
    with refusing_bot(tmp_path):
        r = run("dc-say", tmp_path, "13", "x", wait="0", env={"DC_CHANNELS_FILE": str(mine)})
    assert r.returncode == 3 and "FAILED - REFUSED: chat 13" in r.stderr


def test_the_tools_list_can_only_refuse_more(tmp_path):
    """DC_CHANNELS_FILE in the tool's shell names a list the pre-check reads;
    a missing one refuses, naming it."""
    allow(tmp_path)
    r = run("dc-say", tmp_path, "owner", "x", env={"DC_CHANNELS_FILE": str(tmp_path / "gone.txt")})
    assert r.returncode == 3 and f"NO channels file at {tmp_path / 'gone.txt'}" in r.stderr
    assert jobs_left(tmp_path) == []


def test_a_listed_send_with_wait_zero_still_reports_the_bots_answer(tmp_path):
    allow(tmp_path)
    with Bot(tmp_path / "outbox", lambda job: (True, "sent to chat 42, msg 7")):
        r = run("dc-say", tmp_path, "family", "hi", wait="0")
    assert r.returncode == 0 and r.stdout.strip() == "dc-say: sent to chat 42, msg 7"


# ---- the rest ----------------------------------------------------------------


def test_ending_a_call_by_number_needs_no_list(tmp_path):
    """Design row 9: hangup/answer are not gated. By number they work with no
    list, and DC_TAKEN_WAIT=0 still means don't wait. A NAME needs the list."""
    r = run("dc-hangup", tmp_path, "13", wait="0")
    assert r.returncode == 0 and "not waited for" in r.stdout
    assert len(jobs_left(tmp_path)) == 1                     # queued, no bot: left as it was
    other = tmp_path / "other"
    with ok_bot(other) as bot:
        a = run("dc-answer", other, "13", wait="5")
    assert a.returncode == 0, a.stderr
    assert strip(bot.jobs[0]) == {"answer": 13}
    n = run("dc-hangup", tmp_path, "family", wait="0")
    assert n.returncode == 3 and "NO channels file" in n.stderr


def test_the_public_profile_reads_its_own_list(tmp_path):
    """Q2: each profile has its own list, beside its own outbox."""
    allow(tmp_path / "public", "family  chat:42\n")
    with ok_bot(tmp_path, "public/outbox") as bot:
        r = run("dc-say", tmp_path, "--profile", "public", "family", "hi")
    assert r.returncode == 0, r.stderr
    assert strip(bot.jobs[0]) == {"chat": "family", "text": "hi"}
    d = run("dc-say", tmp_path, "family", "hi")              # the default profile has none
    assert d.returncode == 3 and f"NO channels file at {tmp_path / 'channels.txt'}" in d.stderr


def test_a_pre_check_that_cannot_run_queues_nothing(tmp_path):
    """Fail closed: tools with no src/ beside them and a python3 that cannot run
    the pre-check (a stub that fails, so whatever python the test runs under
    cannot stand in) refuse with exit 2 and queue nothing."""
    lone = tmp_path / "lone" / "bin"
    lone.mkdir(parents=True)
    for t in ("dc-env", "dc-say"):
        shutil.copy2(BIN / t, lone / t)
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "python3").write_text("#!/bin/sh\necho 'ModuleNotFoundError: sessionphone' >&2\nexit 1\n")
    (stub / "python3").chmod(0o755)
    root = tmp_path / "root"
    allow(root)
    r = run("dc-say", root, "owner", "hi", bin_dir=lone, env={"PATH": f"{stub}:{os.environ['PATH']}"})
    assert r.returncode == 2
    assert "the allow-list pre-check could not run" in r.stderr and "nothing was queued" in r.stderr
    assert jobs_left(root) == []


# ---- PHONE-129: a list-wide refusal says whether the owner's agent was mailed --
#
# The bot writes <outbox>/.channels-alert when it mails (or fails to mail) the
# owner's agent about a list that refuses every send. The tool's exit-3 stderr
# reads it: mailed, NOT mailed and why, or no bot has reported it.

MSGID = "<channels-alert-1922963696-4242@host.example>"


def _record(root, **kw):
    out = root / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    rec = {"ts": "2030-12-08T23:34:56+1100", "at": time.time(), "path": str(root / "channels.txt"),
           "kind": "missing", "reason": "NO channels file", "to": "alice@host.example", "ok": True,
           "error": None, "message_id": MSGID, "pid": os.getpid(), "started": "2030-12-08T23:34:54+1100",
           "fix": f"DC_CHANNELS_FILE={root / 'channels.txt'} sessionphone channels add owner {OWNER}"}
    rec.update(kw)
    p = out / ".channels-alert"
    p.write_text(json.dumps(rec))
    p.chmod(0o600)
    return rec


def _home(tmp_path):
    return {"HOME": str(tmp_path)}


@pytest.mark.parametrize("tool, args", [("dc-say", ("owner", "hi")), ("dc-xdc-update", ("5", "{}")),
                                         ("dc-dm", ("Sam", "hi"))])
def test_a_missing_list_says_the_owner_was_mailed(tmp_path, tool, args):
    _record(tmp_path)
    r = run(tool, tmp_path, *args, env=_home(tmp_path))
    assert r.returncode == 3
    assert "REFUSED: NO channels file" in r.stderr
    assert f"{tool}: the owner's agent alice@host.example was mailed about this at" in r.stderr
    assert MSGID in r.stderr
    assert [p.name for p in jobs_left(tmp_path)] == [".channels-alert"]     # no job written


def test_no_record_says_not_mailed_and_restart(tmp_path):
    r = run("dc-say", tmp_path, "42", "hi", env=_home(tmp_path))
    assert r.returncode == 3
    assert "dc-say: the owner's agent has NOT been mailed: no running bot has reported this" in r.stderr
    assert "restart it" in r.stderr


def test_a_failed_mail_says_not_mailed_why_and_the_fix(tmp_path):
    _record(tmp_path, ok=False, error="rc=75: queue full", message_id=None)
    r = run("dc-say", tmp_path, "42", "hi", env=_home(tmp_path))
    assert r.returncode == 3
    assert "the owner's agent alice@host.example was NOT mailed: rc=75: queue full." in r.stderr
    assert f"Fix: DC_CHANNELS_FILE={tmp_path / 'channels.txt'} sessionphone channels add owner {OWNER}" \
        in r.stderr


def test_a_record_for_another_kind_names_it(tmp_path):
    _record(tmp_path)                               # the bot mailed about a MISSING list...
    allow(tmp_path, mode=0o660)                     # ...and now it is loose
    r = run("dc-say", tmp_path, "42", "hi", env=_home(tmp_path))
    assert r.returncode == 3
    assert "has NOT been mailed about this: the bot last reported missing for" in r.stderr
    assert "not loose for" in r.stderr


def test_a_dead_bot_record_says_mailed_but_no_bot_runs(tmp_path):
    _record(tmp_path, pid=2 ** 22 + 12345)
    r = run("dc-say", tmp_path, "42", "hi", env=_home(tmp_path))
    assert r.returncode == 3
    assert "was mailed about this at" in r.stderr and "no bot is running now" in r.stderr


def test_a_per_chat_refusal_prints_no_alert_line(tmp_path):
    allow(tmp_path, "family  chat:42\n")
    _record(tmp_path)
    r = run("dc-say", tmp_path, "13", "hi", env=_home(tmp_path))
    assert r.returncode == 3 and "not on the allow-list" in r.stderr
    assert "owner's agent" not in r.stderr


def test_the_public_profile_reads_its_own_record(tmp_path):
    pub = tmp_path / "public"
    _record(pub)
    r = run("dc-say", tmp_path, "--profile", "public", "42", "hi", env=_home(tmp_path))
    assert r.returncode == 3
    assert "was mailed about this" in r.stderr

"""A list that refuses every send tells the owner's agent ONCE (PHONE-129, ask 1).

On one install the bot root had no channels.txt: every dc-* send exited 3, the Desk
was dark for 3.5 h, and nobody was told. Now the bot judges its list at start
and once per pass, and mails the owner's agent (DC_MAILTO) signed by
`message send`, never by Delta, so the closed gate cannot block it. One mail
per (path, kind) per start and per change of reason, never one per refused
job; a restart loop inside ALERT_FLOOR_S mails nothing new
(design/list-refused-loud.md (a)).

Fakes only: no account, no rpc-server, no real `message send`, HOME in tmp_path.
"""
import json
import os
import stat
import threading
import time

import pytest

from sessionphone import channels
from sessionphone import jobs as jobmod
from sessionphone.bridge import bot, dc_env, listalert, seatmail

OWNER = "bob@relay.example"


class Done(Exception):
    """Ends main()'s loop at the Kth sleep: K passes of the outbox."""


class BotTime:
    """bot.py's `time`: the time module, but sleep is the rig's pass counter.
    Only bot.py's own calls see it; the start probe's child wait and every
    thread keep the real one (patched process-wide, the probe's wait could
    count as a pass or end main() before the outbox is read, PHONE-171)."""

    def __init__(self, sleep):
        self.sleep = sleep

    def __getattr__(self, name):
        return getattr(time, name)


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeChat:
    def __init__(self, cid, sent):
        self.id, self.sent = cid, sent

    def send_text(self, text):
        self.sent.append((self.id, text))
        return Obj(id=77)

    def send_message(self, **kw):
        self.sent.append((self.id, kw))
        return Obj(id=78)


class FakeAccount:
    def __init__(self, sent):
        self.sent = sent

    def is_configured(self):
        return True

    def start_io(self):
        pass

    def get_config(self, key):
        return "carol@test"

    def get_chat_by_id(self, cid):
        return FakeChat(cid, self.sent)


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """rig.run(jobs_per_pass, between=fn(i)) runs main() for len(jobs_per_pass)
    passes. Each `message send` is faked and recorded in rig.mails, rendered
    from its argv as To/Subject/--header lines, a blank line and the body
    (PHONE-106: the alert goes by seatmail); every Delta send is recorded in
    rig.delta."""
    out = tmp_path / "outbox"
    lst = tmp_path / "channels.txt"            # NOT written: the field case
    callers = tmp_path / "callers.txt"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DC_CHANNELS_FILE", str(lst))
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "OUTBOX", out)
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "server_choice", lambda: (None, "package", "test"))
    monkeypatch.setattr(bot, "sweep", lambda account: None)
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "INVITE", None)
    monkeypatch.setattr(bot, "PROFILE", "")
    # bot.py copies sys.argv[1] into DC_AGENT at import, and under pytest that is
    # a pytest flag or path, so the box the bot checks at start would be named
    # after however this run was invoked. Pin it to the bot the suite bound, so
    # the box checked is the box the alert is addressed to.
    monkeypatch.setenv("DC_AGENT", dc_env.AGENT)
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)

    class Rig:
        pass
    r = Rig()
    r.tmp, r.out, r.list, r.callers, r.logged = tmp_path, out, lst, callers, logged
    r.mails, r.delta, r.sendmail = [], [], []   # each send: True, False or an exception, in turn
    r.argv, r.ack = [], 0
    r.calls = []                                # every `message` call but the probe, in order

    def fake_run(argv, input=None, **kw):
        if argv[1:] == seatmail.PROBE_ARGS:     # the start probe (PHONE-72 2b) sends nothing
            return Obj(returncode=2, stderr="", stdout=json.dumps({"sent": "no", "kind": "message-id"}))
        r.calls.append(argv[1:])
        if argv[1:2] == ["path"]:               # the box check at start (seat.have_box): the box is there
            return Obj(returncode=0, stderr="", stdout=f"{tmp_path / 'boxes' / argv[2]}\n")
        assert argv[1:2] == ["send"] and "--json" in argv, argv
        r.argv.append(argv)
        heads = [argv[i + 1] for i, a in enumerate(argv) if a == "--header"]
        r.mails.append(f"To: {argv[2]}\nSubject: {argv[argv.index('-s') + 1]}\n"
                       + "".join(f"{h}\n" for h in heads) + "\n" + input)
        verdict = r.sendmail.pop(0) if r.sendmail else True
        if isinstance(verdict, BaseException):
            r.mails.pop()
            raise verdict
        if verdict is not True:
            r.mails.pop()                       # refused: nothing was accepted
            return Obj(returncode=3, stderr="", stdout=json.dumps(
                {"sent": "no", "kind": "mta", "message": "queue full", "notices": []}))
        return Obj(returncode=0, stderr="", stdout=json.dumps(
            {"sent": "yes", "id": f"<{len(r.mails)}.4242.7@host.session-mail>", "notices": []}))
    monkeypatch.setattr(bot.subprocess, "run", fake_run)

    def run(*passes, between=lambda i: None):
        stop = threading.Event()
        monkeypatch.setattr(bot, "DeltaChat", lambda rpc: Obj(get_all_accounts=lambda: [FakeAccount(r.delta)]))
        monkeypatch.setattr(bot, "handle_events", lambda account: stop.wait())
        out.mkdir(parents=True, exist_ok=True)
        acks = []
        n = {"pass": 0}

        def drop(jobs):
            for job in jobs:
                a = tmp_path / "acks" / f"{r.ack:03d}.json"
                r.ack += 1
                acks.append(a)
                (out / f"{r.ack:03d}.json").write_text(json.dumps(dict(job, ack=str(a))))
        drop(passes[0] if passes else [])

        def sleep(s):
            between(n["pass"])
            n["pass"] += 1
            if n["pass"] >= len(passes):
                raise Done
            drop(passes[n["pass"]])
        monkeypatch.setattr(bot, "time", BotTime(sleep))
        try:
            with pytest.raises(Done):
                bot.main()
        finally:
            stop.set()
        return [json.loads(a.read_text()) for a in acks]
    r.run = run
    return r


def _alerts(rig):
    return [m for m in rig.mails if "X-Delta-Chat-Alert:" in m]


# ---- criterion 1: ONE alert for N refused jobs ------------------------------

def test_one_alert_for_n_refused_jobs_and_no_delta_send(rig):
    """5 jobs in one pass, then 3 more passes: 1 mail. Every ack is a refusal,
    and nothing went by Delta: the alert went out while the list refused every send."""
    jobs = [{"chat_id": 13, "text": f"hi {i}"} for i in range(5)]
    acks = rig.run(jobs, [{"chat_id": 13, "text": "again"}], [], [])
    assert len(_alerts(rig)) == 1
    assert len(acks) == 6 and all(a["ok"] is False for a in acks)
    assert all("NO channels file" in a["detail"] for a in acks)
    assert rig.delta == []                  # the gate held: no Delta send at all


def test_the_bot_checks_its_box_before_the_first_send(rig):
    """The mail drop never makes a box, and refuses a send to one that is not
    there, so the bot asks `message path` for its box at start, reads only,
    and only then sends. The box asked is the one the alert is addressed to,
    and a second start asks again."""
    box = seatmail.local_part(dc_env.MAILTO)
    rig.run([])
    assert [c[:2] for c in rig.calls] == [["path", box], ["send", dc_env.MAILTO]]
    rig.run([])                                   # a restart inside the floor: no new mail
    assert [c[:2] for c in rig.calls[2:]] == [["path", box]]
    assert len(_alerts(rig)) == 1


def test_an_alert_goes_out_with_no_job_at_all(rig):
    """The field case: the tools refused before queueing, so the bot saw no job."""
    rig.run([])
    (mail,) = _alerts(rig)
    assert f"To: {dc_env.MAILTO}\n" in mail
    assert any(line.startswith("CHANNELS ALERT mailed to ") for line in rig.logged)


# ---- a change of reason mails again; the same reason does not ---------------

def test_missing_then_loose_mails_twice(rig):
    def between(i):
        if i == 0:
            rig.list.write_text(f"owner  {OWNER}\n")
            rig.list.chmod(0o660)
    rig.run([], [], [], between=between)
    kinds = [m.split("X-Delta-Chat-Alert: ")[1].split("\n")[0] for m in _alerts(rig)]
    assert kinds == ["channels-missing", "channels-loose"]


def test_missing_then_ok_then_missing_is_a_new_outage(rig):
    def between(i):
        if i == 0:
            rig.list.write_text(f"owner  {OWNER}\n")
            rig.list.chmod(0o600)
        if i == 1:
            rig.list.unlink()
    rig.run([], [], [], between=between)
    assert len(_alerts(rig)) == 2


def test_the_same_reason_across_passes_mails_once(rig):
    rig.list.write_text("x")
    rig.list.chmod(0o666)
    rig.run([], [], [], [], [])
    assert len(_alerts(rig)) == 1


def test_a_reworded_refusal_is_not_a_new_reason(rig, monkeypatch):
    """The key is the kind, never the text."""
    texts = iter(["NO channels file at A", "NO channels file at B (reworded)"])
    real = channels.load

    def load(path=None, log=None):
        lst = real(path, log)
        if lst.kind == "missing":
            lst.refusal = next(texts, "NO channels file, again")
        return lst
    monkeypatch.setattr(channels, "load", load)
    rig.run([], [], [])
    assert len(_alerts(rig)) == 1


# ---- the cross-restart floor (FOREMAN ruling: 1 h) ---------------------------

def test_two_starts_inside_the_floor_mail_once_and_restamp_the_record(rig, monkeypatch):
    rig.run([])
    rec1 = listalert.read_record(rig.out)
    assert rec1["ok"] is True and rec1["kind"] == "missing"
    # run 1 was another process, now dead: a pid no process has
    dead = 2 ** 22 + 12345
    listalert.write_record(rig.out, dict(rec1, pid=dead))
    assert "has NOT been mailed" not in listalert.tool_line(rig.out, rig.list, "missing", "dc-say")
    rig.run([])                                   # run 2, a launchd restart
    assert len(_alerts(rig)) == 1
    rec2 = listalert.read_record(rig.out)
    assert rec2["pid"] == os.getpid() != dead     # F1: the bot that last judged the key
    assert rec2["message_id"] == rec1["message_id"] and rec2["ts"] == rec1["ts"]
    line = listalert.tool_line(rig.out, rig.list, "missing", "dc-say")
    assert "was mailed" in line and rec1["message_id"] in line and "NOT" not in line
    assert any("already mailed" in m and "not again before" in m for m in rig.logged)


def test_two_starts_past_the_floor_mail_twice(rig, monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(listalert, "now", lambda: t["now"])
    rig.run([])
    t["now"] += listalert.ALERT_FLOOR_S + 1
    rig.run([])
    assert len(_alerts(rig)) == 2


def test_a_new_reason_at_restart_mails_at_once(rig):
    rig.run([])
    rig.list.write_text("x")
    rig.list.chmod(0o666)
    rig.run([])
    assert len(_alerts(rig)) == 2


# ---- a failed send retries; only an accepted send spends the key ------------

def test_a_failed_send_retries_after_alert_retry_s(rig, monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(listalert, "now", lambda: t["now"])
    rig.sendmail[:] = [False]
    seen = []

    def between(i):
        seen.append(listalert.read_record(rig.out)["ok"])
        if i == 1:
            t["now"] += listalert.ALERT_RETRY_S + 1
    rig.run([], [], [], between=between)
    assert len(_alerts(rig)) == 1                 # accepted once
    assert seen == [False, False, True]           # not before ALERT_RETRY_S, then ok
    assert any(m.startswith("CHANNELS ALERT NOT MAILED to ") and "queue full" in m for m in rig.logged)


def test_a_sendmail_that_raises_is_a_failed_send_not_a_crash(rig):
    rig.sendmail[:] = [OSError("no sendmail")]
    rig.run([], [])
    rec = listalert.read_record(rig.out)
    assert rec["ok"] is False and "OSError: no sendmail" in rec["error"]


# ---- what the mail says --------------------------------------------------------

def test_the_mail_names_path_reason_and_the_owner_fix(rig):
    rig.callers.write_text(f"{OWNER}  # owner, joined 2030-06-09\n")
    rig.run([])
    (mail,) = _alerts(rig)
    head, body = mail.split("\n\n", 1)
    subject = next(line for line in head.splitlines() if line.startswith("Subject: "))[9:]
    assert len(subject) <= 69 and subject.isascii()
    assert subject == "[channels] REFUSING EVERY SEND: no channels.txt"
    assert head.isascii()
    assert f"To: {dc_env.MAILTO}" in head and "X-Delta-Chat-Alert: channels-missing" in head
    assert "Message-ID" not in head             # message send mints the id (PHONE-106)
    a = rig.argv[0]
    assert a[a.index("-f") + 1] == "-" and {"--raw", "--no-shorten", "--json"} <= set(a)
    assert f"list:    {rig.list}" in body
    assert f"reason:  NO channels file at {rig.list}" in body
    assert f"DC_CHANNELS_FILE={rig.list} sessionphone channels add owner {OWNER}" in body
    assert "channels add NAME chat:N" in body
    assert "not again within the hour" in body


@pytest.mark.parametrize("kind, want", [
    ("missing", "sessionphone link"),
    ("loose", "read it first (cat {p}), then chmod 600 {p}"),
    ("unreadable", "ls -l {p}"),
])
def test_the_fix_command_for_each_kind(tmp_path, kind, want):
    p = tmp_path / "channels.txt"
    fix = listalert.fix_command(kind, p, None)
    assert want.format(p=p) in fix[0]


def test_the_fix_command_quotes_a_path_with_spaces(tmp_path):
    p = tmp_path / "my bot" / "channels.txt"
    assert f"DC_CHANNELS_FILE='{p}' " in listalert.fix_command("missing", p, OWNER)[0]


def test_the_public_profile_says_so_in_subject_and_header(rig, monkeypatch):
    monkeypatch.setattr(bot, "PROFILE", "public")
    rig.run([])
    (mail,) = _alerts(rig)
    assert "Subject: [public channels] REFUSING EVERY SEND" in mail
    assert "X-Delta-Chat-Profile: public\n" in mail


# ---- the record and the ack ----------------------------------------------------

def test_the_record_is_0600_never_a_job_and_the_ack_says_mailed(rig):
    acks = rig.run([{"chat_id": 13, "text": "hi"}])
    path = listalert.record_path(rig.out)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path not in list(rig.out.glob("*.json"))
    assert path.exists()                          # the loop never took it as a job
    rec = listalert.read_record(rig.out)
    (ack,) = acks
    assert ack["ok"] is False
    assert f"; the owner's agent {rec['to']} was mailed at {rec['ts']}" in ack["detail"]


def test_a_refused_ack_says_not_mailed_and_why(rig):
    rig.sendmail[:] = [False]
    (ack,) = rig.run([{"chat_id": 13, "text": "hi"}])
    assert "; the owner's agent was NOT mailed: rc=3 kind=mta: queue full" in ack["detail"]


def test_a_per_chat_refusal_mails_nothing_and_adds_no_note(rig):
    rig.list.write_text("family  chat:42\n")
    rig.list.chmod(0o600)
    (ack,) = rig.run([{"chat_id": 13, "text": "hi"}])
    assert ack["ok"] is False and "not on the allow-list" in ack["detail"]
    assert "owner's agent" not in ack["detail"]
    assert _alerts(rig) == []


def test_a_good_list_mails_nothing(rig):
    rig.list.write_text("thirteen  chat:13\n")
    rig.list.chmod(0o600)
    (ack,) = rig.run([{"chat_id": 13, "text": "hi"}], [])
    assert ack["ok"] is True and rig.mails == []


def test_bot_mail_in_and_the_alert_go_by_message_send(monkeypatch):
    """PHONE-106: mail_in (a chat line) and the alert both go by `message send`,
    signed; mail_in's failure line keeps its BRIDGE MAIL FAILED lead."""
    logged, argvs = [], []
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot.subprocess, "run", lambda argv, **k: argvs.append(argv) or Obj(
        returncode=3, stderr="", stdout='{"sent": "no", "kind": "mta", "message": "busy"}'))
    assert bot.mail_in("Caller", 12, "Caller", "hi") is False
    assert len(logged) == 1 and logged[0].startswith("BRIDGE MAIL FAILED rc=3 kind=mta: busy, kept at ")
    assert bot.send_alert("s", "b", {"X-Delta-Chat-Alert": "channels-missing"}) == (
        False, "rc=3 kind=mta: busy", None)
    assert [a[1] for a in argvs] == ["send", "send"]
    assert "--header" in argvs[1] and "X-Delta-Chat-Alert: channels-missing" in argvs[1]


# ---- kind: load() names the reason -----------------------------------------------

def test_load_sets_kind_missing(tmp_path):
    assert channels.load(tmp_path / "none.txt").kind == "missing"


def test_load_sets_kind_loose(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("x  chat:1\n")
    p.chmod(0o660)
    assert channels.load(p).kind == "loose"


@pytest.mark.parametrize("make", ["mode000", "fifo", "utf8"])
def test_load_sets_kind_unreadable(tmp_path, make):
    p = tmp_path / "c.txt"
    if make == "mode000":
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        p.write_text("x  chat:1\n")
        p.chmod(0)
    elif make == "fifo":
        os.mkfifo(p, 0o600)
    else:
        p.write_bytes(b"x  chat:1  # \xff\xfe\n")
        p.chmod(0o600)
    lst = channels.load(p)
    assert lst.refusal and lst.kind == "unreadable"


def test_a_good_list_has_no_kind(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("x  chat:1\n")
    p.chmod(0o600)
    assert channels.load(p).kind is None


# ---- README + CHANGELOG name the alert (FOREMAN ruling) ------------------------

def test_readme_and_changelog_name_the_alert():
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text()
    assert "not again\nwithin the hour" in readme or "not again within the hour" in readme
    for quoted in ("CHANNELS ALERT mailed to", "CHANNELS ALERT NOT MAILED to", ".channels-alert",
                   "[channels] REFUSING EVERY SEND: no channels.txt"):
        assert quoted in readme                   # each quote is a line the code writes
    changelog = (root / "CHANGELOG.md").read_text().split("## [0.1.0]")[0]
    assert "mails the bot's agent" in changelog and "PHONE-129" in changelog


# ---- do-002: the bot never seeds; the alert names the seed; the id names the kind ----

def test_the_bot_never_seeds_the_list_at_start(rig):
    """Rule 4 (fail closed, Q5): a missing list stays missing, owner line or not.
    Only `sessionphone channels seed` (or `bot install`), run by someone, seeds it."""
    rig.callers.write_text(f"{OWNER}  # owner, joined 2030-06-09\n")
    acks = rig.run([{"chat_id": 13, "text": "hi"}], [], [])
    assert not rig.list.exists()
    assert acks[0]["ok"] is False and rig.delta == []
    assert len(_alerts(rig)) == 1


def test_the_alert_names_the_seed_with_the_bots_callers_file(rig):
    rig.callers.write_text(f"{OWNER}  # owner\n")
    rig.run([])
    (mail,) = _alerts(rig)
    assert (f"or, the same as install: DC_CHANNELS_FILE={rig.list} DC_CALLERS_FILE={rig.callers} "
            "sessionphone channels seed") in mail
    rec = listalert.read_record(rig.out)
    assert rec["fix"].endswith(f"sessionphone channels add owner {OWNER}")   # the first line, unchanged


def test_no_owner_line_means_no_seed_line(tmp_path):
    fix = listalert.fix_command("missing", tmp_path / "channels.txt", None, tmp_path / "callers.txt")
    assert not any("channels seed" in line for line in fix)


def test_a_reason_change_inside_one_second_records_each_sends_own_id(rig, monkeypatch):
    """fix-001 review A1, under PHONE-106: `message send` mints each id, so two
    alerts inside one second carry two ids, and the record keeps the one printed."""
    monkeypatch.setattr(listalert, "now", lambda: 1_000_000.0)
    ids = []

    def between(i):
        ids.append(listalert.read_record(rig.out)["message_id"])
        if i == 0:
            rig.list.write_text(f"owner  {OWNER}\n")
            rig.list.chmod(0o660)
    rig.run([], [], between=between)
    assert len(_alerts(rig)) == 2
    assert ids == ["<1.4242.7@host.session-mail>", "<2.4242.7@host.session-mail>"]
    assert not any("--header" in a and any(h.startswith("Message-ID") for h in a) for a in rig.argv)


# ---- do-003: the bot stamps the job keys it reads (design (c)) ----

def test_main_stamps_the_job_keys_it_reads_in_its_outbox(rig):
    """At start, beside the list alert: <OUTBOX>/.bot, 0600, this pid, jobmod.KEYS,
    and the list the bot reads. The bot's own glob never takes it for a job."""
    rig.list.write_text(f"owner  {OWNER}\n")
    rig.list.chmod(0o600)
    rig.run([])
    p = rig.out / ".bot"
    rec = json.loads(p.read_text())
    assert rec["pid"] == os.getpid() and rec["keys"] == sorted(jobmod.KEYS)
    assert rec["list"] == str(rig.list)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert f"job keys stamped in {p}" in rig.logged
    assert jobmod.check(rig.out, {"chat": "owner", "voice_message": True, "text": "x"}) is None


def test_a_stamp_that_cannot_be_written_is_logged_and_the_bot_runs_on(rig, monkeypatch):
    def refuse(*a, **kw):
        raise OSError("read-only")
    monkeypatch.setattr(bot.jobs, "write_stamp", refuse)
    rig.run([])
    assert f"JOB STAMP NOT WRITTEN in {rig.out}: read-only" in rig.logged
    assert not (rig.out / ".bot").exists()

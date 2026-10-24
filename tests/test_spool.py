"""PHONE-106 do-003: a mail to the agent that fails is kept, not lost (contract §2.4).

The undelivered spool (bridge/spool.py): kept on failure, retried by the
refusal's KIND with a per-mail backoff, oldest first, never one mail blocking
the next; dead/ at once for a kind that never heals, or after 24 h. Fakes only:
a fake `message`, a fake clock, scratch HOME and DC_ROOT. No real message,
sendmail, seat key or bot.
"""
import json
import os
import pathlib
import shutil
import stat
import subprocess
import threading
import time
import types

import pytest

from sessionphone.bridge import bot, dc_env, seatmail, spool

REPO = pathlib.Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def seat(monkeypatch, seat_key):
    seat_key("ada-chat seat")                       # the key's seat is the bot's (PHONE-72 part 2b)
    monkeypatch.setattr(dc_env, "ADDR", "ada-chat@example.net")
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Ada")
    monkeypatch.setattr(dc_env, "MAILTO", "ada@example.net")
    monkeypatch.setattr(bot, "PROFILE", "")


@pytest.fixture
def rig(seat, monkeypatch, undelivered_in_tmp):
    """bot.SPOOL on a fake clock, logging into rig.logged."""
    r = types.SimpleNamespace(clock=Clock(), logged=[], dir=undelivered_in_tmp)
    monkeypatch.setattr(bot, "log", r.logged.append)
    r.spool = spool.Spool(r.logged.append, clock=r.clock)
    monkeypatch.setattr(bot, "SPOOL", r.spool)
    r.waiting = lambda: sorted(p.name for p in r.dir.glob("*.json"))
    r.dead = lambda: sorted(p.name for p in (r.dir / "dead").glob("*.json"))
    r.rec = lambda name: json.loads((r.dir / name).read_text())
    return r


def refusal(kind, sent="no", rc=None):
    rc = rc if rc is not None else (3 if kind in ("mta", "undeliverable") else 2)
    return rc, json.dumps({"sent": sent, "kind": kind, "message": f"refused: {kind}", "notices": []})


OK = (0, json.dumps({"sent": "yes", "id": "<9.9.9@host.session-mail>", "notices": []}))


@pytest.fixture
def script(tmp_path, monkeypatch):
    """A fake `message` that answers from a script of (rc, stdout), one per run, and
    records each run's argv and body. The last answer repeats. The start probe
    (seatmail.PROBE_ARGS, PHONE-72 part 2b) is answered as SM-716 does and not run."""
    plan = tmp_path / "plan.json"
    runs = tmp_path / "runs.jsonl"
    exe = tmp_path / "bin" / "message"
    exe.parent.mkdir()
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"plan, runs = {str(plan)!r}, {str(runs)!r}\n"
        f"if sys.argv[1:] == {seatmail.PROBE_ARGS!r}:\n"
        "    print(json.dumps({'sent': 'no', 'kind': 'message-id'})); sys.exit(2)\n"
        "p = json.load(open(plan))\n"
        "n = sum(1 for _ in open(runs)) if __import__('os').path.exists(runs) else 0\n"
        "rc, out = p[min(n, len(p) - 1)]\n"
        "open(runs, 'a').write(json.dumps({'argv': sys.argv[1:], 'body': sys.stdin.read()}) + '\\n')\n"
        "print(out)\n"
        "sys.exit(rc)\n")
    exe.chmod(0o755)
    monkeypatch.setenv("DC_MESSAGE", str(exe))
    ns = types.SimpleNamespace()
    ns.plan = lambda *answers: plan.write_text(json.dumps(list(answers)))
    ns.runs = lambda: [json.loads(line) for line in runs.read_text().splitlines()] if runs.exists() else []
    return ns


# ---- the criterion: fake message exits 2, then 3, then 0 ---------------------

def test_2_then_3_then_0_the_spool_holds_the_mail_and_the_retry_empties_it(rig, script):
    script.plan(refusal("identity"), refusal("mta"), OK)
    assert bot.mail_in("Team", 12, "Bob", "are you there") is False      # exit 2: kept
    (name,) = rig.waiting()
    rec = rig.rec(name)
    assert rec["attempts"] == 1 and rec["kind"] == "identity"
    assert rec["args"][:2] == ["send", "ada@example.net"] and "--json" in rec["args"]
    assert not any(str(a).startswith("/") and a.endswith("message") for a in rec["args"])  # no binary
    assert "env" not in rec and "CLAUDE_SESSION_AGENT_SOCK" not in json.dumps(rec)
    assert rec["body"].startswith("are you there\n\n---\n")
    assert rig.logged[0] == f"BRIDGE MAIL FAILED rc=2 kind=identity: refused: identity, kept at {rig.dir / name}"

    rig.spool.drain()                                   # not due yet: nothing runs
    assert len(script.runs()) == 1

    rig.clock.now += spool.BACKOFF_S
    rig.spool.drain()                                   # exit 3: still kept, rescheduled
    assert rig.waiting() == [name] and rig.rec(name)["attempts"] == 2
    assert rig.rec(name)["next_try"] == rig.clock.now + spool.backoff(2) == rig.clock.now + 120
    assert any(m.startswith("BRIDGE MAIL RETRY 2 FAILED rc=3 kind=mta: ") for m in rig.logged)

    rig.clock.now += spool.backoff(2)
    rig.spool.drain()                                   # exit 0: sent, and the spool is empty
    assert rig.waiting() == [] and rig.dead() == []
    runs = script.runs()
    assert len(runs) == 3 and runs[2]["argv"] == runs[0]["argv"] and runs[2]["body"] == runs[0]["body"]
    assert any(m.startswith("BRIDGE MAIL SENT on try 3 ") and "<9.9.9@host.session-mail>" in m
               for m in rig.logged)


def test_a_retry_rebuilds_the_env_so_it_signs_with_the_seat(rig, script, monkeypatch, tmp_path):
    script.plan(refusal("identity"), OK)
    bot.mail_in("Team", 12, "Bob", "hi")
    seen = []
    real = subprocess.run
    monkeypatch.setattr(bot.subprocess, "run", lambda *a, **k: seen.append(k["env"]) or real(*a, **k))
    rig.clock.now += spool.BACKOFF_S
    rig.spool.drain()
    (env,) = seen
    assert env["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1" and env["TM_AGENT_ADDRESS"] == "ada-chat"
    assert env["CLAUDE_SESSION_AGENT_SOCK"] == os.environ["DC_SIGNER_SOCK"]


# ---- the criterion: A fails forever, B still goes; subject dead; mta unknown --

def test_mail_a_failing_forever_never_blocks_mail_b(rig):
    rig.spool.keep(seatmail.Sent(False, "rc=2 kind=identity: x", kind="identity", args=["send", "a"]), "A")
    rig.clock.now += 1
    rig.spool.keep(seatmail.Sent(False, "rc=2 kind=identity: x", kind="identity", args=["send", "b"]), "B")
    tried = []

    def send(args, body):
        tried.append(body)
        if body == "A":
            return seatmail.Sent(False, "rc=2 kind=identity: still dead", kind="identity", args=args)
        return seatmail.Sent(True, id="<b@h>", sent="yes", args=args)
    rig.spool._send = send
    for _ in range(6):
        rig.clock.now += spool.BACKOFF_MAX_S
        rig.spool.drain()
    assert tried[:2] == ["A", "B"]                      # oldest first, and A did not stop B
    assert tried.count("B") == 1 and tried.count("A") == 6
    (a,) = rig.waiting()
    assert rig.rec(a)["body"] == "A" and rig.rec(a)["attempts"] == 7 and rig.dead() == []


def test_a_subject_refusal_lands_in_dead_at_once(rig, script):
    script.plan(refusal("subject"))
    assert bot.mail_in("Team", 12, "Bob", "hi") is False
    assert rig.waiting() == []
    (name,) = rig.dead()
    assert rig.rec(f"dead/{name}")["kind"] == "subject"
    assert rig.logged[-1] == f"BRIDGE MAIL DEAD {rig.dir / 'dead' / name}: kind subject is never retried"
    rig.clock.now += spool.CAP_S
    rig.spool.drain()
    assert len(script.runs()) == 1                      # never retried


@pytest.mark.parametrize("kind", ["usage", "address", "header", "body", "thread-id", None])
def test_every_kind_that_never_heals_is_dead_at_once(rig, kind):
    rig.spool.keep(seatmail.Sent(False, "rc=2: x", kind=kind, args=["send"]), "b")
    assert rig.waiting() == [] and len(rig.dead()) == 1


@pytest.mark.parametrize("kind", sorted(spool.RETRIED))
def test_every_kind_that_heals_waits(rig, kind):
    rig.spool.keep(seatmail.Sent(False, "rc=3: x", kind=kind, args=["send"]), "b")
    assert len(rig.waiting()) == 1 and rig.dead() == []


def test_mta_sent_unknown_is_retried_and_logged_sent_unknown(rig, script):
    script.plan(refusal("mta", sent="unknown"), refusal("mta", sent="unknown"), OK)
    bot.mail_in("Team", 12, "Bob", "hi")
    (name,) = rig.waiting()
    assert rig.rec(name)["sent"] == "unknown"
    assert rig.logged[0].startswith("BRIDGE MAIL FAILED rc=3 kind=mta sent=unknown: ")
    rig.clock.now += spool.BACKOFF_S
    rig.spool.drain()
    assert any(m.startswith("BRIDGE MAIL RETRY 2 FAILED rc=3 kind=mta sent=unknown: ") for m in rig.logged)
    rig.clock.now += spool.backoff(2)
    rig.spool.drain()
    assert rig.waiting() == [] and len(script.runs()) == 3
    assert any(m.startswith("BRIDGE MAIL SENT on try 3 ") and "last sent=unknown" in m for m in rig.logged)


def test_burying_never_overwrites_a_mail_already_dead(rig):
    """keep() numbers dead/ and the spool apart, so in one second a healing mail
    (A, <epoch>-1.json) and a dead-at-once mail (B, dead/<epoch>-1.json) share a
    name. When A is buried at the cap, B must survive: nothing is ever deleted."""
    rig.spool.keep(seatmail.Sent(False, "rc=2 kind=identity: x", kind="identity", args=["send"]), "A")
    rig.spool.keep(seatmail.Sent(False, "rc=2 kind=subject: x", kind="subject", args=["send"]), "B")
    assert rig.waiting() == rig.dead() == [f"{int(rig.clock.now)}-1.json"]
    rig.clock.now += spool.CAP_S
    rig.spool.drain()
    assert rig.waiting() == []
    assert sorted(rig.rec(f"dead/{n}")["body"] for n in rig.dead()) == ["A", "B"]
    assert rig.dead() == [f"{int(rig.clock.now - spool.CAP_S)}-{n}.json" for n in (1, 2)]
    assert rig.logged[-1].startswith(f"BRIDGE MAIL DEAD {rig.dir / 'dead' / rig.dead()[1]}: still failing")


def test_a_retry_that_turns_into_a_dead_kind_is_buried(rig):
    rig.spool.keep(seatmail.Sent(False, "rc=2 kind=identity: x", kind="identity", args=["send"]), "b")
    rig.spool._send = lambda a, b: seatmail.Sent(False, "rc=2 kind=header: bad", kind="header", args=a)
    rig.clock.now += spool.BACKOFF_S
    rig.spool.drain()
    (name,) = rig.dead()
    rec = rig.rec(f"dead/{name}")
    assert rig.waiting() == [] and rec["attempts"] == 2 and rec["kind"] == "header"
    assert [t["kind"] for t in rec["tries"]] == ["identity", "header"]


def test_past_24h_a_mail_is_dead_and_kept(rig):
    rig.spool.keep(seatmail.Sent(False, "rc=3 kind=mta: x", kind="mta", args=["send"]), "b")
    rig.spool._send = lambda a, b: seatmail.Sent(False, "rc=3 kind=mta: x", kind="mta", args=a)
    while rig.waiting():
        rig.clock.now += spool.BACKOFF_MAX_S
        rig.spool.drain()
    (name,) = rig.dead()
    rec = rig.rec(f"dead/{name}")
    assert rec["body"] == "b" and "still failing after 24 h" in rec["dead_why"]
    assert rig.clock.now - rec["created"] >= spool.CAP_S
    assert any(m.startswith("BRIDGE MAIL DEAD ") and "24 h" in m for m in rig.logged)


# ---- the spool's own shape ----------------------------------------------------

def test_backoff_doubles_from_60s_to_an_hour():
    assert [spool.backoff(n) for n in range(1, 9)] == [60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_files_are_private_and_named_epoch_n(rig):
    for _ in range(3):
        rig.spool.keep(seatmail.Sent(False, "x", kind="mta", args=["send"]), "b")
    assert rig.waiting() == ["1000000-1.json", "1000000-2.json", "1000000-3.json"]
    for p in rig.dir.glob("*.json"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not list(rig.dir.glob(".*"))                 # no tmp file left behind


def test_the_dirs_are_0700(tmp_path):
    s = spool.Spool(lambda m: None, root=tmp_path / "u")
    s.keep(seatmail.Sent(False, "x", kind="subject", args=["send"]), "b")
    assert stat.S_IMODE((tmp_path / "u").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "u" / "dead").stat().st_mode) == 0o700


def test_oldest_first_by_epoch_then_n_not_by_text(rig):
    for name in ("1000000-10.json", "1000000-9.json", "999999-1.json"):
        (rig.dir / name).write_text(json.dumps({"args": [], "body": name, "created": 0, "next_try": 0}))
    assert [p.name for p in rig.spool.waiting()] == ["999999-1.json", "1000000-9.json", "1000000-10.json"]


@pytest.mark.parametrize("raw", [
    "{not json",
    "[1, 2]",
    json.dumps({"body": "no args", "created": 1_000_000.0, "next_try": 0}),
    json.dumps({"args": "send", "body": "b", "created": 1_000_000.0}),
    json.dumps({"args": ["send"], "body": None, "created": 1_000_000.0}),
    json.dumps({"args": ["send"], "body": "b", "created": 1_000_000.0, "next_try": "soon"}),
    json.dumps({"args": ["send"], "body": "b", "created": 1_000_000.0, "attempts": "2.5"}),
    '{"args": ["send"], "body": "b", "created": 1000000.0, "attempts": Infinity}',
    json.dumps({"args": ["send"], "body": "b", "created": 1_000_000.0, "tries": "none"}),
], ids=["not json", "not an object", "no args", "args not a list", "body not text", "next_try not a number",
        "attempts not an int", "attempts infinite", "tries not a list"])
def test_a_corrupt_record_never_stops_the_rest_and_is_dead_once(rig, raw):
    """Review of do-003: an unreadable record used to stay 'waiting' forever and
    log BRIDGE MAIL RETRY on every pass, once a second. Now: dead/ once, as it was."""
    (rig.dir / "1-1.json").write_text(raw)
    rig.spool.keep(seatmail.Sent(False, "x", kind="mta", args=["send"]), "b")
    sent = []
    rig.spool._send = lambda a, b: sent.append(b) or seatmail.Sent(True, sent="yes", args=a)
    rig.clock.now += spool.BACKOFF_S
    rig.spool.drain()
    assert sent == ["b"] and rig.waiting() == []        # the good mail still went
    assert rig.dead() == ["1-1.json"]
    assert (rig.dir / "dead" / "1-1.json").read_text() == raw     # kept as it was
    dead = [m for m in rig.logged if m.startswith("BRIDGE MAIL DEAD ")]
    assert len(dead) == 1 and "unreadable record" in dead[0]
    before = list(rig.logged)
    for _ in range(3):
        rig.spool.drain()
    assert rig.logged == before                         # silent from then on
    assert rig.spool.counts() == (0, 1)


def test_a_failed_spool_write_leaves_no_tmp_behind(rig, monkeypatch):
    """Review of do-003: a write that fails (disk full) used to leave .<pid>-<ns>.tmp."""
    real = os.fdopen

    class Full:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def write(self, _):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(spool.os, "fdopen", lambda fd, mode: Full(real(fd, mode)))
    with pytest.raises(OSError):
        rig.spool.keep(seatmail.Sent(False, "x", kind="mta", args=["send"]), "b")
    assert list(rig.dir.iterdir()) == []


def test_a_spool_that_cannot_be_written_says_the_mail_is_lost(rig, script, monkeypatch):
    script.plan(refusal("mta"))

    def boom(sent, body):
        raise OSError("disk full")
    monkeypatch.setattr(rig.spool, "keep", boom)
    assert bot.mail_in("Team", 12, "Bob", "hi") is False
    assert rig.logged[-1].startswith("BRIDGE MAIL FAILED rc=3 kind=mta: ")
    assert rig.logged[-1].endswith("NOT KEPT, the mail is lost: disk full")


def test_counts(rig):
    assert rig.spool.counts() == (0, 0)
    for kind in ("mta", "identity", "subject"):
        rig.spool.keep(seatmail.Sent(False, "x", kind=kind, args=["send"]), "b")
    assert rig.spool.counts() == (2, 1)


def test_the_default_dir_is_under_dc_root(monkeypatch, tmp_path):
    monkeypatch.setattr(spool, "DIR", None)
    monkeypatch.setattr(dc_env, "ROOT", tmp_path)
    monkeypatch.delenv("DC_UNDELIVERED", raising=False)
    assert spool.directory() == tmp_path / "undelivered"
    monkeypatch.setenv("DC_UNDELIVERED", str(tmp_path / "elsewhere"))
    assert spool.directory() == tmp_path / "elsewhere"


def test_a_call_mail_is_not_spooled(rig, monkeypatch):
    """M5 stays on sendmail (part 2b): its failure is the call path's to report."""
    monkeypatch.setattr(bot.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=75, stdout="", stderr="busy"))
    assert bot.mail_in("Team", 12, "call", "RINGING", msgid="ring-1@host") is False
    assert rig.spool.counts() == (0, 0)


# ---- the bot drains each pass, and says what it holds at start ---------------

class Done(Exception):
    pass


class BotTime:
    """bot.py's `time`: the time module, but sleep raises Done. Only bot.py's
    own calls see it; the start probe's child wait and every thread keep the
    real one (patched process-wide, the pass could end inside the probe, PHONE-171)."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, s):
        raise Done


def test_the_outbox_loop_drains_the_spool_each_pass(rig, monkeypatch, tmp_path):
    rig.spool.keep(seatmail.Sent(False, "x", kind="mta", args=["send", "a@b"]), "late mail")
    rig.spool.keep(seatmail.Sent(False, "x", kind="subject", args=["send", "a@b"]), "bad mail")
    rig.clock.now += spool.BACKOFF_S
    sent = []
    rig.spool._send = lambda a, b: sent.append(b) or seatmail.Sent(True, sent="yes", args=a)
    _one_outbox_pass(monkeypatch, tmp_path)
    assert sent == ["late mail"] and rig.waiting() == [] and len(rig.dead()) == 1
    assert f"undelivered mail: 1 waiting, 1 dead in {rig.dir}" in rig.logged


def _one_outbox_pass(monkeypatch, tmp_path, account=None):
    """Run bot.main() with fakes through ONE pass of the outbox loop (its sleep stops it)."""
    class Rpc:
        def __init__(self, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    account = account or types.SimpleNamespace(is_configured=lambda: True, start_io=lambda: None,
                                               get_config=lambda k: "bot@relay.example")
    stop = threading.Event()
    lst = tmp_path / "channels.txt"
    lst.write_text("owner  chat:12\n")
    lst.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(lst))
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(bot, "Rpc", Rpc)
    monkeypatch.setattr(bot, "DeltaChat", lambda rpc: types.SimpleNamespace(get_all_accounts=lambda: [account]))
    monkeypatch.setattr(bot, "server_choice", lambda: (None, "package", "test"))
    monkeypatch.setattr(bot, "sweep", lambda a: None)
    monkeypatch.setattr(bot, "handle_events", lambda a: stop.wait())
    monkeypatch.setattr(bot, "alive_or_exit", lambda t, dog: None)
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "INVITE", None)

    monkeypatch.setattr(bot, "time", BotTime())
    try:
        with pytest.raises(Done):
            bot.main()
    finally:
        stop.set()


# ---- the bot's pid file: written once it holds the account, never a stop -----

def test_the_bot_writes_its_pid_file_once_it_holds_the_account(rig, monkeypatch, tmp_path, pid_file_in_tmp):
    """bot.pid names this process, and its clean exit is booked to take it back."""
    registered = []
    monkeypatch.setattr(bot.atexit, "register", lambda *a: registered.append(a))
    _one_outbox_pass(monkeypatch, tmp_path)
    assert pid_file_in_tmp.read_text().split("\n")[0] == str(os.getpid())
    assert registered == [(bot.pidfile.clear, pid_file_in_tmp, os.getpid())]
    assert not any("bot.pid" in line for line in rig.logged)


def test_a_pid_file_that_cannot_be_written_is_one_line_and_the_bot_runs_on(rig, monkeypatch, tmp_path):
    """The write fails (its directory is gone): ONE line in the log, nothing
    booked for the exit, and the bot goes on to its outbox loop."""
    gone = tmp_path / "gone" / "bot.pid"
    monkeypatch.setattr(bot, "PID_FILE", gone)
    registered = []
    monkeypatch.setattr(bot.atexit, "register", lambda *a: registered.append(a))
    sent = []
    rig.spool._send = lambda a, b: sent.append(b) or seatmail.Sent(True, sent="yes", args=a)
    rig.spool.keep(seatmail.Sent(False, "x", kind="mta", args=["send", "a@b"]), "late mail")
    rig.clock.now += spool.BACKOFF_S
    _one_outbox_pass(monkeypatch, tmp_path)
    said = [line for line in rig.logged if "bot.pid" in line]
    assert len(said) == 1 and said[0].startswith(f"bot.pid NOT WRITTEN at {gone}: "), said
    assert registered == [] and not gone.exists()
    assert sent == ["late mail"]        # the outbox loop ran: the bot carried on


# ---- do-008 (review F1): a pass stops at the first timeout or mta refusal ------

def _three_due(rig):
    """Mails A, B and C in the spool, all due now. -> {body: (name, bytes)} as they were."""
    for body in ("A", "B", "C"):
        rig.spool.keep(seatmail.Sent(False, "rc=3 kind=mta: x", kind="mta", args=["send", body]), body)
        rig.clock.now += 1
    rig.clock.now += spool.BACKOFF_S
    return {rig.rec(n)["body"]: (n, (rig.dir / n).read_bytes()) for n in rig.waiting()}


def _held(rig):
    return [m for m in rig.logged if m.startswith("BRIDGE MAIL HELD")]


@pytest.mark.parametrize("kind, sent", [("timeout", "unknown"), ("mta", "no"), ("mta", "unknown")])
def test_an_mta_that_is_down_costs_one_attempt_per_pass(rig, kind, sent):
    before = _three_due(rig)
    tried = []

    def send(args, body):
        tried.append(body)
        return seatmail.Sent(False, f"rc=- kind={kind} sent={sent}: no answer", kind=kind, sent=sent, args=args)
    rig.spool._send = send
    rig.spool.drain()
    assert tried == ["A"]                                       # ONE attempt, the oldest
    assert rig.rec(before["A"][0])["attempts"] == 2             # A is rescheduled as ever
    for body in ("B", "C"):                                     # untouched, byte for byte
        name, raw = before[body]
        assert (rig.dir / name).read_bytes() == raw
        assert rig.rec(name)["attempts"] == 1 and rig.rec(name)["next_try"] <= rig.clock.now  # still due
    [held] = _held(rig)                                         # logged once for the pass
    assert held.startswith(f"BRIDGE MAIL HELD 2 more not tried this pass: {before['A'][0]} got kind {kind}")

    rig.spool.drain()                                           # the next pass: A backs off, B is tried
    assert tried == ["A", "B"] and len(_held(rig)) == 2
    assert _held(rig)[1].startswith("BRIDGE MAIL HELD 1 more")
    rig.spool._send = lambda a, b: tried.append(b) or seatmail.Sent(True, sent="yes", args=a)
    rig.spool.drain()                                           # the MTA is back: C goes, A waits its backoff
    assert tried == ["A", "B", "C"] and len(_held(rig)) == 2
    assert sorted(rig.rec(n)["body"] for n in rig.waiting()) == ["A", "B"]


@pytest.mark.parametrize("kind", sorted(spool.RETRIED - spool.STOP_KINDS) + ["subject"])
def test_a_refusal_that_is_not_the_mta_never_stops_the_pass(rig, kind):
    _three_due(rig)
    tried = []

    def send(args, body):
        tried.append(body)
        if body == "A":
            return seatmail.Sent(False, f"rc=2 kind={kind}: x", kind=kind, args=args)
        return seatmail.Sent(True, sent="yes", args=args)
    rig.spool._send = send
    rig.spool.drain()
    assert tried == ["A", "B", "C"] and _held(rig) == []


def test_the_last_due_mail_timing_out_holds_nothing_and_says_nothing(rig):
    rig.spool.keep(seatmail.Sent(False, "x", kind="mta", args=["send"]), "only")
    rig.clock.now += spool.BACKOFF_S
    rig.spool._send = lambda a, b: seatmail.Sent(False, "t", kind="timeout", sent="unknown", args=a)
    rig.spool.drain()
    assert _held(rig) == [] and rig.rec(rig.waiting()[0])["attempts"] == 2


def test_a_dc_say_behind_three_due_mails_waits_one_timeout_at_most(rig, monkeypatch, tmp_path):
    # the outbox loop drains the spool BEFORE its jobs; a hung MTA costs each send
    # seatmail.TIMEOUT_S. Elapsed time is a fake: each send adds TIMEOUT_S to it.
    _three_due(rig)
    elapsed, events = [0.0], []

    def hung(args, body):
        elapsed[0] += seatmail.TIMEOUT_S
        events.append(f"send {body}")
        return seatmail.Sent(False, "rc=- kind=timeout sent=unknown: hung", kind="timeout",
                             sent="unknown", args=args)
    rig.spool._send = hung

    def send_text(text):
        events.append(f"say {text} after {elapsed[0]:.0f}s")
        return types.SimpleNamespace(id=77)
    chat = types.SimpleNamespace(send_text=send_text)
    account = types.SimpleNamespace(is_configured=lambda: True, start_io=lambda: None,
                                    get_config=lambda k: "bot@relay.example",
                                    get_chat_by_id=lambda cid: chat)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "1-say.json").write_text(json.dumps({"chat_id": 12, "text": "the reply"}))
    _one_outbox_pass(monkeypatch, tmp_path, account)
    assert events == ["send A", f"say the reply after {seatmail.TIMEOUT_S}s"]
    assert len(rig.waiting()) == 3 and len(_held(rig)) == 1


# ---- the criterion: dc-live and bridge-check show the counts (scratch HOME) ---

def _scratch_root(tmp_path, waiting, dead):
    root = tmp_path / "root"
    (root / "undelivered" / "dead").mkdir(parents=True)
    for n in range(waiting):
        (root / "undelivered" / f"1-{n}.json").write_text("{}")
    for n in range(dead):
        (root / "undelivered" / "dead" / f"1-{n}.json").write_text("{}")
    (root / "undelivered" / ".x.tmp").write_text("")     # never counted
    return root


def _scratch_env(tmp_path, root):
    """No bot.pid in a scratch root, so dc-live says the bot is not running (2).
    It finds the bot by that file alone (tests/test_dc_live_pidfile.py), so a
    concurrent run's processes can never make it see one here."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_", "SESSIONPHONE_"))}
    env.update(HOME=str(tmp_path / "home"), DC_ROOT=str(root), DC_AGENT="nobody-dc-live-test")
    return env


def test_dc_live_says_how_many_wait_and_how_many_are_dead(tmp_path):
    root = _scratch_root(tmp_path, 2, 1)
    r = subprocess.run([str(REPO / "bin" / "dc-live")], env=_scratch_env(tmp_path, root),
                       capture_output=True, text=True, check=False)
    assert r.returncode == 2                            # no bot: unchanged
    assert f"dc-live: undelivered mail to the agent: 2 waiting, 1 dead ({root / 'undelivered'})" in r.stderr
    r = subprocess.run([str(REPO / "bin" / "dc-live"), "--id"], env=_scratch_env(tmp_path, root),
                       capture_output=True, text=True, check=False)
    assert "undelivered" not in r.stderr + r.stdout     # --id is for a program: never


def test_dc_live_is_quiet_when_nothing_is_held(tmp_path):
    root = _scratch_root(tmp_path, 0, 0)
    r = subprocess.run([str(REPO / "bin" / "dc-live")], env=_scratch_env(tmp_path, root),
                       capture_output=True, text=True, check=False)
    assert "undelivered" not in r.stderr


@pytest.mark.parametrize("made", ["no spool", "no dead dir"])
def test_dc_live_survives_a_spool_that_is_not_there_yet(tmp_path, made):
    """The usual install has no undelivered/ (and after a first failure, no dead/):
    find exits 1 there, and under set -e + pipefail dc-live died silently with 1,
    its "no call is live" answer. It must go on to say the bot is not running (2)."""
    root = tmp_path / "root"
    root.mkdir()
    if made == "no dead dir":
        (root / "undelivered").mkdir()
        (root / "undelivered" / "1-1.json").write_text("{}")
    r = subprocess.run([str(REPO / "bin" / "dc-live")], env=_scratch_env(tmp_path, root),
                       capture_output=True, text=True, check=False)
    assert r.returncode == 2, r.stderr
    assert "the bot is NOT RUNNING" in r.stderr
    if made == "no dead dir":
        assert "undelivered mail to the agent: 1 waiting, 0 dead" in r.stderr
    else:
        assert "undelivered" not in r.stderr


def test_bridge_check_reports_the_counts_and_never_fails_on_them(tmp_path):
    """Run on a scratch copy: its bin/, the real src and venv, and no bridge tests."""
    if not (REPO / ".venv" / "bin" / "python").exists():
        pytest.skip("no .venv: run `uv sync`")
    copy = tmp_path / "repo"
    (copy / "bin").mkdir(parents=True)
    for tool in ("bridge-check", "dc-env"):
        shutil.copy2(REPO / "bin" / tool, copy / "bin" / tool)
    (copy / "src").symlink_to(REPO / "src")
    (copy / ".venv").symlink_to(REPO / ".venv")
    (copy / "tests" / "bridge").mkdir(parents=True)
    root = _scratch_root(tmp_path, 3, 2)
    env = _scratch_env(tmp_path, root)
    env["TMPDIR"] = "/tmp"
    r = subprocess.run([str(copy / "bin" / "bridge-check")], env=env, capture_output=True, text=True,
                       check=False, timeout=120)
    assert f"3 waiting, 2 dead ({root / 'undelivered'})" in r.stdout, r.stdout + r.stderr
    assert r.returncode == 0 and "ALL GREEN" in r.stdout, r.stdout + r.stderr

"""PHONE-249 fix-001: a kept call mail (M5) is retried, and never sent twice.

sessionmail has no duplicate check at delivery: a send under a --message-id
already in the box lands twice. So before the spool retries a record whose args
carry `--message-id`, it asks seatmail.delivered() whether the target box holds
that id, read-only (`message path <box>`, then the header block of each file
in new/ and cur/, never tmp/):

- there: the record is removed, nothing is sent (BRIDGE MAIL ALREADY THERE);
- not there: it is retried as any other;
- the box cannot be checked: retried only if every try said sent=no; one that
  may have gone (sent=unknown) is buried in dead/, counted, never resent.

Fakes only: a real maildir under tmp_path and a fake `message` that answers
`path NAME`; no real delivery (AGENTS.md). Dates: Sun 2030-06-09.
"""
import json
import os
import pathlib
import types

import pytest
from click.testing import CliRunner

from sessionphone import cli as cli_mod
from sessionphone import service
from sessionphone.bridge import bot, dc_env, seatmail, spool

SUNDAY = 1907229600.0                    # Sun 2030-06-09 10:00 UTC
TURN_ID = "call-turn-12-1@host"
ROOT_ID = "call-12-1@host"
BOX = "ada@example.net"


# ---- a maildir and a `message` that resolves boxes to it ---------------------

@pytest.fixture
def maildirs(tmp_path, monkeypatch):
    """Boxes under tmp_path/mail/<name>/{new,cur,tmp}, and a fake `message` whose
    `path NAME` prints the box's maildir (exit 0) when it exists, else exit 2,
    as sessionmail's does. Every call is recorded: nothing but `path` may run."""
    root = tmp_path / "mail"
    calls = tmp_path / "message-calls"
    exe = tmp_path / "message-bin" / "message"
    exe.parent.mkdir()
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        f"root, calls = pathlib.Path({str(root)!r}), {str(calls)!r}\n"
        "open(calls, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] != ['path'] or len(sys.argv) != 3:\n"
        "    sys.stderr.write('not path\\n'); sys.exit(9)\n"
        "box = root / sys.argv[2]\n"
        "if not box.is_dir():\n"
        "    sys.stderr.write(f'no box {sys.argv[2]!r}\\n'); sys.exit(2)\n"
        "print(box)\n")
    exe.chmod(0o755)
    monkeypatch.setattr(seatmail, "MESSAGE", str(exe))
    monkeypatch.setenv("SESSIONMAIL_HOST", "example.net")      # BOX is on this host
    ns = types.SimpleNamespace(root=root)

    def box(name):
        for sub in ("new", "cur", "tmp"):
            (root / name / sub).mkdir(parents=True, exist_ok=True)
        return root / name

    def deliver(name, msgid, sub="new", when=SUNDAY, header="Message-ID", fold=False):
        d = box(name) / sub
        n = len(list(d.iterdir()))
        f = d / f"{int(when)}.V1I{n}M1.testhost"
        value = f"\n <{msgid}>" if fold else f" <{msgid}>"
        f.write_text(f"From: ada-chat@example.net\nTo: {name}@example.net\n{header}:{value}\n"
                     f"Subject: x\n\nMessage-ID: <not-this-one@host>\n")
        os.utime(f, (when, when))
        return f
    ns.box, ns.deliver = box, deliver
    ns.calls = lambda: calls.read_text().splitlines() if calls.exists() else []
    return ns


# ---- seatmail.delivered ------------------------------------------------------

@pytest.mark.parametrize("sub", ["new", "cur"])
def test_an_id_in_new_or_cur_is_delivered(maildirs, sub):
    maildirs.deliver("ada", TURN_ID, sub=sub)
    assert seatmail.delivered(TURN_ID, BOX) is True


def test_a_file_in_tmp_is_not_delivered_yet(maildirs):
    maildirs.deliver("ada", TURN_ID, sub="tmp")
    assert seatmail.delivered(TURN_ID, BOX) is False


def test_another_id_is_not_this_one(maildirs):
    maildirs.deliver("ada", "call-turn-12-2@host")
    assert seatmail.delivered(TURN_ID, BOX) is False


def test_an_id_only_in_the_body_is_not_delivered(maildirs):
    """Only the header block is read: the body's `Message-ID: <not-this-one@host>`
    never counts."""
    maildirs.deliver("ada", "call-turn-12-2@host")
    assert seatmail.delivered("not-this-one@host", BOX) is False


@pytest.mark.parametrize("asked", [TURN_ID, f"<{TURN_ID}>"])
def test_the_bare_id_and_the_bracketed_id_both_match(maildirs, asked):
    maildirs.deliver("ada", TURN_ID)
    assert seatmail.delivered(asked, BOX) is True


def test_the_header_name_is_any_case_and_may_be_folded(maildirs):
    maildirs.deliver("ada", TURN_ID, header="Message-Id", fold=True)
    assert seatmail.delivered(TURN_ID, BOX) is True


def test_a_file_older_than_since_is_skipped(maildirs):
    maildirs.deliver("ada", TURN_ID, when=SUNDAY - 3600)
    assert seatmail.delivered(TURN_ID, BOX, since=SUNDAY - 600) is False
    assert seatmail.delivered(TURN_ID, BOX, since=SUNDAY - 7200) is True


def test_an_empty_box_is_not_delivered(maildirs):
    maildirs.box("ada")
    assert seatmail.delivered(TURN_ID, BOX) is False


def test_a_box_that_cannot_be_resolved_is_none(maildirs):
    """An off-host box on an MTA: `message path` exits 2. Not known, not False."""
    assert seatmail.delivered(TURN_ID, "nobody@elsewhere.example") is None


def test_a_box_on_another_host_is_none_even_with_a_local_namesake(maildirs):
    """ada@elsewhere.example went over an MTA: the local box `ada` is not where it
    went, so reading it could say "not there" of a mail that went. Not known, and
    `message path` is never asked."""
    maildirs.box("ada")
    assert seatmail.delivered(TURN_ID, "ada@elsewhere.example") is None
    assert maildirs.calls() == []


def test_a_bare_box_name_is_checked_here(maildirs):
    """DC_MAILTO may be a bare box name: no host, so this host's box."""
    maildirs.deliver("ada", TURN_ID)
    assert seatmail.delivered(TURN_ID, "ada") is True


def test_a_plus_address_is_looked_up_in_its_base_box(maildirs):
    """The drop delivers base+tag to the box base (seatmail.cc_boxes)."""
    maildirs.deliver("ada", TURN_ID)
    assert seatmail.delivered(TURN_ID, "ada+phone@example.net") is True
    assert maildirs.calls() == ["path ada+phone", "path ada"]


def test_it_is_read_only_and_asks_by_local_part(maildirs):
    """A full address is not a box name: `message path` is asked for the local
    part, and `path` is the only verb it runs."""
    f = maildirs.deliver("ada", TURN_ID)
    before = sorted(p.name for p in maildirs.root.rglob("*")), f.stat().st_mtime
    assert seatmail.delivered(TURN_ID, BOX) is True
    assert maildirs.calls() == ["path ada"]
    assert (sorted(p.name for p in maildirs.root.rglob("*")), f.stat().st_mtime) == before


def test_no_message_binary_is_none(monkeypatch):
    monkeypatch.setattr(seatmail, "MESSAGE", None)
    monkeypatch.setattr(seatmail, "resolve_message", lambda e=None: None)
    assert seatmail.delivered(TURN_ID, BOX) is None


# ---- the spool's retry asks first --------------------------------------------

class Clock:
    def __init__(self, now=SUNDAY):
        self.now = now

    def __call__(self):
        return self.now


def _kept(root, clock, *, msgid=TURN_ID, sent="no", kind="mta"):
    """A refused M5 kept as bot.mail_in keeps it (seatmail.args)."""
    sp = spool.Spool(lambda m: None, root=root, clock=clock)
    a = seatmail.args(BOX, "[Team 12] Caller: turn", msgid=msgid, in_reply_to=ROOT_ID)
    return sp.keep(seatmail.Sent(False, f"rc=3 kind={kind}", kind=kind, sent=sent, args=a), "the turn")


@pytest.fixture
def retry(tmp_path):
    """A spool on a fake clock whose send and duplicate check are recorded fakes."""
    r = types.SimpleNamespace(clock=Clock(), logged=[], sends=[], asked=[], there=False,
                              root=tmp_path / "undelivered")

    def send(args, body):
        r.sends.append(args)
        return seatmail.Sent(True, id=f"<{args[args.index('--message-id') + 1]}>"
                             if "--message-id" in args else "<1.2.3@host>", sent="yes", args=args)

    def delivered(msgid, to, since=None):
        r.asked.append((msgid, to, since))
        return r.there
    r.spool = spool.Spool(r.logged.append, root=r.root, clock=r.clock, send=send, delivered=delivered)
    r.waiting = lambda: sorted(p.name for p in r.root.glob("*.json"))
    r.dead = lambda: sorted(p.name for p in (r.root / "dead").glob("*.json"))
    return r


def test_a_kept_turn_already_in_the_box_is_removed_and_not_sent(retry):
    _kept(retry.root, retry.clock, sent="unknown")
    retry.there = True
    retry.clock.now += spool.BACKOFF_S
    retry.spool.drain()
    assert retry.sends == [] and retry.waiting() == [] and retry.dead() == []
    assert retry.asked == [(TURN_ID, BOX, SUNDAY - seatmail.DELIVERED_SLACK_S)]
    (line,) = retry.logged
    assert line.startswith(f"BRIDGE MAIL ALREADY THERE on try 2: <{TURN_ID}> is in {BOX}'s box; "
                           "not sent again: ")
    assert retry.spool.counts() == (0, 0)


def test_a_kept_turn_not_in_the_box_is_retried_under_its_own_id(retry):
    _kept(retry.root, retry.clock, sent="unknown")
    retry.clock.now += spool.BACKOFF_S
    retry.spool.drain()
    (args,) = retry.sends
    assert args[args.index("--message-id") + 1] == TURN_ID
    assert retry.waiting() == [] and retry.logged[0].startswith("BRIDGE MAIL SENT on try 2 ")


def test_a_box_that_cannot_be_checked_after_sent_no_is_retried(retry):
    """Every try said sent=no: nothing has gone, so a retry cannot duplicate."""
    _kept(retry.root, retry.clock, sent="no", kind="identity")
    retry.there = None
    retry.clock.now += spool.BACKOFF_S
    retry.spool.drain()
    assert len(retry.sends) == 1 and retry.waiting() == [] and retry.dead() == []


def test_a_box_that_cannot_be_checked_after_sent_unknown_is_buried(retry):
    """It may have gone, and there is no way to know: dead/, counted, never resent."""
    _kept(retry.root, retry.clock, sent="unknown")
    retry.there = None
    retry.clock.now += spool.BACKOFF_S
    retry.spool.drain()
    assert retry.sends == [] and retry.waiting() == []
    (name,) = retry.dead()
    assert retry.spool.counts() == (0, 1)
    (line,) = retry.logged
    assert line.startswith("BRIDGE MAIL DEAD ") and line.endswith(
        f": <{TURN_ID}> may have gone (sent=unknown) and {BOX}'s box cannot be checked "
        "from here; never sent twice")
    rec = json.loads((retry.root / "dead" / name).read_text())
    assert "never sent twice" in rec["dead_why"]


def test_one_sent_unknown_try_among_sent_no_tries_buries_it(retry):
    """Any try that may have gone is enough: a later sent=no does not clear it."""
    path = _kept(retry.root, retry.clock, sent="unknown")
    rec = json.loads(path.read_text())
    rec["tries"].append({"at": SUNDAY + 60, "kind": "identity", "sent": "no", "error": "x"})
    rec.update(sent="no", attempts=2)
    path.write_text(json.dumps(rec))
    retry.there = None
    retry.clock.now += 3600
    retry.spool.drain()
    assert retry.sends == [] and len(retry.dead()) == 1


def test_a_mail_with_no_message_id_is_never_checked(retry):
    """M1-M4 (and an M5 that threads with -r only) mint a new id at each send:
    there is nothing to look for, so a duplicate beats a loss, as before."""
    sp = spool.Spool(lambda m: None, root=retry.root, clock=retry.clock)
    a = seatmail.args(BOX, "[Team 12] Bob: hi", in_reply_to=ROOT_ID)
    sp.keep(seatmail.Sent(False, "x", kind="mta", sent="unknown", args=a), "hi")
    retry.there = None
    retry.clock.now += spool.BACKOFF_S
    retry.spool.drain()
    assert retry.asked == [] and len(retry.sends) == 1 and retry.waiting() == []


def test_a_turn_not_yet_due_is_not_checked(retry):
    _kept(retry.root, retry.clock)
    retry.spool.drain()
    assert retry.asked == [] and retry.sends == [] and len(retry.waiting()) == 1


def test_the_check_reads_the_real_box(maildirs, tmp_path):
    """End to end on the default check (seatmail.delivered): a try that may have
    gone and did go is found in the box, and the turn is not sent twice."""
    clock, sends, logged = Clock(), [], []
    root = tmp_path / "undelivered"
    _kept(root, clock, sent="unknown")
    maildirs.deliver("ada", TURN_ID, when=SUNDAY + 1)
    sp = spool.Spool(logged.append, root=root, clock=clock, send=lambda a, b: sends.append(a))
    clock.now += spool.BACKOFF_S
    sp.drain()
    assert sends == [] and sp.counts() == (0, 0)
    assert logged[0].startswith("BRIDGE MAIL ALREADY THERE on try 2: ")
    assert maildirs.calls() == ["path ada"]


# ---- bot.mail_in keeps the turn, and the retry goes once ----------------------

@pytest.fixture
def seat(monkeypatch, seat_key):
    seat_key("ada-chat seat")
    monkeypatch.setattr(dc_env, "ADDR", "ada-chat@example.net")
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Ada")
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    monkeypatch.setattr(bot, "PROFILE", "")


def test_a_refused_turn_is_kept_then_sent_once(fake_message, seat, monkeypatch, undelivered_in_tmp):
    """mail_in keeps the refused turn; the next due pass checks the box (not
    there) and sends it under the same id; the spool is then empty."""
    logged, asked = [], []
    monkeypatch.setattr(bot, "log", logged.append)
    clock = Clock()
    monkeypatch.setattr(bot, "SPOOL", spool.Spool(logged.append, clock=clock,
                                                  delivered=lambda m, t, since=None: asked.append(m)
                                                  or False))
    fake_message.answer((3, {"sent": "no", "kind": "undeliverable", "message": "no such box"}))
    assert bot.mail_in("Team", 12, "Caller", "(on the call) hello", msgid=TURN_ID,
                       in_reply_to=ROOT_ID) is False
    assert bot.SPOOL.counts() == (1, 0)
    clock.now += spool.BACKOFF_S
    bot.SPOOL.drain()
    assert asked == [TURN_ID] and bot.SPOOL.counts() == (0, 0)
    first, second = fake_message.calls()
    assert first["argv"] == second["argv"] and first["body"] == second["body"]


# ---- bot status counts the spool the installed job names ---------------------

def _plist(tmp_path, monkeypatch, env):
    import plistlib
    p = tmp_path / "job.plist"
    p.write_bytes(plistlib.dumps({"Label": "test", "EnvironmentVariables": env}))
    monkeypatch.setattr(service, "plist_path", lambda name=None: p)


def test_undelivered_dir_is_the_plists_dc_undelivered(tmp_path, monkeypatch):
    _plist(tmp_path, monkeypatch, {"DC_UNDELIVERED": str(tmp_path / "spool"), "DC_ROOT": "/x"})
    assert service.undelivered_dir() == tmp_path / "spool"


def test_undelivered_dir_is_under_the_bots_home(tmp_path, monkeypatch):
    """bin/dc-env: <DC_ROOT, else SESSIONPHONE_ROOT>[/<DC_PROFILE>]/undelivered."""
    _plist(tmp_path, monkeypatch, {"DC_ROOT": str(tmp_path / "r")})
    assert service.undelivered_dir() == tmp_path / "r" / "undelivered"
    _plist(tmp_path, monkeypatch, {"SESSIONPHONE_ROOT": str(tmp_path / "s"), "DC_PROFILE": "public"})
    assert service.undelivered_dir() == tmp_path / "s" / "public" / "undelivered"


def test_bot_status_counts_waiting_and_dead(monkeypatch, tmp_path):
    d = tmp_path / "spool"
    (d / "dead").mkdir(parents=True)
    for name in ("1-1.json", "1-2.json"):
        (d / name).write_text("{}")
    (d / "dead" / "1-3.json").write_text("{}")
    _plist(tmp_path, monkeypatch, {"DC_UNDELIVERED": str(d)})
    monkeypatch.setattr(cli_mod, "_which_bot", lambda verb: ("ada", None))
    monkeypatch.setattr(cli_mod.service, "status", lambda name=None: {
        "label": "test.sessionphone.ada", "installed": False, "legacy": None,
        "pid": None, "last_exit": None, "plist": str(tmp_path / "none.plist")})
    monkeypatch.setattr(cli_mod.service, "last_log", lambda name=None: (tmp_path / "bot.log", None, False))
    st = CliRunner().invoke(cli_mod.main, ["bot", "status"])
    assert f"undelivered: 2 waiting, 1 dead ({d})" in st.output.splitlines(), st.output
    assert st.exit_code == 1                       # not running: the count never changes the exit


def test_bot_status_says_zero_too(monkeypatch, tmp_path):
    _plist(tmp_path, monkeypatch, {"DC_UNDELIVERED": str(tmp_path / "never-made")})
    monkeypatch.setattr(cli_mod, "_which_bot", lambda verb: ("ada", None))
    monkeypatch.setattr(cli_mod.service, "status", lambda name=None: {
        "label": "l", "installed": False, "legacy": None, "pid": 7, "last_exit": None,
        "plist": str(tmp_path / "none.plist")})
    monkeypatch.setattr(cli_mod.service, "last_log", lambda name=None: (tmp_path / "bot.log", None, False))
    st = CliRunner().invoke(cli_mod.main, ["bot", "status"])
    assert f"undelivered: 0 waiting, 0 dead ({pathlib.Path(tmp_path / 'never-made')})" in st.output
    assert st.exit_code == 0

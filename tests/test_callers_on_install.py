"""PHONE-98 gap (6): a bot nobody may ring must never say nothing.

The bot answers a call only from the callers file (DC_CALLERS_FILE, else
<root>/callers.txt), and an empty list means nobody. DC_CALLERS grants
nothing (PHONE-132 design-001 (f)): install refuses while it is set, and the
bot logs that it is IGNORED.
`sessionphone link` writes the owner there first, but `bot install` never
checked: a bot could be installed that nobody can ring, and it said nothing
(a cutover on a user's Mac, Sun 2030-06-09). Now `bot install --owner ADDR`
writes the owner into the file, and with no approved caller at all install
REFUSES, exit 2, naming the file, before it touches launchd. A running bot
with no approved callers logs it loudly at start. launchd here is
tests/conftest.py's FakeLaunchd; no real launchctl, no real root.

PHONE-132 ruling C: nothing seeds the owner from the kit operator setting
(a mail address, never the owner's Delta Chat address). The no-owner refusal
names the two ways forward: link the phone, or --owner with the owner's
Delta Chat address. Every test here runs with HOME pointed at a tmp dir.
"""
import os
import stat
import types

import pytest
from click.testing import CliRunner

from sessionphone import account, keygate, service
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

OWNER = "bea@phone.example.org"


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    given_seat("claude-chat")     # install's signer, given: no seat is made
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))     # a scratch HOME, never the real one
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    return launchd


def _listed(monkeypatch, tmp_path):
    """A channels list, for a test with no callers owner line to seed one from."""
    chans = tmp_path / "channels.txt"
    chans.write_text(f"owner  {OWNER}\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))


# --- install refuses, naming the file, before it touches launchd ------------

def test_install_with_no_callers_refuses_naming_the_file(rig, tmp_path):
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2, r.output
    assert str(tmp_path / "root" / "callers.txt") in r.output
    assert "--owner" in r.output and "sessionphone link" in r.output   # how to fill it
    assert rig.calls == []                              # no bootout, no bootstrap
    assert not (tmp_path / "job.plist").exists()


def test_the_no_owner_refusal_names_the_two_ways_forward(rig, tmp_path):
    """Ruling C: link the phone first, or --owner with a DELTA CHAT address."""
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2, r.output
    text = " ".join(r.output.split())
    ways = text.split("Name the owner, one of two ways: ", 1)[1]
    assert ways.startswith("run `sessionphone link` and scan the invite from the owner's phone")
    assert "the first to join becomes the owner" in ways
    assert "`sessionphone bot install --owner ADDR`" in ways
    assert "ADDR is the owner's Delta Chat address" in ways and "not their mail address" in ways
    assert "DC_CALLERS" not in text                     # an env var is no way forward (f)
    assert "operator" not in text                       # no kit operator setting is named
    assert not (tmp_path / "root" / "callers.txt").exists()
    assert rig.calls == []


def test_past_a_dc_callers_file_the_refusal_offers_link_with_that_file(rig, monkeypatch, tmp_path):
    """`link` writes the bot's file for the environment it runs in, so past
    DC_CALLERS_FILE the first way is still `link`, with that setting in front;
    there are still two ways, and neither is a hand edit."""
    elsewhere = tmp_path / "elsewhere" / "callers.txt"
    monkeypatch.setenv("DC_CALLERS_FILE", str(elsewhere))
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2, r.output
    ways = " ".join(r.output.split()).split("Name the owner, one of two ways: ", 1)[1]
    assert ways.startswith(f"run `DC_CALLERS_FILE={elsewhere} sessionphone link` and scan the invite")
    assert "one per line" not in ways
    assert "--owner ADDR" in ways and "Delta Chat address" in ways
    assert rig.calls == [] and not elsewhere.exists()


def test_install_never_runs_session_for_an_owner(rig, monkeypatch, tmp_path):
    """Ruling C guard: no `session operator` read. A `session` first on PATH
    that would hand out an operator is never run, with or without --owner."""
    stub_dir, marker = tmp_path / "stub-bin", tmp_path / "session-ran"
    stub_dir.mkdir()
    stub = stub_dir / "session"
    stub.write_text(f'#!/bin/sh\ntouch "{marker}"\n'
                    'echo \'{"operator": "op@host.example", "mailbox": "op", "privileged": ["op"]}\'\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2, r.output
    assert "op@host.example" not in r.output
    assert not marker.exists()
    assert not (tmp_path / "root" / "callers.txt").exists()
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 0, r.output
    assert not marker.exists()
    assert account.read_callers(tmp_path / "root" / "callers.txt") == [OWNER]


def test_an_empty_callers_file_is_refused_too(rig, tmp_path):
    (tmp_path / "root" / "callers.txt").write_text("# nobody yet\n\n")
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2
    assert "callers.txt" in r.output and "no address" in r.output
    assert rig.calls == []


def test_install_respects_dc_callers_file(rig, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "who.txt"
    monkeypatch.setenv("DC_CALLERS_FILE", str(elsewhere))
    (tmp_path / "root" / "callers.txt").write_text(OWNER + "\n")   # not the file the bot reads
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2
    assert str(elsewhere) in r.output
    assert rig.calls == []


# --- install --owner writes the owner, then installs ------------------------

def test_install_owner_creates_the_callers_file_with_the_owner(rig, tmp_path):
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", "Bea@Phone.example.org"])
    assert r.exit_code == 0, r.output
    f = tmp_path / "root" / "callers.txt"
    assert account.read_callers(f) == [OWNER]
    assert "owner" in f.read_text()                     # kept as a comment on the line
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert OWNER in r.output and str(f) in r.output
    assert "bootstrap" in rig.verbs()


def test_install_owner_writes_into_dc_callers_file(rig, monkeypatch, tmp_path, given_seat):
    given_seat("ada-chat")     # this test installs as ada
    elsewhere = tmp_path / "elsewhere" / "who.txt"
    monkeypatch.setenv("DC_CALLERS_FILE", str(elsewhere))
    service.install("ada", owner=OWNER)
    assert account.read_callers(elsewhere) == [OWNER]
    assert not (tmp_path / "root" / "callers.txt").exists()


def test_install_owner_is_idempotent(rig, tmp_path, given_seat):
    given_seat("ada-chat")     # this test installs as ada
    service.install("ada", owner=OWNER)
    service.install("ada", owner=OWNER)
    assert account.read_callers(tmp_path / "root" / "callers.txt") == [OWNER]


def test_install_refuses_an_owner_that_is_not_an_address(rig, tmp_path):
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", "bea"])
    assert r.exit_code != 0
    assert "bea" in r.output
    assert rig.calls == []
    assert not (tmp_path / "root" / "callers.txt").exists()


def test_a_junk_host_is_refused_before_the_owner_is_written(rig, monkeypatch, tmp_path):
    # the default host is sessionmail's (dc_env.mail_host): one naming no machine is junk
    monkeypatch.setenv("SESSIONMAIL_HOST", "localhost")
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 2 and "DC_HOST" in r.output
    assert not (tmp_path / "root" / "callers.txt").exists()


# --- what counts as someone who may ring ------------------------------------

def test_an_existing_owner_installs_without_owner(rig, tmp_path, given_seat):
    given_seat("ada-chat")     # this test installs as ada
    (tmp_path / "root" / "callers.txt").write_text(f"{OWNER}  # owner, Bea, joined 2030-06-09\n")
    service.install("ada")
    assert "bootstrap" in rig.verbs()


def test_dc_callers_grants_nothing_and_install_refuses_while_it_is_set(rig, monkeypatch, tmp_path):
    """PHONE-132 design-001 (f): an env var never grants the right to ring, and
    the job would carry a setting that does nothing. Refused before anything is
    written, even with an owner on file and --owner given."""
    callers = tmp_path / "root" / "callers.txt"
    callers.write_text(f"{OWNER}  # owner, Bea, joined 2030-06-09\n")
    before = callers.read_bytes()
    monkeypatch.setenv("DC_CALLERS", "someone@phone.example.org")
    _listed(monkeypatch, tmp_path)
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", "other@phone.example.org"])
    assert r.exit_code == 2, r.output
    text = " ".join(r.output.split())
    assert "DC_CALLERS is set and IGNORED" in text and str(callers) in text
    assert "`sessionphone callers add ADDR`" in text and "unset it" in text
    assert callers.read_bytes() == before               # --owner was not written either
    assert rig.calls == [] and not (tmp_path / "job.plist").exists()


def test_a_blank_dc_callers_is_unset(rig, monkeypatch, tmp_path, given_seat):
    given_seat("ada-chat")     # this test installs as ada
    (tmp_path / "root" / "callers.txt").write_text(f"{OWNER}  # owner, Bea, joined 2030-06-09\n")
    monkeypatch.setenv("DC_CALLERS", " , ")
    service.install("ada")
    assert "bootstrap" in rig.verbs()


def test_callers_ignored_names_the_variable_and_the_file(tmp_path):
    f = tmp_path / "c.txt"
    assert dc_env.callers_ignored({"DC_CALLERS_FILE": str(f)}) is None
    got = dc_env.callers_ignored({"DC_CALLERS": "a@b.example", "DC_CALLERS_FILE": str(f)})
    assert got.startswith("DC_CALLERS is set and IGNORED")
    assert f"with `DC_CALLERS_FILE={f} sessionphone callers add ADDR`" in got
    got = dc_env.callers_ignored({"PIP_CHAT_CALLERS": "a@b.example", "DC_CALLERS_FILE": str(f)})
    assert got.startswith("PIP_CHAT_CALLERS is set and IGNORED")


def test_dc_callers_alone_leaves_nobody_to_ring(tmp_path):
    env = {"DC_CALLERS": OWNER, "SESSIONPHONE_ROOT": str(tmp_path)}
    assert "NOBODY may ring" in dc_env.callers_problem(env)


def test_calls_off_needs_no_callers(rig, monkeypatch, tmp_path, given_seat):
    """A profile with DC_CALLS=off is never rung, by design: nothing to refuse."""
    given_seat("ada-chat")     # this test installs as ada
    monkeypatch.setenv("DC_CALLS", "off")
    _listed(monkeypatch, tmp_path)      # no owner line to seed channels.txt from (PHONE-102)
    service.install("ada")
    assert "bootstrap" in rig.verbs()


def test_callers_file_resolves_as_the_bot_does(tmp_path):
    assert dc_env.callers_file({"DC_CALLERS_FILE": str(tmp_path / "a")}) == tmp_path / "a"
    assert dc_env.callers_file({"DC_ROOT": str(tmp_path)}) == tmp_path / "callers.txt"
    assert dc_env.callers_file({"SESSIONPHONE_ROOT": str(tmp_path)}) == tmp_path / "callers.txt"


# --- the running bot says so, loudly, at start ------------------------------

@pytest.fixture
def bot(monkeypatch, tmp_path):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))     # a scratch HOME, never the real one
    monkeypatch.setenv("DC_AGENT", "ada")
    from sessionphone.bridge import bot as b
    monkeypatch.setattr(b, "LOG", tmp_path / "bot.log")
    monkeypatch.setattr(b, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(b, "check_root", lambda: None)
    # every DC_* is stripped above, the tripwire's fake seat too (PHONE-106)
    monkeypatch.setattr(b.seatmail, "check", lambda environ=None: None)
    monkeypatch.setattr(b.seatmail, "setup", lambda environ=None: None)
    # the start's box check runs `message path`: the box is there (seat.have_box)
    monkeypatch.setattr(b.seat, "have_box", lambda env, shell: b.seat.Box("ada", "ada@h", "/box"))
    monkeypatch.setattr(b, "relink", lambda: None)
    monkeypatch.setattr(b, "server_choice", lambda: ("/x", "venv", "0"))
    monkeypatch.setattr(dc_env, "HOST_PROBLEM", None)

    class Reached(Exception):
        pass

    def rpc(**k):
        raise Reached
    monkeypatch.setattr(b, "Rpc", rpc)
    b.Reached = Reached
    return b


def test_a_bot_nobody_may_ring_says_so_at_start(bot, monkeypatch, tmp_path):
    callers = tmp_path / "callers.txt"
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    with pytest.raises(bot.Reached):
        bot.main()
    text = (tmp_path / "bot.log").read_text()
    assert "NOBODY" in text and str(callers) in text


def test_a_bot_with_an_owner_says_nothing_of_the_kind(bot, monkeypatch, tmp_path):
    callers = tmp_path / "callers.txt"
    callers.write_text(OWNER + "\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    with pytest.raises(bot.Reached):
        bot.main()
    assert "NOBODY" not in (tmp_path / "bot.log").read_text()


def test_a_bot_with_dc_callers_set_says_it_is_ignored(bot, monkeypatch, tmp_path):
    callers = tmp_path / "callers.txt"
    callers.write_text(OWNER + "\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    monkeypatch.setenv("DC_CALLERS", "someone@phone.example.org")
    with pytest.raises(bot.Reached):
        bot.main()
    text = (tmp_path / "bot.log").read_text()
    assert "WARNING: DC_CALLERS is set and IGNORED" in text and str(callers) in text
    assert "NOBODY" not in text


# --- callers bound to a key: --owner-key, a keyed file, the start migration ---

K1 = "753E8B231F10DDFB82D742DE9D43B582A752BA4C"     # tests/fixtures/openpgp/test-v4-key-1.b64
K2 = "1A6302103BABBD68294D5F4EC5BB94C86CF1C259"


def test_install_owner_key_binds_the_owner_line(rig, tmp_path):
    grouped = " ".join(K1[i:i + 4] for i in range(0, 40, 4)).lower()     # as the phone shows it
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER, "--owner-key", grouped])
    assert r.exit_code == 0, r.output
    f = tmp_path / "root" / "callers.txt"
    assert keygate.read_caller_entries(f)[0].key == K1 and keygate.is_marked(f)
    assert f"bound to key {K1}" in r.output


def test_install_refuses_a_bad_owner_key_writing_nothing(rig, tmp_path):
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER, "--owner-key", "1234"])
    assert r.exit_code != 0 and "1234" in r.output
    assert rig.calls == [] and not (tmp_path / "root" / "callers.txt").exists()


def test_install_owner_without_a_key_on_a_keyed_file_is_refused_naming_owner_key(rig, tmp_path):
    f = tmp_path / "root" / "callers.txt"
    f.write_text(f"{keygate.MARKER}\nann@example.org  key:{K2}  # Ann\n")
    before = f.read_text()
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 2 and "--owner-key" in r.output
    assert f.read_text() == before and rig.calls == []


def test_install_owner_already_pinned_needs_no_key(rig, tmp_path, given_seat):
    given_seat("ada-chat")
    f = tmp_path / "root" / "callers.txt"
    f.write_text(f"{keygate.MARKER}\n{OWNER}  key:{K1}  # owner\n")
    service.install("ada", owner=OWNER)                  # a re-install: nothing to write
    assert keygate.read_caller_entries(f)[0].key == K1


def test_install_owner_key_never_replaces_another_pinned_key(rig, tmp_path):
    f = tmp_path / "root" / "callers.txt"
    f.write_text(f"{keygate.MARKER}\n{OWNER}  key:{K1}  # owner\n")
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER, "--owner-key", K2])
    assert r.exit_code == 2 and "callers pin" in r.output
    assert keygate.read_caller_entries(f)[0].key == K1


def test_install_owner_without_a_key_on_an_old_file_is_left_for_the_start_to_pin(rig, tmp_path, given_seat):
    given_seat("ada-chat")
    service.install("ada", owner=OWNER)
    f = tmp_path / "root" / "callers.txt"
    assert keygate.read_caller_entries(f)[0].key is None and not keygate.is_marked(f)


def test_a_keyed_file_with_nothing_pinned_is_nobody(tmp_path):
    f = tmp_path / "callers.txt"
    f.write_text(f"{keygate.MARKER}\n{OWNER}  # owner\n")
    why = dc_env.callers_problem({"DC_CALLERS_FILE": str(f)})
    assert "NOBODY" in why and "none pinned" in why and "callers pin" in why
    f.write_text(f"{OWNER}  # owner\n")                  # an old file: the bot's start pins it
    assert dc_env.callers_problem({"DC_CALLERS_FILE": str(f)}) is None


def test_the_bot_start_pins_an_old_owner_line(bot, monkeypatch, tmp_path):
    import _keyfakes as kf
    callers = tmp_path / "callers.txt"
    callers.write_text(f"{OWNER}  # owner, Bea, joined 2030-06-09\n")
    callers.chmod(0o600)
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    _start(bot, monkeypatch, kf.Account([kf.Contact(10, OWNER, kf.K1)]))
    assert keygate.read_caller_entries(callers)[0].key == kf.FPR_K1
    assert "1 pinned, 0 unpinned, 0 ambiguous" in (tmp_path / "bot.log").read_text()


def test_the_bot_start_pins_an_old_channels_owner_line_too(bot, monkeypatch, tmp_path):
    import _keyfakes as kf

    from sessionphone import channels
    chans = tmp_path / "channels.txt"
    chans.write_text(f"owner  {OWNER}  # owner, Bea, joined 2030-06-09\nfamily  chat:42\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    monkeypatch.setenv("DC_CALLERS_FILE", str(tmp_path / "callers.txt"))
    _start(bot, monkeypatch, kf.Account([kf.Contact(10, OWNER, kf.K1)]))
    assert channels.load(chans).entries["owner"].key == kf.FPR_K1
    assert chans.with_name("channels.txt.pre-keys").exists()
    assert f"keys: {chans}: 1 pinned, 0 unpinned, 0 ambiguous" in (tmp_path / "bot.log").read_text()


def _start(bot, monkeypatch, acc):
    """bot.main() on ACC, up to the line after the start hook."""
    acc.is_configured = lambda: True
    acc.start_io = lambda: None

    class Rpc:
        def __init__(self, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(bot, "Rpc", Rpc)
    monkeypatch.setattr(bot, "DeltaChat", lambda rpc: types.SimpleNamespace(get_all_accounts=lambda: [acc]))
    monkeypatch.setattr(bot.pidfile, "write", lambda *a: False)

    def stop(_account):
        raise bot.Reached
    monkeypatch.setattr(bot, "address", stop)           # the line after the start hook
    with pytest.raises(bot.Reached):
        bot.main()

"""PHONE-102 do-005: `sessionphone channels`, and who seeds the list (design rules 5 and 6).

design/allow-list.md rule 5 (CLI): `sessionphone channels [list|add]` mirrors
`sessionphone callers`. Rule 6: `sessionphone link` lists the owner BEFORE its
greeting, and `bot install` seeds `owner` from --owner, else from the callers
file's owner line, and with neither REFUSES (exit 2, naming the file) before it
touches launchd. Fakes only: no Delta account is opened and no real launchctl
runs (tests/conftest.py's FakeLaunchd).
"""
from __future__ import annotations

import os
import stat
import types

import _keyfakes
import pytest
from click.testing import CliRunner

from sessionphone import account, channels, keygate, service
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

OWNER = "bea@phone.example.org"


def _clean_env(monkeypatch):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- sessionphone channels list|add, against a tmp DC_ROOT ------------------

@pytest.fixture
def root(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    r = tmp_path / "root"
    r.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    monkeypatch.setenv("DC_ROOT", str(r))
    return r


def test_channels_add_then_list(root):
    run = CliRunner()
    r = run.invoke(cli, ["channels", "add", "owner", "Bea@Phone.example.org", "--note", "owner, Bea",
                         "--key", _keyfakes.FPR_K1])
    assert r.exit_code == 0, r.output
    f = root / "channels.txt"
    assert f"listed: owner  {OWNER}  key:{_keyfakes.FPR_K1} ({f})" in r.output
    assert _mode(f) == 0o600
    assert run.invoke(cli, ["channels", "add", "family", "chat:42"]).exit_code == 0
    r = run.invoke(cli, ["channels"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].split() == ["owner", OWNER, f"key:{_keyfakes.FPR_K1}", "#", "owner,", "Bea"]
    assert lines[1].split() == ["family", "chat:42"]
    # the one parser: what the CLI wrote is what the bot's gate reads
    assert set(channels.load(f).entries) == {"owner", "family"}


def test_channels_list_is_the_same_as_bare_channels(root):
    CliRunner().invoke(cli, ["channels", "add", "family", "chat:42"])
    bare = CliRunner().invoke(cli, ["channels"])
    listed = CliRunner().invoke(cli, ["channels", "list"])
    assert bare.exit_code == 0 and "family" in bare.output
    assert listed.exit_code == 0, listed.output
    assert listed.output == bare.output


def test_channels_list_refuses_a_loose_list_as_bare_does(root):
    f = root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o664)
    r = CliRunner().invoke(cli, ["channels", "list"])
    assert r.exit_code == 2 and "0664" in r.output and str(f) in r.output


def test_channels_add_to_an_unreadable_list_exits_2_naming_it(root):
    f = root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o000)
    try:
        r = CliRunner().invoke(cli, ["channels", "add", "owner", OWNER, "--key", _keyfakes.FPR_K1])
    finally:
        f.chmod(0o600)
    assert r.exit_code == 2, (r.output, r.exception)
    assert isinstance(r.exception, SystemExit)          # said, not a traceback
    assert f"cannot add to {f}" in r.stderr and "nothing was listed" in r.stderr
    assert f.read_text() == "family  chat:42\n"


def test_channels_add_refuses_a_duplicate_name(root):
    run = CliRunner()
    run.invoke(cli, ["channels", "add", "family", "chat:42"])
    before = (root / "channels.txt").read_text()
    r = run.invoke(cli, ["channels", "add", "family", "chat:43"])
    assert r.exit_code == 1
    assert "'family' is already listed" in r.output and str(root / "channels.txt") in r.output
    assert (root / "channels.txt").read_text() == before        # nothing written


@pytest.mark.parametrize("name, target, why", [
    ("Family", "chat:42", "lowercase"),
    ("42", "chat:42", "all digits"),
    ("family", "42", "address or chat:N"),
])
def test_channels_add_refuses_a_bad_entry(root, name, target, why):
    r = CliRunner().invoke(cli, ["channels", "add", "--", name, target])
    assert r.exit_code == 1 and why in r.output
    assert not (root / "channels.txt").exists()


def test_channels_with_no_list_says_every_send_is_refused(root):
    r = CliRunner().invoke(cli, ["channels"])
    assert r.exit_code == 0
    assert "every send is refused" in r.output and str(root / "channels.txt") in r.output


def test_channels_list_refuses_a_loose_list(root):
    f = root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o664)
    r = CliRunner().invoke(cli, ["channels"])
    assert r.exit_code == 2
    assert "0664" in r.output and "every send is refused" in r.output and str(f) in r.output


def test_channels_list_says_why_a_line_is_ignored(root):
    f = root / "channels.txt"
    f.write_text("family  chat:42\nBad Line Here\n")
    f.chmod(0o600)
    r = CliRunner().invoke(cli, ["channels"])
    assert r.exit_code == 0
    assert "family" in r.stdout and "IGNORED line 2" not in r.stdout
    assert "IGNORED line 2" in r.stderr


def test_channels_add_says_out_loud_when_it_tightens_a_loose_list(root):
    """do-001 review advisory 1: add() makes a 0664 list 0600, which lifts rule
    4's refusal; the owner must be told, and told to look at what it holds."""
    f = root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o664)
    r = CliRunner().invoke(cli, ["channels", "add", "owner", OWNER, "--key", _keyfakes.FPR_K1])
    assert r.exit_code == 0, r.output
    assert f"tightened {f} from 0664 to 0600; check its entries" in r.stderr
    assert _mode(f) == 0o600


def test_channels_add_to_a_tight_list_says_nothing_of_the_kind(root):
    r = CliRunner().invoke(cli, ["channels", "add", "owner", OWNER, "--key", _keyfakes.FPR_K1])
    assert r.exit_code == 0 and "tightened" not in r.output


def test_channels_respects_dc_channels_file(root, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("DC_CHANNELS_FILE", str(elsewhere / "list.txt"))
    assert CliRunner().invoke(cli, ["channels", "add", "family", "chat:42"]).exit_code == 0
    assert (elsewhere / "list.txt").exists() and not (root / "channels.txt").exists()


def test_channels_before_setup_exits_2(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "nothing"))
    r = CliRunner().invoke(cli, ["channels", "add", "family", "chat:42"])
    assert r.exit_code == 2 and "sessionphone setup" in r.output


def test_channels_file_resolves_as_the_bot_does(tmp_path):
    assert dc_env.channels_file({"DC_CHANNELS_FILE": str(tmp_path / "a")}) == tmp_path / "a"
    assert dc_env.channels_file({"DC_ROOT": str(tmp_path)}) == tmp_path / "channels.txt"
    assert dc_env.channels_file({"SESSIONPHONE_ROOT": str(tmp_path)}) == tmp_path / "channels.txt"


# --- callers add hints at the second list (Q4, DECIDED) ---------------------

def test_callers_add_hints_to_list_the_chat_too(root):
    r = CliRunner().invoke(cli, ["callers", "add", "Bea@Phone.example.org", "--key", _keyfakes.FPR_K1])
    assert r.exit_code == 0, r.output
    assert f"sessionphone channels add NAME {OWNER}" in r.output


# --- check() and resolve(): every refusal names the list ---------------------

def test_every_check_refusal_reason_names_the_list(tmp_path):
    """do-004 review advisory 1 (success criterion 2: the message names the list).
    The reason is what calls.py logs for a refused in-call post; the detail is the ack."""
    f = tmp_path / "channels.txt"
    f.write_text(f"owner  {OWNER}\n")
    f.chmod(0o600)

    class Down:
        def get_chat_by_id(self, _):
            raise RuntimeError("rpc down")

    cases = {
        "not a chat id": (channels.check(Down(), "x", f), f),
        "could not read the chat": (channels.check(Down(), 12, f), f),
        "missing list": (channels.check(Down(), 13, tmp_path / "missing.txt"), tmp_path / "missing.txt"),
    }
    for what, (v, path) in cases.items():
        assert not v, what
        assert str(path) in v.reason, (what, v.reason)
        assert str(path) in v.detail, (what, v.detail)


def test_resolve_hints_add_only_for_a_name_add_would_take(tmp_path):
    """do-003 advisory: no hint at a command add() would refuse."""
    f = tmp_path / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o600)
    with pytest.raises(channels.Refused) as good:
        channels.resolve("nobody", f)
    assert "Add it: sessionphone channels add nobody TARGET" in str(good.value)
    for bad in ("Bob", "-x", "a.b"):
        with pytest.raises(channels.Refused) as e:
            channels.resolve(bad, f)
        assert "channels add" not in str(e.value) and "can never be listed" in str(e.value)
        assert str(f) in str(e.value)


# --- link lists the owner BEFORE the greeting -------------------------------

class _Chat:
    def __init__(self, calls, chans):
        self.calls, self.chans = calls, chans

    def send_text(self, text):
        # what the list held at the moment of the send
        held = self.chans.read_text() if self.chans.exists() else ""
        self.calls.append(("send_text", text, held))


class _Contact:
    def __init__(self, calls, chans):
        self.calls, self.chans = calls, chans

    def get_snapshot(self):
        return types.SimpleNamespace(address="Bea@Phone.example.org", display_name="Bea",
                                     is_key_contact=True)

    def make_vcard(self):        # the joiner's key: link binds the owner line to it
        return _keyfakes.vcard("bea@phone.example.org", _keyfakes.K1)

    def create_chat(self):
        self.calls.append(("create_chat",))
        return _Chat(self.calls, self.chans)


class _Account:
    def __init__(self, calls, chans):
        self.calls, self.chans = calls, chans

    def start_io(self):
        self.calls.append(("start_io",))

    def wait_for_event(self):
        return {"kind": "SecurejoinInviterProgress", "progress": 1000, "contact_id": 7}

    def get_contact_by_id(self, _cid):
        return _Contact(self.calls, self.chans)


@pytest.fixture
def linker(root, monkeypatch):
    calls: list = []
    chans = root / "channels.txt"
    monkeypatch.setattr(account, "_open", lambda create=False: iter([_Account(calls, chans)]))
    monkeypatch.setattr(account.time, "sleep", lambda _s: None)
    return types.SimpleNamespace(calls=calls, chans=chans, root=root)


def test_link_lists_the_owner_before_the_greeting(linker):
    info = account.link(timeout=5)
    assert info["owner"] and info["listed"]
    sends = [c for c in linker.calls if c[0] == "send_text"]
    assert len(sends) == 1
    _, text, held = sends[0]
    assert text == account.GREETING
    # the list already held the owner line when the greeting went
    entries = channels.parse(held, linker.chans, lambda _m: None)
    assert entries["owner"].target == OWNER
    assert entries["owner"].note.startswith("owner, Bea, joined ")
    assert _mode(linker.chans) == 0o600
    assert channels.load(linker.chans).entries["owner"].address == OWNER


def test_link_binds_the_owner_line_to_the_joiners_key(linker):
    info = account.link(timeout=5, greet=False)
    assert info["owner"] and info["key"] == _keyfakes.FPR_K1 and info["key_problem"] is None
    text = (linker.root / "callers.txt").read_text()
    assert text.startswith(keygate.MARKER + "\n")
    assert f"bea@phone.example.org  key:{_keyfakes.FPR_K1}  # owner, Bea, joined " in text
    # the channels owner line is bound to the same key, so the greeting's chat passes the gate
    e = channels.load(linker.chans).entries["owner"]
    assert e.address == OWNER and e.key == _keyfakes.FPR_K1


def test_link_writes_no_owner_when_the_joiner_holds_no_key(linker, monkeypatch):
    monkeypatch.setattr(_Contact, "make_vcard", lambda self: _keyfakes.vcard(OWNER, None))
    info = account.link(timeout=5)
    assert not info["owner"] and "no key" in info["key_problem"]
    assert not (linker.root / "callers.txt").exists()
    assert not [c for c in linker.calls if c[0] == "send_text"]       # not greeted, not listed
    r = CliRunner().invoke(cli, ["link", "--timeout", "5"])
    assert r.exit_code == 0 and "NOT approved to call" in r.stderr


def test_link_without_greeting_still_lists_the_owner(linker):
    info = account.link(timeout=5, greet=False)
    assert info["listed"] and not [c for c in linker.calls if c[0] == "send_text"]
    assert "owner" in channels.load(linker.chans).entries


def test_link_does_not_greet_when_owner_is_someone_elses(linker):
    linker.chans.write_text("owner  someone@else.example\n")
    linker.chans.chmod(0o600)
    info = account.link(timeout=5)
    assert info["owner"] and not info["listed"]
    assert "'owner' is already listed" in info["channels_problem"]
    assert not [c for c in linker.calls if c[0] == "send_text"]   # nothing to an unlisted chat
    assert channels.load(linker.chans).entries["owner"].target == "someone@else.example"


def test_link_cli_says_out_loud_when_it_tightens_the_list(linker):
    linker.chans.write_text("family  chat:42\n")
    linker.chans.chmod(0o664)
    r = CliRunner().invoke(cli, ["link", "--timeout", "5"])
    assert r.exit_code == 0, r.output
    assert f"tightened {linker.chans} from 0664 to 0600" in r.stderr
    assert "listed: owner" in r.stdout


def test_link_names_the_name_the_owner_is_really_listed_under(linker):
    """do-005 review advisory 2: already listed as `bea`, link says `bea`, not `owner`."""
    linker.chans.write_text(f"bea  {OWNER}\n")
    linker.chans.chmod(0o600)
    r = CliRunner().invoke(cli, ["link", "--timeout", "5"])
    assert r.exit_code == 0, r.output
    assert "listed: bea," in r.stdout and "listed: owner" not in r.stdout
    assert "WARNING" not in r.stderr                       # a 0600 list says nothing of the kind
    # nothing added: the line it had is bound to the joiner's key, in place
    assert linker.chans.read_text() == f"{channels.MARKER}\nbea  {OWNER}  key:{_keyfakes.FPR_K1}\n"


def test_link_warns_loudly_when_it_leaves_the_list_loose(linker):
    """Already listed, so link writes nothing and the 0664 stays: the bot refuses
    every send from that list (rule 4), and link must say so out loud."""
    linker.chans.write_text(f"bea  {OWNER}\n")
    linker.chans.chmod(0o664)
    info = account.link(timeout=5)
    assert info["listed"] and info["listed_as"] == "bea" and info["loose"] == 0o664
    assert info["tightened"] is None
    (linker.root / "callers.txt").unlink()                 # so the CLI's joiner is the owner again
    r = CliRunner().invoke(cli, ["link", "--timeout", "5"])
    assert r.exit_code == 0, r.output
    assert "listed: bea," in r.stdout
    assert f"WARNING: {linker.chans} is mode 0664" in r.stderr
    assert "REFUSES EVERY SEND" in r.stderr and f"chmod 600 {linker.chans}" in r.stderr
    assert _mode(linker.chans) == 0o664                     # never tightened quietly
    assert linker.chans.read_text() == f"bea  {OWNER}\n"   # so not pinned either


def test_a_second_joiner_is_not_listed(linker):
    (linker.root / "callers.txt").write_text("ada@x.example  # owner, Ada\n")
    info = account.link(timeout=5)
    assert not info["owner"] and not info["listed"]
    assert not linker.chans.exists()
    assert not [c for c in linker.calls if c[0] == "send_text"]


# --- bot install seeds the owner, or refuses before launchd -----------------

@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    _clean_env(monkeypatch)
    given_seat("claude-chat")     # install's signer, given: no seat is made
    r = tmp_path / "root"
    r.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    launchd.root = r
    return launchd


def test_install_owner_seeds_the_channels_list(rig):
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", "Bea@Phone.example.org"])
    assert r.exit_code == 0, r.output
    f = rig.root / "channels.txt"
    e = channels.load(f).entries["owner"]
    assert e.address == OWNER and "bot install --owner" in e.note
    assert _mode(f) == 0o600
    assert f"channels:  owner {OWNER}, listed in {f}" in r.output
    assert "bootstrap" in rig.verbs()


def test_install_with_no_owner_and_no_list_exits_2_with_zero_launchd_calls(rig, monkeypatch):
    (rig.root / "callers.txt").write_text(OWNER + "\n")   # someone may ring, but no owner line to list
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2, r.output
    f = rig.root / "channels.txt"
    assert str(f) in r.output and "NOWHERE" in r.output
    assert "--owner" in r.output and "sessionphone link" in r.output and "channels add" in r.output
    assert rig.calls == []                          # no bootout, no bootstrap, no print
    assert not f.exists()


def test_install_with_an_empty_list_and_no_owner_is_refused_too(rig, monkeypatch):
    (rig.root / "callers.txt").write_text(OWNER + "\n")
    f = rig.root / "channels.txt"
    f.write_text("# nobody yet\n")
    f.chmod(0o600)
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2 and "has no channel" in r.output and str(f) in r.output
    assert rig.calls == []


def test_install_seeds_from_the_callers_owner_line(rig, given_seat):
    given_seat("ada-chat")     # this test installs as ada
    (rig.root / "callers.txt").write_text(f"{OWNER}  # owner, Bea, joined 2030-06-09\n")
    service.install("ada")
    e = channels.load(rig.root / "channels.txt").entries["owner"]
    assert e.address == OWNER and str(rig.root / "callers.txt") in e.note
    assert "bootstrap" in rig.verbs()


def test_a_caller_with_no_owner_note_seeds_nothing(rig):
    (rig.root / "callers.txt").write_text(f"{OWNER}\n")     # approved, but not marked owner
    with pytest.raises(service.NoChannels):
        service.install("ada")
    assert rig.calls == []


def test_install_leaves_an_existing_list_alone(rig):
    f = rig.root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o600)
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 0, r.output
    assert f.read_text() == "family  chat:42\n"
    assert "channels:" not in r.output


def test_install_refuses_a_loose_list_and_does_not_tighten_it(rig):
    """A 0664 list refuses every send (rule 4). install says so, before launchd,
    and leaves it for the owner to look at: it never lifts the refusal quietly."""
    f = rig.root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o664)
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 2 and "0664" in r.output and str(f) in r.output
    assert _mode(f) == 0o664
    assert rig.calls == []


def test_install_seeds_the_list_the_job_will_read(rig, monkeypatch, tmp_path, given_seat):
    given_seat("ada-chat")     # this test installs as ada
    elsewhere = tmp_path / "elsewhere" / "list.txt"
    monkeypatch.setenv("DC_CHANNELS_FILE", str(elsewhere))
    service.install("ada", owner=OWNER)
    assert channels.load(elsewhere).entries["owner"].address == OWNER
    assert not (rig.root / "channels.txt").exists()


def test_install_owner_is_idempotent_on_the_list(rig, given_seat):
    given_seat("ada-chat")     # this test installs as ada
    service.install("ada", owner=OWNER)
    service.install("ada", owner=OWNER)
    assert list(channels.load(rig.root / "channels.txt").entries) == ["owner"]


def test_a_refused_install_writes_nothing(rig):
    """do-005 review advisory 1: a loose list refuses the install, so --owner must
    not have written callers.txt first. Byte-identical, and zero launchd calls."""
    f = rig.root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o664)
    callers = rig.root / "callers.txt"
    callers.write_text("ada@x.example  # a caller, not the owner\n")
    callers.chmod(0o600)
    before = callers.read_bytes()
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 2 and str(f) in r.output, r.output
    assert callers.read_bytes() == before
    assert rig.calls == []


def test_a_refused_install_creates_no_callers_file(rig):
    f = rig.root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o664)
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 2, r.output
    assert not (rig.root / "callers.txt").exists()
    assert rig.calls == []


def test_an_owner_the_list_would_refuse_is_refused_before_callers_is_written(rig):
    """'bea@@x' passes add_owner's '@' test but is no channel address: refused
    with nothing written, rather than half-installed."""
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", "bea@@phone.example.org"])
    assert r.exit_code != 0 and "bea@@phone.example.org" in r.output
    assert not (rig.root / "callers.txt").exists()
    assert not (rig.root / "channels.txt").exists()
    assert rig.calls == []


# --- PHONE-129 do-002: `channels seed`, install's seed for a bot already running ---
#
# The upgrade step for a bot root with no channels.txt (one install's own bot,
# another's hand-written launchd jobs). It runs service.plan_channels +
# write_channels, install's own path (rule 6), and NEVER launchctl: those bots
# were not made by `bot install`, and re-running install there would write a
# second job for the same account. The bot itself never seeds (rule 4; pinned
# in tests/bridge/test_list_alert.py).

def _owner_line(rig, addr=OWNER):
    c = rig.root / "callers.txt"
    c.write_text(f"{addr}  # owner, Bea, joined 2030-06-09\n")
    c.chmod(0o600)
    return c


def test_seed_lists_the_callers_owner_on_an_existing_root_with_no_list(rig):
    callers = _owner_line(rig)
    f = rig.root / "channels.txt"
    r = CliRunner().invoke(cli, ["channels", "seed"])
    assert r.exit_code == 0, r.output
    e = channels.load(f).entries["owner"]
    assert e.address == OWNER and f"the owner line of {callers}" in e.note
    assert _mode(f) == 0o600
    assert r.output == f"channels:  owner {OWNER}, listed in {f}\n"   # the line install prints
    assert rig.calls == []                          # no bootout, no bootstrap, no print


def test_seed_writes_what_install_writes(rig, monkeypatch, tmp_path, given_seat):
    """The same path, so the same line: only the timestamp in the note may differ."""
    given_seat("ada-chat")     # this test installs as ada
    _owner_line(rig)
    monkeypatch.setattr(service.time, "strftime", lambda fmt, *a: "2030-12-08T23:34:56+1100")
    assert CliRunner().invoke(cli, ["channels", "seed"]).exit_code == 0
    seeded = (rig.root / "channels.txt").read_text()
    (rig.root / "channels.txt").unlink()
    service.install("ada")
    assert (rig.root / "channels.txt").read_text() == seeded


def test_seed_reads_the_bots_own_files_from_the_shell(rig, monkeypatch, tmp_path):
    """A hand-written bot (a ~/.<agent>/delta-chat root): DC_CHANNELS_FILE and
    DC_CALLERS_FILE name its files, and nothing lands under the default root."""
    team = tmp_path / "team"
    team.mkdir(mode=0o700)
    (team / "callers.txt").write_text(f"{OWNER}  # owner\n")
    monkeypatch.setenv("DC_CHANNELS_FILE", str(team / "channels.txt"))
    monkeypatch.setenv("DC_CALLERS_FILE", str(team / "callers.txt"))
    r = CliRunner().invoke(cli, ["channels", "seed"])
    assert r.exit_code == 0, r.output
    assert channels.load(team / "channels.txt").entries["owner"].address == OWNER
    assert not (rig.root / "channels.txt").exists()
    assert rig.calls == []


def test_seed_with_no_owner_line_writes_nothing_and_names_the_file(rig):
    c = rig.root / "callers.txt"
    c.write_text(f"{OWNER}\n")                      # approved, but not marked owner
    f = rig.root / "channels.txt"
    r = CliRunner().invoke(cli, ["channels", "seed"])
    assert r.exit_code == 2, r.output
    assert "NOWHERE" in r.output and str(f) in r.output and str(c) in r.output
    assert not f.exists()
    assert rig.calls == []


def test_seed_with_no_owner_line_leads_with_channels_add_not_install(rig):
    """do-002 review A1: these bots were not made by install, so the seed's
    refusal names `channels add owner ADDR` (and `link`), never `bot install`.
    install's own refusal keeps its text."""
    f = rig.root / "channels.txt"
    r = CliRunner().invoke(cli, ["channels", "seed"])
    assert r.exit_code == 2, r.output
    name = " ".join(r.output.split()).split("Name the owner: ", 1)[1]
    assert name.startswith(f"`DC_CHANNELS_FILE={f} sessionphone channels add owner ADDR`")
    assert "`sessionphone link`" in name and "then run `sessionphone channels seed` again" in name
    assert "bot install" not in r.output
    with pytest.raises(service.NoChannels, match=r"Name the owner: run `sessionphone link`"):
        service.plan_channels({"SESSIONPHONE_ROOT": str(rig.root)})
    assert not f.exists() and rig.calls == []


def test_seed_with_no_callers_file_writes_nothing(rig):
    r = CliRunner().invoke(cli, ["channels", "seed"])
    assert r.exit_code == 2 and "no owner line" in r.output, r.output
    assert not (rig.root / "channels.txt").exists()
    assert rig.calls == []


def test_seed_never_touches_a_list_that_has_a_channel(rig):
    _owner_line(rig)
    f = rig.root / "channels.txt"
    f.write_text("team  chat:12\n")                  # a hand-made stopgap: the Desk only
    f.chmod(0o600)
    before = f.read_bytes()
    r = CliRunner().invoke(cli, ["channels", "seed"])
    assert r.exit_code == 0, r.output
    assert f.read_bytes() == before
    assert "already lists a channel; left alone" in r.output and str(f) in r.output
    assert rig.calls == []


def test_seed_twice_is_a_no_op_the_second_time(rig):
    _owner_line(rig)
    f = rig.root / "channels.txt"
    assert CliRunner().invoke(cli, ["channels", "seed"]).exit_code == 0
    before = f.read_bytes()
    r = CliRunner().invoke(cli, ["channels", "seed"])
    assert r.exit_code == 0 and "left alone" in r.output
    assert f.read_bytes() == before


def test_seed_fills_an_empty_list_as_install_does(rig):
    """No channel in it (comments only): install's seed fills it, keeping the comment."""
    _owner_line(rig)
    f = rig.root / "channels.txt"
    f.write_text("# nobody yet\n")
    f.chmod(0o600)
    r = CliRunner().invoke(cli, ["channels", "seed"])
    assert r.exit_code == 0, r.output
    assert f.read_text().startswith("# nobody yet\n")
    assert list(channels.load(f).entries) == ["owner"]


@pytest.mark.parametrize("mode", [0o664, 0o000])
def test_seed_refuses_a_loose_or_unreadable_list_and_says_seed_again(rig, mode):
    """Never tightened quietly (rule 4). The retry is `channels seed`, not
    `bot install`: these bots were not made by install (design review F2)."""
    if mode == 0 and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    _owner_line(rig)
    f = rig.root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(mode)
    try:
        r = CliRunner().invoke(cli, ["channels", "seed"])
        assert r.exit_code == 2, r.output
        assert str(f) in r.output and "then run `sessionphone channels seed` again" in r.output
        assert "bot install" not in r.output
        assert _mode(f) == mode
        assert rig.calls == []
    finally:
        f.chmod(0o600)


def test_install_keeps_its_own_retry_wording(rig):
    f = rig.root / "channels.txt"
    f.write_text("family  chat:42\n")
    f.chmod(0o664)
    with pytest.raises(service.NoChannels, match="then run `sessionphone bot install` again"):
        service.plan_channels({"SESSIONPHONE_ROOT": str(rig.root)})


def test_readme_and_changelog_name_the_upgrade_step():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    readme = " ".join((root / "README.md").read_text().split())
    notice = " ".join((root / "CHANGELOG.md").read_text().split("## [0.1.0]")[0].split())
    for text in (readme, notice):
        assert "`sessionphone channels seed`" in text
        assert "restart each bot after upgrading" in text
    assert "never touches launchd" in readme and "never touches launchd" in notice

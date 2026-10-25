"""`sessionphone callers` (list, add, pin) and `link` read and write the
callers file the BOT reads: dc_env.callers_file(), which is DC_CALLERS_FILE,
else <DC_ROOT, else SESSIONPHONE_ROOT>/<DC_PROFILE>/callers.txt. A CLI run
with a bot's environment touches that bot's file, never a second one under
SESSIONPHONE_ROOT the bot never reads.

`channels` already resolves through dc_env.channels_file(); the channels
tests here hold that in place."""
from __future__ import annotations

import types

import _keyfakes as kf
import pytest
from click.testing import CliRunner

from sessionphone import account, channels, keygate, paths
from sessionphone.bridge import dc_env
from sessionphone.cli import main

BOB = "bob@example.org"
BEA = "bea@phone.example.org"


def cli(*args):
    return CliRunner().invoke(main, list(args))


@pytest.fixture
def cli_root(tmp_path, monkeypatch):
    """SESSIONPHONE_ROOT: the CLI's own root, private, with no callers file."""
    r = tmp_path / "phone"
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    paths.ensure_private(create=True)
    return r


@pytest.fixture
def callers_file_env(cli_root, tmp_path, monkeypatch):
    """A bot run with DC_CALLERS_FILE: its callers file sits outside the root."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    f = shared / "callers.txt"
    monkeypatch.setenv("DC_CALLERS_FILE", str(f))
    return f


@pytest.fixture
def profile_env(cli_root, tmp_path, monkeypatch):
    """A bot run with DC_ROOT + DC_PROFILE: its file is <root>/<profile>/callers.txt."""
    bot_root = tmp_path / "bot-root"
    (bot_root / "work").mkdir(parents=True, mode=0o700)
    bot_root.chmod(0o700)
    monkeypatch.setenv("DC_ROOT", str(bot_root))
    monkeypatch.setenv("DC_PROFILE", "work")
    return bot_root / "work" / "callers.txt"


@pytest.fixture(params=["callers_file_env", "profile_env"])
def bot_file(request, cli_root):
    """Either bot environment; -> the file that bot reads."""
    f = request.getfixturevalue(request.param)
    assert dc_env.callers_file() == f            # the bot's own resolver agrees
    assert f != cli_root / "callers.txt"         # and it is not the CLI root's file
    return f


def stray(cli_root):
    return cli_root / "callers.txt"


# --- callers add ------------------------------------------------------------

def test_add_writes_the_file_the_bot_reads(bot_file, cli_root):
    r = cli("callers", "add", BOB, "--key", kf.FPR_K1)
    assert r.exit_code == 0, r.output
    assert bot_file.exists(), f"add wrote {stray(cli_root)}, not the bot's {bot_file}"
    assert keygate.read_caller_entries(bot_file)[0].key == kf.FPR_K1
    assert not stray(cli_root).exists()


def test_add_says_which_file_it_wrote(bot_file):
    r = cli("callers", "add", BOB, "--key", kf.FPR_K1)
    assert r.exit_code == 0, r.output
    assert str(bot_file) in r.output


def test_add_sees_a_line_already_in_the_bots_file(bot_file, cli_root, monkeypatch):
    keygate.add_entry(bot_file, BOB, kf.FPR_K1)

    def locked(create=False):           # the bot holds the account: nothing is opened
        raise RuntimeError("account is locked by another process")
        yield
    monkeypatch.setattr(account, "_open", locked)
    r = cli("callers", "add", BOB)      # no --key: pinned already, nothing to find
    assert r.exit_code == 0, r.output
    assert "already approved" in r.output
    assert not stray(cli_root).exists()


# --- callers pin ------------------------------------------------------------

def test_pin_pins_the_line_in_the_bots_file(bot_file, cli_root):
    keygate.add_entry(bot_file, BOB, None)          # an UNPINNED line, as an old file has
    r = cli("callers", "pin", BOB, "--key", kf.FPR_K1)
    assert r.exit_code == 0, r.output
    assert keygate.read_caller_entries(bot_file)[0].key == kf.FPR_K1
    assert not stray(cli_root).exists()


def test_pin_says_which_file_it_wrote(bot_file):
    keygate.add_entry(bot_file, BOB, None)
    r = cli("callers", "pin", BOB, "--key", kf.FPR_K1)
    assert r.exit_code == 0, r.output
    assert str(bot_file) in r.output


# --- callers (the list) -----------------------------------------------------

def test_the_list_reads_the_bots_file(bot_file):
    keygate.add_entry(bot_file, BOB, kf.FPR_K1)
    r = cli("callers")
    assert r.exit_code == 0, r.output
    assert f"{BOB}  key:{kf.FPR_K1}" in r.output


def test_an_empty_list_names_the_bots_file(bot_file):
    r = cli("callers")
    assert r.exit_code == 0, r.output
    assert str(bot_file) in r.output


# --- link -------------------------------------------------------------------

class _Chat:
    def send_text(self, _text):
        pass


class _Contact:
    def get_snapshot(self):
        return types.SimpleNamespace(address="Bea@Phone.example.org", display_name="Bea",
                                     is_key_contact=True)

    def make_vcard(self):
        return kf.vcard(BEA, kf.K1)

    def create_chat(self):
        return _Chat()


class _Account:
    def start_io(self):
        pass

    def wait_for_event(self):
        return {"kind": "SecurejoinInviterProgress", "progress": 1000, "contact_id": 7}

    def get_contact_by_id(self, _cid):
        return _Contact()


@pytest.fixture
def linker(monkeypatch):
    monkeypatch.setattr(account, "_open", lambda create=False: iter([_Account()]))
    monkeypatch.setattr(account.time, "sleep", lambda _s: None)


def test_link_writes_the_owner_line_into_the_bots_file(bot_file, cli_root, linker):
    info = account.link(timeout=5, greet=False)
    assert info["owner"], info
    assert bot_file.exists(), f"link wrote {stray(cli_root)}, not the bot's {bot_file}"
    assert account.read_owner(bot_file) == BEA
    assert not stray(cli_root).exists()


def test_link_reports_the_bots_file(bot_file, linker):
    info = account.link(timeout=5, greet=False)
    assert info["callers"] == str(bot_file)


def test_link_makes_no_second_owner_when_the_bots_file_has_one(bot_file, cli_root, linker):
    keygate.add_entry(bot_file, BOB, kf.FPR_K2, "owner, Bob")
    info = account.link(timeout=5, greet=False)
    assert not info["owner"], info              # the first to join is the owner; Bob was
    assert not stray(cli_root).exists()


# --- channels: already the bot's file (held in place) ----------------------

def test_channels_add_writes_the_bots_channels_file(cli_root, tmp_path, monkeypatch):
    bot_root = tmp_path / "bot-root"
    (bot_root / "work").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("DC_ROOT", str(bot_root))
    monkeypatch.setenv("DC_PROFILE", "work")
    want = bot_root / "work" / "channels.txt"
    assert dc_env.channels_file() == want
    r = cli("channels", "add", "family", "chat:42")
    assert r.exit_code == 0, r.output
    assert channels.load(want).entries["family"].target == "chat:42"
    assert not (cli_root / "channels.txt").exists()


def test_link_lists_the_owner_in_the_bots_channels_file(cli_root, tmp_path, monkeypatch, linker):
    f = tmp_path / "shared" / "channels.txt"
    f.parent.mkdir(mode=0o700)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(f))
    info = account.link(timeout=5, greet=False)
    assert info["channels"] == str(f)
    assert not (cli_root / "channels.txt").exists()


# --- the account: the CLI opens the account the BOT opens -------------------
#
# The bot opens DC_ACCOUNTS, else <home>/accounts (home as the callers file:
# DC_ROOT, else SESSIONPHONE_ROOT, plus /DC_PROFILE). `link` and `callers
# add|pin` without --key open the same folder, never <SESSIONPHONE_ROOT>/accounts.


@pytest.fixture
def accounts_env(cli_root, tmp_path, monkeypatch):
    """A bot run with DC_ACCOUNTS: its account sits outside the root."""
    a = tmp_path / "bot-accounts"
    monkeypatch.setenv("DC_ACCOUNTS", str(a))
    return a


@pytest.fixture
def profile_accounts_env(profile_env):
    """A bot run with DC_ROOT + DC_PROFILE: its account is <root>/<profile>/accounts."""
    return profile_env.parent / "accounts"


@pytest.fixture(params=["accounts_env", "profile_accounts_env"])
def bot_accounts(request, cli_root):
    """Either bot environment; -> the accounts folder that bot opens."""
    a = request.getfixturevalue(request.param)
    assert dc_env.accounts_dir() == a            # the bot's own resolver
    assert a != cli_root / "accounts"            # and it is not the CLI root's
    return a


class _KeyAccount(kf.Account):
    def is_configured(self):
        return True


class _LinkAccount(_Account):
    def is_configured(self):
        return True


@pytest.fixture
def opened(monkeypatch):
    """A fake rpc-server: records the accounts folder each open names, and
    hands back ACCOUNT[0] (a configured account)."""
    seen: list[str] = []
    holder: list = [_KeyAccount([kf.Contact(1, BOB, kf.K1)])]

    class Rpc:
        def __init__(self, accounts_dir, rpc_server_path):
            seen.append(accounts_dir)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class DeltaChat:
        def __init__(self, _rpc):
            pass

        def get_all_accounts(self):
            return [holder[0]]

    monkeypatch.setattr(account, "Rpc", Rpc)
    monkeypatch.setattr(account, "DeltaChat", DeltaChat)
    monkeypatch.setattr(account, "server_path", lambda: "deltachat-rpc-server")
    return types.SimpleNamespace(seen=seen, holder=holder)


def test_the_bot_and_the_cli_share_one_accounts_resolver(monkeypatch, tmp_path):
    e = {"DC_ROOT": str(tmp_path / "r"), "DC_PROFILE": "work"}
    assert dc_env.accounts_dir(e) == tmp_path / "r" / "work" / "accounts"
    assert dc_env.accounts_dir({**e, "DC_ACCOUNTS": str(tmp_path / "a")}) == tmp_path / "a"
    assert dc_env.accounts_dir({"SESSIONPHONE_ROOT": str(tmp_path / "s")}) == tmp_path / "s" / "accounts"
    assert not hasattr(paths, "accounts_dir")      # no second answer left to drift
    assert not hasattr(paths, "callers_path")


def test_callers_pin_without_a_key_opens_the_bots_account(bot_accounts, bot_file, opened):
    keygate.add_entry(bot_file, BOB, None)
    r = cli("callers", "pin", BOB)
    assert r.exit_code == 0, r.output
    assert opened.seen == [str(bot_accounts)]
    assert keygate.read_caller_entries(bot_file)[0].key == kf.FPR_K1
    assert str(bot_accounts) in r.output            # it says which account it opened


def test_callers_add_without_a_key_opens_the_bots_account(bot_accounts, opened, cli_root):
    r = cli("callers", "add", BOB)
    assert r.exit_code == 0, r.output
    assert opened.seen == [str(bot_accounts)]
    assert str(bot_accounts) in r.output
    assert not (cli_root / "accounts").exists()


def test_link_opens_the_bots_account(bot_accounts, opened, monkeypatch, cli_root):
    monkeypatch.setattr(account.time, "sleep", lambda _s: None)
    opened.holder[0] = _LinkAccount()
    info = account.link(timeout=5, greet=False)
    assert opened.seen == [str(bot_accounts)]
    assert info["account"] == str(bot_accounts)
    assert not (cli_root / "accounts").exists()


def test_link_says_which_account_it_holds(bot_accounts, opened, monkeypatch):
    monkeypatch.setattr(account.time, "sleep", lambda _s: None)
    opened.holder[0] = _LinkAccount()
    r = cli("link", "--no-greet", "--timeout", "5")
    assert r.exit_code == 0, r.output
    assert f"account: {bot_accounts}" in r.output


def test_an_unset_environment_still_opens_the_roots_account(cli_root, opened):
    keygate.add_entry(cli_root / "callers.txt", BOB, None)
    r = cli("callers", "pin", BOB)
    assert r.exit_code == 0, r.output
    assert opened.seen == [str(cli_root / "accounts")]


# --- the bot's own hints: no hand-edit branch once the CLI follows the bot --


def test_the_no_owner_hint_names_link_for_a_profile_bot(profile_env):
    got = dc_env.callers_problem()
    assert "sessionphone link" in got and "one per line" not in got
    assert "DC_PROFILE=work" in got                 # with the env the command needs


def test_the_ignored_hint_names_callers_add_past_a_callers_file(callers_file_env, monkeypatch):
    monkeypatch.setenv("DC_CALLERS", BOB)
    got = dc_env.callers_ignored()
    assert "sessionphone callers add" in got and "one per line" not in got
    assert f"DC_CALLERS_FILE={callers_file_env}" in got


def test_the_unpinned_hint_names_pin_with_the_bots_settings(bot_file):
    # the file keeps keys, but no line is pinned yet: the repair is `callers pin`,
    # and it reaches this file only with the bot's settings in front of it
    bot_file.write_text(f"{keygate.MARKER}\n{BOB}\n")
    got = dc_env.callers_problem()
    assert "lists 1 address(es), none pinned" in got
    want = dc_env.with_env()
    assert want                                     # this bot's settings move the file
    assert f"`{want}sessionphone callers pin ADDR --key FPR`" in got
    assert f"`{want}sessionphone callers pin ADDR`" in got


def test_the_unpinned_hint_is_bare_when_nothing_moves_the_file(cli_root):
    (cli_root / "callers.txt").write_text(f"{keygate.MARKER}\n{BOB}\n")
    got = dc_env.callers_problem()
    assert "`sessionphone callers pin ADDR --key FPR`" in got


# --- the invite: kept in the bot's home, beside the account setup opens ------
# `setup` opens the bot's account (above), so the invite it saves is that
# account's: it goes in the bot's home, where the bot itself rewrites it
# (displayname.at_start) and `bot install` reads it, never over the default
# profile's <root>/invite.txt. `invite` reads the same file.

INVITE = "https://i.delta.chat/#ABCDEF0123&v=3&i=x&s=y&a=work%40relay.example&n=Work"


class _SetupAccount:
    def __init__(self):
        self.config = {}

    def is_configured(self):
        return False

    def set_config(self, k, v):
        self.config[k] = v

    def get_config(self, k):
        return self.config.get(k)

    def init_transports(self, *_qr):
        self.config["addr"] = "work@relay.example"

    def start_io(self):
        pass

    def get_qr_code(self):
        return INVITE


def test_setup_saves_the_invite_in_the_bots_home(profile_env, cli_root, opened):
    home = profile_env.parent
    (cli_root / "invite.txt").write_text("https://i.delta.chat/#DEFAULT&n=Default\n")
    opened.holder[0] = _SetupAccount()
    info = account.setup("Work")
    assert opened.seen == [str(home / "accounts")]
    assert info["invite_file"] == str(home / "invite.txt")
    assert (home / "invite.txt").read_text().strip() == INVITE
    # the default profile's invite is left as it was
    assert (cli_root / "invite.txt").read_text().strip() == "https://i.delta.chat/#DEFAULT&n=Default"


def test_invite_reads_the_bots_home(profile_env, cli_root):
    (cli_root / "invite.txt").write_text("https://i.delta.chat/#DEFAULT&n=Default\n")
    (profile_env.parent / "invite.txt").write_text(INVITE + "\n")
    assert account.invite() == INVITE


def test_an_unset_environment_keeps_the_invite_under_the_root(cli_root):
    assert dc_env.invite_file() == cli_root / "invite.txt"
    assert not hasattr(paths, "invite_path")

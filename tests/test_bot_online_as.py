"""The bot comes up as the account `setup --name` made (PHONE-60, half 2).

A walk-through on a fresh Mac (F4, 2030-06-09): after `sessionphone setup --name Sam`
and `sessionphone bot install`, bot.log said "online as None - listening".

`setup` makes the account on a chatmail relay with init_transports, and
reads its address back with account.address(): the first transport, else the
legacy "addr" config key. Before fix-001 the bot's start line read only
account.get_config("addr"), which such an account need not set, and never
named the display name setup gave it; the list-alert mail's account was empty
for the same reason.

These tests run bot.main() up to its "online as" line against a fake account
shaped the way setup leaves one: an address in its transport, none under the
legacy "addr" key, displayname "Sam". Fakes only: no rpc-server, no relay,
no launchctl, no live bot.
"""
import pytest

from sessionphone.bridge import bot

ADDR = "sam1x2y3z@relay.example.org"
NAME = "Sam"


class Online(Exception):
    """Raised by the log stand-in once the "online as" line is out: main()
    stops there, before any loop."""


class FakeAccount:
    """An account as `setup --name Sam` leaves it on a chatmail relay."""

    def __init__(self, transports, config):
        self._transports, self._config = transports, config
        self.started = False

    def is_configured(self):
        return True

    def list_transports(self):
        return list(self._transports)

    def get_config(self, key):
        return self._config.get(key)

    def start_io(self):
        self.started = True


class FakeRpc:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub_main(monkeypatch, tmp_path, account, log) -> None:
    """Stub out everything bot.main() does before it opens ACCOUNT."""
    monkeypatch.setattr(bot, "log", log)
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(bot.seatmail, "check", lambda *a, **k: None)
    monkeypatch.setattr(bot.seatmail, "setup", lambda *a, **k: "stub")
    monkeypatch.setattr(bot.seatmail, "seat", lambda *a, **k: "stub")
    monkeypatch.setattr(bot.seat, "have_box", lambda env, shell: bot.seat.Box("stub", "stub@h", "/box"))
    monkeypatch.setattr(bot.dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot.dc_env, "callers_problem", lambda *a, **k: None)
    monkeypatch.setattr(bot.dc_env, "callers_ignored", lambda *a, **k: None)
    monkeypatch.setattr(bot, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(bot.SPOOL, "counts", lambda: (0, 0))
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "server_choice", lambda: ("/venv/bin/deltachat-rpc-server", "venv", "test"))
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "DeltaChat", lambda rpc: type("DC", (), {"get_all_accounts": lambda s: [account]})())


def online_line(monkeypatch, tmp_path, account) -> str:
    """bot.main() with everything before the account stubbed out, up to the
    "online as" line. -> that line."""
    lines = []

    def log(msg):
        lines.append(msg)
        if msg.startswith("online as"):
            raise Online(msg)

    stub_main(monkeypatch, tmp_path, account, log)
    with pytest.raises(Online):
        bot.main()
    assert account.started, "main() logged online before start_io"
    return next(x for x in lines if x.startswith("online as"))


def setup_sam():
    """What setup --name Sam leaves: the address in the transport, none under
    the legacy key; the name under displayname."""
    return FakeAccount([{"addr": ADDR}], {"displayname": NAME})


def test_a_legacy_account_is_online_as_its_address(monkeypatch, tmp_path):
    """The rig's premise, passing today: an account that does keep "addr"
    (an older core, no transports) is named by it."""
    line = online_line(monkeypatch, tmp_path, FakeAccount([], {"addr": ADDR, "displayname": NAME}))
    assert ADDR in line


def test_the_bot_is_online_as_the_address_setup_made(monkeypatch, tmp_path):
    """F4 half 2, the address. Failed before fix-001: "online as None - listening"."""
    line = online_line(monkeypatch, tmp_path, setup_sam())
    assert "online as None" not in line, line
    assert ADDR in line, line


def test_the_bot_comes_up_under_the_name_setup_gave_it(monkeypatch, tmp_path):
    """F4 half 2, the name: `setup --name Sam` is carried, and the start line
    says so. Failed before fix-001: the line named no display name at all."""
    line = online_line(monkeypatch, tmp_path, setup_sam())
    assert NAME in line, line


def test_an_account_with_no_name_is_online_as_its_address_alone(monkeypatch, tmp_path):
    """No displayname set: the line names the address, and nothing stands in
    for the missing name (no "None <...>")."""
    line = online_line(monkeypatch, tmp_path, FakeAccount([{"addr": ADDR}], {}))
    assert line.startswith(f"online as {ADDR} - listening"), line
    assert "None" not in line, line


def test_an_account_with_no_address_says_so_not_none(monkeypatch, tmp_path):
    """Neither a transport nor the legacy key: the line says so in words."""
    line = online_line(monkeypatch, tmp_path, FakeAccount([], {"displayname": NAME}))
    assert "None" not in line, line
    assert "(no address)" in line, line


def test_the_list_alert_names_the_address_setup_made(monkeypatch, tmp_path):
    """The second site of the same cause (bot.main, ListAlert account=): the
    list-alert mail names the bot's account, "(profile ..., <account>)",
    and it was empty for setup's transport account."""
    seen = {}

    class Captured(Exception):
        pass

    def list_alert(*a, **kw):
        seen.update(kw)
        raise Captured

    stub_main(monkeypatch, tmp_path, setup_sam(), lambda msg: None)
    monkeypatch.setattr(bot.listalert, "ListAlert", list_alert)
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "INVITE", None)
    with pytest.raises(Captured):
        bot.main()
    assert seen.get("account") == ADDR, seen

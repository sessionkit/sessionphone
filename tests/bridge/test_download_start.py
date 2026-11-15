"""The download policy at the bridge's start, and point A (PHONE-283 do-004).

The bot reads its download policy ONCE at start, from the host layer alone
(settings.policy over settings.host_values(dc_env.AGENT)), and logs one line:
`download policy: limit … (file), types … (file), save_dir … (file)`. A
value it cannot use takes its default and is logged; a file that is not the
host layer's shape (a Bad) is caught and logged, and the bot runs on the three
defaults. The parsed policy is kept in bot.POLICY: that is what points A, B
and C read (B and C are tested with their slices).

Point A: at start, before IO, core's download_limit is set to the policy's
limit on an account whose `bot` config is not 1. A bot account (bot=1) gets 0
and a log line saying the bridge holds the limit, since core never tells a
bot account of a message it left unfetched. core_limit never writes `bot`:
main() runs bot_flag_off() first, so a bot=1 account reaches point A only
when turning the flag off failed.

Fakes only: no account, rpc-server, relay, launchd job or dc.db. The point-A
tests use their own fake account here; do-005 may fold them into PHONE-284's
FakeAccount once that is on master.
"""
import json

import pytest

from sessionphone import settings
from sessionphone.bridge import bot

AGENT = "ada"


class FakeAccount:
    """Core's config as a dict, every write recorded in order with start_io."""

    def __init__(self, config=None, refuse=None):
        self.config = dict(config or {})
        self.calls = []
        self.refuse = refuse

    def get_config(self, key):
        return self.config.get(key)

    def set_config(self, key, value):
        if self.refuse:
            raise self.refuse
        self.calls.append(("set_config", key, value))
        self.config[key] = value

    # what main() needs of an account up to its "online as" line
    def is_configured(self):
        return True

    def list_transports(self):
        return [{"addr": "ada1x2y3z@relay.example.org"}]

    def start_io(self):
        self.calls.append(("start_io",))


@pytest.fixture
def phone_json(monkeypatch, tmp_path):
    """phone.json in tmp, nothing in it yet; -> a writer for its bots.ada map."""
    f = tmp_path / "settings" / "phone.json"
    monkeypatch.setattr(settings, "FILE", str(f))
    for k in settings.HOST_ONLY + ("DC_DOWNLOAD_LIMIT", "DC_DOWNLOAD_TYPES", "DC_SAVE_DIR"):
        monkeypatch.delenv(k, raising=False)

    def write(keys, raw=None):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(raw if raw is not None else json.dumps({"bots": {AGENT: keys}}))
        return f
    return write


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(bot, "log", lines.append)
    monkeypatch.setattr(bot, "POLICY", None)
    return lines


def _policy(limit=settings.DEFAULT_LIMIT):
    return settings.Policy(limit=limit, types=frozenset(settings.KINDS),
                           save_dir=settings.default_save_dir("/h"),
                           origin={k: settings.DEFAULT_SOURCE for k in settings.HOST_ONLY})


# ---- point A: core's download_limit ------------------------------------------------

def test_a_non_bot_account_is_told_our_limit(logged):
    """The incident account has no `bot` key: core gets our limit, as a string."""
    acct = FakeAccount({})
    assert bot.core_limit(acct, _policy()) == settings.DEFAULT_LIMIT
    assert acct.calls == [("set_config", "download_limit", "16000000")]
    assert any("download_limit set to 16000000 (16 MB)" in x for x in logged), logged


def test_an_account_with_bot_0_is_not_a_bot_account(logged):
    acct = FakeAccount({"bot": "0"})
    bot.core_limit(acct, _policy(1_000_000))
    assert acct.calls == [("set_config", "download_limit", "1000000")]


def test_a_bot_account_gets_0_and_the_log_line(logged):
    """setup() sets bot=1 on a fresh account: core fetches everything, and the
    bridge holds the limit (design §8)."""
    acct = FakeAccount({"bot": "1"})
    assert bot.core_limit(acct, _policy(1_000_000)) == 0
    assert acct.calls == [("set_config", "download_limit", "0")]
    assert bot.BOT_ACCOUNT_LIMIT in logged, logged
    assert bot.BOT_ACCOUNT_LIMIT == ("download limit kept by the bridge, not core: a bot account "
                                     "(bot=1) is not told of a message core leaves unfetched")


def test_bot_is_never_changed(logged):
    for config in ({}, {"bot": "1"}, {"bot": "0"}):
        acct = FakeAccount(config)
        bot.core_limit(acct, _policy())
        assert [c for c in acct.calls if c[1] != "download_limit"] == []
        assert acct.config.get("bot") == config.get("bot")


def test_a_limit_of_0_means_no_limit_for_core_too(logged):
    acct = FakeAccount({})
    bot.core_limit(acct, _policy(0))
    assert acct.calls == [("set_config", "download_limit", "0")]
    assert any("0 (no limit)" in x for x in logged), logged


def test_core_that_refuses_the_write_never_stops_the_bot(logged):
    acct = FakeAccount({}, refuse=RuntimeError("rpc gone"))
    assert bot.core_limit(acct, _policy()) is None
    assert any("not set: rpc gone" in x and "the bridge holds the limit" in x for x in logged), logged


def test_point_a_reads_the_policy_read_at_start(phone_json, logged, tmp_path):
    """With no policy passed, point A uses bot.POLICY, the one read_policy set."""
    phone_json({"download_limit": "2500000"})
    pol = bot.read_policy(AGENT, tmp_path / "home")
    assert bot.POLICY is pol and pol.limit == 2_500_000
    acct = FakeAccount({})
    bot.core_limit(acct)
    assert acct.calls == [("set_config", "download_limit", "2500000")]


# ---- the start read ----------------------------------------------------------------

def test_the_start_line_names_each_value_and_its_source(phone_json, logged, tmp_path):
    f = phone_json({"download_limit": "1000000", "save_dir": str(tmp_path / "files")})
    pol = bot.read_policy(AGENT, tmp_path / "home")
    line = [x for x in logged if x.startswith("download policy: ")]
    assert line == [pol.summary()], logged
    src = settings.shown(f)
    assert line[0] == (f"download policy: limit 1000000 (1 MB) ({src}), types all (the default), "
                       f"save_dir {settings.shown(tmp_path / 'files')} ({src})")


def test_with_no_file_the_bot_runs_on_the_three_defaults(phone_json, logged, tmp_path):
    pol = bot.read_policy(AGENT, tmp_path / "home")
    assert (pol.limit, pol.types, pol.save_dir) == (
        settings.DEFAULT_LIMIT, frozenset(settings.KINDS), tmp_path / "home" / "attachments")
    assert not [x for x in logged if x.startswith("WARNING")], logged
    assert logged[-1].startswith("download policy: limit 16000000 (16 MB) (the default)")


def test_the_reader_is_the_host_layer_for_dc_agent(phone_json, logged, monkeypatch, tmp_path):
    """No name given: the bot's own name (dc_env.AGENT, the job's DC_AGENT)
    picks the map; another bot's map is not read."""
    phone_json(None, raw=json.dumps({"bots": {AGENT: {"download_limit": "3000000"},
                                              "bea": {"download_limit": "9"}}}))
    monkeypatch.setattr(bot.dc_env, "AGENT", AGENT)
    monkeypatch.setattr(bot.dc_env, "home_dir", lambda: tmp_path / "home")
    pol = bot.read_policy()
    assert pol.limit == 3_000_000
    assert pol.save_dir == tmp_path / "home" / "attachments"


@pytest.mark.parametrize("var", ["DC_DOWNLOAD_LIMIT", "download_limit", "PIP_CHAT_DOWNLOAD_LIMIT"])
def test_the_environment_changes_nothing(phone_json, logged, monkeypatch, tmp_path, var):
    monkeypatch.setenv(var, "5")
    assert bot.read_policy(AGENT, tmp_path / "home").limit == settings.DEFAULT_LIMIT


def test_a_value_that_cannot_be_used_is_logged_and_takes_its_default(phone_json, logged, tmp_path):
    f = phone_json({"download_limit": "16MB", "download_types": "image,video"})
    pol = bot.read_policy(AGENT, tmp_path / "home")
    assert pol.limit == settings.DEFAULT_LIMIT and pol.types == frozenset({"image", "video"})
    warn = [x for x in logged if x.startswith("WARNING: ")]
    assert len(warn) == 1, logged
    assert settings.shown(f) in warn[0] and f"`bots.{AGENT}.download_limit`" in warn[0], warn
    assert logged.index(warn[0]) < logged.index(pol.summary())


@pytest.mark.parametrize("value", [16000000, ["image"], None])
def test_a_shape_error_is_caught_logged_and_the_bot_runs_on_the_defaults(phone_json, logged,
                                                                          tmp_path, value):
    """A typed value makes phone.json the host layer's shape error (Bad):
    caught, logged, and all three keys take their defaults."""
    phone_json({"download_limit": value, "save_dir": "/elsewhere"})
    pol = bot.read_policy(AGENT, tmp_path / "home")
    assert (pol.limit, pol.types, pol.save_dir) == (
        settings.DEFAULT_LIMIT, frozenset(settings.KINDS), tmp_path / "home" / "attachments")
    assert bot.POLICY is pol
    warn = [x for x in logged if x.startswith("WARNING: ")]
    assert len(warn) == 1 and "the defaults are used" in warn[0] and "not a string" in warn[0], logged
    assert logged[-1] == pol.summary()


def test_not_json_is_caught_too(phone_json, logged, tmp_path):
    phone_json(None, raw="{not json")
    pol = bot.read_policy(AGENT, tmp_path / "home")
    assert pol.limit == settings.DEFAULT_LIMIT
    assert any(x.startswith("WARNING: ") and "not JSON" in x for x in logged), logged


def test_anything_else_the_reader_raises_never_stops_the_bot(phone_json, logged, monkeypatch, tmp_path):
    real = settings.policy

    def broken(name, home, found=None):
        if found is None:
            raise PermissionError("no access")
        return real(name, home, found)
    monkeypatch.setattr(settings, "policy", broken)
    pol = bot.read_policy(AGENT, tmp_path / "home")
    assert pol.limit == settings.DEFAULT_LIMIT and bot.POLICY is pol
    assert any(x.startswith("WARNING: ") and "no access" in x for x in logged), logged


# ---- main(): read once, before the account; point A before IO ---------------------

class Online(Exception):
    """Raised by the log stand-in at the "online as" line: main() stops there."""


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_main(monkeypatch, tmp_path, account):
    """bot.main() up to "online as", everything before the account stubbed as
    tests/test_bot_online_as.py stubs it. -> the log lines."""
    lines = []

    def log(msg):
        lines.append(msg)
        if msg.startswith("online as"):
            raise Online(msg)
    reads = []
    real = bot.read_policy

    def counted(*a, **k):
        reads.append(a)
        return real(*a, **k)
    monkeypatch.setattr(bot, "read_policy", counted)
    monkeypatch.setattr(bot, "POLICY", None)
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
    monkeypatch.setattr(bot.dc_env, "AGENT", AGENT)
    monkeypatch.setattr(bot, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(bot.SPOOL, "counts", lambda: (0, 0))
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "server_choice", lambda: ("/venv/bin/deltachat-rpc-server", "venv", "test"))
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "DeltaChat",
                        lambda rpc: type("DC", (), {"get_all_accounts": lambda s: [account]})())
    with pytest.raises(Online):
        bot.main()
    assert len(reads) == 1, reads
    return lines


class FlagStuckAccount(FakeAccount):
    """An account whose `bot` flag cannot be written: bot_flag_off fails."""

    def set_config(self, key, value):
        if key == "bot":
            raise RuntimeError("rpc gone")
        super().set_config(key, value)


# main()'s order is bot_flag_off, then core_limit, then start_io: a bot=1
# account has its flag turned off first, so core is told the REAL limit.
# core_limit's own bot=1 branch (core told 0) is reached from main() only
# when bot_flag_off fails; that branch's unit behaviour is pinned above.
@pytest.mark.parametrize("make, config, want_calls, bot_limit", [
    (FakeAccount, {},
     [("set_config", "download_limit", "1200000"), ("start_io",)], False),
    (FakeAccount, {"bot": "1"},
     [("set_config", "bot", "0"), ("set_config", "download_limit", "1200000"), ("start_io",)], False),
    (FlagStuckAccount, {"bot": "1"},
     [("set_config", "download_limit", "0"), ("start_io",)], True),
], ids=["no-flag", "flag-on-turned-off", "flag-stuck-on"])
def test_main_reads_the_policy_once_and_sets_core_before_io(phone_json, monkeypatch, tmp_path,
                                                            make, config, want_calls, bot_limit):
    phone_json({"download_limit": "1200000"})
    acct = make(config)
    lines = _run_main(monkeypatch, tmp_path, acct)
    assert acct.calls[:len(want_calls)] == want_calls, acct.calls
    assert bot.POLICY is not None and bot.POLICY.limit == 1_200_000
    start = [x for x in lines if x.startswith("download policy: limit 1200000 (1.2 MB)")]
    assert len(start) == 1, lines
    assert (bot.BOT_ACCOUNT_LIMIT in lines) == bot_limit, lines


def test_main_runs_on_the_defaults_with_a_shape_error(phone_json, monkeypatch, tmp_path):
    phone_json({"download_limit": 5})
    acct = FakeAccount({})
    lines = _run_main(monkeypatch, tmp_path, acct)
    assert ("set_config", "download_limit", "16000000") in acct.calls
    assert any(x.startswith("WARNING: download policy: the defaults are used") for x in lines), lines
    assert any(x.startswith("online as") for x in lines)

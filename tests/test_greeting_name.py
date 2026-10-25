"""The call greeting names the bot by the name `setup --name` gave it.

A bot set up with `sessionphone setup --name Ada`, whose agent resolves to the
default (no DC_AGENT, no DC_AGENT_NAME, no DC_GREETING*, no durable seat, no
bot in phone.json), answered a call with "Claude here. Go ahead." and rang
out with "Hi, Claude here, ringing you back.": never Ada.

The greeting is the bridge's own line (bridge/dc_env.py bind_greetings).
`setup --name` sets only the account's display name, and the bot's start
keeps a name set by hand (displayname.at_start). The greetings now take
DC_AGENT_NAME, else DC_NAME, else that settled display name, else the
agent's name; AGENT_NAME (the mail's From, the transcript labels) stays the
agent's.

These tests run bot.main() up to its "online as" line against a fake account
shaped as `setup --name Ada` leaves one (the rig of test_bot_online_as), then
connect a call (the rig of test_recording_notice) and read what is spoken.
Fakes only: no rpc-server, no relay, no launchctl, no speech server.
"""
import pytest
from test_bot_online_as import FakeAccount, online_line
from test_recording_notice import call  # noqa: F401  (a fixture)
from test_recording_switch import rig  # noqa: F401  (a fixture)

from sessionphone.bridge import bot, calls, dc_env

NAME = "Ada"
ADDR = "ada1x2y3z@relay.example.org"
GREETINGS = ("GREETING", "GREETING_OUT", "GREETING_VOICEMAIL")


@pytest.fixture
def unset(monkeypatch, tmp_path):
    """No name set in the shell, agent the default. Every name the start may
    bind is put back after the test."""
    for k in ("DC_AGENT", "DC_AGENT_NAME", "DC_NAME", *(f"DC_{g}" for g in GREETINGS)):
        monkeypatch.delenv(k, raising=False)
    for k in dc_env._BY_AGENT:
        if k in vars(dc_env):
            monkeypatch.setattr(dc_env, k, vars(dc_env)[k])
    for k in GREETINGS:
        monkeypatch.setattr(calls, k, getattr(calls, k))
    monkeypatch.setattr(dc_env, "home_dir", lambda: tmp_path)
    assert dc_env.AGENT == "claude", "premise: the agent is the default"


def start(monkeypatch, tmp_path, display=NAME):
    """bot.main() up to "online as", for an account whose display name is DISPLAY."""
    account = FakeAccount([{"addr": ADDR}], {"displayname": display})
    return online_line(monkeypatch, tmp_path, account)


@pytest.fixture
def started(unset, monkeypatch, tmp_path):
    """The bot's start, for an account `setup --name Ada` made, agent the default."""
    line = start(monkeypatch, tmp_path)
    assert NAME in line, f"premise: the bot comes up as {NAME}: {line}"
    return line


def spoken(call, **kw):  # noqa: F811
    s = call.connect(**kw)
    assert s is not None
    return " ".join(call.spoken)


def test_an_incoming_call_is_greeted_by_the_name_setup_gave(started, call):  # noqa: F811
    said = spoken(call)
    assert "Claude" not in said, said
    assert NAME in said, said


def test_a_ring_out_is_greeted_by_the_name_setup_gave(started, call):  # noqa: F811
    said = spoken(call, outgoing=True)
    assert "Claude" not in said, said
    assert NAME in said, said


def test_voicemail_names_the_bot_by_the_name_setup_gave(started, call):  # noqa: F811
    said = spoken(call, voicemail=True)
    assert "Claude" not in said, said
    assert NAME in said, said


@pytest.mark.parametrize("name", GREETINGS)
def test_every_bound_greeting_carries_the_name(started, name):
    text = getattr(calls, name)
    assert "Claude" not in text, f"calls.{name} = {text!r}"
    assert NAME in text, f"calls.{name} = {text!r}"


# ---- what the fix must keep -----------------------------------------------------------------


def test_the_agent_name_stays_the_agents(started):
    """Only the greetings take the display name: the mail's From and the
    transcript labels (AGENT_NAME) and the addresses stay the agent's."""
    assert dc_env.AGENT == "claude"
    assert dc_env.AGENT_NAME == "Claude"
    assert dc_env.ADDR.startswith("claude-chat@")
    assert dc_env.MAILTO == "claude"                 # bare: no host (PHONE-248)
    assert dc_env.GREETING_NAME == NAME


def test_dc_agent_name_still_wins_over_the_display_name(unset, monkeypatch, tmp_path):
    monkeypatch.setenv("DC_AGENT_NAME", "Bo")
    start(monkeypatch, tmp_path)
    for g in GREETINGS:
        text = getattr(calls, g)
        assert "Bo" in text and NAME not in text, f"calls.{g} = {text!r}"


@pytest.mark.parametrize("name", GREETINGS)
def test_a_dc_greeting_is_a_whole_line_and_still_wins(unset, monkeypatch, tmp_path, name):
    monkeypatch.setenv(f"DC_{name}", "Hello, you have reached the bot.")
    start(monkeypatch, tmp_path)
    assert getattr(calls, name) == "Hello, you have reached the bot."
    for other in GREETINGS:
        if other != name:
            assert NAME in getattr(calls, other), f"calls.{other} = {getattr(calls, other)!r}"


def test_dc_name_names_the_greetings_before_the_start(unset, monkeypatch):
    """DC_NAME is the display name the bot will set, so the greetings bound
    on first use (bind_agent) already carry it."""
    monkeypatch.setenv("DC_NAME", NAME)
    monkeypatch.setattr(dc_env, "agent_from", lambda environ=None: ("claude", "the default"))
    dc_env.bind_agent()
    assert dc_env.AGENT_NAME == "Claude"
    for g in GREETINGS:
        assert NAME in getattr(dc_env, g), f"dc_env.{g} = {getattr(dc_env, g)!r}"


@pytest.mark.parametrize("display", [None, "", "   "])
def test_no_display_name_keeps_the_agents_name(unset, display):
    """The start set no name (displayname.at_start failed): the greetings
    stay as bind_agent made them, the agent's name capitalised."""
    calls.settle_greetings(display)
    for g in GREETINGS:
        text = getattr(calls, g)
        assert "Claude" in text, f"calls.{g} = {text!r}"


def test_a_failed_name_at_start_keeps_the_agents_name(unset, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("account busy")
    monkeypatch.setattr(bot.displayname, "at_start", boom)
    start(monkeypatch, tmp_path, display=None)
    assert calls.GREETING == "Claude here. Go ahead."


def test_a_public_profile_ring_names_the_display_name(started, call, monkeypatch):  # noqa: F811
    monkeypatch.setattr(calls, "PROFILE", "public")
    said = spoken(call, outgoing=True)
    assert "Hi carol, Ada here." in said, said
    assert "Claude" not in said, said


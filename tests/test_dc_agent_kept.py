"""DC_AGENT never leaks from one test to the next (PHONE-332).

bridge/bot.py copies sys.argv[1] into an unset DC_AGENT at import, outside any
monkeypatch: the launchd job runs `bot AGENT`. Under pytest that is pytest's
own argument, and it stayed in os.environ for every later test, so a test of
the default agent could pass only because an earlier file had imported the bot
(PHONE-273). tests/conftest.py puts DC_AGENT back after collection and after
every test (dc_agent_kept).

The tests here run in the order they are written (pytest's file order): one
imports the bot afresh, and the next checks what it left. Without the guard,
the second test fails: DC_AGENT is still the argv word.
"""
import importlib
import os
import sys

import pytest

import sessionphone.bridge as bridge_pkg

BOT = "sessionphone.bridge.bot"
WORD = "phone332-argv-agent"       # argv[1] for the fresh import: no real agent's name
_RAN: dict[str, str | None] = {}   # what the first test saw, for the next


@pytest.fixture
def import_bot_afresh(monkeypatch):
    """-> import(): imports bot.py afresh, as the launchd job starts it, with WORD
    as argv[1]. The bot module this process had before (or none) is put back
    after, in sys.modules and on the package, so no later test sees this one."""
    missing = object()
    kept_mod = sys.modules.get(BOT, missing)
    kept_attr = bridge_pkg.__dict__.get("bot", missing)
    monkeypatch.setattr(sys, "argv", ["bot", WORD])

    def fresh():
        sys.modules.pop(BOT, None)
        return importlib.import_module(BOT)

    yield fresh
    for space, key, kept in ((sys.modules, BOT, kept_mod), (bridge_pkg.__dict__, "bot", kept_attr)):
        if kept is missing:
            space.pop(key, None)
        else:
            space[key] = kept


def test_a_bot_imported_in_a_test_sets_dc_agent_from_argv(import_bot_afresh):
    # collection imported the bot with many test modules: none of that is left
    assert "DC_AGENT" not in os.environ
    _RAN["before"] = os.environ.get("DC_AGENT")
    bot = import_bot_afresh()
    assert bot is sys.modules[BOT]
    assert os.environ["DC_AGENT"] == WORD     # the product's argv fill, untouched
    _RAN["during"] = os.environ["DC_AGENT"]


def test_the_next_test_sees_dc_agent_as_it_was_before():
    if "during" not in _RAN:
        pytest.skip("runs after test_a_bot_imported_in_a_test_sets_dc_agent_from_argv, in file order")
    assert _RAN["during"] == WORD
    assert os.environ.get("DC_AGENT") == _RAN["before"]   # unset, as before the import
    assert "DC_AGENT" not in os.environ


def test_the_bot_module_is_the_one_from_before(import_bot_afresh):
    """The fresh import is the test's alone: the module object later tests (and
    the autouse fixtures) find is the one collection imported."""
    kept = sys.modules.get(BOT)
    fresh = import_bot_afresh()
    assert fresh is not kept
    _RAN["fresh"] = fresh


def test_after_a_fresh_import_the_old_bot_module_is_back():
    if "fresh" not in _RAN:
        pytest.skip("runs after test_the_bot_module_is_the_one_from_before, in file order")
    assert sys.modules.get(BOT) is not _RAN["fresh"]
    assert getattr(bridge_pkg, "bot", None) is not _RAN.pop("fresh")


@pytest.fixture(scope="class")
def dc_agent_set_for_the_class():
    """DC_AGENT=ada for a class of tests, set before any test's own guard looks."""
    os.environ["DC_AGENT"] = "ada"
    yield
    os.environ.pop("DC_AGENT", None)


@pytest.mark.usefixtures("dc_agent_set_for_the_class")
class TestASetDcAgentIsPutBackToItsValue:
    """The guard restores the value DC_AGENT had before a test, not merely unset."""

    def test_a_test_that_rewrites_dc_agent(self):
        assert os.environ["DC_AGENT"] == "ada"
        os.environ["DC_AGENT"] = "zed"          # outside monkeypatch, as bot.py writes it
        _RAN["rewrote"] = "zed"

    def test_the_next_test_sees_the_value_from_before(self):
        if "rewrote" not in _RAN:
            pytest.skip("runs after test_a_test_that_rewrites_dc_agent, in file order")
        assert os.environ["DC_AGENT"] == "ada"

    def test_a_test_that_unsets_dc_agent(self):
        del os.environ["DC_AGENT"]
        _RAN["unset"] = "yes"

    def test_the_next_test_sees_it_set_again(self):
        if "unset" not in _RAN:
            pytest.skip("runs after test_a_test_that_unsets_dc_agent, in file order")
        assert os.environ["DC_AGENT"] == "ada"

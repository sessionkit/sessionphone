"""The caller's name on a web call: ONE setting, ONE reader (PHONE-135 SC2).

--caller-name, else $DC_CALLER_NAME, else the role word `owner`. The sites
that read it (the auth-ok frame, the turn mail, a new passkey's label) are
pinned beside their own rigs: tests/web/test_server.py, test_call.py,
test_passkeys.py.
"""
from __future__ import annotations

import pytest

from sessionphone.web import names


def test_the_default_is_a_role_word_not_a_person():
    assert names.caller_name(environ={}) == names.DEFAULT == "owner"


def test_the_flag_wins_over_the_env():
    assert names.caller_name("Ada", environ={"DC_CALLER_NAME": "Bea"}) == "Ada"


def test_the_env_is_read_when_no_flag_is_given():
    assert names.caller_name(None, environ={"DC_CALLER_NAME": "Bea"}) == "Bea"


def test_the_process_env_is_read_by_default(monkeypatch):
    monkeypatch.setenv("DC_CALLER_NAME", "Bea")
    assert names.caller_name() == "Bea"
    monkeypatch.delenv("DC_CALLER_NAME")
    assert names.caller_name() == "owner"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n", "\x00\x1b"])
def test_a_blank_value_falls_through_to_the_next_source(blank):
    assert names.caller_name(blank, environ={"DC_CALLER_NAME": "Bea"}) == "Bea"
    assert names.caller_name(blank, environ={"DC_CALLER_NAME": blank}) == "owner"


def test_control_characters_are_dropped_and_whitespace_collapses():
    """The name goes into a mail Subject and a JSON frame: one line, no escapes."""
    got = names.caller_name("  Ada\r\nBcc: x@y\x1b[31m  Lovelace\t", environ={})
    assert got == "Ada Bcc: x@y [31m Lovelace"
    assert all(ch.isprintable() for ch in got) and "\n" not in got and "\r" not in got


def test_a_long_name_is_cut_to_a_label():
    got = names.caller_name("x" * 500, environ={})
    assert got == "x" * names.MAX_LEN


def test_the_old_turns_default_is_gone():
    """turns.CALLER kept its own default; now no site does (design 1)."""
    from sessionphone.web import turns
    assert not hasattr(turns, "CALLER")

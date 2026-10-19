"""A NAME that matches no contact, or several, is told in words (PHONE-64).

SK-8 Tart walk F5: `dc-ring nobody@example.org` printed
`FAILED - ValueError: call_to: 0 contacts match 'nobody@example.org'`. The ack
now says WHO was not found (or lists the contacts that collide, as
'Name <addr>') and WHAT TO DO NEXT, with no Python class name. Any other
exception still carries its class name. The tool still exits 3 on it.

One pass of main()'s outbox loop, on tests/test_group_job.py's rig (fakes
only, never a real Delta send); the exit code through the real bin/dc-ring
and bin/dc-dm against tests/test_dc_ack.py's fake bot.
"""
import pytest
from test_dc_ack import Bot, run
from test_group_job import (  # noqa: F401  (rig is a fixture)
    OTHER_BOB,
    STRANGER,
    FakeContact,
    rig,
)

from sessionphone.bridge import bot, calls

NOBODY = "nobody@example.org"
NO_MATCH = (f"{NOBODY!r} matches no contact this profile knows. Check the spelling or the address, "
            "or add them as a contact first.")
TWO_BOBS = (f"'Bob' matches 2 contacts: Bob <{STRANGER}>, Bob <{OTHER_BOB}>. Name one by its address, "
            "or list it (sessionphone channels add NAME ADDRESS) and use that NAME.")


@pytest.fixture
def calls_on(rig, monkeypatch):  # noqa: F811
    """A profile that may ring: dc-ring's job gets past the calls gate (PHONE-145)."""
    monkeypatch.setattr(bot, "CALLS", True)
    monkeypatch.setattr(bot, "calls_refusal", lambda starts=True: None)
    monkeypatch.setattr(calls, "setup", lambda *a: None)    # no call-mail thread: the rig's sleep ends it
    return rig


def a_second_bob(rig):  # noqa: F811
    rig.account.contacts[OTHER_BOB] = FakeContact(rig.account, 3, OTHER_BOB, "Bob", 14)


def test_ring_matching_no_contact_says_who_and_what_next(calls_on) -> None:
    calls_on.write()
    (ack,) = calls_on.run({"call_to": NOBODY})
    assert ack == {"ok": False, "detail": NO_MATCH}, ack
    assert "Error" not in ack["detail"] and "call_to" not in ack["detail"]
    assert calls_on.account.calls == [] and calls_on.account.sent == []   # no 1:1 opened, nothing rung
    assert f"SEND FAILED for 00.json: {NO_MATCH}" in calls_on.logs


def test_ring_matching_two_contacts_lists_them(calls_on) -> None:
    a_second_bob(calls_on)
    calls_on.write()
    (ack,) = calls_on.run({"call_to": "Bob"})
    assert ack == {"ok": False, "detail": TWO_BOBS}, ack
    assert calls_on.account.calls == []


def test_dm_matching_no_contact_says_who_and_what_next(rig) -> None:  # noqa: F811
    rig.write()
    (ack,) = rig.run({"dm": NOBODY, "text": "hi"})
    assert ack == {"ok": False, "detail": NO_MATCH}, ack
    assert rig.account.sent == []


def test_dm_matching_two_contacts_lists_them(rig) -> None:  # noqa: F811
    a_second_bob(rig)
    rig.write()
    (ack,) = rig.run({"dm": "Bob", "text": "hi"})
    assert ack == {"ok": False, "detail": TWO_BOBS}, ack
    assert rig.account.calls == [] and rig.account.sent == []


def test_an_unexpected_error_still_carries_its_class_name(rig, monkeypatch) -> None:  # noqa: F811
    """Only a contact match is told plainly; a real fault keeps its class (bot.py's generic handler)."""
    def broken(self):
        raise RuntimeError("core said no")
    monkeypatch.setattr(FakeContact, "create_chat", broken)
    rig.write()
    (ack,) = rig.run({"dm": "Alice", "text": "hi"})
    assert ack == {"ok": False, "detail": "RuntimeError: core said no"}, ack


@pytest.mark.parametrize("tool, args", [("dc-ring", [NOBODY]), ("dc-dm", [NOBODY, "hi"])])
def test_the_tool_exits_3_and_prints_the_message(tmp_path, tool, args) -> None:
    root = tmp_path / "dc"
    with Bot(root, lambda job: (False, NO_MATCH)):
        r = run(tool, root, *args)
    assert r.returncode == 3, r.stderr
    assert f"{tool}: FAILED - {NO_MATCH}" in r.stderr
    assert "ValueError" not in r.stderr and r.stdout == ""

"""PHONE-52: a call's texts lead with the mouth; dc-say is the fallback.

A call's reply goes by the mouth; dc-say is only for when the mouth fails.
A second profile's bot is named by its outbox (DC_OUTBOX=...), never
`dc-say --profile`, which the dc-say of the bridge this was ported from does
not have. No call, no network: the sendmail run is captured.
"""
import pytest

from sessionphone.bridge import bot, calls, dc_env


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    box = tmp_path / "public" / "outbox"
    monkeypatch.setenv("DC_OUTBOX", str(box))
    return box


def test_a_chat_line_answers_with_dc_say():
    assert dc_env.reply_hint(12, profile="") == "dc-say 12 'your reply'"


def test_a_call_leads_with_the_mouth_and_falls_back_to_dc_say():
    lead, fallback = dc_env.reply_hint(12, call=5531, profile="").splitlines()
    assert lead == 'Answer with: mouth say --device call:5531 "text"'
    assert fallback == "If the mouth fails: dc-say --call 5531 12 'text'"


def test_the_public_profile_names_its_outbox_never_profile(outbox):
    hint = dc_env.reply_hint(12, call=5531, profile="public")
    assert "--profile" not in hint
    assert f"If the mouth fails: DC_OUTBOX={outbox} dc-say --call 5531 12 'text'" in hint
    assert dc_env.reply_hint(12, profile="public") == f"DC_OUTBOX={outbox} dc-say 12 'your reply'"


@pytest.fixture
def sent(monkeypatch):
    out = []

    class _Done:
        returncode, stderr = 0, ""

    monkeypatch.setattr(bot.subprocess, "run", lambda *a, **k: out.append(k["input"]) or _Done())
    monkeypatch.setattr(bot, "log", lambda m: None)
    return out


@pytest.mark.parametrize("profile", ["", "public"])
def test_the_footer_of_a_call_line(sent, outbox, monkeypatch, profile):
    monkeypatch.setattr(bot, "PROFILE", profile)
    bot.mail_in("Alice", 12, "Alice", "(on the call) hello", call=5531)
    foot = sent[0].split("\n---\n", 1)[1]
    say = f"DC_OUTBOX={outbox} dc-say" if profile else "dc-say"
    assert "To answer on the call:\n" in foot
    assert '  Answer with: mouth say --device call:5531 "text"\n' in foot
    assert f"  If the mouth fails: {say} --call 5531 12 'text'\n" in foot
    assert "--profile" not in sent[0]


@pytest.mark.parametrize("profile", ["", "public"])
def test_the_footer_of_a_chat_line(sent, outbox, monkeypatch, profile):
    monkeypatch.setattr(bot, "PROFILE", profile)
    bot.mail_in("Alice", 12, "Alice", "hello")
    say = f"DC_OUTBOX={outbox} dc-say" if profile else "dc-say"
    assert f"To answer in that chat:\n  {say} 12 'your reply'\n" in sent[0]
    assert "mouth" not in sent[0]


@pytest.mark.parametrize("profile", ["", "public"])
def test_call_connected_leads_with_the_mouth(outbox, monkeypatch, profile):
    monkeypatch.setattr(calls, "PROFILE", profile)
    s = object.__new__(calls.CallSession)
    s.chat_id, s.caller, s.msg_id, s.root_id = 12, "Alice", 5531, "call-test@example.org"
    text = s._connected_text("12:34", "rang Bob")
    say = f"DC_OUTBOX={outbox} dc-say" if profile else "dc-say"
    assert text.startswith(
        "CALL CONNECTED with Alice at 12:34 (rang Bob).\n"
        'Answer with: mouth say --device call:5531 "text"\n'
        f"If the mouth fails: {say} --call 5531 12 'text'\n")
    assert "--profile" not in text

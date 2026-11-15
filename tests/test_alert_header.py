"""PHONE-334 fix-002 (PB): every bot MACHINE ALERT carries X-Delta-Chat-Alert.

sessionmail SM-1047 lets a pager rule hold back what need not wake a cold seat.
The bot's own alerts (a refused channels list, an owner left unpinned) are such
mail, so they carry one stable header the rule keys on by PRESENCE; the value
names the alert. Chat and call mail never carry it, or a rule meant for alerts
would let Mike's own words ride. An alert carries no X-Delta-Chat-Kind.

send_alert makes it structural: whatever a caller passes, the header goes out,
under its exact name; a caller's own value is kept, and a missing one is `bot`.

The name is written here as a LITERAL, never read back from bot.ALERT_HEADER,
and checked as the exact `--header` argument `message send` is given. Fakes
only, as tests/test_wake_contract.py (whose rig and builders this borrows).
"""
import concurrent.futures
import pathlib

import pytest
import test_contract_headers as ch
import test_wake_contract as wc

from sessionphone import channels
from sessionphone.bridge import bot, calls, listalert, ownerkey
from sessionphone.web import turns

ALERT = "X-Delta-Chat-Alert"                    # the pager rule keys on its presence
KIND = "X-Delta-Chat-Kind"

rig = ch.rig                                    # the fake account and `message`


def header_args(argv):
    """Every `--header` value in one `message send` argv."""
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--header"]


def alerts_in(argv):
    """The X-Delta-Chat-Alert values, by the exact name, and any header whose
    name is the alert's in another case (that one would be a bug too)."""
    hs = header_args(argv)
    exact = [h[len(ALERT) + 2:] for h in hs if h.startswith(f"{ALERT}: ")]
    other = [h for h in hs if h.lower().startswith(ALERT.lower() + ":") and not h.startswith(f"{ALERT}: ")]
    return exact, other


def the_alert(rig):
    """The one mail sent, and its one alert value."""
    (sent,) = rig.sends()
    exact, other = alerts_in(sent["argv"])
    assert other == [], sent["argv"]
    assert len(exact) == 1, sent["argv"]         # present, exactly once
    return exact[0], sent


def no_alert(argv):
    exact, other = alerts_in(argv)
    return exact == [] and other == [] and not any(ALERT.lower() in a.lower() for a in argv)


# ---- send_alert: the header is always there -------------------------------------------

@pytest.mark.parametrize("headers", [None, {}, {"X-Delta-Chat-Bridge": "1"}, {ALERT: ""}, {ALERT: "  "}])
def test_send_alert_stamps_the_default_when_the_caller_gave_none(rig, headers):
    assert bot.send_alert("s", "b", headers)[0] is True
    value, _ = the_alert(rig)
    assert value == "bot"


def test_send_alert_keeps_the_callers_value(rig):
    bot.send_alert("s", "b", {"X-Delta-Chat-Bridge": "1", ALERT: "channels-missing"})
    value, sent = the_alert(rig)
    assert value == "channels-missing"
    assert "X-Delta-Chat-Bridge: 1" in header_args(sent["argv"])


def test_send_alert_keeps_a_value_given_under_another_case_and_sends_the_exact_name(rig):
    bot.send_alert("s", "b", {"x-delta-chat-alert": "owner-unpinned"})
    assert the_alert(rig)[0] == "owner-unpinned"


def test_send_alert_with_no_headers_argument(rig):
    bot.send_alert("s", "b")
    assert the_alert(rig)[0] == "bot"


def test_send_alert_does_not_change_the_callers_dict(rig):
    given = {"X-Delta-Chat-Bridge": "1"}
    bot.send_alert("s", "b", given)
    assert given == {"X-Delta-Chat-Bridge": "1"}


def test_an_alert_carries_no_kind(rig):
    bot.send_alert("s", "b", {"X-Delta-Chat-Bridge": "1"})
    _, sent = the_alert(rig)
    assert wc.kinds_sent(sent["argv"]) == []
    assert not any(h.lower().startswith(KIND.lower()) for h in header_args(sent["argv"]))


def test_send_alert_goes_to_the_agent_seat(rig):
    bot.send_alert("s", "b")
    (sent,) = rig.sends()
    assert bot.dc_env.MAILTO in sent["argv"]


# ---- the real alerts, built by their own modules and sent by send_alert ------------------

@pytest.mark.parametrize("kind", ["missing", "unreadable", "loose"])
def test_the_list_alert_carries_its_kind(rig, kind):
    listing = channels.Listing(pathlib.Path("/x/channels.txt"), refusal="refused", kind=kind)
    subject, body, headers = listalert.build_mail(listing, ["fix it"], profile="", account="a@b",
                                                  pid=1, started="t0", since="t1")
    bot.send_alert(subject, body, headers)
    value, sent = the_alert(rig)
    assert value == f"channels-{kind}"
    assert wc.kinds_sent(sent["argv"]) == []


def test_the_owner_unpinned_alert_carries_it(rig):
    items = [{"file": "callers", "path": "/x/callers.txt", "addr": "owner@example.org",
              "name": "owner", "line": "UNPINNED owner@example.org"}]
    subject, body, headers = ownerkey.build_mail(items, profile="", account="a@b")
    bot.send_alert(subject, body, headers)
    value, sent = the_alert(rig)
    assert value == "owner-unpinned"
    assert wc.kinds_sent(sent["argv"]) == []


def test_the_bot_wires_both_alerts_to_send_alert():
    """listalert and ownerkey reach the seat by bot.send_alert, so its stamp holds."""
    src = wc.source(bot)
    assert "ownerkey.flush(OUTBOX, send_alert," in src
    assert "listalert.ListAlert(OUTBOX, send_alert," in src


# ---- chat and call mail: never ----------------------------------------------------------

def test_a_chat_line_carries_no_alert(rig):
    rig.account.add(10, 12)
    rig.account.arrive(10)
    rig.run()
    (sent,) = rig.sends()
    assert no_alert(sent["argv"]), sent["argv"]


def test_a_chat_line_found_by_the_startup_sweep_carries_no_alert(rig):
    rig.account.add(11, 20)
    rig.account.fresh.append(rig.account.msgs[11])
    bot.sweep(rig.account)
    (sent,) = rig.sends()
    assert no_alert(sent["argv"]), sent["argv"]


def test_a_contact_request_carries_no_alert(rig):
    rig.account.add(30, 50, addr=ch.STRANGER, text="please call me")
    rig.account.arrive(30)
    rig.run()
    (sent,) = rig.sends()
    assert no_alert(sent["argv"]), sent["argv"]


def test_a_reaction_carries_no_alert(rig):
    rig.account.events.append(ch.Obj(kind=bot.EventType.INCOMING_REACTION, chat_id=20, contact_id=1,
                                     msg_id=7, reaction="+1"))
    rig.run()
    (sent,) = rig.sends()
    assert no_alert(sent["argv"]), sent["argv"]


def test_an_app_tap_carries_no_alert(rig):
    rig.account.msgs[900] = ch.FakeMsg(900, 43, ch.CONTACTS[ch.OWNER],
                                       updates=[{"serial": 101, "payload": {"tap": "yes"}, "info": ""}])
    rig.account.events.append(ch.Obj(kind=bot.EventType.WEBXDC_STATUS_UPDATE, msg_id=900,
                                     status_update_serial=101))
    rig.run()
    (sent,) = rig.sends()
    assert no_alert(sent["argv"]), sent["argv"]


@pytest.mark.parametrize("shape", list(wc.CALL_MAILS))
def test_no_delta_call_mail_carries_an_alert(rig, shape):
    assert bot.mail_in("chat12", 12, "call", "(on the call) hi", **wc.CALL_MAILS[shape]) is True
    (sent,) = rig.sends()
    assert no_alert(sent["argv"]), sent["argv"]


def test_the_late_speech_failure_carries_no_alert(rig, monkeypatch):
    """speech_sweep's NOT-spoken line is a call mail, not an alert: it wakes."""
    failed = concurrent.futures.Future()
    failed.set_exception(RuntimeError("no ffmpeg"))
    monkeypatch.setattr(bot, "_SPEECH_PENDING", [([failed], 12, 77, float("inf"))])
    bot.speech_sweep()
    (sent,) = rig.sends()
    assert no_alert(sent["argv"]), sent["argv"]


def test_a_web_call_turn_carries_no_alert(monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_HOST", "host")
    seat = turns.seat_for("alice@example.net", "example.net", environ={})
    mail = turns.turn_mail(1918213200, 1, "push it", seat, "Ada")
    assert [k for k, _ in mail.headers if k.lower() == ALERT.lower()] == []
    assert no_alert(mail.args()), mail.args()


@pytest.mark.parametrize("module", [calls, turns], ids=["calls.py", "web/turns.py"])
def test_the_call_paths_never_name_the_alert_header(module):
    """A source guard: no call path can stamp the header, even by passing it to mail_in."""
    assert ALERT.lower() not in wc.source(module).lower()


def test_bot_names_the_alert_header_only_in_its_constant():
    """bot.py's chat builders (mail_in and its callers) never pass the header:
    the one place bot.py names it is ALERT_HEADER, which alert_headers stamps for send_alert."""
    src = wc.source(bot)
    assert src.lower().count(ALERT.lower()) == 1 and f'ALERT_HEADER = "{ALERT}"' in src


def test_the_alert_header_name_is_exact():
    assert bot.ALERT_HEADER == ALERT
    assert bot.alert_headers({}) == {ALERT: "bot"}


"""PHONE-334 fix-001 (PA): X-Delta-Chat-Kind is a CONTRACT with sessionmail's pager.

sessionmail SM-1047 gates a cold seat's wake on this header: phone mail always
wakes the seat. If the header is renamed, re-cased or dropped on any path, Mike
rings and the seat stays deaf. So the name and its five values are written here
as LITERALS, never read back from bot.KINDS, and each kind is pinned on the mail
a REAL builder makes:

    message   a chat line (handle_events, and the startup sweep)
    request   a stranger's held contact request
    reaction  a reaction
    webxdc    an app tap, as mail
    call      every call mail: the Delta call's mails (M5, by mail_in), the late
              speech-failure line (speech_sweep), and a web call's turn
              (web/turns.py turn_mail)

The header is checked as the exact `--header` argument `message send` is given,
so a change of case or spelling fails here even where a parsed dict would not.
And a guard reads every mail_in call in bot.py and calls.py: each one must
carry a contract kind, or be an M5 call mail (which mail_in stamps `call`), so
a NEW path that forgets its kind fails here too.

Fakes only, as tests/test_contract_headers.py (whose rig this borrows): no
account, rpc-server, sendmail, message or seat key.
"""
import ast
import concurrent.futures
import pathlib

import pytest
import test_contract_headers as ch

from sessionphone.bridge import bot, calls
from sessionphone.web import turns

HEADER = "X-Delta-Chat-Kind"                                    # SM-1047's pager keys on it
CONTRACT_KINDS = ("message", "request", "reaction", "webxdc", "call")
M5_IDS = ("msgid", "in_reply_to", "supersedes")                 # mail_in: any of them makes an M5

rig = ch.rig                                                    # the fake account and `message`


def kinds_sent(argv):
    """Every X-Delta-Chat-Kind value in one `message send` argv, by the exact
    header name: `--header 'X-Delta-Chat-Kind: <kind>'`."""
    return [argv[i + 1][len(HEADER) + 2:] for i, a in enumerate(argv[:-1])
            if a == "--header" and argv[i + 1].startswith(f"{HEADER}: ")]


def the_kind(rig):
    """The one mail sent, and the one kind it carries."""
    sends = rig.sends()
    assert len(sends) == 1, sends
    found = kinds_sent(sends[0]["argv"])
    assert len(found) == 1, sends[0]["argv"]                   # present, exactly once
    return found[0]


# ---- the pin: the name and the values -------------------------------------------------

def test_the_contract_kinds_are_exactly_these_five():
    """Adding a kind is a contract change: tell sessionmail's pager, then edit both."""
    assert bot.KINDS == CONTRACT_KINDS
    assert set(bot.MSG_ID_KINDS) < set(CONTRACT_KINDS)


@pytest.mark.parametrize("kind", CONTRACT_KINDS)
def test_contract_headers_stamps_each_kind_under_the_exact_name(kind):
    assert bot.contract_headers(kind)[HEADER] == kind
    assert [k for k in bot.contract_headers(kind) if k.lower() == HEADER.lower()] == [HEADER]


# ---- each kind, on the mail its real builder sends -----------------------------------

def test_a_chat_line_wakes_as_message(rig):
    rig.account.add(10, 12)                                     # the listed group `family`
    rig.account.arrive(10)
    rig.run()
    assert the_kind(rig) == "message"


def test_a_chat_line_found_by_the_startup_sweep_wakes_as_message(rig):
    rig.account.add(11, 20)
    rig.account.fresh.append(rig.account.msgs[11])
    bot.sweep(rig.account)
    assert the_kind(rig) == "message"


def test_a_contact_request_wakes_as_request(rig):
    rig.account.add(30, 50, addr=ch.STRANGER, text="please call me")
    rig.account.arrive(30)
    rig.run()
    assert the_kind(rig) == "request"


def test_a_reaction_wakes_as_reaction(rig):
    rig.account.events.append(ch.Obj(kind=bot.EventType.INCOMING_REACTION, chat_id=20, contact_id=1,
                                     msg_id=7, reaction="+1"))
    rig.run()
    assert the_kind(rig) == "reaction"


def test_an_app_tap_wakes_as_webxdc(rig):
    rig.account.msgs[900] = ch.FakeMsg(900, 43, ch.CONTACTS[ch.OWNER],
                                       updates=[{"serial": 101, "payload": {"tap": "yes"}, "info": ""}])
    rig.account.events.append(ch.Obj(kind=bot.EventType.WEBXDC_STATUS_UPDATE, msg_id=900,
                                     status_update_serial=101))
    rig.run()
    assert the_kind(rig) == "webxdc"


# the Delta call's mails, as calls.py hands them to mail_in (_hooks.mail_in): the
# keyword shapes below are those of its call sites. None passes kind=: an M5 is
# stamped `call` by mail_in itself. The guard further down holds calls.py to that.
CALL_MAILS = {
    "ringing": {"msgid": "ring-call-12-1@host", "in_reply_to": "call-12-1@host",
                "subject": "RINGING Alice"},
    "missed": {"in_reply_to": "ring-call-12-1@host", "subject": "MISSED Alice: no answer at 30s"},
    "connected (the call root)": {"msgid": "call-12-1@host", "subject": "CALL 13:00 with Alice",
                                  "reply_to": "ada-chat@example.net", "call": 7},
    "a turn": {"msgid": "call-turn-12-1@host", "in_reply_to": "call-12-1@host", "subject": "Alice: hi",
               "reply_to": "ada-chat@example.net", "supersedes": "prev-1@host",
               "headers": {"Medium": "voice"}},
    "a preview": {"msgid": "call-prev-12-1@host", "in_reply_to": "call-12-1@host",
                  "subject": "Alice (preview): hi", "supersedes": "prev-0@host",
                  "headers": {"X-Call-Preview": "1.1"}},
    "the transcript": {"in_reply_to": "call-12-1@host", "subject": "TRANSCRIPT 3 lines, 2 min"},
}


@pytest.mark.parametrize("shape", list(CALL_MAILS))
def test_each_delta_call_mail_wakes_as_call(rig, shape):
    assert bot.mail_in("chat12", 12, "call", "(on the call) hi", **CALL_MAILS[shape]) is True
    assert the_kind(rig) == "call"


def test_a_late_speech_failure_wakes_as_call(rig, monkeypatch):
    """speech_sweep(): a dc-say line that failed after its ack is mailed, not as an M5."""
    failed = concurrent.futures.Future()
    failed.set_exception(RuntimeError("no ffmpeg"))
    monkeypatch.setattr(bot, "_SPEECH_PENDING", [([failed], 12, 77, float("inf"))])
    bot.speech_sweep()
    assert the_kind(rig) == "call"


def test_a_web_call_turn_wakes_as_call(monkeypatch):
    """web/turns.py turn_mail: the headers it carries, and the argv it sends them by."""
    monkeypatch.setenv("SESSIONMAIL_HOST", "host")             # seatmail.id_host(): no mail.json read
    seat = turns.seat_for("alice@example.net", "example.net", environ={})
    mail = turns.turn_mail(1918213200, 1, "push it", seat, "Ada")
    assert [v for k, v in mail.headers if k == HEADER] == ["call"]
    assert kinds_sent(mail.args()) == ["call"]


# ---- the guard: every mail_in call site carries a kind, or is an M5 --------------------

def mail_in_sites(src):
    """(line, kind) for each mail_in(...) call in the source SRC: kind is the
    literal kind= it passes, or "call" for an M5 that passes none (mail_in's own
    rule), or None: a mail that would go out with NO X-Delta-Chat-Kind."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name != "mail_in":
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if "kind" in kw:
            v = kw["kind"]
            kind = v.value if isinstance(v, ast.Constant) else f"<not a literal: {ast.unparse(v)}>"
        else:
            kind = "call" if any(k in kw for k in M5_IDS) else None
        out.append((node.lineno, kind))
    return out


def source(module):
    return pathlib.Path(module.__file__).read_text()


def test_every_bot_mail_in_carries_a_contract_kind():
    sites = mail_in_sites(source(bot))
    assert sites, "no mail_in call found in bot.py: the guard reads nothing"
    stray = [(line, kind) for line, kind in sites if kind not in CONTRACT_KINDS]
    assert stray == [], f"bot.py mail_in with no contract kind (line, kind): {stray}"
    # all five kinds are made in bot.py (a call's M5 mails are made in calls.py)
    assert {kind for _, kind in sites} >= {"message", "request", "reaction", "webxdc", "call"}


def test_every_calls_mail_in_is_a_call_mail():
    sites = mail_in_sites(source(calls))
    assert sites, "no mail_in call found in calls.py: the guard reads nothing"
    stray = [(line, kind) for line, kind in sites if kind != "call"]
    assert stray == [], f"calls.py mail_in that would not carry kind call (line, kind): {stray}"


def test_the_guard_sees_a_mail_with_no_kind():
    """The guard's own check: a mail_in with no kind and no M5 id is caught."""
    src = ("mail_in('c', 1, 's', 't', subject='x')\n"
           "h.mail_in('c', 1, 's', 't', msgid='m')\n"
           "mail_in('c', 1, 's', 't', kind=k)\n")
    assert mail_in_sites(src) == [(1, None), (2, "call"), (3, "<not a literal: k>")]

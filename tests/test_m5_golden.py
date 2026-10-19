"""PHONE-138 T9, PHONE-72 part 2b: the Delta call mails (M5) lose nothing in the move.

tests/golden/delta_m5.json holds each case's mail as bot.py sent it by RAW
SENDMAIL, before the move (sessionphone 6afe665): the raw mail and the
sendmail argv. Since part 2b the bot sends it by `message send
--message-id`, signed, and sessionmail renders the bytes. So the golden is no
longer compared byte for byte: it is the reference the new argv must carry,
header by header, each by the flag message send owns for it (To, -s, -c,
--message-id, -r for In-Reply-To and References, --supersedes, --header for
the rest), the From's display name and seat by the child's environment, and
the body unchanged. The golden is history: never regenerate it.
"""
import json
import pathlib

import pytest

from sessionphone.bridge import bot, dc_env

GOLDEN = pathlib.Path(__file__).parent / "golden" / "delta_m5.json"
ADDR, MAILTO, HOST = "ada-chat@example.net", "ada@example.net", "example.net"

# Each case is mail_in's keyword arguments, as calls.py passes them, and the profile.
CASES = {
    # CallSession._mail_turn (calls.py): one finished turn, with guests and a preview
    "turn": {
        "profile": "",
        "args": ("Team", 12, "Bob", "(on the call) push the branch now, then ring me"),
        "kw": {"msgid": f"call-turn-12-1918203200000@{HOST}", "in_reply_to": f"call-12-1918203190@{HOST}",
               "subject": "Bob: push the branch now, then ring me", "reply_to": ADDR,
               "cc": ["guest@relay.example", "bob@relay.example"], "supersedes": f"prev-12-1@{HOST}",
               "headers": {"Medium": "voice"}, "call": 4242},
    },
    # the same on a second profile, no guests, no preview
    "turn-profile": {
        "profile": "public",
        "args": ("Family group chat", 43, "Bob", "(on the call) yes"),
        "kw": {"msgid": f"call-turn-p-43-1918203200001@{HOST}", "in_reply_to": f"call-p-43-1918203190@{HOST}",
               "subject": "Bob: yes", "reply_to": ADDR, "cc": [], "supersedes": None,
               "headers": {"Medium": "voice"}, "call": 77},
    },
    # a call's root: a Message-ID of its own, no thread
    "root": {
        "profile": "",
        "args": ("Team", 12, "call", "RINGING"),
        "kw": {"msgid": f"call-12-1918203190@{HOST}", "subject": "RINGING Bob", "reply_to": ADDR, "call": 4242},
    },
    # a long, non-ASCII, shell-ish turn: the subject is flattened, cleaned and cut
    "turn-long": {
        "profile": "",
        "args": ("Team", 12, "Bob", "(on the call) Ça va? run `make test` and $(echo hi)\nthen " + "x" * 80),
        "kw": {"msgid": f"call-turn-12-1918203200002@{HOST}", "in_reply_to": f"call-12-1918203190@{HOST}",
               "subject": "Bob: Ça va? run `make test` and $(echo hi)\nthen", "reply_to": ADDR,
               "headers": {"Medium": "voice"}, "call": 4242},
    },
}


def run_case(monkeypatch, fake_message, case):
    """mail_in on the M5 path, into the fake `message` -> its one recorded call."""
    monkeypatch.setattr(dc_env, "ADDR", ADDR)
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Ada")
    monkeypatch.setattr(dc_env, "MAILTO", MAILTO)
    monkeypatch.setattr(dc_env, "HOST", HOST)
    monkeypatch.setattr(dc_env, "SAY", "dc-say")
    monkeypatch.setenv("DC_OUTBOX", "/srv/sp/public/outbox")
    monkeypatch.setattr(bot, "PROFILE", case["profile"])
    monkeypatch.setattr(bot, "log", lambda m: None)
    assert bot.mail_in(*case["args"], **case["kw"]) is True
    (call,) = fake_message.calls()
    return call


def test_the_golden_was_taken():
    assert GOLDEN.exists() and set(json.loads(GOLDEN.read_text())) == set(CASES)


def wanted(raw):
    """The golden's headers, in order, and its body."""
    head, body = raw.split("\n\n", 1)
    return [tuple(line.split(": ", 1)) for line in head.splitlines()], body


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_delta_m5_mail_carries_every_header_the_sendmail_one_did(monkeypatch, fake_message, name):
    call = run_case(monkeypatch, fake_message, CASES[name])
    old = json.loads(GOLDEN.read_text())[name]
    assert old["argv"] == ["-f", ADDR, "-t"]                  # it WAS raw sendmail
    heads, body = wanted(old["raw"])
    a, env = call["argv"], call["env"]
    assert a[:2] == ["send", MAILTO] and call["body"] == body
    flag = lambda f: [a[i + 1] for i, x in enumerate(a) if x == f]
    headers = [tuple(v.split(": ", 1)) for v in flag("--header")]
    for k, v in heads:
        if k == "From":     # the name and the seat; the domain is sessionmail's now (ruled, 2b)
            name, _, addr = v.partition(" <")
            assert env["SESSIONMAIL_FROM_NAME"] == name and env["EMAIL"] == addr.split("@")[0]
        elif k == "To":
            assert a[1] == v
        elif k == "Subject":
            assert flag("-s") == [v]
        elif k == "Cc":
            assert flag("-c") == v.split(", ")
        elif k == "Message-ID":
            assert [f"<{x}>" for x in flag("--message-id")] == [v]
        elif k in ("In-Reply-To", "References"):
            assert flag("-r") == [v]
        elif k == "Supersedes":
            assert flag("--supersedes") == [v]
        elif k in ("MIME-Version", "Content-Type"):
            continue                                          # message send's own, with --raw
        else:
            assert (k, v) in headers, k
    # and nothing more: every --header was in the golden, in the golden's order
    assert headers == [(k, v) for k, v in heads if (k, v) in headers]
    assert len(headers) == len([k for k, _ in heads if k.startswith("X-") or k in ("Reply-To", "Medium")])
    for owned in ("Message-ID", "In-Reply-To", "References", "Cc", "Supersedes"):
        assert owned not in dict(headers), owned               # never a raw header (sm _send.OWNED)

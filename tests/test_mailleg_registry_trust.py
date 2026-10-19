"""PHONE-156, option B: a guest without a pinned key may be known by sessionmail's
registry (`message verify --trust --require-blessing`).

The ladder (design.md a, the super's PASS 2030-06-09), per mail, top down:

1. Own mail: never spoken (test_mailleg_own_from.py).
2. A From with ANY pinned row: option A only. A mismatch is unknown, and the
   registry is never asked.
3. No pin, but on an address-only guest row or a key of voices.json: known
   only when the verify exits 0, its LAST line is exactly `TRUST: FULL`, it
   names a signer key, and ONE registry-cert line reads ok and names the
   From's whole local part (super condition 1: first-and-only).
4. Anyone else: unknown, no verifier run.

Every outcome of rung 3 logs exactly one named line (design.md c, d). The
`message` here is the stand-in from test_mailleg_sender_trust.py, which
records its argv and environment; no real registry, no real verifier.
The groups below are design.md f's, numbered as there.
"""
import json

import pytest
import test_mailleg_sender_trust as option_a

from sessionphone.bridge import calls, dc_env

# option A's rig, shared: the scratch mail leg and the stand-in `message`
leg, verifier = option_a.leg, option_a.verifier
GUEST_VOICE, announced_unknown, keypair, known = (
    option_a.GUEST_VOICE, option_a.announced_unknown, option_a.keypair, option_a.known)

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureRegistrySeatKey"
TRUST_ARGS = "verify --trust --require-blessing"


def full(agent="alice", key=KEY, cert=None, trust="TRUST: FULL"):
    """What `message verify --trust` prints for a FULL mail (as measured,
    design.md 0). CERT replaces the registry-cert line(s); a list for several."""
    if cert is None:
        cert = [f"registry-cert     : ✅ ok  7d236000 → agent: {agent}"]
    return ("signature-crypto  : ✅ good",
            "principal-binding : ✅ good",
            *([f"signer-key        : {key}"] if key else []),
            "sender-session-id : 7d236000-4105-6b46-8c79-6fcf44a9398f",
            "covers            : v2 h=from:to:cc:subject:date:message-id (12 headers) + 1 part: text/plain",
            "part 1            : ✅ text/plain",
            *cert,
            "bless-sig         : ✅ alice@bless verifies over cert bytes",
            trust)


@pytest.fixture
def logs(leg, monkeypatch):
    """The bridge log, from here on; the OFF banner not yet said this run."""
    out = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(out.append, None, None))
    monkeypatch.setattr(calls, "_registry_off_said", False)
    return out


def leg_lines(logs):
    """The mail leg's trust lines: every `CALL mail leg: ...` line."""
    return [m for m in logs if m.startswith("CALL mail leg:")]


def said(addr, why):
    return f"CALL mail leg: {addr}: {why}"


ALICE = "alice@example.org"


# ---- 1. a pinned row is option A only ---------------------------------------

def test_1_a_pinned_row_with_a_matching_signer_is_known_by_plain_verify(leg, verifier, logs):
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"{ALICE} {alice}\n")
    verifier.signs(alice)
    f = leg.drop(f"Alice <{ALICE}>")
    assert known(leg.spoken[0]) and leg.spoken[0][1] == GUEST_VOICE
    assert verifier.runs() == [f"verify {f}"]           # never --trust


def test_1_a_pinned_row_that_does_not_match_never_falls_back_to_the_registry(leg, verifier, logs):
    """The verifier would say FULL for this seat, but the From has a pin and
    the signer is not it: unknown, and `--trust` is never run."""
    alice, other = keypair(leg.tmp, "alice"), keypair(leg.tmp, "other")
    leg.guests.write_text(f"{ALICE} {alice}\n")
    verifier.trust(*full("alice", key=other), rc_=0)
    f = leg.drop(f"Alice <{ALICE}>", extra="X-Voice: af_sky\n")
    assert announced_unknown(leg.spoken[0]), leg.spoken
    assert verifier.runs() == [f"verify {f}"]
    assert not any("--trust" in r for r in verifier.runs())
    assert leg_lines(logs) == [(f"CALL mail leg: {ALICE} has a pinned key but the signer is not "
                                "pinned - unknown (a pin never falls back to the registry)")]


def test_1_an_address_with_one_pinned_row_and_one_bare_row_is_option_a_only(leg, verifier, logs):
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"{ALICE}\n{ALICE} {alice}\n")
    verifier.trust(*full("alice"), rc_=0)                # FULL, but signed by KEY, not the pin
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert not any("--trust" in r for r in verifier.runs())


# ---- 2. an address-only row, FULL, the seat is the From: known --------------

def test_2_an_address_only_row_on_full_is_known_and_its_x_voice_is_honoured(leg, verifier, logs):
    verifier.trust(*full("alice"), rc_=0)
    f = leg.drop(f"Alice <{ALICE}>", extra="X-Voice: am_echo\n")
    assert leg.spoken == [("Alice", "am_echo", "Hello the call.")]
    assert leg.posted == [(12, "Alice: Hello the call.")]   # no (unverified sender)
    assert verifier.runs() == [f"{TRUST_ARGS} {f}"]
    assert leg_lines(logs) == [said(ALICE, "known by registry (FULL, seat alice)")]


def test_2_without_x_voice_a_registry_known_guest_gets_her_voices_json_voice(leg, verifier, logs):
    verifier.trust(*full("alice"), rc_=0)
    leg.drop(f"Alice <{ALICE}>")
    assert leg.spoken == [("Alice", GUEST_VOICE, "Hello the call.")]


def test_2_the_seat_match_ignores_case(leg, verifier, logs):
    leg.guests.write_text("Alice@Example.ORG\n")
    verifier.trust(*full("Alice"), rc_=0)
    leg.drop("Alice <ALICE@example.org>")
    assert known(leg.spoken[0])


# ---- 3. eligibility ---------------------------------------------------------

def test_3_a_voices_json_key_alone_makes_a_from_eligible(leg, verifier, logs):
    """Not on the guest list, but `alice` is in voices.json: checked, and known on FULL."""
    leg.guests.write_text("")
    verifier.trust(*full("alice"), rc_=0)
    f = leg.drop("Alice <alice@elsewhere.example>")
    assert known(leg.spoken[0]) and leg.spoken[0][1] == GUEST_VOICE
    assert verifier.runs() == [f"{TRUST_ARGS} {f}"]


def test_3_a_from_on_neither_list_runs_no_verifier(leg, verifier, logs):
    verifier.trust(*full("sam"), rc_=0)
    leg.drop("Sam <sam@elsewhere.example>")
    assert announced_unknown(leg.spoken[0])
    assert verifier.runs() == [] and leg_lines(logs) == []


def test_3_the_agents_own_default_voice_entry_does_not_make_a_from_eligible(leg, verifier, logs):
    """DEFAULT_VOICE_MAP is the agent's own entry, not a guest's: a From of the
    agent's bare local part (not the bot's `-chat` seat) is not checked."""
    (agent,) = calls.DEFAULT_VOICE_MAP
    verifier.trust(*full(agent), rc_=0)
    leg.drop(f"Agent <{agent}@example.org>")
    assert announced_unknown(leg.spoken[0]) and verifier.runs() == []


def test_3_the_guest_list_match_is_by_the_whole_address(leg, verifier, logs):
    """alice@example.org is listed; alice@forger.example is eligible only through
    voices.json, so with an empty voices.json it is not checked."""
    leg.tmp.joinpath("voices.json").write_text(json.dumps({}))
    verifier.trust(*full("alice"), rc_=0)
    leg.drop("Alice <alice@forger.example>")
    assert announced_unknown(leg.spoken[0]) and verifier.runs() == []


# ---- 4. every verdict but FULL is unknown, with its line --------------------

@pytest.mark.parametrize("trust, rc, why", [
    ("TRUST: UNBLESSED reason=bless-absent", 4,
     "registry trust UNBLESSED (reason=bless-absent) - unknown; only a blessed cert (FULL) counts"),
    ("TRUST: FAIL leg=3-bless reason=bless-absent", 1,
     "registry trust FAIL (leg=3-bless reason=bless-absent) - unknown"),
    ("TRUST: FAIL leg=2-registry reason=agent-mismatch", 1,
     "registry trust FAIL (leg=2-registry reason=agent-mismatch) - unknown"),
    ("TRUST: FAIL leg=2-registry reason=no-cert", 1,
     "registry trust FAIL (leg=2-registry reason=no-cert) - unknown"),
    ("TRUST: FAIL leg=1-signature reason=bad-signature", 1,
     "registry trust FAIL (leg=1-signature reason=bad-signature) - unknown"),
])
def test_4_a_verdict_other_than_full_is_unknown(leg, verifier, logs, trust, rc, why):
    verifier.trust("signature-crypto  : ✅ good", f"signer-key        : {KEY}", trust, rc_=rc)
    f = leg.drop(f"Alice <{ALICE}>", extra="X-Voice: am_echo\n")
    assert announced_unknown(leg.spoken[0]), leg.spoken
    assert leg.posted == [(12, f"Alice <{ALICE}> (unverified sender): Hello the call.")]
    assert verifier.runs() == [f"{TRUST_ARGS} {f}"]
    assert leg_lines(logs) == [said(ALICE, why)]


# ---- 5. no registry: fail closed per mail, the OFF banner once --------------

def test_5_no_registry_is_unknown_every_mail_and_the_off_banner_is_said_once(leg, verifier, logs):
    verifier.trust("TRUST: ERROR reason=registry-absent", rc_=5)
    leg.drop(f"Alice <{ALICE}>")
    leg.drop(f"Alice <{ALICE}>")
    assert all(announced_unknown(s) for s in leg.spoken) and len(leg.spoken) == 2
    per_mail = said(ALICE, "no sessionmail registry here (TRUST: ERROR reason=registry-absent) "
                                "- unknown; only a pinned key in call-guests.txt makes a sender "
                                "known on this host")
    banner = ('CALL mail leg: registry trust is OFF on this host: message verify --trust found no '
              f'registry (set SESSION_REGISTRY, or "registry" in {calls.MAIL_CONF}). '
              'Rows without a pinned key are never known.')
    assert leg_lines(logs) == [per_mail, banner, per_mail]


def test_5_another_error_reason_is_unknown_with_its_reason_and_no_banner(leg, verifier, logs):
    verifier.trust("TRUST: ERROR reason=allowed-signers-absent", rc_=5)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "message verify --trust could not run (TRUST: ERROR reason=allowed-signers-absent) "
                    "- unknown; only a pinned key in call-guests.txt makes a sender known on this host")]


# ---- 6. the verifier itself fails: unknown, each with its line --------------

def test_6_no_message_binary(leg, logs, monkeypatch):
    monkeypatch.setenv("DC_MESSAGE", str(leg.tmp / "no-such-message"))
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "no message binary (DC_MESSAGE) to verify with - unknown")]


def test_6_a_verifier_that_hangs(leg, verifier, logs, monkeypatch):
    verifier.exe.write_text("#!/bin/sh\nexec sleep 5\n")
    monkeypatch.setattr(calls, "VERIFY_TIMEOUT_S", 0.3)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "message verify --trust timed out after 0.3 s - unknown")]


def test_6_a_verifier_that_cannot_be_run(leg, verifier, logs):
    verifier.exe.write_text("#!/no/such/interpreter\n")
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    (line,) = leg_lines(logs)
    assert line.startswith(said(ALICE, "message verify --trust failed (")), line
    assert line.endswith(") - unknown"), line


def test_6_an_old_message_without_trust_gives_no_trust_line(leg, verifier, logs):
    """click's usage error is exit 2, on stderr; exit 2 also means "unsigned"
    to a plain verify, so only the missing TRUST line can tell."""
    verifier.exe.write_text("#!/bin/sh\necho \"Error: No such option: --trust\" >&2\nexit 2\n")
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "message verify gave no TRUST line (rc 2; too old for --trust?) - unknown")]


def test_6_trust_full_with_a_nonzero_rc_is_a_disagreement(leg, verifier, logs):
    verifier.trust(*full("alice"), rc_=1)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "message verify said 'TRUST: FULL' with rc 1 - unknown")]


@pytest.mark.parametrize("trust, rc", [
    ("TRUST: FAIL leg=1-signature reason=unsigned", 0),     # a FAIL that exits 0
    ("TRUST: FULL reason=extra", 0),                        # not exactly `TRUST: FULL`
    ("TRUST: MAYBE", 0),                                    # no such verdict
])
def test_6_any_other_line_and_rc_disagreement_is_unknown(leg, verifier, logs, trust, rc):
    verifier.trust(*full("alice", trust=trust), rc_=rc)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, f"message verify said '{trust}' with rc {rc} - unknown")]


def test_6_rc_0_whose_last_line_is_not_trust(leg, verifier, logs):
    """A TRUST: FULL line that is not the LAST line does not count."""
    verifier.trust(*full("alice"), "something after the verdict", rc_=0)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "message verify gave no TRUST line (rc 0; too old for --trust?) - unknown")]


def test_6_full_without_a_signer_key_line_is_unknown(leg, verifier, logs):
    verifier.trust(*full("alice", key=None), rc_=0)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "TRUST: FULL but no signer-key line - unknown")]


# ---- 7. FULL is not enough: the seat must be the From's WHOLE local part ----

def test_7_a_project_cert_does_not_vouch_for_a_plus_seat_of_that_project(leg, verifier, logs):
    """On master, --trust compares the cert's agent with the local part CUT at
    `+`: a blessed `sessionphone` cert passes FULL for any
    `sessionphone+<anything>`. The bot's own check keeps the extension."""
    leg.tmp.joinpath("voices.json").write_text(json.dumps({"sessionphone": "am_echo"}))
    verifier.trust(*full("sessionphone"), rc_=0)
    leg.drop("Super <sessionphone+phone-999.super@x.example>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said("sessionphone+phone-999.super@x.example",
        "registry seat 'sessionphone' is not the From 'sessionphone+phone-999.super' - unknown")]


def test_7_full_with_no_registry_cert_line_is_unknown(leg, verifier, logs):
    verifier.trust(*full(cert=[]), rc_=0)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "TRUST: FULL but no registry-cert agent line to match - unknown")]


def test_7_an_ok_registry_cert_line_with_no_agent_is_unknown(leg, verifier, logs):
    verifier.trust(*full(cert=["registry-cert     : ✅ ok  7d236000"]), rc_=0)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "TRUST: FULL but no registry-cert agent line to match - unknown")]


# ---- super condition 1: the agent from ONE ok registry-cert line only -------

def test_c1_a_registry_cert_line_that_is_not_ok_is_unknown_even_under_full(leg, verifier, logs):
    verifier.trust(*full(cert=["registry-cert     : ❌ cert-invalid 7d236000 → agent: alice"]), rc_=0)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "TRUST: FULL but the registry-cert line is not ok - unknown")]


@pytest.mark.parametrize("agents", [("alice", "mallory"), ("mallory", "alice"), ("alice", "alice")])
def test_c1_two_registry_cert_lines_are_unknown_first_and_only_not_first_match(leg, verifier, logs, agents):
    cert = [f"registry-cert     : ✅ ok  7d236000 → agent: {a}" for a in agents]
    verifier.trust(*full(cert=cert), rc_=0)
    leg.drop(f"Alice <{ALICE}>")
    assert announced_unknown(leg.spoken[0])
    assert leg_lines(logs) == [said(ALICE, "TRUST: FULL but 2 registry-cert lines, not one - unknown")]


# ---- 8. SESSIONMAIL_MHS_ORACLE never reaches a verify child -----------------

def test_8_the_oracle_is_dropped_and_the_registry_settings_pass_through(leg, verifier, logs, monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_MHS_ORACLE", "/bin/true")
    monkeypatch.setenv("SESSION_REGISTRY", str(leg.tmp / "registry"))
    verifier.trust(*full("alice"), rc_=0)
    leg.drop(f"Alice <{ALICE}>")
    env = verifier.env()
    assert "SESSIONMAIL_MHS_ORACLE" not in env
    assert env["SESSION_REGISTRY"] == str(leg.tmp / "registry")


def test_8_the_oracle_is_dropped_from_the_plain_verify_too(leg, verifier, logs, monkeypatch):
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"{ALICE} {alice}\n")
    monkeypatch.setenv("SESSIONMAIL_MHS_ORACLE", "/bin/true")
    verifier.signs(alice)
    leg.drop(f"Alice <{ALICE}>")
    assert "SESSIONMAIL_MHS_ORACLE" not in verifier.env()


# ---- 9. the CALL CONNECTED mail names the registry rung ---------------------

def test_9_call_connected_tells_guests_about_the_registry_rung():
    s = object.__new__(calls.CallSession)
    s.chat_id, s.caller, s.msg_id, s.root_id = 12, "Sam", 5531, "call-test@example.org"
    text = " ".join(s._connected_text("12:34", "rang Bob").split())
    assert "signed by a key pinned on your row of call-guests.txt" in text
    assert "message verify --trust" in text and "FULL" in text
    assert "blessed by the owner" in text and "your whole From local part" in text
    assert "A host with no registry knows no one this way" in text
    assert "plus-address seat is not yet known by the registry." in text
    assert dc_env.ADDR in text

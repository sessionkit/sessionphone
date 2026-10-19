"""PHONE-155 repro (SK-80 P1): the mail leg trusts headers that any sender writes.

During a live call MailLeg.one() speaks mail into the call. Nothing checks a
signature or the sender, so three things a sender merely WRITES decide what the
call hears:

1. X-Voice. voice_for() takes the header first and counts the mail as known:
   any sender picks any voice (the agent's own included) and skips the
   "who I do not have a voice for" announcement.
2. The voice map. An unsigned mail whose From local part is in voices.json, or
   is the agent's own DEFAULT_VOICE_MAP entry, is counted known. A From line is
   not an identity.
3. The root. The callchat-patch speaks a mail whose References carry ANY
   `call-[profile-]<live chat id>-<digits>@` string, so a root the bridge never
   issued reaches the live call.

The first three tests were the repro (strict xfails, e5a776e); fix-001 made
them pass and removed the markers. test_rig_speaks_an_unsigned_stranger_as_unknown
is the control that the rig speaks at all.

The fix (the super's ruling, option A and (ii)): KNOWN only when `message
verify` exits 0 AND its signer key is pinned on a call-guests.txt row whose
address is the From; a mail reaches a call only by a root the bridge ISSUED,
matched whole, kept across a turnover of TURNOVER_S and no longer.

Keys: FIXTURE keys only, made by ssh-keygen into tmp_path per test; none is
committed. The `message` here is a stand-in script (the real one is behind the
conftest tripwire) that prints what sessionmail's `message verify` prints.
Fakes only: a scratch maildir, voice map and guest list in tmp_path, a fake
call session, no loop, no TTS, no Delta Chat.
"""
import json
import subprocess
import types

import pytest

from sessionphone.bridge import calls, dc_env

LIVE = "call-12-1893672000@example.org"         # the root the bridge issued for the live call
FORGED = "call-12-4242424242@forger.example"    # chat 12's shape, a root the bridge never issued
GUEST_VOICE = "bf_emma"                         # the guest's voice in voices.json
OWN = calls.DEFAULT_VOICE_MAP                   # {dc_env.AGENT: dc_env.VOICE}, read at import

class Session:
    pc, chat_id, root_id, account = object(), 12, LIVE, None

    async def speak(self, text, voice, who):
        return None


class Manager:
    def __init__(self):
        self.sessions, self.loop = {1: Session()}, None


@pytest.fixture
def leg(tmp_path, monkeypatch):
    box = tmp_path / "box"
    (box / "new").mkdir(parents=True)
    monkeypatch.setattr(calls, "BOT_BOX", box)
    voices = tmp_path / "voices.json"
    voices.write_text(json.dumps({"alice": GUEST_VOICE}))
    monkeypatch.setattr(calls, "VOICE_MAP_FILE", voices)
    guests = tmp_path / "call-guests.txt"
    guests.write_text("alice@example.org\n")      # Alice is on the guest list, by address only
    monkeypatch.setattr(calls, "GUESTS_FILE", guests)
    monkeypatch.setattr(dc_env, "ADDR", f"{dc_env.AGENT}-chat@example.org")
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, None, None))
    monkeypatch.setattr(calls, "reply_seen", lambda *a: None)
    monkeypatch.setattr(calls, "ISSUED", calls.IssuedRoots())   # a restart: nothing issued yet
    spoken, posted = [], []
    monkeypatch.setattr(calls, "post_to_chat",
                        lambda account, chat_id, text, what: posted.append((chat_id, text)) or True)

    def run(coro, loop):
        loc = coro.cr_frame.f_locals
        spoken.append((loc.get("who"), loc.get("voice"), loc.get("text")))
        coro.close()
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", run)

    class Rig:
        n = 0

        def drop(self, frm, ref=LIVE, extra="", body="Hello the call.", refs=None):
            """A mail from FRM replying to REF, through MailLeg.one(). Unsigned
            unless the test's stand-in verifier says otherwise. REFS, if given,
            is the raw References/In-Reply-To value, brackets and all."""
            Rig.n += 1
            f = box / "new" / f"m{Rig.n}"
            refs = f"<{ref}>" if refs is None else refs
            f.write_text(f"From: {frm}\nTo: {dc_env.AGENT}-chat@example.org\nSubject: re: call\n"
                         f"In-Reply-To: {refs}\nReferences: {refs}\n{extra}"
                         "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
                         f"{body}\n")
            calls.MailLeg(Manager()).one(f)
            return f
    r = Rig()
    r.spoken, r.posted, r.guests, r.tmp = spoken, posted, guests, tmp_path
    return r


def announced_unknown(said):
    who, voice, _ = said
    return voice == calls.UNKNOWN_VOICE and "who I do not have a voice for" in who


def test_rig_speaks_an_unsigned_stranger_as_unknown(leg):
    """Control, not a repro: an unsigned stranger on the exact live root is
    spoken, announced unknown, in UNKNOWN_VOICE. True before the fix and after."""
    leg.drop("Sam <sam@elsewhere.example>")
    assert len(leg.spoken) == 1 and announced_unknown(leg.spoken[0]), leg.spoken
    assert len(leg.posted) == 1


def test_x_voice_on_unsigned_mail_is_not_honoured(leg):
    """Defect 1: an unsigned stranger's X-Voice picks the voice and counts it known.

    Sam is in no voice map and on no guest list. He asks for the agent's own
    voice. The call must hear him announced as unknown, in UNKNOWN_VOICE."""
    (agent_voice,) = OWN.values()
    leg.drop("Sam <sam@elsewhere.example>", extra=f"X-Voice: {agent_voice}\n")
    assert len(leg.spoken) == 1, leg.spoken
    who, voice, _ = leg.spoken[0]
    assert announced_unknown(leg.spoken[0]), (
        f"unsigned X-Voice honoured: spoken in {voice!r} as {who!r}, "
        f"not {calls.UNKNOWN_VOICE!r} announced unknown")


def test_unsigned_mail_from_a_mapped_local_part_is_unknown(leg, verifier):
    """Defect 2: an unsigned mail is known by its From local part alone.

    alice@ is in voices.json and on the guest list; the agent's own local part
    is in DEFAULT_VOICE_MAP. Neither mail is signed, so anyone could have
    written either From line: both must be announced unknown, in UNKNOWN_VOICE.
    (Alice's row has no key, so the registry rung checks her mail, PHONE-156:
    unsigned, it FAILs leg 1.)"""
    verifier.trust("TRUST: FAIL leg=1-signature reason=unsigned", rc_=1)
    (agent,) = OWN
    leg.drop("Alice <alice@example.org>")
    leg.drop(f"{dc_env.AGENT_NAME} <{agent}@example.org>")
    assert len(leg.spoken) == 2, leg.spoken
    known = [s for s in leg.spoken if not announced_unknown(s)]
    assert not known, f"unsigned mail counted known by its From line: {known}"


def test_a_forged_root_for_the_live_chat_is_not_spoken(leg):
    """Defect 3: a root the bridge never issued reaches the live call.

    The live call's root is LIVE. FORGED has chat 12's shape and digits the
    bridge never minted. Nothing of it may be spoken or posted to the chat."""
    leg.drop("Sam <sam@elsewhere.example>", ref=FORGED)
    assert leg.spoken == [] and leg.posted == [], (
        f"forged root {FORGED!r} spoken into the live call: {leg.spoken} / {leg.posted}")


# ---- the fix: a pinned key, checked by `message verify` (option A) ----------

def keypair(tmp_path, name):
    """A FIXTURE ed25519 key made here, never committed -> "ssh-ed25519 AAAA..."."""
    key = tmp_path / f"{name}_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{name}-fixture",
                    "-f", str(key)], check=True)
    typ, b64, *_ = (key.with_suffix(".pub")).read_text().split()
    return f"{typ} {b64}"


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    """A stand-in `message`: `message verify FILE` prints what sessionmail's does
    and exits with the code a test sets. Every call is logged."""
    out, rc, calls_log = tmp_path / "verify.out", tmp_path / "verify.rc", tmp_path / "verify.calls"
    env_dump = tmp_path / "verify.env"
    exe = tmp_path / "message"
    exe.write_text('#!/bin/sh\n'
                   f'echo "$*" >> "{calls_log}"\n'
                   f'env > "{env_dump}"\n'
                   '[ "$1" = verify ] || exit 3\n'
                   f'[ -f "{out}" ] && cat "{out}"\n'
                   f'exit "$(cat "{rc}" 2>/dev/null || echo 2)"\n')
    exe.chmod(0o755)
    monkeypatch.setenv("DC_MESSAGE", str(exe))

    class V:
        def signs(self, key, rc_=0):
            """The next mails verify as signed by KEY (exit RC_)."""
            out.write_text("signature-crypto  : ✅ good\n"
                           "principal-binding : ✅ good\n"
                           f"signer-key        : {key}\n"
                           "sender-session-id : 00000000-0000-0000-0000-000000000000\n")
            rc.write_text(f"{rc_}\n")

        def unsigned(self):
            out.write_text("unsigned          : no X-Session-Signature header\n")
            rc.write_text("2\n")

        def trust(self, *lines, rc_):
            """The next mails print LINES (a `message verify --trust` run's
            stdout, the TRUST line last) and exit RC_."""
            out.write_text("".join(f"{l}\n" for l in lines))
            rc.write_text(f"{rc_}\n")

        def runs(self):
            return calls_log.read_text().splitlines() if calls_log.exists() else []

        def env(self):
            """The environment the last run saw, NAME -> value."""
            return dict(l.split("=", 1) for l in env_dump.read_text().splitlines() if "=" in l)

    v = V()
    v.exe = exe
    return v


def known(said):
    who = said[0]
    return "who I do not have a voice for" not in who


def test_signed_by_the_key_pinned_on_the_from_row_is_known(leg, verifier):
    """Signed, and the signer key is on Alice's row: known, in her voices.json
    voice, and the chat post is the plain `<who>: <text>`."""
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"alice@example.org {alice} laptop\n")
    verifier.signs(alice)
    f = leg.drop("Alice <alice@example.org>")
    assert leg.spoken == [("Alice", GUEST_VOICE, "Hello the call.")]
    assert leg.posted == [(12, "Alice: Hello the call.")]
    assert verifier.runs() == [f"verify {f}"]


def test_a_known_senders_x_voice_wins(leg, verifier):
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"alice@example.org {alice}\n")
    verifier.signs(alice)
    leg.drop("Alice <alice@example.org>", extra="X-Voice: am_echo\n")
    assert leg.spoken[0][:2] == ("Alice", "am_echo")


def test_a_known_sender_without_a_voice_is_named_in_the_plain_voice(leg, verifier):
    """Pinned and signed but in no voice map: the plain voice, NOT announced
    as unknown, since the signature is good."""
    pat = keypair(leg.tmp, "pat")
    leg.guests.write_text(f"pat@example.org {pat}\n")
    verifier.signs(pat)
    leg.drop("Pat <pat@example.org>")
    (said,) = leg.spoken
    assert said[1] == calls.UNKNOWN_VOICE and known(said) and said[0] == "Pat"
    assert leg.posted == [(12, "Pat: Hello the call.")]


def test_the_address_match_ignores_case(leg, verifier):
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"Alice@Example.ORG {alice}\n")
    verifier.signs(alice)
    leg.drop("Alice <ALICE@example.org>")
    assert known(leg.spoken[0]) and leg.spoken[0][1] == GUEST_VOICE


def test_an_address_may_pin_several_keys(leg, verifier):
    old, new = keypair(leg.tmp, "alice-old"), keypair(leg.tmp, "alice-new")
    leg.guests.write_text(f"alice@example.org {old}\nalice@example.org {new} phone\n")
    verifier.signs(new)
    leg.drop("Alice <alice@example.org>")
    assert known(leg.spoken[0])


def test_a_good_signature_from_an_unpinned_key_is_unknown(leg, verifier):
    """The verifier checks a mail against its OWN key (sessionmail B1): a good
    signature by a key nobody pinned is not known, X-Voice or no."""
    alice, mallory = keypair(leg.tmp, "alice"), keypair(leg.tmp, "mallory")
    leg.guests.write_text(f"alice@example.org {alice}\n")
    verifier.signs(mallory)
    leg.drop("Alice <alice@example.org>", extra="X-Voice: af_sky\n")
    assert announced_unknown(leg.spoken[0]), leg.spoken
    assert leg.posted == [(12, "Alice <alice@example.org> (unverified sender): Hello the call.")]


def test_a_key_pinned_on_another_guests_row_does_not_vouch_for_this_from(leg, verifier):
    """Pat's key, Pat's good signature, but a From of Alice: not known."""
    alice, pat = keypair(leg.tmp, "alice"), keypair(leg.tmp, "pat")
    leg.guests.write_text(f"alice@example.org {alice}\npat@example.org {pat}\n")
    verifier.signs(pat)
    leg.drop("Alice <alice@example.org>")
    assert announced_unknown(leg.spoken[0]), leg.spoken


def test_a_failing_verify_is_unknown_even_if_it_names_the_pinned_key(leg, verifier):
    """Exit 1 (bad signature) with a signer-key line: the exit code decides."""
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"alice@example.org {alice}\n")
    verifier.signs(alice, rc_=1)
    leg.drop("Alice <alice@example.org>")
    assert announced_unknown(leg.spoken[0]), leg.spoken


def test_an_unsigned_mail_from_a_keyed_row_is_unknown(leg, verifier):
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"alice@example.org {alice}\n")
    verifier.unsigned()
    leg.drop("Alice <alice@example.org>", extra="X-Voice: af_sky\n")
    assert announced_unknown(leg.spoken[0]), leg.spoken


def test_no_verifier_fails_closed(leg, monkeypatch):
    """No `message` binary at all: unknown, and the mail is still spoken."""
    leg.guests.write_text("alice@example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureNotAKey\n")
    monkeypatch.setenv("DC_MESSAGE", str(leg.tmp / "no-such-message"))
    leg.drop("Alice <alice@example.org>")
    assert announced_unknown(leg.spoken[0]), leg.spoken


def test_a_verifier_that_hangs_fails_closed(leg, verifier, monkeypatch):
    alice = keypair(leg.tmp, "alice")
    leg.guests.write_text(f"alice@example.org {alice}\n")
    verifier.exe.write_text("#!/bin/sh\nexec sleep 5\n")
    monkeypatch.setattr(calls, "VERIFY_TIMEOUT_S", 0.3)
    leg.drop("Alice <alice@example.org>")
    assert announced_unknown(leg.spoken[0]), leg.spoken


def test_an_address_only_row_is_not_known_by_a_good_signature_alone(leg, verifier):
    """The rows the repro used: no key column. A good signature is not enough:
    since PHONE-156 such a row goes to the registry rung (`--trust`), and a
    plain verify's output, with no TRUST line, is unknown."""
    verifier.signs("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureNotAKey")
    f = leg.drop("Alice <alice@example.org>")
    assert announced_unknown(leg.spoken[0])
    assert verifier.runs() == [f"verify --trust --require-blessing {f}"]


def test_the_guest_list_parses_old_and_new_rows(leg, monkeypatch):
    logs = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logs.append, None, None))
    k = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureNotAKey"
    leg.guests.write_text("# guests\n"
                          "alice@example.org\n"
                          f"alice@example.org {k} laptop\n"
                          "pat@example.org not-a-key\n"
                          "\n"
                          "sam@example.org\n")
    assert calls.guest_rows() == [("alice@example.org", None), ("alice@example.org", k),
                                  ("pat@example.org", None), ("sam@example.org", None)]
    assert calls.guests() == ["alice@example.org", "pat@example.org", "sam@example.org"]
    assert calls.pinned_keys("ALICE@example.org") == {k}
    assert calls.pinned_keys("pat@example.org") == set()
    assert any("pat@example.org" in m and "never known" in m for m in logs), logs


# ---- the fix: exact roots the bridge issued (ruling (ii)) -------------------

DEAD = "call-12-1893671701@example.org"     # the chat's previous call, which the bridge DID issue


def issue(dead_end, live_start):
    """Chat 12's previous call (DEAD) ended at DEAD_END; the live call was
    minted at LIVE_START."""
    calls.ISSUED.mint(12, DEAD, now=1000)
    calls.ISSUED.end(12, now=dead_end)
    calls.ISSUED.mint(12, LIVE, now=live_start)


def test_a_turnover_inside_the_window_keeps_the_old_root(leg):
    """The 2030-06-09 case: a reply to the root of a call that just ended
    reaches the new call in that chat. 119 s is inside TURNOVER_S."""
    assert calls.TURNOVER_S == 120
    issue(dead_end=1000, live_start=1119)
    leg.drop("Sam <sam@elsewhere.example>", ref=DEAD)
    assert len(leg.spoken) == 1 and len(leg.posted) == 1


def test_a_new_call_after_the_window_does_not_hear_the_old_root(leg):
    issue(dead_end=1000, live_start=1121)
    leg.drop("Sam <sam@elsewhere.example>", ref=DEAD)
    assert leg.spoken == [] and leg.posted == []


def test_a_root_from_before_a_restart_is_not_spoken(leg):
    """The set lives in memory: after a restart nothing old is issued (the
    leg fixture starts each test with an empty set)."""
    leg.drop("Sam <sam@elsewhere.example>", ref=DEAD)
    assert leg.spoken == [] and leg.posted == []


def test_the_live_root_must_match_whole_not_as_a_substring(leg):
    """The old match was `root in refs`: a longer id that CONTAINS the live
    root got through."""
    leg.drop("Sam <sam@elsewhere.example>", refs=f"<x{LIVE}.forger.example>")
    leg.drop("Sam <sam@elsewhere.example>", refs=f"<{LIVE[:-1]}>")
    assert leg.spoken == [] and leg.posted == []


def test_a_bare_live_root_without_brackets_still_matches(leg):
    leg.drop("Sam <sam@elsewhere.example>", refs=LIVE)
    assert len(leg.spoken) == 1


def test_each_chat_keeps_only_its_newest_roots():
    issued = calls.IssuedRoots()
    roots = [f"call-12-{n}@example.org" for n in range(calls.ROOTS_PER_CHAT + 1)]
    for n, r in enumerate(roots):          # one turnover after another
        issued.mint(12, r, now=1000 + 10 * n)
        issued.end(12, now=1000 + 10 * n + 5)
    assert issued.chats({roots[0]}) == set()
    assert issued.chats({roots[1]}) == {12} and issued.chats({roots[-1]}) == {12}
    assert len(issued.by_chat[12]) == calls.ROOTS_PER_CHAT


def test_a_root_of_one_chat_never_reaches_another():
    issued = calls.IssuedRoots()
    issued.mint(12, DEAD)
    issued.mint(99, "call-99-1@example.org")
    assert issued.chats({DEAD}) == {12}


def test_a_call_session_mints_its_root(monkeypatch):
    monkeypatch.setattr(calls, "ISSUED", calls.IssuedRoots())
    s = calls.CallSession(types.SimpleNamespace(sessions={}), None, 12)
    assert calls.ISSUED.chats({s.root_id}) == {12}


def test_only_the_chats_last_call_ending_starts_the_turnover_clock(monkeypatch):
    """A call ending while another call in the chat is live (or ringing) is
    no turnover: the chat is not marked ended."""
    monkeypatch.setattr(calls, "ISSUED", calls.IssuedRoots())

    def sess(pc=None, ringing=False, closed=None, chat_id=12):
        s = object.__new__(calls.CallSession)
        s.chat_id, s.pc, s.ringing, s.closed = chat_id, pc, ringing, closed
        return s
    ending, other = sess(closed="hung up"), sess(pc=object())
    ending.manager = other.manager = types.SimpleNamespace(sessions={1: ending, 2: other})
    ending._roots_end()
    assert 12 not in calls.ISSUED.ended
    other.pc, other.ringing = None, True
    ending._roots_end()
    assert 12 not in calls.ISSUED.ended
    other.ringing, other.closed = False, "declined"
    ending._roots_end()
    assert 12 in calls.ISSUED.ended

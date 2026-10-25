"""Every word heard on a call reaches the agent: a long turn is never cut.

A caller who talks for a long time makes a turn of many thousand characters.
Each test here hears one such turn (15,000 characters, 2,500 numbered words)
and reads what the agent is sent: the turn mail and, if the bridge splits it,
every continuation mail threaded to it (In-Reply-To the turn, or the
continuation before). Joined in order, the words must be exactly the words
heard: none lost, none twice, none out of order. A split is marked in the
text; a plain "..." never stands for words that were not sent.

The real CallSession methods run on a session made with object.__new__, as in
test_noise_turn, and the mail is built by the real bot.mail_in and recorded by
conftest's fake `message`. No call, no network, no sendmail.
"""
import asyncio
import re
import types

import pytest

from sessionphone.bridge import bot, calls, dc_env

BOX = "agent@example.org"
ADDR = "agent-chat@example.org"
ROOT = "call-12-1789@example.org"

WORDS = [f"w{i:04d}" for i in range(2500)]
LONG = " ".join(WORDS)                          # 14,999 characters, one line
WORD = re.compile(r"\bw\d{4}\b")


def test_the_heard_turn_is_long_enough():
    assert len(LONG) >= 10_000 and "..." not in LONG


class _Sink:
    """The heard log: what web turns and the xdc feed read back."""

    def __init__(self):
        self.turns, self.partials = [], []

    def turn(self, text, detector=None, **k):
        self.turns.append(text)

    def partial(self, text, **k):
        self.partials.append(text)


def _session():
    s = object.__new__(calls.CallSession)
    s.chat_id, s.chat_name, s.caller = 12, "Caller", "Caller"
    s.lines, s.preview_id = [], None
    s.root_id = ROOT
    s.msg_id = 4242
    s.guest_list = list
    s.heard = _Sink()
    return s


@pytest.fixture
def rig(monkeypatch, fake_message):
    """calls._hooks with the real bot.mail_in: each mail goes to the fake `message`."""
    r = types.SimpleNamespace(logs=[])
    monkeypatch.setenv("SESSIONMAIL_HOST", "example.org")
    monkeypatch.setattr(dc_env, "MAILTO", BOX)
    monkeypatch.setattr(dc_env, "ADDR", ADDR)
    monkeypatch.setattr(dc_env, "HOST", "example.org")
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setattr(bot, "log", r.logs.append)
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(r.logs.append, lambda p: "", bot.mail_in))
    monkeypatch.setattr(calls, "_call_id", None)
    monkeypatch.setattr(calls, "PREVIEWS", False)
    monkeypatch.setattr(calls, "BARGE_IN", False)
    r.mails = fake_message.mails
    return r


def heard_part(mail) -> str:
    """The heard text a mail carries: its body above the footer."""
    return mail.get_content().split("\n\n---\n", 1)[0]


def thread(mails, first):
    """The mail FIRST and each mail threaded under it (In-Reply-To it, or a
    continuation of it), in the order they were sent."""
    ids, out = set(), []
    for m in mails:
        mid = (m["Message-ID"] or "").strip("<>")
        parent = (m["In-Reply-To"] or "").strip("<>")
        if m is first or parent in ids:
            ids.add(mid)
            out.append(m)
    return out


def assert_whole(mails, what="the turn"):
    """MAILS, joined in order, carry every heard word once, in order, and mark any split."""
    parts = [heard_part(m) for m in mails]
    got = WORD.findall(" ".join(parts))
    missing = len(WORDS) - len(set(got) & set(WORDS))
    assert got == WORDS, (
        f"{what} did not reach the agent whole: {len(got)} of {len(WORDS)} words arrived "
        f"({missing} missing) in {len(mails)} mail(s) of {[len(p) for p in parts]} characters; "
        f"the last ends {parts[-1][-40:]!r}")
    for p in parts:
        assert not p.rstrip().endswith("..."), f"a plain '...' stands for words not sent: {p[-40:]!r}"
    if len(mails) > 1:
        for p in parts[:-1]:
            assert "[continued" in p, f"a split that is not marked: {p[-80:]!r}"


def turn_mails(rig):
    """The turn mail (threaded under the call's root, no preview) and its continuations."""
    mails = rig.mails()
    turns = [m for m in mails if (m["In-Reply-To"] or "").strip("<>") == ROOT
             and m["X-Call-Preview"] is None]
    assert len(turns) == 1, f"one turn mail, not {len(turns)}"
    return thread(mails, turns[0])


# ---- the turn mail: every path into _mail_turn -------------------------------------

def test_a_long_turn_mail_carries_every_word(rig):
    s = _session()
    asyncio.run(s._mail_turn(LONG, detector="turn-join"))
    assert_whole(turn_mails(rig))


def test_a_long_turn_joined_from_chunks_carries_every_word(rig):
    """The turn-join chunker: 25 chunks of 100 words, joined by _turn_out."""
    s = _session()
    s.previews = types.SimpleNamespace(on_turn=lambda text, at: None)
    s.turn_parts = [(i + 1, " ".join(WORDS[i * 100:(i + 1) * 100])) for i in range(25)]
    asyncio.run(s._turn_out())
    assert_whole(turn_mails(rig))


def test_a_long_ears_turn_carries_every_word(rig):
    """DC_EARS=1: the in-process ears leg hands its turn to _mail_turn."""
    s = _session()
    s.ears_futs = []
    s._rec_check = lambda: None
    e = types.SimpleNamespace(type="turn", text=LONG,
                              extra={"reason": "silence", "wait_s": 0.8, "by": "vad"})

    async def run():
        s._ears_dispatch(e)
        await asyncio.gather(*s.ears_futs)
    asyncio.run(run())
    assert_whole(turn_mails(rig))


def test_the_heard_log_keeps_the_whole_turn(rig):
    """The turn the heard log writes is the turn as heard, not a cut copy."""
    s = _session()
    asyncio.run(s._mail_turn(LONG))
    assert WORD.findall(" ".join(s.heard.turns)) == WORDS


# ---- partials: the heard log's chunks -------------------------------------------

def test_a_long_ears_final_is_written_whole_as_a_partial(rig):
    s = _session()
    e = types.SimpleNamespace(type="final", text=LONG, seg=3, start=0.0, end=90.0, t=90.5)
    s._ears_dispatch(e)
    assert WORD.findall(" ".join(s.heard.partials)) == WORDS


def test_a_long_chunk_is_written_whole_as_a_partial_and_kept_for_the_turn(rig, tmp_path, monkeypatch):
    """The turn-join chunker's _heard: one long transcript of one chunk."""
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(rig.logs.append, lambda p: LONG, bot.mail_in))
    s = _session()
    s.turn_pending, s.turn_parts = {1}, []
    s.echo = types.SimpleNamespace(check=lambda text: None)
    s._turn_arm = lambda: None
    chunk = tmp_path / "chunk-1.wav"
    chunk.write_bytes(b"")
    asyncio.run(s._heard(chunk, 1))
    assert WORD.findall(" ".join(s.heard.partials)) == WORDS
    assert WORD.findall(" ".join(t for _, t in s.turn_parts)) == WORDS


# ---- previews: the turn so far, while he is still talking -------------------------

def test_a_long_preview_carries_every_word(rig):
    s = _session()
    p = types.SimpleNamespace(text=LONG, turn=1, seq=4, at=95.0, turn_start=0.0)
    asyncio.run(s._preview_out(p))
    mails = rig.mails()
    (first,) = [m for m in mails if m["X-Call-Preview"] is not None
                and (m["In-Reply-To"] or "").strip("<>") == ROOT]
    assert_whole(thread(mails, first), what="the preview")

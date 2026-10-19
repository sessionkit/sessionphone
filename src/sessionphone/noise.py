"""A call turn with no real word in it is not a turn worth waking for.

A turn that is just a full stop, or music, should not notify anyone: a turn
needs at least one real word. Measured on a call: turns whose whole text was
".", "Music.", "Um" and "Uh." were each mailed as `(on the call) ...` and each
woke the seat.

    no_real_word(text) -> bool    True: drop the turn, never mail it

One rule for both mailing legs: the bridge's CallSession._mail_turn (the
turn-join and ears legs) and the web call's TurnFollower.take. A drop is the
caller's to log, one line, shaped like the ECHO drop.

A one-word ANSWER ("yes", "no", "go", "okay", "5") is a real word and still
mails; so does "Hmm, yes" (two words) and "Mm-hmm" (two: it is a yes).
"""
from __future__ import annotations

import re

# whisper's non-speech annotations, as ears strips them (sessionvoice
# src/ears/listen/stt.py _ANNOTATION): "[BLANK_AUDIO]", "(Music)", "*sighs*".
# The bridge's own transcriber does not strip them, so they are taken out here.
_ANNOTATION = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")

# A word: letters or digits, an inner apostrophe allowed ("don't"). Punctuation
# alone (".", "...", "-") is no word.
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")

# The one-token turns that are noise, not an answer: whisper's "Music." /
# "Applause." captions for a room with no speech, and the hesitations, in the
# spellings whisper gives them ("Um", "Umm", "Uh", "Mm", "Mmm", "Hmm", "Hm").
_NOISE = re.compile(r"music|applause|u+m+|u+h+|m{2,}|h+m+", re.IGNORECASE)


def words(text: str | None) -> list[str]:
    """The words of a turn, annotations and punctuation stripped."""
    return _WORD.findall(_ANNOTATION.sub(" ", text or ""))


def no_real_word(text: str | None) -> bool:
    """True when the turn holds no real word: none at all once punctuation and
    annotations are stripped, or exactly one, and that one is noise."""
    w = words(text)
    return not w or (len(w) == 1 and bool(_NOISE.fullmatch(w[0])))

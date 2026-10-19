"""PHONE-158: the one rule both call legs mail by, `no_real_word(text)`.

Measured on call:78561 (Thu 2030-06-13): ".", "Music.", "Um", "Uh." were
each mailed and each woke the seat. A turn mails only if it has at least one word.
A one-word ANSWER is a real word and still mails.
"""
import pytest

from sessionphone.noise import no_real_word, words

# the turns measured on call:78561, and the rest of the no-word shapes
NO_WORD = [
    "", "   ", None, ".", "...", "Music.", "music", "MUSIC!", "Um", "Um.", "Uh.", "uh",
    "Umm...", "Uhh", "Mm.", "Mmm", "Hmm.", "Hm?", "Applause.", "- .", "…",
    "[BLANK_AUDIO]", "(Music)", "(upbeat music)", "[Music] .", "*sighs*", "( static )",
]

# one-word answers and the turns that have a word beside the noise
REAL = [
    "yes", "Yes.", "no", "No!", "go", "Go.", "okay", "OK", "Okay.", "yep", "nope",
    "Hmm, yes", "Um, go ahead.", "Mm-hmm.", "Uh-huh", "Music, please", "5", "Two.",
    "don't", "Don’t.", "Yes (laughs)", "[Music] yes", "stop", "Alice?",
]


@pytest.mark.parametrize("text", NO_WORD)
def test_a_turn_with_no_real_word_is_noise(text):
    assert no_real_word(text) is True


@pytest.mark.parametrize("text", REAL)
def test_a_turn_with_a_real_word_is_not_noise(text):
    assert no_real_word(text) is False


def test_words_strip_punctuation_and_annotations_and_keep_apostrophes():
    assert words("Hmm, yes.") == ["Hmm", "yes"]
    assert words("[BLANK_AUDIO] don't (laughs) go *sighs*") == ["don't", "go"]
    assert words("...") == []
    assert words(None) == []


def test_a_noise_word_inside_a_longer_word_is_not_noise():
    """The list is matched whole: 'umbrella', 'humming', 'musical' are words."""
    for w in ("umbrella", "humming", "musical", "uhuh", "mum", "hum"):
        assert no_real_word(w) is False, w

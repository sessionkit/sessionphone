"""A long mail spoken into a live call: the caller must be able to read all of it.

MailLeg.one() speaks a mail that threads to a live call and posts the same
words to the call's chat. A mail longer than the spoken cap must still reach
the chat WHOLE; if the spoken part is cut, the cut is said out loud, with a
closing marker that the rest is in the chat, never silently.

The first two tests are the repro: a 3000-character mail, its chat post and
its spoken text. The control is a short mail, spoken and posted whole with no
marker. The rest test spoken_part(), the cut itself: at a sentence end, else a
whole word, else the cap, the marker always counted inside the cap.

Fakes only: a scratch maildir, a fake call session, no loop, no TTS, no Delta
Chat. The real mail-leg path runs; speak() and post_to_chat() are recorded.
"""
import re
import textwrap

import pytest

from sessionphone.bridge import calls

ROOT = "call-12-1789@example.org"
MARKER = "the rest is in the chat"


# 500 distinct five-character words and 499 spaces, then one closing character:
# 3000 characters exactly, so a cut anywhere shows.
LONG = " ".join(f"w{i:04d}" for i in range(500)) + "."


class Session:
    pc, chat_id, root_id, account = object(), 12, ROOT, None

    async def speak(self, text, voice, who, floor=False):
        return None


class Manager:
    def __init__(self):
        self.sessions, self.loop = {1: Session()}, None


@pytest.fixture
def leg(tmp_path, monkeypatch):
    box = tmp_path / "box"
    (box / "new").mkdir(parents=True)
    monkeypatch.setattr(calls, "BOT_BOX", box)
    monkeypatch.setattr(calls, "VOICE_MAP_FILE", tmp_path / "voices.json")
    monkeypatch.setattr(calls, "GUESTS_FILE", tmp_path / "call-guests.txt")
    spoken, posts = [], []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, None, None))
    monkeypatch.setattr(calls, "reply_seen", lambda *a: None)
    monkeypatch.setattr(calls, "post_to_chat",
                        lambda account, chat_id, text, what: posts.append((chat_id, text, what)))

    def run(coro, loop):
        spoken.append(coro.cr_frame.f_locals.get("text"))
        coro.close()
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", run)

    class Rig:
        def drop(self, body, name="m1"):
            f = box / "new" / name
            lines = "\n".join(textwrap.wrap(body, 72))
            f.write_text("From: Alice <alice@example.org>\nTo: ada@example.org\n"
                         f"Subject: re: call\nIn-Reply-To: <{ROOT}>\n"
                         "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
                         f"{lines}\n")
            calls.MailLeg(Manager()).one(f)
            return f
    r = Rig()
    r.spoken, r.posts = spoken, posts
    return r


def tail(text):
    """The spoken text's last words, lower case, closing punctuation dropped."""
    return re.sub(r"[\s.!,;:]+$", "", text).lower()


def test_long_mail_reaches_the_chat_whole(leg):
    assert len(LONG) == 3000
    leg.drop(LONG)
    assert len(leg.posts) == 1
    chat_id, post, what = leg.posts[0]
    assert chat_id == 12 and what == "mail-leg post"
    assert LONG in post, (f"chat post carries {len(post)} chars, "
                          f"not all 3000 of the mail: ends {post[-40:]!r}")


def test_long_mail_spoken_cut_is_said(leg):
    leg.drop(LONG)
    assert len(leg.spoken) == 1
    said = leg.spoken[0]
    assert tail(said).endswith(MARKER), (f"spoken text ({len(said)} chars) ends "
                                         f"{said[-40:]!r}, with no marker that the rest is in the chat")
    assert len(said) <= calls.MAIL_SPEAK_MAX
    assert LONG.startswith(said[:-len(calls.SPOKEN_MORE)].rstrip(" ."))


def test_a_long_mail_is_spoken_to_a_whole_word_then_the_marker_apart(leg):
    leg.drop(LONG)
    said = leg.spoken[0]
    kept = said[:-len(calls.SPOKEN_MORE) - 1]
    assert said.endswith(". " + calls.SPOKEN_MORE)
    assert re.fullmatch(r"(w\d{4} )*w\d{4}\.", kept), kept[-40:]
    assert calls.sentences(said)[-1] == calls.SPOKEN_MORE


def test_short_mail_is_spoken_and_posted_whole_with_no_marker(leg):
    short = "Hello the call, a short one."
    leg.drop(short)
    assert leg.spoken == [short]
    assert len(leg.posts) == 1 and leg.posts[0][1].endswith(short)
    assert MARKER not in leg.spoken[0].lower()


# ---- spoken_part(): the cut itself ---------------------------------------------------

CAP = 100


def test_spoken_part_leaves_a_text_that_fits_untouched():
    assert calls.spoken_part("Short. And sweet.", CAP) == "Short. And sweet."
    exact = "x" * CAP
    assert calls.spoken_part(exact, CAP) == exact


def test_spoken_part_cuts_at_the_last_sentence_end_that_fits():
    text = "One two three. Four five six! Seven eight nine? " + "ten " * 40
    said = calls.spoken_part(text, CAP)
    assert said == "One two three. Four five six! Seven eight nine? " + calls.SPOKEN_MORE
    assert len(said) <= CAP


def test_spoken_part_does_not_take_a_dot_inside_a_word_as_a_sentence_end():
    text = "Version 3.14 is " + "out " * 40
    said = calls.spoken_part(text, CAP)
    assert said.startswith("Version 3.14 is out")
    assert said.endswith("out. " + calls.SPOKEN_MORE)
    assert len(said) <= CAP


def test_spoken_part_cuts_at_the_last_whole_word_with_no_sentence_end():
    text = " ".join(f"w{i:02d}" for i in range(60))      # 239 chars, no punctuation
    said = calls.spoken_part(text, CAP)
    kept = said[:-len(calls.SPOKEN_MORE) - 1]
    assert said.endswith(". " + calls.SPOKEN_MORE) and len(said) <= CAP
    assert text.startswith(kept[:-1]) and text[len(kept) - 1] == " "    # a whole word
    assert calls.sentences(said)[-1] == calls.SPOKEN_MORE


def test_spoken_part_drops_a_trailing_comma_at_a_word_cut():
    text = ", ".join(f"w{i:02d}" for i in range(60))
    said = calls.spoken_part(text, CAP)
    assert re.search(r"w\d\d\. " + re.escape(calls.SPOKEN_MORE) + "$", said), said[-40:]
    assert len(said) <= CAP


def test_spoken_part_cuts_one_giant_token_at_the_cap():
    text = "x" * 500
    said = calls.spoken_part(text, CAP)
    assert len(said) == CAP
    assert said == "x" * (CAP - len(calls.SPOKEN_MORE) - 2) + ". " + calls.SPOKEN_MORE


def test_spoken_part_uses_the_mail_cap_by_default():
    said = calls.spoken_part("y" * 3000)
    assert len(said) == calls.MAIL_SPEAK_MAX and said.endswith(calls.SPOKEN_MORE)

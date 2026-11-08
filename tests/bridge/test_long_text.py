"""A long incoming text is mailed whole.

With the core's bot flag off, the core cuts a long incoming text at about
3.8K chars, ends it with "[...]" and sets has_html; the whole text is only in
the message's html (rpc get_message_html). message_text() reads it back:

  - a cut text (has_html, ending "[...]") is mailed whole, from its html:
    tags dropped, entities unescaped, script and style left out, line breaks kept;
  - the html cannot be read: the cut text, and one line that says so and why;
  - a text that is not cut (no has_html, or html mail not ending "[...]") is
    unchanged, and no html is read;
  - a stranger's (ocr=False, request_in) text is read whole the same way.

Fakes only: the rpc is a recorder; no core, account or relay is opened.
"""
import pytest

from sessionphone.bridge import bot

CUT = "word " * 760 + "[...]"         # about what the core leaves: ~3.8K chars, its mark last
TAIL = "the very end of the message"
# the shape the core gives a plain-text message's html
PAGE = ("<!DOCTYPE html>\n<html><head>\n"
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />\n'
        '<meta name="color-scheme" content="light dark" />\n'
        '</head><body dir="auto" style="unicode-bidi: plaintext">\n'
        "first line &amp; more &lt;b&gt;not a tag&lt;/b&gt;<br/>\n"
        "&nbsp;&nbsp;two spaces in<br/>\n"
        '<a href="https://example.org/x">https://example.org/x</a><br/>\n'
        "<br/>\n"
        f"{TAIL}<br/>\n"
        "</body></html>")
WHOLE = ("first line & more <b>not a tag</b>\n"
         "  two spaces in\n"
         "https://example.org/x\n\n"
         + TAIL)


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeRpc:
    """get_message_html, recorded; answers `page`, or raises it."""

    def __init__(self, page):
        self.page, self.calls = page, []

    def get_message_html(self, account_id, msg_id):
        self.calls.append((account_id, msg_id))
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


def snap(text, *, has_html=True, page=PAGE, mid=77, quote=None, rpc=None):
    """A message snapshot as the client builds it: .message is the Message,
    whose account reaches the rpc (account.manager.rpc)."""
    rpc = rpc or FakeRpc(page)
    account = Obj(id=3, manager=Obj(rpc=rpc))
    return Obj(id=mid, text=text, has_html=has_html, file=None, file_mime="",
               view_type="Text", quote=quote, message=Obj(id=mid, account=account),
               sender=Obj(get_snapshot=lambda: Obj(display_name="Stranger", address="s@relay"))), rpc


@pytest.fixture
def world(monkeypatch):
    w = Obj(logged=[], mailed=[])
    monkeypatch.setattr(bot, "log", w.logged.append)
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: w.mailed.append((a, k)))
    return w


# -- a cut text is mailed whole ------------------------------------------------

@pytest.mark.parametrize("ocr", [True, False], ids=["accepted", "stranger"])
def test_a_cut_text_is_mailed_whole_from_its_html(world, ocr):
    s, rpc = snap(CUT)
    assert bot.message_text(s, ocr=ocr) == WHOLE
    assert rpc.calls == [(3, 77)]                     # this account, this message
    assert any("LONG TEXT msg 77" in line for line in world.logged)


def test_the_text_the_core_cut_off_is_there(world):
    long = "x" * 12000 + TAIL
    s, _ = snap(CUT, page=f"<html><body>{long}<br/>\n</body></html>")
    assert bot.message_text(s) == long


def test_tags_scripts_and_styles_are_dropped_and_entities_unescaped(world):
    page = ("<html><head><title>no</title><style>p { color: red }</style></head>"
            "<body><script>alert('no')</script><p>caf&eacute; &#8212; &quot;ok&quot;</p>"
            "<div>a <b>bold</b>\n   word</div><ul><li>one</li><li>two</li></ul></body></html>")
    s, _ = snap(CUT, page=page)
    said = bot.message_text(s)
    assert said == 'café — "ok"\n\na bold word\n\none\n\ntwo'
    for gone in ("<", ">", "alert", "color", "&"):
        assert gone not in said


def test_pre_keeps_its_own_spacing(world):
    s, _ = snap(CUT, page="<body><pre>a   b\n  c</pre></body>")
    assert bot.message_text(s) == "a   b\n  c"


def test_a_cut_reply_carries_its_quote_once(world):
    quote = {"authorDisplayName": "Bob", "text": "earlier words", "messageId": 5}
    page = "<body><em>&gt; earlier words</em><br/>\n<br/>\nmy long answer<br/>\n</body>"
    s, _ = snap(CUT, page=page, quote=quote)
    assert bot.message_text(s) == ("> in reply to Bob (msg 5):\n> earlier words\n\n"
                                   "my long answer")


def test_a_strangers_cut_text_is_whole_under_the_request_banner(world):
    s, rpc = snap(CUT)
    bot.request_in("chat9", 9, s)
    (args, kw), = world.mailed
    assert args[3].startswith("CONTACT REQUEST - NOT ACCEPTED.")
    assert args[3].endswith("\n\n" + WHOLE)
    assert kw["kind"] == "request"
    assert rpc.calls == [(3, 77)]


# -- the html cannot be read: the cut text, and why ------------------------------

@pytest.mark.parametrize("page, why", [
    (RuntimeError("rpc gone"), "RuntimeError: rpc gone"),
    (None, "ValueError: the html had no text"),
    ("<html><head><title>t</title></head><body> </body></html>", "ValueError: the html had no text"),
], ids=["raises", "none", "no-text"])
def test_an_unreadable_html_keeps_the_cut_text_and_says_why(world, page, why):
    s, rpc = snap(CUT, page=page)
    said = bot.message_text(s)
    assert said == (CUT + "\n\n[this message was cut short by the chat app; "
                    f"its full text could not be read: {why}]")
    assert rpc.calls == [(3, 77)]
    assert any("html not read" in line for line in world.logged)


def test_a_snapshot_with_no_message_handle_keeps_the_cut_text(world):
    s, _ = snap(CUT)
    del s.message
    said = bot.message_text(s)
    assert said.startswith(CUT + "\n\n[this message was cut short by the chat app; ")
    assert said.count("\n") == 2                   # the note is ONE line
    assert "AttributeError" in said


def test_a_reason_with_newlines_is_one_line(world):
    s, _ = snap(CUT, page=RuntimeError("line one\nline two"))
    note = bot.message_text(s).split("\n\n")[-1]
    assert "\n" not in note and "line one line two" in note


# -- a text that is not cut is unchanged, and no html is read ---------------------

@pytest.mark.parametrize("text, has_html", [
    ("just words", False),                    # a short text
    (CUT, False),                             # the mark alone, no html: a user typed it
    ("an html mail's plain part", True),      # html mail, not cut
], ids=["short", "mark-no-html", "html-not-cut"])
def test_a_text_that_is_not_cut_is_unchanged_and_no_html_is_read(world, text, has_html):
    s, rpc = snap(text, has_html=has_html)
    assert bot.message_text(s) == text
    assert bot.message_text(s, ocr=False) == text
    assert rpc.calls == []


def test_a_snapshot_without_has_html_is_unchanged(world):
    """Every fake and snapshot that predates the field: no has_html, no read."""
    s, rpc = snap("hello")
    del s.has_html
    assert bot.message_text(s) == "hello"
    assert rpc.calls == []


# -- html_text() on its own ------------------------------------------------------

def test_html_text_of_nothing_is_empty():
    assert bot.html_text("") == "" and bot.html_text(None) == ""


def test_html_text_keeps_at_most_one_blank_line():
    assert bot.html_text("a<br><br><br><br>b") == "a\n\nb"

"""The text on a posted image, in the bridge mail (PHONE-34, design §4, §6, §7).

  AC 1  an image with no typed text mails its image line and its text, fenced.
  AC 2  an image WITH typed text keeps its image line (bot.py dropped it).
  AC 5  a read() that gives up mails the no-OCR body; mail_in is still called.
  Q7    a contact request's image is never read, held or auto-accepted, on the
        live path and on the startup sweep.
  Q6    DC_IMAGE_TEXT=off: the no-OCR body, and the helper is never started.

Fakes only: imgtext.read is replaced by a recorder, so no test spawns swiftc,
the helper or Vision, and no real Delta blob is read.
"""
import pytest

from sessionphone import imgtext
from sessionphone.bridge import bot

SIGN = ["Pillar Point 800m >", "Squeaky Beach 1.1km >", "< Tidal River 1km"]
FENCE = "\n".join([imgtext.OPEN, *("| " + s for s in SIGN), imgtext.CLOSE])
LINE = "[attachment: sign.png (image/png) at /scratch/sign.png]"          # PHONE-268: the path
UNTRUSTED = " - UNTRUSTED: a stranger's file, not opened; open it only if your owner asks"
REQUEST_LINE = LINE[:-1] + UNTRUSTED + "]"                                # a contact request's


class Done(Exception):
    """The fake account's events ran out; handle_events() loops forever."""


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeMsg:
    def __init__(self, snap):
        self.id, self.snap = snap.id, snap

    def get_snapshot(self):
        return self.snap


class FakeChat:
    def __init__(self, name, request):
        self.info = Obj(name=name, is_contact_request=request)
        self.accepted = False

    def get_basic_snapshot(self):
        return self.info

    def accept(self):
        self.accepted = True
        self.info.is_contact_request = False


class FakeAccount:
    """The account surface handle_events() and sweep() use, and no more."""

    def __init__(self):
        self.msgs, self.chats, self.events, self.fresh = {}, {}, [], []

    def add(self, mid, chat_id, text="", *, request=False, **kw):
        sender = Obj(get_snapshot=lambda: Obj(display_name="Stranger", address="stranger@relay"))
        snap = img(text, id=mid, chat_id=chat_id, sender=sender, **kw)
        self.msgs[mid] = FakeMsg(snap)
        self.chats.setdefault(chat_id, FakeChat(f"chat{chat_id}", request))

    def arrive(self, mid):
        m = self.msgs[mid]
        self.events.append(Obj(kind=bot.EventType.INCOMING_MSG, msg_id=mid, chat_id=m.snap.chat_id))
        self.fresh.append(m)

    def wait_for_event(self):
        if not self.events:
            raise Done
        return self.events.pop(0)

    def get_message_by_id(self, mid):
        return self.msgs[mid]

    def get_chat_by_id(self, chat_id):
        return self.chats[chat_id]

    def get_fresh_messages(self):
        return list(self.fresh)

    def mark_seen_messages(self, msgs):
        for m in msgs:
            if m in self.fresh:
                self.fresh.remove(m)


def img(text="", **kw):
    """A message snapshot carrying sign.png unless told otherwise."""
    base = dict(text=text, file="/scratch/sign.png", file_name="sign.png",
                file_mime="image/png", view_type="Image", quote=None)
    base.update(kw)
    return Obj(**base)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Module state in a scratch dir; mail_in, log and imgtext.read recorded.
    read() answers the sign's lines unless a test sets world.answer."""
    monkeypatch.setattr(bot, "SEEN", tmp_path / "bridged-ids.txt")
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", False)
    monkeypatch.setattr(bot, "IMAGE_TEXT", True)
    w = Obj(mailed=[], logged=[], reads=[], answer=list(SIGN), account=FakeAccount())
    monkeypatch.setattr(bot, "mail_in", lambda *a, **k: w.mailed.append((a, k)))
    monkeypatch.setattr(bot, "log", w.logged.append)

    def read(path, log):
        w.reads.append(path)
        return w.answer
    monkeypatch.setattr(imgtext, "read", read)
    return w


def body(w):
    (args, _), = w.mailed
    return args[3]


# -- message_text(): the body shapes (design §3) --------------------------------

def test_an_image_without_text_mails_its_line_and_its_text_fenced(world):
    assert bot.message_text(img()) == LINE + "\n\n" + FENCE          # AC 1
    assert world.reads == ["/scratch/sign.png"]


def test_an_image_with_typed_text_keeps_its_image_line(world):
    """AC 2, the regression for bot.py:277: typed text used to drop the image."""
    quote = {"authorDisplayName": "Bob", "text": "where are we", "messageId": 412}
    said = bot.message_text(img("which way to the beach?", quote=quote))
    assert said == ("> in reply to Bob (msg 412):\n> where are we\n\n"
                    "which way to the beach?\n\n" + LINE + "\n\n" + FENCE)


def test_a_read_that_gives_up_mails_the_exact_no_ocr_body(world):
    world.answer = None                                              # AC 5
    assert bot.message_text(img()) == LINE
    assert bot.message_text(img("look")) == "look\n\n" + LINE


def test_an_image_with_no_text_found_says_so(world):
    world.answer = []                                                # Q4
    assert bot.message_text(img()) == LINE + "\n\n" + imgtext.NONE_FOUND


def test_the_view_type_alone_marks_an_image(world):
    said = bot.message_text(img(file_mime="", view_type="Gif"))
    assert said.startswith("[attachment: sign.png (unknown type) at /scratch/sign.png]\n\n" + imgtext.OPEN)


def test_a_sticker_is_not_read_and_keeps_its_line(world):
    """A sticker is not read; its line stays beside typed text (PHONE-268)."""
    snap = img(file_name="cat.webp", file_mime="image/webp", view_type="Sticker")
    assert bot.message_text(snap) == "[attachment: cat.webp (image/webp) at /scratch/sign.png]"
    assert (bot.message_text(img("hi", file_mime="image/webp", view_type="Sticker"))
            == "hi\n\n[attachment: sign.png (image/webp) at /scratch/sign.png]")
    assert world.reads == []


def test_a_non_image_file_is_not_read_and_keeps_its_line(world):
    """A file is not read; its line names the path and stays beside typed
    text (PHONE-268: the text alone used to come back, the file unmentioned)."""
    snap = img(file_name="notes.pdf", file_mime="application/pdf", view_type="File")
    pdf = "[attachment: notes.pdf (application/pdf) at /scratch/sign.png]"
    assert bot.message_text(snap) == pdf
    assert bot.message_text(img("see attached", file_name="notes.pdf", file_mime="application/pdf",
                                view_type="File")) == "see attached\n\n" + pdf
    assert world.reads == []


def test_an_svg_is_not_read(world):
    snap = img(file_name="logo.svg", file_mime="image/svg+xml", view_type="File")
    assert bot.message_text(snap) == "[attachment: logo.svg (image/svg+xml) at /scratch/sign.png]"
    assert world.reads == []


def test_ocr_false_never_reads_and_says_why(world):
    said = bot.message_text(img(), ocr=False)
    assert said == REQUEST_LINE + "\n\n" + imgtext.NOT_READ_REQUEST
    assert world.reads == []


# -- the kill switch: DC_IMAGE_TEXT=off (Q6) ----------------------------------

def test_image_text_off_gives_the_no_ocr_body(world, monkeypatch):
    monkeypatch.setattr(bot, "IMAGE_TEXT", False)
    assert bot.message_text(img()) == LINE
    assert bot.message_text(img(), ocr=False) == REQUEST_LINE
    assert world.reads == []


def test_image_text_is_on_by_default_and_off_only_when_said(monkeypatch):
    monkeypatch.delenv("DC_IMAGE_TEXT", raising=False)
    assert bot.dc_env.env("IMAGE_TEXT", "on") != "off"
    monkeypatch.setenv("DC_IMAGE_TEXT", "off")
    assert bot.dc_env.env("IMAGE_TEXT", "on") == "off"


# -- the mail paths: live, sweep, held request (design §6, Q7) ----------------

def run_live(account):
    with pytest.raises(Done):
        bot.handle_events(account)


def test_a_live_image_in_an_accepted_chat_is_read(world):
    world.account.add(30, chat_id=3)
    world.account.arrive(30)
    run_live(world.account)
    assert body(world) == LINE + "\n\n" + FENCE
    assert world.reads == ["/scratch/sign.png"]


def test_a_swept_image_in_an_accepted_chat_is_read(world):
    world.account.add(31, chat_id=3)
    world.account.fresh.append(world.account.msgs[31])
    bot.sweep(world.account)
    assert body(world) == LINE + "\n\n" + FENCE


@pytest.mark.parametrize("path", ["live", "sweep"])
@pytest.mark.parametrize("accept", [False, True], ids=["held", "auto-accepted"])
def test_a_contact_requests_image_is_never_read(world, monkeypatch, path, accept):
    monkeypatch.setattr(bot, "ACCEPT_REQUESTS", accept)
    world.account.add(40, chat_id=7, request=True)
    if path == "live":
        world.account.arrive(40)
        run_live(world.account)
    else:
        world.account.fresh.append(world.account.msgs[40])
        bot.sweep(world.account)
    assert world.reads == []
    assert world.account.chats[7].accepted is accept
    said = body(world)                                               # mail_in called, once
    assert said.endswith(REQUEST_LINE + "\n\n" + imgtext.NOT_READ_REQUEST), said
    assert imgtext.OPEN not in said


def test_main_starts_the_helper_before_the_sweep_and_only_when_on():
    """main() cannot run here (rpc-server, account); its source is the check."""
    import inspect
    src = inspect.getsource(bot.main)
    lines = [line.strip() for line in src.splitlines()]
    start = lines.index('imgtext.start(dc_env.ROOT / "bin", log)')
    assert lines[start - 1].startswith("if IMAGE_TEXT:")
    assert start < lines.index("sweep(account)")

"""PHONE-334 fix-003 (PC): the mail leg never SPEAKS an auto-generated mail.

sessionmail SM-1047 weighs an autoresponder on a cold seat's box. If one ever
answers `<agent>-chat` threaded to a live call, the mail leg would speak it to
Mike in the sender's voice. Sessionmail will not autoreply to X-Delta-Chat-*
mail; this is phone's second guard. A mail whose `Auto-Submitted` (RFC 3834)
is anything but `no` is not spoken and not posted. It gets ONE log line with
no content (Message-ID and the header's keyword only) and is marked read, as
the leg's own mail is, so it is not examined again. `Auto-Submitted: no`, or
no such header, is a human's reply: it is spoken exactly as before.

Fakes only, as tests/test_mailleg_own_from.py: a scratch maildir, a fake call
session, no loop, no TTS, no Delta Chat.
"""
import pytest

from sessionphone.bridge import calls, dc_env

ROOT = "call-12-1789@ms5.elsewhere.example"
ADDR = "ada-chat@ms5.elsewhere.example"
SUBJECT = "Out of office: re: call"
BODY = "I am away until Monday."
MSGID = "<auto-1@relay.example>"


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
    monkeypatch.setattr(dc_env, "ADDR", ADDR)
    logged, spoken, posted = [], [], []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logged.append, None, None))
    monkeypatch.setattr(calls, "reply_seen", lambda *a: None)
    monkeypatch.setattr(calls, "post_to_chat", lambda *a: posted.append(a) or True)

    def run(coro, loop):
        spoken.append(coro.cr_frame.f_locals.get("text"))
        coro.close()
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", run)

    class Rig:
        def drop(self, auto=None, name="m1", root=ROOT):
            """AUTO: the Auto-Submitted header lines (None: no header)."""
            heads = "".join(f"Auto-Submitted: {a}\n" for a in ([] if auto is None else
                                                              [auto] if isinstance(auto, str) else auto))
            f = box / "new" / name
            f.write_text(f"From: Alice <alice@host>\nTo: ada-chat@host\nSubject: {SUBJECT}\n"
                         f"Message-ID: {MSGID}\nIn-Reply-To: <{root}>\n{heads}"
                         "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
                         f"{BODY}\n")
            calls.MailLeg(Manager()).one(f)
            return f
    r = Rig()
    r.box, r.logged, r.spoken, r.posted = box, logged, spoken, posted
    return r


def marked_read(leg, f):
    return not f.exists() and [p.name for p in (leg.box / "cur").iterdir()] == ["m1:2,S"]


def auto_lines(leg):
    return [line for line in leg.logged if "Auto-Submitted" in line]


@pytest.mark.parametrize("value", [
    "auto-replied",
    "auto-generated",
    "auto-notified",
    "Auto-Replied",                                  # case-insensitive
    "AUTO-GENERATED",
    "auto-replied; owner-email=\"a@b\"",            # parameters after ";" are ignored
    " auto-generated ",
    "auto-replied (vacation)",                       # an RFC 5322 comment
    "x-some-robot",                                  # any keyword but "no"
    "",                                              # present but empty is not "no"
])
def test_an_auto_submitted_mail_is_not_spoken(leg, value):
    f = leg.drop(value)
    assert leg.spoken == []
    assert leg.posted == []                          # nor posted to the call's chat
    assert marked_read(leg, f)                       # handled: not re-examined each tick


@pytest.mark.parametrize("value", ["auto-replied", "auto-generated"])
def test_a_skipped_mail_logs_one_line_with_no_content(leg, value):
    leg.drop(value)
    (line,) = auto_lines(leg)
    assert len(leg.logged) == 1, leg.logged          # that line is the only one
    assert MSGID in line and value in line
    for content in (SUBJECT, "Out of office", BODY, "away", "alice", "Alice"):
        assert content not in line, line


@pytest.mark.parametrize("auto", [None, "no", "No", "NO", " no ", "no; x=y", "no (a human)"],
                         ids=["absent", "no", "No", "NO", "padded", "params", "comment"])
def test_a_human_reply_is_spoken_as_before(leg, auto):
    f = leg.drop(auto)
    assert leg.spoken == [BODY]
    assert len(leg.posted) == 1
    assert auto_lines(leg) == []
    assert marked_read(leg, f)


def test_one_auto_header_among_several_is_enough_to_skip(leg):
    f = leg.drop(["no", "auto-replied"])
    assert leg.spoken == [] and marked_read(leg, f)


def test_an_auto_mail_for_no_live_call_is_left_alone(leg):
    """Not for a call in progress: untouched and unlogged, as any such mail."""
    f = leg.drop("auto-replied", root="call-99-1@elsewhere.example")
    assert leg.spoken == [] and leg.logged == []
    assert f.exists()

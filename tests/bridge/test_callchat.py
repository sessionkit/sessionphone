#!/usr/bin/env python3
"""A stale call root must still reach the live call in that chat.

    BOT=/path/to/delta-chat/bot .venv/bin/python test_callchat.py

The case: an agent addressed the root of a call that had
ended eleven seconds earlier. The mail landed in cur/ and was never spoken.

Since PHONE-155 (ruling (ii)) the stale root must be one the bridge ISSUED for
that chat: DEAD is minted through calls.ISSUED, as CallSession does, the call
ends, and the live call is minted eleven seconds later (inside TURNOVER_S).
A call-<chatid>-<digits> string the bridge never issued is not spoken.
"""
import atexit, os, pathlib, shutil, sys, types, tempfile
from sessionphone.bridge import calls

spoken = []

# PHONE-102: an in-call chat post goes only to a listed chat (design rule 5,
# rows 11-13), so the chat these cases post to, 12, is listed.
_LIST = pathlib.Path(tempfile.mkdtemp(prefix="channels-")) / "channels.txt"
atexit.register(shutil.rmtree, _LIST.parent, True)   # leave no channels-* dir behind
_LIST.write_text("family  chat:12\n")
_LIST.chmod(0o600)
os.environ["DC_CHANNELS_FILE"] = str(_LIST)
calls._hooks = calls.Hooks(lambda m: None, lambda p: "", lambda *a, **k: None)

DEAD = "call-12-1893671701@example.org"   # the call that ended
LIVE = "call-12-1893672000@example.org"   # the one running now


class FakeChat:
    def send_text(self, t): spoken.append(("chat", t))


class FakeAccount:
    def get_chat_by_id(self, _): return FakeChat()


def session(root, chat_id=12):
    s = object.__new__(calls.CallSession)
    s.pc, s.root_id, s.chat_id = object(), root, chat_id
    s.account = FakeAccount()
    s.speak = lambda text, voice, who: spoken.append(("voice", who, text))
    return s


def leg(sessions):
    mgr = types.SimpleNamespace(sessions={i: s for i, s in enumerate(sessions)},
                                loop=types.SimpleNamespace(
                                    call_soon_threadsafe=lambda *a: None))
    lg = object.__new__(calls.MailLeg)
    lg.manager = mgr
    return lg


def mail(root, body="hello"):
    p = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "cc-test.eml"
    p.write_text(f"From: Carol <carol@example.org>\n"
                 f"To: alice-chat@example.org\n"
                 f"Subject: t\nIn-Reply-To: <{root}>\n"
                 f"References: <{root}>\n\n{body}\n")
    return p


def run():
    import asyncio
    calls.ISSUED = calls.IssuedRoots()          # a fresh bot: nothing issued
    calls.ISSUED.mint(12, DEAD, now=1893671701)
    calls.ISSUED.end(12, now=1893671989)        # the call ended
    calls.ISSUED.mint(12, LIVE, now=1893672000) # eleven seconds later, the next
    real = asyncio.run_coroutine_threadsafe
    asyncio.run_coroutine_threadsafe = lambda coro, loop: None

    # 1. exact root still wins
    spoken.clear()
    lg = leg([session(LIVE)])
    lg.seen = lambda f: None
    lg.one(mail(LIVE))
    assert any(k == "chat" for k, *_ in spoken), f"exact root broke: {spoken}"

    # 2. STALE but ISSUED root, live call in the same chat -> still spoken
    spoken.clear()
    lg = leg([session(LIVE)])
    lg.seen = lambda f: None
    lg.one(mail(DEAD))
    assert any(k == "chat" for k, *_ in spoken), \
        "a stale root is still dropped - this is the bug"

    # 3. no live call anywhere -> left alone, as before
    spoken.clear()
    lg = leg([])
    lg.seen = lambda f: (_ for _ in ()).throw(AssertionError("marked seen"))
    lg.one(mail(DEAD))
    assert not spoken, f"spoke into nothing: {spoken}"

    # 4. a live call in a DIFFERENT chat must not hear it
    spoken.clear()
    lg = leg([session("call-99-1893672000@example.org", chat_id=99)])
    lg.seen = lambda f: None
    lg.one(mail(DEAD))
    assert not spoken, f"leaked into another chat: {spoken}"

    # 5. a root of chat 12's shape that the bridge never issued -> not spoken
    spoken.clear()
    lg = leg([session(LIVE)])
    lg.seen = lambda f: None
    lg.one(mail("call-12-4242424242@forger.example"))
    assert not spoken, f"a forged root reached the live call: {spoken}"

    asyncio.run_coroutine_threadsafe = real
    print("ok: exact root works; stale issued root reaches the live call in that chat;")
    print("    no live call stays untouched; another chat never hears it;")
    print("    a forged root never matches")


run()

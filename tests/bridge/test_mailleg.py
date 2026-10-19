#!/usr/bin/env python3
"""Unit test for the INBOUND leg: a mail that threads to a live call is spoken
into it exactly once. No network, no whisper, no phone.

Every mail here is UNSIGNED, so since PHONE-155 none is KNOWN: a From in
voices.json and an X-Voice header are both ignored, and the sender is announced
as unknown in the plain voice. The signed path (a key pinned on a guest row)
is tests/test_mailleg_sender_trust.py's.
Usage: .venv/bin/python test_mailleg.py"""
import pathlib, sys, tempfile, time, types
from sessionphone.bridge import calls

box = pathlib.Path(tempfile.mkdtemp(prefix="mailleg-"))
(box / "new").mkdir(); (box / "cur").mkdir()
calls.BOT_BOX = box
calls.VOICE_MAP_FILE = box / "voices.json"
calls.VOICE_MAP_FILE.write_text('{"alice": "af_sky"}')
calls.GUESTS_FILE = box / "call-guests.txt"       # never the real root's guest list
calls.GUESTS_FILE.write_text("alice@example.org\n")   # address only: copied, never known
said = []
calls._hooks = calls.Hooks(lambda m: print("  " + m), None, None)

class Chat:
    def send_text(self, t): said.append(("chat", t))
class Account:
    def get_chat_by_id(self, i): return Chat()
class Session:
    pc, chat_id, root_id, account = object(), 12, "call-12-1789@example.org", Account()
    async def speak(self, text, voice="carol", who="Carol"):
        said.append((who, voice, text))
class Loop:
    pass
class Manager:
    def __init__(self): self.sessions = {1: Session()}; self.loop = Loop()

mgr = Manager()
# run_coroutine_threadsafe needs a real loop; run the coroutine inline instead
import asyncio
def inline(coro, loop):
    asyncio.run(coro)
    class F:
        def result(self, *a): return None
    return F()
calls.asyncio.run_coroutine_threadsafe = inline

def drop(name, frm, refs, body, extra=""):
    (box / "new" / name).write_text(
        f"From: {frm}\nTo: carol-chat@example.org\n"
        f"Subject: re: call\nIn-Reply-To: <{refs}>\n{extra}"
        f"MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n{body}\n")

leg = calls.MailLeg(mgr)
print("1. Alice replies to the live call's thread, unsigned")
drop("m1", "Alice <alice@example.org>", "call-12-1789@example.org",
     "Hello Bob, this is Alice, speaking through Carol.\n\n---\nchat: junk footer\n")
leg.sweep()
print("2. a mail on ANOTHER thread is left alone")
drop("m2", "Alice <alice@example.org>", "some-other-thread@example.org", "Not for the call.")
leg.sweep()
print("3. an unknown sender is announced, not given someone's voice")
drop("m3", "Sam <sam@elsewhere>", "call-12-1789@example.org", "Sam here.")
leg.sweep()
print("4. an unsigned X-Voice is ignored")
drop("m4", "Alice <alice@example.org>", "call-12-1789@example.org", "Speaking in a voice of its own.",
     extra="X-Voice: demo/custom-voice\n")
leg.sweep()
print("5. sweeping again says nothing twice")
before = len(said)
leg.sweep()

spoken = [s for s in said if s[0] != "chat"]
ok = True
def check(cond, why):
    global ok
    print(("  ok   " if cond else "  FAIL ") + why); ok = ok and cond
check(len(spoken) == 3, f"three mails spoken, not {len(spoken)} (thread match works)")
check(spoken[0][1] == calls.UNKNOWN_VOICE and "do not have a voice" in spoken[0][0],
      f"unsigned Alice announced unknown, not in af_sky: {spoken[0][:2]}")
check("junk footer" not in spoken[0][2] and "Hello Bob" in spoken[0][2],
      f"footer stripped: {spoken[0][2]!r}")
check(spoken[1][1] == calls.UNKNOWN_VOICE and "do not have a voice" in spoken[1][0],
      f"unknown sender announced: {spoken[1][:2]}")
check(spoken[2][1] == calls.UNKNOWN_VOICE and "do not have a voice" in spoken[2][0],
      f"unsigned X-Voice ignored: {spoken[2][:2]}")
check(len(said) == before, "nothing spoken twice")
check((box / "new" / "m2").exists(), "the other thread's mail was left in new/")
check(len(list((box / "cur").iterdir())) == 3, "three mails marked read")
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

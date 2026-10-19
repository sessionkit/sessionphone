#!/usr/bin/env python3
"""Loopback test for calls.py: a fake phone (aiortc peer, the sample clip as
its microphone, the webapp's two negotiated data channels) places a call;
CallSession answers through a fake account. Proves, without a person: SDP
answer accepted, connection up, greeting audio flows OUT, the clip is heard,
VAD'd, transcribed (DC_STT_URL) and handed to mail_in. The fake caller is
an approved caller, so the gate lets it through.
Usage: DC_TTS=apple .venv/bin/python tests/bridge/loopback.py CLIP.wav
       (DC_TTS=apple speaks the greeting as a bare Mac would: macOS say)"""
import asyncio, json, os, sys, tempfile, time, pathlib, numpy as np
CALLER = "owner@example.org"
os.environ.setdefault("DC_AGENT", "loopback")          # bot.py would take argv[1] (the clip)
os.environ.setdefault("DC_ROOT", tempfile.mkdtemp(prefix="dc-loopback-"))
# approved in a callers file: DC_CALLERS grants nothing (PHONE-132)
_callers = pathlib.Path(os.environ["DC_ROOT"]) / "callers.txt"
_callers.write_text(CALLER + "\n")
os.environ["DC_CALLERS_FILE"] = str(_callers)
from sessionphone.bridge import calls
from sessionphone.bridge.bot import transcribe
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer

clip = sys.argv[1]
mails, heard_out = [], {"frames": 0, "loud": 0}
calls._hooks = calls.Hooks(lambda m: print(f"  {time.strftime('%H:%M:%S')} {m}", flush=True),
                           transcribe, lambda *a, **k: mails.append(a))
calls.CALLS_DIR = pathlib.Path(os.environ["DC_ROOT"]) / "calls"; calls.CALLS_DIR.mkdir(parents=True, exist_ok=True)
loop = calls._manager.loop
calls._manager.start()

class Snap(dict):
    __getattr__ = dict.get
class Sender:
    def get_snapshot(self): return Snap(display_name="Owner", address=CALLER)
class Msg:
    def __init__(self, phone): self.phone = phone
    def get_snapshot(self): return Snap(sender=Sender(), chat_id=99)
    def accept_incoming_call(self, sdp):
        print("  fake core: answer delivered to the phone")
        asyncio.run_coroutine_threadsafe(
            self.phone.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer")), loop).result(10)
    def end_call(self): print("  fake core: end_call")
class Chat:
    def get_basic_snapshot(self): return Snap(name="Owner & loopback", is_contact_request=False)
class Account:
    def __init__(self, phone): self.phone = phone
    def ice_servers(self): return []
    def get_chat_by_id(self, i): return Chat()
    def get_message_by_id(self, i): return Msg(self.phone)

async def phone_call():
    phone = RTCPeerConnection()
    phone.createDataChannel("iceTrickling", negotiated=True, id=1)
    muted = phone.createDataChannel("mutedState", negotiated=True, id=3)
    muted.on("message", lambda d: print("  phone got mutedState:", d))
    player = MediaPlayer(clip)
    phone.addTrack(player.audio)
    @phone.on("track")
    def on_track(track):
        async def sink():
            try:
                while True:
                    f = await track.recv()
                    a = f.to_ndarray(); heard_out["frames"] += 1
                    if np.abs(a).max() > 500: heard_out["loud"] += 1
            except Exception:
                pass
        asyncio.ensure_future(sink())
    await phone.setLocalDescription(await phone.createOffer())
    print("  phone: offer ready,", sum(1 for l in phone.localDescription.sdp.splitlines() if l.startswith("a=candidate")), "candidates")
    ev = Snap(msg_id=4242, chat_id=99, place_call_info=phone.localDescription.sdp, has_video=False)
    calls.incoming(Account(phone), ev)
    for _ in range(40):
        await asyncio.sleep(0.5)
        if mails and any("(on the call)" in m[3] for m in mails): break
    await asyncio.sleep(1)
    print("\nRESULT")
    print("  phone connection:", phone.connectionState)
    print(f"  Carol audio at phone: {heard_out['frames']} frames, {heard_out['loud']} loud (greeting = ~60 loud)")
    for m in mails: print("  mail_in:", m)
    ok = phone.connectionState == "connected" and heard_out["loud"] > 20 and any("(on the call)" in m[3] for m in mails)
    print("  PASS" if ok else "  FAIL")
    for s in list(calls._manager.sessions.values()): await s.close("test over")
    await phone.close()
    return ok

ok = asyncio.run_coroutine_threadsafe(phone_call(), loop).result(60)
sys.exit(0 if ok else 1)

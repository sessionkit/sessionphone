"""The ear leg: a live call is a microphone the ears can open.

AirPods appear and disappear as an input device; a Delta call can be the same
kind of thing, another ear, just as a new USB microphone is. A USB mic hands
the computer PCM; a call, once this bot has decoded it, is the same PCM. So
while a call is live this socket serves the caller's decoded audio - 16 kHz mono
int16, the frames the bot's own ears get - to any capture that asks. The
ears open ``call:<N>`` like ``airpods``: the same VAD, STT, rows, gates. The
mouth leg (mouthleg.py) is the other half of the pair.

THE SOCKET: ``<ROOT>/call-ears.sock``, or ``<ROOT>/<PROFILE>/call-ears.sock``
for a named profile (DC_EARS_SOCK overrides). Mode 0600, served on the calls
loop. One connection is one listener:

  probe:  ears -> {"probe": true}\\n
          bot  -> {"ok": true, "agent": "claude", "profile": "", "calls": [N, ...], "rate": 16000}\\n

  listen: ears -> {"call": N}\\n     (or {"call": null}: the newest live call)
          bot  -> {"ok": true, "call": N, "rate": 16000, "format": "s16le", "channels": 1,
                   "chat": 12, "chat_name": "Owner"}\\n   or {"ok": false, "error": "..."}\\n
          bot  -> frames: 1 byte kind + 4 byte big-endian length + payload
                    A  int16 little-endian mono PCM at 16 kHz, in call order
                    E  the call ended (payload: the reason, utf-8); the bot closes

A listener that falls more than QUEUE_S behind loses the oldest audio, never
the call: the bot's own work never waits on a slow reader.

WHERE A TAP STARTS IN caller-call.wav (for the speaker side-car naming call
audio): a capture's t0/t1 are seconds into ITS stream,
and its stream is caller-call.wav from the moment its tap was added. So each
tap appends a row to ``<call dir>/ear-taps.jsonl``:

  {"t": <wall>, "call": N, "wav_s": <seconds of caller-call.wav written when the tap began>}
  {"t": <wall>, "call": N, "end": "call-ended", "sent_s": ..., "dropped": <frames lost>}

and wav second = wav_s + t0, exactly. The wall clock will not do: on a
22-minute call the capture's heartbeats drifted 2.0 s from it in 21
minutes, while the audio-position offset stayed put (0.7 s early, 0.8 s
late). A tap that dropped frames shifts after the first drop: its end row
says how many. The ok reply carries ``wav_s`` too.
"""
import asyncio
import json
import os
import pathlib
import struct
import time
import traceback

from sessionphone.bridge import dc_env

PROFILE = dc_env.env("PROFILE", "")
SOCK = dc_env.path("EARS_SOCK", *([PROFILE] if PROFILE else []), "call-ears.sock")
RATE = 16000                    # IN_RATE: what the bot's ears are fed
HEADER_TIMEOUT_S = 5.0
QUEUE_S = 5.0
PTIME = 0.02                    # the bot's frames are 20 ms


def live(manager):
    """The calls a capture could open now, by id."""
    return sorted(int(k) for k, s in list(manager.sessions.items()) if getattr(s, "pc", None))


def _reply(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())


class Tap:
    """One listener's queue on a session. The bot calls put() for every frame
    (never blocking); end() says the call is over."""

    def __init__(self):
        self.q = asyncio.Queue(maxsize=int(QUEUE_S / PTIME))
        self.dropped = 0

    def put(self, pcm: bytes):
        try:
            self.q.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self.q.get_nowait()          # drop the oldest: stay live, not complete
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
            self.q.put_nowait(pcm)

    def end(self, why="call-ended"):
        while True:
            try:
                self.q.put_nowait(("end", why))
                return
            except asyncio.QueueFull:
                self.q.get_nowait()


def feed(session, pcm: bytes):
    """From the session's listen loop: hand one frame to every tap."""
    for t in list(getattr(session, "ear_taps", ())):
        t.put(pcm)


def end(session, why="call-ended"):
    """From the session's close: every tap hears the end."""
    for t in list(getattr(session, "ear_taps", ())):
        t.end(why)


def _tap_row(s, row):
    """One line of <call dir>/ear-taps.jsonl (0600). Never raises: a call with
    no dir yet, or a full disk, loses the row, not the call."""
    d = getattr(s, "dir", None)
    if d is None:
        return
    try:
        path = pathlib.Path(d) / "ear-taps.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps({"t": round(time.time(), 3), **row}) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


async def serve_one(manager, reader, writer, log=print):
    s, tap, call, sent, ended = None, None, None, 0, "eof"
    try:
        try:
            raw = await asyncio.wait_for(reader.readline(), HEADER_TIMEOUT_S)
            hdr = json.loads(raw)
            if not isinstance(hdr, dict):
                raise ValueError("not an object")
        except Exception as e:  # noqa: BLE001
            _reply(writer, {"ok": False, "error": f"bad header: {type(e).__name__}"})
            ended = "refused"
            return
        if hdr.get("probe"):
            _reply(writer, {"ok": True, "agent": dc_env.AGENT, "profile": PROFILE,
                            "calls": live(manager), "rate": RATE})
            ended = "probe"
            return
        have = live(manager)
        want = hdr.get("call")
        try:
            call = int(want) if want is not None else (have[-1] if have else None)
        except (TypeError, ValueError):
            call = None
        s = manager.sessions.get(call) if call is not None else None
        if s is None or not getattr(s, "pc", None):
            _reply(writer, {"ok": False, "error": f"call {want} is not live here", "calls": have})
            ended = "refused"
            return
        tap = Tap()
        if not hasattr(s, "ear_taps"):
            s.ear_taps = []
        s.ear_taps.append(tap)
        # same loop as feed(): no frame lands between this count and the append above
        wav_s = round(getattr(s, "wav_samples", 0) / RATE, 3)
        _tap_row(s, {"call": call, "wav_s": wav_s})
        _reply(writer, {"ok": True, "call": call, "rate": RATE, "format": "s16le", "channels": 1,
                        "chat": getattr(s, "chat_id", None), "chat_name": getattr(s, "chat_name", None),
                        "wav_s": wav_s})
        await writer.drain()
        log(f"EAR listener on call {call}")
        while True:
            item = await tap.q.get()
            if isinstance(item, tuple):
                why = str(item[1]).encode()
                writer.write(b"E" + struct.pack(">I", len(why)) + why)
                await writer.drain()
                ended = "call-ended"
                break
            writer.write(b"A" + struct.pack(">I", len(item)) + item)
            sent += len(item) // 2
            if tap.q.empty():
                await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        ended = "listener-gone"
    except Exception:  # noqa: BLE001
        ended = "error"
        log("ear leg failed:\n" + traceback.format_exc())
    finally:
        if s is not None and tap is not None:
            try:
                s.ear_taps.remove(tap)
            except (AttributeError, ValueError):
                pass
            log(f"EAR listener {ended} on call {call}: {sent / RATE:.1f}s sent"
                + (f", {tap.dropped} frame(s) dropped behind" if tap.dropped else ""))
            _tap_row(s, {"call": call, "end": ended, "sent_s": round(sent / RATE, 3),
                         "dropped": tap.dropped})
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def serve(manager, path=None, log=print):
    """Bind the socket (0600, replacing a stale one) and serve it on this loop."""
    path = pathlib.Path(path or SOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    # NOT os.umask() around the await (as mouthleg does): umask is process-wide, and the
    # two legs start together on one loop - their save/restore interleaved and left the
    # bot at 0o177, so every new call dir was 0600 and a dial failed.
    # The socket is chmod-ed at once, inside a 0700 delta-chat dir.
    server = await asyncio.start_unix_server(
        lambda r, w: serve_one(manager, r, w, log), path=str(path))
    os.chmod(path, 0o600)
    log(f"ear leg: listening on {path}")
    return server


def start(manager, log=print, path=None):
    """From any thread: serve the socket on the calls loop."""
    return asyncio.run_coroutine_threadsafe(serve(manager, path, log), manager.loop)

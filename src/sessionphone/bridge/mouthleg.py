"""The mouth leg: VoiceMode's mouth speaks INTO a live call.

The user calls the bot and the call is ADDED to the ears. Then the agent's
voice has to reach the call. dc-say can already put words in a call, but the
bot synthesises them there, so nothing outside can hold them, cut them or
queue them. Here the mouth does all of that itself (hold, barge-in, retract,
the playlist) and hands the bot finished audio, one line per connection. The
mouth device is ``call:<N>``, where N is the call's id (the invite message's
id, as with ``dc-say --call``).

THE SOCKET: ``<ROOT>/call-mouth.sock``, or ``<ROOT>/<PROFILE>/call-mouth.sock``
for a named profile (DC_MOUTH_SOCK overrides). Mode 0600, served on the calls
loop. One connection is one exchange:

  probe:  mouth -> {"probe": true}\\n
          bot   -> {"ok": true, "agent": "claude", "profile": "", "calls": [N, ...], "sounds": true}\\n
          ("sounds": this bot mixes sounds; a bot without it would take a sound for a line)

  a line: mouth -> {"call": N, "rate": 48000, "text": "...", "who": "Claude"}\\n
          bot   -> {"ok": true, "call": N}\\n   or {"ok": false, "error": "..."}\\n
          mouth -> frames: 1 byte kind + 4 byte big-endian length + payload
                     A  int16 little-endian mono PCM at 48 kHz, queued on the call
                     D  done: what is queued plays out
                     C  cut: drop what is queued, now
          bot   -> {"ok": true|false, "ended": "done|cut|eof|error|call-ended", ...}\\n

  a sound: mouth -> {"call": N, "rate": 48000, "sound": true, "name": "turn"}\n
          bot   -> {"ok": true, "call": N, "sound": true}\n
          mouth -> A frames (the whole sound, at once: no pacing), then D
          bot   -> {"ok": true, "ended": "done", "sound": true, "mixed_s": ...}\n
     A sound is MIXED over whatever the call is playing (PipVoiceTrack.mix): it
     never queues behind a line, never reaches the transcript or the echo guard,
     and neither its end nor an EOF cuts anything. Sounds follow the ears into
     the call.

EOF before D is a cut: a mouth that died must not leave words queued.
Pacing is the mouth's job (it writes in real time, a little ahead of the
track), so a cut drops a fraction of a second, not the rest of the line.

A cut empties the call's whole outgoing queue (PipVoiceTrack.cut), the bot's
own lines included: one voice track, one queue. Barge-in does the same.
"""
import asyncio
import json
import os
import pathlib
import struct
import time
import traceback

import numpy as np

from sessionphone.bridge import dc_env

PROFILE = dc_env.env("PROFILE", "")
SOCK = dc_env.path("MOUTH_SOCK", *([PROFILE] if PROFILE else []), "call-mouth.sock")
RATE = 48000                       # what the call's voice track holds (OUT_RATE)
HEADER_TIMEOUT_S = 5.0
FRAME_MAX = RATE * 2 * 5           # 5 s of audio in one frame is a broken writer
ECHO_EVERY_S = 0.5                 # hand the echo guard a clip this often


def live(manager):
    """The calls this bot could speak into now, by id."""
    return sorted(int(k) for k, s in list(manager.sessions.items()) if getattr(s, "pc", None))


def _reply(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())


async def _sound(s, call, hdr, reader, writer, log):
    """One sound into a live call: read A frames until D, mix each over the track."""
    name, mixed, ended, detail = str(hdr.get("name") or "sound")[:40], 0.0, "eof", None
    _reply(writer, {"ok": True, "call": call, "sound": True})
    await writer.drain()
    try:
        while True:
            head = await reader.readexactly(5)
            kind, n = head[:1], struct.unpack(">I", head[1:])[0]
            if n > FRAME_MAX:
                raise ValueError(f"frame of {n} bytes")
            payload = await reader.readexactly(n) if n else b""
            if kind == b"A":
                if n % 2:
                    raise ValueError("odd PCM length")
                if getattr(s, "pc", None):
                    mixed += s.voice.mix(np.frombuffer(payload, dtype="<i2"))
            elif kind in (b"D", b"C"):
                ended = "done"
                break
            else:
                raise ValueError(f"unknown frame kind {kind!r}")
    except (asyncio.IncompleteReadError, ConnectionError):
        ended = "eof"
    except Exception as e:  # noqa: BLE001
        ended, detail = "error", f"{type(e).__name__}: {e}"
    out = {"ok": ended == "done", "ended": ended, "sound": True, "call": call, "mixed_s": round(mixed, 3)}
    if detail:
        out["error"] = detail
        log(f"MOUTH sound {name} into call {call} failed: {detail}")
    try:
        _reply(writer, out)
        await writer.drain()
    except Exception:  # noqa: BLE001 - the mouth may be gone; the sound is already mixed
        pass
    return ended, mixed


async def serve_one(manager, reader, writer, log=print):
    s = None
    ended, detail = "eof", None
    queued = 0                     # samples handed to the track
    unechoed = 0                   # samples not yet told to the echo guard
    dropped_s, where = 0.0, None
    text, who, call, t0 = "", dc_env.AGENT_NAME, None, time.time()
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
                            "calls": live(manager), "sounds": True})   # the mouth asks before a sound
            ended = "probe"
            return
        try:
            call = int(hdr["call"])
            rate = int(hdr.get("rate", RATE))
        except Exception:  # noqa: BLE001
            _reply(writer, {"ok": False, "error": "the header needs a numeric call"})
            ended = "refused"
            return
        if rate != RATE:
            _reply(writer, {"ok": False, "error": f"rate {rate}: send {RATE} Hz"})
            ended = "refused"
            return
        cand = manager.sessions.get(call)
        if cand is None or not getattr(cand, "pc", None):
            have = live(manager)
            _reply(writer, {"ok": False, "error": f"call {call} is not live here",
                            "calls": have})
            ended = "refused"
            return
        if hdr.get("sound"):     # before `s` is set: the finally below must not cut or transcribe it
            ended, _ = await _sound(cand, call, hdr, reader, writer, log)
            return
        s = cand
        text = str(hdr.get("text") or "")
        who = str(hdr.get("who") or dc_env.AGENT_NAME)
        who = who.capitalize() if who.islower() else who   # the mouth sends the agent name in lowercase
        _reply(writer, {"ok": True, "call": call})
        replied = getattr(s, "replied", None)   # a reply line arrived (never a sound)
        if replied is not None:
            replied("mouth")
        await writer.drain()
        log(f"MOUTH line into call {call} ({who}): {text[:60]}")
        while True:
            head = await reader.readexactly(5)
            kind, n = head[:1], struct.unpack(">I", head[1:])[0]
            if n > FRAME_MAX:
                raise ValueError(f"frame of {n} bytes")
            payload = await reader.readexactly(n) if n else b""
            if not getattr(s, "pc", None):
                ended, detail = "call-ended", f"call {call} ended mid-line"
                break
            if kind == b"A":
                if n % 2:
                    raise ValueError("odd PCM length")
                pcm = np.frombuffer(payload, dtype="<i2").astype(np.int16)
                s.voice.say(pcm)
                queued += len(pcm)
                unechoed += len(pcm)
                if unechoed >= ECHO_EVERY_S * RATE:
                    s.echo.spoken(text, unechoed / RATE, source="mouth")
                    unechoed = 0
            elif kind == b"D":
                ended = "done"
                break
            elif kind == b"C":
                ended = "cut"
                break
            else:
                raise ValueError(f"unknown frame kind {kind!r}")
    except (asyncio.IncompleteReadError, ConnectionError):
        ended = "eof"
    except Exception as e:  # noqa: BLE001
        ended, detail = "error", f"{type(e).__name__}: {e}"
        log("mouth leg failed:\n" + traceback.format_exc())
    finally:
        if s is not None:
            if unechoed:
                s.echo.spoken(text, unechoed / RATE, source="mouth")
            if ended != "done":
                dropped_s = s.voice.cut()
                try:
                    where = str(s.echo.cut())
                except Exception:  # noqa: BLE001
                    where = None
            s.lines.append((t0, who, text if ended == "done" else f"{text} [cut]"))
            log(f"MOUTH line {ended} in call {call}: {queued / RATE:.1f}s queued, "
                f"{dropped_s:.1f}s dropped" + (f" ({detail})" if detail else ""))
            out = {"ok": ended == "done", "ended": ended, "call": call,
                   "queued_s": round(queued / RATE, 3), "dropped_s": round(dropped_s, 3)}
            if where:
                out["where"] = where
            if detail:
                out["error"] = detail
            try:
                _reply(writer, out)
                await writer.drain()
            except Exception:  # noqa: BLE001 - the mouth may be gone; that was a cut
                pass
        try:
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        writer.close()


async def serve(manager, path=None, log=print):
    """Bind the socket (0600, replacing a stale one) and serve it on this loop."""
    path = pathlib.Path(path or SOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    old = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(
            lambda r, w: serve_one(manager, r, w, log), path=str(path))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    log(f"mouth leg: listening on {path}")
    return server


def start(manager, log=print, path=None):
    """From any thread: serve the socket on the calls loop."""
    return asyncio.run_coroutine_threadsafe(serve(manager, path, log), manager.loop)

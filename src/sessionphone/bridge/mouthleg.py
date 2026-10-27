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

ONE LINE AT A TIME. A line is one line on the call's voice track
(PipVoiceTrack.line), from its header to its D, and the bot's replies are
lines too (one per reply). The line that arrived first owns the track until
it ends; a line that arrives while another is speaking waits behind it, its
A frames buffered, and plays whole when its turn comes. It is never spliced
into the other, and the other is never spliced into it. Waiting costs the
mouth nothing: every frame is still read at once, and D is answered at once.

A cut empties the call's whole outgoing queue (PipVoiceTrack.cut), the bot's
own lines included, and the lines waiting behind it: one voice track, one
queue. Barge-in does the same. A C, an EOF before D, an error or the call
ending is a cut while the line is still live (speaking or waiting). A line
already cut (a barge-in mid-line) drops the rest of its frames, cuts nothing
more, and ends "cut" at its D.
"""
import asyncio
import bisect
import json
import os
import pathlib
import struct
import time
import traceback

import numpy as np

from sessionphone.bridge import dc_env

PROFILE = dc_env.env("PROFILE", "")
SOCK = dc_env.path("MOUTH_SOCK", "call-mouth.sock")   # under the profile's home: <ROOT>/<PROFILE>/ for a named one
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
    unechoed = 0                   # samples on the track, not yet told to the echo guard
    echo_from = None               # when those samples start to play
    lost = 0                       # samples dropped: the line was already cut
    line = None
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
        text = str(hdr.get("text") or "")
        who = str(hdr.get("who") or dc_env.AGENT_NAME)
        who = who.capitalize() if who.islower() else who   # the mouth sends the agent name in lowercase
        line = cand.voice.line(who, floor=True)   # its place in the call's lines: arrival order;
                                                  # a reply: it waits for the caller's quiet
        s = cand

        def on_queue(start_at, secs):   # A frames reach the track: now, or at this line's turn
            nonlocal unechoed, echo_from
            if not unechoed:
                echo_from = start_at
            unechoed += round(secs * RATE)
            if unechoed >= ECHO_EVERY_S * RATE:
                flush_echo()

        def flush_echo():
            nonlocal unechoed
            if unechoed:
                s.echo.spoken(text, unechoed / RATE, source="mouth", start=echo_from)
                unechoed = 0
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
                queued += len(pcm)
                if not line.say(pcm, on_queue):
                    lost += len(pcm)       # cut mid-line (a barge-in): the rest is not said
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
            if line.was_cut:               # a barge-in cut it mid-line
                if ended == "done":
                    ended = "cut"
                # The echo guard was cut with the track. Telling it now would
                # open an echo window over audio the cut already dropped.
                unechoed = 0
            if ended == "done":
                line.end(flush_echo)       # what is queued plays out; the next line follows it
            else:
                flush_echo()               # what reached the track, before the cut trims it
                cut_s = line.cut()         # None: already cut, and the next line is not ours to cut
                if cut_s is not None:
                    dropped_s = cut_s
                    try:
                        where = str(s.echo.cut())
                    except Exception:  # noqa: BLE001
                        where = None
            dropped_s += lost / RATE
            # filed at its stamp's place, not at the end: a caller turn heard while it
            # played is already in, stamped later (the transcript reads in time order)
            bisect.insort(s.lines, (t0, who, text if ended == "done" else f"{text} [cut]"),
                          key=lambda r: r[0])
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

"""firstcall - the first call's wait, while the speech models download.

On a Mac where the speech models are not downloaded yet, the first call is
silent for about a minute: the greeting's text-to-speech, and the first
transcription, wait for the server to fetch its models. So a call that
connects while they are missing hears a line saying so, then music, and the
music stops at once when the call can hear.

- **When.** CallSession._first_call_wait runs wait() BESIDE the greeting (the
  greeting is not delayed: its TTS call is what blocks on the models). It
  asks hearing.weights_state() first. None (no reader, an older ears) or
  `.wait` False (the stt and tts weights are cached): it returns, and the call
  is exactly as it was. DC_FIRST_CALL_WAIT=off skips it altogether.
- **The line.** WAIT_LINE, rendered at call time by macOS `say` (the bot's
  own TTS fallback, which needs no model), with the size of what is still to
  fetch when the reader knows it. No `say`: no line.
- **The music.** DC_WAIT_MUSIC (default `<root>/wait-music.opus`), any file
  ffmpeg reads, decoded once per bot (cached by path and mtime), turned down
  GAIN_DB and looped with no gap as the call track's bed (PipVoiceTrack.hold).
  It starts when the line ends. Missing or unreadable: no music, logged once.
  Nothing ships with the package.
- **No music.** The line, then a progress line every REPEAT_S
  (`Still downloading: 1.2 GB of 2.9 GB.`), silence between. No tone.
- **The stop.** The first of: the bot's own STT answers the probe
  (hearing.probe, one at a time, every POLL_S); a real line is queued (the
  greeting came back, or a mouth line: the call is talking now); the call
  ends (the task is cancelled); CAP_S. The bed goes at the next frame, with no
  fade. The weights are re-read only when a progress line is due (no music),
  to keep it current: a weights read can say `cached` while the server is
  still loading, so it is never the stop on its own.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

import numpy as np

from sessionphone import hearing

# The words are open: changing them means editing this one string.
WAIT_LINE = "Please wait for the voice model to download{about}. This only happens once."
POLL_S = 3.0             # the probe's and the weights' interval
REPEAT_S = 20.0          # no music: the progress line's interval
CAP_S = 20 * 60.0        # the wait never runs longer than this
GAIN_DB = -8.0           # the music, under the line
RATE = 48000             # the call track's rate (calls.OUT_RATE)
DECODE_TIMEOUT_S = 60
CLOCK = time.monotonic   # the wait's clock (the cap, the repeat); tests replace it


def wait_line(w: hearing.Weights) -> str:
    """WAIT_LINE with `, about 2.9 GB` when the size still to fetch is known."""
    return WAIT_LINE.format(about=f", about {hearing.size(w.size)}" if w.size is not None else "")


def progress_line(w: hearing.Weights) -> str:
    """`Still downloading: 1.2 GB of 2.9 GB.`, or `Still downloading, 412 MB so
    far.` when the total is not known; while nothing is downloading, no numbers."""
    if w.state != "downloading":
        return "Still waiting for the voice model to download."
    if w.total is None:
        return f"Still {hearing.downloading(w)}."
    return f"Still downloading: {hearing.size(w.done)} of {hearing.size(w.total)}."


def gain(pcm, db: float = GAIN_DB):
    """int16 PCM turned down (or up) by db decibels."""
    x = np.asarray(pcm, dtype=np.int16).astype(np.float32) * (10 ** (db / 20))
    return np.clip(x, -32768, 32767).astype(np.int16)


def ffmpeg_decode(path, ffmpeg: str = "ffmpeg"):
    """Any file ffmpeg reads -> int16 mono 48 kHz. Raises on failure."""
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                        "-ar", str(RATE), "pipe:1"], capture_output=True, timeout=DECODE_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg rc={r.returncode}: {(r.stderr or b'')[:200]!r}")
    return np.frombuffer(r.stdout, dtype="<i2").astype(np.int16)


DECODE = ffmpeg_decode   # how the music file is decoded; tests replace it
_cache: dict = {}        # (path, mtime) -> the decoded, turned-down PCM
_said: set = set()       # the paths whose absence or failure has been logged


def music(path, log=print, ffmpeg: str = "ffmpeg"):
    """The wait music from path, decoded and turned down GAIN_DB, or None.
    None when the file is missing, unreadable or empty, logged once per path.
    Blocking: run it in an executor."""
    path = os.fspath(path)
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        if path not in _said:
            _said.add(path)
            log(f"first-call wait: no music file at {path}; the line alone")
        return None
    if key not in _cache:
        try:
            pcm = DECODE(path, ffmpeg)
        except Exception as e:
            if path not in _said:
                _said.add(path)
                log(f"first-call wait: the music file {path} could not be decoded ({e}); the line alone")
            return None
        _cache.clear()           # one file is kept: a changed file replaces the old one
        _cache[key] = gain(pcm) if len(pcm) else None
    return _cache[key]


async def wait(voice, *, weights, probe, render=None, load_music=None, log=print,
               clock=None) -> str:
    """The first call's wait on voice (the call's PipVoiceTrack). -> why it
    ended, as logged; "not waiting" when the models need no wait.

    weights() -> hearing.Weights or None; probe() -> True when the call's STT
    answers; render(text) -> int16 48 kHz PCM, or None for no line (no `say`);
    load_music() -> the bed's PCM, or None. The blocking ones run in the
    default executor. A cancel (the call ended) unholds and re-raises."""
    loop = asyncio.get_running_loop()
    clock = clock or CLOCK
    base = voice.said_n          # lines queued before the wait: none are ours

    def run(fn, *a):
        return loop.run_in_executor(None, fn, *a)

    w = await run(weights)
    if w is None or not w.wait:
        return "not waiting"
    start = clock()
    own = 0                      # the lines the wait queued itself

    def talking():
        return voice.said_n > base + own

    async def line(text):
        if render is None:
            return None
        try:
            return await run(render, text)
        except Exception as e:
            log(f"first-call wait: the line could not be rendered ({e})")
            return None

    first = await line(wait_line(w))
    bed = await run(load_music) if load_music is not None else None
    if talking():
        log("first-call wait: the call was already talking; no wait")
        return "a line"
    if first is None and bed is None:
        log(f"first-call wait: the models are {w.state}, but there is no line (no say) and no music")
        return "nothing to play"
    log(f"first-call wait: the models are {w.state}"
        f"{f' (about {hearing.size(w.size)} to fetch)' if w.size is not None else ''}; "
        f"{'the line' if first is not None else 'no line'}, then "
        f"{'the music' if bed is not None else f'the progress line every {REPEAT_S:g} s'}")
    if first is not None:
        voice.say(first)
        own += 1
    if bed is not None:
        voice.hold(bed)
    said_at = clock()
    asked, next_ask = None, clock()   # the probe in flight (one at a time), and when the next goes
    why = None
    try:
        while True:
            if talking() or (bed is not None and voice.bed is None):
                why = "a line"
            elif clock() - start >= CAP_S:
                why = f"the {CAP_S / 60:g} min cap"
            if why is not None:
                break
            if asked is None and clock() >= next_ask:
                asked = run(probe)
            if bed is None and first is not None and clock() - said_at >= REPEAT_S:
                w = await run(weights) or w          # fresh progress for the line
                pcm = await line(progress_line(w))
                if pcm is not None and not talking():
                    voice.say(pcm)
                    own += 1
                said_at = clock()
            if asked is None:
                await asyncio.sleep(POLL_S)
                continue
            # the probe's answer ends the wait at once, not at the next poll
            await asyncio.wait({asked}, timeout=POLL_S)
            if not asked.done():
                continue
            try:
                heard = asked.result()
            except Exception as e:
                log(f"first-call wait: the probe failed ({e})")
                heard = False
            asked, next_ask = None, clock() + POLL_S
            if heard:
                why = "hearing answers"
                break
    except asyncio.CancelledError:
        voice.unhold()
        log(f"first-call wait: ended (the call ended) after {clock() - start:.0f} s")
        raise
    voice.unhold()
    log(f"first-call wait: ended ({why}) after {clock() - start:.0f} s")
    return why

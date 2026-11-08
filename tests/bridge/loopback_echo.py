#!/usr/bin/env python3
"""Loopback echo rig: does the bot cut itself off on its own echo?

A leaky handset, without a person. A fake phone (an aiortc peer, as in
loopback.py) rings the real CallSession. Its microphone is the caller's line
MIXED with the bot's own outgoing audio, as the phone received it, fed back
attenuated (--gain, dB) and delayed (--delay, ms). The call runs at the
defaults: DC_BARGE_IN and DC_EARS are removed, so barge-in is on and the
energy-VAD path listens.

One call: the greeting, then one long reply line (BOT_TEXT). With a caller,
his line (CALLER_TEXT) starts --caller-at seconds into that reply; with
--echo-only he is silent, so every BARGE-IN is a self-cut.

THE VOICE, by default a STUB, in-process; nothing reaches a speech server.
The stub TTS says each line as a chirp: one frequency band per line, rising
through the band as the line goes on. The stub STT reads a chunk's audio
back, 20 ms at a time: the loudest tone it finds above its hearing floor
names a line and a place in it, and so a word. Everything between the two
is real: Opus both ways, the bridge's resampler, its energy VAD and
chunking, the echo guard, the barge-in gate. But what an echo "transcribes
to" is the STUB's decision (its codebook and its floor), so stub numbers say
how the bridge's gates behave on audio whose words are known; they are NOT a
measurement of self-cuts on real speech.

--speech-url URL (OPT-IN; it reaches a speech server): real TTS and STT on
the mlx-audio server at URL, for the bot, the caller's clip and every chunk.
Nothing else changes, and no URL is assumed.

Each BARGE-IN (and each held one) is judged two ways. By TIME: did the chunk
it fired on overlap the caller's line? By TEXT: is its transcript closer to
his words or the bot's? A self-cut is a BARGE-IN whose chunk held no caller
audio. The result also counts ECHO drops and echo that reached his turn.

ISOLATED: before any sessionphone import, HOME is a fresh tmp dir (so the
kit's state, log and settings roots, the echo spool and voicemode's event
log are under it), every DC_*/PIP_CHAT_*/ECHO_* and seat variable is
dropped, and the caller is a key-pinned fake on a fake account. No bot, no
relay, no mail. Each run snapshots the real home before and after (the
suite's guard, tests/_home_guard.py) and FAILS if it changed; it also lists
every socket it connected, every send or name lookup bound off this machine
(net_off_host), and every process it started.

Usage:
  .venv/bin/python tests/bridge/loopback_echo.py --gain -30 --delay 120
  .venv/bin/python tests/bridge/loopback_echo.py --gain -30 --delay 120 --echo-only
  .venv/bin/python tests/bridge/loopback_echo.py --grid --keep DIR --out DIR/results.json
       (--grid: every --gains x --delays, with and without a caller, one call
        at a time, each in its own process)

Exit: 0 the call ran; 1 it did not; 3 SKIP (--speech-url does not answer);
4 the real home changed.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from collections import deque
from urllib.parse import urlparse

import numpy as np

RATE = 48000                      # the phone's side: Opus at 48 kHz
FRAME = 960                       # 20 ms
SKIP, HOME_CHANGED = 3, 4         # exit codes
HERE = pathlib.Path(__file__).resolve().parent
CALLER = "owner@example.org"
CALLER_VOICE = "am_michael"       # --speech-url: not the bot's af_sky, two people
CALLER_TEXT = "Hold on, stop there, I want to ask about the other thing first."
BOT_TEXT = ("Here is where the build stands this morning. The first two parts "
            "landed overnight and both of them passed review. The third part is "
            "the documentation, which is waiting on one more careful read. After "
            "that comes the loopback rig, which plays my own voice back into the "
            "call. It tells us how often I would cut myself off on a leaky phone. "
            "When it is finished, the numbers go to the super in the morning report.")

# the seat's environment, dropped before the bridge imports (as tests/conftest.py)
SEAT_ENV = ("CLAUDE_CODE_AGENT", "SESSIONPHONE_ROOT", "EMAIL",
            "SESSIONMAIL_HOST", "SESSION_MAIL_HOST", "XDG_CONFIG_HOME",
            "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")
SEAT_PREFIXES = ("DC_", "PIP_CHAT_", "ECHO_")
# a stub run's tripwire: should anything still try a speech URL, it is the
# discard port on loopback, refused at once, never the real service
NOWHERE = "http://127.0.0.1:9"


# ---------------------------------------------------------------- isolation

def isolate(home=None):
    """HOME and every root under a tmp dir, the seat's variables gone. Call it
    before ANY sessionphone import: dc_env and paths bind their roots then.
    -> (home, root)."""
    early = sorted(m for m in sys.modules if m == "sessionphone" or m.startswith("sessionphone."))
    if early:
        raise RuntimeError(f"loopback_echo: sessionphone imported before isolation: {early}")
    for k in [k for k in os.environ if k in SEAT_ENV or k.startswith(SEAT_PREFIXES)]:
        del os.environ[k]
    home = pathlib.Path(home or tempfile.mkdtemp(prefix="dc-echo-home-")).resolve()
    root = home / ".sessionkit" / "phone" / "state"          # paths.DEFAULT_ROOT under it
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share"),
        "SESSIONPHONE_ROOT": str(root), "DC_ROOT": str(root),
        "DC_AGENT": "loopback",
        "SESSIONMAIL_HOST": "loopback.invalid",   # ids minted here; sessionmail is not asked
        # the first-ever-call wait (models downloading) is not under test:
        # under a fresh HOME it would probe the STT and play a say line
        "DC_FIRST_CALL_WAIT": "off",
        # the bot's own box, named here: unset, bot_box() runs `message path`
        "DC_BOT_BOX": str(home / "no-bot-box"),
    })
    return home, root


def real_home_snapshot(home):
    """The suite's guard (tests/_home_guard.py), plus the kit's voice folder
    the call reads (recording settings). Stats only; reads and writes nothing."""
    sys.path.insert(0, str(HERE.parent))
    import _home_guard
    snap = _home_guard.snapshot(home)
    voice = pathlib.Path(home) / ".sessionkit" / "voice"
    for d, _, files in os.walk(voice):
        for p in [d] + [os.path.join(d, f) for f in files]:
            try:
                st = os.lstat(p)
                snap[p] = (st.st_mtime_ns, st.st_size, st.st_mode)
            except OSError:
                pass
    return snap


def changed(before, after):
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def off_host(host, own):
    """True when HOST is somewhere off this machine: not loopback, not one of
    its own addresses (OWN), not the any-address. A NAME (not an IP) counts:
    resolving it is already asking the network. None is a local bind."""
    import ipaddress
    if host is None:
        return False
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    host = str(host)
    if host in ("", "localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return True
    return not (ip.is_loopback or ip.is_unspecified or str(ip) in own)


def net_audit(own):
    """An audit hook that records the run's network and processes. Returns
    (hook, net, procs, away): every connect() target, every process started,
    and AWAY, each send, connect or name lookup bound off this machine (OWN
    is its own addresses). connect() alone misses UDP: ICE (STUN) sends with
    sendto(), after aioice resolves the server with gethostbyname(), not
    getaddrinfo()."""
    net, procs, away, judged = [], [], set(), {}

    def leaves(host):
        if host not in judged:
            judged[host] = off_host(host, own)
        return judged[host]

    def audit(event, args):
        if event == "socket.connect":
            net.append(repr(args[1]))
            addr = args[1]
            if isinstance(addr, tuple) and leaves(addr[0]):
                away.add(f"connect {addr[0]}:{addr[1]}")
        elif event == "socket.sendto":
            addr = args[1]
            if isinstance(addr, tuple) and leaves(addr[0]):
                away.add(f"sendto {addr[0]}:{addr[1]}")
        elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            if leaves(args[0]):
                away.add(f"{event.split('.')[1]} {args[0]!r}")
        elif event == "subprocess.Popen":
            procs.append(os.path.basename(str(args[0])))
    return audit, net, procs, away


# ---------------------------------------------------------------- the handset

def db(gain_db):
    """dB -> linear amplitude."""
    return 10.0 ** (gain_db / 20.0)


def mono16(a, channels):
    """A decoded frame's samples (packed s16) as one mono int16 channel."""
    a = np.asarray(a).reshape(-1)
    if channels == 2:
        a = a.reshape(-1, 2).astype(np.int32).mean(axis=1)
    return a.astype(np.int16)


class EchoPath:
    """What the phone's speaker sends back into its own microphone: the bot's
    audio, `gain_db` down and `delay_s` late. Fed as the phone hears it
    (heard()), read as the mic samples (take()), both on a clock."""

    def __init__(self, gain_db, delay_s):
        self.gain, self.delay = db(gain_db), delay_s
        self.arrived = deque()                    # (time heard, int16 samples)
        self.pending = np.zeros(0, dtype=np.int16)

    def heard(self, pcm, at):
        self.arrived.append((at, np.asarray(pcm, dtype=np.int16)))

    def take(self, n, now):
        """n samples of echo for the mic frame at `now`: what played `delay` ago."""
        while self.arrived and self.arrived[0][0] <= now - self.delay:
            self.pending = np.concatenate([self.pending, self.arrived.popleft()[1]])
        out = np.zeros(n, dtype=np.float32)
        k = min(n, len(self.pending))
        out[:k] = self.pending[:k] * self.gain
        self.pending = self.pending[k:]
        return out


class Mic:
    """The phone's microphone: the caller's clip (from `start`, once set) plus
    the echo. Mixed in float, clipped to int16. Keeps per-frame energy of each
    part, so a run can say how loud the echo was against the caller."""

    def __init__(self, clip, echo):
        self.clip, self.echo = np.asarray(clip, dtype=np.int16), echo
        self.start = None                         # wall time the caller starts speaking
        self.at = 0
        self.echo_rms, self.caller_rms = [], []

    def caller_window(self):
        return None if self.start is None else (self.start, self.start + len(self.clip) / RATE)

    def frame(self, now):
        e = self.echo.take(FRAME, now)
        c = np.zeros(FRAME, dtype=np.float32)
        if self.start is not None and now >= self.start and self.at < len(self.clip):
            k = min(FRAME, len(self.clip) - self.at)
            c[:k] = self.clip[self.at:self.at + k]
            self.at += k
        if np.any(e):
            self.echo_rms.append(float(np.sqrt(np.mean(e ** 2))))
        if np.any(c):
            self.caller_rms.append(float(np.sqrt(np.mean(c ** 2))))
        return np.clip(c + e, -32768, 32767).astype(np.int16)


# calls.Utterances opens a chunk only above max(350, 3.5 x its floor) rms:
# echo quieter than this never becomes a chunk on its own
VAD_START = 350.0


def dbfs(rms_list, pct=50):
    """Frame energy at percentile PCT (the median by default), dBFS; None when there was none."""
    if not rms_list:
        return None
    return round(20 * np.log10(max(float(np.percentile(rms_list, pct)), 1e-9) / 32768.0), 1)


# ---------------------------------------------------------------- the stub voice

class ToneVoice:
    """The stub TTS and STT: one codebook, so what is said can be read back.

    Line k (k=0 is the caller's, then each new line the bot says) is a tone
    whose frequency rises through its own band, BAND0 + k*BAND_W, across the
    first CHIRP of that band, as the line goes on: the frequency names the
    line and the place in it, and the place names a word. A syllable-rate
    swell (SWELL Hz) rides on it, and LEAD/TAIL seconds of silence frame it,
    so the VAD closes a chunk between lines as it did on real speech.

    hear() is the STT: for each 20 ms frame above FLOOR rms it takes the
    LOUDEST tone (a louder line masks a quieter one, as on a real line), and
    a word counts once it is the loudest for MIN_FRAMES frames. Its words come
    back in order. FLOOR is what it can hear: the stub's choice, not a fact."""

    BAND0, BAND_W, CHIRP = 400.0, 240.0, 0.8
    LEVEL = 3000.0              # tone peak: frames at about -30..-24 dBFS, like the TTS speech measured
    SWELL = 4.0
    LEAD, TAIL = 0.20, 0.30
    CPS = 15.0                  # characters a second, as the TTS spoke the BOT_TEXT lines
    FLOOR = 60.0                # rms, about -55 dBFS: quieter than this, the stub hears nothing
    MIN_FRAMES = 2
    NFFT = 8192

    def __init__(self):
        self.texts = [CALLER_TEXT]

    def code(self, text):
        if text not in self.texts:
            self.texts.append(text)
        return self.texts.index(text)

    def say(self, text, rate=RATE):
        """text -> int16 at RATE: line k's chirp, framed in silence."""
        k = self.code(text)
        n = int(max(0.4, len(text) / self.CPS) * rate)
        t = np.arange(n) / rate
        f0 = self.BAND0 + k * self.BAND_W
        f = f0 + self.CHIRP * self.BAND_W * t / t[-1]
        phase = 2 * np.pi * np.cumsum(f) / rate
        swell = 0.55 + 0.45 * np.sin(2 * np.pi * self.SWELL * t)
        tone = (self.LEVEL * swell * np.sin(phase)).astype(np.int16)
        z = lambda s: np.zeros(int(s * rate), dtype=np.int16)
        return np.concatenate([z(self.LEAD), tone, z(self.TAIL)])

    def peak(self, x, rate):
        """The loudest frequency in X, Hz (parabolic between FFT bins)."""
        w = np.hanning(len(x))
        spec = np.abs(np.fft.rfft(x * w, self.NFFT))
        lo = int(self.BAND0 * 0.9 * self.NFFT / rate)
        i = lo + int(np.argmax(spec[lo:]))
        if 0 < i < len(spec) - 1:
            a, b, c = np.log(spec[i - 1] + 1e-9), np.log(spec[i] + 1e-9), np.log(spec[i + 1] + 1e-9)
            d = a - 2 * b + c
            i = i + (0.5 * (a - c) / d if d else 0.0)
        return i * rate / self.NFFT

    def word_at(self, freq):
        """(line, word index) the frequency names; None outside every band."""
        k, off = divmod(freq - self.BAND0, self.BAND_W)
        k = int(k)
        if not 0 <= k < len(self.texts) or off > self.CHIRP * self.BAND_W + 4.0:
            return None
        words = self.texts[k].split()
        return k, min(int(off / (self.CHIRP * self.BAND_W) * len(words)), len(words) - 1)

    def hear(self, pcm, rate=16000):
        """int16 audio -> the words its loudest tones name, in order ('' for none)."""
        x = np.asarray(pcm, dtype=np.float32)
        step = int(rate * 0.02)
        runs, last, count = [], None, 0
        for i in range(0, len(x) - step + 1, step):
            fr = x[i:i + step]
            if np.sqrt(np.mean(fr ** 2)) < self.FLOOR:
                last, count = None, 0
                continue
            w = self.word_at(self.peak(fr, rate))
            count = count + 1 if w == last else 1
            last = w
            if w is not None and count == self.MIN_FRAMES and (not runs or runs[-1] != w):
                runs.append(w)
        return " ".join(self.texts[k].split()[j] for k, j in runs)

    def hear_wav(self, path):
        import wave
        with wave.open(str(path), "rb") as w:
            return self.hear(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16),
                             w.getframerate())


# ---------------------------------------------------------------- the verdict

def similar(heard, said):
    """0..1: how much of HEARD a span of SAID explains. echo.score's matcher
    with no length floor: the rig must see the short lines the guard does
    not judge ("Loop back here." is under echo.MIN_CHARS)."""
    from sessionphone.bridge import echo
    h, s = echo.norm(heard), echo.norm(said)
    return echo._best_window(h, s)[0] if h and s else 0.0


def overlaps(a, b):
    return a is not None and b is not None and a[0] < b[1] and b[0] < a[1]


def judge(cuts, chunks, caller_win, caller_text, bot_texts, score):
    """Each BARGE-IN: whose audio was it? cuts: [(chunk name, wall time)];
    chunks: {name: (window, text)}. By TIME the chunk overlapped the caller's
    clip or not; by TEXT its transcript scores closer to the caller's words or
    the bot's. A self-cut is one with no caller audio in it (time). Both are
    kept; a disagreement is listed, never resolved quietly."""
    out = []
    for name, at in cuts:
        win, text = chunks.get(name, (None, ""))
        s_caller = score(text, caller_text) if caller_text else 0.0
        s_bot = max((score(text, b) for b in bot_texts), default=0.0)
        by_time = "caller" if overlaps(win, caller_win) else "self"
        by_text = "caller" if s_caller > s_bot else "self"
        out.append({"chunk": name, "at": round(at, 2), "text": text, "by_time": by_time,
                    "by_text": by_text, "caller_score": round(s_caller, 2),
                    "bot_score": round(s_bot, 2)})
    return out


# ---------------------------------------------------------------- the service (opt-in)

def speech_up(url, timeout=0.5):
    import socket
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def caller_clip_real(url, path):
    """--speech-url: the caller's line, spoken once by that server in a second
    voice, as 48 kHz mono int16; cached at `path` (the run's tmp home)."""
    import av
    path = pathlib.Path(path)
    if not path.exists():
        body = {"model": "mlx-community/Kokoro-82M-bf16", "input": CALLER_TEXT,
                "voice": CALLER_VOICE, "response_format": "wav"}
        r = subprocess.run(["curl", "-s", "-m", "60", f"{url}/v1/audio/speech",
                            "-H", "Content-Type: application/json", "-d", json.dumps(body),
                            "-o", str(path)], capture_output=True, timeout=70, check=False)
        if r.returncode != 0 or not path.exists() or path.stat().st_size < 1000:
            raise RuntimeError(f"caller clip: tts rc={r.returncode}")
    out = []
    with av.open(str(path)) as f:
        rs = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for fr in f.decode(audio=0):
            for g in rs.resample(fr):
                out.append(g.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.int16)


# ---------------------------------------------------------------- one call

def one_call(gain, delay_ms, caller_at, echo_only, speech_url=None):
    """Run one call in THIS process; return the result dict. Isolates the
    environment before calls.py is imported: call it once per process."""
    real_home = os.path.expanduser("~")
    before = real_home_snapshot(real_home)
    # this machine's own addresses (the ICE host candidates): traffic to them stays here
    from aioice.ice import get_host_addresses
    audit, net, procs, away = net_audit(set(get_host_addresses(True, True)))
    sys.addaudithook(audit)

    home, root = isolate()
    if speech_url:
        os.environ["DC_STT_URL"] = f"mlx-audio+{speech_url}/v1/audio/transcriptions"
        os.environ["DC_TTS_URL"] = os.environ["DC_KOKORO_URL"] = f"{speech_url}/v1/audio/speech"
    else:
        os.environ["DC_STT_URL"] = f"{NOWHERE}/v1/audio/transcriptions"
        os.environ["DC_TTS_URL"] = os.environ["DC_KOKORO_URL"] = f"{NOWHERE}/v1/audio/speech"
    # an approved caller is an address AND its key (the call gate, keygate.py)
    sys.path.insert(0, str(HERE.parent))
    import _keyfakes

    from sessionphone import keygate
    (root / "callers.txt").write_text(f"{keygate.MARKER}\n{CALLER}  key:{_keyfakes.FPR_K1}\n")
    os.environ["DC_CALLERS_FILE"] = str(root / "callers.txt")

    import asyncio
    import fractions

    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from av import AudioFrame

    from sessionphone.bridge import calls
    from sessionphone.bridge.bot import transcribe

    assert calls.BARGE_IN and not calls.EARS, "the rig runs at the defaults only"
    assert str(calls.CALLS_DIR).startswith(str(home)), calls.CALLS_DIR
    stub = None if speech_url else ToneVoice()
    if echo_only:
        clip = np.zeros(0, dtype=np.int16)
    elif stub:
        clip = stub.say(CALLER_TEXT)
    else:
        clip = caller_clip_real(speech_url, home / "caller.wav")
    path = EchoPath(gain, delay_ms / 1000.0)
    mic = Mic(clip, path)
    lines, chunks = [], {}

    def hook_log(m):
        lines.append((time.time(), m))
        print(f"  {time.strftime('%H:%M:%S')} {m}", flush=True)

    def hook_transcribe(p):
        import wave
        t = time.time()
        try:
            with wave.open(p, "rb") as w:
                dur = w.getnframes() / w.getframerate()
        except (OSError, EOFError, wave.Error):
            dur = 0.0
        text = stub.hear_wav(p) if stub else transcribe(p)
        chunks[pathlib.Path(p).name] = ((t - dur, t), text or "")
        return text

    said_texts = []                       # every line the bot rendered: the echo's source
    real_tts = calls.tts_pcm48

    def hook_tts(text, voice=None):
        said_texts.append(text)
        return stub.say(text) if stub else real_tts(text, voice)

    calls.tts_pcm48 = hook_tts
    calls._hooks = calls.Hooks(hook_log, hook_transcribe, lambda *a, **k: None)
    loop = calls._manager.loop
    calls._manager.start()

    class Snap(dict):
        __getattr__ = dict.get

    class Sender:
        def get_snapshot(self): return Snap(display_name="Owner", address=CALLER, is_key_contact=True)
        def make_vcard(self): return _keyfakes.vcard(CALLER, _keyfakes.K1)

    class Msg:
        def __init__(self, phone): self.phone = phone
        def get_snapshot(self): return Snap(sender=Sender(), chat_id=99)
        def accept_incoming_call(self, sdp):
            asyncio.run_coroutine_threadsafe(
                self.phone.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer")),
                loop).result(10)
        def end_call(self): pass

    class Chat:
        def get_basic_snapshot(self): return Snap(name="Owner & loopback", is_contact_request=False)

    class Account:
        def __init__(self, phone): self.phone = phone
        def ice_servers(self): return []
        def get_chat_by_id(self, i): return Chat()
        def get_message_by_id(self, i): return Msg(self.phone)

    class MicTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.t0, self.n = None, 0

        async def recv(self):
            if self.t0 is None:
                self.t0 = time.time()
            else:
                self.n += FRAME
                wait = self.t0 + self.n / RATE - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
            pcm = mic.frame(time.time())
            f = AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
            f.pts, f.sample_rate, f.time_base = self.n, RATE, fractions.Fraction(1, RATE)
            return f

    async def wait_for(cond, limit, step=0.1):
        t = time.time()
        while not cond():
            if time.time() - t > limit:
                return False
            await asyncio.sleep(step)
        return True

    async def call():
        # iceServers=[], as the bridge builds its own: with NO configuration
        # aiortc gathers against its default public STUN server, off this
        # machine. Host candidates are all a loopback call needs.
        phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        phone.createDataChannel("iceTrickling", negotiated=True, id=1)
        phone.createDataChannel("mutedState", negotiated=True, id=3)
        phone.addTrack(MicTrack())

        @phone.on("track")
        def on_track(track):
            async def sink():
                try:
                    while True:
                        f = await track.recv()
                        path.heard(mono16(f.to_ndarray(), len(f.layout.channels)), time.time())
                except Exception as e:  # noqa: BLE001 - the track ends with the call
                    print(f"  phone: track ended ({e.__class__.__name__})", flush=True)
            asyncio.ensure_future(sink())

        await phone.setLocalDescription(await phone.createOffer())
        ev = Snap(msg_id=4242, chat_id=99, place_call_info=phone.localDescription.sdp, has_video=False)
        calls.incoming(Account(phone), ev)

        def sess():
            return next(iter(calls._manager.sessions.values()), None)
        # ICE on loopback host candidates sometimes stalls 30-60 s before it
        # completes: a slow connect is not a failed call, so it is waited for
        # and recorded (connect_s), never measured as if it were a call
        t_call = time.time()
        ok = await wait_for(lambda: sess() is not None and sess().connected, 90)
        connect_s = round(time.time() - t_call, 1)
        s = sess()
        if s is None:
            await phone.close()
            return {"ok": False, "said": {"n": 0}, "line_at": time.time(), "echo_stats": {},
                    "connect_s": connect_s}
        # the opening: the greeting said and over (or cut)
        ok = ok and await wait_for(lambda: not s.voice.opening and not s.voice.speaking()
                                   and any("SPOKE" in m for _, m in lines), 60)
        await asyncio.sleep(0.5)
        task = asyncio.ensure_future(s.speak(BOT_TEXT))
        started = await wait_for(lambda: s.voice.speaking(), 30, 0.02)
        line_at = time.time()
        if started and not echo_only:
            mic.start = line_at + caller_at
        said = await task                                   # every sentence on the track, or cut
        await wait_for(lambda: not s.voice.speaking(), 90)
        await wait_for(lambda: mic.caller_window() is None or time.time() > mic.caller_window()[1], 30)
        await asyncio.sleep(3.0)                            # the last chunk: quiet, then STT
        await wait_for(lambda: not s.turn_pending, 20)
        await asyncio.sleep(0.5)
        stats = s.echo.stats()
        for x in list(calls._manager.sessions.values()):
            await x.close("test over")
        await phone.close()
        return {"ok": bool(ok and started), "said": said, "line_at": line_at, "echo_stats": stats,
                "connect_s": connect_s}

    r = asyncio.run_coroutine_threadsafe(call(), loop).result(240)
    after = real_home_snapshot(real_home)
    msgs = [m for _, m in lines]
    cuts, holds = [], []
    for t, m in lines:
        if m.startswith("CALL BARGE-IN on "):
            cuts.append((m.split()[3].rstrip(":"), t - r["line_at"]))
        elif m.startswith("CALL BARGE-IN held on "):
            holds.append((m.split()[4].rstrip(":"), t - r["line_at"]))
    win = mic.caller_window()
    caller_text = None if echo_only else CALLER_TEXT
    verdicts = judge(cuts, chunks, win, caller_text, said_texts, similar)
    held = judge(holds, chunks, win, caller_text, said_texts, similar)
    heard = [m.split(": ", 1)[-1].rsplit(" [call ", 1)[0] for m in msgs if m.startswith("CALL HEARD ")]
    leaked = [h for h in heard
              if max((similar(h, b) for b in said_texts), default=0.0) >
              (0.0 if echo_only else similar(h, CALLER_TEXT))]
    turns = [m.split(": ", 1)[-1].rsplit(" [call ", 1)[0] for m in msgs
             if m.startswith("CALL TURN (")]
    first_caller_cut = next((v for v in verdicts if v["by_time"] == "caller"), None)
    return {
        "voice": f"real: mlx-audio at {speech_url}" if speech_url else "stub: ToneVoice",
        "gain_db": gain, "delay_ms": delay_ms, "echo_only": echo_only,
        "caller_at_s": None if echo_only else caller_at,
        "ok": r["ok"], "connect_s": r["connect_s"], "sentences": r["said"].get("n", 0),
        "barge_in": len(verdicts),
        "self_cuts": sum(v["by_time"] == "self" for v in verdicts),
        "caller_cuts": sum(v["by_time"] == "caller" for v in verdicts),
        "text_disagrees": sum(v["by_time"] != v["by_text"] for v in verdicts),
        "held": len(held),
        "held_on_caller": sum(v["by_time"] == "caller" for v in held),
        "echo_drops": sum(m.startswith("CALL ECHO ") for m in msgs),
        "chunks": len(chunks),
        "echo_leaked_as_heard": len(leaked),
        "caller_cut_latency_s": (round(first_caller_cut["at"] - caller_at, 2)
                                 if first_caller_cut and not echo_only else None),
        "echo_dbfs": dbfs(mic.echo_rms), "caller_dbfs": dbfs(mic.caller_rms),
        "echo_p90_dbfs": dbfs(mic.echo_rms, 90), "caller_p90_dbfs": dbfs(mic.caller_rms, 90),
        "echo_frames_over_vad_start": sum(x > VAD_START for x in mic.echo_rms),
        "echo_frames": len(mic.echo_rms),
        "echo_guard": r["echo_stats"],
        "verdicts": verdicts, "holds": held, "bot_said": said_texts,
        "echo_lines": [m for m in msgs if m.startswith(("CALL ECHO ", "CALL BARGE-IN"))],
        "turns": turns, "heard": heard, "leaked": leaked,
        "isolated_home": str(home),
        "real_home_changed": changed(before, after),
        "net_connects": net, "net_off_host": sorted(away), "subprocesses": sorted(set(procs)),
    }


# ---------------------------------------------------------------- the grid

def grid(args):
    """Every setting, one call at a time, each in a fresh process: calls.py
    reads its settings at import, and one call is never shared."""
    keep = pathlib.Path(args.keep)
    keep.mkdir(parents=True, exist_ok=True)
    out = []
    for gain in args.gains:
        for delay in args.delays:
            for echo_only in (True, False):
                jpath = keep / f"g{gain:g}-d{delay}-{'echo' if echo_only else 'caller'}.json"
                cmd = [sys.executable, __file__, "--gain", str(gain), "--delay", str(delay),
                       "--caller-at", str(args.caller_at), "--json", str(jpath)]
                if echo_only:
                    cmd.append("--echo-only")
                if args.speech_url:
                    cmd += ["--speech-url", args.speech_url]
                print("RUN", " ".join(cmd[1:]), flush=True)
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
                jpath.with_suffix(".log").write_text(p.stdout + p.stderr)
                if p.returncode == SKIP:
                    print("SKIP: the speech service does not answer")
                    return SKIP
                try:
                    res = json.loads(jpath.read_text())
                except (OSError, ValueError):
                    res = {"gain_db": gain, "delay_ms": delay, "echo_only": echo_only, "ok": False,
                           "error": (p.stdout + p.stderr)[-800:]}
                res["rc"] = p.returncode
                out.append(res)
                print("  ", summary(res), flush=True)
                # after every call: a grid stopped part-way keeps what it measured
                pathlib.Path(args.out).write_text(json.dumps(out, indent=1))
                if p.returncode == HOME_CHANGED:
                    print("STOP: the real home changed:", res.get("real_home_changed"))
                    return HOME_CHANGED
    print(table(out))
    return 0


def summary(r):
    if "barge_in" not in r or not r.get("ok"):
        return (f"gain {r['gain_db']} delay {r['delay_ms']} "
                f"{'echo only' if r['echo_only'] else 'caller'}: FAILED (the call did not come "
                f"up and play in time; nothing measured)")
    return (f"gain {r['gain_db']:>4} dB delay {r['delay_ms']:>3} ms "
            f"{'echo only' if r['echo_only'] else 'caller   '} | BARGE-IN {r['barge_in']} "
            f"(self {r['self_cuts']}, caller {r['caller_cuts']}) held {r['held']} "
            f"(on caller {r['held_on_caller']}) "
            f"ECHO {r['echo_drops']} leaked {r['echo_leaked_as_heard']} "
            f"echo {r['echo_dbfs']}/{r['echo_p90_dbfs']} dBFS caller "
            f"{r['caller_dbfs']}/{r['caller_p90_dbfs']} dBFS (median/p90) "
            f"| real home changed {len(r['real_home_changed'])} net {len(r['net_connects'])} "
            f"off-host {len(r.get('net_off_host', []))}")


def table(rows):
    head = ("| gain dB | delay ms | caller | BARGE-IN | self-cuts | caller cuts | held | "
            "held on caller | ECHO drops | echo heard as caller | echo dBFS p90 | "
            "echo frames over VAD start | caller dBFS p90 | cut latency s |")
    out = [head, "|" + "---|" * 14]
    for r in rows:
        if "barge_in" not in r or not r.get("ok"):
            out.append(f"| {r['gain_db']} | {r['delay_ms']} | {'no' if r['echo_only'] else 'yes'} | FAILED |")
            continue
        out.append(f"| {r['gain_db']} | {r['delay_ms']} | {'no' if r['echo_only'] else 'yes'} | "
                   f"{r['barge_in']} | {r['self_cuts']} | {r['caller_cuts']} | {r['held']} | "
                   f"{r['held_on_caller']} | {r['echo_drops']} | {r['echo_leaked_as_heard']} | "
                   f"{r['echo_p90_dbfs']} | {r['echo_frames_over_vad_start']}/{r['echo_frames']} | "
                   f"{r['caller_p90_dbfs']} | {r['caller_cut_latency_s']} |")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gain", type=float, default=-30.0, help="echo level, dB (default -30)")
    ap.add_argument("--delay", type=int, default=120, help="echo delay, ms (default 120)")
    ap.add_argument("--caller-at", type=float, default=6.0,
                    help="seconds into the bot's line the caller starts (default 6)")
    ap.add_argument("--echo-only", action="store_true", help="the caller is silent")
    ap.add_argument("--speech-url", default=None, metavar="URL",
                    help="OPT-IN, reaches a speech server: real TTS and STT on the mlx-audio "
                         "server at URL (e.g. http://HOST:PORT). Default: the in-process stub, "
                         "no server")
    ap.add_argument("--grid", action="store_true", help="every --gains x --delays, both ways")
    ap.add_argument("--gains", type=float, nargs="+", default=[0.0, -6.0, -10.0, -20.0, -30.0, -40.0])
    ap.add_argument("--delays", type=int, nargs="+", default=[60, 120, 200])
    ap.add_argument("--keep", default=None, help="--grid: dir for the per-run files")
    ap.add_argument("--json", default=None, help="write this run's result here")
    ap.add_argument("--out", default=None, help="--grid: all results (default KEEP/results.json)")
    args = ap.parse_args(argv)
    if args.speech_url and not speech_up(args.speech_url):
        print(f"SKIP: no speech service at {args.speech_url}")
        return SKIP
    if args.grid:
        args.keep = args.keep or tempfile.mkdtemp(prefix="dc-echo-grid-")
        args.out = args.out or str(pathlib.Path(args.keep) / "results.json")
        return grid(args)
    r = one_call(args.gain, args.delay, args.caller_at, args.echo_only, args.speech_url)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(r, indent=1))
    print("\nRESULT", summary(r))
    for v in r["verdicts"]:
        print("  BARGE-IN", v)
    for v in r["holds"]:
        print("  HELD", v)
    if r["real_home_changed"]:
        print("REAL HOME CHANGED:", *r["real_home_changed"], sep="\n  ")
        return HOME_CHANGED
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)      # the call manager's loop thread is not a daemon

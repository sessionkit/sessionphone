"""A web call: a browser tab as one more call device.

NOT a new voice stack. A `WebCall` is a WebRTC peer (aiortc) with the same
duck type the Delta bridge's legs read, so `earleg` and `mouthleg` serve it
UNCHANGED: the ears open it as `call:N`, the mouth speaks into it with
`mouth say --device call:N`. There is no VAD, no STT and no TTS here. The
ears do the hearing; the mouth does the speaking.

What the legs read (sp bridge/earleg.py, bridge/mouthleg.py):
  manager.sessions[N] with .pc (truthy = live), .ear_taps, .wav_samples, .dir,
  .chat_id, .chat_name; .voice.say/.mix/.cut, .echo.spoken/.cut, .lines.

THE LEGS ARE A SWITCH. With no `--legs` there are no sockets: the
call is a closed loop that plays the caller back to themselves about 1 s later
(echo), so sign-in, mic and speaker can be tried with no seat on the line.
With `--legs` the two sockets are served under `<root>/web/`. A live keeper
(call-ears) picks up ANY `~/.sessionphone/*/call-ears.sock` within
seconds, so `--legs` on a root a keeper or the mouth scans is the live-seat
DEMO: refused unless `--demo` names it (`legs_refusal`).

The leg sockets' module defaults (earleg.SOCK, mouthleg.SOCK) are frozen at
import from dc_env.ROOT, which is the REAL ~/.sessionphone under the test
suite. So this module ALWAYS passes an explicit path to their serve().

EACH FINISHED TURN IS MAILED TO THE SEAT. With `--legs`, a web call mails
each finished turn of the caller's to the seat, as a Delta call's turns are:
To the seat (`--mail-to`, else DC_MAILTO, else the bare <DC_AGENT>; the server
refuses to start with none), Reply-To the call's box, calls+web-<N>@HOST, so
a plain reply is spoken into the call. The ears write the turn to the heard
log; web/turns.py's TurnFollower, one per call, made just before the call
registers, reads its own call:N there and mails each turn once. It is M5
mail, signed, by the ONE path every mail takes: seatmail's `message send
--message-id`, as the web seat. Echo mode mails no one.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from av import AudioResampler

from sessionphone import paths
from sessionphone.bridge import earleg, mouthleg
from sessionphone.bridge.calls import OUT_RATE, PipVoiceTrack
from sessionphone.bridge.echo import SPOOL, VOICEMODE_EVENTS, EchoGuard
from sessionphone.web import names
from sessionphone.web.turns import Seat, TurnFollower

IN_RATE = earleg.RATE           # 16 kHz mono: what the ears are fed
PTIME = 0.020
IN_SAMPLES = int(IN_RATE * PTIME)
GAP_S = 0.7                     # no packet this long (Opus DTX): the ears still need time to move
ECHO_DELAY_S = 1.0              # echo mode: the caller hears themselves this much later
EARS_SOCK, MOUTH_SOCK = "call-ears.sock", "call-mouth.sock"

# The desk's log takes two kinds. A dict is a call's ROW, its start
# and end, {"call": N, "event": ...}: server.run sends it to the same JSON sink
# as the auth rows, so one pane tells the whole story. A str is a line from the
# legs or the turn follower, as before.
Log = Callable[[str | dict], None]


# ---- the --legs guard -------------------------------------------------------

def _parts(p: Path) -> list[str]:
    """Case-blind path parts: macOS's default volume ignores case, so
    ~/.SessionPhone IS ~/.sessionphone."""
    return [x.casefold() for x in p.parts]


def scanned_under(root: Path, home: Path | None = None) -> Path | None:
    """The root a keeper or the mouth scans that `root` is, or is inside; else None.

    They look in ~/.sessionphone and every ~/.*/delta-chat, each with its
    sockets at depth 0 or 1 (sv keeper/call-ears, sources.bot_roots, mouth
    output.call_socks). Matched by PATTERN, not by globbing what exists:
    --legs makes its own dirs, so a ~/.x/delta-chat that does not exist yet
    would exist, and be scanned, once the sockets are bound."""
    h = Path(home or Path.home()).expanduser().resolve()
    r = Path(root).expanduser().resolve()
    hp, rp = _parts(h), _parts(r)
    if rp[:len(hp)] != hp:
        return None
    rel = r.parts[len(hp):]
    if rel and rel[0].casefold() == ".sessionphone":
        return h / rel[0]
    if len(rel) >= 2 and rel[0].startswith(".") and rel[1].casefold() == "delta-chat":
        return h / rel[0] / rel[1]
    return None


def legs_refusal(root: Path, *, explicit_root: bool, demo: bool, home: Path | None = None,
                 own_root: Path | None = None) -> str:
    """'' when `--legs` may serve its sockets under `root/web/`, else why not.

    Refused unless --demo when the root was not given with --root (a default
    root is a live one), when it IS this process's own default root
    (`own_root`, else paths.root(): $SESSIONPHONE_ROOT, which a keeper started
    in the same environment scans), or when it is, or is inside, a root a
    keeper scans. --demo is the DEMO: it names the act, so it never happens by
    omission."""
    if demo:
        return ""
    if not explicit_root:
        return ("--legs on the default root puts a live seat on the line (the keeper hears it): "
                "give a scratch --root, or add --demo for the DEMO, which waits for the operator's GO")
    own = Path(own_root or paths.root()).expanduser().resolve()
    if _parts(Path(root).expanduser().resolve()) == _parts(own):
        return (f"--legs on {own}, this process's own default root ($SESSIONPHONE_ROOT), puts a live seat "
                "on the line: give a scratch --root, or add --demo for the DEMO, which waits for the operator's GO")
    s = scanned_under(root, home)
    if s is not None:
        return (f"--legs under {s} puts a live seat on the line (a keeper scans it): "
                "give a scratch --root, or add --demo for the DEMO, which waits for the operator's GO")
    return ""


# AF_UNIX: sun_path holds 104 bytes on macOS and 108 on Linux, with the NUL.
SUN_PATH_MAX = (104 if sys.platform == "darwin" else 108) - 1


def legs_path_refusal(webdir: Path) -> str:
    """'' when both leg sockets fit in sun_path, else why not: bind() would fail
    with a bare 'AF_UNIX path too long' once the server is already up."""
    longest = max((Path(webdir) / s for s in (EARS_SOCK, MOUTH_SOCK)), key=lambda p: len(os.fsencode(p)))
    n = len(os.fsencode(longest))
    if n <= SUN_PATH_MAX:
        return ""
    return (f"--legs: the socket path {longest} is {n} bytes, and a Unix socket path may be "
            f"{SUN_PATH_MAX}: give a shorter --root")


# ---- the voice track ----------------------------------------------------------

class WebVoiceTrack(PipVoiceTrack):
    """The bridge's outgoing track, unchanged but for cut(): the bridge's logs
    through calls._hooks, which is set only by calls.setup() (the Delta bot).
    Installing hooks would change a process-wide global, so cut() here is the
    track's drop(), which is the same cut without the log line."""

    def cut(self):
        return self.drop()


# ---- the registry and one call --------------------------------------------------

class WebCalls:
    """What the legs call `manager`: .loop and .sessions, one per process."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        self.loop = loop
        self.sessions: dict[int, WebCall] = {}

    def new_id(self, now: float | None = None) -> int:
        """The wall-clock second the call began; +1 while taken. Ten digits, so it
        cannot meet a Delta call's row id (4-5 digits) in the keeper's union."""
        n = int(time.time() if now is None else now)
        while n in self.sessions:
            n += 1
        return n

    def live(self) -> list[int]:
        return sorted(n for n, s in self.sessions.items() if s.pc)


class WebCall:
    """One browser on the line. The browser offers; this answers (the
    incoming-Delta shape). iceServers is EMPTY: both ends are on the tailnet,
    and no STUN or TURN is spent (aiortc's default would be a public STUN)."""

    def __init__(self, registry: WebCalls, n: int, calls_dir: Path, *, echo_mode: bool,
                 voicemode=VOICEMODE_EVENTS, spool=SPOOL, log: Log = print,
                 on_end: Callable[[str], None] | None = None):
        self.registry, self.n, self.echo_mode, self.log, self.on_end = registry, n, echo_mode, log, on_end
        self.pc: RTCPeerConnection | None = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.voice = WebVoiceTrack()
        self.pc.addTrack(self.voice)
        self.echo = EchoGuard(voicemode=voicemode, spool=spool)
        self.lines: list = []
        self.ear_taps: list = []
        self.wav_samples = 0
        self.dir = Path(calls_dir) / str(n)
        self.chat_id, self.chat_name = None, "web"
        self.ended: str | None = None
        self.turns: TurnFollower | None = None   # with --legs and a seat: its turns' mailer (CallDesk)
        self._tasks: list[asyncio.Task] = []

        @self.pc.on("track")
        def _track(track):
            if track.kind == "audio":
                self._tasks.append(asyncio.ensure_future(self._listen(track)))

        @self.pc.on("connectionstatechange")
        async def _state():
            if self.pc is not None and self.pc.connectionState in ("failed", "closed"):
                await self.close(f"connection {self.pc.connectionState}")

    async def answer(self, offer_sdp: str) -> str:
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        await self.pc.setLocalDescription(await self.pc.createAnswer())   # gathers ICE before it returns
        return self.pc.localDescription.sdp

    async def add_ice(self, c) -> None:
        """One trickled browser candidate ({candidate, sdpMid, sdpMLineIndex}); None ends them."""
        if not c or not c.get("candidate"):
            return
        line = str(c["candidate"])
        cand = candidate_from_sdp(line.split(":", 1)[1] if line.startswith("candidate:") else line)
        cand.sdpMid, cand.sdpMLineIndex = c.get("sdpMid"), c.get("sdpMLineIndex")
        await self.pc.addIceCandidate(cand)

    async def _listen(self, track) -> None:
        """The caller's audio, 16 kHz mono, to every ear tap (earleg.feed): that is all.
        In echo mode it is also played back to them, ECHO_DELAY_S later."""
        to_ears = AudioResampler(format="s16", layout="mono", rate=IN_RATE)
        to_echo = AudioResampler(format="s16", layout="mono", rate=OUT_RATE) if self.echo_mode else None
        delay: collections.deque = collections.deque()
        held, hold = 0, int(ECHO_DELAY_S * OUT_RATE)
        silence = np.zeros(IN_SAMPLES, dtype=np.int16).tobytes()
        try:
            while self.ended is None:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=GAP_S)
                except TimeoutError:
                    for _ in range(int(GAP_S / PTIME)):      # DTX: the audio clock still moves
                        earleg.feed(self, silence)
                        self.wav_samples += IN_SAMPLES
                    continue
                for f in to_ears.resample(frame):
                    pcm = f.to_ndarray().reshape(-1).astype(np.int16)
                    earleg.feed(self, pcm.tobytes())
                    self.wav_samples += len(pcm)
                if to_echo is not None:
                    for f in to_echo.resample(frame):
                        pcm = f.to_ndarray().reshape(-1).astype(np.int16)
                        delay.append(pcm)
                        held += len(pcm)
                    while held > hold and delay:
                        out = delay.popleft()
                        held -= len(out)
                        self.voice.say(out)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the track ended: the call is over
            if self.ended is None:
                self.log({"call": self.n, "event": "listen-stopped", "error": type(e).__name__})
                await self.close("caller-gone")

    async def close(self, why: str = "hangup") -> None:
        """The CallSession.close order (sp bridge/calls.py:1997-2044), with no
        transcript: the turn follower is told the end, every tap hears it, the
        voice is cut, the peer closed, and the call leaves the registry. Idempotent."""
        if self.ended is not None:
            return
        self.ended = why
        if self.turns is not None:         # stamped BEFORE "E call-ended": the ears' listen-stopped
            self.turns.end()               # comes after it, so the drain ends on it (build note 3)
        earleg.end(self)                   # every tap hears "E call-ended"
        self.voice.cut()
        self.registry.sessions.pop(self.n, None)
        self.log({"call": self.n, "event": "ended", "why": why})
        pc, self.pc = self.pc, None
        for t in self._tasks:
            if t is not asyncio.current_task():
                t.cancel()
        if pc is not None:
            await pc.close()
        if self.on_end is not None:
            self.on_end(why)


# ---- the desk: the socket's call messages, and the legs --------------------------

class CallDesk:
    """The WebServer's call handler (`on_call`, `on_close`): one live web call per
    process (`busy`), echo by default, the legs with `legs=True`.

    With `legs=True` and a `seat`, each call gets a TurnFollower on the heard
    dir `heard`, and its finished turns are mailed to the seat. A desk with the
    legs and NO seat follows no turns (build note 5): the serve path
    (server.run) refuses --legs with no seat named, so only a test builds one."""

    def __init__(self, webdir: Path, *, legs: bool = False, seat: Seat | None = None,
                 heard: Path | None = None, voicemode=VOICEMODE_EVENTS, spool=SPOOL,
                 log: Log = print, caller: str | None = None):
        self.webdir = Path(webdir)
        self.legs, self.voicemode, self.spool, self.log = legs, voicemode, spool, log
        self.seat, self.heard = seat, heard
        self.caller = names.caller_name(caller)   # each turn mail's speaker
        self.registry = WebCalls()
        self.servers: list = []
        self.followers: dict[int, TurnFollower] = {}   # still running: live, or draining after the end
        self.dialling = False       # an offer being answered: not in sessions yet, but busy

    @property
    def sessions(self) -> dict:
        return self.registry.sessions

    def sock_paths(self) -> tuple[Path, Path]:
        return self.webdir / EARS_SOCK, self.webdir / MOUTH_SOCK

    async def start(self) -> None:
        """Serve the legs, with --legs only, each at an EXPLICIT path under <root>/web/."""
        self.registry.loop = asyncio.get_running_loop()
        if not self.legs:
            return
        ears, mouth = self.sock_paths()
        self.servers = [await earleg.serve(self.registry, path=ears, log=self.log),
                        await mouthleg.serve(self.registry, path=mouth, log=self.log)]

    def make_follower(self, call: WebCall) -> TurnFollower | None:
        """The call's turn follower, or None (echo mode, or no seat). Made, not
        started: the constructor takes the heard files' sizes."""
        if not self.legs or self.seat is None or self.heard is None:
            return None
        return TurnFollower(call.n, self.seat, self.heard, caller=self.caller, echo=call.echo, log=self.log)

    def _drained(self, f: TurnFollower) -> None:
        if self.followers.get(f.n) is f:
            del self.followers[f.n]

    async def stop(self) -> None:
        for c in list(self.registry.sessions.values()):
            await c.close("server-stopped")
        if self.followers:                  # each drains its last turn, bounded
            draining = list(self.followers.values())
            self.log(f"turns: draining {len(draining)} call(s), up to {max(f.grace for f in draining):.0f} s")
            await asyncio.gather(*(f.wait() for f in draining))
        for s in self.servers:
            s.close()
            await s.wait_closed()
        self.servers = []
        for p in self.sock_paths():
            if self.legs:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

    async def on_call(self, ws, st: dict, m: dict) -> None:
        kind, call = m.get("type"), st.get("call")
        if kind == "call":
            if call is not None and call.ended is None:
                await ws.send(json.dumps({"type": "error", "why": "already in a call"}))
                return
            if self.registry.live() or self.dialling:     # the answer awaits ICE: hold the line
                await ws.send(json.dumps({"type": "busy"}))
                return
            sdp = m.get("sdp")
            if not isinstance(sdp, str) or not sdp:
                await ws.send(json.dumps({"type": "error", "why": "call needs an sdp offer"}))
                return
            n = self.registry.new_id()
            while n in self.followers or n in self.registry.sessions:   # a follower still drains call:N
                n += 1

            def ended(why, ws=ws):
                asyncio.ensure_future(_tell_end(ws, why))

            call = WebCall(self.registry, n, self.webdir / "calls", echo_mode=not self.legs,
                           voicemode=self.voicemode, spool=self.spool, log=self.log, on_end=ended)
            st["call"] = call
            self.dialling = True
            try:
                answer = await call.answer(sdp)
            except Exception as e:  # noqa: BLE001 - a bad offer ends this call, not the server
                await call.close("bad-offer")
                await ws.send(json.dumps({"type": "error", "why": f"offer refused: {type(e).__name__}"}))
                return
            finally:
                self.dialling = False
            call.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            call.turns = self.make_follower(call)   # the heard sizes, taken BEFORE the ears can open it
            self.registry.sessions[n] = call        # live for the legs only once it has an answer
            if call.turns is not None:
                self.followers[n] = call.turns
                call.turns.start().add_done_callback(lambda _t, f=call.turns: self._drained(f))
            self.log({"call": n, "event": "started", "mode": "legs" if self.legs else "echo"})
            await ws.send(json.dumps({"type": "answer", "sdp": answer, "call": n}))
            return
        if call is None or call.ended is not None:
            await ws.send(json.dumps({"type": "error", "why": "no call"}))
            return
        if kind == "ice":
            try:
                await call.add_ice(m.get("candidate"))
            except Exception as e:  # noqa: BLE001 - one bad candidate is not the call
                await ws.send(json.dumps({"type": "error", "why": f"bad candidate: {type(e).__name__}"}))
            return
        if kind == "hangup":
            call.on_end = None
            await call.close("hangup")
            await ws.send(json.dumps({"type": "ended", "why": "hangup"}))
            await ws.close()

    async def on_close(self, ws, st: dict) -> None:
        """The socket went: so does its call."""
        call = st.get("call")
        if call is not None and call.ended is None:
            call.on_end = None
            await call.close("socket-closed")


async def _tell_end(ws, why: str) -> str:
    try:
        await ws.send(json.dumps({"type": "ended", "why": why}))
        await ws.close()
    except Exception as e:  # noqa: BLE001 - the browser may be gone already
        return type(e).__name__
    return ""

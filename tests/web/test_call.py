"""A web call end to end, FAKES ONLY (PHONE-133 build-002, design §8).

The browser is an aiortc peer whose mic is a 440 Hz tone; it signs in with the
software passkey and calls over the real WebSocket. The ears and the mouth are
plain Unix-socket clients on the leg sockets under a SCRATCH root. No test
here dials :8890, :2022 or :8880: an autouse guard refuses any such connect
or send, and records it.
"""
from __future__ import annotations

import asyncio
import fractions
import json
import re
import shutil
import socket
import struct
import tempfile
import time
import types
from pathlib import Path

import numpy as np
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from av import AudioFrame
from click.testing import CliRunner
from websockets.asyncio.client import connect

from sessionphone import cli
from sessionphone.bridge import earleg, mouthleg
from sessionphone.web import call as callmod
from sessionphone.web.call import CallDesk, WebCalls, legs_refusal
from sessionphone.web.server import WebServer

LIVE_PORTS = {8890, 2022, 8880}
FLOOR = 300.0                    # RMS of a 8000-amplitude tone is ~5600; silence is 0


# ---- the guard: nothing reaches the live audio ports -------------------------

@pytest.fixture(autouse=True)
def no_live_ports(monkeypatch):
    """Every connect / connect_ex / sendto in this process is checked; a live port
    is refused and recorded. Returns the record (it must stay empty)."""
    hits: list = []

    def guard(name):
        real = getattr(socket.socket, name)

        def wrapped(self, *args):
            addr = args[-1] if name == "sendto" else args[0]
            if isinstance(addr, tuple) and len(addr) >= 2 and addr[1] in LIVE_PORTS:
                hits.append((name, addr[:2]))
                raise ConnectionRefusedError(f"test guard: {addr[:2]} is a live audio port")
            return real(self, *args)
        monkeypatch.setattr(socket.socket, name, wrapped)

    for n in ("connect", "connect_ex", "sendto"):
        guard(n)
    yield hits
    assert hits == [], f"a web call test reached a live audio port: {hits}"


def test_the_guard_refuses_a_live_port(no_live_ports):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError, match="live audio port"):
            s.connect(("127.0.0.1", 8890))
    finally:
        s.close()
    assert no_live_ports == [("connect", ("127.0.0.1", 8890))]
    no_live_ports.clear()                 # it meant to: the teardown must pass


# ---- rigs -------------------------------------------------------------------

@pytest.fixture
def short_root():
    """A scratch root with a SHORT path: macOS caps a Unix socket path at 104
    bytes, and pytest's tmp_path is longer. Under /tmp, removed after the test;
    no keeper scans it."""
    d = Path(tempfile.mkdtemp(prefix="spw-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Tone(MediaStreamTrack):
    """The browser's mic: a 440 Hz tone, 48 kHz mono, paced in real time."""
    kind = "audio"

    def __init__(self, hz=440.0, amp=8000):
        super().__init__()
        self.hz, self.amp, self.t, self.start = hz, amp, 0, None

    async def recv(self):
        n = 960
        if self.start is None:
            self.start = time.time()
        else:
            wait = self.start + self.t / 48000 - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        x = (self.amp * np.sin(2 * np.pi * self.hz * np.arange(self.t, self.t + n) / 48000)).astype(np.int16)
        f = AudioFrame.from_ndarray(x.reshape(1, -1), format="s16", layout="mono")
        f.pts, f.sample_rate, f.time_base = self.t, 48000, fractions.Fraction(1, 48000)
        self.t += n
        return f


def rms(pcm) -> float:
    a = np.asarray(pcm, dtype=np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


async def recv(ws) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), 10))


async def heard(track, seconds=6.0) -> float:
    """The loudest 200 ms the client's received track plays within `seconds`."""
    end, buf, best = time.time() + seconds, [], 0.0
    while time.time() < end:
        f = await asyncio.wait_for(track.recv(), 5)
        buf.append(f.to_ndarray().reshape(-1))
        if len(buf) >= 10:
            best = max(best, rms(np.concatenate(buf)))
            buf = []
            if best > FLOOR:
                return best
    return best


class CallRig:
    def __init__(self, tmp_path, softkey, *, legs, seat=None, heard=None):
        self.webdir = tmp_path / "web"
        self.softkey, self.key, self.logs = softkey, None, []
        self.rows: list[dict] = []          # the server's own JSON rows (auth, and PHONE-137's conn)
        self.desk = CallDesk(self.webdir, legs=legs, seat=seat, heard=heard, voicemode=None, spool=None,
                             log=self.logs.append)
        self.srv = WebServer(self.webdir, port=0, lookup=lambda: "", log=self.rows.append, desk=self.desk)
        self.pcs: list = []

    @property
    def origin(self):
        return f"http://localhost:{self.srv.port}"

    async def start(self):
        await self.srv.start()
        await self.desk.start()

    async def stop(self):
        for pc in self.pcs:
            await pc.close()
        await self.desk.stop()
        await self.srv.close()

    async def signed_in(self):
        ws = await connect(f"ws://127.0.0.1:{self.srv.port}/ws", origin=self.origin)
        await recv(ws)
        o = await recv(ws)
        if o["mode"] == "enrol":
            code = self.srv.keyring.new_enrol_code()
            self.key = self.softkey(self.origin)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                      **self.key.create(o["challenge"])}))
        else:
            await ws.send(json.dumps({"type": "auth-finish", "mode": "login", **self.key.get(o["challenge"])}))
        assert (await recv(ws))["type"] == "auth-ok"
        return ws

    async def dial(self, ws):
        """Offer from a tone-mic peer; -> (pc, the track it receives, call id)."""
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pcs.append(pc)
        pc.addTrack(Tone())
        got = asyncio.get_running_loop().create_future()

        @pc.on("track")
        def _t(track):
            if not got.done():
                got.set_result(track)

        await pc.setLocalDescription(await pc.createOffer())
        await ws.send(json.dumps({"type": "call", "sdp": pc.localDescription.sdp}))
        r = await recv(ws)
        assert r["type"] == "answer" and isinstance(r["call"], int), r
        await pc.setRemoteDescription(RTCSessionDescription(sdp=r["sdp"], type="answer"))
        return pc, await asyncio.wait_for(got, 10), r["call"]


def run(tmp_path, softkey, body, *, legs, timeout=40, seat=None, heard=None):
    async def main():
        rig = CallRig(tmp_path, softkey, legs=legs, seat=seat, heard=heard)
        await rig.start()
        try:
            return await asyncio.wait_for(body(rig), timeout)
        finally:
            await rig.stop()
    return asyncio.run(main())


async def ears_tap(path, call):
    """A fake ears: open call N on the ear leg; -> (reader, writer, reply)."""
    r, w = await asyncio.open_unix_connection(str(path))
    w.write((json.dumps({"call": call}) + "\n").encode())
    await w.drain()
    return r, w, json.loads(await asyncio.wait_for(r.readline(), 5))


async def ear_frame(r):
    head = await asyncio.wait_for(r.readexactly(5), 10)
    return head[:1], await r.readexactly(struct.unpack(">I", head[1:])[0])


# ---- the call ---------------------------------------------------------------

def test_the_ears_hear_the_caller_then_the_end(short_root, softkey):
    async def body(rig):
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        assert n > 10**9 and rig.desk.sessions[n].chat_name == "web"
        r, w, reply = await ears_tap(rig.webdir / "call-ears.sock", n)
        assert reply["ok"] and reply["call"] == n and reply["rate"] == 16000
        pcm, loud = [], 0.0
        while loud <= FLOOR:
            kind, payload = await ear_frame(r)
            assert kind == b"A"
            pcm.append(np.frombuffer(payload, dtype="<i2"))
            if sum(len(p) for p in pcm) >= 3200:           # 200 ms at 16 kHz
                loud, pcm = max(loud, rms(np.concatenate(pcm))), []
        await ws.send(json.dumps({"type": "hangup"}))
        assert (await recv(ws)) == {"type": "ended", "why": "hangup"}
        while True:
            kind, payload = await ear_frame(r)
            if kind == b"E":
                assert payload == b"call-ended"
                break
        w.close()
        assert rig.desk.sessions == {}
        rows = (rig.webdir / "calls" / str(n) / "ear-taps.jsonl").read_text().splitlines()
        assert json.loads(rows[0])["call"] == n
    run(short_root, softkey, body, legs=True)


def test_the_mouth_reaches_the_caller(short_root, softkey):
    async def body(rig):
        ws = await rig.signed_in()
        _, track, n = await rig.dial(ws)
        r, w = await asyncio.open_unix_connection(str(rig.webdir / "call-mouth.sock"))
        w.write((json.dumps({"call": n, "rate": 48000, "text": "a tone", "who": "Test"}) + "\n").encode())
        await w.drain()
        assert json.loads(await asyncio.wait_for(r.readline(), 5)) == {"ok": True, "call": n}
        t = np.arange(48000 * 2)
        pcm = (8000 * np.sin(2 * np.pi * 660 * t / 48000)).astype("<i2").tobytes()
        w.write(b"A" + struct.pack(">I", len(pcm)) + pcm + b"D" + struct.pack(">I", 0))
        await w.drain()
        done = json.loads(await asyncio.wait_for(r.readline(), 5))
        assert done["ok"] and done["ended"] == "done" and done["queued_s"] == 2.0
        assert await heard(track) > FLOOR
        assert rig.desk.sessions[n].lines[-1][2] == "a tone"
        w.close()
    run(short_root, softkey, body, legs=True)


def test_echo_mode_has_no_sockets_and_plays_the_caller_back(tmp_path, softkey):
    async def body(rig):
        ws = await rig.signed_in()
        _, track, n = await rig.dial(ws)
        assert not list(rig.webdir.glob("*.sock")) and not list(tmp_path.rglob("*.sock"))
        assert await heard(track) > FLOOR
        assert rig.desk.sessions[n].echo_mode
    run(tmp_path, softkey, body, legs=False)


def test_busy_while_a_call_is_live(tmp_path, softkey):
    async def body(rig):
        a = await rig.signed_in()
        await rig.dial(a)
        b = await rig.signed_in()
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        rig.pcs.append(pc)
        pc.addTrack(Tone())
        await pc.setLocalDescription(await pc.createOffer())
        await b.send(json.dumps({"type": "call", "sdp": pc.localDescription.sdp}))
        assert (await recv(b)) == {"type": "busy"}
        assert len(rig.desk.sessions) == 1
    run(tmp_path, softkey, body, legs=False)


def test_busy_holds_while_the_first_answer_is_still_being_made(tmp_path, softkey, monkeypatch):
    """Two offers at once: the first is not in `sessions` until its answer is made
    (ICE gathering), and the second must still be told busy."""
    real = callmod.WebCall.answer

    async def slow(self, sdp):
        await asyncio.sleep(0.5)
        return await real(self, sdp)
    monkeypatch.setattr(callmod.WebCall, "answer", slow)

    async def offer():
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        pc.addTrack(Tone())
        await pc.setLocalDescription(await pc.createOffer())
        return pc

    async def body(rig):
        a, b = await rig.signed_in(), await rig.signed_in()
        pa, pb = await offer(), await offer()
        rig.pcs += [pa, pb]
        await a.send(json.dumps({"type": "call", "sdp": pa.localDescription.sdp}))
        await asyncio.sleep(0.1)
        await b.send(json.dumps({"type": "call", "sdp": pb.localDescription.sdp}))
        ra, rb = await recv(a), await recv(b)
        assert ra["type"] == "answer" and rb == {"type": "busy"}, (ra.get("type"), rb)
        assert list(rig.desk.sessions) == [ra["call"]] and not rig.desk.dialling
    run(tmp_path, softkey, body, legs=False)


def test_a_bad_offer_does_not_leave_the_line_busy(tmp_path, softkey):
    async def body(rig):
        ws = await rig.signed_in()
        await ws.send(json.dumps({"type": "call", "sdp": "not sdp"}))
        assert "offer refused" in (await recv(ws))["why"]
        assert not rig.desk.dialling
        await rig.dial(await rig.signed_in())     # the bad offer's socket is ended with it
    run(tmp_path, softkey, body, legs=False)


def test_a_socket_closed_mid_call_ends_it_and_the_taps_hear_e(short_root, softkey):
    async def body(rig):
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        r, w, reply = await ears_tap(rig.webdir / "call-ears.sock", n)
        assert reply["ok"]
        await ws.close()
        while True:
            kind, payload = await ear_frame(r)
            if kind == b"E":
                assert payload == b"call-ended"
                break
        w.close()
        for _ in range(50):
            if not rig.desk.sessions:
                break
            await asyncio.sleep(0.05)
        assert rig.desk.sessions == {}
        assert any("socket-closed" in str(m) for m in rig.logs), rig.logs
    run(short_root, softkey, body, legs=True)


def test_a_call_after_a_hangup_is_not_busy(tmp_path, softkey):
    async def body(rig):
        ws = await rig.signed_in()
        await rig.dial(ws)
        await ws.send(json.dumps({"type": "hangup"}))
        assert (await recv(ws))["type"] == "ended"
        ws2 = await rig.signed_in()
        await rig.dial(ws2)
        assert len(rig.desk.sessions) == 1
    run(tmp_path, softkey, body, legs=False)


@pytest.mark.parametrize("msg,why", [
    ({"type": "call"}, "sdp"),
    ({"type": "call", "sdp": "not sdp"}, "offer refused"),
    ({"type": "ice", "candidate": None}, "no call"),
    ({"type": "hangup"}, "no call"),
])
def test_bad_call_messages_are_answered_and_leave_no_call(tmp_path, softkey, msg, why):
    async def body(rig):
        ws = await rig.signed_in()
        await ws.send(json.dumps(msg))
        r = await recv(ws)
        assert r["type"] == "error" and why in r["why"]
        assert rig.desk.sessions == {}
    run(tmp_path, softkey, body, legs=False)


def test_a_trickled_candidate_is_taken(tmp_path, softkey):
    async def body(rig):
        ws = await rig.signed_in()
        pc, _, _ = await rig.dial(ws)
        line = next(x for x in pc.localDescription.sdp.splitlines() if x.startswith("a=candidate:"))
        await ws.send(json.dumps({"type": "ice", "candidate": {"candidate": line[2:], "sdpMid": "0",
                                                               "sdpMLineIndex": 0}}))
        await ws.send(json.dumps({"type": "ice", "candidate": None}))     # end of candidates
        await ws.send(json.dumps({"type": "teleport"}))
        assert (await recv(ws))["why"] == "unknown message 'teleport'"   # nothing else was said
    run(tmp_path, softkey, body, legs=False)


# ---- the legs: explicit paths, and the --legs guard ---------------------------

def test_the_legs_are_served_at_explicit_paths_never_the_import_default(tmp_path, monkeypatch):
    """earleg.SOCK / mouthleg.SOCK are frozen at import to the REAL root: never used."""
    seen = []

    async def spy(manager, path=None, log=print):
        seen.append(path)

        class S:
            def close(self):
                pass

            async def wait_closed(self):
                pass
        return S()

    monkeypatch.setattr(earleg, "serve", spy)
    monkeypatch.setattr(mouthleg, "serve", spy)
    desk = CallDesk(tmp_path / "web", legs=True, voicemode=None, spool=None, log=lambda m: None)

    async def main():
        await desk.start()
        await desk.stop()
    asyncio.run(main())
    assert seen == [tmp_path / "web" / "call-ears.sock", tmp_path / "web" / "call-mouth.sock"]
    assert all(p is not None for p in seen)
    assert tmp_path not in earleg.SOCK.parents and tmp_path not in mouthleg.SOCK.parents


def test_legs_refused_on_the_default_root(tmp_path):
    assert "default root" in legs_refusal(tmp_path / ".sessionphone", explicit_root=False, demo=False,
                                          home=tmp_path)


@pytest.mark.parametrize("rel", [".sessionphone", ".sessionphone/x", ".team/delta-chat", ".team/delta-chat/p"])
def test_legs_refused_where_a_keeper_scans(tmp_path, rel):
    (tmp_path / ".team" / "delta-chat").mkdir(parents=True)
    why = legs_refusal(tmp_path / rel, explicit_root=True, demo=False, home=tmp_path)
    assert "keeper scans" in why and "--demo" in why


@pytest.mark.parametrize("rel", [".new/delta-chat", ".new/delta-chat/p", ".SessionPhone", ".Team/Delta-Chat",
                                 ".sessionphone/../.sessionphone/web"])
def test_legs_refused_on_a_scanned_root_that_does_not_exist_yet_or_differs_in_case(tmp_path, rel):
    """--legs makes its own dirs, so a ~/.x/delta-chat made by this very run would be
    scanned too; and macOS's default volume ignores case."""
    why = legs_refusal(tmp_path / rel, explicit_root=True, demo=False, home=tmp_path)
    assert "keeper scans" in why and "--demo" in why
    assert not (tmp_path / ".new").exists()


@pytest.mark.parametrize("rel", ["web", "delta-chat", ".x", ".x/y/delta-chat", "sessionphone"])
def test_legs_allowed_on_roots_no_keeper_scans(tmp_path, rel):
    assert legs_refusal(tmp_path / rel, explicit_root=True, demo=False, home=tmp_path) == ""


def test_legs_allowed_on_a_scratch_root_or_with_demo(tmp_path):
    assert legs_refusal(tmp_path / "scratch", explicit_root=True, demo=False, home=tmp_path) == ""
    assert legs_refusal(tmp_path / ".sessionphone", explicit_root=False, demo=True, home=tmp_path) == ""


def test_cli_legs_without_demo_on_the_default_root_exits_2_and_makes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSIONPHONE_ROOT")
    monkeypatch.setenv("HOME", str(tmp_path))
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0"])
    assert r.exit_code == 2 and "--demo" in r.output
    assert not (tmp_path / ".sessionphone").exists() and not list(tmp_path.rglob("*.sock"))


def test_cli_legs_with_root_pointing_at_the_live_root_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0", "--root", str(tmp_path / ".sessionphone")])
    assert r.exit_code == 2 and "keeper scans" in r.output
    assert not list(tmp_path.rglob("*.sock"))


def test_legs_refused_on_this_processs_own_default_root(tmp_path, scratch_root):
    """--root given, but it IS $SESSIONPHONE_ROOT, the root a keeper started in this
    environment scans: refused like the default root (do-002 re-review note 1)."""
    why = legs_refusal(scratch_root, explicit_root=True, demo=False, home=tmp_path)
    assert "own default root" in why and "--demo" in why
    assert "own default root" in legs_refusal(Path(str(scratch_root).upper()), explicit_root=True,
                                              demo=False, home=tmp_path)    # macOS ignores case
    assert legs_refusal(scratch_root, explicit_root=True, demo=True, home=tmp_path) == ""
    assert legs_refusal(tmp_path / "other", explicit_root=True, demo=False, home=tmp_path) == ""


def test_cli_legs_with_root_equal_to_sessionphone_root_exits_2_and_makes_nothing(tmp_path, scratch_root,
                                                                                 monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0", "--root", str(scratch_root)])
    assert r.exit_code == 2 and "own default root" in r.output
    assert not scratch_root.exists() and not list(tmp_path.rglob("*.sock"))


def test_cli_legs_with_a_root_too_long_for_a_socket_exits_2_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    long_root = tmp_path / ("r" * (callmod.SUN_PATH_MAX + 1))
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0", "--root", str(long_root)])
    assert r.exit_code == 2 and "give a shorter --root" in r.output and "bytes" in r.output
    assert not long_root.exists()


def test_the_socket_path_check_allows_what_fits():
    fits = Path("/tmp/spw-x/web")
    assert callmod.legs_path_refusal(fits) == ""
    tail = "/web/call-mouth.sock"                    # the longer of the two names
    edge = Path("/" + "a" * (callmod.SUN_PATH_MAX - 1 - len(tail))) / "web"
    assert len(str(edge / "call-mouth.sock")) == callmod.SUN_PATH_MAX
    assert callmod.legs_path_refusal(edge) == ""
    assert "shorter --root" in callmod.legs_path_refusal(Path(str(edge.parent) + "b") / "web")


def test_cli_has_no_to_mail(tmp_path):
    """The seat is named with --mail-to (PHONE-138), never a bare --to; and a web call
    has no mail path of its own: its turns go by seatmail's `message send`, the ONE
    path every mail takes (PHONE-72 part 2b; tests/test_seatmail.py pins it)."""
    r = CliRunner().invoke(cli.main, ["web", "--root", str(tmp_path), "--to", "x@example.invalid"])
    assert r.exit_code == 2 and "No such option" in r.output
    assert not hasattr(callmod, "send_mail")


# ---- the registry ---------------------------------------------------

def test_call_ids_are_epoch_seconds_and_never_reused(anchor):
    reg = WebCalls()
    n = reg.new_id(now=anchor.timestamp() + 0.4)
    assert n == int(anchor.timestamp())
    reg.sessions[n] = object()
    assert reg.new_id(now=anchor.timestamp() + 0.9) == n + 1


# ---- PHONE-138 build-002: the turns, wired into the desk (T7) and the serve path (T8) ----------

TURN_HOST = "example.net"         # DC_HOST: the Reply-To
TURN_IDS = "host"                      # seatmail.id_host(): the ids (PHONE-72 part 2b)


@pytest.fixture(autouse=True)
def id_host(monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_HOST", TURN_IDS)


def seat_and_heard(tmp_path):
    from sessionphone.web.turns import seat_for
    heard_dir = tmp_path / "conversations"
    heard_dir.mkdir()
    return seat_for(f"alice@{TURN_HOST}", TURN_HOST, environ={}), heard_dir


def heard_line(d, n, seq, kind="turn", t=None, **over):
    """One heard line for call:N, appended (the v1 shape; tests/web/test_turns.py has the full one)."""
    t = time.time() if t is None else t
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}" + \
        time.strftime("%z", time.localtime(t))[:3] + ":" + time.strftime("%z", time.localtime(t))[3:]
    rec = {"ts": ts, "seq": seq, "kind": kind, "source": "call", "device": f"call:{n}", "agent": "ears", "v": 1}
    rec.update({"text": "hello alice", "final": True} if kind == "turn" else {})
    rec.update(over)
    with (d / "heard_2030-06-09.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


async def until(cond, seconds=5.0):
    end = time.time() + seconds
    while not cond():
        assert time.time() < end, "timed out"
        await asyncio.sleep(0.05)


def test_t7_legs_with_a_seat_follows_the_call_from_registration_and_mails_its_turn(short_root, softkey, tmp_path,
                                                                                    fake_message):
    seat, heard_dir = seat_and_heard(tmp_path)
    heard_line(heard_dir, 1, 100, text="a line from before the call")

    async def body(rig):
        size = (heard_dir / "heard_2030-06-09.jsonl").stat().st_size
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        f = rig.desk.followers[n]
        assert rig.desk.sessions[n].turns is f and f.echo is rig.desk.sessions[n].echo
        assert f.offsets == {heard_dir / "heard_2030-06-09.jsonl": size}       # the snapshot: at registration
        assert f.task is not None and not f.task.done()
        heard_line(heard_dir, n, 101, text="hello alice, are you there")
        await until(lambda: f.mailed)
        return n

    n = run(short_root, softkey, body, legs=True, seat=seat, heard=heard_dir)
    (m,) = fake_message.mails()
    assert m["To"] == f"alice@{TURN_HOST}" and m["Reply-To"] == f"calls+web-{n}@{TURN_HOST}"
    assert m["Message-ID"] == f"<call-turn-web-{n}-101@{TURN_IDS}>"
    # PHONE-135 SC2: the speaker is the setting's default, a role word, never a person
    assert m["Subject"] == f"[web {n}] owner: hello alice, are you there"
    assert "\nfrom: owner\n" in m.get_content()


def test_t7_close_stamps_the_end_before_e_call_ended_and_stop_awaits_the_drain(short_root, softkey, tmp_path,
                                                                                fake_message, monkeypatch):
    seat, heard_dir = seat_and_heard(tmp_path)
    at_end, real_end = [], earleg.end

    def end(call):
        at_end.append(call.turns.ended_at)          # what the follower knew when the taps heard the end
        real_end(call)
    monkeypatch.setattr(earleg, "end", end)
    box = {}

    async def body(rig):
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        box["f"] = f = rig.desk.followers[n]
        await ws.send(json.dumps({"type": "hangup"}))
        assert (await recv(ws))["type"] == "ended"
        assert at_end == [f.ended_at] and f.ended_at is not None
        # the caller's last words land after the hang-up, then the capture's listen-stopped:
        # the drain is still reading, and stop() (the rig's teardown) awaits it
        heard_line(heard_dir, n, 201, text="and one more thing")
        heard_line(heard_dir, n, 202, kind="event", event="listen-stopped", t=f.ended_at + 0.5)
        return n

    n = run(short_root, softkey, body, legs=True, seat=seat, heard=heard_dir)
    f = box["f"]
    assert f.task.done() and not f.task.cancelled() and f.stopped_at is not None
    assert f.mailed == [f"call-turn-web-{n}-201@{TURN_IDS}"] and len(fake_message.calls()) == 1


def test_t7_stop_waits_for_a_follower_still_draining(tmp_path, fake_message):
    """CallDesk.stop() closes each call, then awaits each follower's drain, bounded."""
    from sessionphone.web.turns import TurnFollower
    seat, heard_dir = seat_and_heard(tmp_path)
    logs = []
    desk = CallDesk(tmp_path / "web", legs=False, seat=seat, heard=heard_dir, voicemode=None, spool=None,
                    log=logs.append)

    async def main():
        f = TurnFollower(7, seat, heard_dir, caller="owner", log=logs.append, poll=0.01, grace=0.2)
        desk.followers[7] = f
        f.start().add_done_callback(lambda _t: desk._drained(f))
        f.end()
        await desk.stop()
        return f

    f = asyncio.run(main())
    assert f.task.done() and not f.task.cancelled() and desk.followers == {}
    assert any("draining 1 call(s)" in m for m in logs) and fake_message.calls() == []


def test_t7_echo_mode_follows_nothing_and_mails_no_one(tmp_path, softkey, fake_message):
    seat, heard_dir = seat_and_heard(tmp_path)

    async def body(rig):
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        assert rig.desk.followers == {} and rig.desk.sessions[n].turns is None
        heard_line(heard_dir, n, 301)
        await asyncio.sleep(0.5)

    run(tmp_path, softkey, body, legs=False, seat=seat, heard=heard_dir)
    assert fake_message.calls() == []


def test_t7_legs_with_no_seat_follows_nothing(short_root, softkey):
    """Build note 5: a desk with the legs and no seat (only a test builds one; the serve
    path refuses --legs with no seat named) starts no follower."""
    async def body(rig):
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        assert rig.desk.followers == {} and rig.desk.sessions[n].turns is None

    run(short_root, softkey, body, legs=True)


def test_t7_a_new_call_never_takes_the_n_of_a_follower_still_draining(tmp_path):
    from sessionphone.web.turns import seat_for
    desk = CallDesk(tmp_path / "web", legs=True, seat=seat_for("alice@x.y", "x.y", environ={}))
    n = desk.registry.new_id()
    desk.followers[n] = desk.followers[n + 1] = object()
    sent = []

    class WS:
        async def send(self, m):
            sent.append(json.loads(m))

    async def main():
        st = {}
        await desk.on_call(WS(), st, {"type": "call", "sdp": "not sdp"})   # refused, but it chose an N
        return st["call"].n

    assert asyncio.run(main()) >= n + 2 and any(m["type"] == "error" for m in sent)


def no_seat_env(monkeypatch):
    """No DC_MAILTO or DC_AGENT. conftest strips them at start, but importing bridge/bot.py
    (test_turns.py's T2 does) sets DC_AGENT = sys.argv[1] for the rest of the run: the
    import-time freeze PHONE-142 owns. A seat named that way would make --legs SERVE,
    and this test would never return."""
    for k in ("DC_MAILTO", "DC_AGENT", "PIP_CHAT_MAILTO", "PIP_CHAT_AGENT"):
        monkeypatch.delenv(k, raising=False)


def test_t8_cli_legs_with_no_seat_named_exits_2_and_makes_nothing(short_root, monkeypatch, tmp_path):
    no_seat_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "worker")             # the shell's name is not the seat's
    root = short_root / "r"
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0", "--root", str(root)])
    assert r.exit_code == 2 and "no seat is named" in r.output and "--mail-to" in r.output
    assert not root.exists() and not list(short_root.rglob("*.sock"))


def test_t8_cli_legs_on_a_junk_host_exits_2_even_with_mail_to(short_root, monkeypatch, tmp_path):
    """PHONE-217: a reverse-DNS getfqdn() no longer refuses (the host is dc_env.host_part,
    the short host); a .arpa host that SESSIONMAIL_HOST names still does (PHONE-98)."""
    no_seat_env(monkeypatch)
    monkeypatch.delenv("DC_HOST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SESSIONMAIL_HOST", "x.ip6.arpa")
    monkeypatch.setattr(socket, "getfqdn", lambda *a: "1.0.0.0.ip6.arpa")
    root = short_root / "r"
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0", "--root", str(root),
                                      "--mail-to", f"alice@{TURN_HOST}"])
    assert r.exit_code == 2 and "DC_HOST" in r.output and "'x.ip6.arpa'" in r.output
    assert not root.exists()


def test_t8_cli_legs_on_a_reverse_dns_fqdn_takes_the_short_host(short_root, monkeypatch, tmp_path):
    """PHONE-217's bug at the CLI: getfqdn() is the loopback PTR, SESSIONMAIL_HOST is set
    (the autouse fixture's). The seat's host is the short host, never a refusal naming .arpa."""
    no_seat_env(monkeypatch)
    monkeypatch.delenv("DC_HOST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(socket, "getfqdn", lambda *a: "1.0.0.0.ip6.arpa")
    from sessionphone.web import turns
    seen, real = [], turns.named_seat
    monkeypatch.setattr(turns, "named_seat", lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])
    root = short_root / "r"
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0", "--root", str(root),
                                      "--mail-to", f"alice@{TURN_HOST}"])
    (seat, why), = seen
    assert why == "" and seat.host == TURN_IDS and "ip6.arpa" not in r.output


def test_t8_the_seat_refusal_comes_after_the_legs_refusals(tmp_path, monkeypatch):
    """The four pinned refusals keep their words: none of them names a seat (§4.5, §11 A8)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0", "--root", str(tmp_path / ".sessionphone")])
    assert r.exit_code == 2 and "keeper scans" in r.output and "no seat is named" not in r.output


def legs_run(short_root, tmp_path, monkeypatch, mail_to=f"alice@{TURN_HOST}"):
    """server.run with --legs, a seat named by --mail-to, a good DC_HOST -> (rc, its lines)."""
    from sessionphone.web import server as srvmod
    no_seat_env(monkeypatch)                                      # --mail-to names it; DC_AGENT would name the From
    monkeypatch.setenv("DC_HOST", TURN_HOST)
    monkeypatch.setattr(srvmod.WebServer, "_own_origins", lambda self: {f"http://localhost:{self.port}"})
    out = []
    rc = srvmod.run(short_root / "web", port=0, host="127.0.0.1", tls=None, origins=(), legs=True,
                    explicit_root=True, mail_to=mail_to, heard_dir=tmp_path / "conversations",
                    echo=out.append, until=lambda: asyncio.sleep(0))
    return rc, out


def test_t8_legs_refuses_an_old_message_at_start(short_root, tmp_path, monkeypatch, fake_message, seat_key):
    """PHONE-72 part 2b, the second door: the probe for --message-id runs at start."""
    seat_key("alice-chat seat")
    fake_message.probe(2, "", "Error: No such option: --message-id\n")
    rc, out = legs_run(short_root, tmp_path, monkeypatch)
    assert rc == 2 and fake_message.probes() == 1
    (why,) = out
    assert why.startswith(f"sessionphone web: {fake_message.exe} answered the --message-id probe")
    assert "needs sessionmail with `message send --message-id`" in why and not list(short_root.rglob("*.sock"))


@pytest.mark.parametrize("comment", ["team-chat seat", "team-chat@example.net seat"])
def test_t8_legs_refuses_a_key_of_another_seat_naming_both(short_root, tmp_path, monkeypatch, fake_message,
                                                          seat_key, comment):
    """(b'): the web seat (alice-chat, from --mail-to) is not the key's (team-chat)."""
    pub = seat_key(comment)
    rc, out = legs_run(short_root, tmp_path, monkeypatch)
    assert rc == 2
    (why,) = out
    assert why.startswith(f"sessionphone web: the signing key is seat team-chat (DC_SIGNER_PUB={pub}), "
                          "but the mail goes From seat alice-chat (the web seat")
    assert fake_message.probes() == 0 and not list(short_root.rglob("*.sock"))


def test_t8_legs_refuses_with_no_signer(short_root, tmp_path, monkeypatch, fake_message):
    monkeypatch.delenv("DC_SIGNER_SOCK")
    rc, out = legs_run(short_root, tmp_path, monkeypatch)
    assert rc == 2 and out[0].startswith("sessionphone web: no signer: DC_SIGNER_SOCK is not set")


@pytest.mark.parametrize("comment", ["alice-chat seat", "alice-chat@relay.example seat"])
def test_t8_legs_starts_when_the_key_is_the_web_seats(short_root, tmp_path, monkeypatch, fake_message,
                                                     seat_key, comment):
    seat_key(comment)
    rc, out = legs_run(short_root, tmp_path, monkeypatch)
    assert rc == 0, out
    assert fake_message.probes() == 1 and fake_message.calls() == []      # the probe sent nothing


def test_t8_run_with_legs_prints_the_to_and_the_reply_to_at_start(short_root, tmp_path, monkeypatch, seat_key):
    from sessionphone.web import server as srvmod
    seat_key("alice-chat seat")                                    # the key is the web seat's (PHONE-72 2b)
    no_seat_env(monkeypatch)                                      # --mail-to names it; DC_AGENT would name the From
    monkeypatch.setenv("DC_HOST", TURN_HOST)
    monkeypatch.setattr(srvmod.WebServer, "_own_origins", lambda self: {f"http://localhost:{self.port}"})
    out, heard_dir = [], tmp_path / "conversations"
    rc = srvmod.run(short_root / "web", port=0, host="127.0.0.1", tls=None, origins=(), legs=True,
                    explicit_root=True, mail_to=f"alice@{TURN_HOST}", heard_dir=heard_dir, echo=out.append,
                    until=lambda: asyncio.sleep(0))
    assert rc == 0, out
    assert (f"  turns: each finished turn of a web call mailed to alice@{TURN_HOST} (From seat alice-chat), "
            f"Reply-To calls+web-<N>@{TURN_HOST} (M5, signed (carrier /usr/sbin/sendmail is an MTA, "
            "from sessionmail's default))") in out                  # the suite's carrier: an MTA
    assert (f"  turns box: alice@{TURN_HOST} is not a box on this host: its reader makes the box") in out
    (line,) = [m for m in out if m.startswith("  heard log: ")]   # never made here: marked, not silent (N1)
    assert line.startswith(f"  heard log: {heard_dir}  (missing") and "--heard-dir" in line
    assert not list(short_root.rglob("*.sock"))                  # stop() took the legs down


def test_t8_cli_legs_with_a_mail_to_that_is_no_address_exits_2(short_root, monkeypatch, tmp_path):
    """`--mail-to alice@` names no box on any host: refused before anything is made."""
    no_seat_env(monkeypatch)
    monkeypatch.setenv("DC_HOST", TURN_HOST)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = short_root / "r"
    r = CliRunner().invoke(cli.main, ["web", "--legs", "--port", "0", "--root", str(root), "--mail-to", "alice@"])
    assert r.exit_code == 2 and "'alice@' is not a mail address" in r.output
    assert not root.exists()


def test_t8_legs_keeps_a_bare_mail_to(short_root, tmp_path, monkeypatch, fake_message, seat_key):
    """PHONE-248 (reverses build-002 review N3): `--mail-to alice` is the box alice on
    this host. sessionmail adds the host to a bare To on both carriers (measured), so
    the turns go To `alice`, never to a local user, and no host is baked in."""
    seat_key("alice-chat seat")
    rc, out = legs_run(short_root, tmp_path, monkeypatch, mail_to="alice")
    assert rc == 0, out
    (line,) = [m for m in out if m.startswith("  turns: ")]
    assert line.startswith("  turns: each finished turn of a web call mailed to alice (From seat alice-chat), "
                           f"Reply-To calls+web-<N>@{TURN_HOST} ")


def test_t8_run_in_echo_mode_says_nothing_of_turns(tmp_path, monkeypatch):
    """A seat nameable from the env (DC_MAILTO) changes nothing in echo mode.
    (The --mail-to FLAG without --legs is refused: PHONE-137, below.)"""
    from sessionphone.web import server as srvmod
    monkeypatch.setattr(srvmod.WebServer, "_own_origins", lambda self: {f"http://localhost:{self.port}"})
    monkeypatch.setenv("DC_MAILTO", "alice@x.y")
    out = []
    rc = srvmod.run(tmp_path / "web", port=0, host="127.0.0.1", tls=None, origins=(),
                    echo=out.append, until=lambda: asyncio.sleep(0))
    assert rc == 0 and not any("turns:" in m or "heard log" in m for m in out), out


@pytest.mark.parametrize(("kw", "named"), [
    ({"mail_to": "alice@x.y"}, "--mail-to"),
    ({"heard_dir": Path("/nowhere/heard")}, "--heard-dir"),
    ({"mail_to": "alice@x.y", "heard_dir": Path("/nowhere/heard")}, "--mail-to and --heard-dir"),
])
def test_echo_mode_refuses_the_turn_flags(tmp_path, kw, named):
    """PHONE-137 (folded in, PHONE-138 review N2): echo mode mails no one, so
    --mail-to and --heard-dir without --legs are refused (exit 2), not ignored,
    and nothing is made on disk."""
    from sessionphone.web import server as srvmod
    out = []
    rc = srvmod.run(tmp_path / "web", port=0, host="127.0.0.1", tls=None, origins=(),
                    echo=out.append, until=lambda: asyncio.sleep(0), **kw)
    assert rc == 2, out
    [line] = out
    assert f"{named} without --legs" in line and "mails no one" in line, line
    assert not (tmp_path / "web").exists()


def test_echo_mode_refuses_the_turn_flags_from_the_cli(tmp_path):
    r = CliRunner().invoke(cli.main, ["web", "--root", str(tmp_path), "--port", "0", "--mail-to", "alice@x.y"])
    assert r.exit_code == 2 and "--mail-to without --legs" in r.output, r.output
    assert not (tmp_path / "web").exists()


def test_t8_web_help_names_the_new_flags():
    r = CliRunner().invoke(cli.main, ["web", "--help"])
    assert r.exit_code == 0 and "--mail-to" in r.output and "--heard-dir" in r.output


# ---- a dropped socket mid-call (PHONE-137) -----------------------------------
#
# A phone locked mid-call drops the WebSocket with NO close frame. The server
# logs ONE JSON row in its own sink, {"conn": "closed", "code": 1006,
# "during": "call"}, websockets logs no traceback, and on_close still ends the
# call (SC1, SC2). Call start and end are JSON rows in the SAME sink as the auth
# rows, so the pane tells the whole story (SC3). A clean 1000 close adds no
# conn row (SC4).

def drop(ws) -> None:
    """Drop the client's TCP connection with NO close frame: a locked phone."""
    ws.transport.abort()


async def handled(srv) -> None:
    """Until every connection handler has finished, the library's logging included."""
    for _ in range(200):
        if not srv._server.handlers:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{len(srv._server.handlers)} connection handler(s) still running")


def conn_rows(rows) -> list[dict]:
    return [r for r in rows if "conn" in r]


def assert_no_traceback(caplog, capsys) -> None:
    """websockets logged no failure, and nothing printed a traceback."""
    bad = [r for r in caplog.records if r.name.startswith("websockets") and (r.levelno >= 40 or r.exc_info)]
    assert bad == [], [f"{r.name}: {r.getMessage()}" for r in bad]
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err and "ConnectionClosed" not in out + err, out + err


def test_a_drop_mid_call_logs_one_conn_line_and_ends_the_call(tmp_path, softkey, caplog, capsys):
    """SC1 (during a call) and SC2 (echo): one row, no traceback, the call ended."""
    async def body(rig):
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        call = rig.desk.sessions[n]
        drop(ws)
        await handled(rig.srv)
        [row] = conn_rows(rig.rows)
        assert row["conn"] == "closed" and row["code"] == 1006 and row["during"] == "call", row
        assert call.ended == "socket-closed" and call.pc is None
        assert rig.desk.sessions == {} and not rig.desk.registry.live()
    run(tmp_path, softkey, body, legs=False)
    assert_no_traceback(caplog, capsys)


def test_a_drop_mid_call_still_ends_it_and_releases_the_legs(short_root, softkey):
    """SC2 with the legs: the ears' tap hears "E call-ended", the call leaves the
    registry, and a new call is not busy. (A guard: on_close runs in the finally.)"""
    async def body(rig):
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        r, w, reply = await ears_tap(rig.webdir / "call-ears.sock", n)
        assert reply["ok"]
        drop(ws)
        while True:
            kind, payload = await ear_frame(r)
            if kind == b"E":
                assert payload == b"call-ended"
                break
        w.close()
        await handled(rig.srv)
        assert rig.desk.sessions == {} and not rig.desk.registry.live()
        assert any("socket-closed" in str(m) for m in rig.logs), rig.logs
        await rig.dial(await rig.signed_in())          # the line is free again
    run(short_root, softkey, body, legs=True)


def test_a_clean_close_mid_call_ends_it_with_no_conn_line(tmp_path, softkey, caplog, capsys):
    """SC4: a 1000 close is not news. The call ends as before; no conn row, no traceback."""
    async def body(rig):
        ws = await rig.signed_in()
        _, _, n = await rig.dial(ws)
        call = rig.desk.sessions[n]
        await ws.close()
        await handled(rig.srv)
        assert conn_rows(rig.rows) == [], rig.rows
        assert call.ended == "socket-closed" and rig.desk.sessions == {}
    run(tmp_path, softkey, body, legs=False)
    assert_no_traceback(caplog, capsys)


def json_rows(out: str) -> list[dict]:
    rows = []
    for line in out.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def test_call_start_and_end_are_json_rows_beside_the_auth_rows(tmp_path, softkey, monkeypatch, capsys):
    """SC3, through `sessionphone web` itself (server.run, echo mode): the pane is
    stdout, where the auth rows go (_log). The call's start and end must be JSON
    rows there too, stamped like them, naming the call and why it ended."""
    from sessionphone.web import server as srvmod
    monkeypatch.setattr(srvmod.WebServer, "_own_origins", lambda self: {f"http://localhost:{self.port}"})
    before, got, pcs = [], {}, []

    async def until():
        out = capsys.readouterr().out
        before.append(out)
        port = int(re.search(r"http://127\.0\.0\.1:(\d+)/", out)[1])
        code = re.search(r"enrol code: (\S+)", out)[1]
        origin = f"http://localhost:{port}"
        ws = await connect(f"ws://127.0.0.1:{port}/ws", origin=origin)
        await recv(ws)
        o = await recv(ws)
        await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                  **softkey(origin).create(o["challenge"])}))
        assert (await recv(ws))["type"] == "auth-ok"
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        pcs.append(pc)
        pc.addTrack(Tone())
        await pc.setLocalDescription(await pc.createOffer())
        await ws.send(json.dumps({"type": "call", "sdp": pc.localDescription.sdp}))
        r = await recv(ws)
        assert r["type"] == "answer", r
        got["n"] = r["call"]
        await ws.send(json.dumps({"type": "hangup"}))
        assert (await recv(ws))["type"] == "ended"
        await pc.close()

    rc = srvmod.run(tmp_path / "web", port=0, host="127.0.0.1", tls=None, origins=(), echo=print,
                    until=lambda: asyncio.wait_for(until(), 40))
    out = "".join(before) + capsys.readouterr().out
    assert rc == 0, out
    rows = json_rows(out)
    assert [r["result"] for r in rows if r.get("auth") == "enrol"] == ["ok"], out
    n = got["n"]
    mine = [r for r in rows if r.get("call") == n]
    started = [r for r in mine if "started" in json.dumps(r)]
    ended = [r for r in mine if "ended" in json.dumps(r)]
    assert len(started) == 1 and len(ended) == 1, f"no JSON call rows for call {n} in the pane:\n{out}"
    assert "hangup" in json.dumps(ended[0]) and started[0]["ts"] and ended[0]["ts"]


# ---- PHONE-135 SC2: the caller's name, ONE setting, read by the turn mail and the auth-ok frame ----

def test_the_desk_hands_the_setting_to_each_calls_follower(tmp_path, fake_message):
    """The turn mail's speaker is the desk's caller (web/names.py), not a default of turns'."""
    seat, heard_dir = seat_and_heard(tmp_path)
    desk = CallDesk(tmp_path / "web", legs=True, seat=seat, heard=heard_dir, voicemode=None, spool=None,
                    log=lambda m: None, caller="Ada")
    call = types.SimpleNamespace(n=7, echo=None)
    f = desk.make_follower(call)
    assert f.caller == "Ada"
    heard_line(heard_dir, 7, 101, text="are you there")

    async def main():
        for rec, new in f.read():
            await f.take(rec, new)

    asyncio.run(main())
    (m,) = fake_message.mails()
    assert m["Subject"] == "[web 7] Ada: are you there" and "\nfrom: Ada\n" in m.get_content()


def test_the_desk_reads_dc_caller_name_when_none_is_handed_in(tmp_path, monkeypatch):
    seat, heard_dir = seat_and_heard(tmp_path)
    monkeypatch.setenv("DC_CALLER_NAME", "Bea")
    desk = CallDesk(tmp_path / "web", legs=True, seat=seat, heard=heard_dir, voicemode=None, spool=None)
    assert desk.caller == "Bea"
    monkeypatch.delenv("DC_CALLER_NAME")
    assert CallDesk(tmp_path / "web", legs=True, seat=seat, heard=heard_dir).caller == "owner"


@pytest.mark.parametrize(("flag", "env", "want"), [
    (None, None, "owner"), ("Ada", None, "Ada"), (None, "Bea", "Bea"), ("Ada", "Bea", "Ada"), ("  ", "Bea", "Bea")])
def test_run_reads_the_setting_once_and_hands_it_to_both_sites(tmp_path, monkeypatch, flag, env, want):
    """server.run: --caller-name, else $DC_CALLER_NAME, else `owner`, to the server
    (auth-ok, a passkey's label) AND the desk (each turn's mail): one value."""
    from sessionphone.web import server as srvmod
    if env is not None:
        monkeypatch.setenv("DC_CALLER_NAME", env)
    monkeypatch.setattr(srvmod.WebServer, "_own_origins", lambda self: {f"http://localhost:{self.port}"})
    seen = {}
    real_start = srvmod.WebServer.start

    async def spy_start(self):
        seen["server"], seen["desk"] = self.caller, self.desk.caller
        return await real_start(self)

    monkeypatch.setattr(srvmod.WebServer, "start", spy_start)
    out = []
    rc = srvmod.run(tmp_path / "web", port=0, host="127.0.0.1", tls=None, origins=(), caller_name=flag,
                    echo=out.append, until=lambda: asyncio.sleep(0))
    assert rc == 0, out
    assert seen == {"server": want, "desk": want}


def test_the_cli_hands_caller_name_to_the_server(tmp_path, monkeypatch):
    from sessionphone.web import server as srvmod
    got = {}
    monkeypatch.setattr(srvmod, "run", lambda webdir, **kw: got.update(kw) or 0)
    r = CliRunner().invoke(cli.main, ["web", "--root", str(tmp_path), "--caller-name", "Ada"])
    assert r.exit_code == 0, r.output
    assert got["caller_name"] == "Ada"
    r = CliRunner().invoke(cli.main, ["web", "--root", str(tmp_path)])
    assert r.exit_code == 0 and got["caller_name"] is None      # the reader's fallbacks: env, then the role word


def test_the_web_help_names_caller_name_and_its_fallbacks():
    r = CliRunner().invoke(cli.main, ["web", "--help"])
    flat = " ".join(r.output.split())
    assert r.exit_code == 0 and "--caller-name NAME" in flat
    assert "$DC_CALLER_NAME" in flat and "'owner'" in flat and "grants nothing" in flat

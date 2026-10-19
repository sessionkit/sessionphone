"""The ear leg: a live call is served as a mic - his 16 kHz PCM, in order, then
the end. A real socket and a real loop; a fake call."""
import asyncio
import json
import pathlib
import struct
import tempfile

import numpy as np
import pytest  # noqa: F401 - bin/bridge-check runs a file under pytest only if it imports it;
                # without this line it ran as a script and ran none of these tests

from sessionphone.bridge import earleg


class Session:
    def __init__(self, live=True, chat_id=12):
        self.pc = object() if live else None
        self.chat_id, self.chat_name = chat_id, "Bob Team"


class Manager:
    def __init__(self, sessions):
        self.sessions = sessions


def pcm(value, ms=20):
    return (np.ones(16 * ms, dtype="<i2") * value).tobytes()


async def read_frames(r, limit=1000):
    out = []
    for _ in range(limit):
        head = await r.readexactly(5)
        kind, n = head[:1], struct.unpack(">I", head[1:])[0]
        out.append((kind, await r.readexactly(n) if n else b""))
        if kind == b"E":
            break
    return out


async def listen(mgr, header, feed=(), end_why="connection closed", session=None):
    path = pathlib.Path(tempfile.mkdtemp(prefix="el-")) / "s.sock"
    logs = []
    server = await earleg.serve(mgr, path, logs.append)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    r, w = await asyncio.open_unix_connection(str(path))
    w.write((json.dumps(header) + "\n").encode())
    first = json.loads(await r.readline())
    frames = []
    if first.get("ok") and "probe" not in header:
        await asyncio.sleep(0.05)            # the tap is on the session now
        for chunk in feed:
            earleg.feed(session, chunk)
        earleg.end(session, end_why)
        frames = await asyncio.wait_for(read_frames(r), 5)
    w.close()
    server.close()
    await asyncio.sleep(0.05)
    return first, frames, logs


def run(coro):
    return asyncio.run(coro)


def test_probe_lists_live_calls_and_the_rate():
    mgr = Manager({1139: Session(), 9: Session(live=False)})
    first, _, _ = run(listen(mgr, {"probe": True}))
    assert first["ok"] and first["calls"] == [1139] and first["rate"] == 16000


def test_refuses_a_call_that_is_not_live():
    mgr = Manager({1139: Session()})
    first, _, _ = run(listen(mgr, {"call": 77}))
    assert not first["ok"] and "77" in first["error"] and first["calls"] == [1139]


def test_his_audio_in_order_then_the_end():
    s = Session()
    mgr = Manager({1139: s})
    first, frames, logs = run(listen(mgr, {"call": 1139}, [pcm(1), pcm(2), pcm(3)], session=s))
    assert first["ok"] and first["call"] == 1139 and first["rate"] == 16000 and first["chat"] == 12
    kinds = [k for k, _ in frames]
    assert kinds == [b"A", b"A", b"A", b"E"]
    vals = [int(np.frombuffer(p, dtype="<i2")[0]) for k, p in frames if k == b"A"]
    assert vals == [1, 2, 3]
    assert frames[-1][1] == b"connection closed"
    assert s.ear_taps == []                  # the listener is gone from the session
    assert any("sent" in line for line in logs)


def test_null_call_means_the_newest_live_call():
    s_old, s_new = Session(), Session()
    mgr = Manager({1120: s_old, 1139: s_new})
    first, frames, _ = run(listen(mgr, {"call": None}, [pcm(7)], session=s_new))
    assert first["ok"] and first["call"] == 1139 and [k for k, _ in frames] == [b"A", b"E"]


def test_a_slow_listener_loses_the_oldest_not_the_newest():
    t = earleg.Tap()
    n = t.q.maxsize
    for i in range(n + 10):
        t.put(pcm(i))
    assert t.dropped == 10 and t.q.qsize() == n
    first = int(np.frombuffer(t.q.get_nowait(), dtype="<i2")[0])
    assert first == 10                       # the 10 oldest went; the newest stayed


def test_feed_with_no_listener_is_free():
    earleg.feed(Session(), pcm(1))           # no taps: nothing happens, nothing raises
    earleg.end(Session())


def test_starting_beside_the_mouth_leg_leaves_the_umask_alone():
    """Both legs once started together and the umask was left at 0o177,
    so the next call dir was 0600 and the dial failed."""
    import os
    from sessionphone.bridge import mouthleg

    async def both():
        d = pathlib.Path(tempfile.mkdtemp(prefix="um-"))
        mgr = Manager({})
        a = await asyncio.gather(mouthleg.serve(mgr, d / "m.sock", lambda m: None),
                                 earleg.serve(mgr, d / "e.sock", lambda m: None))
        modes = (oct((d / "m.sock").stat().st_mode & 0o777), oct((d / "e.sock").stat().st_mode & 0o777))
        for srv in a:
            srv.close()
        return modes

    before = os.umask(0o022); os.umask(before)
    modes = run(both())
    after = os.umask(before); os.umask(after)
    assert after == before and modes == ("0o600", "0o600")


def test_each_tap_says_where_it_starts_in_the_call_wav(tmp_path):
    """The side-car names call audio by reading caller-call.wav at wav_s + t0 (the
    wall clock drifted 2.0 s in 21 minutes on call 54541; audio position does not)."""
    s = Session()
    s.dir, s.wav_samples = tmp_path, 16000 * 42 + 8000          # 42.5 s already written
    mgr = Manager({1139: s})
    first, frames, _ = run(listen(mgr, {"call": 1139}, [pcm(1), pcm(2)], session=s))
    assert first["ok"] and first["wav_s"] == 42.5
    rows = [json.loads(x) for x in (tmp_path / "ear-taps.jsonl").read_text().splitlines()]
    assert rows[0]["call"] == 1139 and rows[0]["wav_s"] == 42.5 and "t" in rows[0]
    assert rows[1]["end"] == "call-ended" and rows[1]["dropped"] == 0 and rows[1]["sent_s"] == 0.04
    assert oct((tmp_path / "ear-taps.jsonl").stat().st_mode & 0o777) == "0o600"


def test_a_session_with_no_dir_still_serves(tmp_path):
    s = Session()                                               # no dir, no wav count
    first, frames, _ = run(listen(Manager({1139: s}), {"call": 1139}, [pcm(1)], session=s))
    assert first["ok"] and first["wav_s"] == 0 and frames[-1][0] == b"E"

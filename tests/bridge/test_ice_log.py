"""PHONE-114 R4 (SC3): a call that never connects says why, without addresses.

Before this, a failed call logged only `connection failed`. Now the ICE
gathering and connection states are logged, with this side's candidate counts
by type when gathering completes, and the selected pair's types (or `none`,
with the remote counts) when the checks end. Types, states and counts only:
no line may carry an IP address or a port.

A fake peer connection (a pyee AsyncIOEventEmitter, aiortc's own base) is
stepped through the states. One test connects two REAL aiortc peer
connections over loopback, so the private aioice read is proven against the
pinned aioice, not against a fake of it.
"""
import asyncio
import re
import types

import pytest
from pyee.asyncio import AsyncIOEventEmitter

from sessionphone.bridge import calls

IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
IPV6 = re.compile(r"\b[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}\b")
LOCAL_IP, REMOTE_IP, PORT = "192.0.2.10", "198.51.100.20", "54321"


def _cand(t, host=LOCAL_IP):
    return types.SimpleNamespace(type=t, host=host, ip=host, port=int(PORT),
                                 address=f"{host}:{PORT}")


class _Ice:
    """An RTCIceTransport as far as the logging reads it: the gatherer's local
    candidates, the remote ones, and aioice's private connection."""
    def __init__(self, local=(), remote=(), nominated=None):
        self.iceGatherer = types.SimpleNamespace(getLocalCandidates=lambda: list(local))
        self._remote = list(remote)
        self._connection = types.SimpleNamespace(_nominated=dict(nominated or {}))

    def getRemoteCandidates(self):
        return list(self._remote)


class FakePC(AsyncIOEventEmitter):
    def __init__(self, ice):
        super().__init__()
        self.iceGatheringState, self.iceConnectionState = "new", "new"
        dtls = types.SimpleNamespace(transport=ice)
        self._tx = [types.SimpleNamespace(receiver=types.SimpleNamespace(transport=dtls))]
        self.sctp = types.SimpleNamespace(transport=dtls)       # bundled: the same transport

    def getTransceivers(self):
        return self._tx

    def gathering(self, state):
        self.iceGatheringState = state
        self.emit("icegatheringstatechange")

    def ice(self, state):
        self.iceConnectionState = state
        self.emit("iceconnectionstatechange")


@pytest.fixture
def logs(monkeypatch):
    out = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(out.append, None, None))
    monkeypatch.setattr(calls, "_call_id", None)
    return out


def _no_address(lines):
    text = "\n".join(lines)
    assert not IPV4.search(text), text
    assert not IPV6.search(text), text
    assert PORT not in text, text


def _pair(local, remote):
    return types.SimpleNamespace(local_candidate=_cand(local), remote_candidate=_cand(remote, REMOTE_IP))


# ---- SC3: a call whose checks fail ----

def test_a_failed_call_logs_each_transition_in_order_then_pair_none(logs):
    ice = _Ice(local=[_cand("host"), _cand("host"), _cand("relay")],
               remote=[_cand("host", REMOTE_IP), _cand("srflx", REMOTE_IP)])
    pc = calls.watch_ice(FakePC(ice))
    for g in ("gathering", "complete"):
        pc.gathering(g)
    for s in ("checking", "failed"):
        pc.ice(s)
    assert logs == [
        "CALL ice gathering gathering",
        "CALL ice gathering complete: local host=2 srflx=0 relay=1",
        "CALL ice state checking",
        "CALL ice state failed",
        "CALL ice pair none: no candidate pair succeeded (remote host=1 srflx=1 relay=0)",
    ]
    _no_address(logs)


def test_a_failure_after_a_pair_was_chosen_names_the_pair(logs):
    # `connection failed` with a pair means ICE found a path and DTLS failed
    pc = calls.watch_ice(FakePC(_Ice(nominated={1: _pair("srflx", "relay")})))
    pc.ice("failed")
    assert logs == ["CALL ice state failed", "CALL ice pair local=srflx remote=relay"]


def test_a_remote_with_no_candidates_at_all_says_zero(logs):
    pc = calls.watch_ice(FakePC(_Ice()))
    pc.ice("failed")
    assert logs[-1] == "CALL ice pair none: no candidate pair succeeded (remote host=0 srflx=0 relay=0)"


# ---- SC3: the selected pair at completed ----

def test_completed_logs_the_selected_pair_types(logs):
    pc = calls.watch_ice(FakePC(_Ice(nominated={1: _pair("relay", "host")})))
    pc.ice("checking")
    pc.ice("completed")
    assert logs == ["CALL ice state checking", "CALL ice state completed",
                    "CALL ice pair local=relay remote=host"]
    _no_address(logs)


def test_completed_with_no_pair_yet_logs_no_pair_line(logs):
    pc = calls.watch_ice(FakePC(_Ice()))
    pc.ice("completed")
    assert logs == ["CALL ice state completed"]


def test_closed_logs_the_state_only(logs):
    pc = calls.watch_ice(FakePC(_Ice(nominated={1: _pair("host", "host")})))
    pc.ice("closed")
    assert logs == ["CALL ice state closed"]


def test_an_unusual_type_is_counted_not_dropped(logs):
    ice = _Ice(remote=[_cand("prflx", REMOTE_IP), _cand("host", REMOTE_IP)])
    pc = calls.watch_ice(FakePC(ice))
    pc.ice("failed")
    assert logs[-1].endswith("(remote host=1 srflx=0 relay=0 prflx=1)")


# ---- the private aioice read is guarded ----

def test_a_changed_aioice_never_crashes_the_call(logs):
    ice = _Ice()
    del ice._connection._nominated             # as if a pin bump renamed it
    pc = calls.watch_ice(FakePC(ice))
    pc.ice("completed")
    pc.ice("failed")
    assert logs == ["CALL ice state completed", "CALL ice pair unknown: AttributeError",
                    "CALL ice state failed", "CALL ice pair unknown: AttributeError"]


def test_a_gatherer_that_raises_still_logs_complete(logs):
    ice = _Ice()
    ice.iceGatherer.getLocalCandidates = lambda: 1 / 0
    pc = calls.watch_ice(FakePC(ice))
    pc.gathering("complete")
    assert logs == ["CALL ice gathering complete: local unknown (ZeroDivisionError)"]


def test_ice_pair_prefers_component_one():
    pc = FakePC(_Ice(nominated={2: _pair("host", "host"), 1: _pair("relay", "srflx")}))
    assert calls.ice_pair(pc) == ("relay", "srflx")
    assert calls.ice_pair(FakePC(_Ice())) is None


def test_distinct_transports_are_each_read_once():
    a, b = _Ice(local=[_cand("host")]), _Ice(local=[_cand("relay")])
    pc = FakePC(a)
    pc.sctp = types.SimpleNamespace(transport=types.SimpleNamespace(transport=b))
    pc._tx.append(pc._tx[0])                   # a second transceiver on the same transport
    assert calls._ice_transports(pc) == [a, b]


def test_build_pc_registers_the_ice_watch(monkeypatch):
    seen = []
    monkeypatch.setattr(calls, "watch_ice", lambda pc: seen.append(pc))
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(lambda m: None, None, None))
    s = object.__new__(calls.CallSession)
    s.account = types.SimpleNamespace(ice_servers=list)
    s.voice = None                             # addTrack is faked below

    async def go():
        monkeypatch.setattr(calls.RTCPeerConnection, "addTrack", lambda self, t: None)
        pc = await s._build_pc()
        await pc.close()
        return pc
    pc = asyncio.run(go())
    assert seen == [pc]


# ---- the real thing: two aiortc peer connections over loopback ----

def test_a_real_loopback_call_logs_a_host_pair_and_no_address(logs):
    """The private read against the pinned aioice: host candidates only (no
    ICE servers, so no STUN or TURN traffic leaves the machine)."""
    from aiortc import RTCConfiguration, RTCPeerConnection

    async def go():
        cfg = RTCConfiguration(iceServers=[])
        a, b = RTCPeerConnection(cfg), RTCPeerConnection(cfg)
        calls.watch_ice(a)
        a.createDataChannel("x")
        try:
            await a.setLocalDescription(await a.createOffer())
            await b.setRemoteDescription(a.localDescription)
            await b.setLocalDescription(await b.createAnswer())
            await a.setRemoteDescription(b.localDescription)
            for _ in range(200):
                if a.iceConnectionState in ("completed", "failed"):
                    break
                await asyncio.sleep(0.05)
            return a.iceConnectionState
        finally:
            await a.close()
            await b.close()
    state = asyncio.run(go())
    assert state == "completed", logs
    assert "CALL ice pair local=host remote=host" in logs
    assert re.fullmatch(r"CALL ice gathering complete: local host=[1-9]\d* srflx=0 relay=0",
                        next(m for m in logs if m.startswith("CALL ice gathering complete"))), logs
    _no_address(logs)

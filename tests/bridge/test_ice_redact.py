"""PHONE-114 R6 (SC7): bot.log never carries the relay's TURN credential or
the caller's address.

`ice servers:` used to dump account.ice_servers() as JSON, username and
credential included, and `added trickled candidate` logged the raw candidate
line, the caller's IP and port. Now: the URL count with each URL's scheme and
host, the candidate's TYPE, and on a parse error the exception's type only.
No socket opens: RTCPeerConnection is faked in _build_pc.
"""
import asyncio
import json
import types

import pytest
from pyee.asyncio import AsyncIOEventEmitter

from sessionphone.bridge import calls

USER, SECRET = "u-1907238896:agent", "c2VjcmV0LWNyZWRlbnRpYWwtOTk="
IP, PORT = "203.0.113.7", "54321"
TURN = {"urls": ["turn:relay.example.org:3478?transport=udp",
                 "turns:relay.example.org:5349?transport=tcp"],
        "username": USER, "credential": SECRET}


@pytest.fixture
def logs(monkeypatch):
    out = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(out.append, None, None))
    monkeypatch.setattr(calls, "_call_id", None)
    return out


def _absent(logs, *needles):
    text = "\n".join(logs)
    for n in needles:
        assert n not in text, f"{n!r} leaked: {text}"


# ---- (a) the ice servers line ----

MADE = []      # every FakePC built, newest last


class FakePC(AsyncIOEventEmitter):

    def __init__(self, configuration=None):
        super().__init__()
        self.configuration = configuration
        self.sctp = None
        MADE.append(self)

    def createDataChannel(self, label, **k):
        return AsyncIOEventEmitter()

    def addTrack(self, track):
        pass

    def getTransceivers(self):
        return []


def test_a_build_pc_logs_the_urls_never_the_credential(logs, monkeypatch):
    MADE.clear()
    monkeypatch.setattr(calls, "RTCPeerConnection", FakePC)
    s = object.__new__(calls.CallSession)
    s.account = types.SimpleNamespace(ice_servers=lambda: [TURN, {"urls": "stun:relay.example.org:3478"}])
    s.voice, s.tasks = None, []
    asyncio.run(s._build_pc())
    line, = [m for m in logs if m.startswith("CALL ice servers:")]
    assert line == ("CALL ice servers: 3 url(s): turn:relay.example.org "
                    "turns:relay.example.org stun:relay.example.org")
    _absent(logs, USER, SECRET, "3478", "5349", "transport=")
    # the relay still gets its credential: only the LOG is redacted
    turn, = [x for x in MADE[0].configuration.iceServers if x.username]
    assert (turn.username, turn.credential) == (USER, SECRET)


def test_the_summary_never_echoes_userinfo_a_port_or_a_query():
    assert calls.ice_servers_summary([{"urls": f"turn:{USER}:{SECRET}@relay.example.org:3478?x=1"}]) \
        == "ice servers: 1 url(s): turn:relay.example.org"


@pytest.mark.parametrize("ice,line", [
    (None, "ice servers: 0 url(s)"),
    ([], "ice servers: 0 url(s)"),
    ([{"username": USER}], "ice servers: 0 url(s)"),
    ([{"urls": "turn:[2001:db8::1]:3478"}], "ice servers: 1 url(s): turn:[2001:db8::1]"),
    ([{"urls": ["not a url", "turn:"]}], "ice servers: 2 url(s): ?:? turn:?"),
])
def test_the_summary_counts_what_the_relay_sent(ice, line):
    # the count is before _build_pc drops bracketed IPv6: that is what was sent
    assert calls.ice_servers_summary(ice) == line


@pytest.mark.parametrize("ice", [5, [{"urls": 5}], object()])
def test_the_summary_never_raises(ice):
    out = calls.ice_servers_summary(ice)
    assert out.startswith("ice servers: unreadable (") and out.endswith(")")


# ---- (b) a trickled candidate: its type only ----

class _TricklePC:
    def __init__(self, fail=None):
        self.added, self.fail = [], fail

    async def addIceCandidate(self, cand):
        if self.fail:
            raise self.fail
        self.added.append(cand)


def _trickle(s, line):
    asyncio.run(s._add_candidate(json.dumps(
        {"candidate": line, "sdpMid": "0", "sdpMLineIndex": 0})))


def _session(pc):
    s = object.__new__(calls.CallSession)
    s.pc = pc
    return s


def test_b_a_trickled_candidate_logs_its_type_never_the_address(logs):
    pc = _TricklePC()
    _trickle(_session(pc), f"candidate:842163049 1 udp 1677729535 {IP} {PORT} typ srflx "
                           "raddr 0.0.0.0 rport 0 generation 0")
    assert logs == ["CALL added trickled candidate type=srflx"]
    assert pc.added[0].ip == IP and pc.added[0].port == int(PORT)   # still added, intact
    _absent(logs, IP, PORT)


# ---- (c) a malformed candidate: the exception's type only ----

def test_c_a_malformed_candidate_logs_the_exception_type_only(logs):
    pc = _TricklePC()
    _trickle(_session(pc), f"candidate:1 1 udp 1 {IP} 5432x1 typ host")   # int() quotes it
    assert logs == ["CALL trickled candidate ignored: ValueError"]
    assert pc.added == []
    _absent(logs, IP, "5432x1")


def test_a_refused_candidate_never_quotes_the_error(logs):
    pc = _TricklePC(fail=RuntimeError(f"cannot add {IP}:{PORT}"))
    _trickle(_session(pc), f"candidate:1 1 udp 1 {IP} {PORT} typ host")
    assert logs == ["CALL trickled candidate ignored: RuntimeError"]
    _absent(logs, IP, PORT)


def test_an_end_of_candidates_message_is_still_silent(logs):
    _trickle(_session(_TricklePC()), "")
    assert logs == []

"""PHONE-329 (d): each call's ICE checks, by pair TYPE and seconds, never an address.

An incoming call sat 42 s in ICE `checking` (PHONE-317, call 158170). The
suspected cause, a caller's check reaching a pair whose own check was still
IN_PROGRESS, was not measurable from the log. ice_checks writes, when a call's
checking ends, a summary line (pair count, the caller's USE-CANDIDATE, peer
checks that hit an IN_PROGRESS pair) and one line per pair type (time to
succeeded or failed), all in seconds from checking-start.

Most tests feed aioice's own record texts, carrying fake Connection, pair and
candidate objects, to a ChecksWatch and assert the EXACT lines. The fakes'
reprs carry addresses on purpose: a record's args are never formatted. One
test connects two REAL aioice Connections over loopback, so the records read
are proven against the pinned aioice 0.10.2, not against a fake of it.
"""
import asyncio
import logging
import re

import aioice
import aioice.ice
import aioice.stun as stun
import pytest
from aioice.ice import CandidatePair

from sessionphone.bridge import calls, ice_checks

S = CandidatePair.State
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
IPV6 = re.compile(r"[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}")
LOCAL_IP, REMOTE_IP, RELAY_IP, V6 = "192.0.2.10", "198.51.100.20", "203.0.113.7", "2001:db8::1"
PORT, UFRAG, RFRAG, PWD = 54321, "Uf4gLoc", "Rf4gRem", "s3cretPassw0rd"
T0 = 1000.0


class Cand:
    def __init__(self, type_, host, port=PORT):
        self.type, self.host, self.port = type_, host, port
        self.component, self.foundation = 1, f"{type_}{host}"

    def __repr__(self):
        return f"Candidate({self.host} {self.port} {self.type})"


class Proto:
    def __init__(self, local):
        self.local_candidate = local

    def __repr__(self):
        return f"protocol({self.local_candidate.host}:{self.local_candidate.port})"


class Pair:
    def __init__(self, proto, remote, state=S.FROZEN):
        self.protocol, self.remote_candidate, self.state = proto, remote, state

    @property
    def local_candidate(self):
        return self.protocol.local_candidate

    @property
    def remote_addr(self):
        return (self.remote_candidate.host, self.remote_candidate.port)

    def __repr__(self):
        return f"CandidatePair({self.local_candidate.host} -> {self.remote_addr})"


class Conn:
    def __init__(self, pairs, remotes, controlling=False):
        self._check_list, self._remote_candidates = list(pairs), list(remotes)
        self.ice_controlling = controlling
        self.local_username, self.remote_username = UFRAG, RFRAG
        self.local_password = PWD

    def __repr__(self):
        return f"Connection({UFRAG}:{RFRAG} {PWD} {LOCAL_IP} {V6})"


@pytest.fixture
def out():
    return []


@pytest.fixture
def watch(out, monkeypatch):
    monkeypatch.setattr(stun, "RETRY_RTO", 2.0)       # the bot's DC_STUN_RTO default
    lg = logging.getLogger("test.ice_checks")
    return ice_checks.ChecksWatch(lg, logging.NOTSET, sink=out.append)


def rec(msg, *args, at=0.0, level=logging.INFO):
    r = logging.LogRecord("aioice.ice", level, __file__, 1, msg, args, None)
    r.created = T0 + at
    return r


def state(conn, pair, new, at):
    """aioice's check_state record, made when fed: the pair's state changes in
    feed order, as aioice changes it, so a peer check reads the state of its time."""
    def make():
        old, pair.state = pair.state, new
        return rec(ice_checks.CHECK_STATE, conn, pair, old, new, at=at)
    return make


def request(conn, proto, addr, at, use=False, username=None, cls=stun.Class.REQUEST):
    m = stun.Message(message_method=stun.Method.BINDING, message_class=cls)
    m.attributes["USERNAME"] = username or f"{UFRAG}:{RFRAG}"
    if use:
        m.attributes["USE-CANDIDATE"] = None
    return rec(ice_checks.INBOUND, conn, proto, addr, m, at=at, level=logging.DEBUG)


def before(record):
    """Whether a NOTSET logger under the root passed this record before."""
    return record.levelno >= logging.getLogger().getEffectiveLevel()


def feed(watch, *records):
    for r in records:
        watch.filter(r() if callable(r) else r)


# ---- the 158170 shape: the caller's USE-CANDIDATE hits a pair still IN_PROGRESS

def stall(watch, local_ip=LOCAL_IP, remote_ip=REMOTE_IP, relay_ip=RELAY_IP):
    h1, h2 = Proto(Cand("host", local_ip)), Proto(Cand("host", V6, PORT + 1))
    a, relay = Cand("host", remote_ip), Cand("relay", relay_ip, PORT + 2)
    p1, p2, p3 = Pair(h1, a), Pair(h2, a), Pair(h1, relay)
    conn = Conn([p1, p2, p3], [a, relay])
    feed(watch,
         state(conn, p1, S.WAITING, 0.0),              # checking-start
         state(conn, p2, S.WAITING, 0.0),
         state(conn, p1, S.IN_PROGRESS, 0.3),
         state(conn, p2, S.IN_PROGRESS, 0.32),
         state(conn, p3, S.IN_PROGRESS, 0.34),
         request(conn, h1, (a.host, a.port), 2.1, use=True),                  # hits p1 in progress
         request(conn, h1, (a.host, a.port), 2.2, use=True),                  # its retransmit
         request(conn, h1, (a.host, a.port), 2.5, username="nope:nope"),      # aioice says 400
         request(conn, h1, (a.host, a.port), 2.6, cls=stun.Class.RESPONSE),   # not a check
         request(conn, h2, ("192.0.2.99", 4000), 5.0),                        # a new prflx pair
         state(conn, p3, S.FAILED, 20.0),
         state(conn, p1, S.SUCCEEDED, 41.8),
         rec(ice_checks.COMPLETED, conn, at=41.9))
    return conn


STALL = [
    "ice checks: end=completed@41.9s rto=2s pairs=3 peer-checks=3 "
    "use-candidate=2.1s:host/host:in-progress:started@0.3s:succeeded@41.8s in-progress-hits=2 "
    "first-hit=2.1s:host/host:started@0.3s",
    "ice checks host/host: pairs=2 succeeded=1@41.8s failed=0 open=1",
    "ice checks host/relay: pairs=1 succeeded=0 failed=1@20.0s open=0",
]


def test_a_use_candidate_on_an_in_progress_pair_is_logged_exactly(watch, out):
    stall(watch)
    assert out == STALL


def test_the_lines_are_written_once_per_connection(watch, out):
    conn = stall(watch)
    p = conn._check_list[1]
    feed(watch,
         state(conn, p, S.SUCCEEDED, 42.0),                 # after its lines: not counted
         request(conn, p.protocol, p.remote_addr, 45.0),    # a consent check
         rec(ice_checks.CONNECTION_LOST, conn, p.protocol, None, at=60.0, level=logging.DEBUG))
    assert out == STALL


def test_the_controlling_side_hears_no_use_candidate(watch, out):
    h, a = Proto(Cand("host", LOCAL_IP)), Cand("host", REMOTE_IP)
    p = Pair(h, a)
    conn = Conn([p], [a], controlling=True)
    feed(watch,
         state(conn, p, S.WAITING, 0.0),
         state(conn, p, S.IN_PROGRESS, 0.02),
         request(conn, h, (a.host, a.port), 0.5),     # the callee's own check: pair in progress
         state(conn, p, S.SUCCEEDED, 1.04),
         rec(ice_checks.COMPLETED, conn, at=1.04))
    assert out == [
        "ice checks: end=completed@1.0s rto=2s pairs=1 peer-checks=1 use-candidate=none "
        "in-progress-hits=1 first-hit=0.5s:host/host:started@0.0s",
        "ice checks host/host: pairs=1 succeeded=1@1.0s failed=0 open=0",
    ]


def test_a_failed_check_list_says_failed(watch, out):
    h, a, r = Proto(Cand("host", LOCAL_IP)), Cand("host", REMOTE_IP), Cand("relay", RELAY_IP)
    p1, p2 = Pair(h, a), Pair(h, r)
    conn = Conn([p1, p2], [a, r])
    feed(watch,
         state(conn, p1, S.WAITING, 0.0),
         state(conn, p1, S.IN_PROGRESS, 0.0),
         state(conn, p2, S.IN_PROGRESS, 0.02),
         state(conn, p1, S.FAILED, 63.5),
         state(conn, p2, S.FAILED, 63.52),
         rec(ice_checks.FAILED, conn, at=63.52))
    assert out == [
        "ice checks: end=failed@63.5s rto=2s pairs=2 peer-checks=0 use-candidate=none "
        "in-progress-hits=0 first-hit=none",
        "ice checks host/host: pairs=1 succeeded=0 failed=1@63.5s open=0",
        "ice checks host/relay: pairs=1 succeeded=0 failed=1@63.5s open=0",
    ]


def test_a_call_closed_mid_checking_says_closed_and_counts_frozen_pairs(watch, out):
    h, a, s = Proto(Cand("host", LOCAL_IP)), Cand("host", REMOTE_IP), Cand("srflx", REMOTE_IP, 9)
    p1, p2 = Pair(h, a), Pair(h, s)              # p2 never leaves FROZEN: no record names it
    conn = Conn([p1, p2], [a, s])
    feed(watch,
         state(conn, p1, S.WAITING, 0.0),
         state(conn, p1, S.IN_PROGRESS, 0.0),
         rec(ice_checks.CONNECTION_LOST, conn, h, None, at=12.0, level=logging.DEBUG))
    assert out == [
        "ice checks: end=closed@12.0s rto=2s pairs=2 peer-checks=0 use-candidate=none "
        "in-progress-hits=0 first-hit=none",
        "ice checks host/host: pairs=1 succeeded=0 failed=0 open=1",
        "ice checks host/srflx: pairs=1 succeeded=0 failed=0 open=1",
    ]


def test_a_peer_check_before_checking_start_is_counted_at_a_negative_time(watch, out):
    h, a = Proto(Cand("host", LOCAL_IP)), Cand("host", REMOTE_IP)
    p = Pair(h, a)
    conn = Conn([p], [a])
    feed(watch,
         request(conn, h, (a.host, a.port), -0.5, use=True),    # p still FROZEN
         state(conn, p, S.WAITING, 0.0),
         state(conn, p, S.IN_PROGRESS, 0.0),
         state(conn, p, S.SUCCEEDED, 0.4),
         rec(ice_checks.COMPLETED, conn, at=0.4))
    assert out[0] == ("ice checks: end=completed@0.4s rto=2s pairs=1 peer-checks=1 "
                      "use-candidate=-0.5s:host/host:frozen:started@0.0s:succeeded@0.4s "
                      "in-progress-hits=0 first-hit=none")


def test_a_use_candidate_on_a_pair_aioice_has_not_made_yet_says_new(watch, out):
    h, a = Proto(Cand("host", LOCAL_IP)), Cand("host", REMOTE_IP)
    p = Pair(h, a)
    conn = Conn([p], [a])
    feed(watch,
         state(conn, p, S.WAITING, 0.0),
         request(conn, h, ("192.0.2.200", 7), 1.0, use=True),   # unknown: peer reflexive
         rec(ice_checks.COMPLETED, conn, at=1.5))
    assert "use-candidate=1.0s:host/prflx:new:started@-:succeeded@- " in out[0], out


def test_two_connections_are_kept_apart(watch, out):
    h, a = Proto(Cand("host", LOCAL_IP)), Cand("host", REMOTE_IP)
    p, q = Pair(h, a), Pair(h, a)
    c1, c2 = Conn([p], [a]), Conn([q], [a])
    feed(watch,
         state(c1, p, S.WAITING, 0.0),
         state(c2, q, S.WAITING, 5.0),
         state(c2, q, S.SUCCEEDED, 6.0),
         rec(ice_checks.COMPLETED, c2, at=6.0))
    assert out == [
        "ice checks: end=completed@1.0s rto=2s pairs=1 peer-checks=0 use-candidate=none "
        "in-progress-hits=0 first-hit=none",
        "ice checks host/host: pairs=1 succeeded=1@1.0s failed=0 open=0",
    ]


@pytest.mark.parametrize("rto,word", [(0.5, "rto=0.5s"), (2.0, "rto=2s"), ("x", "rto=?")])
def test_the_line_names_the_stun_retry_rto_the_bot_runs_with(watch, out, monkeypatch, rto, word):
    monkeypatch.setattr(stun, "RETRY_RTO", rto)
    h, a = Proto(Cand("host", LOCAL_IP)), Cand("host", REMOTE_IP)
    p = Pair(h, a)
    conn = Conn([p], [a])
    feed(watch, state(conn, p, S.WAITING, 0.0), rec(ice_checks.FAILED, conn, at=1.0))
    assert out[0].split()[3] == word, out


# ---- never an address, a port, a ufrag or a password

def test_no_address_port_ufrag_or_password_reaches_any_line(watch, out):
    conn = stall(watch, local_ip=LOCAL_IP, remote_ip=V6, relay_ip=RELAY_IP)
    # a candidate whose TYPE itself is address-like is written as `?`
    h, bad = Proto(Cand("10.1.2.3", "10.1.2.3")), Cand("fe80::1", "fe80::1")
    p = Pair(h, bad)
    c2 = Conn([p], [bad])
    feed(watch, state(c2, p, S.WAITING, 0.0), state(c2, p, S.IN_PROGRESS, 0.1),
         request(c2, h, (bad.host, bad.port), 0.2, use=True),
         rec(ice_checks.FAILED, c2, at=0.3))
    text = "\n".join(out)
    assert conn and len(out) == 5, out
    assert "ice checks ?/?: pairs=1" in text
    for word in (LOCAL_IP, REMOTE_IP, RELAY_IP, V6, "fe80", "10.1.2.3", "192.0.2",
                 str(PORT), str(PORT + 1), str(PORT + 2), UFRAG, RFRAG, PWD, "Candidate",
                 "Connection", "protocol("):
        assert word not in text, (word, text)
    assert not IPV4.search(text) and not IPV6.search(text), text


def test_a_remote_candidate_type_that_is_not_one_of_the_four_is_written_as_a_question_mark(watch, out):
    """A remote candidate's type is the caller's SDP token: a word there that
    is not host, srflx, prflx or relay could be a name, so it is never written."""
    h = Proto(Cand("host", LOCAL_IP))
    rs = [Cand(word, REMOTE_IP, PORT + i) for i, word in enumerate(("mike", "van", "Host", "relayed"))]
    ps = [Pair(h, r) for r in rs]
    conn = Conn(ps, rs)
    feed(watch, *(state(conn, p, S.WAITING, 0.0) for p in ps), rec(ice_checks.FAILED, conn, at=1.0))
    assert out[1:] == ["ice checks host/?: pairs=4 succeeded=0 failed=0 open=4"], out
    for word in ("mike", "van", "Host", "relayed"):
        assert word not in "\n".join(out), word


# ---- a log line can never break a call, and aioice prints nothing new

class Boom:
    def __getattr__(self, name):
        raise RuntimeError(name)


@pytest.mark.parametrize("record", [
    rec(ice_checks.CHECK_STATE, Boom(), Boom(), Boom(), Boom()),
    rec(ice_checks.CHECK_STATE, object()),
    rec(ice_checks.INBOUND, Conn([], []), Boom(), ("x", 1), Boom()),
    rec(ice_checks.INBOUND, Boom(), Boom(), Boom(), stun.Message(
        message_method=stun.Method.BINDING, message_class=stun.Class.REQUEST)),
    rec(ice_checks.COMPLETED, Boom()),
    rec(ice_checks.CONNECTION_LOST, Boom(), Boom(), None),
    rec("%s %s", 1, 2),
    rec(None),
])
def test_an_unreadable_record_is_passed_on_and_never_raises(watch, out, record):
    assert watch.filter(record) is before(record)
    assert out == []


def test_a_sink_or_a_connection_that_raises_never_breaks_the_check(out):
    lg = logging.getLogger("test.ice_checks")

    def sink(line):
        raise OSError("disk full")
    w = ice_checks.ChecksWatch(lg, logging.NOTSET, sink=sink)
    h, a = Proto(Cand("host", LOCAL_IP)), Cand("host", REMOTE_IP)
    p = Pair(h, a)
    conn = Conn([p], [a])
    feed(w, state(conn, p, S.WAITING, 0.0))
    conn._check_list = Boom()                    # unreadable at the end, too
    r = rec(ice_checks.COMPLETED, conn, at=1.0)
    assert w.filter(r) is before(r)


@pytest.mark.parametrize("level,parent,passed", [
    (logging.NOTSET, logging.WARNING, {logging.WARNING, logging.ERROR}),
    (logging.INFO, logging.WARNING, {logging.INFO, logging.WARNING, logging.ERROR}),
    (logging.NOTSET, logging.DEBUG, {logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR}),
])
def test_a_record_is_passed_on_only_if_the_logger_passed_it_before(level, parent, passed):
    up = logging.getLogger("test.ice_checks_parent")
    up.setLevel(parent)
    lg = logging.getLogger("test.ice_checks_parent.ice")
    w = ice_checks.ChecksWatch(lg, level, sink=lambda line: None)
    got = {lv for lv in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR)
           if w.filter(rec("%s x", object(), level=lv))}
    assert got == passed


def test_calls_installs_the_watch_once_and_writes_through_its_log(monkeypatch):
    lg = logging.getLogger("aioice.ice")
    watches = [f for f in lg.filters if isinstance(f, ice_checks.ChecksWatch)]
    assert len(watches) == 1 and lg.isEnabledFor(logging.DEBUG)
    ice_checks.watch_checks()
    assert [f for f in lg.filters if isinstance(f, ice_checks.ChecksWatch)] == watches
    seen = []
    monkeypatch.setattr(calls, "log", seen.append)
    ice_checks._calls_log("ice checks: x")
    assert seen == ["ice checks: x"]


def test_the_consent_watch_still_sees_its_record(monkeypatch):
    """PHONE-316's consent watch reads an INFO record that the root does not
    print. Filters stop at the first no: this watch goes first, after the
    consent watch was installed, and lets that record through to it."""
    lg = logging.getLogger("aioice.ice")
    kinds = [type(f).__name__ for f in lg.filters]
    assert kinds[:2] == ["ChecksWatch", "_ConsentWatch"], kinds
    conn = Conn([], [])
    lg.info("%s Consent to send expired", conn)
    assert conn in calls._consent_lost


def test_a_debug_record_never_reaches_the_consent_watch_or_a_handler(monkeypatch):
    lg = logging.getLogger("aioice.ice")
    (watch,) = [f for f in lg.filters if isinstance(f, ice_checks.ChecksWatch)]
    assert watch.filter(rec("%s x", object(), level=logging.DEBUG)) is False
    assert watch.filter(rec("%s x", object(), level=logging.INFO)) is (
        logging.INFO >= (watch.level or lg.parent.getEffectiveLevel()))


# ---- the pinned aioice, for real: two Connections over loopback

def test_two_real_aioice_connections_write_their_lines(monkeypatch):
    assert aioice.__version__ == "0.10.2"       # a pin bump re-checks every record read here
    monkeypatch.setattr(aioice.ice, "get_host_addresses", lambda use_ipv4, use_ipv6: ["127.0.0.1"])
    lg = logging.getLogger("aioice.ice")
    (watch,) = [f for f in lg.filters if isinstance(f, ice_checks.ChecksWatch)]
    out, reached = [], []
    monkeypatch.setattr(watch, "sink", out.append)

    class H(logging.Handler):
        def emit(self, record):
            if record.name == "aioice.ice":
                reached.append(record.levelno)
    root = logging.getLogger()
    h = H(level=logging.NOTSET)
    root.addHandler(h)

    async def call():
        a = aioice.Connection(ice_controlling=True, use_ipv6=False)
        b = aioice.Connection(ice_controlling=False, use_ipv6=False)
        await asyncio.gather(a.gather_candidates(), b.gather_candidates())
        for x, y in ((a, b), (b, a)):
            x.remote_username, x.remote_password = y.local_username, y.local_password
            for c in y.local_candidates:
                await x.add_remote_candidate(c)
            await x.add_remote_candidate(None)
        try:
            await asyncio.wait_for(asyncio.gather(a.connect(), b.connect()), 15)
        finally:
            await a.close()
            await b.close()
    try:
        asyncio.run(call())
    finally:
        root.removeHandler(h)

    summary = re.compile(
        r"ice checks: end=completed@\d+\.\ds rto=\S+ pairs=1 peer-checks=\d+ "
        r"use-candidate=(none|-?\d+\.\ds:host/host:[a-z-]+:started@\S+:succeeded@\S+) "
        r"in-progress-hits=\d+ first-hit=(none|-?\d+\.\ds:host/host:started@\S+)\Z")
    tops = [line for line in out if line.startswith("ice checks: ")]
    assert len(tops) == 2 and all(summary.match(t) for t in tops), out
    assert sorted(summary.match(t).group(1) == "none" for t in tops) == [False, True], out
    assert sum(bool(re.fullmatch(r"ice checks host/host: pairs=1 succeeded=1@\d+\.\ds "
                                 r"failed=0 open=0", line)) for line in out) == 2, out
    assert "127.0.0.1" not in "\n".join(out)
    floor = root.getEffectiveLevel()
    assert all(lv >= floor for lv in reached), (floor, reached)   # aioice prints nothing new

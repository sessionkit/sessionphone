"""Per-call ICE check timings, by pair TYPE and seconds: never an address.

An incoming Delta call can sit in ICE `checking` for tens of seconds (PHONE-317:
call 158170, 42 s, host/host, after 0.6 s of gathering). The suspected cause is
not measured: the caller's check reaches a pair whose own check is still
IN_PROGRESS, aioice 0.10.2 does not restart that check (ice.py check_incoming),
and the pair waits for a late retry of a STUN transaction that runs to 63.5 s.
These lines measure it (PHONE-329, step (d)). When a call's checking ends,
`bot.log` gets one summary line and one line per pair type:

    CALL ice checks: end=completed@41.9s rto=2s pairs=3 peer-checks=3 use-candidate=2.1s:host/host:in-progress:started@0.3s:succeeded@41.8s in-progress-hits=2 first-hit=2.1s:host/host:started@0.3s
    CALL ice checks host/host: pairs=2 succeeded=1@41.8s failed=0 open=1
    CALL ice checks host/relay: pairs=1 succeeded=0 failed=1@20.0s open=0

Every time is in seconds from checking-start (`-` for one that never came): the first change of state of
any of the call's pairs (aioice's connect() unfreezing the first pairs).

- `end=` how checking ended: `completed` or `failed` (aioice's own verdict),
  or `closed` (the connection was closed before either).
- `rto=` aioice's STUN retry RTO as the bot set it (calls.py, DC_STUN_RTO):
  a check is sent at 0, 1, 3, 7, 15, 31 and 63 RTOs and fails at 127 RTOs
  (2 s: 0, 2, 6, 14, 30, 62, 126 s; fails at 254 s).
- `pairs=` the pairs in aioice's check list.
- `peer-checks=` how many of the caller's connectivity checks arrived.
- `use-candidate=` when the caller's first USE-CANDIDATE arrived, on which
  pair type, and what state that pair was in at that moment (`in-progress`,
  `succeeded`, `waiting`, `frozen`, `failed`, or `new` for a pair not yet in
  the list), then when that pair's own check started and when it
  succeeded (`-`: never). `none` if none came: the bot was the controlling
  side.
- `in-progress-hits=` how many peer checks reached a pair whose own check
  was still IN_PROGRESS; `first-hit=` the first of them, its pair type, and
  when that pair's own check had started (`none` if there was no hit).
- per pair type: how many pairs, how many ended SUCCEEDED or FAILED and the
  first time one did, and how many were still open (frozen, waiting, in
  progress) when checking ended.

How: a logging.Filter on aioice's `aioice.ice` logger, the PHONE-316
consent-watch pattern (calls.py, `_ConsentWatch`). It reads these records of
aioice's, each with the Connection as its first argument, keeping a little
state per Connection in a WeakKeyDictionary:

    "%s Check %s %s -> %s"          check_state: (conn, pair, old, new), INFO
    "%s %s < %s %s"                 StunProtocol.datagram_received:
                                    (conn, protocol, addr, message), DEBUG
    "%s ICE completed" / "%s ICE failed"                              INFO
    "%s %s connection_lost(%s)"     StunProtocol.connection_lost, DEBUG

A record's args are never formatted: every aioice repr carries an address.
Only candidate TYPES, state names, counts and seconds are written. A peer
check is matched to its pair by comparing addresses in memory, and a request
is counted only if its USERNAME is this connection's: both compare, neither
is ever written. (aioice also checks the request's MESSAGE-INTEGRITY; this
does not, so a forged request with the right USERNAME would be counted.)

Read here only because pyproject pins aioice==0.10.2: the record texts, the
order check_incoming runs in after the inbound record, and the private
`_check_list`, `ice_controlling`, `local_username`, `remote_username` and
`_remote_candidates`. A pin bump must re-check them;
tests/bridge/test_ice_checks.py drives a real aioice Connection pair to
prove the records against the pin.

LOGGING ONLY: no aioice patch and no change to what aioice does, when it
checks or how long it waits. The filter raises the logger to DEBUG so the
inbound record is made at all. It drops only the records the logger would
not have made before (below its level just before the raise) and passes the
rest on unchanged, so no new line is printed by aioice anywhere. Every read
is guarded: a log line can never break a call.

ORDER MATTERS. A logger's filters run in order and stop at the first that
says no. This one goes FIRST, so it sees every record, and it says no only to
records that did not exist before it. The consent watch (calls.py,
`_ConsentWatch`) must be installed BEFORE it: that one reads the logger's
level when installed and gates against it, so it has to read the level from
before the raise to DEBUG. calls.py therefore imports this module on the line
after `watch_consent()` (one line), and this module installs itself on
import. The lines go out through calls.log, looked up when a line is written,
so they carry the call's tag like every other `CALL` line.
"""
import logging
import sys
import weakref

import aioice.stun

LOGGER = "aioice.ice"

# aioice 0.10.2's record texts (ice.py), matched exactly.
CHECK_STATE = "%s Check %s %s -> %s"
INBOUND = "%s %s < %s %s"
COMPLETED = "%s ICE completed"
FAILED = "%s ICE failed"
CONNECTION_LOST = "%s %s connection_lost(%s)"

# RFC 8445's four candidate types, and nothing else. A remote candidate's type
# is the caller's own SDP token (aioice Candidate.from_sdp, `typ <word>`), so
# any other word could be a name: it is written as `?`.
_RANK = {"host": 0, "srflx": 1, "prflx": 2, "relay": 3}


def _type(candidate):
    """A candidate's TYPE word, one of the four, or `?`: nothing else of a candidate."""
    t = getattr(candidate, "type", None)
    return t if isinstance(t, str) and t in _RANK else "?"


def _pair_type(pair):
    """`<local type>/<remote type>` of an aioice CandidatePair. May raise."""
    return f"{_type(pair.local_candidate)}/{_type(pair.remote_candidate)}"


_STATES = ("FROZEN", "WAITING", "IN_PROGRESS", "SUCCEEDED", "FAILED")   # CandidatePair.State


def _state(state):
    """An aioice CandidatePair.State as a word: `IN_PROGRESS` -> `in-progress`."""
    name = getattr(state, "name", None)
    return name.lower().replace("_", "-") if name in _STATES else "?"


def _secs(t, t0):
    """Seconds from checking-start, or `-` for a time that never came."""
    return "-" if t is None or t0 is None else f"{t - t0:.1f}s"


def _rto():
    """aioice's STUN retry RTO as set (calls.py sets it from DC_STUN_RTO), or `?`."""
    try:
        return f"{float(aioice.stun.RETRY_RTO):g}s"
    except Exception:  # noqa: BLE001
        return "?"


def _type_key(label):
    local, _, remote = label.partition("/")
    return (_RANK.get(local, 9), _RANK.get(remote, 9), label)


class _Pair:
    """What is kept of one candidate pair: its types and its state times."""

    def __init__(self, label):
        self.label = label
        self.state = None           # its last state word
        self.started = None         # first IN_PROGRESS
        self.succeeded = None       # first SUCCEEDED
        self.failed = None          # first FAILED


class _Checks:
    """What is kept of one aioice Connection's checks, until its lines are written."""

    def __init__(self):
        self.t0 = None              # checking-start
        self.pairs = {}             # id(pair) -> _Pair
        self.peer_checks = 0
        self.hits = 0
        self.first_hit = None       # (time, pair type, that pair's check start)
        self.use = None             # (time, pair type, that pair's state, its _Pair or None)
        self.done = False

    def pair(self, pair):
        p = self.pairs.get(id(pair))
        if p is None:
            p = self.pairs[id(pair)] = _Pair(_pair_type(pair))
        return p


def _find_pair(conn, protocol, addr):
    """The pair in conn's check list for (protocol, remote addr), or None.
    The addresses are compared here and nowhere written."""
    for pair in list(conn._check_list):
        if pair.protocol is protocol and tuple(pair.remote_addr) == tuple(addr):
            return pair
    return None


def _remote_type(conn, addr):
    """The TYPE of the remote candidate at addr, `prflx` if aioice has none yet
    (it will learn a peer-reflexive one from this very check)."""
    for c in list(conn._remote_candidates):
        if (c.host, c.port) == tuple(addr):
            return _type(c)
    return "prflx"


def _calls_log(line):
    """calls.log, if calls is loaded: so the line carries the call's tag."""
    calls = sys.modules.get("sessionphone.bridge.calls")
    if calls is not None:
        calls.log(line)


class ChecksWatch(logging.Filter):
    """On aioice's ice logger: notes each Connection's checks, writes its lines
    when its checking ends, then passes a record on only if the logger would
    have made it before (its level just before watch_checks raised it). The
    filters after this one (the consent watch) see the rest, and gate them."""

    def __init__(self, logger, level, sink=None):
        super().__init__()
        self.logger, self.level = logger, level   # level: the logger's own, before the raise
        self.sink = sink or _calls_log
        self.calls = weakref.WeakKeyDictionary()  # aioice Connection -> _Checks

    def filter(self, record):
        try:
            self._note(record)
        except Exception:  # noqa: BLE001 - a log line must never break a call
            pass
        floor = self.level or self.logger.parent.getEffectiveLevel()
        return record.levelno >= floor

    # ---- reading aioice's records

    def _note(self, record):
        msg, args = record.msg, record.args
        if not isinstance(msg, str) or not isinstance(args, tuple) or not args:
            return
        if msg == CHECK_STATE and len(args) == 4:
            self._check_state(args[0], args[1], args[3], record.created)
        elif msg == INBOUND and len(args) == 4:
            self._inbound(*args, t=record.created)
        elif msg in (COMPLETED, FAILED):
            self._end(args[0], "completed" if msg == COMPLETED else "failed", record.created)
        elif msg == CONNECTION_LOST:
            self._end(args[0], "closed", record.created)

    def _checks(self, conn, create=True):
        c = self.calls.get(conn)
        if c is None and create:
            c = self.calls[conn] = _Checks()
        return c

    def _check_state(self, conn, pair, new, t):
        c = self._checks(conn)
        if c.done:
            return
        if c.t0 is None:
            c.t0 = t
        p = c.pair(pair)
        p.state = _state(new)
        if p.state == "in-progress" and p.started is None:
            p.started = t
        elif p.state == "succeeded" and p.succeeded is None:
            p.succeeded = t
        elif p.state == "failed" and p.failed is None:
            p.failed = t

    def _inbound(self, conn, protocol, addr, message, t):
        if (message.message_class != aioice.stun.Class.REQUEST
                or message.message_method != aioice.stun.Method.BINDING):
            return
        c = self._checks(conn)
        if c.done:
            return                  # its lines are written: consent checks from here on
        if conn.remote_username is not None and message.attributes.get("USERNAME") != (
                f"{conn.local_username}:{conn.remote_username}"):
            return                  # aioice answers this with 400: not the caller's check
        c.peer_checks += 1
        pair = _find_pair(conn, protocol, addr)
        if pair is None:
            p, label, state, started = None, (f"{_type(protocol.local_candidate)}/"
                                              f"{_remote_type(conn, addr)}"), "new", None
        else:
            p = c.pair(pair)
            label, state, started = p.label, _state(pair.state), p.started
        if state == "in-progress":
            c.hits += 1
            if c.first_hit is None:
                c.first_hit = (t, label, started)
        if (c.use is None and "USE-CANDIDATE" in message.attributes
                and not conn.ice_controlling):
            c.use = (t, label, state, p)

    def _end(self, conn, how, t):
        c = self._checks(conn, create=False)
        if c is None or c.done or c.t0 is None:
            return
        c.done = True
        try:                        # pairs that never changed state are in the list too
            for pair in list(conn._check_list):
                p = c.pair(pair)
                p.state = p.state or _state(pair.state)
        except Exception:  # noqa: BLE001 - count what was seen
            pass
        for line in lines(c, how, t):
            self.sink(line)


def lines(c, how, t):
    """The summary line, then one line per pair type."""
    t0 = c.t0
    if c.use is None:
        use = "none"
    else:
        at, label, state, p = c.use
        use = (f"{_secs(at, t0)}:{label}:{state}"
               f":started@{_secs(p and p.started, t0)}:succeeded@{_secs(p and p.succeeded, t0)}")
    if c.first_hit is None:
        hit = "none"
    else:
        hit = f"{_secs(c.first_hit[0], t0)}:{c.first_hit[1]}:started@{_secs(c.first_hit[2], t0)}"
    out = [f"ice checks: end={how}@{_secs(t, t0)} rto={_rto()} pairs={len(c.pairs)} "
           f"peer-checks={c.peer_checks} use-candidate={use} "
           f"in-progress-hits={c.hits} first-hit={hit}"]
    by_type = {}
    for p in c.pairs.values():
        by_type.setdefault(p.label, []).append(p)
    for label in sorted(by_type, key=_type_key):
        ps = by_type[label]
        ok = [p for p in ps if p.state == "succeeded"]
        bad = [p for p in ps if p.state == "failed"]
        out.append(f"ice checks {label}: pairs={len(ps)} "
                   f"succeeded={_count_at(ok, 'succeeded', t0)} "
                   f"failed={_count_at(bad, 'failed', t0)} "
                   f"open={len(ps) - len(ok) - len(bad)}")
    return out


def _count_at(ps, attr, t0):
    """`<n>@<first time>`, or `0`."""
    times = [getattr(p, attr) for p in ps if getattr(p, attr) is not None]
    if not ps:
        return "0"
    return f"{len(ps)}@{_secs(min(times), t0)}" if times else str(len(ps))


def watch_checks(sink=None):
    """Install the check watch on aioice's ice logger, once, FIRST among its
    filters: so it sees every record, and no record the others need is lost."""
    lg = logging.getLogger(LOGGER)
    if any(isinstance(f, ChecksWatch) for f in lg.filters):
        return
    lg.filters.insert(0, ChecksWatch(lg, lg.level, sink))
    if lg.getEffectiveLevel() > logging.DEBUG:
        lg.setLevel(logging.DEBUG)          # so the inbound record is made at all


watch_checks()

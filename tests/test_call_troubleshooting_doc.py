"""PHONE-114 R5 (SC4): README's "Call troubleshooting" quotes only log lines
the code actually writes.

The section is what a reader greps `bot.log` with, and other docs link it as
README.md#call-troubleshooting. So:

- the heading exists (renaming it breaks this test, not the link silently);
- every log line it quotes is a template that matches at least one line
  emitted by DRIVING the code, here, with the rigs of the do-001/002/003/005
  acceptance tests (tests/bridge/, loaded by path). Nothing greps the source.

A quoted log line is every line of a ```text block in the section, and every
inline `CALL ...` span. In a template, `<a|b|c>` means exactly one of those
words, and EACH of them must be emitted somewhere. `<name>` stands for one
token, without spaces (`\\S+`). `...` stands for anything. Templates are
matched against whole lines.

No call, no network beyond loopback, no mail, no TTS.
"""
import asyncio
import importlib.util
import pathlib
import re
import sys
import types

import pytest

from sessionphone.bridge import bot, calls, dc_env, earleg, heard_sink, mouthleg

HERE = pathlib.Path(__file__).resolve().parent
README = HERE.parent / "README.md"
HEADING = "## Call troubleshooting"
BOX = "agent@example.org"


# ---- the section and its templates ----

def section() -> str:
    text = README.read_text()
    assert f"\n{HEADING}\n" in text, f"README.md has no {HEADING!r}"
    body = text.split(f"\n{HEADING}\n", 1)[1]
    return re.split(r"^## ", body, maxsplit=1, flags=re.MULTILINE)[0]


def quoted_lines(sec: str) -> list[str]:
    out, fence = [], None
    for line in sec.splitlines():
        if line.startswith("```"):
            fence = None if fence is not None else line[3:].strip()
            continue
        if fence == "text" and line.strip():
            out.append(line.strip())
    prose = re.sub(r"^```.*?^```", "", sec, flags=re.MULTILINE | re.DOTALL)
    out += [m for m in re.findall(r"`([^`]+)`", prose.replace("\n", " "))
            if m.startswith("CALL ")]
    return list(dict.fromkeys(out))


_TOKEN = re.compile(r"<([^<>]+)>|\.\.\.")


def _parts(template: str):
    """[("lit", s) | ("one",) | ("any",) | ("alt", [words])]"""
    parts, at = [], 0
    for m in _TOKEN.finditer(template):
        parts.append(("lit", template[at:m.start()]))
        if m.group(0) == "...":
            parts.append(("any",))
        elif "|" in m.group(1):
            parts.append(("alt", m.group(1).split("|")))
        else:
            parts.append(("one",))
        at = m.end()
    parts.append(("lit", template[at:]))
    return parts


def _regex(parts, fixed=None):
    """fixed: {alt-group index: the one word it must be}"""
    out, g = [], 0
    for p in parts:
        if p[0] == "lit":
            out.append(re.escape(p[1]))
        elif p[0] == "one":
            out.append(r"\S+")
        elif p[0] == "any":
            out.append(".*")
        else:
            words = [fixed[g]] if fixed and g in fixed else p[1]
            out.append("(?:" + "|".join(map(re.escape, words)) + ")")
            g += 1
    return re.compile("".join(out))


def variants(template: str):
    """The template as is, then once per word of each `<a|b>` group."""
    parts = _parts(template)
    yield template, _regex(parts)
    alts = [p[1] for p in parts if p[0] == "alt"]
    for g, words in enumerate(alts):
        for w in words:
            yield f"{template}  [with {w!r}]", _regex(parts, {g: w})


TEMPLATES = quoted_lines(section())


# ---- the rigs: the acceptance tests' own, loaded by path ----

_RIGS = {}


def rig(name):
    if name not in _RIGS:
        path = HERE / "bridge" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_doc_rig_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _RIGS[name] = mod
    return _RIGS[name]


def _drive_turns(mp, out):
    """do-001: turns through the REAL bot.mail_in, with `message send` faked."""
    t = rig("test_call_turn_log")
    rc = types.SimpleNamespace(code=0, out="")
    mp.setattr(bot, "log", out.append)
    mp.setattr(bot.subprocess, "run",
               lambda *a, **k: types.SimpleNamespace(returncode=rc.code, stdout=rc.out, stderr=""))
    mp.setattr(calls, "_hooks", calls.Hooks(out.append, None, bot.mail_in))

    s = t._session()
    t.turn(s, "can you check the build")
    for via in ("mouth", "dc-say", "mail"):
        t.turn(s, f"and again by {via}")
        s.replied(via)
    t.turn(_session_with_guests(t), "the guests hear this")

    mp.setattr(calls, "_call_id", 5531)                 # a live call: every line is tagged
    t.turn(t._session(), "tagged")
    mp.setattr(calls, "_call_id", None)

    rc.code = 3                                         # the MTA refuses (message send --json)
    rc.out = '{"sent": "no", "kind": "mta", "message": "no MTA at /usr/sbin/sendmail", "notices": []}'
    t.turn(t._session(), "refused")
    live = t._session()                                 # on a live call the caller is told (PHONE-249 D5)
    live.pc, live.closed = object(), None

    async def _said(*a, **k):
        return {"n": 1, "failed": 0, "error": None, "cut": False}
    live.speak = _said
    t.turn(live, "refused on a live call")
    rc.code, rc.out = 0, ""

    def boom(*a, **k):
        raise OSError("no message here")
    mp.setattr(calls, "_hooks", calls.Hooks(out.append, None, boom))
    with pytest.raises(OSError):
        t.turn(t._session(), "raises")
    mp.setattr(calls, "_hooks", calls.Hooks(out.append, None, bot.mail_in))

    s = t._session()                                    # hang-up with a turn waiting
    t.turn(s, "bye")
    t._closable(s, mp)
    asyncio.run(s.close("hung up"))


def _session_with_guests(t):
    return t._session(guests=["guest1@example.org", "guest2@example.org"])


def _drive_noreply(mp, out):
    """do-002: the watchdog, its window at 0.05 s; its start line."""
    n = rig("test_noreply")
    mp.setattr(calls, "_hooks", calls.Hooks(out.append, None, lambda *a, **k: True))
    mp.setattr(calls, "NO_REPLY_S", n.W)
    r = types.SimpleNamespace(logs=out, spoken=[], done=[], send_gate={})

    async def quiet():                                  # nobody answers
        s = n._session(r)
        await s._mail_turn("one")
        await s._mail_turn("two")                       # turn 1 folds into turn 2's wait
        await asyncio.sleep(n.SETTLE)

    async def talking():                                # he is already talking again
        s = n._session(r)
        await s._mail_turn("three")
        s.speaking = True
        await asyncio.sleep(n.SETTLE)
    asyncio.run(quiet())
    asyncio.run(talking())
    assert r.spoken == [(calls.HOLDING_LINE, "bridge")]

    mp.setattr(calls, "MailLeg", lambda m: types.SimpleNamespace(start=lambda: None))
    mp.setattr(earleg, "start", lambda *a: None)
    mp.setattr(mouthleg, "start", lambda *a: None)
    for window in (8.0, 0):
        mp.setattr(calls, "NO_REPLY_S", window)
        mgr = object.__new__(calls.CallManager)
        mgr.thread = types.SimpleNamespace(start=lambda: None)
        mgr.start()


def _drive_first_wait(mp, out):
    """PHONE-211: the first call's wait, the line then the music, ended by the probe."""
    import numpy as np

    from sessionphone import hearing
    from sessionphone.bridge import firstcall
    mp.setattr(calls, "_hooks", calls.Hooks(out.append, None, None))
    mp.setattr(firstcall, "POLL_S", 0.001)
    answers = iter([False, True])
    pcm = np.full(calls.OUT_SAMPLES, 1, dtype=np.int16)
    asyncio.run(firstcall.wait(
        calls.PipVoiceTrack(),
        weights=lambda: hearing.Weights("downloading", 10**9, 3 * 10**9, 2_864_263_300),
        probe=lambda: next(answers), render=lambda text: pcm, load_music=lambda: pcm,
        log=calls.log))


def _drive_ice(mp, out):
    """do-003: ICE states and the selected pair, every candidate type."""
    i = rig("test_ice_log")
    mp.setattr(calls, "_hooks", calls.Hooks(out.append, None, None))
    failed = calls.watch_ice(i.FakePC(i._Ice(
        local=[i._cand("host"), i._cand("relay")],
        remote=[i._cand("host", i.REMOTE_IP), i._cand("srflx", i.REMOTE_IP)])))
    for g in ("gathering", "complete"):
        failed.gathering(g)
    for st in ("checking", "failed"):
        failed.ice(st)
    for local, remote in (("host", "host"), ("srflx", "relay"), ("prflx", "prflx"), ("relay", "srflx")):
        pc = calls.watch_ice(i.FakePC(i._Ice(nominated={1: i._pair(local, remote)})))
        pc.ice("completed")
        pc.ice("closed")
    broken = i._Ice()
    del broken._connection._nominated
    calls.watch_ice(i.FakePC(broken)).ice("completed")


def _drive_redact(mp, out):
    """do-005: the ice servers line, trickled candidates, connection state."""
    x = rig("test_ice_redact")
    mp.setattr(calls, "_hooks", calls.Hooks(out.append, None, None))
    mp.setattr(calls, "RTCPeerConnection", x.FakePC)
    s = object.__new__(calls.CallSession)
    s.account = types.SimpleNamespace(ice_servers=lambda: [x.TURN, {"urls": "stun:relay.example.org"}])
    s.voice, s.tasks = None, []

    async def build():
        pc = await s._build_pc()
        pc.connectionState = "checking"
        pc.emit("connectionstatechange")     # its handler is a coroutine: let it run
        await asyncio.sleep(0.01)
    asyncio.run(build())

    for typ in ("host", "srflx", "prflx", "relay"):
        x._trickle(x._session(x._TricklePC()),
                   f"candidate:1 1 udp 1 {x.IP} {x.PORT} typ {typ}")
    x._trickle(x._session(x._TricklePC()), f"candidate:1 1 udp 1 {x.IP} 5432x1 typ host")


def _drive_heard(mp, out):
    """The heard log's one bot.log line: a chat that is not listed."""
    mp.setenv("DC_HEARD", "1")
    mp.setenv("DC_HEARD_CHATS", "99")
    heard_sink._reset_for_tests()
    heard_sink.CallSink(12, calls.log, call="5531").turn("not listed")
    heard_sink._reset_for_tests()


def _drive_ring(mp, out):
    """PHONE-240: rings through the REAL handle_events: its CALL EVENT line,
    video off and on, then a ring ignored for want of the media pack; then
    the answered call's session to its first line, CALL INCOMING, and no
    further (no ring wait, no media)."""
    class Stop(Exception):
        pass
    rings = [types.SimpleNamespace(kind=bot.EventType.INCOMING_CALL, msg_id=77, chat_id=12,
                                   has_video=v) for v in (False, True)]

    def wait_for_event():
        if not rings:
            raise Stop
        return rings.pop(0)
    mp.setattr(bot, "log", out.append)
    mp.setattr(bot, "calls_refusal", lambda starts=True: None)
    mp.setattr(bot, "calls", types.SimpleNamespace(incoming=lambda account, event: None))
    with pytest.raises(Stop):
        bot.handle_events(types.SimpleNamespace(wait_for_event=wait_for_event))
    rings.append(types.SimpleNamespace(kind=bot.EventType.INCOMING_CALL, msg_id=78, chat_id=12,
                                       has_video=False))
    mp.setattr(bot, "calls", None)                      # heard, and ignored: no media pack
    with pytest.raises(Stop):
        bot.handle_events(types.SimpleNamespace(wait_for_event=wait_for_event))

    mp.setattr(calls, "_hooks", calls.Hooks(out.append, None, None))
    mp.setattr(calls, "RING", True)
    s = object.__new__(calls.CallSession)
    s.msg_id, s.chat_id, s.chat_name, s.caller, s.has_video = 77, 12, "Pat", "Pat", False

    async def named():
        pass

    async def not_picked_up(loop):
        return False
    s._names, s._ring = named, not_picked_up
    asyncio.run(s.run())


@pytest.fixture(scope="module")
def emitted(_message_tripwire_script, tmp_path_factory):
    """Every line the drives log. A module fixture runs BEFORE the autouse,
    function-scoped message_tripwire, so it pins the `message` lookup itself
    (PHONE-190): DC_MESSAGE is the suite's tripwire script, and seatmail's
    cached MESSAGE is reset. Unpinned, seatmail.resolve_message() asked the
    host PATH: with no sessionmail installed no turn was "mailed" and the
    turn lines were never emitted; with it installed, the REAL binary was
    resolved. Now the test means the same on any machine."""
    from sessionphone.bridge import seatmail, spool
    out = []
    trips = tmp_path_factory.mktemp("doc-message-calls") / "calls"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(spool, "DIR", tmp_path_factory.mktemp("doc-undelivered"))   # a refused turn is kept
        mp.setenv("DC_MESSAGE", str(_message_tripwire_script))
        mp.setenv("MESSAGE_TRIP_LOG", str(trips))
        mp.setattr(seatmail, "MESSAGE", None)
        mp.setattr(dc_env, "MAILTO", BOX)
        mp.setattr(calls, "_call_id", None)
        for drive in (_drive_turns, _drive_noreply, _drive_ice, _drive_redact, _drive_heard,
                      _drive_first_wait, _drive_ring):
            drive(mp, out)
    assert not trips.exists() or not trips.read_text(), f"a drive ran message:\n{trips.read_text()}"
    return out


# ---- the tests ----

def test_the_heading_exists_for_the_hubs_link():
    assert re.search(rf"^{re.escape(HEADING)}$", README.read_text(), re.MULTILINE)
    # GitHub's anchor for it is the one the hub links
    assert HEADING.removeprefix("## ").lower().replace(" ", "-") == "call-troubleshooting"


def test_the_section_quotes_the_lines_it_must():
    # R5: the R1-R4 and R6 lines; an empty parse would pass everything below
    for must in ("CALL TURN <n> mailed to <box>", "taken=replied", "taken=unknown",
                 "taken=no", "CALL NO-REPLY box=<box>", "CALL ice state", "CALL ice pair",
                 "CALL ice servers:", "CALL added trickled candidate type="):
        assert any(must in t for t in TEMPLATES), (must, TEMPLATES)


@pytest.mark.parametrize("template", TEMPLATES)
def test_every_quoted_line_is_one_the_code_emits(template, emitted):
    for what, rx in variants(template):
        assert any(rx.fullmatch(line) for line in emitted), \
            f"no emitted line matches {what!r}:\n" + "\n".join(emitted)


def test_the_template_reader_is_strict():
    # guard the matcher itself: a wrong word, an extra token, a missing tail
    def ok(t, line):
        return bool(_regex(_parts(t)).fullmatch(line))
    assert ok("CALL ice state <checking|failed>", "CALL ice state failed")
    assert not ok("CALL ice state <checking|failed>", "CALL ice state closed")
    assert not ok("CALL TURN <n> taken=no", "CALL TURN 1 2 taken=no")
    assert ok("CALL x: <a>, ...", "CALL x: b, anything at all")
    assert not ok("CALL connection <state>", "CALL connection failed [call 1]")
    [_, *each] = variants("CALL ice state <checking|failed>")
    assert [w for w, _ in each] == ["CALL ice state <checking|failed>  [with 'checking']",
                                    "CALL ice state <checking|failed>  [with 'failed']"]


def test_the_heard_fields_are_the_ones_the_sink_writes(monkeypatch):
    """`"source": "call"`, `"device": "delta:<agent>/<chat_id>"`, `"call": "<id>"`
    and `"kind": "turn"`, as quoted, against what CallSink hands heard.py.
    `call:<id>` is the mouth and ears' device name, never the heard log's."""
    sec = section()
    fields = dict(re.findall(r'`"(\w+)": "([^"]*)"`', sec.replace("\n", " ")))
    assert set(fields) == {"kind", "source", "device", "call"}, fields

    got = []
    fake = types.SimpleNamespace(turn=lambda *a, **kw: got.append(("turn", kw)))
    monkeypatch.setenv("DC_HEARD", "1")
    monkeypatch.setenv("DC_HEARD_CHATS", "12")
    monkeypatch.setattr(heard_sink, "_load", lambda log: fake)
    heard_sink.CallSink(12, lambda m: None, call="5531").turn("can you hear me")
    (kind, kw), = got
    assert _regex(_parts(fields["kind"])).fullmatch(kind)          # heard.turn() writes kind "turn"
    for k in ("source", "device", "call"):
        assert _regex(_parts(fields[k])).fullmatch(str(kw[k])), (k, fields[k], kw[k])
    assert not fields["device"].startswith("call:")

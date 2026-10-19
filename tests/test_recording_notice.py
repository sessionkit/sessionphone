"""A call says whether it is kept (PHONE-177; the contract's rules 10-11).

1. The chat line (`record_notice`, env EARS_RECORD_NOTICE): ONE line into the
   call's own chat at connect, on every call, worded by the call's switch:
   off, on for N days, on with record_days 0. Silent when the setting is off
   (env or file); an invalid value posts it (it fails to ON).
2. The spoken notice (`record_spoken_notice`, env EARS_RECORD_SPOKEN_NOTICE):
   "This call is being recorded and kept." said BEFORE the greeting, once,
   only on a call that is kept. Its own setting, independent of the chat line.
   A TTS failure is logged, never sent as chat text, and the greeting goes on.

The resolution of both settings against the shared fixture table is in
tests/test_recording_switch.py (test_the_contract_case).
"""
import asyncio
import json
import types

import numpy as np
import pytest
from test_recording_switch import rig, session  # noqa: F401  (rig is a fixture)

from sessionphone.bridge import calls, recording

REAL_POST = calls.post_to_chat
OFF_LINE = "This call is not being kept: audio is deleted when the call ends."
SPOKEN = "This call is being recorded and kept."


class FakeRecorder:
    NAMES = calls.Recorder.NAMES

    def __init__(self, d):
        self.dir = d

    def close(self):
        pass


class Heard:
    def __init__(self):
        self.chat_name = None

    def event(self, *a, **k):
        pass

    def saying(self, *a, **k):
        pass

    def said(self, *a, **k):
        pass


@pytest.fixture
def call(rig, tmp_path, monkeypatch):  # noqa: F811
    """A call ready to connect, its switch over tmp ears.json and `env`.

    -> ns with .connect(env=..., conf=...) that runs _on_connected and returns
    (posts into the chat, lines spoken in order, the CALL log)."""
    conf = tmp_path / "ears.json"
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("DC_OUTBOX", str(outbox))
    monkeypatch.setattr(calls, "Recorder", FakeRecorder)
    posts, spoken = [], []
    monkeypatch.setattr(calls, "post_to_chat",
                        lambda account, chat_id, text, what: posts.append((chat_id, text, what)) or True)
    ns = types.SimpleNamespace(posts=posts, spoken=spoken, conf=conf, outbox=outbox, tts_fail=set())

    def tts(text, voice=None):
        if text in ns.tts_fail:
            raise RuntimeError("tts server down")
        spoken.append(text)
        return np.zeros(4800, dtype=np.int16)
    monkeypatch.setattr(calls, "tts_pcm48", tts)

    def connect(env=None, file=None, outgoing=False, voicemail=False, twice=False):
        if file is not None:
            conf.write_text(json.dumps(file))
        monkeypatch.setattr(calls, "_switch", recording.Switch(None, env=env or {}, path=str(conf),
                                                               log=rig.logs.append))
        s = session(rig)
        s.account = object()
        s.outgoing, s.voicemail = outgoing, voicemail
        s.greeting, s.greet_voice = None, None
        s.chat_name, s.root_id, s.caller = "carol", "root@x", "carol"
        s.heard = Heard()
        s._live_write = lambda: None
        s.guest_list = list
        s.lines = []

        async def go():
            s.manager = types.SimpleNamespace(forget=lambda x: None, sessions={},
                                              loop=asyncio.get_running_loop())
            await s._on_connected()
            if twice:
                s.connected = False
                await s._on_connected()
            await asyncio.sleep(0.05)               # the chat line is posted off the loop
            await asyncio.gather(*list(calls._background), return_exceptions=True)
        asyncio.run(go())
        calls.set_call_id(None)
        return s
    ns.connect = connect
    ns.greeting = list(calls.sentences(calls.GREETING))   # spoken a sentence at a time
    return ns


def texts(call):
    return [t for _cid, t, _w in call.posts]


# ---- 1. the chat line ----------------------------------------------------------------------


def test_off_by_default_the_chat_line_says_it_is_not_kept(call, rig):  # noqa: F811
    s = call.connect()
    assert call.posts == [(12, OFF_LINE, "recording notice")]
    assert SPOKEN not in call.spoken                  # never spoken when record is off
    assert call.spoken == call.greeting
    assert any(m.startswith("CALL recording notice: chat line on (default), spoken notice on (default)")
               for m in rig.logs)
    assert s.rec_noticed


@pytest.mark.parametrize("file, line", [
    ({"record": True}, "This call is being recorded and kept for 7 days."),
    ({"record": True, "record_days": 30}, "This call is being recorded and kept for 30 days."),
    ({"record": True, "record_days": 1}, "This call is being recorded and kept for 1 day."),
    ({"record": True, "record_days": 0}, "This call is being recorded and kept."),
    ({"record": "on"}, OFF_LINE),                     # an invalid record fails closed: not kept
])
def test_the_chat_line_is_worded_by_the_calls_own_switch(call, file, line):
    call.connect(file=file)
    assert texts(call) == [line]


def test_the_env_days_word_the_line(call):
    call.connect(env={"EARS_RECORD": "1", "EARS_RECORD_DAYS": "14"})
    assert texts(call) == ["This call is being recorded and kept for 14 days."]


def test_the_chat_line_goes_out_once_even_if_connect_runs_twice(call):
    call.connect(file={"record": True}, twice=True)
    assert len(call.posts) == 1
    assert call.spoken.count(SPOKEN) == 1


@pytest.mark.parametrize("env, file", [
    ({"EARS_RECORD_NOTICE": "0"}, None),
    ({"EARS_RECORD_NOTICE": " OFF "}, {"record_notice": True}),   # env beats file
    ({}, {"record_notice": False}),
])
def test_record_notice_off_silences_the_chat_line(call, rig, env, file):  # noqa: F811
    call.connect(env=env, file=file)
    assert call.posts == []
    assert any("CALL recording notice: chat line off (" in m for m in rig.logs)


@pytest.mark.parametrize("env, file, layer", [
    ({"EARS_RECORD_NOTICE": "nope"}, {"record_notice": False}, "EARS_RECORD_NOTICE='nope'"),
    ({}, {"record_notice": "off"}, '"record_notice": "off"'),
])
def test_an_invalid_record_notice_posts_the_line_and_names_the_layer(call, rig, env, file, layer):  # noqa: F811
    call.connect(env=env, file=file)
    assert texts(call) == [OFF_LINE]
    warned = [m for m in rig.logs if layer in m and " is not " in m]   # the warning, said once
    assert len(warned) == 1 and "fails to on" in warned[0]


@pytest.mark.parametrize("outgoing, voicemail", [(True, False), (False, True)])
def test_every_call_gets_the_line_rung_out_or_voicemail(call, outgoing, voicemail):
    call.connect(outgoing=outgoing, voicemail=voicemail)
    assert texts(call) == [OFF_LINE]


def test_the_line_follows_the_file_live_from_one_call_to_the_next(call):
    call.connect(file={"record": False})
    call.connect(file={"record": True, "record_days": 0, "listen": 1})
    assert texts(call) == [OFF_LINE, "This call is being recorded and kept."]


def test_a_failed_post_is_logged_and_the_greeting_goes_on(call, rig, monkeypatch):  # noqa: F811
    def boom(*a):
        raise RuntimeError("rpc gone")
    monkeypatch.setattr(calls, "post_to_chat", boom)
    call.connect()
    assert call.spoken == call.greeting
    assert any("recording notice: could not post to chat 12: rpc gone" in m for m in rig.logs)


def test_the_real_post_goes_through_the_channels_gate(call, rig, monkeypatch):  # noqa: F811
    sent = []

    class Chat:
        def send_text(self, t):
            sent.append(t)

    class Account:
        def get_chat_by_id(self, cid):
            return Chat()

    class Yes:
        reason = ""

        def __bool__(self):
            return True
    monkeypatch.setattr(calls.channels, "check", lambda account, chat_id, log=None: Yes())
    monkeypatch.setattr(calls, "post_to_chat", REAL_POST)   # the real one, over a fake account
    s = session(rig)
    s.account, s.chat_id = Account(), 12
    s.rec_state = types.SimpleNamespace(keep=False)
    monkeypatch.setattr(calls, "_switch", recording.Fixed(False))

    async def go():
        s._rec_notice_post()
        await asyncio.gather(*list(calls._background), return_exceptions=True)
    asyncio.run(go())
    assert sent == [OFF_LINE]



# ---- 2. the spoken notice ------------------------------------------------------------------


def test_on_the_call_speaks_the_notice_before_the_greeting(call):
    s = call.connect(file={"record": True})
    assert call.spoken == [SPOKEN, *call.greeting]
    assert [w for _t, _who, w in s.lines] == [SPOKEN, *call.greeting]   # the transcript has it too


@pytest.mark.parametrize("env, file", [
    ({"EARS_RECORD_SPOKEN_NOTICE": "0"}, {"record": True}),
    ({"EARS_RECORD": "on"}, {"record_spoken_notice": False}),
])
def test_record_spoken_notice_off_keeps_it_silent(call, env, file):
    call.connect(env=env, file=file)
    assert call.spoken == call.greeting
    assert len(call.posts) == 1                       # the chat line is independent


def test_the_chat_line_off_does_not_silence_the_spoken_notice(call):
    call.connect(file={"record": True, "record_notice": False})
    assert call.posts == []
    assert call.spoken == [SPOKEN, *call.greeting]


def test_an_invalid_spoken_setting_speaks_it(call, rig):  # noqa: F811
    call.connect(env={"EARS_RECORD_SPOKEN_NOTICE": "maybe"}, file={"record": True})
    assert call.spoken == [SPOKEN, *call.greeting]
    assert any("EARS_RECORD_SPOKEN_NOTICE='maybe'" in m and "fails to on" in m for m in rig.logs)


def test_off_never_speaks_it_even_with_the_setting_on(call):
    call.connect(env={"EARS_RECORD_SPOKEN_NOTICE": "1"}, file={"record": False})
    assert call.spoken == call.greeting


def test_a_tts_failure_is_logged_and_the_greeting_goes_on(call, rig):  # noqa: F811
    call.tts_fail.add(SPOKEN)
    call.connect(file={"record": True})
    assert call.spoken == call.greeting
    assert any("tts failed: tts server down" in m for m in rig.logs)
    assert not call.outbox.exists()                   # never re-sent as chat text
    assert texts(call) == ["This call is being recorded and kept for 7 days."]


# ---- the pure parts ------------------------------------------------------------------------


def test_the_notice_text():
    assert recording.notice_text(False, 7) == OFF_LINE
    assert recording.notice_text(False, 0) == OFF_LINE
    assert recording.notice_text(True, 0) == SPOKEN
    assert recording.notice_text(True, 2) == "This call is being recorded and kept for 2 days."
    assert recording.SPOKEN_NOTICE == SPOKEN


def test_the_notices_follow_the_file_live(tmp_path):
    conf = tmp_path / "ears.json"
    conf.write_text('{"record_notice": false}')
    sw = recording.Switch(None, env={}, path=str(conf), log=lambda m: None)
    assert sw.notice().as_dict() == {"on": False, "source": "file", "invalid": False}
    conf.write_text('{"record_notice": true, "record_spoken_notice": false}')
    assert sw.notice().on is True and sw.spoken_notice().as_dict() == {
        "on": False, "source": "file", "invalid": False}
    env = {"EARS_RECORD_NOTICE": "0"}
    sw = recording.Switch(None, env=env, path=str(conf), log=lambda m: None)
    env["EARS_RECORD_NOTICE"] = "1"                   # too late: taken at start
    assert sw.notice().on is False


def test_fixed_carries_the_notices():
    f = recording.Fixed(True, 0, notice=False, spoken=True)
    assert (f.notice().on, f.spoken_notice().on) == (False, True)

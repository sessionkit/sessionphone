"""A kept call is never silent about being kept (PHONE-238).

The combination: the call is KEPT (record on), the chat line is OFF
(record_notice false), the spoken notice is ON (record_spoken_notice, the
default), and the spoken notice fails. Two ways it can fail today:

1. TTS raises inside speak(): speak() catches it per sentence and, with
   fallback=False, REPORTS it in its outcome ({"failed": 1, ...}) without
   raising (the shape since PHONE-229). _rec_notice_speak ignores the outcome.
2. speak() itself raises (anything outside its per-sentence guard):
   _rec_notice_speak logs "the spoken notice failed" and goes on.

Either way the caller must still learn the call is kept, by chat text or by
speech, or the call must not be kept. The invariant asserted is exactly that:
kept => told.

The fix (design-001, option a): on a kept call where neither the spoken notice
nor the chat line reached the caller, the notice is posted ONCE as chat text.
The same rule holds with record_notice ON (the chat line refused, raising or
hung) and with the spoken notice OFF. A chat line refused by channels.txt is not
retried, and one not confirmed in time is not duplicated. Each case where a
notice did not get through logs ONE line. The call stays kept in every case.
"""
import json
import re
import threading

import pytest
from test_recording_notice import SPOKEN, call  # noqa: F401  (call is a fixture)
from test_recording_switch import rig  # noqa: F401  (rig is a fixture)

from sessionphone.bridge import calls

KEPT_CHAT_LINE_OFF = {"record": True, "record_notice": False}   # spoken notice on by default

KEPT_7 = "This call is being recorded and kept for 7 days."


def told(c):
    """Every form in which the caller of call rig C could have learned the call
    is kept: a chat post, an outbox job (chat text the bot's runner sends), or speech."""
    forms = [t for _cid, t, _w in c.posts]
    if c.outbox.exists():
        forms += [json.loads(p.read_text(encoding="utf-8")).get("text", "")
                  for p in sorted(c.outbox.glob("*.json"))]
    forms += [t for t in c.spoken if t == SPOKEN]
    return [t for t in forms if "being recorded" in t]


def kept_or_told(s, c):
    return (not s.rec_state.keep) or told(c)


def test_the_combination_speaks_when_tts_works(call):  # noqa: F811
    """The control: with TTS working, this combination does tell the caller."""
    s = call.connect(file=KEPT_CHAT_LINE_OFF)
    assert s.rec_state.keep
    assert call.posts == []                            # the chat line is off
    assert told(call) == [SPOKEN]


def test_tts_raising_still_tells_the_caller(call, rig):  # noqa: F811
    """Path 1: TTS raises; speak() reports failed=1 and does not raise."""
    call.tts_fail.add(SPOKEN)
    s = call.connect(file=KEPT_CHAT_LINE_OFF)
    assert any("tts failed: tts server down" in m for m in rig.logs)   # it did fail
    assert call.spoken == call.greeting                # the greeting went on
    assert kept_or_told(s, call), "a kept call reached the greeting with the caller told nothing"


def test_speak_raising_still_tells_the_caller(call, rig, monkeypatch):  # noqa: F811
    """Path 2: speak() itself raises on the notice."""
    real = calls.CallSession.speak

    async def speak(self, text, *a, **k):
        if text == SPOKEN:
            raise RuntimeError("voice track gone")
        return await real(self, text, *a, **k)
    monkeypatch.setattr(calls.CallSession, "speak", speak)
    s = call.connect(file=KEPT_CHAT_LINE_OFF)
    assert any("recording notice: the spoken notice failed: voice track gone" in m
               for m in rig.logs)                      # it did fail, the logged way
    assert call.spoken == call.greeting
    assert kept_or_told(s, call), "a kept call reached the greeting with the caller told nothing"


# ---- the fix: helpers --------------------------------------------------------------------


def outcome_lines(rig):  # noqa: F811
    """The one new line per kept call whose notice did not get through (the
    log's "[call N]" tag dropped)."""
    return [re.sub(r" \[call [^]]*\]$", "", m) for m in rig.logs
            if "recording notice: the spoken notice " in m]


def scripted_post(monkeypatch, c, *script, hang=None):
    """post_to_chat faked per attempt: True posts, False is a refusal, an
    Exception raises, "hang" blocks on HANG (a threading.Event) then posts.
    -> the list of attempts, (chat_id, text) each."""
    attempts, script = [], list(script)

    def post(account, chat_id, text, what):
        attempts.append((chat_id, text))
        step = script.pop(0) if script else True
        if step == "hang":
            hang.wait(5)
            step = True
        if isinstance(step, Exception):
            raise step
        if step:
            c.posts.append((chat_id, text, what))
        return step
    monkeypatch.setattr(calls, "post_to_chat", post)
    return attempts


def release_at_greeting(monkeypatch, ev):
    """Set EV when the greeting is spoken: after the notice was decided, before
    the call rig gathers the posts still running."""
    real = calls.CallSession.speak

    async def speak(self, text, *a, **k):
        if text != SPOKEN:
            ev.set()
        return await real(self, text, *a, **k)
    monkeypatch.setattr(calls.CallSession, "speak", speak)


# ---- the fix: record_notice OFF ---------------------------------------------------------


def test_off_tts_failing_posts_the_notice_once_as_text(call, rig):  # noqa: F811
    call.tts_fail.add(SPOKEN)
    s = call.connect(file=KEPT_CHAT_LINE_OFF)
    assert call.posts == [(12, KEPT_7, "recording notice")]     # the chat line's own words, once
    assert not call.outbox.exists()                             # not the tts-fallback marker path
    assert outcome_lines(rig) == [
        "CALL recording notice: the spoken notice failed: tts server down; delivered as text"]
    assert s.rec_state.keep
    assert call.spoken == call.greeting


def test_off_speak_raising_is_one_line_naming_both(call, rig, monkeypatch):  # noqa: F811
    real = calls.CallSession.speak

    async def speak(self, text, *a, **k):
        if text == SPOKEN:
            raise RuntimeError("voice track gone")
        return await real(self, text, *a, **k)
    monkeypatch.setattr(calls.CallSession, "speak", speak)
    call.connect(file=KEPT_CHAT_LINE_OFF)
    assert outcome_lines(rig) == [
        "CALL recording notice: the spoken notice failed: voice track gone; delivered as text"]
    assert [t for _c, t, _w in call.posts] == [KEPT_7]


def test_off_a_cut_notice_counts_as_not_delivered(call, rig, monkeypatch):  # noqa: F811
    real = calls.CallSession.speak

    async def speak(self, text, *a, **k):
        if text == SPOKEN:
            return {"n": 1, "failed": 0, "error": None, "cut": True}
        return await real(self, text, *a, **k)
    monkeypatch.setattr(calls.CallSession, "speak", speak)
    call.connect(file=KEPT_CHAT_LINE_OFF)
    assert [t for _c, t, _w in call.posts] == [KEPT_7]
    assert outcome_lines(rig) == [
        "CALL recording notice: the spoken notice failed: cut short; delivered as text"]


def test_off_the_text_refused_is_logged_and_the_call_is_still_kept(call, rig, monkeypatch):  # noqa: F811
    call.tts_fail.add(SPOKEN)
    attempts = scripted_post(monkeypatch, call, False)
    s = call.connect(file=KEPT_CHAT_LINE_OFF)
    assert attempts == [(12, KEPT_7)]
    assert told(call) == []
    assert s.rec_state.keep                                     # recording is unchanged
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice failed: tts server down; NOT delivered: "
        "the text was refused by channels.txt; the call is still kept")]
    assert call.spoken == call.greeting


def test_off_the_text_raising_is_one_line(call, rig, monkeypatch):  # noqa: F811
    call.tts_fail.add(SPOKEN)
    scripted_post(monkeypatch, call, RuntimeError("rpc gone"))
    s = call.connect(file=KEPT_CHAT_LINE_OFF)
    assert s.rec_state.keep
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice failed: tts server down; NOT delivered: "
        "the text could not be sent: rpc gone; the call is still kept")]
    assert not any("could not post to chat" in m for m in rig.logs)   # no second line for it


def test_off_the_text_hung_past_the_bound_is_not_confirmed(call, rig, monkeypatch):  # noqa: F811
    monkeypatch.setattr(calls, "REC_NOTICE_TEXT_WAIT", 0.05)
    call.tts_fail.add(SPOKEN)
    ev = threading.Event()
    release_at_greeting(monkeypatch, ev)
    attempts = scripted_post(monkeypatch, call, "hang", hang=ev)
    call.connect(file=KEPT_CHAT_LINE_OFF)
    assert attempts == [(12, KEPT_7)]
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice failed: tts server down; "
        "text not confirmed within 0.05 s; the call is still kept")]
    assert call.spoken == call.greeting                         # the greeting went on


def test_off_tts_working_posts_no_text_and_logs_nothing_new(call, rig):  # noqa: F811
    call.connect(file=KEPT_CHAT_LINE_OFF)
    assert call.posts == []
    assert outcome_lines(rig) == []


def test_both_notices_off_on_a_kept_call_stay_silent(call, rig):  # noqa: F811
    s = call.connect(file={**KEPT_CHAT_LINE_OFF, "record_spoken_notice": False})
    assert s.rec_state.keep
    assert call.posts == [] and call.spoken == call.greeting    # the operator's choice
    assert outcome_lines(rig) == []


# ---- the fix: record_notice ON ----------------------------------------------------------


def test_on_chat_line_posted_tts_failing_logs_told_by_the_chat_line(call, rig):  # noqa: F811
    call.tts_fail.add(SPOKEN)
    call.connect(file={"record": True})
    assert [t for _c, t, _w in call.posts] == [KEPT_7]          # no duplicate
    assert not call.outbox.exists()
    assert outcome_lines(rig) == [
        "CALL recording notice: the spoken notice failed: tts server down; told by the chat line"]


def test_on_chat_line_refused_tts_failing_is_not_retried(call, rig, monkeypatch):  # noqa: F811
    call.tts_fail.add(SPOKEN)
    attempts = scripted_post(monkeypatch, call, False)
    s = call.connect(file={"record": True})
    assert attempts == [(12, KEPT_7)]                           # ONE attempt, no retry
    assert told(call) == []                                     # the residual, asserted
    assert s.rec_state.keep
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice failed: tts server down; NOT delivered: "
        "the chat line was refused by channels.txt, so no text was sent; the call is still kept")]
    assert call.spoken == call.greeting


def test_on_chat_line_raising_then_the_text_goes(call, rig, monkeypatch):  # noqa: F811
    call.tts_fail.add(SPOKEN)
    attempts = scripted_post(monkeypatch, call, RuntimeError("rpc gone"), True)
    s = call.connect(file={"record": True})
    assert attempts == [(12, KEPT_7), (12, KEPT_7)]             # the chat line, then the text once
    assert told(call) == [KEPT_7]
    assert s.rec_state.keep
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice failed: tts server down; "
        "the chat line could not be sent: rpc gone; delivered as text")]


def test_on_chat_line_always_raising_is_logged_and_the_greeting_goes_on(call, rig, monkeypatch):  # noqa: F811
    call.tts_fail.add(SPOKEN)
    boom = RuntimeError("rpc gone")
    attempts = scripted_post(monkeypatch, call, boom, boom)
    s = call.connect(file={"record": True})
    assert len(attempts) == 2
    assert told(call) == []
    assert s.rec_state.keep
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice failed: tts server down; "
        "the chat line could not be sent: rpc gone; NOT delivered: the text could not be sent: "
        "rpc gone; the call is still kept")]
    assert call.spoken == call.greeting


def test_on_chat_line_hung_past_the_bound_sends_no_second_line(call, rig, monkeypatch):  # noqa: F811
    monkeypatch.setattr(calls, "REC_NOTICE_TEXT_WAIT", 0.05)
    call.tts_fail.add(SPOKEN)
    ev = threading.Event()
    release_at_greeting(monkeypatch, ev)
    attempts = scripted_post(monkeypatch, call, "hang", hang=ev)
    call.connect(file={"record": True})
    assert attempts == [(12, KEPT_7)]                           # no second attempt: it may land
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice failed: tts server down; the chat line was "
        "not confirmed within 0.05 s, so no text was sent; the call is still kept")]
    assert call.spoken == call.greeting
    assert [t for _c, t, _w in call.posts] == [KEPT_7]          # it landed late, once


def test_on_tts_working_never_waits_on_the_chat_line(call, rig, monkeypatch):  # noqa: F811
    """A said notice returns at once: a hung chat line never holds the greeting."""
    monkeypatch.setattr(calls, "REC_NOTICE_TEXT_WAIT", 30.0)
    ev = threading.Event()
    release_at_greeting(monkeypatch, ev)
    attempts = scripted_post(monkeypatch, call, "hang", hang=ev)
    call.connect(file={"record": True})
    assert call.spoken == [SPOKEN, *call.greeting]
    assert len(attempts) == 1
    assert outcome_lines(rig) == []


# ---- the fix: record_spoken_notice OFF, and calls not kept --------------------------------


def test_spoken_off_chat_line_raising_then_the_text_goes(call, rig, monkeypatch):  # noqa: F811
    attempts = scripted_post(monkeypatch, call, RuntimeError("rpc gone"), True)
    s = call.connect(file={"record": True, "record_spoken_notice": False})
    assert attempts == [(12, KEPT_7), (12, KEPT_7)]
    assert told(call) == [KEPT_7]
    assert s.rec_state.keep
    assert SPOKEN not in call.spoken                            # the setting is obeyed
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice is off; "
        "the chat line could not be sent: rpc gone; delivered as text")]


def test_spoken_off_chat_line_posted_logs_nothing_new(call, rig):  # noqa: F811
    call.connect(file={"record": True, "record_spoken_notice": False})
    assert [t for _c, t, _w in call.posts] == [KEPT_7]
    assert outcome_lines(rig) == []


def test_spoken_off_chat_line_refused_is_logged_not_retried(call, rig, monkeypatch):  # noqa: F811
    attempts = scripted_post(monkeypatch, call, False)
    s = call.connect(file={"record": True, "record_spoken_notice": False})
    assert len(attempts) == 1
    assert s.rec_state.keep
    assert SPOKEN not in call.spoken
    assert outcome_lines(rig) == [(
        "CALL recording notice: the spoken notice is off; NOT delivered: the chat line was "
        "refused by channels.txt, so no text was sent; the call is still kept")]


@pytest.mark.parametrize("step", [False, RuntimeError("rpc gone")])
def test_not_kept_the_chat_line_is_not_chased(call, rig, monkeypatch, step):  # noqa: F811
    attempts = scripted_post(monkeypatch, call, step)
    s = call.connect(file={"record": False})
    assert not s.rec_state.keep
    assert len(attempts) == 1                                   # no retry
    assert outcome_lines(rig) == []
    assert call.spoken == call.greeting

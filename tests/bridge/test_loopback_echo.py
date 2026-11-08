"""The loopback echo rig (tests/bridge/loopback_echo.py): its isolation, its
handset, its stub voice and its verdicts, offline; and one stub call.

The rig answers "does the bot cut itself off on its own echo?" without a
person: the fake phone's microphone is the caller's line plus the bot's own
audio fed back attenuated and late. Its numbers are only as good as these
parts, so they are pinned here: the real home is never touched (HOME and
every root under tmp, before any import), the echo path's gain and delay,
the mic's mix, the stub voice's codebook, and the judge that calls a
BARGE-IN a self-cut or the caller's.

The stub call is the rig's default: an in-process stub TTS and STT, no
speech server, no network. Its opt-in real-speech mode (--speech-url)
reaches a server, so no test here runs it.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
RIG = HERE / "loopback_echo.py"
ROOT = HERE.parents[1]


def _rig():
    spec = importlib.util.spec_from_file_location("loopback_echo", RIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # no calls.py import, no env change at import
    return mod


rig = _rig()


# ---- the handset

def test_db_is_amplitude():
    assert rig.db(0) == 1.0
    assert rig.db(-20) == pytest.approx(0.1)
    assert rig.db(-40) == pytest.approx(0.01)


def test_mono16_folds_packed_stereo():
    st = np.array([[100, 300, -50, -150]], dtype=np.int16)      # L R L R
    assert rig.mono16(st, 2).tolist() == [200, -100]
    assert rig.mono16(np.array([[7, 8]], dtype=np.int16), 1).tolist() == [7, 8]


def test_echo_path_is_late_by_the_delay_and_down_by_the_gain():
    p = rig.EchoPath(-20.0, 0.100)
    p.heard(np.full(rig.FRAME, 1000, dtype=np.int16), at=10.0)
    assert not p.take(rig.FRAME, now=10.05).any()               # 50 ms: not yet
    got = p.take(rig.FRAME, now=10.10)                           # 100 ms: here
    assert got == pytest.approx(np.full(rig.FRAME, 100.0))       # -20 dB = x0.1
    assert not p.take(rig.FRAME, now=10.12).any()                # played once only


def test_echo_path_keeps_order_across_frames():
    p = rig.EchoPath(0.0, 0.06)
    p.heard(np.full(rig.FRAME, 1, dtype=np.int16), at=1.00)
    p.heard(np.full(rig.FRAME, 2, dtype=np.int16), at=1.02)
    assert p.take(rig.FRAME, now=1.08)[0] == 1.0
    assert p.take(rig.FRAME, now=1.10)[0] == 2.0


def test_mic_mixes_caller_and_echo_and_clips():
    p = rig.EchoPath(0.0, 0.0)
    clip = np.full(rig.FRAME * 2, 30000, dtype=np.int16)
    m = rig.Mic(clip, p)
    assert not m.frame(now=0.0).any()                            # caller not started
    m.start = 1.0
    p.heard(np.full(rig.FRAME, 10000, dtype=np.int16), at=1.0)
    out = m.frame(now=1.0)
    assert out.dtype == np.int16 and out.max() == 32767          # 30000 + 10000, clipped
    assert m.frame(now=1.02).tolist() == [30000] * rig.FRAME     # clip's 2nd frame, no echo
    assert not m.frame(now=1.04).any()                           # clip over
    assert m.caller_window() == (1.0, 1.0 + 2 * rig.FRAME / rig.RATE)
    assert len(m.caller_rms) == 2 and len(m.echo_rms) == 1


def test_echo_only_mic_is_the_echo_alone():
    p = rig.EchoPath(-6.0, 0.0)
    m = rig.Mic(np.zeros(0, dtype=np.int16), p)
    m.start = 0.0
    p.heard(np.full(rig.FRAME, 2000, dtype=np.int16), at=0.0)
    assert m.frame(now=0.0)[0] == int(2000 * rig.db(-6.0))
    assert m.caller_rms == []


def test_dbfs():
    assert rig.dbfs([]) is None
    assert rig.dbfs([32768.0]) == 0.0
    assert rig.dbfs([327.68] * 3) == -40.0


# ---- the verdicts

def test_similar_sees_short_lines_the_guard_does_not_judge():
    from sessionphone.bridge import echo
    assert echo.score("Loop back here.", "Loopback here.") == 0.0   # under MIN_CHARS
    assert rig.similar("Loop back here.", "Loopback here.") > 0.8
    assert rig.similar("", "anything") == 0.0


def test_judge_self_cut_by_time_and_text():
    said = ["The third part is the documentation, which is waiting on one more careful read."]
    chunks = {"caller-003.wav": ((5.0, 7.0), "which is waiting on one more careful read")}
    [v] = rig.judge([("caller-003.wav", 4.0)], chunks, (10.0, 13.0),
                    rig.CALLER_TEXT, said, rig.similar)
    assert (v["by_time"], v["by_text"]) == ("self", "self")


def test_judge_caller_cut_by_time_and_text():
    said = ["Here is where the build stands this morning."]
    chunks = {"caller-005.wav": ((10.5, 12.0), "I want to ask about the other thing first.")}
    [v] = rig.judge([("caller-005.wav", 6.0)], chunks, (10.0, 13.0),
                    rig.CALLER_TEXT, said, rig.similar)
    assert (v["by_time"], v["by_text"]) == ("caller", "caller")
    assert v["caller_score"] == 1.0


def test_judge_keeps_a_disagreement():
    """Echo words in a chunk that overlapped the caller: time says caller, text
    says self. Both are reported, never resolved quietly."""
    said = ["When it is finished, the numbers go to the super in the morning report."]
    chunks = {"caller-007.wav": ((11.0, 12.0), "the numbers go to the super")}
    [v] = rig.judge([("caller-007.wav", 6.0)], chunks, (10.0, 13.0),
                    rig.CALLER_TEXT, said, rig.similar)
    assert (v["by_time"], v["by_text"]) == ("caller", "self")


def test_judge_echo_only_has_no_caller():
    chunks = {"caller-001.wav": ((1.0, 2.0), "anything at all here")}
    [v] = rig.judge([("caller-001.wav", 1.0)], chunks, None, None, ["x"], rig.similar)
    assert v["by_time"] == "self" and v["caller_score"] == 0.0


def test_judge_an_unknown_chunk_is_a_self_cut_with_no_text():
    [v] = rig.judge([("caller-009.wav", 1.0)], {}, (0.0, 5.0), rig.CALLER_TEXT, ["x"], rig.similar)
    assert v["by_time"] == "self" and v["text"] == ""


def test_table_renders_a_failed_run():
    t = rig.table([{"gain_db": -20.0, "delay_ms": 60, "echo_only": True, "ok": False}])
    assert "FAILED" in t.splitlines()[-1]
    # a call that never came up says so; its zeros are not data
    assert "FAILED" in rig.summary({"gain_db": 0, "delay_ms": 60, "echo_only": False,
                                    "ok": False, "barge_in": 0})


def test_an_unreachable_speech_url_is_a_skip_not_a_failure(monkeypatch):
    monkeypatch.setattr(rig, "speech_up", lambda *a, **k: False)
    assert rig.main(["--echo-only", "--speech-url", "http://127.0.0.1:9"]) == rig.SKIP


def test_the_speech_url_is_opt_in_with_no_default():
    help_text = subprocess.run([sys.executable, str(RIG), "--help"], capture_output=True,
                               text=True, timeout=30, check=False).stdout
    assert "reaches a speech server" in " ".join(help_text.split())
    assert "8890" not in RIG.read_text()          # no server is assumed


# ---- isolation

def test_isolate_refuses_once_sessionphone_is_imported(monkeypatch):
    import sessionphone  # noqa: F401 - already imported by the suite; the point
    monkeypatch.setenv("DC_BARGE_IN", "0")
    with pytest.raises(RuntimeError, match="before isolation"):
        rig.isolate()
    assert os.environ["DC_BARGE_IN"] == "0"       # refused before it changed anything


ISOLATED = r"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("rig", sys.argv[1])
rig = importlib.util.module_from_spec(spec); spec.loader.exec_module(rig)
home, root = rig.isolate()
from sessionphone import paths
from sessionphone.bridge import calls, dc_env, echo, heard_sink
print(json.dumps({"home": str(home), "env_home": os.environ["HOME"],
    "roots": [str(dc_env.ROOT), str(calls.CALLS_DIR), str(paths.log_dir("loopback").expanduser()),
              os.path.expanduser(paths.kit_dir("voice", "settings")), echo.SPOOL,
              echo.VOICEMODE_EVENTS, str(heard_sink.heard_py())],
    "barge": calls.BARGE_IN, "ears": calls.EARS, "agent": dc_env.AGENT,
    "seat": [k for k in os.environ if k in ("CLAUDE_CODE_AGENT", "EMAIL", "ECHO_SPOOL")
             or k in ("DC_BARGE_IN", "DC_EARS", "PIP_CHAT_BARGE_IN")]}))
"""


def test_isolate_puts_every_root_under_a_tmp_home(tmp_path):
    env = dict(os.environ, DC_BARGE_IN="0", DC_EARS="1", PIP_CHAT_BARGE_IN="0",
               CLAUDE_CODE_AGENT="someseat", EMAIL="a+b.worker@example.org",
               ECHO_SPOOL=str(tmp_path / "seat-spool"), DC_ROOT=str(tmp_path / "seat-root"))
    p = subprocess.run([sys.executable, "-c", ISOLATED, str(RIG)], capture_output=True,
                       text=True, timeout=60, env=env, cwd=ROOT, check=False)
    assert p.returncode == 0, p.stderr[-2000:]
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert r["env_home"] == r["home"] and r["home"] != os.environ["HOME"]
    for root in r["roots"]:
        assert root.startswith(r["home"] + os.sep), root
    assert r["barge"] is True and r["ears"] is False           # the defaults, whatever the seat set
    assert r["agent"] == "loopback" and r["seat"] == []


def test_off_host_is_anything_that_leaves_this_machine():
    own = {"192.0.2.7", "2001:db8::7"}
    for here in (None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0", "192.0.2.7",
                 "2001:db8::7", b"127.0.0.1"):
        assert not rig.off_host(here, own), here
    for away in ("198.51.100.9", "2001:db8::9", "stun.example.org", b"relay.example.org"):
        assert rig.off_host(away, own), away


def test_net_audit_records_every_way_off_this_machine():
    """STUN leaves by a gethostbyname() lookup then a UDP sendto(), never a
    connect(): the audit must see each, and stay quiet about loopback."""
    audit, net, procs, away = rig.net_audit({"192.0.2.7"})
    sock = object()
    for event, args in [
        ("socket.gethostbyname", ("stun.example.org",)),
        ("socket.gethostbyaddr", ("198.51.100.8",)),
        ("socket.getaddrinfo", ("relay.example.org", 443, 0, 0, 0)),
        ("socket.sendto", (sock, ("198.51.100.9", 19302))),
        ("socket.connect", (sock, ("203.0.113.5", 443))),
        # all of these stay here
        ("socket.gethostbyname", ("localhost",)),
        ("socket.getaddrinfo", ("127.0.0.1", 8890, 0, 0, 0)),
        ("socket.sendto", (sock, ("192.0.2.7", 50000))),
        ("socket.sendto", (sock, ("::1", 50000, 0, 0))),
        ("socket.connect", (sock, ("127.0.0.1", 8890))),
        ("socket.connect", (sock, "/tmp/some.sock")),
        ("subprocess.Popen", ("/usr/bin/true", ["true"], None, None)),
    ]:
        audit(event, args)
    assert away == {
        "gethostbyname 'stun.example.org'", "gethostbyaddr '198.51.100.8'",
        "getaddrinfo 'relay.example.org'", "sendto 198.51.100.9:19302",
        "connect 203.0.113.5:443",
    }
    assert len(net) == 3 and procs == ["true"]


def test_the_fake_phone_asks_no_ice_server():
    """With no configuration aiortc's peer gathers against its default public
    STUN server, and STUN is UDP sendto(), which the connect() audit never
    saw. The phone is built as the bridge builds its own: iceServers=[]."""
    src = RIG.read_text()
    assert "RTCPeerConnection(RTCConfiguration(iceServers=[]))" in src
    assert "RTCPeerConnection()" not in src


def test_changed_names_every_difference():
    before = {"a": (1, 1, 1), "b": (1, 1, 1)}
    after = {"a": (2, 1, 1), "c": (1, 1, 1)}
    assert rig.changed(before, after) == ["a", "b", "c"]
    assert rig.changed(before, dict(before)) == []


# ---- the stub voice

def _to16(x):
    return np.asarray(x)[::3]                 # 48 kHz -> 16 kHz, enough for a tone


def test_stub_voice_reads_back_what_it_said():
    v = rig.ToneVoice()
    for text in (rig.CALLER_TEXT, "Loopback here.", "Here is where the build stands this morning."):
        assert v.hear(_to16(v.say(text))) == text


def test_stub_voice_hears_the_louder_of_two_lines():
    v = rig.ToneVoice()
    him, us = v.say(rig.CALLER_TEXT), v.say("The first two parts landed overnight and both passed.")
    n = min(len(him), len(us))
    assert v.hear(_to16(him[:n] + (us[:n] * 0.3).astype(np.int16))).startswith("Hold on, stop there")
    assert v.hear(_to16((him[:n] * 0.3).astype(np.int16) + us[:n])).startswith("The first two parts")


def test_stub_voice_hears_nothing_under_its_floor():
    v = rig.ToneVoice()
    quiet = (v.say("Here is where the build stands this morning.") * rig.db(-40)).astype(np.int16)
    assert v.hear(_to16(quiet)) == ""
    assert v.hear(np.zeros(16000, dtype=np.int16)) == ""


def test_stub_voice_gives_each_line_its_own_band():
    v = rig.ToneVoice()
    assert v.code(rig.CALLER_TEXT) == 0
    assert v.code("one") == 1 and v.code("two") == 2 and v.code("one") == 1
    assert v.word_at(v.BAND0 - 50) is None
    assert v.word_at(v.BAND0 + 3 * v.BAND_W) is None             # no line 3 yet


# ---- two stub calls: the regression tests

def _stub_call(tmp_path, *args):
    out = tmp_path / "r.json"
    env = {k: v for k, v in os.environ.items() if k not in ("DC_BARGE_IN", "DC_EARS")}
    p = subprocess.run([sys.executable, str(RIG), *args, "--json", str(out)],
                       capture_output=True, text=True, timeout=300, env=env, cwd=ROOT, check=False)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]
    r = json.loads(out.read_text())
    assert r["voice"].startswith("stub") and r["ok"]
    assert r["real_home_changed"] == [] and r["net_connects"] == [] and r["subprocesses"] == []
    assert r["net_off_host"] == []                # no send or lookup left this machine
    return r


def test_stub_call_a_loud_leak_alone_never_cuts(tmp_path):
    """The caller silent, the handset leaking at 0 dB, 120 ms: the echo is
    loud enough to become chunks, and every one is the echo guard's to drop.
    Any BARGE-IN here is a self-cut, and there is none."""
    r = _stub_call(tmp_path, "--gain", "0", "--delay", "120", "--echo-only")
    assert r["barge_in"] == 0 and r["held"] == 0, r["echo_lines"]
    assert r["echo_drops"] >= 5, r["echo_lines"]               # the guard was exercised


def test_stub_call_the_caller_cuts_the_reply(tmp_path):
    """The caller talks over the reply on a -20 dB leak: his words, and only
    they, cut it."""
    r = _stub_call(tmp_path, "--gain", "-20", "--delay", "120")
    assert r["self_cuts"] == 0 and r["caller_cuts"] == 1, r["echo_lines"]
    assert r["held_on_caller"] == 0, r["echo_lines"]

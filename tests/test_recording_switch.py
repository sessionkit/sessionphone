"""A call keeps no audio unless recording is on (PHONE-177).

The switch is the ears' one (sessionvoice's recording-switch contract, "EARS-387
recording switch contract, rules 1-11"), re-read here by
bridge/recording.py: --record/--no-record on the bot's argv, then EARS_RECORD,
then "record" in $EARS_CONF or recording.EARS_CONF_DEFAULT, then OFF.

1. Parity: the contract's fixture cases, copied byte for byte from the
   sessionvoice side (tests/fixtures/recording-switch-cases.md): `record` and
   `record_days` on every case, and the two notice settings (record_notice,
   record_spoken_notice, rules 10-11; tests/test_recording_notice.py holds
   what a call does with them).
2. A call with the switch off leaves no audio file under CALLS_DIR once it is
   over, on both hearing paths (the energy chunker and the ears leg). During the
   call, STT still gets a real chunk file and the ears path still writes
   caller-call.wav at its usual path (the speaker side-car reads it live).
3. With the switch on, a call keeps what it kept before, named in recording.json.
4. A crash sweep at start cleans only a dir marked transient; a call dir made
   before the switch (no marker) is never touched. The retention prune deletes
   only audio a call kept under the switch.
"""
import asyncio
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import types

import numpy as np
import pytest
from av import AudioFrame

from sessionphone.bridge import calls, recording

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests/fixtures/recording-switch-cases.json"
FIXTURE_SHA = "0b63e136c4584ff2585cc961c09e4ec6711a50f922fb904785faa10fda5d19ce"
CASES = json.loads(FIXTURE.read_text())["cases"]


# ---- 1. the contract's fixture cases ------------------------------------------------


def test_the_fixture_is_the_contracts_own_copy():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA
    assert json.loads(FIXTURE.read_text())["contract"] == "EARS-387 recording switch contract, rules 1-11"
    assert len(CASES) == 59        # the first 35 are the switch's; 24 more for rules 10-11


def test_every_expected_key_is_asserted():
    keys = set().union(*(c["expect"] for c in CASES))
    assert keys == {"record", "record_days", "record_notice", "record_spoken_notice"}


def _layers(case, tmp_path):
    """-> (argv for the bot, env, ears.json path) as the case describes them."""
    inp = case["input"]
    names = ("EARS_RECORD", "EARS_RECORD_DAYS", "EARS_RECORD_NOTICE", "EARS_RECORD_SPOKEN_NOTICE")
    env = {k: inp.get(f"env_{k}") for k in names if inp.get(f"env_{k}") is not None}
    path = tmp_path / "ears.json"
    f = inp["file"]
    if f == "absent":
        pass
    elif f == "unreadable":
        path.write_text('{"record": true}')
        path.chmod(0)
    elif f == "invalid-json":
        path.write_text("{not json")
    else:
        path.write_text(f["raw"])
    argv = ["carol"] + ({True: ["--record"], False: ["--no-record"], None: []}[inp["cli"]])
    return argv, env, str(path)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_contract_case(case, tmp_path):
    if case["input"]["file"] == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    argv, env, path = _layers(case, tmp_path)
    rest, cli = recording.cli_flag(argv)      # the CLI layer is the bot's own argv
    assert rest == ["carol"]
    rec, days, _warnings = recording.resolve_all(cli, env=env, path=path)
    assert rec.as_dict() == case["expect"]["record"]
    assert days.as_dict()["value"] == case["expect"]["record_days"]["value"]
    assert days.source == case["expect"]["record_days"]["source"]
    notice, spoken, _warnings = recording.resolve_notices(env=env, path=path)
    assert notice.as_dict() == case["expect"]["record_notice"]
    assert spoken.as_dict() == case["expect"]["record_spoken_notice"]
    sw = recording.Switch(cli, env=env, path=path, log=lambda m: None)   # what a call reads
    assert sw.record().as_dict() == case["expect"]["record"]
    assert sw.days().value == case["expect"]["record_days"]["value"]
    assert sw.notice().as_dict() == case["expect"]["record_notice"]
    assert sw.spoken_notice().as_dict() == case["expect"]["record_spoken_notice"]


def test_an_invalid_value_is_said_once_naming_the_layer(tmp_path):
    said = []
    sw = recording.Switch(None, env={"EARS_RECORD": "yes"}, path=str(tmp_path / "none.json"), log=said.append)
    for _ in range(3):
        assert sw.record().on is False
    bad = [m for m in said if "EARS_RECORD='yes'" in m]
    assert len(bad) == 1 and "fails closed" in bad[0]


def test_the_last_flag_on_argv_wins():
    assert recording.cli_flag(["--record", "a", "--no-record"]) == (["a"], False)
    assert recording.cli_flag(["--no-record", "--record", "a"]) == (["a"], True)
    assert recording.cli_flag(["a"]) == (["a"], None)


def test_the_file_is_followed_live_and_the_env_is_fixed(tmp_path):
    conf = tmp_path / "ears.json"
    conf.write_text('{"record": false}')
    sw = recording.Switch(None, env={}, path=str(conf), log=lambda m: None)
    assert sw.record().as_dict() == {"on": False, "source": "file", "invalid": False}
    conf.write_text('{"record": true, "listen": 1}')     # a new size: a new stat
    assert sw.record().as_dict() == {"on": True, "source": "file", "invalid": False}
    conf.unlink()
    assert sw.record().source == "default"
    env = {"EARS_RECORD": "1"}
    sw = recording.Switch(None, env=env, path=str(conf), log=lambda m: None)
    env["EARS_RECORD"] = "0"                             # too late: taken at start
    assert sw.record().on is True


# ---- what the bot's argv carries -------------------------------------------------------

PROBE = ("import json, runpy, sys; sys.argv = ['bot.py', *sys.argv[1:]]; "
         "g = runpy.run_module('sessionphone.bridge.bot', run_name='argv_probe'); "
         "from sessionphone.bridge import dc_env; "
         "print(json.dumps([dc_env.AGENT, g['RECORD_CLI'], sys.argv[1:]]))")


@pytest.mark.parametrize("argv, want", [
    (["carol"], ["carol", None, ["carol"]]),
    (["--record", "carol"], ["carol", True, ["carol"]]),
    (["carol", "--record", "--no-record"], ["carol", False, ["carol"]]),
])
def test_the_bot_takes_the_flag_off_argv_before_it_reads_the_agent(argv, want, tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_AGENT", "SESSIONPHONE_ROOT") and not k.startswith(("DC_", "PIP_CHAT_"))}
    env.update(DC_HOST="host.example", DC_ROOT=str(tmp_path / "root"), DC_CALLS="off")
    r = subprocess.run([sys.executable, "-c", PROBE, *argv], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.strip().splitlines()[-1]) == want


# ---- 2/3. a call, end to end -------------------------------------------------------------


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """CALLS_DIR in tmp, the hang-up drain at 0 s, every CALL line kept."""
    ns = types.SimpleNamespace(logs=[], heard=[], root=tmp_path / "calls")
    ns.root.mkdir()
    monkeypatch.setattr(calls, "CALLS_DIR", ns.root)
    monkeypatch.setattr(recording, "RECORD_DRAIN", 0.0)
    monkeypatch.setattr(calls, "_recorder", None)
    monkeypatch.setattr(calls, "_last_prune", None)
    monkeypatch.setattr(calls.earleg, "end", lambda *a: None)

    def transcribe(p):
        # STT gets a real file while the call is live, switch or no switch
        ns.heard.append((pathlib.Path(p).name, os.path.getsize(p)))
        return ""
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(ns.logs.append, transcribe, lambda *a, **k: None))
    ns.audio = lambda: sorted(str(p.relative_to(ns.root)) for p in ns.root.rglob("*")
                              if p.suffix == ".wav" or p.name == calls.CALLER_JSON)
    ns.files = lambda d: sorted(p.name for p in d.iterdir())
    ns.said = lambda: [m for m in ns.logs if m.startswith(("CALL recording: on (", "CALL recording: off ("))]
    return ns


def session(rig):
    s = object.__new__(calls.CallSession)
    s.dir = rig.root / "20300609-123456-chat12-msg7"
    s.chat_id, s.msg_id = 12, 7
    s.voice, s.echo = calls.PipVoiceTrack(), calls.EchoGuard(log=lambda m: None)
    s.lines, s.turn_parts, s.turn_pending, s.tasks = [], [], set(), []
    s.turn_timer, s.speaking, s.chunk_n = None, False, 0
    s.answer_go, s.closed, s.connected = None, None, False
    s.manager = types.SimpleNamespace(forget=lambda x: None, sessions={})
    s.awaiting, s.ear_taps = (), []
    s._live_clear = lambda: None
    s._turn_arm = lambda: None             # no turn timers: the chunks are what is under test

    async def no_turn():
        s.turn_parts = []
    s._turn_out = no_turn

    class PC:
        async def close(self):
            pass
    s.pc = PC()
    return s


def frame(loud, pts=None):
    n = 960                                 # 20 ms at 48 kHz
    a = (np.sin(np.arange(n) * 2 * np.pi * 440 / 48000) * 8000).astype(np.int16) if loud \
        else np.zeros(n, dtype=np.int16)
    f = AudioFrame.from_ndarray(a.reshape(1, -1), format="s16", layout="mono")
    f.sample_rate = 48000
    return f


class Speech:
    """He says two things, with a pause after each, then the call ends."""

    def __init__(self, between=None):
        self.frames = ([True] * 50 + [False] * 40) * 2
        self.between = between          # called once, after the first phrase

    async def recv(self):
        if not self.frames:
            raise ConnectionError("call ended")
        if self.between and len(self.frames) == 90:
            self.between()
            self.between = None
        await asyncio.sleep(0)
        return frame(self.frames.pop(0))


async def a_call(s, listener, track):
    await listener(track)
    await asyncio.gather(*s.tasks, return_exceptions=True)   # STT returns
    during = sorted(p.name for p in s.dir.iterdir())
    await s.close("hung up")
    if s.rec_drain is not None:
        await s.rec_drain
    return during


class Pipeline:
    def __init__(self, *a, **k):
        self.b, self.edges, self.fed = types.SimpleNamespace(name="fake"), None, 0

    def feed(self, pcm):
        self.fed += len(pcm)

    def close(self):
        pass


@pytest.fixture
def ears(monkeypatch):
    monkeypatch.setattr(calls, "EARS", True)
    monkeypatch.setitem(sys.modules, "ears", types.ModuleType("ears"))
    monkeypatch.setitem(sys.modules, "ears.cli", types.SimpleNamespace(Pipeline=Pipeline))


@pytest.fixture
def energy(monkeypatch):
    monkeypatch.setattr(calls, "EARS", False)


def test_off_the_energy_path_keeps_no_audio_after_the_call(rig, energy):
    s = session(rig)
    asyncio.run(a_call(s, s._listen, Speech()))
    assert [n for n, _size in rig.heard] == ["caller-001.wav", "caller-002.wav"]
    assert all(size > 44 for _n, size in rig.heard)          # STT read real audio
    assert rig.audio() == []
    assert rig.files(s.dir) == []                              # no marker, no recording.json either
    assert rig.said()[0] == "CALL recording: off (default), retention 7 days (default)"


def test_off_the_ears_path_writes_the_wav_only_while_the_call_is_live(rig, ears):
    s = session(rig)
    during = asyncio.run(a_call(s, s._listen_ears, Speech()))
    assert during == ["caller-call.transient", "caller-call.wav"]   # the side-car reads it live; no clock
    assert rig.audio() == []
    assert rig.files(s.dir) == []
    assert any("audio file(s) deleted from 20300609-123456-chat12-msg7" in m for m in rig.logs)


def test_off_with_nothing_on_disk_the_ears_path_never_opens_the_wav(rig, ears, monkeypatch):
    monkeypatch.setattr(recording, "TRANSIENT_DURING_CALL", False)
    s = session(rig)
    during = asyncio.run(a_call(s, s._listen_ears, Speech()))
    assert during == []
    assert s.wav_samples > 0                       # the clock (and so ears and the ear leg) still ran
    assert rig.audio() == []


def test_on_the_ears_path_keeps_the_call_and_names_it(rig, ears, monkeypatch):
    monkeypatch.setenv("EARS_RECORD", "1")
    s = session(rig)
    asyncio.run(a_call(s, s._listen_ears, Speech()))
    want = ["caller-call.json", "caller-call.wav", "full-in.wav", "full-out.wav"]
    assert rig.files(s.dir) == want + ["recording.json"]
    kept = recording.read_kept(s.dir)
    assert sorted(kept["kept"]) == want
    assert kept["record"] == {"on": True, "source": "env", "invalid": False}
    assert kept["record_days"]["value"] == 7
    assert rig.said()[0] == "CALL recording: on (env EARS_RECORD=1), retention 7 days (default)"
    assert "CALL recording: on: every voice the call carries is kept, guests included" in rig.logs


def test_on_the_energy_path_keeps_every_chunk(rig, energy, monkeypatch):
    monkeypatch.setenv("EARS_RECORD", "on")
    s = session(rig)
    asyncio.run(a_call(s, s._listen, Speech()))
    assert rig.files(s.dir) == ["caller-001.wav", "caller-002.wav", "full-in.wav", "full-out.wav",
                                "recording.json"]


def test_an_invalid_switch_records_nothing_and_says_why(rig, energy, monkeypatch):
    monkeypatch.setenv("EARS_RECORD", "yes")
    s = session(rig)
    asyncio.run(a_call(s, s._listen, Speech()))
    assert rig.audio() == []
    assert rig.said()[0] == ("CALL recording: off (env EARS_RECORD='yes' invalid, fails closed), "
                             "retention 7 days (default)")


def test_turned_off_mid_call_off_wins_at_once(rig, ears, tmp_path, monkeypatch):
    conf = tmp_path / "ears.json"
    conf.write_text('{"record": true}')
    monkeypatch.setattr(calls, "_switch", recording.Switch(None, env={}, path=str(conf), log=lambda m: None))
    s = session(rig)

    def off():
        conf.write_text('{"record": false, "x": 1}')
        s._rec_check()                  # the next unit (a turn, here) reads it
    asyncio.run(a_call(s, s._listen_ears, Speech(between=off)))
    assert rig.audio() == []
    assert any("turned off mid-call" in m for m in rig.logs)


def test_turned_on_mid_call_keeps_from_the_next_chunk(rig, energy, tmp_path, monkeypatch):
    conf = tmp_path / "ears.json"
    conf.write_text('{"record": false}')
    monkeypatch.setattr(calls, "_switch", recording.Switch(None, env={}, path=str(conf), log=lambda m: None))
    s = session(rig)
    asyncio.run(a_call(s, s._listen, Speech(between=lambda: conf.write_text('{"record": true, "x": 1}'))))
    # the first chunk was transient; the second was kept; the whole-call files wait for the next call
    assert rig.files(s.dir) == ["caller-002.wav", "recording.json"]
    assert recording.read_kept(s.dir)["kept"] == ["caller-002.wav"]


def test_a_cancelled_stt_still_deletes_its_chunk(rig, energy, monkeypatch):
    s = session(rig)
    s.dir.mkdir()
    chunk = s.dir / "caller-001.wav"
    chunk.write_bytes(b"RIFF")

    async def go():
        loop = asyncio.get_running_loop()
        never = loop.create_future()
        monkeypatch.setattr(loop, "run_in_executor", lambda *a: never)
        t = asyncio.ensure_future(s._heard(chunk, 1, transient=True))
        await asyncio.sleep(0)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())
    assert not chunk.exists()


# ---- 4. the crash sweep and the prune -------------------------------------------------------


def legacy_dir(root):
    """A call dir from before the switch: audio, no marker, no recording.json."""
    d = root / "20300609-123456-chat12-msg1"
    d.mkdir()
    for n in ("full-in.wav", "full-out.wav", "caller-call.wav", "caller-call.json", "caller-001.wav",
              "transcript.md"):
        (d / n).write_bytes(b"old " + n.encode())
    old = time.time() - 90 * 86400
    for p in d.iterdir():
        os.utime(p, (old, old))
    return d


def snapshot(d):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()}


def test_the_crash_sweep_cleans_a_marked_dir_and_never_an_unmarked_one(rig, monkeypatch):
    legacy = legacy_dir(rig.root)
    before = snapshot(legacy)
    crashed = rig.root / "20300609-123456-chat12-msg5"
    crashed.mkdir()
    for n in ("caller-call.wav", "caller-003.wav", "caller-002.wav", "transcript.md", "ear-taps.jsonl"):
        (crashed / n).write_bytes(b"x")
    recording.mark_transient(crashed, "test")
    recording.keep(crashed, ["caller-002.wav"], recording.Record(True, "file"), recording.Days(7, "default"))
    # the bot's start: calls.setup() sweeps, then starts the manager (stubbed here)
    monkeypatch.setattr(calls, "_manager", types.SimpleNamespace(start=lambda: None))
    calls.setup(rig.logs.append, lambda p: "", lambda *a, **k: None, None)
    assert rig.files(crashed) == ["caller-002.wav", "ear-taps.jsonl", "recording.json", "transcript.md"]
    assert snapshot(legacy) == before
    assert any("crash sweep removed 2 audio file(s) from 20300609-123456-chat12-msg5" in m for m in rig.logs)
    assert any(m.startswith("CALL recording: off (default), retention 7 days (default); read again")
               for m in rig.logs)


def test_the_sweep_skips_a_live_call():
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        d = pathlib.Path(t) / "live"
        d.mkdir()
        (d / "caller-call.wav").write_bytes(b"x")
        recording.mark_transient(d, "test")
        assert recording.sweep(pathlib.Path(t), live=["live"]) == []
        assert (d / "caller-call.wav").exists()


ON, OFF = recording.Record(True, "env"), recording.Record(False, "default")


def prune_fixture(root, now):
    """An old unmarked call dir holding audio, and a marked dir past the cutoff."""
    legacy = legacy_dir(root)
    d = root / "20300609-123456-chat12-msg3"
    d.mkdir()
    for n in ("full-in.wav", "caller-call.wav", "transcript.md"):
        (d / n).write_bytes(b"x")
    recording.keep(d, ["full-in.wav", "caller-call.wav"], ON, recording.Days(7, "default"),
                   now=now - 8 * 86400)
    return legacy, d


def test_the_prune_takes_a_marked_dir_past_its_days_and_never_the_old_archive(rig):
    now = time.time()
    legacy, marked = prune_fixture(rig.root, now)
    before = snapshot(legacy)
    assert recording.prune(rig.root, ON, 7, now=now) == [marked.name]
    assert snapshot(legacy) == before                       # the old file is still there
    assert rig.files(marked) == ["recording.json", "transcript.md"]   # the marked audio is gone
    assert recording.read_kept(marked)["kept"] == []


@pytest.mark.parametrize("rec, days", [(OFF, 7), (ON, 0)], ids=["record off", "record_days 0"])
def test_with_record_off_or_zero_days_nothing_is_pruned(rig, rec, days):
    now = time.time()
    legacy, marked = prune_fixture(rig.root, now)
    before, kept = snapshot(legacy), snapshot(marked)
    assert recording.prune(rig.root, rec, days, now=now) == []
    assert snapshot(legacy) == before and snapshot(marked) == kept


def test_a_marked_dir_inside_its_days_is_kept(rig):
    now = time.time()
    _legacy, marked = prune_fixture(rig.root, now)
    assert recording.prune(rig.root, ON, 30, now=now) == []
    assert "full-in.wav" in rig.files(marked)


@pytest.mark.parametrize("env, pruned", [({}, False), ({"EARS_RECORD": "1"}, True),
                                         ({"EARS_RECORD": "1", "EARS_RECORD_DAYS": "0"}, False)],
                         ids=["off", "on", "on, 0 days"])
def test_the_bot_prunes_at_start_only_while_recording_is_on(rig, monkeypatch, env, pruned):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    legacy, marked = prune_fixture(rig.root, time.time())
    before = snapshot(legacy)
    monkeypatch.setattr(calls, "_manager", types.SimpleNamespace(start=lambda: None))
    calls.setup(rig.logs.append, lambda p: "", lambda *a, **k: None, None)
    assert ("full-in.wav" not in rig.files(marked)) is pruned
    assert snapshot(legacy) == before

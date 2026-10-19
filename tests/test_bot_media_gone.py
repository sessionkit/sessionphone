"""The bot with no media pack: a chat-only bot starts, and a call is refused
plainly, never with an ImportError traceback (PHONE-145).

PHONE-10 moved aiortc, aioice, av, numpy, websockets and cryptography into the
optional `media` extra. Its guard is CLI-side (`media.require()` in `bot run`
and `bot install`); the bot itself, run as launchd runs it
(`python -m sessionphone.bridge.bot AGENT`), never checks. Two halves:

  (A) PHONE-48 F5: a chat-only bot (DC_CALLS=off) cannot start on the light
      core. bot.py imports bridge.calls unconditionally, and calls.py imports
      numpy, aiortc, av and aioice at module top, so the import dies with
      "ModuleNotFoundError: No module named 'numpy'" before main() runs.
  (B) PHONE-10 Q3: a calls-on bot whose pack is gone. At start it dies the
      same way. Under a LIVE bot (pack loaded, then removed from disk) the two
      lazy imports named in triage, calls.py:760 (aiortc.mediastreams) and
      calls.py:2111 (mouthleg), are already in sys.modules and are safe; what
      breaks is the first call: RTCPeerConnection() makes its DTLS certificate
      through cryptography's default_backend(), which imports
      cryptography.hazmat.backends.openssl lazily. A dial job is acked
      "dialling chat N" (ok) and bot.log then says "dial failed:" with the
      traceback; an incoming call logs "session failed:" with the same one.

Each case runs bot.main() in a FRESH process (the suite's own process has the
pack loaded, so a block there would prove nothing). A meta-path finder raises
ModuleNotFoundError for the six modules (the shape media.missing() handles,
PHONE-10 design review 2, N2). Blocked BEFORE the bot imports models a light
env; blocked AFTER models a pack removed from disk under a running bot: a
module already in sys.modules is never looked up again, exactly as a deleted
file does not unload it. The repro steps and the tracebacks seen are in the
task dir, findings/repro-001.md.

Fakes only: no rpc-server, no relay, no launchctl, no mail (DC_SENDMAIL and
DC_MESSAGE are the suite's tripwires). The plain rig tests prove the rig.

THE FIX (fix-001; the root cause is in the task dir, findings/rca-001.md):
bot.py imports calls only when calls are on and the pack is here, and every
call (job or incoming) asks calls_refusal() first. A live removal is seen by
media.gone(), which skips sys.modules, BEFORE the call is queued. A reach the
preflight misses lands on the ImportError backstop in calls.py's go(). The
five tests under (A) and (B) were strict xfails until fix-001 made them pass.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from sessionphone import media

HEAVY = sorted(media.MODULES)
ADDR = "probe@relay.example.org"
CALLER = "pat@relay.example.org"
CHAT = 12

# The probe. PROBE_WHEN: "start" blocks the pack before the bot imports, "live"
# after (the pack removed under a running bot), "never" blocks nothing. It runs
# main() to the end of its first outbox pass (bot's own time.sleep raises Done),
# with the real handle_events on its own thread, then waits for the calls loop
# to settle and writes what it saw to PROBE_OUT. It prints nothing of its own.
PROBE = r'''
import importlib.abc, json, os, sys, threading, time, traceback, types
OUT = os.environ["PROBE_OUT"]
HEAVY = set(json.loads(os.environ["PROBE_BLOCK"]))
WHEN = os.environ["PROBE_WHEN"]
JOBS = json.loads(os.environ["PROBE_JOBS"])
RING_IN = os.environ.get("PROBE_INCOMING") == "1"
PREFLIGHT = os.environ.get("PROBE_PREFLIGHT", "on") == "on"
rec = {"import_error": None, "main": None, "error": None, "lines": [], "attempts": [],
       "acks": {}, "loaded_at_block": None, "dialled": False, "ended": False}

def write():
    with open(OUT, "w") as fh:
        json.dump(rec, fh)

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in HEAVY:
            rec["attempts"].append(name)
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

def block():
    rec["loaded_at_block"] = sorted({m.split(".")[0] for m in sys.modules if m.split(".")[0] in HEAVY})
    sys.meta_path.insert(0, Block())

if WHEN == "start":
    block()
sys.argv = ["sessionphone.bridge.bot", "probe"]    # as launchd runs it
try:
    from sessionphone.bridge import bot
except BaseException:
    rec["import_error"] = traceback.format_exc()
    write()
    sys.exit(0)

class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

class Done(Exception):
    pass

ring = [Obj(kind=bot.EventType.INCOMING_CALL, msg_id=77, chat_id=int(os.environ["PROBE_CHAT"]),
            place_call_info="v=0\r\n", has_video=False)] if RING_IN else []

class Chat:
    def __init__(self, cid):
        self.id = cid
    def get_basic_snapshot(self):
        return Obj(name="Pat", is_contact_request=False)
    def place_outgoing_call(self, sdp, video):
        rec["dialled"] = True
        return Obj(id=501)
    def send_text(self, text):
        return Obj(id=78)

class Account:
    id = 1
    def is_configured(self): return True
    def start_io(self): pass
    def list_transports(self): return [{"addr": os.environ["PROBE_ADDR"]}]
    def get_config(self, key): return None
    def get_qr_code(self): return "https://i.delta.chat/#x"
    def ice_servers(self): return []
    def get_chat_by_id(self, cid): return Chat(cid)
    def get_message_by_id(self, mid):
        sender = Obj(get_snapshot=lambda: Obj(address=os.environ["PROBE_CALLER"], display_name="Pat"))
        def end_call():
            rec["ended"] = True
        return Obj(id=mid, end_call=end_call,
                   get_snapshot=lambda: Obj(chat_id=int(os.environ["PROBE_CHAT"]), sender=sender))
    def wait_for_event(self):
        if ring:
            return ring.pop(0)
        threading.Event().wait()

class Rpc:
    def __init__(self, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

def log(msg):
    rec["lines"].append(str(msg))

bot.log = log
bot.check_root = lambda: None
bot.check_stt = lambda: None
bot.seatmail.check = lambda *a, **k: None
bot.seatmail.setup = lambda *a, **k: "stub"
bot.seatmail.seat = lambda *a, **k: "stub"
bot.dc_env.HOST_PROBLEM = None
bot.SPOOL.counts = lambda: (0, 0)
bot.SPOOL.drain = lambda: None
bot.relink = lambda: None
bot.server_choice = lambda: ("/venv/bin/deltachat-rpc-server", "venv", "test")
bot.Rpc = Rpc
bot.DeltaChat = lambda rpc: Obj(get_all_accounts=lambda: [Account()])
bot.sweep = lambda account: None
bot.Watchdog = lambda account: Obj()
bot.alive_or_exit = lambda t, dog: None
bot.IMAGE_TEXT = False
bot.INVITE = None
if not PREFLIGHT:          # the pack goes AFTER the bot's check and before the reach
    bot.calls_refusal = lambda starts=True: None

class BotTime:
    """bot.py's `time`: only main()'s own loop stops here. The time module stays
    real for every other caller, on the main thread too (a child wait, PHONE-171)."""
    def __getattr__(self, name):
        return getattr(time, name)
    def sleep(self, s):
        raise Done
bot.time = BotTime()

outbox = bot.OUTBOX
outbox.mkdir(parents=True, exist_ok=True)
acks = {}
for i, job in enumerate(JOBS):
    job.setdefault("ack", os.path.join(os.environ["PROBE_ACKS"], f"{i:02d}.json"))   # a test may name its own
    acks[f"{i:02d}"] = job["ack"]
    with open(outbox / f"{i:02d}.json", "w") as fh:
        json.dump(job, fh)

if WHEN == "live":
    block()            # the bot is loaded and running its code; the pack is now gone from disk

try:
    bot.main()
except Done:
    rec["main"] = "one pass"
except SystemExit as e:
    rec["main"] = f"exit {e.code}"
except BaseException:
    rec["main"] = "raised"
    rec["error"] = traceback.format_exc()

# the calls loop runs on its own thread: wait for a dial or a ring to settle
ENDS = ("RINGING msg", "dial failed", "session failed", "media pack", "REFUSED", "closing msg")
if RING_IN or any("call" in j for j in JOBS):
    deadline = time.time() + 30
    while time.time() < deadline and not any(e in l for l in rec["lines"] for e in ENDS):
        time.sleep(0.1)
    time.sleep(0.5)                    # a line or two after the marker
for k, p in acks.items():
    try:
        with open(p) as fh:
            rec["acks"][k] = json.load(fh)
    except OSError:
        rec["acks"][k] = None
write()
os._exit(0)                            # daemon threads (calls loop, events) end with us
'''


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, rec: dict):
        self.stdout, self.stderr = proc.stdout, proc.stderr
        self.__dict__.update(rec)

    @property
    def log(self) -> str:
        return "\n".join(self.lines)

    def everything(self) -> str:
        """Every place a traceback could land: the bot's log, its stderr
        (launchd's StandardErrorPath), and an import or main() failure."""
        return "\n".join([self.log, self.stderr, self.import_error or "", self.error or ""])


def probe(tmp_path: pathlib.Path, *, when: str, calls: str, jobs=(), incoming=False,
          preflight: bool = True) -> Run:
    """bot.main() over fakes in a fresh process, the pack blocked `when`."""
    root, home, out, ack_dir = tmp_path / "root", tmp_path / "home", tmp_path / "probe.json", tmp_path / "acks"
    for d in (home, ack_dir):
        d.mkdir(exist_ok=True)
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    (root / "callers.txt").write_text(f"{CALLER}  # owner\n")
    lst = root / "channels.txt"
    lst.write_text(f"pat  chat:{CHAT}\n")
    lst.chmod(0o600)
    keep = ("DC_SENDMAIL", "DC_MESSAGE", "DC_SIGNER_")      # the suite's mail tripwires stay
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_AGENT", "SESSIONPHONE_ROOT", "PYTHONPATH")
           and (not k.startswith(("DC_", "PIP_CHAT_")) or k.startswith(keep))}
    env.update(HOME=str(home), SESSIONPHONE_ROOT=str(root), DC_HOST="host.example", DC_AGENT="probe",
               DC_CALLS=calls, DC_ANSWER="auto", DC_CHANNELS_FILE=str(lst), DC_EARS="0",
               PROBE_OUT=str(out), PROBE_BLOCK=json.dumps(HEAVY if when != "never" else []),
               PROBE_WHEN="start" if when == "never" else when, PROBE_JOBS=json.dumps(list(jobs)),
               PROBE_INCOMING="1" if incoming else "0", PROBE_ACKS=str(ack_dir),
               PROBE_PREFLIGHT="on" if preflight else "off",
               PROBE_ADDR=ADDR, PROBE_CALLER=CALLER, PROBE_CHAT=str(CHAT),
               PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", PROBE], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
    assert out.exists(), f"the probe died before it recorded:\n{proc.stderr[-3000:]}"
    return Run(proc, json.loads(out.read_text()))


def assert_no_traceback(r: Run) -> None:
    text = r.everything()
    assert "Traceback" not in text and "ModuleNotFoundError" not in text, text[-4000:]


# ---- the rig -------------------------------------------------------------------


@pytest.mark.parametrize("calls", ["off", "on"])
def test_rig_the_bot_starts_over_the_fakes_with_the_pack(tmp_path, calls):
    """Nothing blocked: the probe's bot comes online and makes one outbox pass,
    calls off or on. So a failure below is the missing pack, not the rig."""
    r = probe(tmp_path, when="never", calls=calls)
    assert r.import_error is None, r.import_error
    assert r.main == "one pass", (r.main, r.error, r.stderr[-2000:])
    assert any(line.startswith(f"online as {ADDR}") for line in r.lines), r.log
    assert r.attempts == []


def test_rig_a_live_bot_dials_with_the_pack(tmp_path):
    """Nothing blocked: a call job is acked and the call reaches RINGING (an
    offer was built and placed). The live-removal test below differs only in
    the block."""
    r = probe(tmp_path, when="never", calls="on", jobs=[{"call": CHAT}])
    assert r.acks["00"] == {"ok": True, "detail": f"dialling chat {CHAT}"}, r.acks
    assert r.dialled and any("RINGING msg 501" in line for line in r.lines), r.log
    assert_no_traceback(r)


def test_rig_the_named_lazy_imports_survive_a_live_removal(tmp_path):
    """calls.py:760 (aiortc.mediastreams) and calls.py:2111 (mouthleg, which
    imports numpy) were named in triage as the lazy imports a live bot hits.
    Measured: both are in sys.modules by the time a calls-on bot is running,
    so the removal does not reach them. The pack WAS loaded when it was taken
    away, and the bot came online on it."""
    r = probe(tmp_path, when="live", calls="on")
    assert r.loaded_at_block == ["aioice", "aiortc", "av", "cryptography", "numpy"], r.loaded_at_block
    assert r.main == "one pass", (r.main, r.error)
    assert "aiortc.mediastreams" not in r.attempts and "numpy" not in r.attempts, r.attempts
    assert_no_traceback(r)      # start-up (mouthleg included) never reached for the pack


def test_a_reach_past_the_preflight_lands_on_the_backstop_without_a_traceback(tmp_path):
    """repro-001's fact: under a live bot the first call is what reaches for
    the pack, and the module it wants is cryptography's OpenSSL backend
    (RTCPeerConnection -> generateCertificate -> default_backend). The bot's
    preflight now refuses before that reach; this one turns it off, as a
    removal between the check and the reach would. The reach is still made,
    and calls.py's ImportError backstop says it in one plain line naming the
    media pack: "dial failed:", no traceback. The ack was already ok (the dial
    was queued), which is why the preflight, not this, is the refusal."""
    r = probe(tmp_path, when="live", calls="on", jobs=[{"call": CHAT}], preflight=False)
    assert "cryptography.hazmat.backends.openssl" in r.attempts, (r.attempts, r.log)
    line = next((x for x in r.lines if "dial failed:" in x), None)
    assert line is not None and "media pack is gone" in line, r.log
    assert "bot install" in line and "restart the bot" in line, line
    assert not r.dialled
    assert_no_traceback(r)


# ---- (A) F5: a chat-only bot on the light core --------------------------------


def test_a_chat_only_bot_starts_on_the_light_core(tmp_path):
    """DC_CALLS=off, the pack absent: the bot comes online, makes its pass,
    and loads none of the pack. Failed before fix-001: the import of
    sessionphone.bridge.bot died, "No module named 'numpy'" (calls.py:36).
    Its sends are acked through bridge/ack.py, not calls (rca-001 item 1)."""
    r = probe(tmp_path, when="start", calls="off", jobs=[{"chat_id": CHAT, "text": "hello"}])
    assert r.import_error is None, r.import_error
    assert r.main == "one pass", (r.main, r.error)
    assert any(line.startswith(f"online as {ADDR}") for line in r.lines), r.log
    assert r.acks["00"] == {"ok": True, "detail": f"sent to chat {CHAT}, msg 78"}, r.acks
    assert r.attempts == [], r.attempts         # calls off: nothing reached for the pack
    assert_no_traceback(r)


# ---- (B) Q3: a calls-on bot whose pack is gone --------------------------------


def test_a_calls_on_bot_with_no_pack_starts_and_says_why_calls_are_refused(tmp_path):
    """DC_CALLS on (the default), the pack absent at start: the bot does not
    die. It comes online (chat works without the pack) and its log says, in
    words, that calls need the media pack. Failed before fix-001: the same
    import traceback as the chat-only bot."""
    r = probe(tmp_path, when="start", calls="on")
    assert r.import_error is None, r.import_error
    assert r.main == "one pass", (r.main, r.error)
    assert any(line.startswith(f"online as {ADDR}") for line in r.lines), r.log
    assert "media pack" in r.log, r.log
    assert_no_traceback(r)


def test_a_call_job_with_no_pack_is_refused_plainly(tmp_path):
    """The pack absent at start, a dc-call job queued: its ack is a plain
    no that names the media pack, and nothing is dialled."""
    r = probe(tmp_path, when="start", calls="on", jobs=[{"call": CHAT}])
    assert r.import_error is None, r.import_error
    ack = r.acks["00"]
    assert ack is not None and ack["ok"] is False and "media pack" in ack["detail"], ack
    assert not r.dialled
    assert_no_traceback(r)


def test_a_call_job_after_the_pack_is_removed_under_a_live_bot_is_refused_plainly(tmp_path):
    """The bot started with the pack; it is removed; a dc-call job comes in.
    The ack is a plain no naming the media pack, and bot.log carries no
    traceback. Failed before fix-001: the ack said {"ok": true, "detail":
    "dialling chat 12"}, then bot.log said "CALL dial failed:" with a
    traceback ending "No module named 'cryptography.hazmat.backends.openssl'"."""
    r = probe(tmp_path, when="live", calls="on", jobs=[{"call": CHAT}])
    assert r.main == "one pass", (r.main, r.error)
    ack = r.acks["00"]
    assert ack is not None and ack["ok"] is False and "media pack" in ack["detail"], ack
    assert not r.dialled
    assert_no_traceback(r)


def test_an_incoming_call_after_the_pack_is_removed_under_a_live_bot_is_refused_plainly(tmp_path):
    """The bot started with the pack; it is removed; an approved caller rings.
    The call is refused in words that name the media pack, and bot.log
    carries no traceback. Failed before fix-001: "CALL session failed:" with
    the same cryptography traceback."""
    r = probe(tmp_path, when="live", calls="on", incoming=True)
    assert r.main == "one pass", (r.main, r.error)
    assert "media pack" in r.log, r.log
    assert_no_traceback(r)


# ---- around the fix: the one refusal gate, and what calls no longer carries ------


@pytest.mark.parametrize("calls", ["off", "on"])
def test_a_ring_job_is_refused_like_every_call_job(tmp_path, calls):
    """`call_to` (dc-ring) was missing from the calls-off refusal's four keys:
    on a calls-off profile it went on to calls.place() on a manager that never
    ran, and was acked "dialling". CALL_JOB_KEYS holds all five now. Calls off
    refuses as before; calls on with no pack refuses naming the pack."""
    r = probe(tmp_path, when="start", calls=calls, jobs=[{"call_to": "Pat"}])
    assert r.main == "one pass", (r.main, r.error)
    ack = r.acks["00"]
    assert ack is not None and ack["ok"] is False, ack
    if calls == "off":
        assert ack["detail"] == "refused: calls are off on this profile"
        assert any("REFUSED call job on a calls-off profile" in line for line in r.lines), r.log
    else:
        assert "media pack" in ack["detail"], ack
    assert not r.dialled
    assert_no_traceback(r)


def test_a_chat_only_bot_whose_ack_cannot_be_written_logs_it_and_goes_on(tmp_path):
    """The ack used to be written by calls.ack_write, which logged a failure
    through calls.log: hooks only calls.setup() sets, so a calls-off bot raised
    AttributeError out of its job loop. bridge/ack.py logs through the bot."""
    wall = tmp_path / "a-file"
    wall.write_text("")
    r = probe(tmp_path, when="start", calls="off",
              jobs=[{"chat_id": CHAT, "text": "hello", "ack": str(wall / "00.json")}])
    assert r.main == "one pass", (r.main, r.error)
    assert any(line.startswith(f"ack write failed for {wall / '00.json'}") for line in r.lines), r.log
    assert any(f"SENT to chat {CHAT}" in line for line in r.lines), r.log
    assert_no_traceback(r)


def test_with_no_pack_a_say_to_a_call_is_refused_and_a_plain_say_still_goes(tmp_path):
    """dc-say --call N asks calls.live_call_id(); with no calls module it is
    "no live call", not an AttributeError. A plain dc-say goes and is not
    spoken."""
    r = probe(tmp_path, when="start", calls="on",
              jobs=[{"chat_id": CHAT, "text": "hi", "call_id": 5}, {"chat_id": CHAT, "text": "hello"}])
    assert r.main == "one pass", (r.main, r.error)
    assert r.acks["00"] == {"ok": False, "detail": f"no live call in chat {CHAT}"}, r.acks
    assert r.acks["01"] == {"ok": True, "detail": f"sent to chat {CHAT}, msg 78"}, r.acks
    assert_no_traceback(r)


def test_with_no_pack_at_start_an_incoming_call_is_refused_in_words(tmp_path):
    r = probe(tmp_path, when="start", calls="on", incoming=True)
    assert r.main == "one pass", (r.main, r.error)
    line = next((x for x in r.lines if x.startswith("REFUSED incoming call")), None)
    assert line is not None and "media pack" in line and "bot install" in line, r.log
    assert not r.ended                   # not answered, as an unapproved caller is not
    assert_no_traceback(r)


def test_a_hangup_under_a_live_removal_is_not_stopped_by_the_media_gate(tmp_path):
    """A hangup starts no media, and refusing it would leave a call up, so the
    gate passes it (calls_refusal(starts=False)) and calls.hangup answers: here
    there is no call to end. The ack says that, not "media pack"."""
    r = probe(tmp_path, when="live", calls="on", jobs=[{"hangup": CHAT}])
    assert r.main == "one pass", (r.main, r.error)
    ack = r.acks["00"]
    assert ack is not None and ack["ok"] is False and f"no live call in chat {CHAT}" in ack["detail"], ack
    assert "media pack" not in ack["detail"], ack
    assert_no_traceback(r)

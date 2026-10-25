#!/usr/bin/env python3
"""The bot answers Delta Chat calls.

A Delta Chat call is a message: the caller's WebRTC offer rides in
`place_call_info`, the callee's answer goes back through
`accept_incoming_call`, and the media is the apps' business - the core only
signals (chatmail/core src/calls.rs; deltachat/calls-webapp src/lib/calls.ts).
Both the iOS app and desktop run that same webapp, so ONE peer here speaks
to both.

The wire, as the webapp does it:
  offer / answer      raw SDP strings, vanilla ICE (gathering finished before
                      the SDP is sent), bundlePolicy max-bundle, Opus.
  data channel id 1   "iceTrickling", negotiated: JSON RTCIceCandidate or null
  data channel id 3   "mutedState",   negotiated: {"videoEnabled","audioEnabled"}
  ICE servers         account.ice_servers() -> the chatmail relay's TURN/STUN

What this does with a call:
  in   the caller's audio -> 16 kHz -> energy VAD -> one wav per CHUNK (speech
       between short pauses) -> whisper (the server voicemode already runs).
       Chunks that belong to one thought are joined into a TURN, and the turn
       is what reaches the agent's MAILBOX, exactly like a chat message
       ([Call N] <caller>: ...), so the pager wakes the agent.
  out  `dc-say CHAT_ID text` while a call is live in that chat is ALSO spoken
       into the call: the agent's voice, DC_VOICE (a kokoro id, or a clone on the mlx-audio server
       voicemode uses, ref ~/.voicemode/voices/<name>) -> 48 kHz -> Opus.
       The text still lands in the chat, so the call leaves a transcript.

Runs in its own thread with its own asyncio loop; the bot's RPC calls are
sync and are pushed to the default executor.
"""
import asyncio, collections, concurrent.futures, email, email.policy, email.utils, fractions
import json, os, pathlib, re, shlex, subprocess
import threading, time, traceback, types, uuid, wave

import numpy as np
# >>> echo-patch import
from sessionphone.bridge.echo import EchoGuard, interrupts   # ours coming back; theirs cutting in
from sessionphone.noise import no_real_word   # '.', 'Music.', 'Um' never wake

# Barge-in: if anyone speaks while the agent is speaking, the bridge stops
# sending the audio, and the agent is told at what point it stopped.
#
# DEFAULT OFF, and that is the point of the flag. Barge-in DEPENDS on
# echo suppression: 34 of 39 echo chunks on the recorded call arrived while
# we were still speaking, so before the echo is being dropped this
# cuts us off on our own voice. Both ship in the same patch, so the
# flag is what keeps the order the dependency demands - echo first,
# watched working on a live call, then DC_BARGE_IN=1.
BARGE_IN = os.environ.get("DC_BARGE_IN", "0") == "1"

# ...and echo suppression catches most of our voice, not all. What
# leaks through arrives WHILE WE ARE SPEAKING - exactly the state
# barge-in watches - so a line that looks even somewhat like us is not
# an interruption. Measured on the recorded call: blocks 31 of 173 would-be
# triggers on the echoing day and 0 of 40 on the clean days, which is
# the signature of catching residual echo and nothing else.
BARGE_IN_MAX_ECHO = 0.55

# On a cut, post what he did not hear into the chat as text. A stop
# button should not also be a delete key.
BARGE_IN_KEEP_TEXT = os.environ.get("DC_BARGE_IN_KEEP_TEXT", "1") == "1"
# <<< echo-patch import
import shutil
from aiortc import (RTCPeerConnection, RTCConfiguration, RTCIceServer,
                    RTCSessionDescription, MediaStreamTrack)
from aiortc.sdp import candidate_from_sdp
from av import AudioFrame, AudioResampler

HOME = pathlib.Path.home()
from sessionphone.bridge import dc_env                 # >>> identity: who this bridge is
from sessionphone.account import read_callers         # the approved-callers file, one parser
from sessionphone import channels                      # where the bot may post, one gate
from sessionphone import keygate                       # approved callers, bound to a key
from sessionphone.bridge.heard_sink import CallSink, NullSink   # M8: the call's words into VoiceMode's heard log
from sessionphone.bridge import earleg                 # the call as a mic: his PCM on call-ears.sock
from sessionphone.bridge import ack                    # a job's yes or no
from sessionphone import media                         # a pack gone under a live bot, said plainly
from sessionphone.bridge import seatmail               # the `message` binary, for `message verify`
from sessionphone.bridge import recording              # is a call's audio kept? off by default
from sessionphone import paths                         # kit settings paths, one layout
from sessionphone import hearing                       # weights_state() and probe(): will the call hear?
from sessionphone.bridge import firstcall              # >>> first-call-wait: the line and the music
from sessionphone.bridge.floor import Floor            # a reply waits for the caller's quiet
# A second profile (the PUBLIC relay) runs this same file
# under its own launchd job: its own call dir, its profile name in the call's
# mail-thread id (chat ids are per account - chat 12 exists on both), and a
# longer STUN wait. Every default is the main profile's, unchanged.
PROFILE = dc_env.env("PROFILE", "")

# The approved callers: one `ADDR  key:FPR` line each in DC_CALLERS_FILE (default
# $DC_ROOT/callers.txt; "#" starts a comment; `sessionphone link` writes the owner
# there first). Read on every call, so an edit takes effect without a restart.
# Empty = nobody may ring. DC_CALLERS is IGNORED: an env var never
# grants the right to ring (dc_env.callers_ignored).
CALLERS_FILE = dc_env.path("CALLERS_FILE", "callers.txt")
CALLERS_HELP = f"`sessionphone callers add ADDR --key FPR` writes {CALLERS_FILE}"


def approved_callers() -> set:
    """The callers file's addresses, and nothing from the environment."""
    try:
        return set(read_callers(CALLERS_FILE))   # one parser, shared with `sessionphone callers`
    except OSError:
        return set()


# >>> channels-gate
# The in-call posts - mail into the call's chat (MailLeg), the barge-in
# remainder and the transcript - send outside the outbox, so each one asks
# channels.check() itself, re-reading channels.txt every time. A refusal is LOGGED,
# never raised: there is no tool waiting to exit non-zero. Only the chat text
# is gated; the speech into the call already passed a gate (the call's own).
def post_to_chat(account, chat_id, text, what):
    """send_text TEXT to CHAT_ID only if the list lets it through. -> True if sent.

    WHAT names the path in the refusal log. A send error propagates, as before."""
    v = channels.check(account, chat_id, log=log)
    if not v:
        log(f"REFUSED {what} to chat {chat_id}: {v.reason}; nothing was posted")
        return False
    account.get_chat_by_id(chat_id).send_text(text)
    return True
# <<< channels-gate


# >>> recording-notice: the longest the greeting waits on the chat line's result,
# or on the notice sent as text, when no other notice reached the caller. A send
# still running at the bound may land; the log says "not confirmed".
REC_NOTICE_TEXT_WAIT = 5.0
# <<< recording-notice


def caller_address(account, msg_id):
    """The address that placed the call, lower-cased; None if it cannot be read (then: refused)."""
    try:
        snap = account.get_message_by_id(msg_id).get_snapshot()
        return (snap.sender.get_snapshot().address or "").strip().lower() or None
    except Exception:
        log("incoming: could not read the caller - refusing:\n" + traceback.format_exc())
        return None
CALLS_DIR = dc_env.path("CALLS_DIR", "calls")  # one dir per call: text always, audio only while recording is on
# >>> call-id
# THE LIVE CALL, ON DISK. The bot holds the account lock, so a shell cannot
# ask it anything; this file is how dc-say finds out whether the call it
# means is still up, before it queues a word. Absent = no call.
LIVE_FILE = dc_env.LIVE_FILE   # DC_LIVE, else <home>/call-live.json
# a bot being stopped (SIGTERM) ends its calls within this many seconds: well
# inside launchd's grace (ExitTimeOut, 20 s by default) before its SIGKILL
STOP_S = 8.0


def ack_write(path, ok, detail=""):
    """The bot's yes or no, for a dc-say that is waiting on an answer. It lives
    in bridge/ack.py, which a chat-only bot imports without this module;
    this name stays for its callers."""
    ack.write(path, ok, detail, log=log)
# <<< call-id
# THE 33 s WALL: aioice's consent check is
# ONE try with a RETRY_RTO (0.5 s) window; the public relay's TURN server is
# ~360 ms away, so Mac > relay > phone > relay > Mac never fits and six
# misses close a call whose audio is flowing. A test call proved 2 s end to end
# (3 min 12). Consent stays ON. Shared with the setup checks: a call that
# cannot connect gives up more slowly.
import aioice.stun
aioice.stun.RETRY_RTO = dc_env.STUN_RTO   # 2.0 unless DC_STUN_RTO
# TTS, OpenAI-compatible /v1/audio/speech. A kokoro id (af_sky) goes to
# DC_KOKORO_URL with DC_KOKORO_MODEL; a clone dir under ~/.voicemode/voices
# goes to DC_TTS_URL with DC_TTS_MODEL (mlx-audio, clone-capable). Each bot
# reads its own, so each can pick its TTS by a setting. Both default to
# mlx-audio on :8890, VoiceMode's TTS. kokoro-fastapi is retired here: for it,
# set DC_KOKORO_URL to its :8880 and DC_KOKORO_MODEL=kokoro.
VOICES_ROOT = pathlib.Path(dc_env.env("VOICES_ROOT") or HOME / ".voicemode/voices").expanduser()
VOICE_DIR = VOICES_ROOT / dc_env.VOICE
TTS_URL = dc_env.env("TTS_URL", "http://127.0.0.1:8890/v1/audio/speech")
TTS_MODEL = dc_env.env("TTS_MODEL", "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16")
KOKORO_URL = dc_env.env("KOKORO_URL", "http://127.0.0.1:8890/v1/audio/speech")
KOKORO_MODEL = dc_env.env("KOKORO_MODEL", "mlx-community/Kokoro-82M-bf16")
KOKORO_ID = re.compile(r"^[a-z]{2}_[a-z0-9]+$")
OUT_RATE = 48000                                    # what Opus wants
PTIME = 0.020                                       # 20 ms frames
OUT_SAMPLES = int(OUT_RATE * PTIME)
IN_RATE = 16000                                     # what whisper wants
IN_SAMPLES = int(IN_RATE * PTIME)
# The far party's audio in a call dir is named for the role, not a person:
# caller-NNN.wav per chunk, caller-call.wav for the whole call,
# caller-call.json for its clock. Calls before this were mike-*; nothing in
# this repo reads them by name (dc-call-mix reads full-in/full-out.wav).
# Which of them outlive the call is the recording switch's to say
# (bridge/recording.py): with it off (the default) none do.
CALLER_CHUNK = "caller-{n:03d}.wav"
CALLER_WAV = "caller-call.wav"
CALLER_JSON = "caller-call.json"
# Endpointing: telling when the caller has finished speaking. A CHUNK is speech between
# short pauses and goes to whisper the moment it closes; a TURN is what the agent is
# TOLD about, and it is every chunk the caller ran together. Before the split, one
# 30 s guillotine cut the caller mid-word ("Okay, and are you doing a tri-") and each
# cough started its own mail, so one thought reached the agent in three pieces.
CHUNK_QUIET = 0.35          # quiet that closes a chunk -> whisper starts now
CHUNK_MAX = 12.0            # a chunk this long is cut at its quietest frame
TURN_JOIN = 1.60            # more speech this soon -> the same turn.
                            # MEASURED on a 12 min call: the caller's
                            # within-thought gaps are all <= 0.98 s and the
                            # real turn gaps start at 2.46 s - an empty
                            # valley between, so anything in 1.1-2.3 s is
                            # equivalent and the middle is the safe pick
TURN_JOIN_OPEN = 2.20       # ... when the last chunk ended mid-thought
LEAD_IN = 0.30              # audio kept from before speech was detected
MIN_SPEECH = 0.30           # less real speech than this is not a chunk
GREETING = dc_env.GREETING
GREETING_OUT = dc_env.GREETING_OUT
# >>> ring-first: a call can ring the agent without answering, and the agent
# answers if it chooses; auto answer stays an option, because it is cheap.
# DC_ANSWER=auto (the default) answers at once, as before. DC_ANSWER=ring
# mails the agent RINGING, waits RING_WAIT s for dc-answer, else voicemail.
RING = dc_env.ANSWER == "ring"
RING_WAIT = dc_env.RING_WAIT
# >>> no-reply: the no-reply watchdog's window (0 = off) and its line
NO_REPLY_S = dc_env.NO_REPLY_S
HOLDING_LINE = dc_env.HOLDING_LINE
# <<< no-reply
FLOOR_GAP, FLOOR_CAP = dc_env.FLOOR_GAP, dc_env.FLOOR_CAP   # floor control (bridge/floor.py)
# >>> undelivered: said on the call once for each turn whose mail did not go.
# It promises no retry: the spool retries a kept turn, but a
# kind that never heals goes to dead/.
UNDELIVERED_LINE = "I couldn't reach the agent."
# <<< undelivered
GREETING_VOICEMAIL = dc_env.GREETING_VOICEMAIL


def settle_greetings(display):
    """The greetings above are copied when calls is imported, before the
    bot's start has settled the account's display name. Bind them again from
    DISPLAY (dc_env.settle_greetings), so a call greets by the name people
    see in their chat list."""
    global GREETING, GREETING_OUT, GREETING_VOICEMAIL
    dc_env.settle_greetings(display)
    GREETING, GREETING_OUT, GREETING_VOICEMAIL = (
        dc_env.GREETING, dc_env.GREETING_OUT, dc_env.GREETING_VOICEMAIL)


# The RINGING mail tells its reader what to run, and the package puts
# no dc-* tool on any PATH (pyproject installs only `sessionphone`). The bridge
# always runs from its checkout, so name the checkout's own bin/ tool by its
# absolute path; a bare name is the fallback only when that file is absent.
TOOL_BIN = pathlib.Path(__file__).resolve().parents[3] / "bin"


def tool_cmd(name, profile=PROFILE, bin_dir=None):
    """The shell line that runs bin/NAME: absolute and shell-quoted when the
    checkout has it executable, else the bare NAME; plus --profile PROFILE."""
    tool = (TOOL_BIN if bin_dir is None else pathlib.Path(bin_dir)) / name
    head = shlex.quote(str(tool)) if tool.is_file() and os.access(tool, os.X_OK) else name
    return head + (f" --profile {shlex.quote(profile)}" if profile else "")


ANSWER_CMD = tool_cmd("dc-answer")
HANGUP_CMD = tool_cmd("dc-hangup")
# <<< ring-first
POST_TRANSCRIPT = False    # not built yet
# PREVIEWS give the agent time to look things up before it answers, so the
# reply reads as if from memory. While a turn is still open,
# the turn-so-far is mailed as a PREVIEW: think on it, never answer it. Each
# supersedes the last; the turn supersedes the final preview. Policy and
# numbers: previews.py (FIRST_AFTER 3 s, EVERY 5 s, MIN_WORDS 8).
PREVIEWS = dc_env.env("PREVIEWS", "0") == "1"   # OFF until agents have a PREVIEW rule
HOLD_TURN = dc_env.env("HOLD_TURN", "1") != "0"   # see CallSession._feed
# EARS: the call leg listens through ears (in-process) instead of energy chunks
# -> whisper: Apple SpeechTranscriber (Nemotron / whisper elsewhere), Silero,
# and ears' turn stage, which holds the turn while the caller talks and
# COMMITS the open phrase before the turn goes out. OFF until the live-mic
# test; DC_EARS=1 turns it on.
EARS = dc_env.env("EARS", "0") == "1"
EARS_BACKEND = dc_env.env("EARS_BACKEND", "auto")     # apple | nemotron | whisper | auto
EARS_TURNS = dc_env.env("EARS_TURNS", "silence")      # silence | smart (ears' Smart Turn)
EARS_SRC = dc_env.env("EARS_SRC")     # an ears checkout's src/, for development; else the package
# ears is OPTIONAL: not a declared dependency. The turn-so-far policy lives there, in
# one copy. Without ears the bridge still runs chat and calls, with no
# previews. setup() logs that once, and only when DC_PREVIEWS=1 or DC_EARS=1
# asked for something ears provides: with both off, as by default, a missing
# ears costs nothing, and "ears is not installed" was read as "no hearing".
# The call's ears proper are a separate process on call-ears.sock
# (earleg.py), not this import.
import sys
if EARS_SRC and EARS_SRC not in sys.path:
    sys.path.insert(0, EARS_SRC)
try:
    from ears.previews import Previews
    EARS_MISSING = None
except ImportError as _e:               # no ears: a policy that never previews
    EARS_MISSING = str(_e)

    class Previews:
        def on_phrase(self, text, now):
            return None

        def on_turn(self, text, now):
            return None

        def tick(self, now):
            return None
# The INBOUND leg: a new call is a new mail thread, and the bridge voices
# whatever is mailed on that thread. The bridge reads its OWN box, never
# the agent's seat box - reading it would claim mail out from under the agent's pager,
# which is the bug that retired `session mail arm`.
BOT_BOX = pathlib.Path(dc_env.env("BOT_BOX") or HOME / ".mail/agents" / f"{dc_env.AGENT}-chat").expanduser()
VOICE_MAP_FILE = dc_env.path("VOICES", "voices.json")
GUESTS_FILE = dc_env.path("GUESTS", "call-guests.txt")
DEFAULT_VOICE_MAP = {dc_env.AGENT: dc_env.VOICE}   # the agent's own mail, in its own voice
UNKNOWN_VOICE = "am_adam"   # a plain voice: an unknown sender is ANNOUNCED as
                            # unknown, never quietly given someone else's voice
# The longest mail SPOKEN into a call, marker included. A mail reply holds the
# call's track as one line: 1200 characters is about 90 s at the echo
# canceller's measured CPS, and it bounds the longest single TTS request (a mail
# with no sentence punctuation is one sentence). Only the speech is cut, at a
# sentence or word end, and it closes with SPOKEN_MORE; the chat copy is whole.
MAIL_SPEAK_MAX = 1200
SPOKEN_MORE = "The rest is in the chat."
# launchd's PATH has no Homebrew; the bot runs under launchd
FFMPEG = (dc_env.env("FFMPEG") or shutil.which("ffmpeg")
          or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                   if os.path.exists(p)), "ffmpeg"))

# set by setup(): the bot's own log / transcribe / mail_in
_hooks = None


class Hooks:
    def __init__(self, log, transcribe, mail_in):
        self.log, self.transcribe, self.mail_in = log, transcribe, mail_in


def ears_missing_line():
    """The one start-up line about a missing ears, or None.

    None when ears imported, or when neither DC_PREVIEWS=1 nor DC_EARS=1 asked
    for what it provides. Otherwise the line names the asked-for feature that
    is off. With previews alone it says hearing is unaffected, which is true:
    the call is still heard and transcribed. With DC_EARS=1 it says the
    opposite, because that is true too: the in-process leg cannot start
    without ears (_listen_ears), so the call is not heard until DC_EARS is
    unset or ears is installed."""
    if not EARS_MISSING or not (PREVIEWS or EARS):
        return None
    off = [what for on, what in ((PREVIEWS, "turn previews (DC_PREVIEWS=1)"),
                                 (EARS, "the in-process ears leg (DC_EARS=1)")) if on]
    line = (f"ears is not installed ({EARS_MISSING}), so {' and '.join(off)} "
            f"{'is' if len(off) == 1 else 'are'} off. ")
    if EARS:
        line += ("Calls are NOT heard through the ears leg: unset DC_EARS for the "
                 "default hearing, or put ears in the bot's environment (or DC_EARS_SRC).")
    else:
        line += ("Hearing and transcription are unaffected. Put ears in the bot's "
                 "environment (or DC_EARS_SRC) for previews.")
    return line


def setup(log, transcribe, mail_in, record_cli=None):
    """record_cli: the recording switch's CLI layer, from the bot's own argv
    (True for --record, False for --no-record, None for neither)."""
    global _hooks, _switch
    _hooks = Hooks(log, transcribe, mail_in)
    line = ears_missing_line()
    if line:
        log(line)
    CALLS_DIR.mkdir(parents=True, exist_ok=True)
    # >>> recording-switch: said once at start, then read again at every call
    _switch = recording.Switch(record_cli, log=_rec_log)
    _rec_log(f"{recording.line(_switch)}; read again at each call")
    recording.sweep(CALLS_DIR, log=_rec_log)
    _rec_prune(force=True)
    # <<< recording-switch
    _manager.start()


# >>> recording-switch
# THE ONE RECORDING SWITCH (the ears' own, re-read here: ears need not be
# installed). _switch is the bot's, made by setup() with its CLI flag; before
# setup (a test, the web page) each read is a fresh one over the env and the file.
_switch = None
_last_prune = None


def rec_switch():
    return _switch if _switch is not None else recording.Switch(None, log=_rec_log)


def _rec_log(msg):
    try:
        log(f"recording: {msg}")
    except Exception:
        pass


def _rec_prune(force=False):
    """The retention prune: at start, then at most every PRUNE_EVERY, at a call's end.
    Only while recording is on and record_days > 0, and only audio a call kept
    under the switch (named in its recording.json); never a dir made before it."""
    global _last_prune
    now = time.time()
    if not force and _last_prune is not None and now - _last_prune < recording.PRUNE_EVERY:
        return
    _last_prune = now
    try:
        sw = rec_switch()
        recording.prune(CALLS_DIR, sw.record(), sw.days().value, log=_rec_log)
    except Exception as e:
        _rec_log(f"prune failed: {e.__class__.__name__}: {e}")
# <<< recording-switch


# >>> call-id-log
# The live call's id, so every CALL line can say which call it belongs to.
# Set where the LIVE file is written, cleared where it is cleared. 99.3% of
# this log could not be attributed to a call before this (10,076 of 10,146).
_call_id = None


def set_call_id(n):
    global _call_id
    _call_id = n


def log(msg):
    # The tag goes at the END: dc-log parses this text with regexes, and
    # every existing `grep "CALL recording_closed"` has to keep working.
    tag = f" [call {_call_id}]" if _call_id else ""
    _hooks.log(f"CALL {msg}{tag}")
# <<< call-id-log


# >>> turn-log
_background = set()


def _keep(task):
    """Hold a fire-and-forget task until it is done: the loop keeps only a
    weak reference, and a task nobody holds can be collected mid-flight."""
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def reply_seen(loop, s, via):
    """From a thread that is not the calls loop (the outbox, the mail leg):
    tell session `s` a reply arrived. Scheduled with call_soon_threadsafe
    BEFORE the reply's speak coroutine, so the loop's FIFO runs it first.
    Bookkeeping must never stop a reply from being spoken: an error is
    logged by type and dropped."""
    fn = getattr(s, "replied", None)
    if fn is None:
        return
    try:
        loop.call_soon_threadsafe(fn, via)
    except Exception as e:  # noqa: BLE001 - the reply is spoken whatever this does
        log(f"reply not counted (via={via}): {type(e).__name__}")
# <<< turn-log


# >>> ice-redact
# `ice servers:` used to dump account.ice_servers() as JSON, and that carries
# the relay's TURN username and credential. Now: the URL count, and each URL's
# scheme and host. The host is the RELAY's (public), never the caller's.
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*\Z", re.IGNORECASE)


def _scheme_host(url):
    """`turn:relay.example.org:3478?transport=udp` -> `turn:relay.example.org`.
    Port, query and anything before an `@` are dropped; what cannot be read
    is `?`, never echoed."""
    scheme, sep, rest = str(url).partition(":")
    if not sep or not _SCHEME.match(scheme):
        return "?:?"
    rest = rest.split("?", 1)[0].lstrip("/").rsplit("@", 1)[-1]
    if rest.startswith("["):                 # bracketed IPv6: the host is the whole bracket
        end = rest.find("]")
        host = rest[:end + 1] if end > 0 else ""
    else:
        host = rest.split(":", 1)[0]
    return f"{scheme.lower()}:{host or '?'}"


def ice_servers_summary(ice):
    """The `ice servers:` log line for account.ice_servers(): never raises.
    The count is of the URLs the relay sent, before _build_pc drops the
    bracketed-IPv6 ones aiortc cannot parse."""
    try:
        urls = []
        for server in ice or []:
            u = server.get("urls") if isinstance(server, dict) else None
            urls += [u] if isinstance(u, str) else list(u or [])
        line = f"ice servers: {len(urls)} url(s)"
        return line + (": " + " ".join(_scheme_host(u) for u in urls) if urls else "")
    except Exception as e:  # noqa: BLE001 - a log line must never break a call
        return f"ice servers: unreadable ({type(e).__name__})"
# <<< ice-redact


# >>> ice-log
# A call that never connects said only `connection failed`. These lines say
# whether ICE gathered anything, how the checks ended, and over which kind of
# path. STATE WORDS, CANDIDATE TYPES AND COUNTS ONLY: never an address, a
# port or a credential: nothing beyond what `connection <state>` logs.
ICE_TYPES = ("host", "srflx", "relay")      # always printed; any other type only when seen


def _ice_transports(pc):
    """The peer connection's distinct ICE transports: each transceiver's, and
    the data channels' (max-bundle makes these one transport, so dedupe)."""
    out, seen = [], set()
    dtls = [getattr(getattr(t, "receiver", None), "transport", None)
            for t in pc.getTransceivers()]
    sctp = getattr(pc, "sctp", None)
    if sctp is not None:
        dtls.append(getattr(sctp, "transport", None))
    for d in dtls:
        ice = getattr(d, "transport", None)
        if ice is not None and id(ice) not in seen:
            seen.add(id(ice))
            out.append(ice)
    return out


def ice_counts(candidates):
    """`host=<n> srflx=<n> relay=<n>` by candidate TYPE: nothing else of a candidate."""
    n = collections.Counter(getattr(c, "type", "?") for c in candidates)
    keys = [*ICE_TYPES, *sorted(k for k in n if k not in ICE_TYPES)]
    return " ".join(f"{k}={n.get(k, 0)}" for k in keys)


def ice_pair(pc):
    """(local_type, remote_type) of the selected candidate pair, or None if
    no pair was nominated. aiortc has no public API for this: the pair is in
    aioice's PRIVATE `Connection._nominated` ({component: CandidatePair}),
    read here only because pyproject pins aioice==0.10.2 (ice.py:424). A pin
    bump must re-check it. May raise: callers log the type, never crash."""
    for ice in _ice_transports(pc):
        nominated = ice._connection._nominated
        pair = nominated.get(1) or next(iter(nominated.values()), None)
        if pair is not None:
            return pair.local_candidate.type, pair.remote_candidate.type
    return None


def _ice_pair_line(pc, failed):
    try:
        pair = ice_pair(pc)
        if pair is not None:
            return f"ice pair local={pair[0]} remote={pair[1]}"
        if not failed:
            return None
        remote = [c for t in _ice_transports(pc) for c in t.getRemoteCandidates()]
        return f"ice pair none: no candidate pair succeeded (remote {ice_counts(remote)})"
    except Exception as e:  # noqa: BLE001 - a log line must never break a call
        return f"ice pair unknown: {type(e).__name__}"


def watch_ice(pc):
    """Log the peer connection's ICE gathering and connection states.
    Registered in _build_pc, before setLocalDescription, so gathering is seen."""
    @pc.on("icegatheringstatechange")
    def on_gathering():
        state = pc.iceGatheringState
        if state != "complete":
            log(f"ice gathering {state}")
            return
        try:
            local = [c for t in _ice_transports(pc) for c in t.iceGatherer.getLocalCandidates()]
            log(f"ice gathering complete: local {ice_counts(local)}")
        except Exception as e:  # noqa: BLE001
            log(f"ice gathering complete: local unknown ({type(e).__name__})")

    @pc.on("iceconnectionstatechange")
    def on_ice_state():
        state = pc.iceConnectionState
        log(f"ice state {state}")
        if state in ("completed", "failed"):
            line = _ice_pair_line(pc, failed=state == "failed")
            if line:
                log(line)
    return pc
# <<< ice-log


# ---------------------------------------------------------------- voice out

# DC_TTS: server (the URLs above), apple (macOS say, on-device), or auto:
# the server when it accepts a connection, else apple on a Mac. A bare Mac
# has no speech server and no ffmpeg; say renders 48 kHz s16 itself.
TTS_MODE = (dc_env.env("TTS", "auto") or "auto").strip().lower()
SAY_BIN = "/usr/bin/say"
# >>> first-call-wait: a line, then music, while the speech models download
FIRST_CALL_WAIT = dc_env.FIRST_CALL_WAIT
WAIT_MUSIC = dc_env.WAIT_MUSIC


def _answers(url, timeout=0.3):
    """Does anything accept a TCP connection at url's host:port? No request is sent."""
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def apple_pcm48(text, voice=None):
    """Text -> int16 mono 48 kHz, by macOS say. The text goes in on stdin, so a
    line starting with '-' is spoken, not parsed as an option. DC_APPLE_VOICE
    names an Apple voice; else the system voice (a kokoro id means nothing here)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="dc-say-") as d:
        out = os.path.join(d, "line.wav")
        cmd = [SAY_BIN, "-f", "-", "-o", out, f"--data-format=LEI16@{OUT_RATE}"]
        if dc_env.env("APPLE_VOICE"):
            cmd += ["-v", dc_env.env("APPLE_VOICE")]
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"say rc={r.returncode}: {(r.stderr or '')[:200]!r}")
        with wave.open(out, "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


def tts_pcm48(text, voice=None):
    """Text -> int16 mono 48 kHz. `voice` is a clone dir under
    ~/.voicemode/voices on the mlx-audio server, or a kokoro id (af_sky) on
    the kokoro server; with no server on a Mac, macOS say (DC_TTS). Blocking.
    A proxied voice speaks another agent's words in that agent's voice."""
    server = KOKORO_URL if KOKORO_ID.match(voice or "") else TTS_URL
    if TTS_MODE == "apple":
        return apple_pcm48(text, voice)
    if TTS_MODE == "auto" and os.path.exists(SAY_BIN) and not _answers(server):
        # Never silently: the caller hears a different voice (a call
        # was greeted in the Apple voice with :8880 stopped).
        log(f"TTS SERVER DOWN: {server} does not answer; "
            f"speaking in the Apple voice, not {voice or dc_env.VOICE}")
        return apple_pcm48(text, voice)
    if KOKORO_ID.match(voice or ""):
        url = KOKORO_URL
        body = {"model": KOKORO_MODEL, "input": text, "voice": voice, "response_format": "wav"}
    else:
        vdir = VOICES_ROOT / (voice or dc_env.VOICE)
        ref_txt = vdir / "default.txt"
        url = TTS_URL
        body = {
            "model": TTS_MODEL, "input": text, "voice": voice or dc_env.VOICE,
            "response_format": "wav",
            "ref_audio": str(vdir / "default.wav"),
            "ref_text": ref_txt.read_text().strip() if ref_txt.exists() else "",
            "lang_code": "auto",
        }
    # >>> tts-retry
    # A blip is not an outage: measured rc=7 twice in one second, with
    # both servers healthy minutes later.
    last = None
    for attempt in (1, 2):
        r = subprocess.run(
            ["curl", "-s", "-m", "120", url, "-H", "Content-Type: application/json",
             "-d", json.dumps(body)], capture_output=True, timeout=130)
        if r.returncode == 0 and len(r.stdout) >= 100:
            if attempt == 2:
                log("tts recovered on retry")
            break
        last = f"tts curl rc={r.returncode} bytes={len(r.stdout)}"
        if attempt == 1:
            log(f"tts blip, retrying: {last}")
            time.sleep(0.4)
    else:
        raise RuntimeError(last or "tts failed twice")
    # <<< tts-retry
    ff = subprocess.run(
        [FFMPEG, "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1",
         "-ar", str(OUT_RATE), "pipe:1"], input=r.stdout, capture_output=True,
        timeout=60)
    if ff.returncode != 0:
        raise RuntimeError(f"ffmpeg rc={ff.returncode}: {ff.stderr[:200]!r}")
    return np.frombuffer(ff.stdout, dtype=np.int16)


NON_SPEECH = re.compile(r"^\s*[\[\(\*][^\]\)\*]*[\]\)\*]\s*$")


def not_speech(text):
    """whisper's tags for what is not words: [COUGH], (coughing), [BLANK_AUDIO],
    *music*. An early call had five of these paging the agent."""
    t = (text or "").strip()
    return (not t) or bool(NON_SPEECH.match(t)) or sum(c.isalpha() for c in t) < 2


OPEN_END = re.compile(
    r"(?:\b(?:and|or|but|so|if|then|the|a|an|to|of|that|with|because|when|"
    r"which|for|is|are|was|were|it'?s|i|we|you|they|my|your|about|like|as|at|"
    r"in|on|by|from|would|could|should|can|will|do|does|did|there|this|these|"
    r"those|what|how|why|um+|uh+)\b|[,;:\-])\s*$", re.I)


FILLER_ONLY = re.compile(
    r"^(?:[-\s>]*(?:um+|uh+|er+|ah+|hmm+|mm+|well|okay|ok|so|right|yeah|"
    r"let'?s see|i mean|you know)[\s,.!?-]*)+$", re.I)


def mid_thought(text):
    """Did he stop mid-sentence? Then his turn is not over. A truncation comes
    back from whisper with no full stop ("are you doing a tri-"), or hanging on
    an open word ("if we could, if I") - both earn the longer join window."""
    t = (text or "").strip().rstrip("\"\u201d'")
    if not t:
        return False
    if t.endswith("...") or t.endswith("\u2026"):
        return True          # whisper's own "trailing off" (seen on a live call)
    if FILLER_ONLY.match(t):
        return True          # "Um, well." is him THINKING, not him finished
                             # (seen on a live call, as was "Let's see,")
    return t[-1] not in ".!?" or bool(OPEN_END.search(t))


def flat(text):
    """Heard words on one line: whisper's line breaks and runs of spaces become
    one space, and nothing is cut. Every path that carries heard words to an
    agent (the turn mail, the preview mail, the heard log's turn and partials)
    uses this, never one_line: what the caller said reaches the agent whole,
    however long the turn."""
    return " ".join((text or "").split())


def one_line(text, n=400):
    """whisper answers with line breaks in it. Flatten before logging: a log
    line cut at 80 chars mid-sentence reads exactly like the SPEAKER being cut
    off mid-sentence, and an agent once read its own log that
    way and reported a truncation bug that was not there.

    Display only: it cuts at N and adds "...". Never use it on text an agent
    acts on; use flat()."""
    t = flat(text)
    return t if len(t) <= n else t[:n] + "..."


def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [p for p in parts if p.strip()]



# >>> recording-patch
class Recorder:
    """Continuous audio for one call, both directions.

    Chunked-on-detection audio cannot evidence silence: without a tape,
    silence is a claim, not evidence. The chunks stay; this is
    the tape underneath them.
    """

    NAMES = ("full-in.wav", "full-out.wav")

    def __init__(self, call_dir):
        self.dir = call_dir
        self._in = self._open(call_dir / "full-in.wav", IN_RATE)
        self._out = self._open(call_dir / "full-out.wav", OUT_RATE)
        self._failed = set()      # the files whose write failure is already logged
        self.t0 = time.time()

    @staticmethod
    def _open(path, rate):
        try:
            w = wave.open(str(path), "wb")
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            return w
        except Exception:
            log(f"recorder: could not open {path.name}")
            return None

    @staticmethod
    def _pcm_bytes(pcm):
        """16-bit PCM as bytes. The ears path hands over bytes, the chunker
        path and the outgoing track an ndarray; both are accepted."""
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            return bytes(pcm)
        return np.asarray(pcm, dtype=np.int16).tobytes()

    def _write(self, w, name, pcm):
        if w is None or not len(pcm):
            return
        try:
            w.writeframes(self._pcm_bytes(pcm))
        except Exception as e:
            # A tape must never break a call, but a failure is logged, once
            # per file, so an empty tape has a reason in the log.
            if name in self._failed:
                return
            self._failed.add(name)
            try:
                log(f"recorder: could not write {name}: {e.__class__.__name__}: {e}")
            except Exception:
                pass

    def write_in(self, pcm16):
        self._write(self._in, "full-in.wav", pcm16)

    def write_out(self, pcm48):
        self._write(self._out, "full-out.wav", pcm48)

    def close(self):
        for w, name in ((self._in, "full-in.wav"), (self._out, "full-out.wav")):
            if w is None:
                continue
            try:
                secs = w.getnframes() / float(w.getframerate() or 1)
                w.close()
                log(f"recording_closed {name} {secs:.1f}s")
            except Exception:
                pass
        self._in = self._out = None


_recorder = None          # the live call's Recorder, or None


def set_recorder(rec):
    global _recorder
    _recorder = rec
# <<< recording-patch

def _line_log(msg):
    """The track also serves web calls, which install no hooks: log only when the bot did."""
    if _hooks is not None:
        log(msg)


class VoiceLine:
    """One line on a PipVoiceTrack: a mouth line (header to D) or a bot reply
    (one speak() call). Open one with PipVoiceTrack.line().

    A line owns the track from its turn until it ends. Until its turn it
    waits, and what it says waits with it, in its own buffer; at its turn the
    buffer goes onto the track in one step. So a line is never spliced into
    another, and lines play in the order they arrived. No writer ever waits:
    say() and end() return at once, so a waiting mouth still reads its own C
    or EOF, and a waiting bot reply still renders.

    state: "waiting", "owning", "done" (ended and released) or "cut".

    floor: a REPLY line (a dc-say, a mail into the call, a mouth line). It
    does not start while the caller is speaking (bridge/floor.py): at the
    head of the queue, with the track idle, it waits for the caller's quiet,
    or the cap. The bridge's own lines are opened without it."""

    def __init__(self, track, who="", floor=False):
        self.track, self.who = track, who
        self.floor = floor
        self.held_at = None        # when the floor first held it (floor clock)
        self.state = "waiting"
        self.buf = []              # (pcm, on_queue) said while waiting
        self.complete = False      # end() was called while waiting
        self.said = False          # it has said a piece (buffered or not)
        self._on_over = None

    @property
    def live(self):
        """Owning or waiting: a line that can still reach the call."""
        return self.state in ("waiting", "owning")

    @property
    def was_cut(self):
        return self.state == "cut"

    def say(self, pcm, on_queue=None):
        """Queue PCM on this line. on_queue(start_at, seconds) is called when it
        reaches the track, with the wall-clock time it will start to play.
        -> False when the line is over (cut or ended) and the PCM was dropped."""
        if not self.said and self.live:
            self.said = True
            if self.state == "waiting":
                self.track.said_n += 1     # a waiting line is talking too (firstcall reads said_n)
        if self.state == "owning":
            self.track._append(pcm, on_queue)
            return True
        if self.state == "waiting":
            self.buf.append((pcm, on_queue))
            return True
        return False

    def end(self, on_over=None):
        """All said: the next line may have the track once this one's audio is
        on it. on_over() is called once, when the line is over: its audio all
        on the track, or cut (see was_cut). On a line that is over already
        (cut, or ended before) it only calls on_over, at once."""
        self._on_over = on_over
        if self.state == "owning":
            self.track._release(self)
        elif self.state == "waiting":
            self.complete = True
        else:
            self._over(self.state)

    def _over(self, state):
        self.state = state
        cb, self._on_over = self._on_over, None
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001 - one writer's bookkeeping must not stop another's line
                _line_log("voice line: on_over failed:\n" + traceback.format_exc())

    def cut(self):
        """A cut of the WHOLE track (PipVoiceTrack.cut), but only while this line
        is live. -> the seconds dropped, or None when the line was already over
        (a cut that already dropped it must not cut the next line too)."""
        if not self.live:
            return None
        return self.track.cut()


class PipVoiceTrack(MediaStreamTrack):
    """Outgoing audio: whatever is queued, else silence, paced at 20 ms.

    One line at a time: a writer opens a VoiceLine (line()), and the line
    that arrived first owns the track until it ends; the others wait in
    arrival order and are never spliced in. A cut drops them all."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.queue = collections.deque()      # int16 arrays at 48 kHz
        self.pending = np.zeros(0, dtype=np.int16)
        self.overlay = np.zeros(0, dtype=np.int32)   # sounds, mixed OVER whatever plays
        self.bed = None                       # int16 looped while nothing is queued (hold)
        self._bed_at = 0
        self.said_n = 0                       # lines queued by say(), ever
        self.owner = None                     # the VoiceLine that has the track
        self.waiting = collections.deque()    # VoiceLines behind it, in arrival order
        self.floor = None                     # bridge/floor.py's Floor: the call sets it; None = no gate
        self._start = None
        self._timestamp = 0

    def say(self, pcm):
        """Queue PCM with no line: straight onto the track. A held bed ends
        here, for good: see hold()."""
        self.queue.append(pcm)
        self.said_n += 1
        self.bed = None

    # ---- one line at a time

    def line(self, who="", floor=False):
        """Open a line. Its arrival is now: it owns the track at once if
        nothing does, else it waits behind the lines that arrived before it.
        floor: a reply line, held while the caller speaks (see _may_start)."""
        ln = VoiceLine(self, who, floor)
        self.waiting.append(ln)
        self._grant()
        return ln

    def queued_s(self):
        """Seconds of audio on the track still to play."""
        return (sum(len(a) for a in self.queue) + len(self.pending)) / OUT_RATE

    def _append(self, pcm, on_queue=None):
        start_at = time.time() + self.queued_s()
        self.say(pcm)
        if on_queue is not None:
            try:
                on_queue(start_at, len(pcm) / OUT_RATE)
            except Exception:  # noqa: BLE001 - one writer's bookkeeping must not stop another's line
                _line_log("voice line: on_queue failed:\n" + traceback.format_exc())

    def _release(self, ln):
        if self.owner is ln:
            self.owner = None
        ln._over("done")
        self._grant()

    def _grant(self):
        """Hand the track to the next waiting line, its buffer onto the queue in
        one step. A line that already ended passes the track straight on. A
        reply line the floor holds stays at the head, and every line behind it
        waits too: the order they arrived in is kept."""
        while self.owner is None and self.waiting:
            if not self._may_start(self.waiting[0]):
                return
            ln = self.waiting.popleft()
            self.owner, ln.state = ln, "owning"
            buf, ln.buf = ln.buf, []
            for pcm, on_queue in buf:
                self._append(pcm, on_queue)
            if ln.complete:
                self.owner = None
                ln._over("done")

    # ---- floor control (bridge/floor.py)

    def _may_start(self, ln):
        """May LN, at the head of the queue, take the track now? A reply line
        STARTS only when the track has nothing left to play and the caller has
        been quiet for the floor's gap, or once the cap has held it. Its hold
        is counted from the moment it could otherwise have started, and logged
        once, as it is released. Every other line, a track with no floor (a web
        call) and floor control off start as before."""
        f = self.floor
        if f is None or not ln.floor or not f.on or (ln.complete and not ln.buf):
            return True          # (an ended line with nothing to say passes the track on)
        if self.speaking():
            return False         # the line before it is still playing: it has not started
        now = f.tick()
        why = f.verdict(ln.held_at, now)
        if why is None:
            if ln.held_at is None:
                ln.held_at = now
            return False
        if ln.held_at is not None:
            _line_log(f"FLOOR {ln.who or 'reply'} line held {now - ln.held_at:.1f}s, "
                      f"released by {why}")
        return True

    def floor_tick(self):
        """Once a frame: the floor reads the VAD, and a held line starts as soon
        as it may. Nothing to do without a floor."""
        if self.floor is None:
            return
        self.floor.tick()
        if self.owner is None and self.waiting:
            self._grant()

    # >>> first-call-wait: the bed (bridge/firstcall.py)
    # A bed is what the track plays when it has nothing to say: the first call's
    # wait music, looped with no gap. It is not a line: it plays only when no line
    # is queued, so a line queued before it is held plays first, and it never
    # reaches the transcript, the said rows or the echo guard. speaking() does not
    # count it, and a cut (barge-in) leaves it alone. The next line queued by say()
    # ends it at once (the call is talking now, so it never returns between
    # sentences), as does unhold(). The recording gets it: the caller heard it.

    def hold(self, pcm):
        """Loop PCM (int16, 48 kHz) under nothing, from the next frame that has no line."""
        x = np.asarray(pcm, dtype=np.int16)
        self.bed, self._bed_at = (x if len(x) else None), 0

    def unhold(self):
        """Drop the bed, from the next frame. -> whether one was held."""
        held, self.bed = self.bed is not None, None
        return held

    def _bed_samples(self):
        out = np.empty(OUT_SAMPLES, dtype=np.int16)
        n = 0
        while n < OUT_SAMPLES:
            take = min(OUT_SAMPLES - n, len(self.bed) - self._bed_at)
            out[n:n + take] = self.bed[self._bed_at:self._bed_at + take]
            n += take
            self._bed_at = (self._bed_at + take) % len(self.bed)
        return out
    # <<< first-call-wait

    # Sounds follow the ears into the call: the sounds for the user's voice play
    # back through the call while the user is on one. A sound is not a line: it is
    # MIXED over the voice, so it never waits behind a line, never delays one, never
    # reaches the transcript or the echo guard, and a cut (barge-in) leaves it alone.
    # OVERLAY_MAX_S bounds a broken writer.
    OVERLAY_MAX_S = 2.0

    def mix(self, pcm):
        """Add PCM (int16, 48 kHz) over what the track is playing, from the next frame.
        Sounds that arrive together play together (summed). Returns the seconds kept: a
        sound longer than OVERLAY_MAX_S is trimmed to it."""
        x = np.asarray(pcm, dtype=np.int16).astype(np.int32)[: int(self.OVERLAY_MAX_S * OUT_RATE)]
        if len(x) > len(self.overlay):
            self.overlay = np.concatenate([self.overlay, np.zeros(len(x) - len(self.overlay), np.int32)])
        self.overlay[:len(x)] += x
        return len(x) / OUT_RATE

    def _mixed(self, out):
        if not len(self.overlay):
            return out
        n = min(OUT_SAMPLES, len(self.overlay))
        y = out.astype(np.int32)
        y[:n] += self.overlay[:n]
        self.overlay = self.overlay[n:]
        return np.clip(y, -32768, 32767).astype(np.int16)

    def speaking(self):
        return bool(self.queue) or len(self.pending) > 0
# >>> echo-patch cut

    def cut(self):
        """Barge-in: stop talking NOW. Returns the seconds dropped.

        The track keeps pacing and emits silence, so the connection is
        untouched - only the words go."""
        secs = self.drop()
        # >>> recording-interrupt
        log(f"interruption dropped={secs:.2f}s")
        # <<< recording-interrupt
        return secs

    def drop(self):
        """cut() without the log line: everything queued goes, the line that
        owns the track and every line waiting behind it are cut (their buffers
        too), and the track is free. Sounds and the bed stay. -> seconds dropped."""
        n = sum(len(a) for a in self.queue) + len(self.pending)
        self.queue.clear()
        self.pending = np.zeros(0, dtype=np.int16)
        lines = ([self.owner] if self.owner is not None else []) + list(self.waiting)
        self.owner = None
        self.waiting.clear()
        for ln in lines:
            n += sum(len(pcm) for pcm, _ in ln.buf)
            ln.buf = []
            ln._over("cut")
        return n / OUT_RATE
# <<< echo-patch cut

    def _next_samples(self):
        self.floor_tick()
        return self._mixed(self._voice_samples())

    def _voice_samples(self):
        while len(self.pending) < OUT_SAMPLES and self.queue:
            self.pending = np.concatenate([self.pending, self.queue.popleft()])
        if len(self.pending) >= OUT_SAMPLES:
            out, self.pending = self.pending[:OUT_SAMPLES], self.pending[OUT_SAMPLES:]
            return out
        if len(self.pending):
            out = np.concatenate([self.pending, np.zeros(OUT_SAMPLES - len(self.pending), np.int16)])
            self.pending = np.zeros(0, dtype=np.int16)
            return out
        if self.bed is not None:              # >>> first-call-wait: nothing to say
            return self._bed_samples()
        return np.zeros(OUT_SAMPLES, dtype=np.int16)

    async def recv(self):
        if self.readyState != "live":
            from aiortc.mediastreams import MediaStreamError
            raise MediaStreamError
        if self._start is None:
            self._start = time.time()
            self._timestamp = 0
        else:
            self._timestamp += OUT_SAMPLES
            wait = self._start + (self._timestamp / OUT_RATE) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        samples = self._next_samples()
        # >>> recording-out
        if _recorder is not None:
            _recorder.write_out(samples)
        # <<< recording-out
        frame = AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.pts = self._timestamp
        frame.sample_rate = OUT_RATE
        frame.time_base = fractions.Fraction(1, OUT_RATE)
        return frame


# ------------------------------------------------- the inbound leg: mail in

def voice_map():
    """sender local part -> voice. A FILE, so adding a guest is data."""
    m = dict(DEFAULT_VOICE_MAP)
    try:
        m.update(json.loads(VOICE_MAP_FILE.read_text()))
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"voices.json ignored: {e}")
    return m


# The guest list. One row a line, `address [ssh-ed25519 AAAA...]`:
#
#     alice@example.org                                 copied on the thread, never known
#     alice@example.org ssh-ed25519 AAAAC3Nza... laptop  copied, and KNOWN when signed by this key
#
# The key column is optional, so a file of bare addresses still parses. An
# address may sit on several rows, one key each. A guest's key is the
# `signer-key` line `message verify` prints for a mail they signed.
KEY_TYPES = ("ssh-ed25519",)
VERIFY_TIMEOUT_S = 10
SIGNER_KEY = re.compile(r"^signer-key\s*:\s*(\S+)\s+(\S+)", re.MULTILINE)


def guest_rows():
    """-> [(address, key or None)], one per row of the guest list. KEY is
    "<type> <base64>", the comment dropped. A key column that is not a key is
    logged and the row kept address-only: it can copy, it can never be known."""
    try:
        text = GUESTS_FILE.read_text()
    except FileNotFoundError:
        return []
    rows = []
    for l in text.splitlines():
        if not l.strip() or l.startswith("#"):
            continue
        words = l.split()
        key = None
        if len(words) >= 3 and words[1] in KEY_TYPES:
            key = f"{words[1]} {words[2]}"
        elif len(words) > 1:
            log(f"call-guests: the key on {words[0]}'s row is not an "
                f"{' or '.join(KEY_TYPES)} key; the row copies but is never known")
        rows.append((words[0], key))
    return rows


def guests():
    """Who is copied on a call thread - the guest list's addresses, each once,
    in file order. A key column does not change who is copied."""
    seen, out = set(), []
    for addr, _ in guest_rows():
        if addr not in seen:
            seen.add(addr); out.append(addr)
    return out


def pinned_keys(addr):
    """The keys pinned on every guest row whose address is ADDR, by the whole
    address, case-insensitively."""
    a = (addr or "").strip().lower()
    return {key for row, key in guest_rows() if key and a and row.lower() == a}


# It swaps sessionmail's verifier out for another program. Nothing the bot runs
# may do that, so it never reaches a verify child.
ORACLE_VAR = "SESSIONMAIL_MHS_ORACLE"


def verify_env():
    """The bot's environment for a `message verify` child, without ORACLE_VAR.
    The registry and bless settings (SESSION_REGISTRY, SESSION_BLESS_*) are
    the operator's, and pass through as set."""
    return {k: v for k, v in os.environ.items() if k != ORACLE_VAR}


def signer_key(path):
    """`message verify PATH` -> the signer's key, "<type> <base64>", or None.

    None unless the verifier ran and exited 0 (a good signature) AND printed a
    signer-key line. A missing binary, a timeout, a bad or absent signature:
    None. That is failing CLOSED: what we cannot check is not known."""
    exe = seatmail.MESSAGE or seatmail.resolve_message()
    if exe is None:
        log("mail leg: no `message` binary (DC_MESSAGE) to verify with - sender unknown")
        return None
    try:
        r = subprocess.run([exe, "verify", str(path)], capture_output=True, text=True,
                           timeout=VERIFY_TIMEOUT_S, check=False, env=verify_env())
    except (OSError, subprocess.SubprocessError) as e:
        log(f"mail leg: message verify failed ({type(e).__name__}: {e}) - sender unknown")
        return None
    if r.returncode != 0:
        return None
    m = SIGNER_KEY.search(r.stdout or "")
    return f"{m.group(1)} {m.group(2)}" if m else None


# ---- the registry rung ----
#
# A From with NO pinned row, that is on an address-only guest row or is a key
# of voices.json, is checked by `message verify --trust --require-blessing`:
# sessionmail's registry vouches for the key, and the operator's blessing
# vouches for the registry cert. Known only on FULL (an UNBLESSED cert is self-asserted,
# so --require-blessing turns it into FAIL), and only when the cert's seat is
# the From's whole local part. Everything else is unknown, with one named line.
#
# The verdict is the LAST stdout line, from one writer in sessionmail. Its exit
# code must agree with it. A too-old `message` prints no TRUST line at all,
# which is how it is caught: exit 2 also means "unsigned", so the code alone
# says nothing.
TRUST_RC = {"FULL": 0, "FAIL": 1, "UNBLESSED": 4, "ERROR": 5}
TRUST_FIELD = re.compile(r"\b(leg|reason)=(\S+)")
REGISTRY_CERT = re.compile(r"^registry-cert\s*:")
# The agent is read ONLY off a registry-cert line that reads
# ok, `registry-cert : ✅ ok <sid8> → agent: <agent>`.
REGISTRY_CERT_IS_OK = re.compile(r"^registry-cert\s*:\s*(?:✅\s*)?ok\b")
REGISTRY_CERT_OK = re.compile(REGISTRY_CERT_IS_OK.pattern + r".*?\bagent:\s*(\S+)\s*$")
NO_REGISTRY_TAIL = "only a pinned key in call-guests.txt makes a sender known on this host"
#: sessionmail's settings file, named in the OFF banner: the mail package's,
#: built by paths.kit_settings.
MAIL_CONF = paths.kit_settings("mail", "mail.json")
REGISTRY_OFF = ('mail leg: registry trust is OFF on this host: message verify --trust found '
                f'no registry (set SESSION_REGISTRY, or "registry" in {MAIL_CONF}). '
                'Rows without a pinned key are never known.')
_registry_off_said = False     # the OFF banner, once per bot run


def voices_file_keys():
    """The local parts voices.json names: the FILE only. DEFAULT_VOICE_MAP is
    the agent's own entry, and its own mail never reaches a trust check."""
    try:
        m = json.loads(VOICE_MAP_FILE.read_text())
    except Exception:
        return set()               # missing or broken: voice_map() says so
    return set(m) if isinstance(m, dict) else set()


def registry_eligible(addr):
    """May ADDR be known by the registry? Only if it is on a guest row by its
    whole address (case-insensitively), or its base local part (voice_for's
    key, cut at `+`) is in voices.json. A stranger costs no verifier run."""
    a = (addr or "").strip().lower()
    if not a:
        return False
    if any(row.lower() == a for row, _ in guest_rows()):
        return True
    return a.split("@")[0].split("+")[0] in voices_file_keys()


def _registry_off():
    global _registry_off_said
    if not _registry_off_said:
        _registry_off_said = True
        log(REGISTRY_OFF)


def registry_known(path, addr):
    """`message verify --trust --require-blessing PATH` -> is ADDR known by the
    registry? True only for rc 0, a last line of exactly `TRUST: FULL`, a
    signer-key line, and ONE registry-cert line that reads ok and names the
    From's whole local part. Every outcome logs exactly one line."""
    say = lambda why: log(f"mail leg: {addr}: {why}")   # noqa: E731
    exe = seatmail.MESSAGE or seatmail.resolve_message()
    if exe is None:
        say("no message binary (DC_MESSAGE) to verify with - unknown")
        return False
    try:
        r = subprocess.run([exe, "verify", "--trust", "--require-blessing", str(path)],
                           capture_output=True, text=True, timeout=VERIFY_TIMEOUT_S,
                           check=False, env=verify_env())
    except subprocess.TimeoutExpired:
        say(f"message verify --trust timed out after {VERIFY_TIMEOUT_S:g} s - unknown")
        return False
    except (OSError, subprocess.SubprocessError) as e:
        say(f"message verify --trust failed ({type(e).__name__}: {e}) - unknown")
        return False
    lines = [l.rstrip() for l in (r.stdout or "").splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    if not last.startswith("TRUST:"):
        say(f"message verify gave no TRUST line (rc {r.returncode}; too old for --trust?) - unknown")
        return False
    words = last.split()
    verdict = words[1] if len(words) > 1 else ""
    if TRUST_RC.get(verdict) != r.returncode or (verdict == "FULL" and last != "TRUST: FULL"):
        say(f"message verify said '{last}' with rc {r.returncode} - unknown")
        return False
    fields = dict(TRUST_FIELD.findall(last))
    if verdict == "ERROR":
        reason = fields.get("reason", "?")
        if reason == "registry-absent":
            say(f"no sessionmail registry here (TRUST: ERROR reason=registry-absent) - unknown; "
                f"{NO_REGISTRY_TAIL}")
            _registry_off()
        else:
            say(f"message verify --trust could not run (TRUST: ERROR reason={reason}) - unknown; "
                f"{NO_REGISTRY_TAIL}")
        return False
    if verdict == "FAIL":
        say(f"registry trust FAIL (leg={fields.get('leg', '?')} "
            f"reason={fields.get('reason', '?')}) - unknown")
        return False
    if verdict == "UNBLESSED":     # --require-blessing makes this FAIL; an old verifier may not
        say(f"registry trust UNBLESSED (reason={fields.get('reason', '?')}) - unknown; "
            "only a blessed cert (FULL) counts")
        return False
    # FULL, rc 0.
    if not SIGNER_KEY.search("\n".join(lines)):
        say("TRUST: FULL but no signer-key line - unknown")
        return False
    certs = [l for l in lines if REGISTRY_CERT.match(l)]
    if len(certs) > 1:             # first-and-only, never first-match
        say(f"TRUST: FULL but {len(certs)} registry-cert lines, not one - unknown")
        return False
    if certs and not REGISTRY_CERT_IS_OK.match(certs[0]):
        say("TRUST: FULL but the registry-cert line is not ok - unknown")
        return False
    m = REGISTRY_CERT_OK.match(certs[0]) if certs else None
    if m is None:                  # no line, or an ok line with no agent on it
        say("TRUST: FULL but no registry-cert agent line to match - unknown")
        return False
    agent, local = m.group(1), local_part(addr)
    if agent.lower() != local:
        say(f"registry seat '{agent}' is not the From '{local}' - unknown")
        return False
    say(f"known by registry (FULL, seat {agent})")
    return True


def sender_known(path, addr):
    """Is the mail at PATH from a guest we can vouch for? The ladder, top down,
    the first rung that applies deciding (own mail never gets here):

    1. ADDR has a PINNED row (option A): known iff `message verify` exits 0
       and its signer key is pinned for ADDR. A mismatch is unknown, and a pin
       NEVER falls back to the registry: it is the owner's narrower claim.
    2. No pin, but eligible (registry_eligible): registry_known() decides.
    3. Anyone else: unknown, with no verifier run.

    Nothing else is known: not a From line, not an X-Voice, not a voices.json
    entry on its own."""
    keys = pinned_keys(addr)
    if keys:
        if signer_key(path) in keys:
            return True
        log(f"mail leg: {addr} has a pinned key but the signer is not pinned - unknown "
            "(a pin never falls back to the registry)")
        return False
    if registry_eligible(addr):
        return registry_known(path, addr)
    return False                       # a stranger: never known, and no verifier run


def voice_for(addr, name=None, header=None, known=False):
    """-> (voice, who, known). KNOWN is sender_known()'s answer, never a header's.

    A sender that is not known speaks in UNKNOWN_VOICE and is announced so:
    X-Voice and voices.json are both ignored, because From is a header and a
    forged From must not borrow anyone's voice. A known sender's X-Voice wins
    (the per-message override); then the map on its local part; then the
    plain voice, still named, since its signature is good."""
    local = (addr or "").split("@")[0].split("+")[0].lower()
    who = (name or "").strip() or local.capitalize() or "someone"
    if not known:
        return UNKNOWN_VOICE, who, False
    if header and header.strip():
        return header.strip(), who, True
    m = voice_map()
    if local in m:
        return m[local], who, True
    return UNKNOWN_VOICE, who, True


QUOTE = re.compile(r"^\s*(?:>|On .*wrote:|-{2,}\s*$)")


def mail_speech(msg):
    """The part of a mail worth SAYING: first text/plain part, quoted lines
    and the bridge's own footer removed."""
    part = msg
    if msg.is_multipart():
        part = next((p for p in msg.walk()
                     if p.get_content_type() == "text/plain"), None)
        if part is None:
            return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", "replace")
    lines = []
    for l in text.splitlines():
        if l.strip() in ("---", "-- "):
            break                      # the bridge's own footer starts here
        if QUOTE.match(l):
            continue
        lines.append(l)
    return " ".join(" ".join(lines).split())


SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def spoken_part(text, cap=MAIL_SPEAK_MAX):
    """The part of a mail's text to SAY: all of it when it fits in cap,
    else a clean cut closed by SPOKEN_MORE, marker counted inside the cap.
    The cut falls at the last sentence end that fits, else the last whole
    word, else where the room runs out (one giant token)."""
    if len(text) <= cap:
        return text
    room = cap - len(SPOKEN_MORE) - 2        # a space, and a "." a mid-sentence cut needs
    ends = [m.end() for m in SENTENCE_END.finditer(text) if m.end() <= room]
    if ends:
        kept = text[:ends[-1]]
    else:
        space = text.rfind(" ", 0, room + 1)
        kept = text[:space] if space > 0 else text[:room]
        kept = kept.rstrip(" ,;:-") + "."     # its own sentence: the marker is spoken apart
    return f"{kept} {SPOKEN_MORE}"


def local_part(addr: str) -> str:
    """ADDR's local part, lower case: the host discarded, and a bare name is its own."""
    a = (addr or "").strip().lower()
    return a.rpartition("@")[0] if "@" in a else a


def own_mail(addr: str) -> bool:
    """Is ADDR the bridge's own (its seat, `<agent>-chat`)? By the LOCAL PART, the
    host discarded: the seat's From is qualified by the MTA, so
    `<agent>-chat@host`, `@host.example.net` or bare can each arrive while
    dc_env.ADDR is spelt with DC_HOST's or sessionmail's host. sessionmail groups a seat's
    addresses the same way. No local part is never own."""
    mine = local_part(addr)
    return bool(mine) and mine == local_part(dc_env.ADDR)


# The roots this bridge ISSUED, per chat.
# A mail reaches a call only by a root on this list, matched exactly: never by
# a call-<chatid>-<digits> pattern, which anyone can write. A guest's root goes
# stale when a call turns over (eleven seconds, measured), so a
# chat keeps its prior roots across a turnover, and no longer:
TURNOVER_S = 120         # a call minted this soon after the chat's last call
                         # ended keeps that chat's prior roots; later, they go
ROOTS_PER_CHAT = 8       # the newest roots kept per chat, so a flapping call
                         # cannot grow the set without bound


class IssuedRoots:
    """Per chat, the roots the bridge minted, in memory only: a restart clears
    them, so a reply to a root from before a restart is not spoken.

    mint() records a call's root. end() marks the chat ended at T when its last
    live or ringing call closes. The next mint() in that chat keeps the prior
    roots only within TURNOVER_S of T; otherwise it clears them first."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_chat = {}        # chat_id -> [root, ...], oldest first
        self.ended = {}          # chat_id -> when its last call ended

    def mint(self, chat_id, root, now=None):
        now = time.time() if now is None else now
        with self.lock:
            roots = self.by_chat.setdefault(chat_id, [])
            t = self.ended.pop(chat_id, None)
            if t is not None and now - t > TURNOVER_S:
                roots.clear()
            if root not in roots:
                roots.append(root)
            del roots[:-ROOTS_PER_CHAT]

    def end(self, chat_id, now=None):
        with self.lock:
            self.ended[chat_id] = time.time() if now is None else now

    def chats(self, ids):
        """The chats one of whose issued roots is, exactly, one of IDS."""
        with self.lock:
            return {c for c, roots in self.by_chat.items() if any(r in ids for r in roots)}


ISSUED = IssuedRoots()

MSGID = re.compile(r"<([^<>\s]+)>")


def ref_ids(msg):
    """The Message-IDs a mail answers: In-Reply-To and References, each id
    whole and without its <>. A bare id (no brackets) counts too. Matching is
    by equality on these, never by substring."""
    raw = f"{msg.get('In-Reply-To', '') or ''} {msg.get('References', '') or ''}"
    ids = set(MSGID.findall(raw))
    ids |= {w for w in MSGID.sub(" ", raw).split() if "@" in w}
    return ids


class MailLeg(threading.Thread):
    """Mail that threads to a LIVE call is spoken into that call, in the
    sender's voice. One thread for every call; it only looks while a call is
    up, and it moves what it speaks to cur/ so nothing is said twice."""

    def __init__(self, manager, every=1.0):
        super().__init__(name="call-mail", daemon=True)
        self.manager, self.every = manager, every

    def run(self):
        while True:
            try:
                if any(s.pc for s in list(self.manager.sessions.values())):
                    self.sweep()
            except Exception:
                log("mail leg failed:\n" + traceback.format_exc())
            time.sleep(self.every)

    # How far back in cur/ to look for mail an indexer took before we
    # could. Long enough to cover a slow `notmuch new`, short enough
    # that a stranded mail from a past call is never resurrected.
    CUR_WINDOW = 600

    def candidates(self):
        """Guest mail we have not spoken: new/, plus cur/ without `S`.

        Watching new/ alone loses a race we cannot win. notmuch indexes
        ~/.mail, which contains this box, and `notmuch new` MOVES mail
        out of new/ into cur/ as it goes - so a one-second poll of new/
        finds nothing and the guest is silently dropped. Measured here:
        77 guest mails delivered and never spoken, the oldest six days
        old, while the leg looked healthy.

        No extra bookkeeping is needed, because seen() is the only
        thing that writes the maildir `S` flag: a name ending `:2,` in
        cur/ means arrived-and-never-spoken. A maildir shared with an
        indexer has to be read by FLAG, not by directory.
        """
        # paths.is_dir/is_file, never Path.*: on 3.14 an unsearchable box would
        # read "empty" and drop the guest's mail in silence; this way it raises
        # on every Python and run() logs it.
        out = []
        newd = BOT_BOX / "new"
        if paths.is_dir(newd):
            out += [f for f in newd.iterdir() if paths.is_file(f)]
        curd, cutoff = BOT_BOX / "cur", time.time() - self.CUR_WINDOW
        if paths.is_dir(curd):
            for f in curd.iterdir():
                if not paths.is_file(f) or "S" in f.name.rsplit(":2,", 1)[-1]:
                    continue
                try:
                    if f.stat().st_mtime >= cutoff:
                        out.append(f)
                except OSError:
                    pass
        return sorted(out, key=lambda f: f.name)

    def sweep(self):
        for f in self.candidates():
            try:
                self.one(f)
            except FileNotFoundError:
                # The indexer moved it from new/ to cur/ between candidates()
                # listing it and one() opening it. Benign: candidates() reads
                # cur/ by FLAG, so the next sweep finds it again. A traceback
                # for a harmless race hides the errors that matter.
                log(f"mail leg: {f.name} moved under us mid-sweep (indexer)")
            except Exception:
                log(f"mail leg could not read {f.name}:\n" + traceback.format_exc())

    def one(self, f):
        msg = email.message_from_bytes(f.read_bytes(), policy=email.policy.default)
        ids = ref_ids(msg)
        up = [s for s in list(self.manager.sessions.values()) if s.pc]
        live = [s for s in up if s.root_id and s.root_id in ids]
        if not live:
            # No live session with that exact root. A guest's root goes stale
            # the moment a call turns over, so a root this bridge ISSUED for a
            # chat reaches the live call in that chat.
            # Only an issued root: a call-<chatid>-<digits> string is not one.
            chats = ISSUED.chats(ids)
            if chats:
                live = [s for s in up if s.chat_id in chats]
                if live:
                    log(f"mail leg: root is stale, but chat "
                        f"{sorted(chats)} has a live call - speaking there")
        name, addr = email.utils.parseaddr(msg.get("From", ""))
        if not live:
            return                     # not for a call in progress: leave it
        if own_mail(addr):             # by local part, any host spelling
            self.seen(f)               # the bridge's own mail, never spoken
            return
        text = mail_speech(msg)
        if not text:
            log(f"mail leg: {addr} sent nothing sayable, skipped")
            self.seen(f)
            return
        known = sender_known(f, addr)
        voice, who, known = voice_for(addr, name, msg.get("X-Voice"), known=known)
        post = f"{who}: {text}"
        if not known:
            # The owner reads the chat post as much as he hears the call: it
            # must not read as trusted either.
            post = f"{who} <{addr}> (unverified sender): {text}"
            who = f"{who}, who I do not have a voice for,"
        for s in live:
            log(f"MAIL IN -> call chat {s.chat_id}: {who} ({voice}, {addr}, "
                f"{'known' if known else 'unverified'}): {one_line(text, 120)}")
            reply_seen(self.manager.loop, s, "mail")   # >>> turn-log: before speak, FIFO
            asyncio.run_coroutine_threadsafe(s.speak(spoken_part(text), voice, who, floor=True),
                                             self.manager.loop)
            try:
                post_to_chat(s.account, s.chat_id, post, "mail-leg post")
            except Exception as e:
                log(f"mail leg: could not post to chat {s.chat_id}: {e}")
        self.seen(f)

    def seen(self, f):
        """maildir: new/NAME -> cur/NAME:2,S. Done before speaking finishes, so
        a slow TTS cannot make the same mail be spoken twice."""
        try:
            (BOT_BOX / "cur").mkdir(parents=True, exist_ok=True)
            f.rename(BOT_BOX / "cur" / (f.name.split(":")[0] + ":2,S"))
        except Exception as e:
            log(f"mail leg: could not mark {f.name} read: {e} - unlinking")
            f.unlink(missing_ok=True)


# ----------------------------------------------------------------- voice in

class Utterances:
    """Energy VAD with hysteresis over 20 ms frames at 16 kHz: yields wav
    paths, one per CHUNK - speech between short pauses. It never cuts
    mid-word: a chunk that runs past CHUNK_MAX is cut at its quietest frame
    and the rest carries into the next chunk. Putting the chunks back together
    into one turn is CallSession's job."""

    def __init__(self, call_dir):
        self.call_dir = call_dir
        # >>> recording-open: the Recorder is CallSession._rec_start's, and only
        # while recording is on; this only writes the chunks STT reads.
        # <<< recording-open
        self.floor = 200.0
        self.buf = []            # frames of the current chunk
        self.rms = []            # each frame's energy, parallel to buf
        self.speech_frames = 0
        self.quiet_frames = 0
        self.in_speech = False
        self.n = 0

    def feed(self, frame16):
        """frame16: int16 array, 16 kHz mono, ~20 ms. Returns a wav path or None."""
        rms = float(np.sqrt(np.mean(frame16.astype(np.float32) ** 2))) if len(frame16) else 0.0
        if not self.in_speech:
            self.floor = (min(self.floor * 1.02 + 1, max(self.floor * 0.98, rms))
                          if rms < self.floor else self.floor * 1.005)
        # hysteresis: loud enough to START speech is not loud enough to KEEP
        # it. A trailing syllable is quieter than the onset, and the single
        # threshold ended his sentence on it.
        start = max(350.0, self.floor * 3.5)
        keep = max(200.0, self.floor * 2.0)
        loud = rms > (keep if self.in_speech else start)
        # >>> recording-in
        if _recorder is not None:
            _recorder.write_in(frame16)
        # <<< recording-in
        self.buf.append(frame16)
        self.rms.append(rms)
        lead = max(int(LEAD_IN / PTIME), 1)
        if not self.in_speech:
            if loud:
                self.speech_frames += 1
                if self.speech_frames >= 2:
                    self.in_speech = True
                    # >>> recording-speech-start
                    log("user_speech_started")
                    # <<< recording-speech-start
                    self.quiet_frames = 0
                    self.buf, self.rms = self.buf[-lead:], self.rms[-lead:]
            else:
                self.speech_frames = 0
                self.buf, self.rms = self.buf[-lead:], self.rms[-lead:]
            return None
        if loud:
            self.quiet_frames = 0
            self.speech_frames += 1
        else:
            self.quiet_frames += 1
        if self.quiet_frames >= max(int(CHUNK_QUIET / PTIME), 1):
            path = (self._flush(len(self.buf))
                    if self.speech_frames >= max(int(MIN_SPEECH / PTIME), 1) else None)
            self.buf, self.rms = [], []
            self.in_speech, self.speech_frames, self.quiet_frames = False, 0, 0
            return path
        if len(self.buf) * PTIME >= CHUNK_MAX:
            # he has not paused at all: cut where he is quietest, not where
            # the clock says, and carry the tail into the next chunk
            return self._flush(self._quietest())
        return None

    def _quietest(self):
        """The index of the lowest-energy frame in the last third of the
        buffer - the least bad place to cut a sentence that will not end."""
        tail = max(int(len(self.rms) * 0.66), 1)
        if len(self.rms) <= tail:
            return len(self.rms)
        return max(tail + int(np.argmin(self.rms[tail:])), 1)

    def _flush(self, upto):
        """Write buf[:upto] as a wav; whatever is left starts the next chunk."""
        self.n += 1
        path = self.call_dir / CALLER_CHUNK.format(n=self.n)
        pcm = np.concatenate(self.buf[:upto])
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(IN_RATE)
            w.writeframes(pcm.tobytes())
        self.buf, self.rms = self.buf[upto:], self.rms[upto:]
        self.speech_frames = max(self.speech_frames - upto, 0)
        self.quiet_frames = 0
        return path


# --------------------------------------------------------------- a session

# >>> ears-clock
EARS_WAIT = 0.7          # s with no frame from him before the timeout path fills
FILL_CAP = 2.0           # s: the most silence one fill writes
WALL_LEAD = 0.5          # s: the wav may run this far ahead of the wall clock, no further


class EarsClock:
    """Where caller-call.wav should be, in samples: one clock for the wav, ears
    and the ear leg. It counts; _listen_ears writes.

    His frames carry pts (the RTP timestamp, so media time): a gap in pts is
    time that passed, and that much silence is owed. A timeout (no frame for
    EARS_WAIT) fills the TRUE time since his last frame, marked provisional,
    because the VAD needs silence to end his turn; the next frame reconciles
    it, and an over-fill (a stall, then a burst of contiguous pts) is debt that
    later gaps pay off. Silence goes in whole IN_SAMPLES blocks, the remainder
    carried. At most FILL_CAP in one fill, never past the wall clock plus
    WALL_LEAD. No pts, pts going back, or more than FILL_CAP owed at once (a
    false 2^32 wrap) re-anchors: no fill. Each cap and re-anchor kind is
    logged once per call."""

    def __init__(self, t0, log):
        self.t0, self.log = t0, log
        self.written = 0          # samples in caller-call.wav so far
        self.start_epoch = None   # the wav's second 0 on the wall clock, from his first frame
        self.last_frame = None    # wall time of his last frame
        self.prev_end = None      # media time (s) where his last frame ended; None = no anchor
        self.provisional = 0      # samples the timeout path wrote since his last frame
        self.owed = 0             # samples owed (< one block, carried) or, below 0, written ahead
        self.said = set()

    def _once(self, kind, msg):
        if kind not in self.said:
            self.said.add(kind)
            self.log(f"ears clock: {msg} (said once a call)")

    def _room(self, now, want):
        """want samples of silence, held to FILL_CAP and the wall budget, in
        whole blocks; and whether a cap cut it."""
        n, cut = want, False
        if n > FILL_CAP * IN_RATE:
            n, cut = int(FILL_CAP * IN_RATE), True
            self._once("fill-cap", f"a fill of {want / IN_RATE:.2f}s held to the {FILL_CAP:g}s fill cap")
        base = self.t0 if self.start_epoch is None else self.start_epoch
        room = int((now - base + WALL_LEAD) * IN_RATE) - self.written
        if n > room:
            n, cut = max(0, room), True
            self._once("wall", f"silence held to the wall budget (wall + {WALL_LEAD:g}s)")
        return n // IN_SAMPLES * IN_SAMPLES, cut

    def timeout(self, now):
        """No frame for EARS_WAIT: the silence to write now."""
        since = self.t0 if self.last_frame is None else self.last_frame
        want = round((now - since) * IN_RATE) - self.provisional
        if want < IN_SAMPLES:
            return 0
        n, _ = self._room(now, want)
        self.provisional += n
        return n

    def frame(self, frame, now):
        """His frame arrived: the silence to write before it."""
        prov, self.provisional = self.provisional, 0
        self.last_frame = now
        if self.start_epoch is None:
            # any pre-roll silence is already in the wav, so second 0 is that much earlier
            self.start_epoch = now - self.written / IN_RATE
        pts, tb = frame.pts, frame.time_base
        if pts is None or tb is None:
            self.prev_end = None
            self._once("no-pts", "his frames carry no pts: only timeouts move the clock")
            return 0
        at = pts * tb
        prev, self.prev_end = self.prev_end, at + fractions.Fraction(frame.samples, frame.sample_rate)
        if prev is None:
            return 0                              # the first frame, or after no pts: anchor here
        gap = round((at - prev) * IN_RATE)
        owed = gap - prov
        if gap < -IN_SAMPLES or owed > FILL_CAP * IN_RATE:
            self.owed = 0
            self._once("re-anchor", f"pts {'went back' if gap < 0 else 'jumped'} "
                                    f"{abs(gap) / IN_RATE:.2f}s: re-anchor, no fill")
            return 0
        self.owed += owed
        if self.owed < IN_SAMPLES:
            return 0                              # carried, or debt still owed
        n, cut = self._room(now, self.owed)
        self.owed = 0 if cut else self.owed - n
        return n
# <<< ears-clock


class CallSession:
    """One call, either way. Incoming: answer(offer). Outgoing: dial()."""

    heard = NullSink()   # M8; __init__ sets the real one
    msg_id = None        # the call's id (the invite message's); set when it rings or dials
    answer_go = None     # >>> ring-first; defaults here too, for a session made without __init__
    voicemail = False
    ringing = False      # holding an incoming call for dc-answer: no pc yet, so never "live"
    picked = False       # dc-answer won the ring (set on the calls loop, so no race with RING_WAIT)
    closed = None        # why close() ran; set even when there is no pc yet
    # >>> turn-log: defaults for a session made without __init__,
    # as the tests make them. `awaiting` is a TUPLE and is rebound, never
    # appended to: a class-level list would be one list shared by every session.
    pc = None
    speaking = False
    noreply_task = None
    turn_n = 0           # turns mailed (or tried) on this call, counting from 1
    awaiting = ()        # (n, msgid, t_mailed) of mailed turns with no reply yet, by n
    # <<< turn-log
    wait_task = None     # >>> first-call-wait: the wait's task while it runs; close() cancels it

    def __init__(self, manager, account, chat_id, msg_id=None, offer_sdp=None, has_video=False):
        self.manager, self.account = manager, account
        self.chat_id, self.msg_id, self.offer_sdp = chat_id, msg_id, offer_sdp
        self.has_video = has_video
        self.outgoing = offer_sdp is None
        self.pc = None
        self.voice = PipVoiceTrack()
        # a reply line waits for his quiet: the floor reads the VAD's speaking
        self.voice.floor = Floor(lambda: self.speaking, FLOOR_GAP, FLOOR_CAP)
        # >>> call-id
        # The directory carries the CALL, not just the room, so the log line
        # "closing msg 5531" and the tape on disk name the same thing.
        self._stamp = time.strftime('%Y%m%d-%H%M%S')
        self.dir = CALLS_DIR / self._dirname()
        # <<< call-id
        self.chat_name, self.caller = "call", "caller"
        self.connected = False
        self.tasks = []
        # the call is ONE mail thread: this root, the caller's lines as replies as
        # they land, the whole transcript as the last reply at hang-up
        # (passively, by replies to one mail; the chat post is not built
        # yet, so it is OFF (POST_TRANSCRIPT) until the owner asks)
        # the ids are minted at sessionmail's own host (seatmail.id_host), never
        # DC_HOST: `message send --message-id` refuses any other
        self.root_id = (f"call-{PROFILE + '-' if PROFILE else ''}{chat_id}-"
                        f"{int(time.time())}@{seatmail.id_host()}")
        ISSUED.mint(chat_id, self.root_id)   # a root we issued, matched exactly
        self.lines = []            # (time.time(), who, text)
        # M8: partials and turns into VoiceMode's heard log, so every agent's
        # ride-along hook and wake waiter see a call as they see the mic.
        # OFF unless DC_HEARD=1, and only for chats in DC_HEARD_CHATS.
        self.heard = CallSink(chat_id, log, call=str(msg_id) if msg_id else None)
# >>> echo-patch init
        # a speakerphone in a noisy room puts our speech back into their mic,
        # and it returns labelled as the caller. See echo.py.
        self.echo = EchoGuard(log=log)
# <<< echo-patch init
        self.started = None
        self.invited = []          # guests added mid-call by dc-invite
        # a chosen opening line and voice for an outgoing call; None = the usual
        self.greeting, self.greet_voice = None, None
        # >>> ring-first: set while an incoming call rings the agent; None = never rang
        self.answer_go = None      # asyncio.Event: dc-answer, or the call ended
        self.voicemail = False     # nobody answered: greet as voicemail
        self.ringing = self.picked = False
        self.closed = None
        # <<< ring-first
        # a turn is his chunks run back together: chunks go to whisper as they
        # close, the TURN is what reaches her mailbox (see CHUNK_QUIET above)
        self.chunk_n = 0
        self.turn_parts = []       # (seq, text) transcribed, not yet sent
        self.turn_pending = set()  # seqs still inside whisper
        self.turn_timer = None
        self.previews = Previews()
        self.speaking = False      # the VAD's in_speech, kept by _feed
        self.preview_id = None     # Message-ID of the newest preview this turn

    def guest_list(self):
        """Who is copied on this call: the standing opt-ins plus anyone
        invited while it was up. Opt-in:
        a call is not overheard by default."""
        seen, out = set(), []
        for a in list(guests()) + list(self.invited):
            if a and a not in seen:
                seen.add(a); out.append(a)
        return out

    async def invite(self, addr):
        """Add a guest mid-call and mail them the way in."""
        if addr in self.guest_list():
            log(f"invite: {addr} is already on this call")
            return
        self.invited.append(addr)
        stamp = time.strftime("%H:%M", time.localtime(self.started or time.time()))
        log(f"INVITED {addr} to the call in chat {self.chat_id}")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: _hooks.mail_in(
            self.chat_name, self.chat_id, "call",
            f"You are on {self.caller}'s call, live since {stamp}. Every line "
            "said reaches you as a reply to this thread from now on. To be "
            "HEARD, mail the bridge and thread to the call root - spell the "
            "address out, Reply-To will not do it for a CLI:\n"
            f"  message send {dc_env.ADDR} \\\n"
            f"    -r '<{self.root_id}>' -s 'your line' -f FILE\n"
            "One or two sentences: it is read aloud and cannot be interrupted.",
            in_reply_to=self.root_id, cc=[addr],
            reply_to=dc_env.ADDR,
            subject=f"INVITED to the call with {self.caller}"))
        await self.speak(f"{addr.split('@')[0]} is on the call now.")

    # ---- shared setup

    async def _ring(self, loop):
        """>>> ring-first: hold an incoming call, unanswered, until dc-answer or RING_WAIT.

        Held BEFORE the peer connection exists. aiortc starts ICE checks at
        setLocalDescription, and checks sent into a call nobody has answered
        back off: 5-12 s of silence after a pick-up was measured, and
        ICE FAILED after a 66 s hold. Held here, the phone just keeps ringing
        through the ~5 s of ICE, as in auto.
        True = go on and answer (picked up, or voicemail). False = it ended
        (hung up, or declined with dc-hangup) while it rang."""
        rang = time.time()
        self.answer_go = asyncio.Event()
        if not self.closed:              # it can end during _names(), before it rings
            self.ringing = True
            loop.run_in_executor(None, self._mail_ringing)
            try:
                await asyncio.wait_for(self.answer_go.wait(), RING_WAIT)
            except asyncio.TimeoutError:
                pass
        # picked is set on this loop by CallManager.answer, only while ringing:
        # reading it after ringing goes False leaves no window for a late one
        self.ringing = False
        self.answer_go.set()
        took = time.time() - rang
        if self.closed:
            log(f"NOT ACCEPTING msg {self.msg_id}: {self.closed} while ringing ({took:.1f}s)")
            loop.run_in_executor(None, self._mail_missed, self.closed, took)
            self.manager.forget(self)
            return False
        if self.picked:
            log(f"PICKED UP msg {self.msg_id} by {ANSWER_CMD} after {took:.1f}s")
        else:
            self.voicemail = True
            log(f"VOICEMAIL msg {self.msg_id}: no {ANSWER_CMD} in {RING_WAIT:.0f}s")
        return True

    def _mail_missed(self, why, took):
        """>>> ring-first: a ring that never became a call still reaches the agent."""
        try:
            _hooks.mail_in(
                self.chat_name, self.chat_id, "call",
                f"MISSED call from {self.caller} (call msg {self.msg_id}): {why} "
                f"after {took:.0f} s of ringing. Nothing was answered or recorded.",
                in_reply_to=f"ring-{self.root_id}",
                subject=f"MISSED {self.caller}: {why} at {took:.0f}s")
        except Exception:
            log("missed-call mail failed:\n" + traceback.format_exc())

    def _mail_ringing(self):
        """>>> ring-first: the agent's doorbell. Runs in the executor."""
        try:
            _hooks.mail_in(
                self.chat_name, self.chat_id, "call",
                f"{self.caller} is RINGING {dc_env.AGENT_NAME} (call msg {self.msg_id}).\n\n"
                f"  Pick up:  {ANSWER_CMD} {self.chat_id}\n"
                f"  Decline:  {HANGUP_CMD} {self.chat_id}\n\n"
                f"No pick-up in {RING_WAIT:.0f} s and the bridge answers as voicemail: "
                f"the caller hears \"{GREETING_VOICEMAIL}\" and what they say "
                "reaches you as mail, as on any call.",
                msgid=f"ring-{self.root_id}", in_reply_to=self.root_id,
                subject=f"RINGING {self.caller}: {ANSWER_CMD} {self.chat_id} in {RING_WAIT:.0f}s")
            log(f"RINGING mailed to {dc_env.MAILTO} for msg {self.msg_id}")
        except Exception:
            log("ringing mail failed - it will go to voicemail unless someone "
                "picks up:\n" + traceback.format_exc())

    async def _names(self):
        loop = asyncio.get_running_loop()
        try:
            chat = self.account.get_chat_by_id(self.chat_id)
            info = await loop.run_in_executor(None, chat.get_basic_snapshot)
            self.chat_name = getattr(info, "name", "call") or "call"
            if self.msg_id:
                msg = self.account.get_message_by_id(self.msg_id)
                snap = await loop.run_in_executor(None, msg.get_snapshot)
                self.caller = snap.sender.get_snapshot().display_name
            else:
                self.caller = self.chat_name       # a 1:1 chat is named after the other side
        except Exception:
            log("could not name the caller:\n" + traceback.format_exc())

    async def _build_pc(self):
        loop = asyncio.get_running_loop()
        ice = await loop.run_in_executor(None, self.account.ice_servers)
        log(ice_servers_summary(ice))   # >>> ice-redact: never the TURN credential
        servers = []
        for s in ice or []:
            urls = s.get("urls") if isinstance(s, dict) else None
            if isinstance(urls, str):
                urls = [urls]
            # aiortc's parse_stun_turn_uri refuses bracketed IPv6 ("malformed
            # uri", seen on the first call); the relay lists an IPv4 twin
            urls = [u for u in (urls or []) if "[" not in u]
            if not urls:
                continue
            servers.append(RTCIceServer(urls=urls, username=s.get("username"),
                                        credential=s.get("credential")))
        self.pc = pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))

        # mirror the webapp's negotiated channels so its messages have a home
        trickle = pc.createDataChannel("iceTrickling", negotiated=True, id=1)
        muted = pc.createDataChannel("mutedState", negotiated=True, id=3)

        @trickle.on("message")
        def on_candidate(data):
            asyncio.ensure_future(self._add_candidate(data))

        @muted.on("open")
        def on_muted_open():
            try:
                muted.send(json.dumps({"videoEnabled": False, "audioEnabled": True}))
            except Exception as e:
                log(f"mutedState send failed: {e}")

        @muted.on("message")
        def on_muted(data):
            log(f"caller mutedState: {data}")

        @pc.on("track")
        def on_track(track):
            log(f"remote track: {track.kind}")
            if track.kind == "audio":
                self.tasks.append(asyncio.ensure_future(self._listen(track)))
            else:
                self.tasks.append(asyncio.ensure_future(self._drain(track)))

        watch_ice(pc)                   # >>> ice-log: types and counts, no addresses

        @pc.on("connectionstatechange")
        async def on_state():
            log(f"connection {pc.connectionState}")
            if pc.connectionState == "connected" and not self.connected:
                await self._on_connected()
            elif pc.connectionState in ("failed", "closed"):
                await self.close("connection " + pc.connectionState)

        pc.addTrack(self.voice)
        return pc

    def _connected_text(self, stamp, way):
        """The CALL CONNECTED mail: how to answer, the mouth first."""
        return (
            f"CALL CONNECTED with {self.caller} at {stamp} ({way}).\n"
            f"{dc_env.reply_hint(self.chat_id, self.msg_id, PROFILE)}\n"
            "Either is spoken into the call. Keep it short. Every line "
            "heard is a reply to this mail; the transcript lands here at "
            "hang-up.\n\n"
            "ANYONE CC'd: to be HEARD on this call, mail the bridge and "
            "thread to this message - the address must be spelled out, "
            "Reply-To will not do it for a CLI:\n"
            f"  message send {dc_env.ADDR} \\\n"
            f"    -r '<{self.root_id}>' -s 'your line' -f FILE\n"
            f"Mail to {dc_env.MAILTO.split('@')[0]} lands in {dc_env.AGENT_NAME}'s box and is never spoken. "
            "One or two sentences: it is read aloud. Your own voice "
            "(voices.json, or an X-Voice: header) is used only when your "
            "mail is signed by a key pinned on your row of call-guests.txt, "
            "or, with no key pinned for you, when `message verify --trust` "
            "says FULL (a sessionmail registry cert blessed by the owner) "
            "for a seat that is your whole From local part; otherwise you "
            "are announced as a sender without a voice, unverified. "
            "A host with no registry knows no one this way, and a "
            "plus-address seat is not yet known by the registry.")

    async def _on_connected(self):
        self.connected = True
        self.started = time.time()
        self._live_write()              # >>> call-id: dc-say can now see us
        set_call_id(self.msg_id)        # >>> call-id-log: stamp every line
        # >>> recording-ears-open: both paths meet here (or at their listener,
        # if it started first): the switch is read and said once a call
        self._rec_start()
        # <<< recording-ears-open
        self._rec_notice_post()         # >>> recording-notice: the chat line, every call
        stamp = time.strftime("%H:%M", time.localtime(self.started))
        way = f"{dc_env.AGENT_NAME} rang" if self.outgoing else f"rang {dc_env.AGENT_NAME}"
        if self.voicemail:        # >>> ring-first
            way += f"; VOICEMAIL, no {ANSWER_CMD} in {RING_WAIT:.0f} s"
        self.manager.loop.run_in_executor(
            None, lambda: _hooks.mail_in(
                self.chat_name, self.chat_id, "call",
                self._connected_text(stamp, way),
                msgid=self.root_id, subject=f"CALL {stamp} with {self.caller}",
                reply_to=dc_env.ADDR, cc=self.guest_list(), call=self.msg_id))
        self.heard.chat_name = self.chat_name   # M8: the call opens in the heard log
        self.heard.event("call-started", direction="out" if self.outgoing else "in")
        hello = GREETING_OUT
        if self.outgoing and PROFILE and self.caller:
            hello = f"Hi {self.caller}, {dc_env.GREETING_NAME} here."   # a public-profile ring names whoever it rings
        # a chosen line and voice win; who = the voice, so the transcript says who spoke
        first = GREETING_VOICEMAIL if self.voicemail else GREETING      # >>> ring-first
        if FIRST_CALL_WAIT and self.wait_task is None:   # >>> first-call-wait: beside the greeting
            self.wait_task = asyncio.ensure_future(self._first_call_wait())
        await self._rec_notice_speak()  # >>> recording-notice: before the greeting
        await self.speak(self.greeting or (first if not self.outgoing else hello),
                         self.greet_voice, self.greet_voice or None)

    # >>> first-call-wait
    async def _first_call_wait(self):
        """The first call's wait (bridge/firstcall.py): while the stt and tts
        weights are not cached, a line saying so (macOS say), then the music
        file as the track's bed, until the call can hear. Cancelled with the call."""
        render = (lambda text: apple_pcm48(text)) if os.path.exists(SAY_BIN) else None
        try:
            await firstcall.wait(
                self.voice,
                weights=lambda: hearing.weights_state(os.environ),
                probe=lambda: hearing.probe(os.environ, say=lambda _m: None).answer == "yes",
                render=render,
                load_music=lambda: firstcall.music(WAIT_MUSIC, log=log, ffmpeg=FFMPEG),
                log=log)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.voice.unhold()
            log("first-call wait failed:\n" + traceback.format_exc())
    # <<< first-call-wait

    # ---- incoming

    async def run(self):
        await self._names()
        log(f"INCOMING msg {self.msg_id} chat {self.chat_id} ({self.chat_name}) from {self.caller}, video={self.has_video}")
        loop = asyncio.get_running_loop()
        # >>> ring-first
        if RING and not await self._ring(loop):
            return
        # <<< ring-first
        pc = await self._build_pc()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=self.offer_sdp, type="offer"))
        answer = await pc.createAnswer()
        t = time.time()
        await pc.setLocalDescription(answer)       # gathers ICE before it returns
        sdp = pc.localDescription.sdp
        log(f"answer ready in {time.time()-t:.1f}s, {sum(1 for l in sdp.splitlines() if l.startswith('a=candidate'))} candidates")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "offer.sdp").write_text(self.offer_sdp)
        (self.dir / "answer.sdp").write_text(sdp)
        # >>> call-id-accept
        # The SAME bug on the answering side, measured while this fix was
        # being written:
        #     12:34:56 INCOMING msg 5608 chat 12 from <owner>
        #     12:34:58 CallEnded msg 5608 / closing msg 5608
        #     12:35:01 answer ready in 5.0s, 33 candidates
        #     12:35:01 ACCEPTED msg 5608          <- already dead for 3 s
        # Then "InvalidStateError: RTCIceTransport is closed" in launchd.log.
        # Building an answer takes ~5 s (ICE gathering); the caller gave up after 2.
        # close() sets self.pc to None, so that is the flag: do not accept a
        # call that ended while we were still answering it.
        if self.pc is None or self.closed:
            if self.pc is not None:      # >>> ring-first: close() ran before the pc existed
                await self.close(self.closed)
            log(f"NOT ACCEPTING msg {self.msg_id}: it ended while the answer "
                f"was being built ({time.time()-t:.1f}s of ICE)")
            if self.answer_go is not None:   # >>> ring-first: it rang the agent; say how it ended
                loop.run_in_executor(None, self._mail_missed,
                                     f"{self.closed or 'ended'} while connecting", time.time() - t)
            return
        # <<< call-id-accept
        msg = self.account.get_message_by_id(self.msg_id)
        await loop.run_in_executor(None, msg.accept_incoming_call, sdp)
        log(f"ACCEPTED msg {self.msg_id}")

    # >>> call-id
    def _dirname(self):
        base = f"{self._stamp}-chat{self.chat_id}"
        return base + (f"-msg{self.msg_id}" if self.msg_id else "")

    def _relabel(self):
        """An outgoing call learns its id AFTER it has made its directory."""
        want = CALLS_DIR / self._dirname()
        if want == self.dir:
            return
        try:
            if self.dir.exists():
                self.dir.rename(want)
            self.dir = want
            log(f"call dir {want.name}")
        except Exception as e:
            log(f"call dir relabel failed: {e}")

    def _live_write(self):
        # whole or not at all: a temp sibling renamed in, so a bot stopped
        # mid-write never leaves a torn file for dc-live to doubt
        part = LIVE_FILE.with_name(f".{LIVE_FILE.name}.{os.getpid()}.part")
        try:
            LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
            part.write_text(json.dumps({
                "call_id": self.msg_id, "chat_id": self.chat_id,
                "dir": self.dir.name, "since": time.time(),
                "caller": self.caller}))
            os.replace(part, LIVE_FILE)
        except Exception as e:
            log(f"live file write failed: {e}")
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass

    def _live_clear(self):
        try:
            if not LIVE_FILE.exists():
                return
            d = json.loads(LIVE_FILE.read_text())
            if int(d.get("call_id", -1)) != int(self.msg_id if self.msg_id else -2):
                return                  # a newer call owns the file; leave it
            LIVE_FILE.unlink()
        except Exception as e:
            log(f"live file clear failed: {e}")
    # <<< call-id

    # ---- outgoing

    async def dial(self):
        await self._names()
        log(f"DIALING chat {self.chat_id} ({self.chat_name})")
        loop = asyncio.get_running_loop()
        pc = await self._build_pc()
        offer = await pc.createOffer()
        t = time.time()
        await pc.setLocalDescription(offer)        # gathers ICE before it returns
        sdp = pc.localDescription.sdp
        log(f"offer ready in {time.time()-t:.1f}s, {sum(1 for l in sdp.splitlines() if l.startswith('a=candidate'))} candidates")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "offer.sdp").write_text(sdp)
        chat = self.account.get_chat_by_id(self.chat_id)
        msg = await loop.run_in_executor(None, chat.place_outgoing_call, sdp, False)
        self.msg_id = msg.id
        self._relabel()                 # >>> call-id: the dir learns the id
        self.manager.sessions[self.msg_id] = self
        log(f"RINGING msg {self.msg_id} chat {self.chat_id} - the phone rings up to 120 s")

    async def accepted(self, answer_sdp):
        log(f"ANSWERED msg {self.msg_id}")
        (self.dir / "answer.sdp").write_text(answer_sdp)
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    # ---- media

    async def _add_candidate(self, data):
        try:
            c = json.loads(data)
            if not c or not c.get("candidate"):
                return
            cand = candidate_from_sdp(c["candidate"].split(":", 1)[1])
            cand.sdpMid = c.get("sdpMid")
            cand.sdpMLineIndex = c.get("sdpMLineIndex")
            await self.pc.addIceCandidate(cand)
            log(f"added trickled candidate type={cand.type}")   # >>> ice-redact: never his address
        except Exception as e:
            # the TYPE only: a parse error can quote the candidate, port and all
            log(f"trickled candidate ignored: {type(e).__name__}")

    async def _drain(self, track):
        try:
            while True:
                await track.recv()
        except Exception:
            pass

    async def _listen(self, track):
        if EARS:
            return await self._listen_ears(track)
        resampler = AudioResampler(format="s16", layout="mono", rate=IN_RATE)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._rec_start()               # >>> recording-switch: before the first chunk
        vad = Utterances(self.dir)
        # The caller's voice while the agent talks is kept: the phone's WebRTC echo-cancels
        # the agent, and dropping frames here broke the VAD's continuity (a loopback
        # test). Barge-in works as a side effect.
        silence = np.zeros(IN_SAMPLES, dtype=np.int16)
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=0.7)
                except asyncio.TimeoutError:
                    # no packets at all (Opus DTX, or the mic stopped): the VAD
                    # still needs its 700 ms of quiet to close an utterance
                    for _ in range(int(TURN_JOIN_OPEN / PTIME)):
                        self._feed(vad, silence)
                    continue
                for f in resampler.resample(frame):
                    pcm = f.to_ndarray().reshape(-1).astype(np.int16)
                    self._feed(vad, pcm)
        except Exception as e:
            log(f"listen ended: {e.__class__.__name__}: {e}")

    # ---- the ears leg (DC_EARS=1): one Pipeline per call

    async def _listen_ears(self, track):
        """His audio into ears, 20 ms at a time; ears' events come back through
        _ears_event. With recording on, the whole call is also kept as
        caller-call.wav (and its clock as caller-call.json), so it can be
        replayed through any backend or turn stage later. With it off, the wav
        is written only for the life of the call (the speaker side-car reads it
        live) and deleted after hang-up, or never opened at all when
        recording.TRANSIENT_DURING_CALL is False."""
        from ears.cli import Pipeline   # DC_EARS=1 needs ears in this environment
        self.loop = asyncio.get_running_loop()
        self.ears_futs = []
        resampler = AudioResampler(format="s16", layout="mono", rate=IN_RATE)
        self.dir.mkdir(parents=True, exist_ok=True)
        # >>> recording-switch: kept, transient, or not written at all
        st = self._rec_start()
        rec = None
        if st.keep or recording.TRANSIENT_DURING_CALL:
            if not st.keep:
                recording.mark_transient(self.dir, "caller-call.wav is read live by the speaker side-car")
            rec = wave.open(str(self.dir / CALLER_WAV), "wb")
            rec.setnchannels(1); rec.setsampwidth(2); rec.setframerate(IN_RATE)
            if st.keep:
                self._rec_keep([CALLER_WAV, CALLER_JSON])
        self.callwav = rec
        # <<< recording-switch
        t = time.time()
        self.ears = Pipeline(EARS_BACKEND, IN_RATE, vad="auto", turns=EARS_TURNS, previews=PREVIEWS,
                             emit=self._ears_event)
        log(f"ears up in {time.time()-t:.2f}s: backend={self.ears.b.name} "
            f"vad={self.ears.edges.vad.name if self.ears.edges else 'none'} turns={EARS_TURNS} previews={PREVIEWS}")
        silence = np.zeros(IN_SAMPLES, dtype=np.int16).tobytes()
        meta = None
        clock = EarsClock(time.time(), log)   # >>> ears-clock: media time, not a fixed block
        self.wav_samples = 0              # samples in caller-call.wav, for the ear leg: where a tap starts (earleg)

        def put(pcm):
            # the one writer: the wav, ears and the ear leg never get out of step
            if self.callwav is not None:
                self.callwav.writeframes(pcm)
            self.ears.feed(pcm)
            earleg.feed(self, pcm)              # >>> ear-leg: the call as a mic
            clock.written += len(pcm) // 2
            self.wav_samples = clock.written

        def fill(n):
            for _ in range(n // IN_SAMPLES):
                put(silence)

        try:
            while True:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=EARS_WAIT)
                except asyncio.TimeoutError:
                    # no packets (Opus DTX, or the mic stopped): the audio clock
                    # must still move, or no silence ever ends his turn
                    fill(clock.timeout(time.time()))
                    continue
                fill(clock.frame(frame, time.time()))
                if meta is None:
                    # the recording's audio second 0 on the wall clock: a tap log
                    # (a live-mic test) lines up with caller-call.wav through it.
                    # Any pre-roll silence (timeouts before this first frame) is
                    # already in the wav, so second 0 is that much EARLIER than now.
                    meta = dict(start_epoch=clock.start_epoch,
                                preroll_s=round(clock.written / IN_RATE, 3), rate=IN_RATE,
                                backend=self.ears.b.name, turns=EARS_TURNS, previews=PREVIEWS)
                    if st.keep:          # the clock serves the kept wav only
                        (self.dir / CALLER_JSON).write_text(json.dumps(meta) + "\n")
                for f in resampler.resample(frame):
                    pcm = f.to_ndarray().reshape(-1).astype(np.int16).tobytes()
                    # >>> recording-ears-in
                    if _recorder is not None:
                        _recorder.write_in(pcm)
                    # <<< recording-ears-in
                    put(pcm)
        except Exception as e:
            log(f"listen ended: {e.__class__.__name__}: {e}")
        finally:
            if self.callwav is not None:
                self.callwav.close()
                self.callwav = None
            if meta and st.keep:
                # end_epoch - start_epoch - audio_s is mostly hang-up TEARDOWN (his
                # last frame to here), not drift: measure drift from last_frame_epoch
                meta.update(end_epoch=time.time(), last_frame_epoch=clock.last_frame,
                            audio_s=round(clock.written / IN_RATE, 3))
                (self.dir / CALLER_JSON).write_text(json.dumps(meta) + "\n")
            await self._ears_close()

    async def _ears_close(self):
        """Flush ears (its last turn comes out here) and wait for the mail."""
        p, self.ears = getattr(self, "ears", None), None
        if p is None:
            return
        await asyncio.get_running_loop().run_in_executor(None, p.close)
        await asyncio.sleep(0)                    # let call_soon_threadsafe land
        futs, self.ears_futs = self.ears_futs, []
        if futs:
            await asyncio.gather(*futs, return_exceptions=True)

    def _ears_event(self, e):
        """Called by ears from any thread (pipe.close runs in an executor)."""
        self.loop.call_soon_threadsafe(self._ears_dispatch, e)

    def _ears_dispatch(self, e):
        if e.type == "final":
            if e.text.strip():
                log(f"HEARD seg {e.seg} [{e.start:.2f}-{e.end:.2f}] lag {(e.t or 0) - e.end:.2f}s: {one_line(e.text)}")
                self.heard.partial(flat(e.text), seg=e.seg)
        elif e.type == "vad":
            self.speaking = bool(e.get("speech"))
            # >>> recording-ears-speech
            log("user_speech_started" if self.speaking else "user_speech_stopped")
            # <<< recording-ears-speech
        elif e.type == "turn":
            self._rec_check()           # >>> recording-switch: a turn is a unit
            x = e.extra
            log(f"TURN (ears {x.get('reason')}, wait {x.get('wait_s')}, by {x.get('by')}"
                f"{', partial tail' if x.get('partial_tail') else ''}): {one_line(e.text)}")
            self.ears_futs.append(asyncio.ensure_future(
                self._mail_turn(e.text, detector=f"ears-{x.get('reason') or 'turn'}")))
        elif e.type == "preview" and PREVIEWS:
            p = types.SimpleNamespace(text=e.text, turn=e.get("turn"), seq=e.get("seq"), at=e.end, turn_start=e.start)
            self.ears_futs.append(asyncio.ensure_future(self._preview_out(p)))
        elif e.type == "error":
            log(f"ears error: {e.get('message')}{' (fatal)' if e.get('fatal') else ''}")
        elif e.type == "ready":
            log(f"ears ready: {e.get('backend')} {e.get('model')} load {e.get('load_s')}s")

    def _feed(self, vad, pcm):
        """One 20 ms frame into the VAD. HOLD THE TURN while he is talking:
        before this, the turn timer was cancelled only when his NEXT chunk
        CLOSED, so any phrase longer than ~1.9 s after a pause was cut in two
        and mailed as two turns - 227 of 505 turns (45%) on the seven recorded
        calls went out while he was still speaking (seen in a replay)."""
        was = vad.in_speech
        path = vad.feed(pcm)
        self.speaking = vad.in_speech
        if path:
            self._chunk(path)
        elif HOLD_TURN and vad.in_speech and not was:
            self._turn_cancel()          # he started again: the turn stays open
        elif HOLD_TURN and was and not vad.in_speech:
            self._turn_arm()             # a blip too short to be a chunk: resume the wait

    # ---- chunks in, one turn out

    def _chunk(self, path):
        """A chunk just closed: transcribe it now, and hold the turn open."""
        self.chunk_n += 1
        seq = self.chunk_n
        self.turn_pending.add(seq)
        # ms, not the log's 1 s stamp: measuring his pauses off the log gave 14
        # negative gaps out of 42.
        log(f"chunk {seq} {path.name} closed t={time.time():.3f}")
        self._turn_cancel()
        # >>> recording-switch: a chunk is a unit. Off: STT reads it, then it goes.
        kept = self._rec_check().on
        if kept:
            self._rec_keep([path.name])
        else:
            recording.mark_transient(self.dir, "a chunk is kept only until speech-to-text returns")
        self.tasks.append(asyncio.ensure_future(self._heard(path, seq, transient=not kept)))
        # <<< recording-switch

    def _turn_cancel(self):
        if self.turn_timer and not self.turn_timer.done():
            self.turn_timer.cancel()
        self.turn_timer = None

    def _turn_arm(self):
        """Start the join window. If nothing more arrives inside it, he has
        finished speaking; a chunk that ended mid-thought waits longer."""
        if self.turn_pending:
            return                      # whisper is still holding a chunk
        if HOLD_TURN and self.speaking:
            return                      # he is talking again; his next chunk re-arms
                                        # (the race: he started while whisper held
                                        # the last chunk, then _heard armed mid-speech)
        texts = [t for _, t in sorted(self.turn_parts) if t]
        wait = TURN_JOIN_OPEN if (texts and mid_thought(texts[-1])) else TURN_JOIN
        self._turn_cancel()
        self.turn_timer = asyncio.ensure_future(self._turn_after(wait))

    async def _turn_after(self, wait):
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        try:
            await self._turn_out()
        except Exception:
            log("turn failed:\n" + traceback.format_exc())

    async def _turn_out(self):
        """Mail the assembled turn: his sentence whole, in one message."""
        parts, self.turn_parts = sorted(self.turn_parts), []
        # flat: whisper's own line breaks made the subject and the
        # transcript ragged for no gain. Flattened, never cut.
        text = flat(" ".join(t for _, t in parts if t))
        if not text:
            return
        log(f"TURN ({len(parts)} chunk{'' if len(parts) == 1 else 's'}): {one_line(text)}")
        self.previews.on_turn(text, time.time())
        await self._mail_turn(text, detector="turn-join")

    async def _mail_turn(self, text, detector=None):
        """One turn to her mailbox, superseding this turn's last preview.

        It logs the box it went to (`TURN <n> mailed to <box>`),
        then exactly one verdict later: `replied` (a reply line arrived in the
        call, see replied()) or `unknown`. A send the MTA refused is `NOT
        mailed ... taken=no`, the one hard no the bridge can measure, and the
        caller hears UNDELIVERED_LINE once for it (_say_undelivered)."""
        text = flat(text)           # the whole turn: mail, heard log, transcript
        if not text:
            return
        if no_real_word(text):
            # ".", "Music.", "Um" would wake the seat. Not mailed, and no
            # TURN number taken; the heard log keeps it, the log says so.
            self.heard.turn(text, detector=detector)
            log(f"NOISE {detector or 'turn'} after TURN {self.turn_n}: no real word "
                f"| dropped: {one_line(text)}")
            return
        # The number and the Message-ID are taken HERE, before the send: ears
        # turns are separate futures, so two sends can finish out of order,
        # and TURN <n> must stay in the order he spoke them.
        self.turn_n += 1
        n = self.turn_n
        mid = (f"call-turn-{PROFILE + '-' if PROFILE else ''}{self.chat_id}-"
               f"{int(time.time()*1000)}@{seatmail.id_host()}")
        self.heard.turn(text, detector=detector)
        last_preview, self.preview_id = self.preview_id, None
        self.lines.append((time.time(), self.caller, text))
        loop = asyncio.get_running_loop()

        def send():
            cc, why = self.guest_list(), []
            return cc, _hooks.mail_in(self.chat_name, self.chat_id, self.caller,
                                      f"(on the call) {text}",
                                      msgid=mid,
                                      in_reply_to=self.root_id,
                                      subject=f"{self.caller}: {text[:44]}",   # display only: the body is whole
                                      reply_to=dc_env.ADDR,
                                      cc=cc,
                                      supersedes=last_preview,
                                      headers={"Medium": "voice"},
                                      call=self.msg_id,
                                      refusal=why), why
        fut = loop.run_in_executor(None, send)
        try:
            cc, ok, why = await asyncio.shield(fut)
        except asyncio.CancelledError:
            # The join timer was cancelled MID-SEND (_turn_cancel runs on every
            # chunk close and speech start, and at hang-up). The executor sends
            # anyway, so the box line is written when the send lands.
            fut.add_done_callback(lambda f: self._sent_late(n, mid, f))
            raise
        except Exception as e:
            self._not_sent(n, e)
            raise
        self._sent(n, mid, cc, ok, why)

    def _not_sent(self, n, e):
        log(f"TURN {n} NOT mailed to {dc_env.MAILTO}: {type(e).__name__}: "
            f"{one_line(str(e), 120)}, taken=no")
        self._say_undelivered(n)

    def _say_undelivered(self, n):
        """Turn n's mail did not go: say UNDELIVERED_LINE on the call, ONCE for
        the turn. A turn is one mail however many chunks it joined (_turn_out),
        and the spool's later retries never come back here, so the caller hears
        it once per undelivered turn, never per chunk or per retry. Only on a
        live call (voicemail too: the caller expects the message to reach the
        agent); a call that is closing or closed says nothing. Previews never
        come here: they do not go through _sent."""
        if not getattr(self, "pc", None) or getattr(self, "closed", None):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:                # no calls loop here: nothing can be said
            return
        log(f"UNDELIVERED box={dc_env.MAILTO} turn={n}: said on the call")
        _keep(asyncio.ensure_future(self.speak(UNDELIVERED_LINE, None, "bridge")))

    def _sent_late(self, n, mid, f):
        """A send whose waiter was cancelled has landed: log it as _mail_turn would."""
        if f.cancelled():
            return
        if f.exception() is not None:
            self._not_sent(n, f.exception())
            return
        self._sent(n, mid, *f.result())

    def _sent(self, n, mid, cc, ok, why=()):
        if ok is False:      # `is False`: a hook that returns None has sent it
            # the refusal, as message send gave it (mail_in's refusal=), or "refused"
            log(f"TURN {n} NOT mailed to {dc_env.MAILTO}: {why[0] if why else 'refused'}, taken=no")
            self._say_undelivered(n)
            return
        self.awaiting = tuple(sorted((*self.awaiting, (n, mid, time.time()))))
        to = dc_env.MAILTO + (f" cc {','.join(cc)}" if cc else "")
        log(f"TURN {n} mailed to {to}: msg {mid}, taken=pending")
        if self.closed and self.pc is None:
            self._unanswered("hang-up")   # landed after close(): no reply can reach it now
            return
        self._noreply_arm(n)

    # ---- the no-reply watchdog

    def _noreply_arm(self, n):
        """Start the no-reply window for turn n, which was just mailed. ONE
        timer per call: a new turn replaces the old one, so two quick turns
        never mean two holding lines. Not in voicemail (its greeting already
        said nobody can pick up), not once the call is closing, and not for an
        older turn whose send landed late: the newer turn's timer stands."""
        if NO_REPLY_S <= 0 or not self.pc or self.voicemail:
            return
        if not self.awaiting or self.awaiting[-1][0] != n:
            return
        self._noreply_cancel()
        self.noreply_task = asyncio.ensure_future(self._noreply_after(n, NO_REPLY_S))

    def _noreply_cancel(self):
        task, self.noreply_task = self.noreply_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _noreply_after(self, n, wait):
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        # Let go of the timer FIRST: a reply that lands while the holding line
        # is in TTS must not cancel the holding line.
        if self.noreply_task is asyncio.current_task():
            self.noreply_task = None
        if not any(t[0] == n for t in self.awaiting):
            return
        box = f"box={dc_env.MAILTO} turn={n} after={wait:g}s"
        # the older turns fold into this one's wait; only n stays, for a late reply
        older = tuple(t for t in self.awaiting if t[0] < n)
        self.awaiting = tuple(t for t in self.awaiting if t[0] >= n)
        if self.speaking:
            # he is talking again: his next turn is forming and gets its own window
            log(f"NO-REPLY {box}: caller talking, no holding line")
        else:
            log(f"NO-REPLY {box}: holding line spoken")
            _keep(asyncio.ensure_future(self.speak(HOLDING_LINE, None, "bridge")))
        for m, _mid, _t in older:
            log(f"TURN {m} taken=unknown: no reply before the next turn")

    def replied(self, via):
        """A reply line arrived in this call: `via` is mouth, dc-say or mail.
        It is the one fact the bridge can measure about whether anyone took a
        turn, so it resolves EVERY awaiting turn: which turn a reply answers
        would be a guess. Runs on the calls loop, never from speak(): the
        greeting, the invite notice and a holding line are the bridge's own
        words, not replies."""
        self._noreply_cancel()
        now = time.time()
        for n, _mid, t in self.awaiting:
            log(f"TURN {n} taken=replied via={via} after {now - t:.1f}s")
        self.awaiting = ()

    def _unanswered(self, before):
        """Every awaiting turn is resolved `unknown`: no reply came before
        `before`. Not `no`: the agent may have read it and chosen silence."""
        for n, _mid, _t in self.awaiting:
            log(f"TURN {n} taken=unknown: no reply before {before}")
        self.awaiting = ()

    async def _heard(self, path, seq, transient=False):
        """transient: recording is off, so the chunk is deleted the moment
        transcribe returns (or fails, or the call ends under it)."""
        loop = asyncio.get_running_loop()
        t = time.time()
        try:
            text = await loop.run_in_executor(None, _hooks.transcribe, str(path))
        except Exception as e:
            log(f"transcribe failed on {path.name}: {e.__class__.__name__}: {e}")
            text = ""
        finally:
            if transient:                # >>> recording-switch
                pathlib.Path(path).unlink(missing_ok=True)
        self.turn_pending.discard(seq)
# >>> echo-patch heard
        why = self.echo.check(text) if text else None
        if why:
            # never silently: a gate nobody has seen fail is a decoration
            log(f"ECHO {path.name}: {why} | dropped: {one_line(text)}")
            self.turn_parts.append((seq, ""))   # keeps his turn OPEN
            self._turn_arm()
            return
        if (BARGE_IN and text and self.voice.speaking()
                and (why.score if why else 0.0) < BARGE_IN_MAX_ECHO
                and interrupts(text)):
            # He talked over us and it was NOT our own echo - stop.
            # Driven by the transcript, not the VAD, on purpose: 34 of
            # 39 echo chunks on the recorded call arrived while we were
            # still speaking, so a VAD trigger would cut us off on our
            # own voice. Costs ~0.65s (chunk quiet + transcribe).
            secs, where = self.voice.cut(), self.echo.cut()
            log(f"BARGE-IN on {path.name}: {where} | {secs:.1f}s dropped")
            self.lines.append((time.time(), "(barge-in)", str(where)))
            if BARGE_IN_KEEP_TEXT:
                # He asked for a stop button, not a delete key. Cutting
                # the AUDIO is the feature; losing the WORDS is not.
                # Measured: with barge-in on, 60.8s of 150.4s
                # spoken never reached him - 40% - because lines land
                # 20-34s late and are cut 1.2s in, every time. Posting
                # the remainder to the chat keeps the stop button and
                # keeps the content; he reads what he did not hear.
                #
                # ONLY the sentence in flight. The lines still queued
                # behind it are dropped silently: posting them too would
                # land a wall of text on the caller as punishment for
                # interrupting, and if the queue had mattered it would
                # have been said sooner. (Measured on a call.)
                rest = where.unsaid
                if rest and rest.strip():
                    try:
                        post_to_chat(self.account, self.chat_id,
                                     f"(cut off) {rest}", "barge-in remainder")
                    except Exception as e:
                        log(f"barge-in: could not post the remainder: {e}")
# <<< echo-patch heard
        if not text or not_speech(text):
            # a cough is not the end of his sentence: it keeps the turn OPEN
            # (five of them paged her separately on the first call)
            log(f"heard {path.name}: {text!r} - not words, turn kept open")
            self.turn_parts.append((seq, ""))
        else:
            log(f"HEARD {path.name} in {time.time()-t:.1f}s: {one_line(text)}")
            self.turn_parts.append((seq, text))
            self.heard.partial(flat(text), chunk=seq)
            if PREVIEWS:
                p = self.previews.on_phrase(text, time.time())
                if p:
                    await self._preview_out(p)
        self._turn_arm()

    async def _preview_out(self, p):
        """Mail the turn so far as a PREVIEW (never a turn): the whole text
        so far, superseding the last preview of this turn."""
        text = flat(p.text)
        mid = (f"call-preview-{PROFILE + '-' if PROFILE else ''}{self.chat_id}-"
               f"{int(time.time()*1000)}@{seatmail.id_host()}")
        prev, self.preview_id = self.preview_id, mid
        log(f"PREVIEW t{p.turn}.{p.seq} ({len(text.split())} words, turn open "
            f"{p.at - p.turn_start:.1f}s): {one_line(text)}")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: _hooks.mail_in(
                self.chat_name, self.chat_id, self.caller,
                f"(on the call, PREVIEW - {self.caller} is still talking; "
                f"think and look things up, do not reply) {text}",
                msgid=mid, in_reply_to=self.root_id,
                subject=f"{self.caller} (preview): {text[:34]}",   # display only
                reply_to=dc_env.ADDR,
                supersedes=prev,
                headers={"X-Call-Preview": f"{p.turn}.{p.seq}",
                         "Medium": "voice"},
                call=self.msg_id))

    async def speak(self, text, voice=None, who=None, fallback=True, floor=False):
        """Say TEXT into the call. fallback: a line TTS could not say is sent
        as chat text instead (False: it is only logged). floor: a REPLY (a
        dc-say, a mail into the call): it does not start while the caller is
        speaking (bridge/floor.py). The bridge's own lines pass False.

        The whole reply is ONE line on the track (PipVoiceTrack.line), and it
        takes its place as it arrives, here: a line already speaking (the
        mouth, another reply) finishes first, and nothing lands between this
        reply's sentences. A cut ends it: no later sentence is rendered or said.
        Never waits for the track or for playback.
        Returns what happened: {"n": sentences, "failed": how many TTS could
        not say, "error": the first failure, "cut": cut short}. Every sentence
        not failed and not cut was rendered and handed to the call."""
        voice, who = voice or dc_env.VOICE, who or dc_env.AGENT_NAME   # >>> identity
        loop = asyncio.get_running_loop()
        line = self.voice.line(who, floor=floor)
        # >>> said-rows: one utt per line, like the mouth. A sentence starts
        # playing when it reaches the track, behind what is queued there, so
        # the line's end is estimated then; `said` is written at that end.
        utt, played, failed, started = uuid.uuid4().hex[:12], 0.0, 0, False
        # <<< said-rows
        parts, error = sentences(text), None

        def queued(s):
            def on_queue(start_at, dur):       # the sentence is on the track now
                nonlocal played, started
                # >>> said-rows
                self._play_end = max(getattr(self, "_play_end", 0.0), start_at) + dur
                played += dur
                if not started:
                    started = True
                    self.heard.saying(text, utt=utt, voice=voice, who=who)
                # <<< said-rows
# >>> echo-patch speak
                self.echo.spoken(s, dur, start=start_at)
# <<< echo-patch speak
            return on_queue

        def over():                            # all on the track, or cut
            # >>> said-rows
            if not started:
                return
            cut = line.was_cut
            fields = dict(utt=utt, voice=voice, who=who, played_s=round(played, 2),
                          dur_s=round(played, 2), cut=cut,
                          reason="cut" if cut else "tts-failed" if failed else "done",
                          text_played_est=text)
            loop.call_later(max(0.0, getattr(self, "_play_end", 0.0) - time.time()),
                            lambda: self.heard.said(**fields))
            # <<< said-rows

        try:
            for s in parts:
                if line.was_cut:
                    break
                try:
                    t = time.time()
                    pcm = await loop.run_in_executor(None, tts_pcm48, s, voice)
                    if line.was_cut:       # cut while it rendered: it is not said
                        break
                    line.say(pcm, queued(s))
                    self.lines.append((time.time(), who, s))
                    log(f"SPOKE {len(pcm)/OUT_RATE:.1f}s (tts {time.time()-t:.1f}s, {voice}): {s[:60]}")
                except Exception as e:
                    log(f"tts failed: {e}")
                    error = error or str(e)
                    if not fallback:
                        failed += 1
                        continue
                    # >>> tts-fallback
                    # He must still receive the words. An outbox job is the
                    # lock-safe path (the same one dc-say uses); the bot's own
                    # runner sends it as chat text.
                    try:
                        outbox = dc_env.path("OUTBOX", "outbox")   # this profile's, never another's
                        outbox.mkdir(parents=True, exist_ok=True)
                        # no_speak: these are the words that could NOT be said;
                        # speaking them into the live call re-fed the failure,
                        # marker and all ("(unspoken, TTS failed)" voiced in
                        # a demo).
                        # Chat text only. Same key as the original bridge's fix.
                        job = {"chat_id": int(self.chat_id),
                               "text": f"(unspoken, TTS failed) {s}",
                               "no_speak": True}
                        (outbox / f"{time.time():.6f}.json").write_text(
                            json.dumps(job), encoding="utf-8")
                        log(f"tts fallback: sent as text: {s[:60]}")
                    except Exception:
                        log("tts fallback FAILED too - the line is lost")
                    # <<< tts-fallback
                    failed += 1   # >>> said-rows
        finally:
            line.end(over)         # the next line may have the track: never left holding it
        return {"n": len(parts), "failed": failed, "error": error, "cut": line.was_cut}

    # ---- the end

    async def close(self, why):
        # >>> ring-first: a call can end before it has a pc (while it rings)
        if not self.closed:
            self.closed = why or "closed"
        if self.answer_go is not None:  # stop ringing now, not at RING_WAIT
            self.answer_go.set()
        # <<< ring-first
        if self.wait_task is not None:  # >>> first-call-wait: the music stops with the call
            self.wait_task.cancel()
        self._roots_end()               # the turnover clock starts
        if self.pc is None:
            return
        pc, self.pc = self.pc, None
        log(f"closing msg {self.msg_id}: {why}")
        voice = getattr(self, "voice", None)
        if callable(getattr(voice, "drop", None)):
            voice.drop()                # no line may hold the track of a call that is gone
        self._live_clear()              # >>> call-id: before anything can block
        set_call_id(None)               # >>> call-id-log: stop stamping
        earleg.end(self, why)           # >>> ear-leg: every listener hears the call end
        # >>> recording-close
        if _recorder is not None:
            _recorder.close()
            set_recorder(None)
        # <<< recording-close
        self._turn_cancel()
        self._noreply_cancel()          # >>> no-reply
        if EARS:
            try:
                await self._ears_close()   # his last words (ears' final turn), before the tasks die
            except Exception:
                log("ears close failed:\n" + traceback.format_exc())
        if self.turn_pending:
            log(f"hang-up with {len(self.turn_pending)} chunk(s) still in whisper")
        if self.turn_parts:
            try:
                await self._turn_out()   # his last words, before the tasks die
            except Exception:
                log("last turn failed:\n" + traceback.format_exc())
        self._unanswered("hang-up")      # >>> turn-log: after the last turn is mailed
        for t in self.tasks:
            t.cancel()
        try:
            await pc.close()
        except Exception:
            pass
        self.manager.forget(self)
        if self.connected:
            self.heard.event("call-ended", reason=str(why))
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.transcript_out)
            except Exception:
                log("transcript failed:\n" + traceback.format_exc())
        self._rec_end()                  # >>> recording-switch: after the last turn

    # >>> recording-switch
    # A call's own view of the switch: read at its start, and again at every
    # unit (a chunk, a turn) and at hang-up. On at the start keeps the
    # whole-call files; off at any later read takes them back at once (off
    # wins), and the call's audio is deleted RECORD_DRAIN s after hang-up. A
    # switch turned on mid-call keeps chunks from the next one; the whole-call
    # files start at the next call.
    rec_state = None      # types.SimpleNamespace(keep=bool), set by _rec_start
    rec_drain = None      # the hang-up drain task, while it runs
    callwav = None        # the ears path's open caller-call.wav, or None

    def _rec_start(self):
        """Once a call: read the switch, log it, and open the Recorder if it is on."""
        if self.rec_state is not None:
            return self.rec_state
        sw = rec_switch()
        r = sw.record()
        self.rec_state = types.SimpleNamespace(keep=r.on)
        log(f"recording: {recording.line(sw)}")
        if r.on:
            log(f"recording: on: {recording.ALL_VOICES}")
            if _recorder is None:
                try:
                    self.dir.mkdir(parents=True, exist_ok=True)
                    set_recorder(Recorder(self.dir))
                    log(f"recording_started {self.dir.name} in+out")
                    self._rec_keep(Recorder.NAMES)
                except Exception:
                    log("recording: could not start")
        return self.rec_state

    # >>> recording-notice
    # Rules 10-11: the call says whether it is kept. record_notice posts ONE
    # line into the call's chat at connect, on every call, worded by the
    # call's own switch; record_spoken_notice speaks SPOKEN_NOTICE before the
    # greeting, only on a kept call. Each has its own setting, default on.
    # The chat post's outcome is kept (rec_chat). On a kept call where neither
    # notice reached the caller, the notice is posted ONCE as chat text before
    # the greeting; a chat line refused by channels.txt, or not confirmed in
    # time, is not chased. Nothing here changes whether the call is kept.
    rec_noticed = False   # the chat line went out (or was decided against)
    rec_spoken = False    # the spoken notice was said (or decided against)
    rec_chat = None       # the chat line's future -> (outcome, detail); None = not attempted
    rec_notice_text = None   # the chat line's words for this call

    def _rec_notice_post(self):
        """Once a call, at connect: the chat line, if record_notice is on. Sent
        off the loop, so it never holds up the greeting. Its outcome is kept as
        rec_chat, a future of ("posted" | "refused" | "raised", detail), read
        only if the spoken notice does not reach the caller; a failure is logged."""
        if self.rec_noticed:
            return
        self.rec_noticed = True
        sw = rec_switch()
        st = self._rec_start()
        n = sw.notice()
        # the words, kept for the text fallback: it never re-reads the switch
        text = self.rec_notice_text = recording.notice_text(st.keep, sw.days().value)
        log(f"recording notice: {recording.notice_line(sw)}")
        if not n.on:
            return                              # rec_chat stays None: not attempted
        account, chat_id = self.account, self.chat_id

        def post():
            try:
                return ("posted", "") if post_to_chat(account, chat_id, text, "recording notice") \
                    else ("refused", "")        # post_to_chat logged the refusal
            except Exception as e:
                log(f"recording notice: could not post to chat {chat_id}: {e}")
                return ("raised", str(e))
        loop = None
        try:
            loop = asyncio.get_running_loop()
            self.rec_chat = _keep(loop.run_in_executor(None, post))
        except Exception as e:
            log(f"recording notice: could not post to chat {chat_id}: {e}")
            if loop is not None:                # one shape for the reader: a done future
                self.rec_chat = loop.create_future()
                self.rec_chat.set_result(("raised", str(e)))

    async def _rec_chat_outcome(self):
        """The chat line's outcome, waited for at most REC_NOTICE_TEXT_WAIT s:
        ("off" | "posted" | "refused" | "raised" | "unconfirmed", detail). The wait
        never cancels the post: a post still running may land."""
        if self.rec_chat is None:
            return ("off", "")
        try:
            return await asyncio.wait_for(asyncio.shield(self.rec_chat),
                                          timeout=REC_NOTICE_TEXT_WAIT)
        except asyncio.TimeoutError:
            return ("unconfirmed", "")
        except Exception as e:                  # post() returns; this is only a guard
            return ("raised", str(e))

    async def _rec_notice_as_text(self):
        """Post the notice ONCE as chat text, through the chat line's own path
        (channels.txt asks). Waits at most REC_NOTICE_TEXT_WAIT s. -> the log's tail."""
        text = self.rec_notice_text or recording.SPOKEN_NOTICE
        try:
            loop = asyncio.get_running_loop()
            fut = _keep(loop.run_in_executor(None, post_to_chat, self.account, self.chat_id,
                                             text, "recording notice"))
            sent = await asyncio.wait_for(asyncio.shield(fut), timeout=REC_NOTICE_TEXT_WAIT)
        except asyncio.TimeoutError:
            return f"text not confirmed within {REC_NOTICE_TEXT_WAIT:g} s; the call is still kept"
        except Exception as e:
            return f"NOT delivered: the text could not be sent: {e}; the call is still kept"
        if not sent:
            return "NOT delivered: the text was refused by channels.txt; the call is still kept"
        return "delivered as text"

    async def _rec_notice_speak(self):
        """Once a call, before the greeting, only on a kept call: the notice, told
        once. If record_spoken_notice is on, the spoken notice is said. If it was not
        delivered (TTS failed, speak() raised, or a barge-in cut it) or is off, the
        chat line's outcome decides: posted, the caller was told; off or raised, the
        notice is posted once as chat text; refused by channels.txt or not confirmed
        in time, no text is sent (it would be refused too, or could duplicate). Each
        case where a notice did not get through logs ONE line. Both notices off by
        setting: nothing, the operator's choice. The call stays kept either way."""
        if self.rec_spoken:
            return
        self.rec_spoken = True
        st = self._rec_start()
        if not st.keep:
            return
        spoken_on = rec_switch().spoken_notice().on
        why = None
        if spoken_on:
            try:
                out = await self.speak(recording.SPOKEN_NOTICE, fallback=False)
                if out["failed"]:
                    why = out["error"] or "tts failed"
                elif out["cut"]:
                    why = "cut short"         # a one-sentence notice cut part-way is not a notice
            except Exception as e:
                why = str(e) or type(e).__name__
            if why is None:
                return                          # said: told
        elif self.rec_chat is None:
            return                              # both off: the operator's choice
        head = f"the spoken notice failed: {why}" if spoken_on else "the spoken notice is off"
        chat, detail = await self._rec_chat_outcome()
        if chat == "posted":
            if spoken_on:
                log(f"recording notice: {head}; told by the chat line")
            return
        if chat == "refused":
            log(f"recording notice: {head}; NOT delivered: the chat line was refused by "
                "channels.txt, so no text was sent; the call is still kept")
            return
        if chat == "unconfirmed":
            log(f"recording notice: {head}; the chat line was not confirmed within "
                f"{REC_NOTICE_TEXT_WAIT:g} s, so no text was sent; the call is still kept")
            return
        lead = f"the chat line could not be sent: {detail}; " if chat == "raised" else ""
        log(f"recording notice: {head}; {lead}{await self._rec_notice_as_text()}")
    # <<< recording-notice

    def _rec_keep(self, names):
        sw = rec_switch()
        try:
            recording.keep(self.dir, names, sw.record(), sw.days())
        except Exception as e:
            log(f"recording: could not note {', '.join(names)} as kept: {e}")

    def _rec_check(self):
        """At a unit: the switch now. Turned off since the call started: the
        whole-call audio stops being kept, at once."""
        st = self._rec_start()
        sw = rec_switch()
        r = sw.record()
        if st.keep and not r.on:
            st.keep = False
            log(f"recording: turned off mid-call ({recording.line(sw)}): "
                f"this call's audio is deleted at hang-up")
            if _recorder is not None:
                _recorder.close()
                set_recorder(None)
            recording.unkeep(self.dir, (*Recorder.NAMES, CALLER_WAV, CALLER_JSON))
            recording.mark_transient(self.dir, "recording was turned off during the call")
            if not recording.TRANSIENT_DURING_CALL and self.callwav is not None:
                self.callwav.close()
                self.callwav = None
        return r

    def _rec_end(self):
        """Hang-up: the switch once more, then the transient audio's drain."""
        if self.rec_state is None:
            return                       # no media ever started: no audio
        try:
            self._rec_check()
        except Exception:
            log("recording: hang-up check failed:\n" + traceback.format_exc())
        # paths.exists, not Path.exists: on 3.14 an unsearchable call dir would read
        # "unmarked", skip the drain and leave its transient audio in silence
        if paths.exists(self.dir / recording.TRANSIENT_MARK):
            self.rec_drain = _keep(asyncio.ensure_future(self._rec_drain()))
        else:
            _rec_prune()

    async def _rec_drain(self):
        await asyncio.sleep(recording.RECORD_DRAIN)
        try:
            gone = recording.clean(self.dir, log=_rec_log)
            log(f"recording: off: {len(gone)} audio file(s) deleted from {self.dir.name}, "
                f"{recording.RECORD_DRAIN:g} s after hang-up")
        except Exception:
            log("recording: hang-up delete failed:\n" + traceback.format_exc())
        _rec_prune()
    # <<< recording-switch

    def _roots_end(self):
        """The chat's issued roots start their TURNOVER_S when its LAST call
        ends: no other call in this chat is live or ringing."""
        others = [o for o in list(getattr(self.manager, "sessions", {}).values())
                  if o is not self and o.chat_id == self.chat_id
                  and not o.closed and (o.pc or o.ringing)]
        if not others:
            ISSUED.end(self.chat_id)

    def transcript_text(self):
        t0 = time.strftime("%H:%M", time.localtime(self.started or time.time()))
        t1 = time.strftime("%H:%M")
        mins = int((time.time() - (self.started or time.time())) // 60)
        head = f"Call {time.strftime('%a %Y-%m-%d')} {t0}-{t1} ({mins} min) - {self.caller} and {dc_env.AGENT_NAME}"
        body = "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(t))} {who}: {text}"
                         for t, who, text in self.lines)
        return head, body or "(nothing transcribed)"

    def transcript_out(self):
        """At hang-up: the transcript closes the mail thread; a copy into the
        chat (passive, both sides, on the phone) only when POST_TRANSCRIPT."""
        head, body = self.transcript_text()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "transcript.md").write_text(f"# {head}\n\n{body}\n")
        if POST_TRANSCRIPT:
            try:
                if post_to_chat(self.account, self.chat_id, f"{head}\n\n{body}", "transcript post"):
                    log(f"TRANSCRIPT posted to chat {self.chat_id}: {len(self.lines)} lines")
            except Exception as e:
                log(f"transcript to chat failed: {e}")
        _hooks.mail_in(self.chat_name, self.chat_id, "call", f"{head}\n\n{body}",
                       in_reply_to=self.root_id, cc=self.guest_list(),
                       subject=f"TRANSCRIPT {len(self.lines)} lines, {mins_word(self.started)}")


def mins_word(started):
    m = int((time.time() - (started or time.time())) // 60)
    return f"{m} min"


# ------------------------------------------------------------- the manager

def pack_gone(what, e):
    """One plain line for a call that reached a pack module gone from disk.

    The BACKSTOP. bot.py refuses a call before it is queued when
    media.gone() says the pack has left. A removal between that check and the
    reach, or a lazy import deeper in a live call, lands here instead. It is
    an ImportError, never a traceback: what is missing, and how to fetch it."""
    names = media.gone() or [getattr(e, "name", None) or str(e)]
    return f"{what}: the media pack is gone from under this bot ({e}). {media.calls_refused(names)}"


class CallManager:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.sessions = {}          # msg_id -> CallSession
        self.thread = threading.Thread(target=self._run, name="calls", daemon=True)

    def start(self):
        self.thread.start()
        # >>> ring-first: the mode, once, in the log
        mode = f"ring (mail the agent, {RING_WAIT:.0f} s, then voicemail)" if RING else "auto"
        if dc_env.ANSWER not in ("auto", "ring"):
            mode += f" - DC_ANSWER={dc_env.ANSWER!r} is not auto or ring"
        for bad in dc_env.BAD:
            mode += f" - {bad}"
        log(f"incoming calls answer: {mode}")
        log(f"no-reply watchdog: {NO_REPLY_S:g}s" if NO_REPLY_S > 0 else "no-reply watchdog: off")
        log(Floor(lambda: False, FLOOR_GAP, FLOOR_CAP).line())
        MailLeg(self).start()          # the inbound leg: mail -> a live call
        # >>> mouth-leg: the mouth speaks into a live call (call:<N>)
        from sessionphone.bridge import mouthleg
        mouthleg.start(self, log)
        # <<< mouth-leg
        # >>> ear-leg: the ears open a live call like a mic (call:<N>)
        earleg.start(self, log)
        # <<< ear-leg

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def incoming(self, account, event):
        msg_id, chat_id = int(event.msg_id), int(event.chat_id)
        # Who may ring this bot: an allowlist, on EVERY profile, failing CLOSED: an
        # allowlist of approved callers. Until then a call was refused
        # only on a named profile and only from a contact request - so any accepted chat, or
        # anyone at all on the default profile, got a Claude Code with a shell on the line.
        # A caller is the KEY a callers.txt line is pinned to, not the address (keygate).
        v = keygate.caller(account, msg_id, CALLERS_FILE, log)
        if not v:
            log(f"REFUSED incoming call from {v.reason} (chat {chat_id}; {CALLERS_HELP})")
            return
        if PROFILE:
            # a public relay: a STRANGER's call is never answered. Only a
            # chat already accepted may ring the bot.
            try:
                info = account.get_chat_by_id(chat_id).get_basic_snapshot()
                if getattr(info, "is_contact_request", False):
                    log(f"REFUSED incoming call from a contact request, chat {chat_id}")
                    return
            except Exception:
                log("incoming: could not read the chat - refusing the call:\n"
                    + traceback.format_exc())
                return
        s = CallSession(self, account, chat_id, msg_id=msg_id,
                        offer_sdp=event.place_call_info,
                        has_video=bool(getattr(event, "has_video", False)))
        self.sessions[msg_id] = s

        async def go():
            try:
                await s.run()
            except ImportError as e:
                log(pack_gone("session failed", e))     # the bot's preflight missed it: no traceback
                await s.close("setup failed")
                try:
                    account.get_message_by_id(msg_id).end_call()
                except Exception:
                    pass
            except Exception:
                log("session failed:\n" + traceback.format_exc())
                await s.close("setup failed")
                try:
                    account.get_message_by_id(msg_id).end_call()
                except Exception:
                    pass
        asyncio.run_coroutine_threadsafe(go(), self.loop)

    def place(self, account, chat_id, greeting=None, voice=None):
        """The bot rings a chat (dc-call CHAT_ID). One live call per chat."""
        if self.live_for_chat(chat_id):
            log(f"not dialing chat {chat_id}: a call is already live there")
            return False
        s = CallSession(self, account, int(chat_id))
        s.greeting, s.greet_voice = greeting, voice

        async def go():
            try:
                await s.dial()
            except ImportError as e:
                log(pack_gone("dial failed", e))        # the bot's preflight missed it: no traceback
                await s.close("dial failed")
            except Exception:
                log("dial failed:\n" + traceback.format_exc())
                await s.close("dial failed")
        asyncio.run_coroutine_threadsafe(go(), self.loop)
        return True

    def accepted(self, event):
        s = self.sessions.get(int(event.msg_id))
        if not s:
            log(f"OutgoingCallAccepted for unknown msg {event.msg_id}")
            return
        asyncio.run_coroutine_threadsafe(s.accepted(event.accept_call_info), self.loop)

    def hangup(self, chat_id):
        """dc-hangup CHAT_ID: the bot ends the call(s) in a chat. The core tells
        the phone; close() files the transcript."""
        # >>> ring-first: a call still ringing has no pc, so it is not "live"; decline it
        ringing = [s for s in list(self.sessions.values()) if s.chat_id == int(chat_id) and s.ringing]
        for s in ringing:
            async def decline(s=s):
                try:
                    msg = s.account.get_message_by_id(s.msg_id)
                    await self.loop.run_in_executor(None, msg.end_call)
                    log(f"DECLINED msg {s.msg_id}")
                except Exception as e:
                    log(f"end_call (decline) failed: {e}")
                await s.close(f"declined by {dc_env.AGENT_NAME}")
            asyncio.run_coroutine_threadsafe(decline(), self.loop)
        # <<< ring-first
        live = self.live_for_chat(chat_id)
        if not live and not ringing:
            log(f"hangup: no live call in chat {chat_id}")
        for s in live:
            async def go(s=s):
                try:
                    msg = s.account.get_message_by_id(s.msg_id)
                    await self.loop.run_in_executor(None, msg.end_call)
                    log(f"HUNG UP msg {s.msg_id}")
                except Exception as e:
                    log(f"end_call failed: {e}")
                await s.close(f"hung up by {dc_env.AGENT_NAME}")
            asyncio.run_coroutine_threadsafe(go(), self.loop)
        return bool(live or ringing)

    def answer(self, chat_id):
        """dc-answer CHAT_ID: the agent picks up a call ringing it (DC_ANSWER=ring).
        Decided ON the calls loop, where the ring ends, so the answer given
        (ok, detail) is the truth: picked up, or it had already gone to
        voicemail or ended. Returns (ok, detail) for the ack."""
        async def pick():
            got = [s for s in list(self.sessions.values())
                   if s.chat_id == int(chat_id) and s.ringing and not s.picked and not s.closed]
            for s in got:
                s.picked = True
                s.answer_go.set()
            return [s.msg_id for s in got]
        fut = asyncio.run_coroutine_threadsafe(pick(), self.loop)
        try:
            got = fut.result(timeout=5)
        except Exception as e:
            fut.cancel()                 # never a pick-up after the ack said no
            log(f"answer: the calls loop did not answer: {e!r}")
            return False, f"the calls loop did not answer ({e!r})"
        if got:
            ids = ", ".join(str(i) for i in got)
            log(f"answer: picking up msg {ids} in chat {chat_id}")
            return True, f"picked up msg {ids} in chat {chat_id}"
        live = (" - a call is live there (voicemail, or already answered): speak with dc-say"
                if self.live_for_chat(chat_id) else "")
        log(f"answer: nothing ringing in chat {chat_id}{live}")
        return False, f"nothing ringing in chat {chat_id}{live}"

    def ended(self, msg_id):
        s = self.sessions.get(int(msg_id))
        if s:
            asyncio.run_coroutine_threadsafe(s.close("CallEnded event"), self.loop)

    def forget(self, s):
        self.sessions.pop(s.msg_id, None)

    # >>> stop: the bot is being stopped (SIGTERM, launchd's stop)
    def stop(self, why, budget=None):
        """End every call this bot holds, for a bot that is going away: each
        call is ended (end_call, so the phone hangs up too) and its live file
        removed, then closed as a hang-up closes it, best-effort. The file goes
        first and on this thread: neither a stuck end_call nor a close() that
        raises can keep it. Bounded: returns within about BUDGET seconds,
        whatever the account or the calls loop does. -> how many calls."""
        budget = STOP_S if budget is None else budget
        sessions = list(self.sessions.values())
        if not sessions:
            return 0
        for s in sessions:
            s._live_clear()             # >>> call-id: first, owned-by-call-id, as close() clears it

        def end(s):
            try:
                s.account.get_message_by_id(s.msg_id).end_call()
                log(f"ENDED msg {s.msg_id}: {why}")
            except Exception as e:
                log(f"end_call failed for msg {s.msg_id}: {e}")
        # daemon threads, so an end_call that never returns cannot hold the exit
        enders = [threading.Thread(target=end, args=(s,), daemon=True, name=f"stop-{s.msg_id}")
                  for s in sessions if s.msg_id]
        for t in enders:
            t.start()
        log(f"stopping ({why}): ending {len(sessions)} call(s)")
        deadline = time.monotonic() + budget
        for t in enders:
            t.join(max(0.0, deadline - time.monotonic()))
        if self.thread.is_alive():
            futs = [asyncio.run_coroutine_threadsafe(s.close(why), self.loop) for s in sessions]
            done, waiting = concurrent.futures.wait(futs, timeout=max(0.0, deadline - time.monotonic()))
            for f in done:
                if f.exception() is not None:
                    log(f"close on stop failed: {f.exception()!r}")
            if waiting:
                log(f"stopping: {len(waiting)} call(s) still closing at the {budget:g} s limit")
        return len(sessions)
    # <<< stop

    def live_for_chat(self, chat_id):
        return [s for s in self.sessions.values() if s.chat_id == int(chat_id) and s.pc]

    # >>> call-id
    def live_call_id(self, chat_id):
        """The id of the live call in a chat, or None. Chat = room, msg = call."""
        live = self.live_for_chat(chat_id)
        return live[0].msg_id if live else None
    # <<< call-id

    def invite(self, chat_id, addr):
        """dc-invite CHAT_ID ADDRESS, from the outbox loop."""
        live = self.live_for_chat(chat_id)
        for s in live:
            asyncio.run_coroutine_threadsafe(s.invite(addr), self.loop)
        if not live:
            log(f"invite: no live call in chat {chat_id} for {addr}")
        return bool(live)

    def speak_if_live(self, chat_id, text, voice=None, who=None):
        """Called from the outbox loop: a dc-say into a chat with a live call.
        Returns one future per live call, each with speak()'s result: an
        empty list (falsy) when no call is live."""
        live, futs = self.live_for_chat(chat_id), []
        for s in live:
            reply_seen(self.loop, s, "dc-say")   # >>> turn-log: before speak, FIFO
            futs.append(asyncio.run_coroutine_threadsafe(s.speak(text, voice, who, floor=True), self.loop))
        return futs


_manager = CallManager()
incoming, ended, speak_if_live = _manager.incoming, _manager.ended, _manager.speak_if_live
invite = _manager.invite
live_call_id = _manager.live_call_id            # >>> call-id
place, accepted, hangup = _manager.place, _manager.accepted, _manager.hangup
answer = _manager.answer                        # >>> ring-first
stop = _manager.stop                            # >>> stop

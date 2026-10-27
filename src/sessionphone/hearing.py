"""hearing - will the bot hear the caller? `bot install` asks the STT.

On a Delta call the bot hears the caller by posting what they say to the
speech-to-text server at DC_STT_URL (bridge/stt.py), not through ears. A first
call with no STT there connects, speaks, and hears nothing, and nothing else
says so beforehand. So `bot install` ends with a `hearing:` line, judged on
the environment the launchd job carries:

- It sends the bot's own request (stt.resolve, then stt.curl_argv) with half a
  second of a quiet 220 Hz tone, the clip sessionvoice's install probes with.
  "It answers" means curl exits 0, the status is 2xx, and the body is a JSON
  object whose `text` is a string. An empty text counts: a tone has no words.
- It never imports ears and never asks service.ears_src(): ears gives turn
  previews, not hearing. It never settles for a TCP connect or a GET: an
  `ears serve` with no helper passes both and answers 500 to every
  transcription.
- `probing <url> (read-only)` is said BEFORE the request: an install check may
  ask the user's own STT, but never silently.
- The verdict is `yes`, `NO` or `not confirmed`. Every verdict names the URL,
  that it is DC_STT_URL (set, or the default), what was sent, what came back,
  and what that means for a call. When it is NO it points to the one route
  offered, an mlx-audio server on :8890, which sessionphone does not
  install. Under Session Kit (`sessionkit` on the PATH `bot install` ran
  with, the signal docs/call-hearing.md uses) that server is the kit's own:
  the line points at `ears services status` and `ears services install`,
  in both its forms, one line each with the Mac it fits: `--no-agent` for
  a Mac that only runs the bot, `--agent NAME` for one with an agent seat to
  wake (a bare install that names no seat to wake is refused, exit 2),
  and never at VoiceMode's installer, since a second mlx-audio job beside
  the kit's leaves one of the two crash-looping. Standalone it is
  VoiceMode's, with VoiceMode's command. It never says which model the
  server serves, only which model was requested.

When the probe gets no answer, the speech models may simply not be on this
Mac yet: the server's first request downloads them, and a probe that times
out at 30 s says nothing about why. So for the offered route (the one server
on this Mac the weights are for) `weights_state()` asks
`ears services weights --json`, a read-only, filesystem-only reader of the
speech weights in the Hugging Face cache. The line then says `downloading`
with how far it has got, or that the models are not downloaded yet and the
first call fetches them. The first call waits only for the stt and tts
weights: the voices pack missing alone is no wait. A reader that is absent,
fails, or answers in a schema it does not know leaves the line as it was.

`setup` asks too, BEFORE the phone is linked: on a fresh Mac
the speech server is a long install, and a NO first said at `bot install`,
the last step, comes after the link. It probes the STT the bot will use:
DC_STT_URL from setup's own shell, the one `bot install` carries into the
job, else the default, with the job's own PATH. `lines(..., before_link=True)`
says it was checked before the link, gives `yes` on one line, and points
"check again" at setup itself (`bot install` refuses before a phone is
linked). Under Session Kit its install step is the kit's
`ears services install --only speech` (the kit installs speech before
the link), never VoiceMode's. Said, never a refusal.

`bot status` asks the job's STT too, in ONE line (`status_line`): a bot
whose STT died after install is reported, not silent. Its probe is capped
at STATUS_MAX_TIME_S, so a server still loading its model reads
`not confirmed` in seconds, and status never waits 30 s.

Hearing is half of a call; the other half is speaking. On the speech-server
route (DC_TTS=auto when the server at DC_TTS_URL answers, which it does
whenever the default :8890 STT does, or DC_TTS=server) the bot turns each
reply into call audio with an ffmpeg binary. With none, every spoken reply
fails and goes to the caller as chat text, and `hearing: yes` said nothing
about it. So `speaking_lines()` adds a `speaking:` line, judged
on the job's environment too: `ffmpeg_for()` looks where the bot itself
looks (bridge/calls.py FFMPEG): DC_FFMPEG, else `ffmpeg` on the job's PATH,
else /opt/homebrew/bin/ffmpeg or /usr/local/bin/ffmpeg. It never reads the
shell's PATH or calls.FFMPEG, both settled on this shell, not the job. A
DC_FFMPEG that names no executable file is NO: the bot would run it and fail.
`bot install` prints it after `hearing:`, and `bot status` prints it too.
With DC_TTS=apple the bot speaks by macOS say and needs no ffmpeg.
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import textwrap
import time
import wave
from dataclasses import dataclass
from urllib.parse import urlparse

from sessionphone.bridge import dc_env, stt

MAX_TIME_S = 30          # curl's -m for the probe; the bot's own requests keep 120
RUN_TIMEOUT_S = 40       # the subprocess's own cap, above curl's (transcribe: 130 over 120)
# `bot status`'s cap: a refused port answers at once, a loaded server in well under
# a second; a server still loading its model says `not confirmed`, never a 30 s wait
STATUS_MAX_TIME_S = 5
STATUS_RUN_TIMEOUT_S = 10
CLIP = "a half-second test clip"
# The one route offered: mlx-audio on :8890. sessionphone does not install it.
OFFERED_URL = "mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions"
OFFERED_PORT = 8890
OFFERED_INSTALL = "voicemode service install mlx-audio"   # standalone only, never under the kit
# Under Session Kit the server on :8890 is the kit's own (ears services install loads it)
KIT_COMMAND = "sessionkit"         # on PATH: this is Session Kit (docs/call-hearing.md's signal)
KIT_STATUS = "ears services status"
KIT_INSTALL = "ears services install"
# A bare install with no wake seat named is refused (exit 2), so the hint gives
# the two forms ears accepts, each with the Mac it fits: which one is this Mac
# is the user's to say, and sessionphone cannot read it (the wake file is ears's).
KIT_INSTALL_PHONE = f"{KIT_INSTALL} --no-agent"
KIT_INSTALL_AGENT = f"{KIT_INSTALL} --agent NAME"
# Before the link (setup), the kit installs speech alone: --only speech wakes no
# seat and is never refused for want of --agent (sessionvoice services.py usage)
KIT_INSTALL_SPEECH = f"{KIT_INSTALL} --only speech"
KIT_FITS_PHONE = ("A Mac that only runs the bot, with no agent seat here for ears to wake "
                  "when you speak:")
KIT_FITS_AGENT = ("A Mac with an agent seat that ears should wake when you speak (NAME is "
                  "that seat):")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
HEAD = "hearing:   "
STATUS_HEAD = "hearing:  "      # `bot status`: aligned with its `speaking: ` line
INDENT = " " * len(HEAD)
WIDTH = 88
# the speech weights' reader (ears, read-only and filesystem-only); see weights_state()
EARS_COMMAND = "ears"
WEIGHTS_ARGV = ("services", "weights", "--json")
WEIGHTS_SCHEMA = "ears.weights/1"
WEIGHTS_TIMEOUT_S = 10
WEIGHT_STATES = ("cached", "downloading", "missing")
FIRST_CALL_USES = ("stt", "tts")   # what a first call waits for; the voices pack is not
# speaking: the ffmpeg the bot turns replies into call audio with (bridge/calls.py FFMPEG)
SPEAK_HEAD = "speaking:  "
FFMPEG_HOMEBREW = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
FFMPEG_FIX = "brew install ffmpeg"
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"   # launchd's own PATH, for a job that sets none
REINSTALL = "sessionphone bot install"
# curl -s prints no error of its own, so the common exit codes are named here
CURL_RC = {6: "could not resolve the host", 7: "connection refused", 28: "timed out",
           52: "the server closed the connection with no reply", 56: "the connection broke"}


def clip() -> bytes:
    """Half a second of a quiet 220 Hz tone, 16 kHz mono s16 WAV: real samples, so
    a server must transcribe it, and small enough to cost nothing. Deterministic,
    stdlib only (sessionvoice's ears/services.py _probe_clip)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(300 * math.sin(2 * math.pi * 220 * i / 16000)))
                               for i in range(8000)))
    return buf.getvalue()


@dataclass(frozen=True)
class Verdict:
    """What the probe found. answer is "yes", "NO" or "not confirmed"."""
    answer: str
    url: str                   # DC_STT_URL as the job carries it (or the default), tag and all
    bare: str                  # the URL the request goes to (the `provider+` tag stripped)
    user_set: bool             # DC_STT_URL is set in the job, not the default
    kind: str | None = None
    model: str | None = None   # the model REQUESTED; never a claim about what the server runs
    sent: bool = False         # a clip went out
    came_back: str = ""        # what came back, in a few words
    answered: bool = False     # an HTTP reply came back, whatever its status
    refused: str = ""          # no model can be named: stt.Refused's text; nothing is sent


RUN = subprocess.run     # how the request is run; tests replace it (tests/conftest.py)
WEIGHTS_RUN = subprocess.run   # how the weights reader is run; tests replace it (tests/conftest.py)


@dataclass(frozen=True)
class Weights:
    """The speech weights, as `ears services weights --json` reads them.

    state is keyed on what a first call needs, the stt and tts weights, never
    on the reader's own top-level state: `downloading` if either is,
    `cached` if both are, else `missing`. done and total are the reader's
    top-level bytes (total None when it cannot know it). size is the bytes
    the stt and tts weights not yet cached come to, None when unknown."""
    state: str
    done: int
    total: int | None
    size: int | None = None

    @property
    def wait(self) -> bool:
        """A first call waits for the models: they are downloading, or it fetches them."""
        return self.state != "cached"


def _int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def weights_state(environ: dict | None = None, run=None) -> Weights | None:
    """Ask `ears services weights --json` (found on environ's PATH) where the
    speech weights stand. -> Weights, or None when it cannot be told: no
    `ears` there, the verb exits non-zero (an older ears), the output does not
    parse, or its schema is not ears.weights/1. Never raises; read-only, and
    safe to call often (the reader touches only the filesystem)."""
    env = os.environ if environ is None else environ
    exe = shutil.which(EARS_COMMAND, path=env.get("PATH") or "")
    if exe is None:
        return None
    try:
        r = (run or WEIGHTS_RUN)([exe, *WEIGHTS_ARGV], capture_output=True, text=True,
                                 timeout=WEIGHTS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        doc = json.loads(r.stdout or "")
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("schema") != WEIGHTS_SCHEMA:
        return None
    done, total, rows = doc.get("bytes_done"), doc.get("bytes_total"), doc.get("weights")
    if not _int(done) or not (total is None or _int(total)) or not isinstance(rows, list):
        return None
    needed = []
    for w in rows:
        if not isinstance(w, dict) or w.get("state") not in WEIGHT_STATES:
            return None
        if w.get("use") in FIRST_CALL_USES:
            needed.append(w)
    if {w.get("use") for w in needed} != set(FIRST_CALL_USES):
        return None                  # no stt or no tts row: it cannot say what a call waits for
    states = {w["state"] for w in needed}
    state = ("downloading" if "downloading" in states
             else "cached" if states == {"cached"} else "missing")
    fetch = [w.get("bytes_total") for w in needed if w["state"] != "cached"]
    size = sum(fetch) if fetch and all(_int(b) for b in fetch) else None
    return Weights(state, done, total, size)


def weights_for(v: Verdict, shell=None, run=None) -> Weights | None:
    """The weights, read only when they can explain verdict v: the clip was
    sent to the offered route (this Mac's :8890, the server the weights are
    for) and it did not answer yes. Otherwise None, and nothing is run."""
    if v.answer == "yes" or not v.sent or not _offered(v.bare):
        return None
    return weights_state(os.environ if shell is None else shell, run=run)


def size(n: int) -> str:
    """Bytes in decimal units, as a person reads a download: 412 MB, 2.9 GB."""
    for unit, scale in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}" if unit == "GB" else f"{n // scale} {unit}"
    return f"{n} B"


def downloading(w: Weights) -> str:
    """`downloading 412 MB of 2.9 GB`, or `downloading, 412 MB so far` when the
    total is not known: the reader's top-level bytes."""
    if w.total is None:
        return f"downloading, {size(w.done)} so far"
    return f"downloading {size(w.done)} of {size(w.total)}"


def _about(w: Weights) -> str:
    return f" (about {size(w.size)})" if w.size is not None else ""


def probe(environ: dict, run=None, say=print, clock=time.monotonic,
          max_time: int = MAX_TIME_S, run_timeout: int = RUN_TIMEOUT_S) -> Verdict:
    """Ask the STT a bot run with `environ` would use (the job's env, read back
    from the plist) to transcribe the test clip. say() gets
    `probing <url> (read-only)` before anything is sent. max_time is curl's
    -m, run_timeout the subprocess's own cap (`bot status` passes shorter
    ones). Never raises for a failed request: every failure is a verdict."""
    run = run or RUN
    url = dc_env.stt_url(environ)
    is_set = bool(dc_env._get(environ, "STT_URL"))
    try:
        kind, bare, model = stt.resolve(url, dc_env.stt_model(environ))
    except stt.Refused as e:
        _, bare, _ = stt.endpoint(url)
        return Verdict("NO", url, bare, is_set, refused=str(e))
    path = None
    try:
        try:
            fd, path = tempfile.mkstemp(prefix="sessionphone-hearing-", suffix=".wav")   # 0600
            with os.fdopen(fd, "wb") as fh:
                fh.write(clip())
        except OSError as e:
            # an unwritable TMPDIR: nothing was sent, so the STT was never asked
            return Verdict("not confirmed", url, bare, is_set, kind, model,
                           came_back=f"the test clip could not be written ({e})")
        say(f"probing {bare} (read-only)")
        # with the job's PATH, so the curl that answers is the one the bot would run
        child_env = dict(os.environ, PATH=environ.get("PATH") or os.environ.get("PATH", ""))
        start = clock()
        try:
            r = run(stt.curl_argv(bare, model, path, max_time=max_time), capture_output=True,
                    text=True, timeout=run_timeout, env=child_env)
        except subprocess.TimeoutExpired:
            return Verdict("not confirmed", url, bare, is_set, kind, model, sent=True,
                           came_back=f"no answer within {max_time} s")
        except (OSError, subprocess.SubprocessError) as e:
            return Verdict("NO", url, bare, is_set, kind, model,
                           came_back=f"curl could not run: {e}")
        took = clock() - start
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    return _judge(r, Verdict("NO", url, bare, is_set, kind, model, sent=True), took, max_time)


def _judge(r, v: Verdict, took: float, max_time: int = MAX_TIME_S) -> Verdict:
    def as_(answer, came_back, answered=False):
        return Verdict(answer, v.url, v.bare, v.user_set, v.kind, v.model, True, came_back, answered)

    if r.returncode == 28:
        return as_("not confirmed", f"curl rc=28, no answer within {max_time} s")
    if r.returncode != 0:
        why = (r.stderr or "").strip()[:150] or CURL_RC.get(r.returncode, "no reply")
        return as_("NO", f"curl rc={r.returncode}, {why}")
    body, _, status = (r.stdout or "").rpartition("\n")
    status = status.strip()
    if not status.isdigit() or not 200 <= int(status) < 300:
        return as_("NO", f"HTTP {status or '?'}: {stt._short(body) or '(no body)'}", answered=status.isdigit())
    try:
        text = json.loads(body).get("text")
    except (ValueError, AttributeError):
        text = None
    if not isinstance(text, str):
        return as_("NO", f"HTTP {status}, but not a transcription (a JSON object with a text): "
                         f"{stt._short(body) or '(no body)'}", answered=True)
    return as_("yes", f"HTTP {status} in {took:.1f} s", answered=True)


def _offered(bare: str) -> bool:
    """Is this URL the offered route's own: mlx-audio on this Mac's :8890?"""
    try:
        u = urlparse(bare)
        return (u.hostname or "") in LOOPBACK and u.port == OFFERED_PORT
    except ValueError:
        return False


def _p(text: str) -> tuple[str, str]:
    """A paragraph: wrapped under the `hearing:` column."""
    return ("p", text)


def _cmd(line: str) -> tuple[str, str]:
    """A command, shown as is on a line of its own, so it can be copied."""
    return ("cmd", line)


def under_kit(shell) -> bool:
    """Is this Session Kit? It is when `sessionkit` is on the shell's PATH, as
    `command -v sessionkit` asks in docs/call-hearing.md. shell is the
    environment `bot install` ran in; with no PATH there, the answer is no."""
    return shutil.which(KIT_COMMAND, path=shell.get("PATH") or "") is not None


def _route(environ: dict, setting: bool, kit: bool = False, again: str = REINSTALL,
           before_link: bool = False) -> list[tuple[str, str]]:
    """The offered route, as paragraphs (_p wraps, _cmd is a command shown as
    is). setting: the URL is not the route's, so the check-again
    line carries DC_STT_URL. kit: this is Session Kit, so the server on the
    route's port is the kit's own, and VoiceMode's installer is never named.
    again: the command that checks again (`bot install`; setup's own line
    before the link). before_link: setup's route, where the kit's step is
    its speech server alone (KIT_INSTALL_SPEECH)."""
    _, _, model = stt.resolve(OFFERED_URL, dc_env.stt_model(environ))
    again = f"DC_STT_URL={OFFERED_URL} {again}" if setting else again
    if kit and before_link:
        return [
            _p(f"This is Session Kit ({KIT_COMMAND} is on PATH), so the STT on 127.0.0.1:{OFFERED_PORT} "
               f"is the kit's own speech server; the bot asks it for the model {model}. Install it "
               "now, before the phone is linked (see the kit's install steps). Do not install "
               "VoiceMode's mlx-audio beside it: one of the two would crash-loop. Read the kit's "
               "`speech` row first (read-only):"),
            _cmd(KIT_STATUS),
            _p(f"`none on :{OFFERED_PORT}`: no server and no job. Install the kit's speech server:"),
            _cmd(KIT_INSTALL_SPEECH),
            _p(f"`mlx-audio on :{OFFERED_PORT}`: it is up, and its first request downloads that "
               "model, so hearing can read `not confirmed` (or NO) for a minute or more. Wait, "
               "then check again:"),
            _cmd(again),
            _p("Any other row names who holds the port: install nothing over it."),
        ]
    if kit:
        return [
            _p(f"This is Session Kit ({KIT_COMMAND} is on PATH), so the STT on 127.0.0.1:{OFFERED_PORT} "
               f"is the kit's own speech server, if it loads one; the bot asks it for the model "
               f"{model}. Do not install VoiceMode's mlx-audio beside it: one of the two would "
               "crash-loop. Read the kit's `speech` row (read-only):"),
            _cmd(KIT_STATUS),
            _p(f"`none on :{OFFERED_PORT}`: no server and no job, and the kit's installer loads "
               "one. Run the form that fits this Mac (a bare install that names no seat to wake "
               "is refused). " + KIT_FITS_PHONE),
            _cmd(KIT_INSTALL_PHONE),
            _p(KIT_FITS_AGENT),
            _cmd(KIT_INSTALL_AGENT),
            _p(f"`mlx-audio on :{OFFERED_PORT}`: it is up, and its first request downloads that "
               "model, so hearing can read `not confirmed` (or NO) for a minute or more. Wait, "
               "then check again:"),
            _cmd(again),
            _p("Any other row names who holds the port: install nothing over it. No `speech` "
               "row: this kit loads no speech server, so the sessionphone skill offers "
               "VoiceMode's."),
        ]
    return [
        _p(f"The STT route sessionphone offers is VoiceMode's mlx-audio on 127.0.0.1:{OFFERED_PORT} "
              f"(Apple Silicon); the bot asks it for the model {model}. sessionphone does not "
              "install it. With VoiceMode installed, run:"),
        _cmd(OFFERED_INSTALL),
        _p("A first request may be slow while it fetches that model. Then check again:"),
        _cmd(again),
    ]


def lines(v: Verdict, environ: dict, shell: dict | None = None,
          weights: Weights | None = None, again: str = REINSTALL,
          before_link: bool = False) -> list[str]:
    """The `hearing:` lines for verdict v, for a bot run with `environ`. shell is
    the environment `bot install` ran in, to say when only the deprecated
    PIP_CHAT_STT_URL was set there (the job carries DC_* only). weights is
    weights_for(v)'s answer: None leaves every line as it was. again is the
    command that checks again. before_link is setup's: every verdict says it
    was checked before the phone is linked, `yes` is one line, and under the
    kit the install step is KIT_INSTALL_SPEECH."""
    calls_off = (dc_env._get(environ, "CALLS") or "on") == "off"
    via_ears = dc_env._get(environ, "EARS") == "1"
    where = f"{v.bare} (DC_STT_URL, {'as set' if v.user_set else 'the default'})"
    paras: list[tuple[str, str]] = []

    # what it means: a call's hearing only when calls are on AND they listen through DC_STT_URL
    calls_here = not calls_off and not via_ears
    if calls_off:
        lead = "Calls are off (DC_CALLS=off), so this is for voice notes only. "
    elif via_ears:
        lead = ("Calls were not checked: the job sets DC_EARS=1, so a call listens through ears, "
                "not DC_STT_URL. This is for voice notes only. ")
    else:
        lead = ""
    if before_link:
        lead = "Checked before the phone is linked. " + lead
    if v.refused:
        paras.append(_p(f"NO. {lead}No request was sent to {where}: {v.refused}. The bot will "
                           "refuse to start until one of those is set."))
        return _render(paras)
    if v.answer == "yes" and before_link:     # one line, never wrapped
        means = "the bot will hear the caller." if calls_here else "voice notes will be transcribed."
        return [(f"{HEAD}yes, checked before the phone is linked: {v.bare} answered {CLIP} "
                 f"({v.came_back}), so {means}")]
    if v.answer == "yes":
        means = "The bot will hear the caller." if calls_here else "Voice notes will be transcribed."
        model = f", model requested: {v.model}" if v.model else ""
        paras.append(_p(f"yes. {lead}{where} answered {CLIP}: {v.came_back} "
                           f"({v.kind or 'unknown kind'}{model}). {means}"))
        return _render(paras)
    if v.answer == "not confirmed" and not v.sent:
        paras.append(_p(f"not confirmed. {lead}{where} was not asked: {v.came_back}. Nothing was "
                           "sent, so this says nothing about that server. Fix that, then run "
                           f"`{again}` again."))
        return _render(paras)
    sh = os.environ if shell is None else shell
    kit = under_kit(sh)
    # the weights speak only for the offered route, this Mac's own server
    w = weights if weights is not None and weights.wait and _offered(v.bare) else None
    first = "the first call" if calls_here else "the first voice note"
    if v.answer == "not confirmed" and w is not None:
        if w.state == "downloading":
            paras.append(_p(f"{downloading(w)}. {lead}{where} was sent {CLIP} and gave "
                               f"{v.came_back}: the speech models it needs are still downloading "
                               f"to this Mac, so it cannot answer yet. {first.capitalize()} waits for "
                               f"that download to finish. Run `{again}` again when it has."))
        else:
            paras.append(_p(f"not confirmed. {lead}{where} was sent {CLIP} and gave {v.came_back}. "
                               f"The speech models it needs are not downloaded yet{_about(w)}: the "
                               f"first request fetches them. {first.capitalize()} waits for that download. Run "
                               f"`{again}` again in a few minutes."))
        if kit:
            paras += [_p("Watch the download (read-only):"), _cmd(KIT_STATUS)]
        return _render(paras)
    if v.answer == "not confirmed":
        paras.append(_p(f"not confirmed. {lead}{where} was sent {CLIP} and gave {v.came_back}. "
                           "A first request can be slow while the server loads its model. Run "
                           f"`{again}` again in a few minutes."))
        return _render(paras)

    # NO
    if calls_here:
        means = ("A call will connect, but the bot will not hear the caller, and voice notes will "
                 "not be transcribed. Hearing needs an STT server, not ears.")
    else:
        means = "Voice notes will not be transcribed."
    sent = f"was sent {CLIP}" if v.sent else "was not sent the test clip"
    if v.answered:
        paras.append(_p(f"NO. {lead}{where} {sent} and answered {v.came_back}. {means} "
                           "Something answers there, but it does not transcribe: check that "
                           "server's log."))
    else:
        paras.append(_p(f"NO. {lead}{where} {sent}, and nothing answered: {v.came_back}. {means}"))
    if w is not None and w.state == "downloading":
        paras.append(_p(f"Its speech models are {downloading(w)}. Once a server answers, "
                           f"{first} waits for that download to finish."))
    elif w is not None:
        paras.append(_p(f"Its speech models are not downloaded yet{_about(w)}: once a server "
                           f"answers, its first request fetches them, and "
                           f"{first} waits for that download."))
    # the route is offered only for the route's own URL or the default;
    # a URL the user set names their own server
    if _offered(v.bare):
        if not v.answered:
            paras += _route(environ, setting=False, kit=kit, again=again, before_link=before_link)
    elif v.user_set:
        paras.append(_p("That is the DC_STT_URL you set: start or fix that server, or unset "
                           "DC_STT_URL to use the default."))
    else:
        paras += _route(environ, setting=True, kit=kit, again=again, before_link=before_link)
    if not v.user_set and sh.get(f"{dc_env.ALIAS}STT_URL"):
        paras.append(_p(f"{dc_env.ALIAS}STT_URL is set in this shell, but the job carries DC_* "
                           "settings only, so it was not used: rename it DC_STT_URL."))
    return _render(paras)


def status_line(v: Verdict, environ: dict, head: str = STATUS_HEAD) -> str:
    """`bot status`'s ONE hearing line for verdict v, for a bot run with
    `environ` (the job's): never wrapped. `yes` is short; NO names the URL
    that did not answer and points at `bot install`, whose `hearing:` lines
    say how to fix it; `not confirmed` says why it could not tell."""
    calls_here = ((dc_env._get(environ, "CALLS") or "on") != "off"
                  and dc_env._get(environ, "EARS") != "1")
    where = f"{v.bare} (DC_STT_URL{'' if v.user_set else ', the default'})"
    how = f"`{REINSTALL}` says how to fix it."
    if v.refused:
        return f"{head}NO. {where} names no model: {v.refused}. {how}"
    if v.answer == "yes":
        return f"{head}yes. {v.bare} answered {CLIP} ({v.came_back})."
    if v.answer == "not confirmed" and not v.sent:
        return f"{head}not confirmed. {where} was not asked: {v.came_back}."
    if v.answer == "not confirmed":
        return (f"{head}not confirmed. {where} gave {v.came_back}. A server loading its "
                f"model can be slow; `{REINSTALL}` waits longer.")
    means = "callers will not be heard" if calls_here else "voice notes will not be transcribed"
    if v.answered:
        return f"{head}NO. {where} answered {CLIP} with {v.came_back}, so {means}. {how}"
    return f"{head}NO. {where} did not answer {CLIP} ({v.came_back}), so {means}. {how}"


def _render(paras: list[tuple[str, str]], head: str = HEAD) -> list[str]:
    indent = " " * len(head)
    out: list[str] = []
    for i, (what, text) in enumerate(paras):
        if what == "cmd":
            out.append(f"{indent}  {text}")
            continue
        out += textwrap.wrap(text, WIDTH, initial_indent=head if i == 0 else indent,
                             subsequent_indent=indent, break_on_hyphens=False, break_long_words=False)
    return out


# ---- speaking: will the bot's replies be spoken? ---------------------------

@dataclass(frozen=True)
class Ffmpeg:
    """The ffmpeg a bot run with some environment will use. path is what it
    runs (None: none found); setting names the variable that set it, else None;
    came says how it was found, in words; ok is whether that path is a
    file it can run."""
    path: str | None
    setting: str | None
    ok: bool
    job_path: str = ""          # the PATH it was looked for on
    came: str = ""


def ffmpeg_for(environ: dict) -> Ffmpeg:
    """The ffmpeg a bot run with `environ` (the job's, read back from its
    plist) would run, found as bridge/calls.py's FFMPEG finds it: DC_FFMPEG
    (or its deprecated alias) as set, else `ffmpeg` on the job's PATH, else
    the first Homebrew path that exists. Never this shell's PATH. A bare
    name in DC_FFMPEG (no slash) is looked up on the job's PATH, as the
    bot's subprocess looks it up."""
    job_path = environ.get("PATH") or LAUNCHD_PATH
    for name in ("DC_FFMPEG", f"{dc_env.ALIAS}FFMPEG"):
        if environ.get(name):
            p = environ[name]
            if os.sep not in p:
                found = shutil.which(p, path=job_path)
                return Ffmpeg(found or p, name, found is not None, job_path, name)
            return Ffmpeg(p, name, os.path.isfile(p) and os.access(p, os.X_OK), job_path, name)
    found = shutil.which("ffmpeg", path=job_path)
    if found is not None:
        return Ffmpeg(found, None, True, job_path, "on the job's PATH")
    found = next((p for p in FFMPEG_HOMEBREW if os.path.exists(p)), None)
    return Ffmpeg(found, None, found is not None, job_path, "the Homebrew path" if found else "")


def tts_mode(environ: dict) -> str:
    """DC_TTS as the bot reads it (bridge/calls.py TTS_MODE): auto, server or apple."""
    return (dc_env._get(environ, "TTS") or "auto").strip().lower()


def speaking_lines(environ: dict, head: str = SPEAK_HEAD) -> list[str]:
    """The `speaking:` lines for a bot run with `environ`: is there an ffmpeg
    to turn the speech server's replies into call audio? A NO names ffmpeg,
    says spoken replies go as chat text, and gives the fix as a command."""
    mode = tts_mode(environ)
    ff = ffmpeg_for(environ)
    if mode == "apple":
        return _render([_p("yes. DC_TTS=apple: replies are spoken by macOS say, "
                           "which needs no ffmpeg.")], head)
    if ff.ok:
        return _render([_p(f"yes. ffmpeg at {ff.path} ({ff.came}) turns the speech server's "
                           "replies into call audio.")], head)
    if ff.setting:
        where = (f"not on the job's PATH ({ff.job_path})" if os.sep not in ff.path
                 else "not a file the bot can run")
        looked = f"{ff.setting} is {ff.path}, which is {where}, and the bot runs what it names."
        fix = (f"Point {ff.setting} at ffmpeg, or unset it and install ffmpeg, then run "
               f"`{REINSTALL}` again so the bot picks it up:")
    else:
        looked = (f"No ffmpeg was found where the bot looks: DC_FFMPEG is not set, none is on the "
                  f"job's PATH ({ff.job_path}), and none is at {' or '.join(FFMPEG_HOMEBREW)}.")
        fix = f"Install ffmpeg, then run `{REINSTALL}` again so the bot picks it up:"
    when = ("" if mode == "server" else
            " (If no speech server answers, the bot speaks by macOS say, which needs no "
            "ffmpeg.)")
    paras = [
        _p(f"NO. {looked} The bot turns each spoken reply from the speech server into call "
           "audio with ffmpeg, so every spoken reply will fail and go to the caller as chat text "
           f"instead.{when}"),
        _p(fix),
        _cmd(FFMPEG_FIX),
        _cmd(REINSTALL),
    ]
    return _render(paras, head)

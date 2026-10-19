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
  offered, VoiceMode's mlx-audio on :8890, which sessionphone does not
  install. It never says which model the server serves, only which model
  was requested.

`setup` never probes: hearing is judged where the bot is installed.
"""
from __future__ import annotations

import io
import json
import math
import os
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
CLIP = "a half-second test clip"
# The one route offered: VoiceMode's mlx-audio. sessionphone does not install it.
OFFERED_URL = "mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions"
OFFERED_PORT = 8890
OFFERED_INSTALL = "voicemode service install mlx-audio"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
HEAD = "hearing:   "
INDENT = " " * len(HEAD)
WIDTH = 88
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


def probe(environ: dict, run=None, say=print, clock=time.monotonic) -> Verdict:
    """Ask the STT a bot run with `environ` would use (the job's env, read back
    from the plist) to transcribe the test clip. say() gets
    `probing <url> (read-only)` before anything is sent. Never raises for a
    failed request: every failure is a verdict."""
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
            r = run(stt.curl_argv(bare, model, path, max_time=MAX_TIME_S), capture_output=True,
                    text=True, timeout=RUN_TIMEOUT_S, env=child_env)
        except subprocess.TimeoutExpired:
            return Verdict("not confirmed", url, bare, is_set, kind, model, sent=True,
                           came_back=f"no answer within {MAX_TIME_S} s")
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
    return _judge(r, Verdict("NO", url, bare, is_set, kind, model, sent=True), took)


def _judge(r, v: Verdict, took: float) -> Verdict:
    def as_(answer, came_back, answered=False):
        return Verdict(answer, v.url, v.bare, v.user_set, v.kind, v.model, True, came_back, answered)

    if r.returncode == 28:
        return as_("not confirmed", f"curl rc=28, no answer within {MAX_TIME_S} s")
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


def _route(environ: dict, setting: bool) -> list[tuple[str, str]]:
    """The offered route, as paragraphs (_p wraps, _cmd is a command shown as
    is). setting: the URL is not the route's, so the re-install
    line carries DC_STT_URL."""
    _, _, model = stt.resolve(OFFERED_URL, dc_env.stt_model(environ))
    again = (f"DC_STT_URL={OFFERED_URL} sessionphone bot install" if setting
             else "sessionphone bot install")
    return [
        _p(f"The STT route sessionphone offers is VoiceMode's mlx-audio on 127.0.0.1:{OFFERED_PORT} "
              f"(Apple Silicon); the bot asks it for the model {model}. sessionphone does not "
              "install it. With VoiceMode installed, run:"),
        _cmd(OFFERED_INSTALL),
        _p("A first request may be slow while it fetches that model. Then check again:"),
        _cmd(again),
    ]


def lines(v: Verdict, environ: dict, shell: dict | None = None) -> list[str]:
    """The `hearing:` lines for verdict v, for a bot run with `environ`. shell is
    the environment `bot install` ran in, to say when only the deprecated
    PIP_CHAT_STT_URL was set there (the job carries DC_* only)."""
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
    if v.refused:
        paras.append(_p(f"NO. {lead}No request was sent to {where}: {v.refused}. The bot will "
                           "refuse to start until one of those is set."))
        return _render(paras)
    if v.answer == "yes":
        means = "The bot will hear the caller." if calls_here else "Voice notes will be transcribed."
        model = f", model requested: {v.model}" if v.model else ""
        paras.append(_p(f"yes. {lead}{where} answered {CLIP}: {v.came_back} "
                           f"({v.kind or 'unknown kind'}{model}). {means}"))
        return _render(paras)
    if v.answer == "not confirmed" and not v.sent:
        paras.append(_p(f"not confirmed. {lead}{where} was not asked: {v.came_back}. Nothing was "
                           "sent, so this says nothing about that server. Fix that, then run "
                           "`sessionphone bot install` again."))
        return _render(paras)
    if v.answer == "not confirmed":
        paras.append(_p(f"not confirmed. {lead}{where} was sent {CLIP} and gave {v.came_back}. "
                           "A first request can be slow while the server loads its model. Run "
                           "`sessionphone bot install` again in a few minutes."))
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
    # the route is offered only for the route's own URL or the default;
    # a URL the user set names their own server
    if _offered(v.bare):
        if not v.answered:
            paras += _route(environ, setting=False)
    elif v.user_set:
        paras.append(_p("That is the DC_STT_URL you set: start or fix that server, or unset "
                           "DC_STT_URL to use the default."))
    else:
        paras += _route(environ, setting=True)
    sh = os.environ if shell is None else shell
    if not v.user_set and sh.get(f"{dc_env.ALIAS}STT_URL"):
        paras.append(_p(f"{dc_env.ALIAS}STT_URL is set in this shell, but the job carries DC_* "
                           "settings only, so it was not used: rename it DC_STT_URL."))
    return _render(paras)


def _render(paras: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for i, (what, text) in enumerate(paras):
        if what == "cmd":
            out.append(f"{INDENT}  {text}")
            continue
        out += textwrap.wrap(text, WIDTH, initial_indent=HEAD if i == 0 else INDENT,
                             subsequent_indent=INDENT, break_on_hyphens=False, break_long_words=False)
    return out

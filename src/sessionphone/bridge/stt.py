"""stt - a voice note becomes text, on an endpoint that's told which model to use.

Every request names its model. mlx-audio refuses a request that doesn't name
one: FastAPI answers 422 "Field required" for `model`, before anything
loads. The bridge used to send none, and the refusal was swallowed, so the
note arrived as "[voice note - transcription failed]" with nothing in the
log to say why. VoiceMode had the same bug. The setting shape here is
VoiceMode's. It is taken, not forked.

Which model is sent:
  1. DC_STT_MODEL, when it's set. It's sent as is, to any endpoint.
  2. Else the default for the endpoint's KIND, from MODEL_DEFAULTS below.
     The table is VoiceMode's, so if VoiceMode's default changes, change one
     line here.
  3. Else the bot refuses to START (check(), called from bot.main), naming
     DC_STT_URL and DC_STT_MODEL: tag the URL or set the model. A refusal
     at start means voice notes never fail one by one at runtime. The
     catch-all `whisper-1` is never sent: an mlx-audio server answers it
     with a 404. (transcribe() still refuses and logs if it ever gets here.)

How the kind is known:
  - A `provider+` prefix on DC_STT_URL names it, the way git uses `git+ssh://`:
        DC_STT_URL=mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions
    The prefix is stripped before the URL is used. A prefix that isn't in the
    table is still a kind, just one with no default.
  - An untagged URL falls back to the port guess: 8890 is mlx-audio, 2022 is
    whisper (whisper.cpp), 2023 is apple (`ears serve`) and an openai.com host
    is openai. Each untagged URL logs the hint to tag it once. The bridge's own
    default is the tagged mlx-audio URL above (dc_env.STT_DEFAULT).
"""
import json
import re
import subprocess
from urllib.parse import urlparse

# VoiceMode's STT defaults. whisper.cpp never reads the field
# (server.cpp:1163-1174), so it gets a name it ignores, VoiceMode's example.
# The apple row is OURS: VoiceMode's table lacks it, though `apple` slots in
# with no new syntax. Its model is ears' /v1/models id (ears serve.py:34).
# VoiceMode can adopt the row or rename it.
MODEL_DEFAULTS = {
    "mlx-audio": "mlx-community/parakeet-tdt-0.6b-v3",
    "whisper": "large-v2",
    "openai": "whisper-1",
    "apple": "apple-speechtranscriber",
}
PORT_KINDS = {8890: "mlx-audio", 2022: "whisper", 2023: "apple"}

_TAG = re.compile(r"^([a-z][a-z0-9_-]*)\+([a-z][a-z0-9.+-]*://.*)$", re.IGNORECASE)
BODY_MAX = 300           # how much of a server's error body goes in the log
_hinted = set()          # untagged URLs already told to tag themselves (once each)


class Refused(Exception):
    """No model could be named for this endpoint, so nothing is sent."""


def endpoint(url):
    """DC_STT_URL -> (kind, url to POST to, tagged).

    The `provider+` prefix is the kind, and it's stripped. With no prefix it's
    the port guess, and the kind is None when nothing matches."""
    url = (url or "").strip()
    m = _TAG.match(url)
    if m:
        return m.group(1).lower(), m.group(2), True
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None, url, False
    host = (parsed.hostname or "").lower()
    if host == "openai.com" or host.endswith(".openai.com"):
        return "openai", url, False
    return PORT_KINDS.get(port), url, False


def resolve(url, model=None):
    """-> (kind, url, model) for one request, or raises Refused saying how to fix it."""
    kind, bare, _ = endpoint(url)
    if model:
        return kind, bare, model
    if kind in MODEL_DEFAULTS:
        return kind, bare, MODEL_DEFAULTS[kind]
    known = ", ".join(f"{k}+" for k in MODEL_DEFAULTS)
    what = f"its kind is {kind!r}, which has no default model" if kind else "its kind is unknown"
    raise Refused(
        f"no STT model for DC_STT_URL={url}: {what}. Tag the URL with its "
        f"provider ({known}), e.g. mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions, "
        f"or set DC_STT_MODEL")


def check(url, model=None):
    """At bot start: -> None, or why the bot refuses to start. With no model
    for DC_STT_URL, every voice note would fail, so the bot refuses once at
    start and names both settings. Each note doesn't fail on its own at runtime."""
    try:
        resolve(url, model)
    except Refused as e:
        return str(e)
    return None


def curl_argv(url, model, path, max_time=120):
    """The request: the file, the model and json back, with the HTTP status
    written after the body on a line of its own so a refusal can be seen.
    max_time is curl's -m: 120 s for a voice note, less for `bot install`'s
    hearing probe (sessionphone/hearing.py), which sends this same request."""
    return ["curl", "-s", "-m", str(max_time), "-X", "POST", url,
            "-F", f"file=@{path}", "-F", f"model={model}", "-F", "response_format=json",
            "-w", "\n%{http_code}"]


def _short(body):
    body = " ".join((body or "").split())
    return body if len(body) <= BODY_MAX else body[:BODY_MAX] + "..."


def transcribe(path, url, model, log, run=subprocess.run):
    """Voice note at path -> its text, or None. Every None is logged with its
    reason: no model, a curl failure, an HTTP error with the server's body, a
    reply that isn't JSON, or a 200 with no text in it."""
    try:
        kind, bare, model = resolve(url, model)
    except Refused as e:
        log(f"TRANSCRIBE refused: {e}")
        return None
    if not endpoint(url)[2] and url not in _hinted:
        _hinted.add(url)
        log(f"STT endpoint {url} is untagged; guessed {kind!r} from it. Tag it to be sure, "
            f"e.g. DC_STT_URL={kind or 'mlx-audio'}+{bare}")
    try:
        r = run(curl_argv(bare, model, path), capture_output=True, text=True, timeout=130)
    except (OSError, subprocess.SubprocessError) as e:     # no curl, or the 130 s timeout
        log(f"TRANSCRIBE failed ({kind}, model {model}): {e}")
        return None
    if r.returncode != 0:
        log(f"TRANSCRIBE curl rc={r.returncode} ({kind}, model {model}, {bare}): {r.stderr.strip()[:150]}")
        return None
    body, _, status = (r.stdout or "").rpartition("\n")
    status = status.strip()
    if not status.isdigit() or not 200 <= int(status) < 300:
        log(f"TRANSCRIBE HTTP {status or '?'} from {bare} ({kind}, model {model}): {_short(body)}")
        return None
    try:
        text = json.loads(body).get("text")
    except (ValueError, AttributeError):
        log(f"TRANSCRIBE HTTP {status} from {bare} ({kind}, model {model}) is not a JSON object: {_short(body)}")
        return None
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text:
        log(f"TRANSCRIBE HTTP {status} from {bare} ({kind}, model {model}) has no text: {_short(body)}")
        return None
    return text

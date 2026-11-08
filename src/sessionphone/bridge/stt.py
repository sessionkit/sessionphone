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

A voice note is posted as wav (as_wav):
  A voice note can arrive as M4A (AAC), Ogg or another container. Some
  speech servers read only wav: an mlx-audio server answers an M4A with
  "Unsupported output format: m4a", and the agent read "[voice note -
  transcription failed]". So a note that isn't wav is decoded first, with
  PyAV (av, from the media pack), to 16 kHz mono 16-bit wav in a temp file,
  and that file is what's posted. The temp file is deleted afterwards. A wav
  is posted as it came, so a call's chunks (always wav) are never decoded.
  When a note can't be decoded, Undecodable says why in plain words, the log
  has the line, and nothing is posted: a half-written file never is.
  Without PyAV (a chat-only bot with no media pack), the note is posted as
  it came, as before, and the log says it wasn't decoded: a server that
  reads M4A (whisper.cpp does) still hears it. A raw file is not a half-file.
"""
import json
import os
import re
import subprocess
import tempfile
import wave
from contextlib import contextmanager
from pathlib import Path
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


WAV_RATE = 16000         # what a decoded note becomes: 16 kHz, mono, 16-bit
WAV_PREFIX = "sessionphone-voicenote-"
NO_PYAV = "PyAV (av, from the media pack) is not installed"


class Refused(Exception):
    """No model could be named for this endpoint, so nothing is sent."""


class Undecodable(Exception):
    """A voice note that isn't wav could not be decoded to wav. str() is the
    plain reason, e.g. "Invalid data found when processing input"."""


def is_wav(path):
    """Whether the file at path is a RIFF/WAVE file, read from its first 12
    bytes, not its name. A file that can't be read isn't wav."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return len(head) == 12 and head[:4] == b"RIFF" and head[8:] == b"WAVE"


def _reason(e):
    """An exception -> its plain words, without the errno or the path."""
    words = getattr(e, "strerror", None)
    return str(words or e or e.__class__.__name__).strip()


def have_pyav():
    """Whether PyAV can be imported: the media pack is installed."""
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True


def to_wav(path, dest):
    """Decode the audio at path to 16 kHz mono 16-bit wav at dest.
    -> its length in seconds, or raises Undecodable with the reason."""
    try:
        import av  # the media pack; stt imports without it
    except ImportError:
        raise Undecodable(NO_PYAV) from None
    try:
        with av.open(str(path)) as box:
            stream = next((s for s in box.streams if s.type == "audio"), None)
            if stream is None:                 # before the wav opens: an unset wav can't close
                raise Undecodable("it has no audio track")
            with wave.open(str(dest), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(WAV_RATE)
                resample = av.AudioResampler(format="s16", layout="mono", rate=WAV_RATE).resample
                samples = 0
                for frame in box.decode(stream):
                    for f in resample(frame):
                        out.writeframes(f.to_ndarray().tobytes())
                        samples += f.samples
                for f in resample(None):       # what the resampler still holds
                    out.writeframes(f.to_ndarray().tobytes())
                    samples += f.samples
    except Undecodable:
        raise
    except Exception as e:     # av.FFmpegError and the rest: a note never crashes the bot
        raise Undecodable(_reason(e)) from e
    if not samples:
        raise Undecodable("it holds no audio")
    return samples / WAV_RATE


@contextmanager
def as_wav(path, log):
    """Yield a wav path for the voice note at path: path itself when it's wav
    already, else a temp wav decoded from it and deleted on the way out.
    A decode failure is logged and raised as Undecodable, and the partial
    temp file is deleted first, so nothing half-written is ever posted.
    With no PyAV to decode it, path itself is yielded, raw, and logged:
    never after a failed decode, only when no decode could be tried."""
    if is_wav(path):
        yield path
        return
    name = Path(path).name
    if not have_pyav():
        log(f"VOICE NOTE not decoded to wav ({name}): {NO_PYAV}; posting it as it came")
        yield path
        return
    fd, tmp = tempfile.mkstemp(prefix=WAV_PREFIX, suffix=".wav")   # 0600
    os.close(fd)
    try:
        try:
            secs = to_wav(path, tmp)
        except Undecodable as e:
            log(f"VOICE NOTE not decoded to wav ({name}): {e}")
            raise
        log(f"VOICE NOTE decoded to 16 kHz mono wav ({name}, {secs:.1f} s)")
        yield tmp
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


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

"""Every STT request names its model, and every refusal is loud (PHONE-110).

mlx-audio answers a request with no `model` with a 422, and the bridge used to
swallow it: the note became "[voice note - transcription failed]" and nothing
went in the log. These tests pin the request built for each endpoint kind, the
`provider+` prefix strip, the DC_STT_MODEL override, the refusal when no model
can be named, and the log line a 422 now leaves.

Fakes only. curl is replaced by a recorder, and the one end-to-end test talks
to a stdlib server on 127.0.0.1 that answers the way mlx-audio does. No shared
mlx-audio, ears or whisper is touched.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from sessionphone.bridge import bot, dc_env, stt

PARAKEET = "mlx-community/parakeet-tdt-0.6b-v3"
MISSING_MODEL = '{"detail":[{"type":"missing","loc":["body","model"],"msg":"Field required","input":null}]}'


class FakeCurl:
    """Stands in for subprocess.run: records the argv, answers with body + status."""

    def __init__(self, body='{"text": "hello there"}', status=200, rc=0, stderr=""):
        self.body, self.status, self.rc, self.stderr = body, status, rc, stderr
        self.argv = None

    def __call__(self, argv, **kw):
        self.argv = argv
        out = "" if self.rc else f"{self.body}\n{self.status}"
        return subprocess.CompletedProcess(argv, self.rc, out, self.stderr)

    def form(self):
        """The -F fields sent, as a dict."""
        a = self.argv
        return dict(a[i + 1].split("=", 1) for i, x in enumerate(a) if x == "-F")

    def url(self):
        return self.argv[self.argv.index("POST") + 1]


@pytest.fixture
def logged(monkeypatch):
    monkeypatch.setattr(stt, "_hinted", set())
    return []


def run(url, model=None, curl=None, logged=None):
    curl = curl or FakeCurl()
    logs = [] if logged is None else logged
    return stt.transcribe("/tmp/note.wav", url, model, logs.append, run=curl), curl, logs


# ---- the request, per kind ------------------------------------------------

@pytest.mark.parametrize("url, kind, model, posted", [
    # tagged: the prefix is the kind, and it's stripped before use
    ("mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions", "mlx-audio", PARAKEET,
     "http://127.0.0.1:8890/v1/audio/transcriptions"),
    ("whisper+http://stt.example.net:2022/v1/audio/transcriptions", "whisper", "large-v2",
     "http://stt.example.net:2022/v1/audio/transcriptions"),
    ("openai+https://api.openai.com/v1/audio/transcriptions", "openai", "whisper-1",
     "https://api.openai.com/v1/audio/transcriptions"),
    ("apple+http://127.0.0.1:2023/v1/audio/transcriptions", "apple", "apple-speechtranscriber",
     "http://127.0.0.1:2023/v1/audio/transcriptions"),
    # a tag beats the port: mlx-audio on another port, apple on whisper's
    ("mlx-audio+http://studio.example.net:9000/v1/audio/transcriptions", "mlx-audio", PARAKEET,
     "http://studio.example.net:9000/v1/audio/transcriptions"),
    ("apple+http://127.0.0.1:2022/v1/audio/transcriptions", "apple", "apple-speechtranscriber",
     "http://127.0.0.1:2022/v1/audio/transcriptions"),
    # untagged: the port guess
    ("http://127.0.0.1:8890/v1/audio/transcriptions", "mlx-audio", PARAKEET,
     "http://127.0.0.1:8890/v1/audio/transcriptions"),
    ("http://stt.example.net:2022/v1/audio/transcriptions", "whisper", "large-v2",
     "http://stt.example.net:2022/v1/audio/transcriptions"),
    ("http://127.0.0.1:2023/v1/audio/transcriptions", "apple", "apple-speechtranscriber",
     "http://127.0.0.1:2023/v1/audio/transcriptions"),
    ("https://api.openai.com/v1/audio/transcriptions", "openai", "whisper-1",
     "https://api.openai.com/v1/audio/transcriptions"),
])
def test_each_kind_gets_its_model_and_the_bare_url(url, kind, model, posted, logged):
    assert stt.endpoint(url)[:2] == (kind, posted)
    text, curl, _ = run(url, logged=logged)
    assert text == "hello there"
    assert curl.url() == posted
    assert curl.form() == {"file": "@/tmp/note.wav", "model": model, "response_format": "json"}


def test_an_mlx_audio_endpoint_never_gets_whisper_1(logged):
    for url in ("mlx-audio+http://h:1/v1/audio/transcriptions", "http://h:8890/v1/audio/transcriptions"):
        _, curl, _ = run(url, logged=logged)
        assert curl.form()["model"] == PARAKEET


def test_the_default_table_is_vm_2470s():
    # VM-2470 design.md §1.3 step 5 (STT column), plus apple (§1.2: "slots in").
    # A different default is a one-line change to stt.MODEL_DEFAULTS.
    assert stt.MODEL_DEFAULTS == {
        "mlx-audio": PARAKEET, "whisper": "large-v2", "openai": "whisper-1",
        "apple": "apple-speechtranscriber"}


DEFAULT = "mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions"   # PHONE-18 U2
OLD_DEFAULT = "http://127.0.0.1:2023/v1/audio/transcriptions"           # `ears serve`, before PHONE-18


def test_the_bridges_own_default_url_is_mlx_audio_tagged_and_gets_parakeet(logged):
    """PHONE-18 U2: the default is VoiceMode's mlx-audio on :8890, the server the
    bot's voice uses. It is tagged, so it resolves with no port guess and no hint."""
    assert dc_env.STT_DEFAULT == DEFAULT
    assert stt.resolve(DEFAULT) == ("mlx-audio", "http://127.0.0.1:8890/v1/audio/transcriptions", PARAKEET)
    assert stt.check(DEFAULT) is None
    _, curl, logs = run(DEFAULT, logged=logged)
    assert curl.url() == "http://127.0.0.1:8890/v1/audio/transcriptions"
    assert curl.form()["model"] == PARAKEET
    assert not any("untagged" in l for l in logs)


def test_the_old_default_still_resolves_to_apple_when_set():
    """CHANGELOG's restore line, DC_STT_URL set to the old default, still means `ears serve`."""
    assert stt.resolve(OLD_DEFAULT) == ("apple", OLD_DEFAULT, "apple-speechtranscriber")
    assert dc_env.stt_url({"DC_STT_URL": OLD_DEFAULT}) == OLD_DEFAULT
    assert dc_env.stt_url({}) == DEFAULT


# ---- DC_STT_MODEL ---------------------------------------------------------

def test_dc_stt_model_overrides_the_default(logged):
    _, curl, _ = run("mlx-audio+http://h:8890/v1/audio/transcriptions",
                     model="mlx-community/whisper-large-v3-turbo-asr-4bit", logged=logged)
    assert curl.form()["model"] == "mlx-community/whisper-large-v3-turbo-asr-4bit"
    assert curl.url() == "http://h:8890/v1/audio/transcriptions"   # still stripped


def test_dc_stt_model_names_an_endpoint_of_unknown_kind(logged):
    text, curl, logs = run("http://gpu.example.net:7000/v1/audio/transcriptions", model="my-model", logged=logged)
    assert text == "hello there"
    assert curl.form()["model"] == "my-model"
    assert not any("refused" in l for l in logs)


def _bot_in_a_fresh_process(tmp_path, **env):
    """Import the bot the way launchd does; report the STT settings it read."""
    full = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    full.update(DC_ROOT=str(tmp_path / "root"), DC_CALLS="off", **env)
    out = subprocess.run(
        [sys.executable, "-c",
         ("import json; from sessionphone.bridge import bot, dc_env; "
          "print(json.dumps([dc_env.STT_URL, dc_env.STT_MODEL]))")],
        env=full, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_dc_stt_model_is_read_from_the_environment(tmp_path):
    assert _bot_in_a_fresh_process(tmp_path) == [DEFAULT, None]
    assert _bot_in_a_fresh_process(tmp_path, DC_STT_URL=OLD_DEFAULT)[0] == OLD_DEFAULT
    assert _bot_in_a_fresh_process(tmp_path, DC_STT_MODEL="x/y")[1] == "x/y"
    assert _bot_in_a_fresh_process(tmp_path, PIP_CHAT_STT_MODEL="x/z")[1] == "x/z"
    assert _bot_in_a_fresh_process(tmp_path, DC_STT_MODEL="")[1] is None     # empty = unset


def test_the_bot_hands_its_settings_to_stt(monkeypatch):
    seen = []
    monkeypatch.setattr(bot.dc_env, "STT_URL", "mlx-audio+http://h:8890/v1/audio/transcriptions")
    monkeypatch.setattr(bot.dc_env, "STT_MODEL", "x/y")
    monkeypatch.setattr(bot.stt, "transcribe", lambda *a: seen.append(a) or "words")
    assert bot.transcribe("/n.wav") == "words"
    assert seen == [("/n.wav", "mlx-audio+http://h:8890/v1/audio/transcriptions", "x/y", bot.log)]


# ---- refusal: no model, nothing sent -------------------------------------

@pytest.mark.parametrize("url", [
    "http://gpu.example.net:7000/v1/audio/transcriptions",          # untagged, no port match
    "http://127.0.0.1/v1/audio/transcriptions",             # no port at all
    "kokoro+http://127.0.0.1:8880/v1/audio/transcriptions",  # a kind with no STT default
])
def test_no_kind_and_no_model_refuses_loudly_and_sends_nothing(url, logged):
    curl = FakeCurl()
    text, _, logs = run(url, curl=curl, logged=logged)
    assert text is None
    assert curl.argv is None                                  # never sent, never whisper-1
    assert len(logs) == 1 and logs[0].startswith("TRANSCRIBE refused:")
    assert "DC_STT_MODEL" in logs[0] and "mlx-audio+" in logs[0]


# ---- refusal at BOT START, not per note at runtime ------------------------

UNKNOWN = "http://gpu.example.net:7000/v1/audio/transcriptions"


@pytest.mark.parametrize("url", [
    UNKNOWN,
    "http://127.0.0.1/v1/audio/transcriptions",
    "kokoro+http://127.0.0.1:8880/v1/audio/transcriptions",
])
def test_check_names_both_settings_when_no_model_can_be_named(url):
    why = stt.check(url)
    assert why and "DC_STT_URL" in why and "DC_STT_MODEL" in why


@pytest.mark.parametrize("url, model", [
    # a live setup: untagged whisper.cpp on :2022 must still start
    ("http://stt.example.net:2022/v1/audio/transcriptions", None),
    ("http://127.0.0.1:8890/v1/audio/transcriptions", None),       # untagged mlx-audio
    ("http://127.0.0.1:2023/v1/audio/transcriptions", None),       # `ears serve`, the old default
    ("mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions", None),  # the bridge's default
    ("mlx-audio+http://studio.example.net:9000/v1/audio/transcriptions", None),
    (UNKNOWN, "x/y"),                                              # DC_STT_MODEL rescues any URL
])
def test_check_passes_every_url_with_a_model(url, model):
    assert stt.check(url, model) is None


def test_untagged_2022_is_whisper_and_8890_is_mlx_audio():
    # Pinned on the super's ruling (c): these two port guesses keep live setups working.
    assert stt.resolve("http://h:2022/v1/audio/transcriptions")[::2] == ("whisper", "large-v2")
    assert stt.resolve("http://h:8890/v1/audio/transcriptions")[::2] == ("mlx-audio", PARAKEET)


def test_the_bot_refuses_to_start_with_no_stt_model(monkeypatch, capsys):
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot.dc_env, "STT_URL", UNKNOWN)
    monkeypatch.setattr(bot.dc_env, "STT_MODEL", None)

    def started(*a, **kw):
        raise AssertionError("the bot went past its start checks")
    monkeypatch.setattr(bot, "relink", started)
    monkeypatch.setattr(bot, "server_choice", started)
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("sessionphone bot: ") and "DC_STT_URL" in err and "DC_STT_MODEL" in err


def test_the_bot_start_check_reads_its_settings(monkeypatch):
    monkeypatch.setattr(bot.dc_env, "STT_URL", UNKNOWN)
    monkeypatch.setattr(bot.dc_env, "STT_MODEL", None)
    assert "DC_STT_MODEL" in bot.check_stt()
    monkeypatch.setattr(bot.dc_env, "STT_MODEL", "x/y")
    assert bot.check_stt() is None
    monkeypatch.setattr(bot.dc_env, "STT_MODEL", None)
    monkeypatch.setattr(bot.dc_env, "STT_URL", "http://h:2022/v1/audio/transcriptions")
    assert bot.check_stt() is None


# ---- loud failures --------------------------------------------------------

def test_a_422_logs_its_status_and_the_servers_body(logged):
    text, _, logs = run("mlx-audio+http://h:8890/v1/audio/transcriptions",
                        curl=FakeCurl(body=MISSING_MODEL, status=422), logged=logged)
    assert text is None
    [line] = logs
    assert "TRANSCRIBE HTTP 422" in line and "http://h:8890/v1/audio/transcriptions" in line
    assert PARAKEET in line and '"msg":"Field required"' in line


def test_a_long_error_body_is_truncated(logged):
    _, _, logs = run("whisper+http://h:2022/v1/audio/transcriptions",
                     curl=FakeCurl(body="x" * 5000, status=500), logged=logged)
    assert "HTTP 500" in logs[0]
    assert "x" * stt.BODY_MAX + "..." in logs[0] and "x" * (stt.BODY_MAX + 1) not in logs[0]


@pytest.mark.parametrize("body", ['{"text": ""}', '{"text": "   "}', "{}", '{"text": null}'])
def test_a_200_with_no_text_is_logged(body, logged):
    text, _, logs = run("whisper+http://h:2022/v1/audio/transcriptions",
                        curl=FakeCurl(body=body), logged=logged)
    assert text is None
    assert len(logs) == 1 and "HTTP 200" in logs[0] and "has no text" in logs[0]


@pytest.mark.parametrize("body", ["<html>gateway</html>", "[1, 2]"])
def test_a_200_that_is_not_a_json_object_is_logged(body, logged):
    text, _, logs = run("whisper+http://h:2022/v1/audio/transcriptions",
                        curl=FakeCurl(body=body), logged=logged)
    assert text is None and "not a JSON object" in logs[0]


def test_a_curl_failure_is_logged(logged):
    text, _, logs = run("whisper+http://h:2022/v1/audio/transcriptions",
                        curl=FakeCurl(rc=7, stderr="Failed to connect"), logged=logged)
    assert text is None and "curl rc=7" in logs[0] and "Failed to connect" in logs[0]


def test_an_exception_is_logged(logged):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 130)
    logs = []
    assert stt.transcribe("/n.wav", "whisper+http://h:2022/x", None, logs.append, run=boom) is None
    assert "TRANSCRIBE failed" in logs[0] and "large-v2" in logs[0]


def test_an_untagged_url_is_told_to_tag_itself_once(logged):
    url = "http://127.0.0.1:8890/v1/audio/transcriptions"
    for _ in range(3):
        run(url, logged=logged)
    hints = [l for l in logged if "untagged" in l]
    assert len(hints) == 1 and f"mlx-audio+{url}" in hints[0]
    run("mlx-audio+" + url, logged=logged)
    assert len([l for l in logged if "untagged" in l]) == 1    # a tagged URL never is


# ---- end to end: real curl, a fake mlx-audio on 127.0.0.1 ----------------

class FakeMlxAudio(BaseHTTPRequestHandler):
    """Answers like mlx-audio's stt_transcriptions: a form without `model` is 422."""
    got: ClassVar[list] = []   # reset by the mlx_audio fixture

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeMlxAudio.got.append(body)
        if b'name="model"' not in body:
            code, reply = 422, MISSING_MODEL.encode()
        else:
            code, reply = 200, b'{"text": " parakeet heard you "}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *a):
        pass


@pytest.fixture
def mlx_audio():
    srv = HTTPServer(("127.0.0.1", 0), FakeMlxAudio)
    FakeMlxAudio.got = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1/audio/transcriptions"
    srv.shutdown()
    srv.server_close()


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_end_to_end_the_model_reaches_the_server(mlx_audio, tmp_path, logged):
    note = tmp_path / "note.wav"
    note.write_bytes(b"RIFF0000WAVE")
    assert stt.transcribe(str(note), "mlx-audio+" + mlx_audio, None, logged.append) == "parakeet heard you"
    assert f'name="model"\r\n\r\n{PARAKEET}'.encode() in FakeMlxAudio.got[0]
    assert logged == []


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_end_to_end_a_422_is_loud_not_silent(mlx_audio, tmp_path, logged, monkeypatch):
    # the server refuses whatever we send: the PHONE-110 repro, now with a log line
    monkeypatch.setattr(FakeMlxAudio, "do_POST", _always_422)
    note = tmp_path / "note.wav"
    note.write_bytes(b"RIFF0000WAVE")
    assert stt.transcribe(str(note), "mlx-audio+" + mlx_audio, None, logged.append) is None
    [line] = logged
    assert "TRANSCRIBE HTTP 422" in line and "Field required" in line


def _always_422(self):
    self.rfile.read(int(self.headers["Content-Length"]))
    reply = MISSING_MODEL.encode()
    self.send_response(422)
    self.send_header("Content-Length", str(len(reply)))
    self.end_headers()
    self.wfile.write(reply)

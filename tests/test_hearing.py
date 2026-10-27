"""`bot install` says whether the bot will hear the caller (PHONE-18).

On a Delta call the bot hears the caller through the STT at DC_STT_URL, not
through ears (define-001). The trial's first call was one-way and nothing said
so beforehand. Now `bot install` ends with a `hearing:` line, judged by asking
that STT to transcribe a half-second test clip, the bot's own request.

These pin design.md section 9: the verdict comes from the STT, never from
`import ears`; the probe sends the bot's request; `probing <url> (read-only)`
is said first; it is judged on the job's env; each branch; the wording rules
(ruling (b), S3); and the conftest tripwire that keeps
every test off a live STT. No test here reaches a live STT: the runner is a
fake, or curl runs against a stub server on an ephemeral loopback port, or
against the tripwire's closed port.
"""
import importlib.abc
import json
import os
import plistlib
import subprocess
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from click.testing import CliRunner

from sessionphone import hearing, service
from sessionphone.bridge import dc_env, stt
from sessionphone.cli import main as cli

OWNER = "bea@phone.example.org"
OFFERED = "http://127.0.0.1:8890/v1/audio/transcriptions"       # hearing.OFFERED_URL, its tag stripped
DEFAULT = OFFERED            # dc_env.STT_DEFAULT, its tag stripped: the offered route (PHONE-18 U2)
OLD_DEFAULT = "http://127.0.0.1:2023/v1/audio/transcriptions"   # `ears serve`, the default before U2
OWN = "mlx-audio+http://stt.example.net:9000/v1/audio/transcriptions"
OWN_BARE = "http://stt.example.net:9000/v1/audio/transcriptions"
PARAKEET = "mlx-community/parakeet-tdt-0.6b-v3"
UNDER_KIT = hearing.under_kit   # the real check; the launchd rig replaces it with "no" (conftest)


class FakeCurl:
    """Stands in for hearing.RUN: records each call (argv, the clip it saw, what
    had been said by then) and answers with body + status, a curl rc, or raises."""

    def __init__(self, body='{"text": ""}', status=200, rc=0, stderr="", raises=None, said=None):
        self.body, self.status, self.rc, self.stderr, self.raises = body, status, rc, stderr, raises
        self.said = said          # a list the probe's say() appends to, snapshotted per call
        self.calls = []

    def __call__(self, argv, **kw):
        path = argv[argv.index("-F") + 1].split("@", 1)[1]
        with open(path, "rb") as fh:
            clip = fh.read()
        self.calls.append({"argv": argv, "kw": kw, "path": path, "clip": clip,
                           "said": list(self.said) if self.said is not None else None})
        if self.raises:
            raise self.raises
        out = "" if self.rc else f"{self.body}\n{self.status}"
        return subprocess.CompletedProcess(argv, self.rc, out, self.stderr)


def _flat(lines):
    return " ".join(" ".join(lines).split())


def _probe(env, curl=None):
    said = []
    curl = curl or FakeCurl(said=said)
    curl.said = said
    v = hearing.probe(env, run=curl, say=said.append)
    return v, curl, said


# ---- the probe: the bot's own request, said first --------------------------

def test_the_probe_sends_the_bots_own_request():
    v, curl, _ = _probe({"DC_STT_URL": hearing.OFFERED_URL, "PATH": "/usr/bin:/bin"})
    [call] = curl.calls
    assert call["argv"] == stt.curl_argv(OFFERED, PARAKEET, call["path"], max_time=30)
    assert call["argv"][call["argv"].index("-m") + 1] == "30"
    assert call["kw"]["timeout"] > 30                          # its own cap, above curl's
    assert call["kw"]["env"]["PATH"] == "/usr/bin:/bin"         # curl as the job would find it
    assert not os.path.exists(call["path"])                     # the clip is removed afterwards
    assert v.answer == "yes" and v.sent and v.model == PARAKEET and v.kind == "mlx-audio"


def test_the_clip_is_real_audio_not_a_header():
    _, curl, _ = _probe({"DC_STT_URL": hearing.OFFERED_URL})
    clip = curl.calls[0]["clip"]
    assert clip == hearing.clip()
    import io
    with wave.open(io.BytesIO(clip)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 8000)
        assert any(w.readframes(8000))                          # samples, not silence


def test_probing_is_said_before_the_request():
    _, curl, said = _probe({"DC_STT_URL": hearing.OFFERED_URL})
    assert curl.calls[0]["said"] == [f"probing {OFFERED} (read-only)"]
    assert said == [f"probing {OFFERED} (read-only)"]


def test_the_default_url_curl_argv_keeps_120_for_the_bot():
    assert stt.curl_argv("u", "m", "/p")[2:4] == ["-m", "120"]


# ---- the branches ----------------------------------------------------------

@pytest.mark.parametrize(("curl", "answer", "came_back"), [
    (FakeCurl(), "yes", "HTTP 200 in"),
    (FakeCurl(body='{"text": " hello "}'), "yes", "HTTP 200 in"),
    (FakeCurl(body="Internal Server Error: no helper", status=500), "NO", "HTTP 500: Internal Server Error: no helper"),
    (FakeCurl(body="<html>ok</html>"), "NO", "HTTP 200, but not a transcription"),
    (FakeCurl(body='{"text": null}'), "NO", "HTTP 200, but not a transcription"),
    (FakeCurl(rc=7), "NO", "curl rc=7, connection refused"),
    (FakeCurl(rc=6), "NO", "curl rc=6, could not resolve the host"),
    (FakeCurl(rc=28), "not confirmed", "curl rc=28, no answer within 30 s"),
    (FakeCurl(raises=subprocess.TimeoutExpired("curl", 40)), "not confirmed", "no answer within 30 s"),
    (FakeCurl(raises=FileNotFoundError(2, "No such file or directory", "curl")), "NO", "curl could not run"),
], ids=["200-empty", "200-words", "500", "200-html", "200-null", "rc7", "rc6", "rc28", "timeout", "no-curl"])
def test_each_answer_is_judged(curl, answer, came_back):
    v, _, _ = _probe({"DC_STT_URL": hearing.OFFERED_URL}, curl)
    assert v.answer == answer
    assert v.came_back.startswith(came_back), v.came_back


def test_with_no_model_nothing_is_sent_and_the_bot_will_refuse():
    """An untagged URL on an unknown port names no model: the bot refuses to start (bot.py stt_problem)."""
    url = "http://127.0.0.1:7000/v1/audio/transcriptions"
    v, curl, said = _probe({"DC_STT_URL": url})
    assert curl.calls == [] and said == []                      # nothing said, nothing sent
    assert v.answer == "NO" and v.refused and not v.sent
    text = _flat(hearing.lines(v, {"DC_STT_URL": url}, shell={}))
    assert text.startswith("hearing: NO.")
    assert url in text and "DC_STT_URL" in text and "No request was sent" in text
    assert "will refuse to start" in text and "DC_STT_MODEL" in text


def test_a_model_named_for_an_unknown_kind_is_sent():
    v, curl, _ = _probe({"DC_STT_URL": "http://127.0.0.1:7000/v1/audio/transcriptions", "DC_STT_MODEL": "m1"})
    assert v.answer == "yes" and "model=m1" in curl.calls[0]["argv"]


# ---- the wording (ruling (b); S3; review D6) -------------------------------

VERDICTS = {
    "yes": FakeCurl(),
    "NO-refused": FakeCurl(rc=7),
    "NO-500": FakeCurl(body="boom", status=500),
    "not-confirmed": FakeCurl(rc=28),
}


@pytest.mark.parametrize("name", VERDICTS)
@pytest.mark.parametrize("url", [None, hearing.OFFERED_URL, OWN], ids=["default", "offered", "own"])
def test_every_verdict_names_url_setting_clip_reply_and_meaning(name, url):
    env = {"DC_STT_URL": url} if url else {}
    v, _, said = _probe(env, VERDICTS[name])
    lines = hearing.lines(v, env, shell={})
    text = _flat(lines)
    bare = stt.endpoint(url or DEFAULT)[1]
    assert lines[0].startswith(f"hearing:   {v.answer}.")
    assert said == [f"probing {bare} (read-only)"]
    assert bare in text                                         # 1. the URL, in the line itself (D6)
    assert f"(DC_STT_URL, {'as set' if url else 'the default'})" in text   # 2. it is DC_STT_URL
    assert "a half-second test clip" in text                    # 3. what it sent
    assert ("HTTP " in text) or ("curl rc=" in text)            # 4. what came back
    assert any(m in text for m in ("will hear the caller", "will not hear the caller",
                                   "first request can be slow"))   # 5. what it means for a call
    assert "Parakeet" not in text and "GB" not in text          # S3: never as fact
    assert "ears" not in text.replace("not ears", "")           # never names ears as the reason


def test_yes_names_the_kind_and_the_model_requested_not_served():
    v, _, _ = _probe({"DC_STT_URL": hearing.OFFERED_URL})
    text = _flat(hearing.lines(v, {"DC_STT_URL": hearing.OFFERED_URL}, shell={}))
    assert f"(mlx-audio, model requested: {PARAKEET})" in text
    assert text.endswith("The bot will hear the caller.")


def test_the_default_is_the_offered_route():
    """U2 (design.md section 6): the default and the offered route are one URL."""
    assert dc_env.STT_DEFAULT == hearing.OFFERED_URL


def test_no_at_the_default_offers_the_route_without_the_setting():
    """U2: the default IS the offered route, so installing it is enough: the
    re-install line carries no DC_STT_URL."""
    v, _, _ = _probe({}, FakeCurl(rc=7))
    lines = hearing.lines(v, {}, shell={})
    text = _flat(lines)
    assert "(DC_STT_URL, the default)" in text
    assert "A call will connect, but the bot will not hear the caller" in text
    assert "Hearing needs an STT server, not ears." in text
    assert "VoiceMode's mlx-audio on 127.0.0.1:8890" in text and "sessionphone does not install it" in text
    assert f"{hearing.INDENT}  voicemode service install mlx-audio" in lines
    assert f"{hearing.INDENT}  sessionphone bot install" in lines
    assert not any("DC_STT_URL=" in ln for ln in lines)


def test_no_at_a_default_that_is_not_the_route_offers_it_with_the_setting(monkeypatch):
    """Should the default ever move off the route again (as it was, :2023, before
    U2), the re-install line carries the route's DC_STT_URL."""
    monkeypatch.setattr(dc_env, "STT_DEFAULT", OLD_DEFAULT)
    v, _, _ = _probe({}, FakeCurl(rc=7))
    lines = hearing.lines(v, {}, shell={})
    text = _flat(lines)
    assert "A call will connect, but the bot will not hear the caller" in text
    assert "Hearing needs an STT server, not ears." in text
    assert "VoiceMode's mlx-audio on 127.0.0.1:8890" in text and "sessionphone does not install it" in text
    assert f"the bot asks it for the model {PARAKEET}" in text
    assert f"{hearing.INDENT}  voicemode service install mlx-audio" in lines
    assert f"{hearing.INDENT}  DC_STT_URL={hearing.OFFERED_URL} sessionphone bot install" in lines


def test_no_at_the_offered_url_offers_the_route_without_the_setting():
    env = {"DC_STT_URL": hearing.OFFERED_URL}
    v, _, _ = _probe(env, FakeCurl(rc=7))
    lines = hearing.lines(v, env, shell={})
    assert f"{hearing.INDENT}  voicemode service install mlx-audio" in lines
    assert f"{hearing.INDENT}  sessionphone bot install" in lines
    assert not any("DC_STT_URL=" in ln for ln in lines)


def test_a_server_that_answers_but_fails_is_told_to_check_its_log_not_to_install():
    env = {"DC_STT_URL": hearing.OFFERED_URL}
    v, _, _ = _probe(env, FakeCurl(body="model load failed", status=500))
    text = _flat(hearing.lines(v, env, shell={}))
    assert "answered HTTP 500: model load failed" in text and "check that server's log" in text
    assert "voicemode service install" not in text


def test_no_at_a_url_the_user_set_names_their_server_not_the_route():
    env = {"DC_STT_URL": OWN}
    v, _, _ = _probe(env, FakeCurl(rc=7))
    text = _flat(hearing.lines(v, env, shell={}))
    assert "the DC_STT_URL you set: start or fix that server, or unset DC_STT_URL" in text
    assert "voicemode" not in text and "sessionphone does not install it" not in text


def test_dc_ears_never_says_yes_for_calls():
    env = {"DC_STT_URL": hearing.OFFERED_URL, "DC_EARS": "1"}
    for curl in (FakeCurl(), FakeCurl(rc=7)):
        v, _, _ = _probe(env, curl)
        text = _flat(hearing.lines(v, env, shell={}))
        assert "Calls were not checked: the job sets DC_EARS=1" in text
        assert "will hear the caller" not in text and "will not hear the caller" not in text
        assert "Voice notes will" in text


def test_calls_off_speaks_of_voice_notes_only():
    env = {"DC_STT_URL": hearing.OFFERED_URL, "DC_CALLS": "off"}
    v, _, _ = _probe(env)
    text = _flat(hearing.lines(v, env, shell={}))
    assert "Calls are off (DC_CALLS=off)" in text and "Voice notes will be transcribed." in text
    assert "caller" not in text


def test_an_alias_only_in_the_shell_is_named():
    """PIP_CHAT_STT_URL in the shell never reaches the job (service.job carries DC_* only)."""
    v, _, _ = _probe({}, FakeCurl(rc=7))
    text = _flat(hearing.lines(v, {}, shell={"PIP_CHAT_STT_URL": "http://elsewhere:1/x"}))
    assert "PIP_CHAT_STT_URL is set in this shell" in text and "rename it DC_STT_URL" in text


# ---- under Session Kit: the kit's own server, never VoiceMode's installer ----

@pytest.fixture
def kit_path(tmp_path):
    """A PATH on which `sessionkit` is found, as on a Session Kit Mac."""
    bin_ = tmp_path / "kit-bin"
    bin_.mkdir()
    exe = bin_ / hearing.KIT_COMMAND
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return str(bin_)


def test_under_kit_is_sessionkit_on_the_shells_path(kit_path, tmp_path):
    assert hearing.under_kit({"PATH": f"/nonexistent:{kit_path}"})
    assert not hearing.under_kit({"PATH": str(tmp_path)})           # no sessionkit there
    assert not hearing.under_kit({"PATH": ""}) and not hearing.under_kit({})


def _kit_lines(env, curl, kit_path):
    v, _, _ = _probe(env, curl)
    return hearing.lines(v, env, shell={"PATH": kit_path})


@pytest.mark.parametrize("env", [{}, {"DC_STT_URL": hearing.OFFERED_URL}], ids=["default", "offered"])
def test_no_under_the_kit_points_at_ears_services_never_voicemode(env, kit_path):
    """The kit's `ears services install` loads mlx-audio on :8890; a second
    mlx-audio job beside it crash-loops. So under the kit the line sends the
    user to the kit's read-only status, names the kit's installer for an
    empty port only, and never names VoiceMode's installer (docs/call-hearing.md)."""
    lines = _kit_lines(env, FakeCurl(rc=7), kit_path)
    text = _flat(lines)
    assert lines[0].startswith("hearing:   NO.")
    assert "A call will connect, but the bot will not hear the caller" in text
    assert "voicemode service install" not in text and hearing.OFFERED_INSTALL not in lines
    assert "This is Session Kit (sessionkit is on PATH)" in text
    assert "the kit's own speech server" in text and f"the model {PARAKEET}" in text
    assert "Do not install VoiceMode's mlx-audio beside it: one of the two would crash-loop" in text
    # status first, read-only; the installer only for `none on :8890`
    status = lines.index(f"{hearing.INDENT}  {hearing.KIT_STATUS}")
    install = lines.index(f"{hearing.INDENT}  {hearing.KIT_INSTALL_PHONE}")
    assert status < install
    assert "(read-only):" in lines[status - 1]
    assert "`none on :8890`: no server and no job" in _flat(lines[status + 1:install])
    assert "the kit's own speech server, if it loads one" in text
    # the first-run download belongs to the `mlx-audio on :8890` row (as in
    # docs/call-hearing.md), so its wait-and-retry comes before the other rows
    again = lines.index(f"{hearing.INDENT}  sessionphone bot install")
    agent = lines.index(f"{hearing.INDENT}  {hearing.KIT_INSTALL_AGENT}")
    up = _flat(lines[agent + 1:again])
    assert up.startswith("`mlx-audio on :8890`: it is up, and its first request downloads that model")
    assert "can read `not confirmed` (or NO) for a minute or more. Wait, then check again" in up
    assert "No `speech` row" not in up and "VoiceMode's" not in up
    rest = _flat(lines[again + 1:])
    assert "Any other row names who holds the port: install nothing over it" in rest
    assert "No `speech` row: this kit loads no speech server" in rest
    assert rest.endswith("the sessionphone skill offers VoiceMode's.")
    assert not any("DC_STT_URL=" in ln for ln in lines)


def test_under_the_kit_the_installer_is_given_in_both_forms_each_with_the_mac_it_fits(kit_path):
    """A bare `ears services install` that names no seat to wake is refused (exit 2),
    and which Mac this is cannot be read from here. So the line gives both forms
    ears accepts, one command line each, each after the line saying which Mac it
    fits: --no-agent for a Mac that only runs the bot, --agent NAME for a Mac with
    an agent seat to wake. The bare form is never offered as a command."""
    lines = _kit_lines({}, FakeCurl(rc=7), kit_path)
    cmds = [ln.strip() for ln in lines if ln.startswith(f"{hearing.INDENT}  ")]
    assert hearing.KIT_INSTALL not in cmds
    assert hearing.KIT_INSTALL_PHONE == "ears services install --no-agent"
    assert hearing.KIT_INSTALL_AGENT == "ears services install --agent NAME"
    phone = lines.index(f"{hearing.INDENT}  {hearing.KIT_INSTALL_PHONE}")
    agent = lines.index(f"{hearing.INDENT}  {hearing.KIT_INSTALL_AGENT}")
    assert phone < agent
    status = lines.index(f"{hearing.INDENT}  {hearing.KIT_STATUS}")
    # the phone-only case: its fit comes right before its command
    before_phone = _flat(lines[status + 1:phone])
    assert before_phone.startswith("`none on :8890`: no server and no job")
    assert "a bare install that names no seat to wake is refused" in before_phone
    assert before_phone.endswith(hearing.KIT_FITS_PHONE)
    assert "only runs the bot, with no agent seat here for ears to wake" in before_phone
    # the agent-seat case: its own fit, between the two commands
    assert _flat(lines[phone + 1:agent]) == hearing.KIT_FITS_AGENT
    assert "agent seat that ears should wake when you speak (NAME is that seat)" in hearing.KIT_FITS_AGENT


def test_no_under_the_kit_at_a_default_off_the_route_carries_the_setting(monkeypatch, kit_path):
    monkeypatch.setattr(dc_env, "STT_DEFAULT", OLD_DEFAULT)
    lines = _kit_lines({}, FakeCurl(rc=7), kit_path)
    assert "voicemode service install" not in _flat(lines)
    assert f"{hearing.INDENT}  DC_STT_URL={hearing.OFFERED_URL} sessionphone bot install" in lines
    assert f"{hearing.INDENT}  sessionphone bot install" not in lines


def test_under_the_kit_a_server_that_answers_or_a_url_the_user_set_gets_no_route(kit_path):
    """The kit changes only the route's lines: a 500 on :8890 is still "check its
    log", and a URL the user set still names their own server."""
    env = {"DC_STT_URL": hearing.OFFERED_URL}
    text = _flat(_kit_lines(env, FakeCurl(body="model load failed", status=500), kit_path))
    assert "check that server's log" in text
    assert hearing.KIT_STATUS not in text and "voicemode" not in text
    env = {"DC_STT_URL": OWN}
    text = _flat(_kit_lines(env, FakeCurl(rc=7), kit_path))
    assert "the DC_STT_URL you set" in text
    assert hearing.KIT_STATUS not in text and "voicemode" not in text


def test_standalone_keeps_the_voicemode_line(tmp_path):
    """No sessionkit on the shell's PATH: the line offers VoiceMode's, as before."""
    v, _, _ = _probe({}, FakeCurl(rc=7))
    lines = hearing.lines(v, {}, shell={"PATH": str(tmp_path)})
    text = _flat(lines)
    assert "VoiceMode's mlx-audio on 127.0.0.1:8890" in text and "sessionphone does not install it" in text
    assert f"{hearing.INDENT}  {hearing.OFFERED_INSTALL}" in lines
    assert hearing.KIT_STATUS not in text and hearing.KIT_INSTALL not in text
    assert "Session Kit" not in text


# ---- bot install: the lines, the job's env, and before the first call -------

@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    """test_callers_on_install's rig: FakeLaunchd, a scratch HOME and root, an owner."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    given_seat("claude-chat")     # install's signer, given: no seat is made
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(service, "ears_src", lambda: None)
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    return launchd


def _install(monkeypatch, curl, *args):
    """`bot install --owner OWNER`, with curl as hearing.RUN. -> (result, curl)."""
    def run(argv, **kw):
        curl.said = [_stdout()]          # what bot install had said when curl ran
        return curl(argv, **kw)
    monkeypatch.setattr(hearing, "RUN", run)
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER, *args])
    return r, curl


def _stdout():
    """What CliRunner has captured so far (its stdout wraps a BytesIO)."""
    sys.stdout.flush()
    return sys.stdout.buffer.getvalue().decode()


def _hearing_block(output):
    lines = output.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("hearing:"))
    return lines[i - 1], lines[i:]


def test_install_ends_with_no_and_the_route_when_nothing_answers(rig, monkeypatch):
    """(a) refused: NO, the route's command line, sessionphone installs nothing, exit 0."""
    r, _ = _install(monkeypatch, FakeCurl(rc=7))
    assert r.exit_code == 0, r.output
    probing, block = _hearing_block(r.output)
    assert probing == f"probing {DEFAULT} (read-only)"
    assert block[0].startswith("hearing:   NO.")
    text = _flat(block)
    assert "sessionphone does not install it" in text and "curl rc=7" in text
    assert any(ln.strip() == "voicemode service install mlx-audio" for ln in block)
    assert r.output.splitlines()[-len(block):] == block        # the hearing block ends the output
    assert r.output.index("log:") < r.output.index("probing")   # after the job is installed


def test_install_said_probing_before_it_sent(rig, monkeypatch):
    r, curl = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    before = curl.calls[0]["said"][0].splitlines()
    assert before[-1] == f"probing {DEFAULT} (read-only)"       # the last thing said when curl ran
    assert "hearing:" not in "\n".join(before)


def test_install_with_a_url_of_the_users_own_gives_no_route(rig, monkeypatch):
    """(b) a user's own URL: no route lines, and the "you set" wording."""
    monkeypatch.setenv("DC_STT_URL", OWN)
    r, _ = _install(monkeypatch, FakeCurl(rc=6))
    assert r.exit_code == 0, r.output
    _, block = _hearing_block(r.output)
    text = _flat(block)
    assert OWN_BARE in text and "the DC_STT_URL you set" in text
    assert "voicemode" not in text


def test_install_under_the_kit_points_at_ears_services(rig, monkeypatch, kit_path):
    """`bot install` judges the kit on the shell it ran in (cli passes os.environ)."""
    monkeypatch.setattr(hearing, "under_kit", UNDER_KIT)
    monkeypatch.setenv("PATH", f"{kit_path}:{os.environ.get('PATH', '')}")
    r, _ = _install(monkeypatch, FakeCurl(rc=7))
    assert r.exit_code == 0, r.output
    _, block = _hearing_block(r.output)
    text = _flat(block)
    assert block[0].startswith("hearing:   NO.") and "This is Session Kit" in text
    assert any(ln.strip() == hearing.KIT_STATUS for ln in block)
    assert any(ln.strip() == hearing.KIT_INSTALL_PHONE for ln in block)
    assert any(ln.strip() == hearing.KIT_INSTALL_AGENT for ln in block)
    assert not any(ln.strip() == hearing.KIT_INSTALL for ln in block)
    assert "voicemode service install" not in r.output


def test_a_refused_install_probes_nothing(rig, monkeypatch):
    """(c) an install refused with exit 2 writes nothing and says no `probing` line."""
    curl = FakeCurl()
    monkeypatch.setattr(hearing, "RUN", curl)
    r = CliRunner().invoke(cli, ["bot", "install"])               # no owner, no callers: refused
    assert r.exit_code == 2, r.output
    assert "probing" not in r.output and "hearing:" not in r.output and curl.calls == []


def test_an_unloaded_job_probes_nothing(rig, monkeypatch):
    rig.never_loads = True
    curl = FakeCurl()
    monkeypatch.setattr(hearing, "RUN", curl)
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 1 and "probing" not in r.output and curl.calls == []


def test_a_runner_that_raises_is_no_never_a_traceback(rig, monkeypatch):
    """(d) curl cannot run: NO with the error."""
    r, _ = _install(monkeypatch, FakeCurl(raises=OSError("curl: exec format error")))
    assert r.exit_code == 0 and r.exception is None, r.output
    text = _flat(_hearing_block(r.output)[1])
    assert text.startswith("hearing: NO.") and "curl could not run: curl: exec format error" in text
    assert "Traceback" not in r.output


def _no_tmp(*a, **k):
    raise PermissionError(13, "Permission denied", "/unwritable/tmp")


def test_an_unwritable_tmpdir_is_not_confirmed_never_a_traceback(rig, monkeypatch):
    """do-001 review N1: the clip cannot be written, after the job is installed.
    Nothing is sent, so the STT was never asked: `not confirmed`, naming the error."""
    monkeypatch.setattr(hearing.tempfile, "mkstemp", _no_tmp)
    r, curl = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0 and r.exception is None, r.output
    assert "Traceback" not in r.output and "probing" not in r.output and curl.calls == []
    _, block = _hearing_block(r.output)
    text = _flat(block)
    assert text.startswith("hearing: not confirmed.")
    assert f"{DEFAULT} (DC_STT_URL, the default) was not asked" in text
    assert "the test clip could not be written" in text and "Permission denied" in text
    assert "Nothing was sent" in text and "sessionphone bot install" in text
    assert "will hear the caller" not in text and "will not hear" not in text   # no claim either way
    assert r.output.splitlines()[-len(block):] == block


def _disk_full():
    raise OSError(28, "No space left on device")


def test_a_clip_write_that_fails_leaves_no_file_and_sends_nothing(monkeypatch, tmp_path):
    """mkstemp made the file, then the write failed: the file is removed, nothing is said or sent."""
    monkeypatch.setattr(hearing.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(hearing, "clip", _disk_full)
    v, curl, said = _probe({"DC_STT_URL": hearing.OFFERED_URL})
    assert v.answer == "not confirmed" and not v.sent and not v.answered
    assert "No space left on device" in v.came_back
    assert curl.calls == [] and said == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("spoil, why", [
    (lambda p: p.unlink(), "No such file"),                                       # OSError
    (lambda p: p.write_bytes(b"not a plist"), "Invalid file"),                   # InvalidFileException
    (lambda p: p.write_bytes(plistlib.dumps(["a", "list"])), "has no attribute"),  # not a dict
], ids=["gone", "garbled", "not-a-dict"])
def test_a_plist_that_cannot_be_read_back_is_not_confirmed(rig, monkeypatch, spoil, why):
    """cli._hearing: the job's env cannot be read back, so DC_STT_URL is not checked,
    nothing is sent, and the install still exits 0 (do-001 review, the testing gap)."""
    real = service.install

    def install(**kw):
        p = real(**kw)
        spoil(p)
        return p
    monkeypatch.setattr(service, "install", install)
    r, curl = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0 and r.exception is None, r.output
    assert "Traceback" not in r.output and "probing" not in r.output and curl.calls == []
    last = r.output.splitlines()[-1]
    assert last.startswith("hearing:   not confirmed. The job's environment could not be read back")
    assert "so DC_STT_URL was not checked" in last and why in last


def test_judged_on_the_jobs_env_dc_stt_url(rig, monkeypatch):
    monkeypatch.setenv("DC_STT_URL", OWN)
    r, curl = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0, r.output
    assert OWN_BARE in curl.calls[0]["argv"]
    assert curl.calls[0]["kw"]["env"]["PATH"] == service.BASE_PATH    # the job's PATH, not this shell's


def test_judged_on_the_jobs_env_not_the_alias(rig, monkeypatch, capsys):
    """Only PIP_CHAT_STT_URL in the shell: the job never carries it, so the default is probed."""
    monkeypatch.setenv("PIP_CHAT_STT_URL", OWN)
    r, curl = _install(monkeypatch, FakeCurl(rc=7))
    assert r.exit_code == 0, r.output
    assert DEFAULT in curl.calls[0]["argv"] and OWN_BARE not in curl.calls[0]["argv"]
    assert "PIP_CHAT_STT_URL is set in this shell" in _flat(r.output.splitlines())


@pytest.mark.parametrize(("env", "curl", "want"), [
    ({"DC_STT_URL": hearing.OFFERED_URL}, FakeCurl(body="no helper", status=500),
     ["hearing:   NO.", "answered HTTP 500: no helper"]),
    ({"DC_STT_URL": hearing.OFFERED_URL}, FakeCurl(rc=28), ["hearing:   not confirmed.", "curl rc=28"]),
    ({"DC_STT_URL": hearing.OFFERED_URL, "DC_EARS": "1"}, FakeCurl(),
     ["hearing:   yes.", "Calls were not checked", "Voice notes will be transcribed."]),
], ids=["500", "rc28", "dc-ears"])
def test_install_branches(rig, monkeypatch, env, curl, want):
    for k, val in env.items():
        monkeypatch.setenv(k, val)
    r, curl = _install(monkeypatch, curl)
    assert r.exit_code == 0, r.output
    text = _flat(_hearing_block(r.output)[1])
    for w in want:
        assert " ".join(w.split()) in text, (w, text)
    if "DC_EARS" in env:
        assert "will hear the caller" not in text


def test_install_with_no_model_is_refused_before_the_probe(rig, monkeypatch):
    """A DC_STT_URL with no model no longer installs and then says the bot
    will refuse (PHONE-151): install refuses first, exit 2, and nothing is
    probed (tests/test_install_stt.py has the rest)."""
    monkeypatch.setenv("DC_STT_URL", "http://127.0.0.1:7000/v1/audio/transcriptions")
    r, curl = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 2, r.output
    assert "DC_STT_MODEL" in r.output
    assert curl.calls == [] and "probing" not in r.output and "hearing:" not in r.output


# ---- criterion 2: the verdict is the STT's, not ears' ------------------------

@pytest.fixture
def fake_ears(tmp_path, monkeypatch):
    """An importable ears with a previews module, on sys.path and as DC_EARS_SRC."""
    site = tmp_path / "site"
    (site / "ears").mkdir(parents=True)
    (site / "ears" / "__init__.py").write_text("")
    (site / "ears" / "previews.py").write_text("def on_turn(*a, **k):\n    return None\n")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setenv("DC_EARS_SRC", str(site))
    monkeypatch.delitem(sys.modules, "ears", raising=False)
    return site


def test_ears_importable_but_the_stt_refused_is_no(rig, monkeypatch, fake_ears):
    monkeypatch.setenv("DC_EARS_SRC", str(fake_ears))           # the rig stripped DC_*: set it again
    import ears.previews  # noqa: F401 - it imports
    r, _ = _install(monkeypatch, FakeCurl(rc=7))
    assert r.exit_code == 0, r.output
    text = _flat(_hearing_block(r.output)[1])
    assert text.startswith("hearing: NO.")
    for must in (DEFAULT, "DC_STT_URL", "a half-second test clip", "curl rc=7"):
        assert must in text


class _BlockEars(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "ears":
            raise ModuleNotFoundError(f"blocked: {name}", name=name)


def test_ears_blocked_but_the_stt_answering_is_yes(rig, monkeypatch):
    monkeypatch.setattr(sys, "meta_path", [_BlockEars(), *sys.meta_path])
    monkeypatch.delitem(sys.modules, "ears", raising=False)
    with pytest.raises(ModuleNotFoundError):
        import ears  # noqa: F401
    r, _ = _install(monkeypatch, FakeCurl(body='{"text": ""}'))
    assert r.exit_code == 0, r.output
    text = _flat(_hearing_block(r.output)[1])
    assert text.startswith("hearing: yes.")
    for must in (DEFAULT, "DC_STT_URL", "a half-second test clip", "HTTP 200"):
        assert must in text


def test_the_probe_never_asks_ears_src_or_imports_ears(monkeypatch):
    def boom():
        raise AssertionError("the hearing probe asked ears_src()")
    monkeypatch.setattr(service, "ears_src", boom)
    monkeypatch.setattr(sys, "meta_path", [_BlockEars(), *sys.meta_path])
    v, _, _ = _probe({"DC_STT_URL": hearing.OFFERED_URL})
    assert v.answer == "yes"
    with open(hearing.__file__) as fh:
        code = "\n".join(ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#"))
    assert "import ears" not in code and "ears_src()" not in code.split('"""', 2)[2]


# ---- setup probes too, before the link: tests/test_stt_before_link.py (PHONE-251)
# (review D4's "setup never probes" pin is reversed there.)


# ---- the tripwire: no test reaches a live STT (conftest stt_tripwire) --------
# Every launch below that the guard should catch runs with a PATH holding no
# curl, so even a broken guard reaches nothing: curl is not found.

def test_dc_stt_url_is_the_tripwires_closed_port(stt_tripwire):
    url = os.environ["DC_STT_URL"]
    assert url == "mlx-audio+http://127.0.0.1:1/v1/audio/transcriptions"
    v = hearing.probe(dict(os.environ), say=lambda _: None)       # the real RUN, real curl
    assert v.answer == "NO" and v.came_back.startswith("curl rc=7")
    assert not stt_tripwire.exists() or stt_tripwire.read_text() == ""


def test_a_probe_of_the_default_with_dc_stripped_is_caught(stt_tripwire, monkeypatch, tmp_path):
    """A rig that strips DC_* falls back to the live default (:8890): refused and recorded."""
    monkeypatch.delenv("DC_STT_URL")
    monkeypatch.setattr(hearing, "RUN", subprocess.run)
    v = hearing.probe({"PATH": str(tmp_path)}, say=lambda _: None)
    assert v.answer == "NO" and v.came_back.startswith("curl rc=7"), v
    calls = stt_tripwire.read_text()
    assert "test_a_probe_of_the_default_with_dc_stripped_is_caught" in calls
    assert f"LIVE STT {DEFAULT}" in calls and calls.count("\n") == 1
    stt_tripwire.write_text("")       # this test meant to trip it: clear, so teardown passes


@pytest.mark.parametrize(("args", "kw"), [
    (["curl", "-s", "-X", "POST", OFFERED, "-F", "file=@/x.wav"], {}),
    (["/nonexistent/bin/curl", "-X", "POST", "http://localhost:2022/inference", "-F", "file=@/x.wav"], {}),
    (f"curl -s -F file=@/x.wav {OFFERED}", {"shell": True}),
    (["curl", "-F", "file=@/x.wav", "https://api.openai.com/v1/audio/transcriptions"], {}),
], ids=["mlx-audio", "whisper-abs", "shell", "hosted"])
def test_any_live_stt_is_refused(stt_tripwire, tmp_path, args, kw):
    r = subprocess.run(args, env={"PATH": str(tmp_path)}, capture_output=True, check=False, **kw)
    assert r.returncode == 7
    assert stt_tripwire.read_text().count("| LIVE STT ") == 1
    stt_tripwire.write_text("")


class _StubSTT(BaseHTTPRequestHandler):
    got: ClassVar[list] = []     # reset by the test that starts it

    def do_POST(self):
        _StubSTT.got.append(self.rfile.read(int(self.headers["Content-Length"])))
        reply = json.dumps({"text": ""}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *a):
        pass


@pytest.mark.skipif(not __import__("shutil").which("curl"), reason="needs curl")
def test_a_stub_server_on_an_ephemeral_port_is_allowed_end_to_end(stt_tripwire):
    """The design's manual run (review D3), as a test: the probe, real curl, a stub
    STT this test starts. Never `bot install` on a fleet host."""
    srv = HTTPServer(("127.0.0.1", 0), _StubSTT)
    _StubSTT.got = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"mlx-audio+http://127.0.0.1:{srv.server_port}/v1/audio/transcriptions"
        said = []
        v = hearing.probe({"DC_STT_URL": url, "PATH": os.environ["PATH"]}, say=said.append)
    finally:
        srv.shutdown()
        srv.server_close()
    assert v.answer == "yes", v
    assert said == [f"probing {url.split('+', 1)[1]} (read-only)"]
    body = _StubSTT.got[0]
    assert f'name="model"\r\n\r\n{PARAKEET}'.encode() in body and b"RIFF" in body
    assert not stt_tripwire.exists() or stt_tripwire.read_text() == ""


# ---- the speech weights: `downloading` with progress, never a bare timeout ----
# `ears services weights --json` (schema ears.weights/1) reads the speech
# weights from the Hugging Face cache. These build its answer as fixtures; no
# test runs a real ears (conftest no_ears_weights).

TOTALS = {"stt": 2_508_940_517, "tts": 355_322_783, "voices": 56_471_015}
REPOS = {"stt": PARAKEET, "tts": "mlx-community/Kokoro-82M-bf16", "voices": "example/voices"}
SHA = "0" * 40


def _row(use, state, done=None, total="known"):
    total = TOTALS[use] if total == "known" else total
    if done is None:
        done = TOTALS[use] if state == "cached" else 0
    return {"repo": REPOS[use], "use": use, "state": state, "bytes_done": done,
            "bytes_total": total, "expected": SHA, "cached_ref": SHA if state == "cached" else None,
            "drift": False}


def _doc(*rows, **over):
    """The reader's one object: top-level state and bytes from the rows, as the contract sums them."""
    rows = list(rows) or [_row(u, "cached") for u in TOTALS]
    states = {r["state"] for r in rows}
    totals = [r["bytes_total"] for r in rows]
    doc = {"schema": "ears.weights/1", "cache": "/hf/hub",
           "state": "downloading" if "downloading" in states else "cached" if states == {"cached"} else "missing",
           "bytes_done": sum(r["bytes_done"] for r in rows),
           "bytes_total": None if None in totals else sum(totals), "weights": rows}
    doc.update(over)
    return doc


class FakeEars:
    """Stands in for hearing.WEIGHTS_RUN: records each argv and answers with stdout and rc, or raises."""

    def __init__(self, doc=None, rc=0, stdout=None, raises=None):
        self.out = stdout if stdout is not None else json.dumps(_doc() if doc is None else doc) + "\n"
        self.rc, self.raises, self.calls = rc, raises, []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": argv, "kw": kw})
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.rc, self.out, "")


@pytest.fixture
def ears_path(tmp_path):
    """A PATH on which an `ears` is found (it never runs: the reader is a FakeEars)."""
    bin_ = tmp_path / "ears-bin"
    bin_.mkdir()
    exe = bin_ / hearing.EARS_COMMAND
    exe.write_text("#!/bin/sh\nexit 99\n")
    exe.chmod(0o755)
    return str(bin_)


def _read(ears_path, fake):
    return hearing.weights_state({"PATH": ears_path}, run=fake)


DOWNLOADING = _doc(_row("stt", "downloading", done=412_000_000), _row("tts", "cached"), _row("voices", "cached"))
MISSING = _doc(_row("stt", "missing"), _row("tts", "missing"), _row("voices", "missing"))


def test_the_reader_runs_the_verb_found_on_path(ears_path):
    fake = FakeEars()
    w = _read(ears_path, fake)
    [call] = fake.calls
    assert call["argv"] == [os.path.join(ears_path, "ears"), "services", "weights", "--json"]
    assert call["kw"]["timeout"] == hearing.WEIGHTS_TIMEOUT_S and call["kw"]["capture_output"]
    assert w == hearing.Weights("cached", sum(TOTALS.values()), sum(TOTALS.values()), None)
    assert not w.wait


@pytest.mark.parametrize(("doc", "state", "size"), [
    (DOWNLOADING, "downloading", TOTALS["stt"]),
    (_doc(_row("stt", "cached"), _row("tts", "downloading", done=5), _row("voices", "missing")), "downloading",
     TOTALS["tts"]),
    (MISSING, "missing", TOTALS["stt"] + TOTALS["tts"]),
    (_doc(_row("stt", "cached"), _row("tts", "missing"), _row("voices", "cached")), "missing", TOTALS["tts"]),
    (_doc(_row("stt", "missing", total=None), _row("tts", "missing"), _row("voices", "cached")), "missing", None),
], ids=["stt-downloading", "tts-downloading", "all-missing", "tts-missing", "size-unknown"])
def test_each_state_is_keyed_on_stt_and_tts(ears_path, doc, state, size):
    w = _read(ears_path, FakeEars(doc))
    assert (w.state, w.size, w.wait) == (state, size, True)
    assert (w.done, w.total) == (doc["bytes_done"], doc["bytes_total"])   # the top-level bytes


@pytest.mark.parametrize("voices", ["missing", "downloading"])
def test_voices_alone_is_not_a_wait(ears_path, voices):
    """A host installed before the prefetch: voices (and so the top level) is not
    cached while stt and tts are, and speech works. No wait."""
    doc = _doc(_row("stt", "cached"), _row("tts", "cached"), _row("voices", voices))
    assert doc["state"] == voices                       # the top level says otherwise
    w = _read(ears_path, FakeEars(doc))
    assert w.state == "cached" and not w.wait


def test_new_keys_under_schema_1_are_read(ears_path):
    doc = _doc(_row("stt", "downloading", done=7), _row("tts", "cached"), _row("voices", "cached"), later="x")
    doc["weights"][0]["eta_s"] = 30
    assert _read(ears_path, FakeEars(doc)).state == "downloading"


@pytest.mark.parametrize("fake", [
    FakeEars(rc=2, stdout=""),
    FakeEars(rc=1),
    FakeEars(stdout="downloading 412 MB of 2.9 GB\n"),
    FakeEars(stdout=""),
    FakeEars(stdout=json.dumps(_doc()) + "\n" + json.dumps(_doc())),
    FakeEars(_doc(schema="ears.weights/2")),
    FakeEars(_doc(schema=None)),
    FakeEars(stdout="[]"),
    FakeEars(_doc(weights=None)),
    FakeEars(_doc(bytes_done=None)),
    FakeEars(_doc(bytes_done=True)),
    FakeEars(_doc(bytes_total="2.9 GB")),
    FakeEars(_doc(_row("stt", "fetching"), _row("tts", "cached"))),
    FakeEars(_doc(_row("stt", "cached"), _row("voices", "cached"))),
    FakeEars(raises=subprocess.TimeoutExpired("ears", 10)),
    FakeEars(raises=PermissionError(13, "Permission denied", "ears")),
], ids=["older-ears", "rc1", "text", "empty", "two-objects", "schema-2", "no-schema", "not-an-object",
        "no-weights", "no-bytes", "bool-bytes", "text-total", "unknown-state", "no-tts", "timeout", "cannot-run"])
def test_every_fallback_is_none(ears_path, fake):
    assert _read(ears_path, fake) is None


def test_no_ears_on_path_runs_nothing(tmp_path):
    fake = FakeEars(DOWNLOADING)
    assert hearing.weights_state({"PATH": str(tmp_path)}, run=fake) is None
    assert hearing.weights_state({}, run=fake) is None
    assert fake.calls == []


@pytest.mark.parametrize(("n", "text"), [
    (0, "0 B"), (999, "999 B"), (56_471_015, "56 MB"), (412_000_000, "412 MB"),
    (2_508_940_517, "2.5 GB"), (sum(TOTALS.values()), "2.9 GB"),
])
def test_sizes_read_as_a_download_does(n, text):
    assert hearing.size(n) == text


def test_the_reader_is_asked_only_when_the_offered_route_gave_no_answer(ears_path):
    shell = {"PATH": ears_path}
    fake = FakeEars(DOWNLOADING)
    for env, curl in [({}, FakeCurl()),                                      # yes
                      ({"DC_STT_URL": OWN}, FakeCurl(rc=28)),                 # the user's own server
                      ({"DC_STT_URL": "http://127.0.0.1:7000/v1/audio/transcriptions"}, FakeCurl())]:  # refused
        v, _, _ = _probe(env, curl)
        assert hearing.weights_for(v, shell, run=fake) is None
    assert fake.calls == []
    for curl in (FakeCurl(rc=28), FakeCurl(rc=7)):
        v, _, _ = _probe({}, curl)
        assert hearing.weights_for(v, shell, run=fake).state == "downloading"
    assert len(fake.calls) == 2


def _weights(doc, ears_path):
    return _read(ears_path, FakeEars(doc))


def test_not_confirmed_while_downloading_says_downloading_with_progress(ears_path, tmp_path):
    w = _weights(DOWNLOADING, ears_path)
    v, _, _ = _probe({}, FakeCurl(rc=28))
    lines = hearing.lines(v, {}, shell={"PATH": str(tmp_path)}, weights=w)
    text = _flat(lines)
    done = 412_000_000 + TOTALS["tts"] + TOTALS["voices"]
    assert lines[0].startswith(f"hearing:   downloading {hearing.size(done)} of 2.9 GB.")
    assert "not confirmed" not in text
    assert f"{DEFAULT} (DC_STT_URL, the default) was sent a half-second test clip" in text
    assert "curl rc=28" in text and "still downloading" in text
    assert "The first call waits for that download to finish." in text
    assert "sessionphone bot install` again" in text
    assert hearing.KIT_STATUS not in text                    # standalone


def test_a_total_it_cannot_know_says_so_far(ears_path, tmp_path):
    doc = _doc(_row("stt", "downloading", done=412_000_000, total=None), _row("tts", "cached"),
               _row("voices", "cached"))
    assert doc["bytes_total"] is None
    v, _, _ = _probe({}, FakeCurl(rc=28))
    lines = hearing.lines(v, {}, shell={"PATH": str(tmp_path)}, weights=_weights(doc, ears_path))
    done = 412_000_000 + TOTALS["tts"] + TOTALS["voices"]
    assert lines[0].startswith(f"hearing:   downloading, {hearing.size(done)} so far.")
    assert " of " not in lines[0]


def test_not_confirmed_while_missing_says_the_first_request_fetches_them(ears_path, tmp_path):
    v, _, _ = _probe({}, FakeCurl(rc=28))
    text = _flat(hearing.lines(v, {}, shell={"PATH": str(tmp_path)}, weights=_weights(MISSING, ears_path)))
    assert text.startswith("hearing: not confirmed.")
    assert "The speech models it needs are not downloaded yet (about 2.9 GB): the first request fetches them." in text
    assert "The first call waits for that download." in text


def test_missing_with_an_unknown_size_names_no_size(ears_path, tmp_path):
    doc = _doc(_row("stt", "missing", total=None), _row("tts", "missing"), _row("voices", "cached"))
    v, _, _ = _probe({}, FakeCurl(rc=28))
    text = _flat(hearing.lines(v, {}, shell={"PATH": str(tmp_path)}, weights=_weights(doc, ears_path)))
    assert "not downloaded yet: the first request fetches them" in text and "GB" not in text


@pytest.mark.parametrize("curl", [FakeCurl(rc=28), FakeCurl(rc=7), FakeCurl(body="boom", status=500)],
                         ids=["not-confirmed", "NO", "NO-500"])
@pytest.mark.parametrize("doc", [_doc(), _doc(_row("stt", "cached"), _row("tts", "cached"), _row("voices", "missing")),
                                 None], ids=["cached", "voices-missing-alone", "fallback"])
def test_cached_voices_alone_or_no_reader_leaves_the_line_as_it_was(ears_path, tmp_path, curl, doc):
    w = _weights(doc, ears_path) if doc else None
    v, _, _ = _probe({}, curl)
    shell = {"PATH": str(tmp_path)}
    assert hearing.lines(v, {}, shell=shell, weights=w) == hearing.lines(v, {}, shell=shell)


def test_a_url_of_the_users_own_is_not_explained_by_this_macs_weights(ears_path):
    """The weights are the offered route's: a server elsewhere is told nothing about them."""
    env = {"DC_STT_URL": OWN}
    v, _, _ = _probe(env, FakeCurl(rc=28))
    w = _weights(DOWNLOADING, ears_path)
    assert hearing.lines(v, env, shell={}, weights=w) == hearing.lines(v, env, shell={})


@pytest.mark.parametrize(("doc", "says"), [
    (DOWNLOADING, "Its speech models are downloading "),
    (MISSING, "Its speech models are not downloaded yet (about 2.9 GB)"),
], ids=["downloading", "missing"])
def test_no_keeps_its_advice_and_adds_where_the_models_stand(ears_path, tmp_path, doc, says):
    v, _, _ = _probe({}, FakeCurl(rc=7))
    shell = {"PATH": str(tmp_path)}
    lines = hearing.lines(v, {}, shell=shell, weights=_weights(doc, ears_path))
    text = _flat(lines)
    assert lines[0].startswith("hearing:   NO.") and says in text
    assert "the first call waits for that download" in text
    for line in hearing.lines(v, {}, shell=shell):            # today's advice, every line of it
        assert line.strip() in text or _flat([line]) in text


def test_voice_notes_only_waits_for_the_first_voice_note(ears_path):
    env = {"DC_CALLS": "off"}
    v, _, _ = _probe(env, FakeCurl(rc=28))
    text = _flat(hearing.lines(v, env, shell={}, weights=_weights(DOWNLOADING, ears_path)))
    assert "The first voice note waits" in text and "first call" not in text


def test_under_the_kit_the_download_points_at_ears_services_status(ears_path, kit_path):
    for doc in (DOWNLOADING, MISSING):
        v, _, _ = _probe({}, FakeCurl(rc=28))
        lines = hearing.lines(v, {}, shell={"PATH": kit_path}, weights=_weights(doc, ears_path))
        text = _flat(lines)
        assert f"{hearing.INDENT}  {hearing.KIT_STATUS}" in lines
        assert "Watch the download (read-only):" in text
        assert "voicemode" not in text.lower() and hearing.KIT_INSTALL not in text


def test_under_the_kit_no_while_downloading_keeps_the_kits_rows(ears_path, kit_path):
    v, _, _ = _probe({}, FakeCurl(rc=7))
    lines = hearing.lines(v, {}, shell={"PATH": kit_path}, weights=_weights(DOWNLOADING, ears_path))
    text = _flat(lines)
    assert "Its speech models are downloading" in text and "This is Session Kit" in text
    assert f"{hearing.INDENT}  {hearing.KIT_STATUS}" in lines and "voicemode service install" not in text


def test_install_says_downloading_through_the_reader(rig, monkeypatch, ears_path):
    """End to end: the reader is found on the PATH `bot install` ran with."""
    fake = FakeEars(DOWNLOADING)
    monkeypatch.setattr(hearing, "WEIGHTS_RUN", fake)
    monkeypatch.setenv("PATH", f"{ears_path}:{os.environ.get('PATH', '')}")
    r, _ = _install(monkeypatch, FakeCurl(rc=28))
    assert r.exit_code == 0, r.output
    _, block = _hearing_block(r.output)
    assert block[0].startswith("hearing:   downloading ") and " of 2.9 GB." in block[0]
    assert len(fake.calls) == 1
    assert r.output.splitlines()[-len(block):] == block


def test_install_under_the_kit_says_downloading_and_points_at_status(rig, monkeypatch, ears_path, kit_path):
    monkeypatch.setattr(hearing, "under_kit", UNDER_KIT)
    monkeypatch.setattr(hearing, "WEIGHTS_RUN", FakeEars(MISSING))
    monkeypatch.setenv("PATH", f"{kit_path}:{ears_path}:{os.environ.get('PATH', '')}")
    r, _ = _install(monkeypatch, FakeCurl(rc=28))
    assert r.exit_code == 0, r.output
    _, block = _hearing_block(r.output)
    text = _flat(block)
    assert text.startswith("hearing: not confirmed.") and "not downloaded yet (about 2.9 GB)" in text
    assert any(ln.strip() == hearing.KIT_STATUS for ln in block)


def test_install_with_an_older_ears_keeps_not_confirmed(rig, monkeypatch, ears_path, no_ears_weights):
    """The verb is absent (conftest's reader exits 2, as an older ears does): today's wording."""
    monkeypatch.setenv("PATH", f"{ears_path}:{os.environ.get('PATH', '')}")
    r, _ = _install(monkeypatch, FakeCurl(rc=28))
    assert r.exit_code == 0, r.output
    text = _flat(_hearing_block(r.output)[1])
    assert text.startswith("hearing: not confirmed.") and "A first request can be slow" in text
    assert no_ears_weights == [[os.path.join(ears_path, "ears"), "services", "weights", "--json"]]


def test_install_that_hears_never_asks_the_reader(rig, monkeypatch, ears_path, no_ears_weights):
    monkeypatch.setenv("PATH", f"{ears_path}:{os.environ.get('PATH', '')}")
    r, _ = _install(monkeypatch, FakeCurl())
    assert r.exit_code == 0 and "hearing:   yes." in r.output, r.output
    assert no_ears_weights == []

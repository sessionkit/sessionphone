"""PHONE-259: `bot install` refuses a named profile on the default profile's
callers file or allow-list, and `bot status` names the refusal.

A named profile's bot refuses to START when its callers file or allow-list
resolves (by realpath) to the default profile's own, <root>/callers.txt or
<root>/channels.txt (bot.check_profile_files). `bot install` used to write and
load that job anyway, so launchd restarted a bot that refused every time. Now
install asks the same rule (dc_env.profile_files_problem) of the env the job
will carry, before it writes anything, and exits 2 as it does for an STT URL
with no model (NoSTTModel). And `bot status` reads the profile's own bot.log,
where the refusal lands (~/.sessionkit/phone/log/<bot>/<profile>/), not the
default profile's.

Fakes only: a tmp root, conftest's FakeLaunchd as service._launchctl, plist_path
in tmp. No account, no relay, no real launchd, no mail.
"""
import json
import os
import pathlib
import plistlib
import subprocess
import sys

import pytest
from _kit_log import kit_log
from click.testing import CliRunner

from sessionphone import service
from sessionphone.bridge import bot, dc_env
from sessionphone.cli import main as cli

OWNER = "owner@phone.example.org"
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
CALLERS_LINE = "old@phone.example.org  key:0123456789ABCDEF0123456789ABCDEF01234567  # the root's\n"
CHANNELS_LINE = "old old@phone.example.org\n"
CASES = ["DC_CALLERS_FILE", "DC_CHANNELS_FILE", "file-link", "folder-link"]


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    """A `x` profile on a tmp root that already holds the default profile's
    callers file and allow-list. -> the root."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    given_seat("ada-chat")     # install's signer, given: no seat is made
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "callers.txt").write_text(CALLERS_LINE)
    (root / "callers.txt").chmod(0o600)
    (root / "channels.txt").write_text(CHANNELS_LINE)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setenv("DC_HOST", "mac.example.net")
    monkeypatch.setenv("DC_AGENT", "ada")
    monkeypatch.setenv("DC_PROFILE", "x")
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    return root


def _share(case, root, monkeypatch):
    """Make the `x` profile read the default profile's file, the CASE way.
    -> (the file it reads, as the refusal names it; the setting; the root's file)."""
    if case == "DC_CALLERS_FILE":
        monkeypatch.setenv(case, str(root / "callers.txt"))
        return root / "callers.txt", case, root / "callers.txt"
    if case == "DC_CHANNELS_FILE":
        monkeypatch.setenv(case, str(root / "channels.txt"))
        return root / "channels.txt", case, root / "channels.txt"
    if case == "file-link":        # the profile's own path, a link to the root's file
        (root / "x").mkdir(mode=0o700)
        os.symlink(root / "callers.txt", root / "x" / "callers.txt")
        return root / "x" / "callers.txt", None, root / "callers.txt"
    os.symlink(root, root / "x")   # folder-link: the profile's folder, a link to the root itself
    return root / "x" / "channels.txt", None, root / "channels.txt"   # the allow-list is judged first


def _snapshot(root):
    """Every entry under ROOT, links not followed: a file's bytes, a link's target."""
    out = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = pathlib.Path(d) / n
            out[str(p.relative_to(root))] = (f"-> {os.readlink(p)}" if p.is_symlink()
                                             else p.read_bytes() if p.is_file() else "dir")
    return out


# ---- install refuses, writing nothing ---------------------------------------------------

@pytest.mark.parametrize("case", CASES)
def test_bot_install_refuses_a_profile_on_the_default_profiles_file(rig, monkeypatch, tmp_path,
                                                                    launchd, case):
    root = rig
    mine, var, theirs = _share(case, root, monkeypatch)
    before = _snapshot(root)
    boxes = []
    monkeypatch.setattr(service.seat, "ensure_box", lambda *a, **k: boxes.append(a))
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", "new@phone.example.org"])
    assert r.exit_code == 2, r.output
    assert "installed:" not in r.output
    out = " ".join(r.output.split())      # the refusal, unwrapped
    assert "profile x's" in out and str(mine) in out and "the default profile's own" in out
    if var:          # names the setting and the fix
        assert f"Unset {var} (this profile then reads {root / 'x' / theirs.name})" in out
    else:            # names the link it resolves through and the fix
        assert f"which resolves to {os.path.realpath(theirs)}" in out
        assert f"Make {mine} a file of this profile's own, not a link to the default profile's." in out
    # nothing written, nothing loaded: no plist, no launchctl, no box, the root's files as they were
    assert launchd.calls == []
    assert not (tmp_path / "job.plist").exists() and not (tmp_path / "job.plist.part").exists()
    assert boxes == []
    assert _snapshot(root) == before
    assert (root / "callers.txt").read_text() == CALLERS_LINE      # --owner never reached add_owner
    assert (root / "channels.txt").read_text() == CHANNELS_LINE    # no channels seed


def test_bot_install_refuses_before_the_seat_is_made(rig, monkeypatch):
    """No signer given: install would make the seat (seat.run_agent). The refusal
    comes first."""
    for k in ("DC_MESSAGE", "DC_SIGNER_SOCK", "DC_SIGNER_PUB"):
        monkeypatch.delenv(k)
    monkeypatch.setenv("DC_CALLERS_FILE", str(rig / "callers.txt"))
    made = []
    for verb in ("plan", "key", "run_agent", "ensure_box"):
        monkeypatch.setattr(service.seat, verb, lambda *a, _v=verb, **k: made.append(_v))
    with pytest.raises(service.ProfileFiles):
        service.install("ada", owner=OWNER)
    assert made == []


# ---- one source: the bot's own words -----------------------------------------------------

@pytest.mark.parametrize("case", CASES)
def test_the_refusal_is_the_bots_own_start_check(rig, monkeypatch, tmp_path, case):
    """install raises exactly what dc_env.profile_files_problem says of the job's
    env, and the bot, started on that env, says the same text."""
    _share(case, rig, monkeypatch)
    seen = []
    real = dc_env.profile_files_problem

    def spy(environ=None, root=None, profile_name=None):
        seen.append(dict(environ))
        return real(environ, root, profile_name)
    monkeypatch.setattr(dc_env, "profile_files_problem", spy)
    with pytest.raises(service.ProfileFiles) as e:
        service.install("ada", owner=OWNER)
    job_env = seen[0]
    assert job_env["DC_PROFILE"] == "x" and job_env["SESSIONPHONE_ROOT"] == str(rig)   # the job's env
    assert str(e.value) == real(job_env)
    # the bot's start check, on that env as its os.environ, in a fresh process
    env = {**job_env, "HOME": str(tmp_path / "home"), "PATH": os.environ["PATH"]}
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); from sessionphone.bridge import bot; "
             "import json; print(json.dumps(bot.check_profile_files()))")
    out = subprocess.run([sys.executable, "-c", probe, str(SRC)], env=env,
                         capture_output=True, text=True, timeout=120, check=True)
    assert json.loads(out.stdout) == str(e.value)


def test_the_callers_reason_says_who_may_ring_and_the_allow_list_keeps_chat_ids(rig, monkeypatch):
    monkeypatch.setenv("DC_CALLERS_FILE", str(rig / "callers.txt"))
    why = dc_env.profile_files_problem(dict(os.environ))
    assert "It says who may ring, so a caller approved on one profile could ring the other." in why
    assert "Chat ids" not in why
    monkeypatch.delenv("DC_CALLERS_FILE")
    monkeypatch.setenv("DC_CHANNELS_FILE", str(rig / "channels.txt"))
    why = dc_env.profile_files_problem(dict(os.environ))
    assert "Chat ids are per account, so an entry for one account would open a chat on the other." in why


def test_the_bot_wrapper_reads_the_bots_bound_root_and_profile(tmp_path, monkeypatch):
    """bot.check_profile_files passes the bot's ROOT and PROFILE, bound at import,
    not root_of(os.environ): a later SESSIONPHONE_ROOT does not move it."""
    for k in ("DC_CHANNELS_FILE", "DC_CALLERS_FILE", "DC_ROOT", "DC_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(dc_env, "ROOT", tmp_path)
    monkeypatch.setattr(bot, "PROFILE", "public")
    os.symlink(tmp_path, tmp_path / "public")
    why = bot.check_profile_files()
    assert why == dc_env.profile_files_problem(os.environ, tmp_path, "public")
    assert why.startswith(f"profile public's allow-list is {tmp_path / 'public' / 'channels.txt'}, which resolves")


# ---- what still installs, as before ------------------------------------------------------

def test_the_default_profile_installs_on_the_roots_files(rig, monkeypatch, tmp_path, launchd):
    monkeypatch.delenv("DC_PROFILE")
    monkeypatch.setenv("DC_CALLERS_FILE", str(rig / "callers.txt"))     # the default's own: allowed
    monkeypatch.setenv("DC_CHANNELS_FILE", str(rig / "channels.txt"))
    assert dc_env.profile_files_problem(dict(os.environ)) is None
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 0, r.output
    assert "installed:" in r.output
    assert f"owner:     {OWNER}, approved in {rig / 'callers.txt'}" in r.output
    assert OWNER in (rig / "callers.txt").read_text()
    env = plistlib.loads((tmp_path / "job.plist").read_bytes())["EnvironmentVariables"]
    assert "DC_PROFILE" not in env
    assert any(c[0] == "bootstrap" for c in launchd.calls)


def test_a_named_profile_on_its_own_files_installs(rig, monkeypatch, tmp_path, launchd):
    (rig / "x").mkdir(mode=0o700)
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 0, r.output
    assert "installed:" in r.output
    assert f"owner:     {OWNER}, approved in {rig / 'x' / 'callers.txt'}" in r.output
    assert OWNER in (rig / "x" / "callers.txt").read_text()
    assert (rig / "callers.txt").read_text() == CALLERS_LINE        # the root's file untouched
    env = plistlib.loads((tmp_path / "job.plist").read_bytes())["EnvironmentVariables"]
    assert env["DC_PROFILE"] == "x"
    assert any(c[0] == "bootstrap" for c in launchd.calls)
    assert f"log:       {kit_log('ada', 'x')}" in r.output          # the profile's own log


def test_a_named_profile_on_a_file_of_its_own_named_by_setting_installs(rig, monkeypatch, tmp_path):
    own = tmp_path / "elsewhere-callers.txt"
    monkeypatch.setenv("DC_CALLERS_FILE", str(own))
    r = CliRunner().invoke(cli, ["bot", "install", "--owner", OWNER])
    assert r.exit_code == 0, r.output
    assert OWNER in own.read_text()


# ---- bot status reads the profile's own log --------------------------------------------

def _job(tmp_path, root, **env):
    j = {"Label": service.label("ada"),
         "EnvironmentVariables": {"SESSIONPHONE_ROOT": str(root), "DC_AGENT": "ada", **env},
         "StandardErrorPath": str(kit_log("ada", env.get("DC_PROFILE", ""), "bot.launchd.log"))}
    (tmp_path / "job.plist").write_bytes(plistlib.dumps(j))


def _refusing(launchd, monkeypatch):
    """launchctl print as a loaded job that is not running, last exit 2."""
    def printing(*args):
        launchd.calls.append(args)
        if args[0] == "print" and args[-1].endswith(service.label("ada")):
            return subprocess.CompletedProcess(args, 0, "state = not running\nlast exit code = 2\n", "")
        return subprocess.CompletedProcess(args, 113, "", "Could not find service")
    monkeypatch.setattr(service, "_launchctl", printing)


def test_log_paths_is_the_profiles_own_home(rig, tmp_path):
    _job(tmp_path, rig, DC_PROFILE="x")
    assert service.log_paths("ada")[0] == kit_log("ada", "x")
    _job(tmp_path, rig)
    assert service.log_paths("ada")[0] == kit_log("ada")           # the default profile's own
    _job(tmp_path, rig, DC_PROFILE="x", DC_LOG=str(tmp_path / "given.log"))
    assert service.log_paths("ada")[0] == tmp_path / "given.log"    # DC_LOG still wins


def test_bot_status_names_a_refusing_profile_jobs_refusal_and_its_fix(rig, monkeypatch, tmp_path, launchd):
    """The default bot's line in its bot.log no longer hides the profile
    bot's refusal, which the bot writes to its own (log/ada/x/bot.log) every start."""
    _job(tmp_path, rig, DC_PROFILE="x", DC_CALLERS_FILE=str(rig / "callers.txt"))
    why = dc_env.profile_files_problem({"SESSIONPHONE_ROOT": str(rig), "DC_PROFILE": "x",
                                        "DC_CALLERS_FILE": str(rig / "callers.txt")})
    kit_log("ada").parent.mkdir(parents=True)
    kit_log("ada").write_text("2030-06-09 09:00:00 default bot: online\n")
    kit_log("ada", "x").parent.mkdir()
    kit_log("ada", "x").write_text(f"2030-06-09 09:00:10 REFUSED to start: {why}\n")
    _refusing(launchd, monkeypatch)
    s = CliRunner().invoke(cli, ["bot", "status"])
    assert "running: no (last exit 2)" in s.output, s.output
    assert f"log:     {kit_log('ada', 'x')}" in s.output
    assert f"last:    2030-06-09 09:00:10 REFUSED to start: {why}" in s.output
    assert "Unset DC_CALLERS_FILE" in s.output


def test_bot_status_of_the_default_profile_is_unchanged(rig, monkeypatch, tmp_path, launchd):
    _job(tmp_path, rig)
    kit_log("ada").parent.mkdir(parents=True)
    kit_log("ada").write_text("2030-06-09 09:00:00 default bot: online\n")
    _refusing(launchd, monkeypatch)
    s = CliRunner().invoke(cli, ["bot", "status"])
    assert f"log:     {kit_log('ada')}" in s.output, s.output
    assert "last:    2030-06-09 09:00:00 default bot: online" in s.output

"""A stranger's first `sessionphone bot install`: it never half-sets-up.

With no signer and a sessionmail whose `message seat agent --help` says
`Capability: install`, install runs the seat's agent itself (`message seat
agent --install <agent>-chat`) and the job carries the SEAT_SOCKET and
SEAT_PUB it prints, parsed, never evaluated. A seat not minted, a refusal or a
failure stops install before it writes anything, with sessionmail's own line
and the step that fixes it. Without that capability, install refuses before
it says "installed" or loads anything, naming the steps by hand. With a
signer, it pins DC_MESSAGE, runs the bot's own start check, and makes the box
Delta text goes to. `bot status` shows the last log line. The default To is
sessionmail's host. Must-sign is never lowered.

Every sessionmail here is FAKE_MESSAGE below, answering from a script; launchd
is conftest's FakeLaunchd. Nothing real is run.
"""
import json
import os
import pathlib
import plistlib
import re
import shlex
import socket
import stat
import subprocess

import pytest
from _kit_log import kit_log
from click.testing import CliRunner

from sessionphone import seat, service, settings
from sessionphone.bridge import dc_env, seatmail
from sessionphone.cli import main as cli

OWNER = "owner@phone.example.org"
LABEL = "dev.sessionkit.mail.seat-agent.claude-chat"
REAL_LIST_AGENTS = seat._list_agents     # taken at import: conftest stubs it in every test
PROBE_OK = [2, json.dumps({"sent": "no", "kind": "message-id", "message": "invalid --message-id"}), ""]
NEW_AGENT_HELP = ("Usage: message seat agent ADDRESS [SOCKET] | --install ADDRESS [SOCKET] | "
                  "--uninstall ADDRESS | --template\n\n  run a seat's key\n\nExit: 0 ok\n"
                  "Capability: install\n")
OLD_AGENT_HELP = "Usage: message seat agent ADDRESS SOCKET\n\n  run a seat's key\n\nExit: 0 ok\n"
NOT_MINTED = ("message seat agent: REFUSED - no key home /h/.sessionmail/seat-keys/claude-chat: mint "
              "the seat first (message seat init --registry DIR claude-chat). Nothing was written.\n")

# The fake `message`: every call is recorded (argv, env); each verb is answered
# from answers.json as [rc, stdout, stderr]. `send` judges must-sign as
# sessionmail does: with SESSIONMAIL_REQUIRE_SIGNATURE=1 and no usable key
# (CLAUDE_SESSION_PUB not a file, or CLAUDE_SESSION_AGENT_SOCK not a live
# socket) it refuses, kind identity, exit 2, and sends nothing.
FAKE_MESSAGE = r'''#!/usr/bin/env python3
import json, os, pathlib, socket, stat, sys
d = pathlib.Path(__file__).resolve().parent.parent / "fake-message"   # beside fake-bin/
a = sys.argv[1:]
n = len(list(d.glob("call-*.json")))
(d / f"call-{n:03d}.json").write_text(json.dumps({"argv": a, "env": dict(os.environ)}))
ans = json.loads((d / "answers.json").read_text())
def out(key):
    rc, so, se = ans[key]
    sys.stdout.write(so); sys.stderr.write(se); sys.exit(rc)
if a == PROBE:
    out("probe")
if a == ["--version"]:
    out("version")
if a[:3] == ["seat", "agent", "--help"]:
    out("agent_help")
if a[:3] == ["seat", "agent", "--install"]:
    # sessionmail refuses until the seat is minted, when the test says it is not yet
    out("install" if "unminted" not in ans or (d / "minted").exists() else "unminted")
if a[:2] == ["seat", "init"]:
    if ans["init"][0] == 0:
        (d / "minted").write_text(" ".join(a))
    out("init")
if a[:2] == ["setup", "--box"]:
    out("setup")
if a[:1] == ["path"]:
    out("path")
if a[:2] == ["subscription", "list"] and len(a) > 2:
    # the BOX form, who hears one box: sub_list_box, when the test gives it
    out("sub_list_box" if "sub_list_box" in ans else "no_box_form")
if a[:2] == ["subscription", "list"]:
    # after a `subscription add` this session hears the box: sub_list_after, when given
    out("sub_list_after" if (d / "subscribed").exists() and "sub_list_after" in ans else "sub_list")
if a[:2] == ["subscription", "add"]:
    if ans["sub_add"][0] == 0:
        (d / "subscribed").write_text(" ".join(a))
    out("sub_add")
if a[:1] == ["send"]:
    sys.stdin.read()
    pub, sock = os.environ.get("CLAUDE_SESSION_PUB", ""), os.environ.get("CLAUDE_SESSION_AGENT_SOCK", "")
    def live(p):
        try:
            if not stat.S_ISSOCK(os.stat(p).st_mode):
                return False
            s = socket.socket(socket.AF_UNIX); s.connect(p); s.close(); return True
        except OSError:
            return False
    if os.environ.get("SESSIONMAIL_REQUIRE_SIGNATURE") == "1" and not (pub and os.path.isfile(pub) and live(sock)):
        print(json.dumps({"sent": "no", "kind": "identity", "message": "must-sign: no usable key"})); sys.exit(2)
    print(json.dumps({"sent": "yes", "id": "<1.2.3@host.session-mail>"})); sys.exit(0)
print("unexpected: " + " ".join(a), file=sys.stderr); sys.exit(64)
'''


class Fake:
    """The fake sessionmail: .answer(verb, rc, out, err), .calls(), .argvs()."""

    def __init__(self, root: pathlib.Path, monkeypatch, pub: pathlib.Path, sock: str):
        self.dir = root / "fake-message"
        self.dir.mkdir()
        self.bin = root / "fake-bin"
        self.bin.mkdir()
        self.exe = self.bin / "message"
        self.exe.write_text(FAKE_MESSAGE.replace("PROBE", repr(seatmail.PROBE_ARGS), 1))
        self.exe.chmod(0o755)
        self.pub, self.sock = pub, sock
        self.answers = {
            "probe": PROBE_OK,
            "version": [0, "message, version 9.9.9\n", ""],
            "agent_help": [0, NEW_AGENT_HELP, ""],
            "install": [0, self.install_out(),
                        f"message seat agent: installed and loaded {LABEL}; the agent answers on {sock}\n"],
            "setup": [4, "box: claude DONE\nnotmuch hook BLOCKED\n", ""],
            "init": [0, "seat-init: claude-chat minted, and NOT blessed\n", ""],
            "path": [0, f"{root}/mail/agents/claude\n", ""],
            # a Terminal: no session, no seat (conftest stubs the ask unless a test restores it)
            "sub_list": [2, "", ("message subscription list: no session id and no seat in the "
                                 "environment (looked for CLAUDE_CODE_SESSION_ID, CLAUDE_SESSION_ID)\n")],
            "sub_add": [0, "row: claude -> 11111111-2222-3333-4444-555555555555\n", ""],
            # the BOX form, unless a test gives sub_list_box: a sessionmail that cannot read it
            "no_box_form": [64, "", "unexpected: subscription list BOX\n"],
        }
        self._write()

    def install_out(self, extra: str = "") -> str:
        """`message seat agent --install`'s five lines, shell-quoted as sessionmail quotes them."""
        return (f"SEAT_LABEL={LABEL}\n"
                f"SEAT_PLIST=/scratch/LaunchAgents/{LABEL}.plist\n"
                f"SEAT_SOCKET={shlex.quote(self.sock)}\n"
                f"SEAT_PUB={shlex.quote(str(self.pub))}\n"
                "SEAT_LOG=/scratch/Logs/seat-agent.claude-chat.log\n" + extra)

    def _write(self):
        (self.dir / "answers.json").write_text(json.dumps(self.answers))

    def answer(self, verb: str, rc: int, out: str = "", err: str = ""):
        self.answers[verb] = [rc, out, err]
        self._write()

    def calls(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.dir.glob("call-*.json"))]

    def argvs(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls()]

    def writes(self) -> list[list[str]]:
        """The calls that can write: anything but a help page, --version, the probe, path."""
        return [a for a in self.argvs() if a and a[-1] != "--help" and a != ["--version"]
                and a != seatmail.PROBE_ARGS and a[0] != "path"]


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd):
    """A stranger's Mac: no SESSIONMAIL_*, a scratch HOME and root, a fake
    sessionmail on PATH (DC_MESSAGE unset), the bot's seat made by hand and
    given as DC_SIGNER_*, someone who may ring. A no-signer test unsets them."""
    for k in [k for k in os.environ
              if k.startswith(("DC_", "PIP_CHAT_", "SESSIONMAIL_", "SESSION_MAIL_", "CLAUDE_SESSION_"))
              or k in ("SESSION_REGISTRY", "CLAUDE_CODE_AGENT")]:
        monkeypatch.delenv(k)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(service, "ears_src", lambda: None)
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger.local")
    keys = tmp_path / "seat-keys" / "claude-chat"
    keys.mkdir(parents=True)
    pub = keys / "seat.pub"
    pub.write_text("ssh-ed25519 AAAAfake claude-chat@stranger seat\n")
    fake = Fake(tmp_path, monkeypatch, pub, str(tmp_path / "agent.sock"))
    monkeypatch.setenv("PATH", f"{fake.bin}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("DC_SIGNER_SOCK", fake.sock)       # the seat, made by hand (README)
    monkeypatch.setenv("DC_SIGNER_PUB", str(pub))
    callers = tmp_path / "callers.txt"
    callers.write_text(f"{OWNER}\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    chans = tmp_path / "channels.txt"
    chans.write_text(f"owner  {OWNER}\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    fake.launchd, fake.tmp, fake.plist = launchd, tmp_path, tmp_path / "job.plist"
    return fake


def _install(*args):
    return CliRunner().invoke(cli, ["bot", "install", *args])


def _job_env(rig) -> dict:
    return plistlib.loads(rig.plist.read_bytes())["EnvironmentVariables"]


def _nothing_written(rig):
    """T17: no plist, so no job runs without a signer; launchd never asked to load."""
    assert not rig.plist.exists()
    assert "bootstrap" not in rig.launchd.verbs() and "bootout" not in rig.launchd.verbs()


# ---- T1: no signer: install runs the seat's agent with sessionmail ----------

def _no_signer(monkeypatch):
    monkeypatch.delenv("DC_SIGNER_SOCK")
    monkeypatch.delenv("DC_SIGNER_PUB")


# The suite's sessionmail sends by an MTA (conftest.suite_carrier), so every
# install here is must-sign, and the seat: line says why (seatmail.signing).
MTA_REASON = "signed: carrier /usr/sbin/sendmail is an MTA, from sessionmail's default"
MTA_SIGNED = "signed (carrier /usr/sbin/sendmail is an MTA, from sessionmail's default)"


def _installs(rig) -> list[list[str]]:
    return [a for a in rig.argvs() if a[:3] == ["seat", "agent", "--install"]]


def test_t1_one_command_runs_the_seats_agent_and_wires_the_signer(rig, monkeypatch):
    _no_signer(monkeypatch)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _installs(rig) == [["seat", "agent", "--install", "claude-chat"]]    # no SOCKET: the default
    assert [a for a in rig.argvs() if a[:2] == ["seat", "init"] and a[-1] != "--help"] == []  # never mints
    env = _job_env(rig)
    assert env["DC_SIGNER_SOCK"] == rig.sock and env["DC_SIGNER_PUB"] == str(rig.pub)
    lines = r.output.splitlines()
    i = lines.index(next(ln for ln in lines if ln.startswith("installed:")))
    assert lines[i + 1] == (f"seat:      claude-chat, {MTA_SIGNED}, key {rig.pub}, agent {LABEL} on "
                            f"{rig.sock} (run by `message seat agent --install`)")


def test_t1_the_capability_is_probed_before_the_agent_is_installed(rig, monkeypatch):
    _no_signer(monkeypatch)
    assert _install().exit_code == 0
    argvs = rig.argvs()
    assert argvs.index(["seat", "agent", "--help"]) < argvs.index(["seat", "agent", "--install", "claude-chat"])


def test_t1_the_agent_runs_in_the_installing_shells_env_with_the_pinned_message_first(rig, monkeypatch):
    _no_signer(monkeypatch)
    assert _install().exit_code == 0
    call = next(c for c in rig.calls() if c["argv"][:3] == ["seat", "agent", "--install"])
    assert call["env"]["HOME"] == os.environ["HOME"]
    assert call["env"]["PATH"].split(os.pathsep)[0] == str(rig.bin)      # `message` on PATH, the same one


def test_t1_the_agent_runs_before_install_writes_anything_of_its_own(rig, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    seen = []
    real = service.add_owner

    def add_owner(addr, path, key=None):
        seen.append(_installs(rig))
        return real(addr, path, key)
    monkeypatch.setattr(service, "add_owner", add_owner)
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    assert seen == [[["seat", "agent", "--install", "claude-chat"]]]


def test_t1_the_settings_block_names_where_the_signer_came_from(rig, monkeypatch):
    _no_signer(monkeypatch)
    r = _install()
    assert r.exit_code == 0, r.output
    for k in ("DC_SIGNER_SOCK", "DC_SIGNER_PUB"):
        row = next(ln for ln in r.output.splitlines() if ln.strip().startswith(k + " "))
        assert row.endswith("from `message seat agent --install` (not saved: asked again at each install)")


# ---- T2: a sessionmail that cannot install the agent: the manual steps stand ---

def test_t2_without_the_capability_install_refuses_naming_the_steps(rig, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    rig.answer("agent_help", 0, OLD_AGENT_HELP)
    rig.answer("version", 0, "message, version 0.1.0\n")
    (tmp_path / "callers.txt").unlink()
    (tmp_path / "channels.txt").unlink()
    r = _install("--owner", OWNER)
    assert r.exit_code == 2, r.output
    flat = " ".join(r.output.split())
    assert flat.startswith("sessionphone: no signer: DC_SIGNER_SOCK and DC_SIGNER_PUB are not set, "
                           f"and the bot never sends its agent unsigned mail here ({MTA_REASON}).")
    assert "message seat init --registry DIR claude-chat" in flat           # the steps by hand
    assert "message seat agent claude-chat SOCKET" in flat
    assert ("`bot install` cannot run the seat's agent itself with this sessionmail (message, version "
            "0.1.0): its `message seat agent` cannot install the seat's agent (no \"Capability: "
            "install\"). Follow the README's steps, or upgrade sessionmail. Nothing was written.") in flat
    assert flat.endswith("Nothing was written.")
    assert "installed:" not in r.output
    assert rig.writes() == []                      # no --install, no setup --box
    assert not (tmp_path / "callers.txt").exists() and not (tmp_path / "channels.txt").exists()
    _nothing_written(rig)
    assert rig.launchd.calls == []                 # not even a print: refused before the orphan check


def test_t2_a_sessionmail_with_no_seat_noun_is_no_capability(rig, monkeypatch):
    _no_signer(monkeypatch)
    rig.answer("agent_help", 2, "", "Error: No such command 'seat'.\n")
    r = _install()
    assert r.exit_code == 2 and '(no "Capability: install")' in r.output
    assert _installs(rig) == []
    _nothing_written(rig)


def test_t2_a_marker_on_a_failing_page_does_not_count(rig, monkeypatch):
    _no_signer(monkeypatch)
    rig.answer("agent_help", 2, NEW_AGENT_HELP)    # a page that exits 2 is no capability
    r = _install()
    assert r.exit_code == 2 and '(no "Capability: install")' in r.output
    assert _installs(rig) == []
    _nothing_written(rig)


@pytest.mark.parametrize("page", [
    "  Capability: install\n",                     # indented: click's epilog, not the line
    "Capability: install (soon)\n",
    "Capability: installed\n",
    "see Capability: install\n",
])
def test_t2_the_marker_must_be_a_whole_line_as_grep_qx_matches(rig, monkeypatch, page):
    _no_signer(monkeypatch)
    rig.answer("agent_help", 0, OLD_AGENT_HELP + page)
    r = _install()
    assert r.exit_code == 2 and '(no "Capability: install")' in r.output
    assert _installs(rig) == []


def test_t2_the_gate_is_one_table():
    assert [(argv, m) for argv, m, _ in seat.CAPABILITIES] == [(("seat", "agent", "--help"), "Capability: install")]


# ---- T3 / T4: DC_SIGNER_* given ------------------------------------------------

@pytest.mark.parametrize("have,lack", [("DC_SIGNER_SOCK", "DC_SIGNER_PUB"),
                                       ("DC_SIGNER_PUB", "DC_SIGNER_SOCK")])
def test_t3_exactly_one_signer_setting_refuses_naming_the_other(rig, monkeypatch, have, lack):
    monkeypatch.delenv(lack)
    r = _install()
    assert r.exit_code == 2, r.output
    flat = " ".join(r.output.split())
    assert f"no signer: {lack} is not set" in flat and "Nothing was written." in flat
    assert [a for a in rig.argvs() if a[:1] == ["seat"]] == []      # no page read, no agent run
    _nothing_written(rig)


def test_t4_a_given_signer_installs_and_names_the_seat(rig):
    r = _install()
    assert r.exit_code == 0, r.output
    assert [a for a in rig.argvs() if a[:1] == ["seat"]] == []      # no agent is run, no page read
    env = _job_env(rig)
    assert env["DC_SIGNER_SOCK"] == rig.sock and env["DC_SIGNER_PUB"] == str(rig.pub)
    lines = r.output.splitlines()
    i = lines.index(next(ln for ln in lines if ln.startswith("installed:")))
    assert lines[i + 1] == (f"seat:      claude-chat, {MTA_SIGNED}, key {rig.pub}, agent on {rig.sock} "
                            "(given: DC_SIGNER_*)")


def test_t4_a_saved_signer_is_given_and_no_agent_is_run(rig, monkeypatch):
    rig.sock_file = pathlib.Path(rig.sock)
    rig.sock_file.write_text("")                   # a saved path must still be there
    p = settings.file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"bots": {"claude": {"DC_SIGNER_SOCK": rig.sock,
                                                 "DC_SIGNER_PUB": str(rig.pub)}}}))
    _no_signer(monkeypatch)
    r = _install()
    assert r.exit_code == 0, r.output
    assert [a for a in rig.argvs() if a[:1] == ["seat"]] == []
    assert "(given: DC_SIGNER_*)" in r.output


def _saved_signer_whose_agent_is_gone(rig):
    """phone.json keeps the bot's signer, and its agent's socket is gone."""
    p = settings.file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"bots": {"claude": {"DC_SIGNER_SOCK": rig.sock,
                                                 "DC_SIGNER_PUB": str(rig.pub)}}}))
    assert not pathlib.Path(rig.sock).exists()


def _gone_words(rig) -> str:
    """The dead saved signer refusal, as it reads without the capability."""
    return (f"sessionphone: DC_SIGNER_SOCK in {settings.shown(settings.file_path())} names {rig.sock}, "
            "which is not there any more, so the bot could not sign: nothing was written. Give it "
            "again (`DC_SIGNER_SOCK=PATH sessionphone bot install`), or `--forget DC_SIGNER_SOCK`.")


AGENT_INSTEAD = ("This sessionmail can run the seat's agent itself (`Capability: install`): "
                 "`sessionphone bot install --forget DC_SIGNER_SOCK --forget DC_SIGNER_PUB` lets "
                 "install run `message seat agent --install` for it.")


def test_t4_a_dead_saved_signer_names_the_forget_that_runs_the_agent(rig, monkeypatch):
    _saved_signer_whose_agent_is_gone(rig)
    _no_signer(monkeypatch)
    r = _install()
    assert r.exit_code == 2, r.output
    flat = " ".join(r.output.split())
    assert flat == f"{_gone_words(rig)} {AGENT_INSTEAD}"
    assert rig.argvs() == [["seat", "agent", "--help"]]      # a help page read, nothing run
    _nothing_written(rig)
    # what it names is true: that command installs, the agent run by sessionmail
    r = _install("--forget", "DC_SIGNER_SOCK", "--forget", "DC_SIGNER_PUB")
    assert r.exit_code == 0, r.output
    assert _installs(rig) == [["seat", "agent", "--install", "claude-chat"]]
    assert _job_env(rig)["DC_SIGNER_SOCK"] == rig.sock


@pytest.mark.parametrize("page", [OLD_AGENT_HELP, None], ids=["old-help", "no-seat-noun"])
def test_t4_a_dead_saved_signer_without_the_capability_keeps_its_words(rig, monkeypatch, page):
    _saved_signer_whose_agent_is_gone(rig)
    _no_signer(monkeypatch)
    if page is None:
        rig.answer("agent_help", 2, "", "Error: No such command 'seat'.\n")
    else:
        rig.answer("agent_help", 0, page)
    r = _install()
    assert r.exit_code == 2, r.output
    assert " ".join(r.output.split()) == _gone_words(rig)
    assert "Capability" not in r.output and "--install" not in r.output
    _nothing_written(rig)


def test_t4_a_dead_saved_signer_beside_a_shell_signer_names_no_agent_run(rig, monkeypatch):
    # this shell gives DC_SIGNER_PUB: forgetting the saved pair cannot leave
    # the job with no signer, so the agent-run line would be false
    _saved_signer_whose_agent_is_gone(rig)
    monkeypatch.delenv("DC_SIGNER_SOCK")
    r = _install()
    assert r.exit_code == 2, r.output
    assert " ".join(r.output.split()) == _gone_words(rig)
    assert rig.argvs() == []                                  # not even asked


def test_t4_a_dead_saved_signer_counts_a_forget_already_given(rig, monkeypatch):
    # --forget DC_SIGNER_PUB on this run still needs naming in the command it prints
    _saved_signer_whose_agent_is_gone(rig)
    _no_signer(monkeypatch)
    r = _install("--forget", "DC_SIGNER_PUB")
    assert r.exit_code == 2, r.output
    assert " ".join(r.output.split()) == f"{_gone_words(rig)} {AGENT_INSTEAD}"


# ---- T5 / T6 / T7: sessionmail refuses or fails ----------------------------------

def test_t5_a_seat_not_minted_is_exit_2_naming_the_step(rig, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    (tmp_path / "callers.txt").unlink()
    (tmp_path / "channels.txt").unlink()
    rig.answer("install", 2, "", NOT_MINTED)
    r = _install("--owner", OWNER)
    assert r.exit_code == 2, r.output
    assert NOT_MINTED.strip() in r.output                     # sessionmail's line, verbatim
    flat = " ".join(r.output.split())
    assert flat.startswith("sessionphone: the bot's seat claude-chat is not minted yet, so `message "
                           "seat agent --install claude-chat` refused (exit 2):")
    assert ("Give it a key with `sessionphone bot install --registry DIR` (DIR: a seat registry; the "
            "README's \"The bot's seat\" says how to make one): it mints the seat there, then runs its "
            "agent.") in flat
    assert "installed:" not in r.output
    assert not (tmp_path / "callers.txt").exists() and not (tmp_path / "channels.txt").exists()
    assert [a for a in rig.writes() if a[:1] != ["seat"]] == []    # no setup --box
    _nothing_written(rig)


@pytest.mark.parametrize("refused", [
    "message seat agent: REFUSED - HOME 'home' is not an absolute path. Nothing was written.\n",
    ("message seat agent: REFUSED - a job already holds dev.sessionkit.mail.seat-agent.claude-chat "
     "(with no plist at /x.plist). Nothing was written.\n"),
])
def test_t6_any_other_refusal_is_exit_2_with_sessionmails_line_verbatim(rig, monkeypatch, refused):
    _no_signer(monkeypatch)
    rig.answer("install", 2, "", refused)
    r = _install()
    assert r.exit_code == 2, r.output
    assert refused.strip() in r.output
    assert "not minted" not in r.output and "seat init" not in r.output
    assert "`message seat agent --install claude-chat` refused (exit 2):" in r.output
    _nothing_written(rig)


def test_t7_a_failure_is_exit_1_naming_uninstall_then_install(rig, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    (tmp_path / "callers.txt").unlink()
    (tmp_path / "channels.txt").unlink()
    failed = ("message seat agent: FAILED - the agent never answered on /s/agent.sock within 10 s; "
              f"booted out {LABEL} and removed its plist\n"
              "  seat-agent: REFUSED - no key home\n  seat-agent: exit 2\n")
    rig.answer("install", 1, "", "message seat agent: narration first\n" + failed)
    r = _install("--owner", OWNER)
    assert r.exit_code == 1, r.output
    assert "narration first" not in r.output                   # from the FAILED line on
    for ln in failed.strip().splitlines():
        assert ln.strip() in r.output                          # the line, and the log tail it quotes
    flat = " ".join(r.output.split())
    assert ("`message seat agent --install claude-chat` failed (exit 1), so the bot has no signer and "
            "nothing of the bot was installed:") in flat
    assert ("Run `message seat agent --uninstall claude-chat`, then `message seat agent --install "
            "claude-chat` again (or run `sessionphone bot install` again).") in flat
    assert not (tmp_path / "callers.txt").exists() and not (tmp_path / "channels.txt").exists()
    _nothing_written(rig)


def test_t7_an_answer_with_no_socket_or_pub_is_a_failure(rig, monkeypatch):
    _no_signer(monkeypatch)
    rig.answer("install", 0, f"SEAT_LABEL={LABEL}\n")
    r = _install()
    assert r.exit_code == 1 and "printed no SEAT_SOCKET and SEAT_PUB" in r.output
    assert "--uninstall claude-chat" in r.output
    _nothing_written(rig)


def test_t7_a_timeout_is_a_failure_and_sets_no_signer(rig, monkeypatch):
    def slow(argv, env, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(seat, "_run", slow)
    env = {"PATH": os.environ["PATH"]}
    with pytest.raises(seat.SeatFailed) as e:
        seat.run_agent(env, "claude-chat", env)
    assert "did not finish in 120s" in str(e.value)
    assert "DC_SIGNER_SOCK" not in env and "DC_SIGNER_PUB" not in env


# ---- T8: the answer is parsed, never evaluated -------------------------------------

def test_t8_an_unknown_line_is_ignored_never_evaluated(rig, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    canary = tmp_path / "canary"
    rig.answer("install", 0, rig.install_out(f"DC_SIGNER_SOCK=/evil\n$(touch {canary})\n"
                                             f"SEAT_EXTRA=x; touch {canary}\n`touch {canary}`\n"))
    r = _install()
    assert r.exit_code == 0, r.output
    assert not canary.exists()
    env = _job_env(rig)
    assert env["DC_SIGNER_SOCK"] == rig.sock and "SEAT_EXTRA" not in env


def test_t8_quoted_values_are_read_as_the_shell_reads_them(rig, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    odd = tmp_path / "it's a $HOME" / "seat.pub"
    odd.parent.mkdir()
    odd.write_text(rig.pub.read_text())
    sock = str(tmp_path / "a b" / "agent.sock")
    rig.answer("install", 0, f"SEAT_LABEL={LABEL}\nSEAT_SOCKET={shlex.quote(sock)}\n"
                             f"SEAT_PUB={shlex.quote(str(odd))}\n")
    r = _install()
    assert r.exit_code == 0, r.output
    env = _job_env(rig)
    assert (env["DC_SIGNER_SOCK"], env["DC_SIGNER_PUB"]) == (sock, str(odd))


def test_t8_parse_install_reads_only_its_keys():
    got = seat.parse_install("SEAT_PUB='/a b/seat.pub'\nSEAT_SOCKET=/s\nPATH=/x\nnot a line\n"
                             "SEAT_LOG=\nSEAT_PLIST='/it'\"'\"'s.plist'\nSEAT_LABEL='unclosed\n")
    assert got == {"SEAT_PUB": "/a b/seat.pub", "SEAT_SOCKET": "/s", "SEAT_LOG": "",
                   "SEAT_PLIST": "/it's.plist"}


def test_t8_a_rerun_asks_again_and_saves_no_signer(rig, monkeypatch):
    _no_signer(monkeypatch)
    assert _install().exit_code == 0
    first = _job_env(rig)
    saved = json.loads(settings.file_path().read_text())["bots"]["claude"]
    assert "DC_SIGNER_SOCK" not in saved and "DC_SIGNER_PUB" not in saved
    rig.answer("install", 0, rig.install_out(),
               f"message seat agent: {LABEL} already installed; the agent answers on {rig.sock}\n")
    r = _install()
    assert r.exit_code == 0, r.output
    assert len(_installs(rig)) == 2
    again = _job_env(rig)
    assert (again["DC_SIGNER_SOCK"], again["DC_SIGNER_PUB"]) == (first["DC_SIGNER_SOCK"], first["DC_SIGNER_PUB"])


# ---- T9: DC_MESSAGE is pinned -----------------------------------------------------

def test_t9_dc_message_is_pinned_to_the_installing_paths_message(rig):
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(rig)["DC_MESSAGE"] == str(rig.exe)
    assert os.path.isabs(_job_env(rig)["DC_MESSAGE"])


def test_t9_a_dc_message_the_shell_set_is_kept(rig, monkeypatch, tmp_path):
    other = tmp_path / "other-bin" / "message"
    other.parent.mkdir()
    other.write_text(rig.exe.read_text())
    other.chmod(0o755)
    monkeypatch.setenv("DC_MESSAGE", str(other))
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(rig)["DC_MESSAGE"] == str(other)


def test_t9_no_message_anywhere_refuses_before_any_write(rig, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    r = _install()
    assert r.exit_code == 2, r.output
    assert "no `message` on PATH and DC_MESSAGE is not set" in r.output
    assert rig.calls() == []
    _nothing_written(rig)


# ---- T10: the bot's own start check runs before the plist ------------------------

def test_t10_a_key_minted_for_another_seat_is_refused_before_the_plist(rig):
    rig.pub.write_text("ssh-ed25519 AAAAfake other-chat seat\n")
    r = _install()
    assert r.exit_code == 2, r.output
    assert "the signing key is seat other-chat" in r.output
    _nothing_written(rig)


def test_t10_a_probe_answering_the_wrong_kind_is_refused_before_the_plist(rig):
    rig.answer("probe", 1, json.dumps({"sent": "no", "kind": "usage"}), "No such option: --message-id")
    r = _install()
    assert r.exit_code == 2, r.output
    assert "--message-id probe" in r.output
    _nothing_written(rig)


# ---- T11: `bot status` shows the last log line ------------------------------------

@pytest.fixture
def status_rig(rig, monkeypatch, tmp_path):
    rig.launchd.loaded = False
    monkeypatch.setenv("DC_AGENT", "ada")       # the bot status reads: named, so its log is known
    return tmp_path / "root"


def _status():
    return CliRunner().invoke(cli, ["bot", "status"]).output


def _write_plist(rig, env: dict, err: str | None = None):
    j = {"Label": "x", "EnvironmentVariables": env}
    if err:
        j["StandardErrorPath"] = err
    rig.plist.write_bytes(plistlib.dumps(j))


def _kit_log_at(text, name="bot.log"):
    p = kit_log("ada", name=name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_t11_status_shows_the_bots_last_line(rig, status_rig):
    log = _kit_log_at("started\nonline as bot@relay - listening\n\n")
    out = _status()
    assert f"log:     {log}" in out
    assert "last:    online as bot@relay - listening" in out


@pytest.mark.parametrize("where", ["kit", "an older plist's, in the root"])
def test_t11_with_no_bot_log_it_shows_launchds(rig, status_rig, where):
    err = kit_log("ada", name="bot.launchd.log") if where == "kit" else status_rig / "bot.launchd.log"
    _write_plist(rig, {"SESSIONPHONE_ROOT": str(status_rig)}, str(err))
    err.parent.mkdir(parents=True, exist_ok=True)
    err.write_text("sessionphone bot: no signer: DC_SIGNER_SOCK is not set\n")
    out = _status()
    assert f"log:     {err}" in out
    assert "last (bot.launchd.log): sessionphone bot: no signer: DC_SIGNER_SOCK is not set" in out


def test_t11_with_neither_it_says_no_log_yet(rig, status_rig):
    out = _status()
    assert "last:    no log yet" in out


def test_t11_dc_log_in_the_plist_names_the_bots_log(rig, status_rig, tmp_path):
    mine = tmp_path / "elsewhere.log"
    mine.write_text("a line\nthe last line\n")
    _kit_log_at("not this one\n")
    (status_rig / "bot.log").write_text("not this one either\n")
    _write_plist(rig, {"SESSIONPHONE_ROOT": str(status_rig), "DC_LOG": str(mine)})
    out = _status()
    assert f"log:     {mine}" in out and "last:    the last line" in out


# ---- T12: no DC_HOST on a Mac whose DNS name is reverse-DNS ------------------------

def test_t12_the_default_to_is_the_bare_agent_and_install_passes(rig, monkeypatch):
    """PHONE-248: the box install prints is bare, never claude@<host> (the line a
    user copied into DC_MAILTO, which a host rename then broke)."""
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "1.0.0.0.ip6.arpa")
    r = _install()
    assert r.exit_code == 0, r.output
    assert "box:       Delta text goes to claude, box " in r.output
    assert "claude@" not in r.output


# ---- T13: the box Delta text goes to -----------------------------------------------

def test_t13_install_makes_the_box_and_believes_only_message_path(rig):
    r = _install()                        # setup answers 4 (steps left), path answers 0
    assert r.exit_code == 0, r.output
    argvs = rig.argvs()
    assert argvs.index(["setup", "--box", "claude"]) < argvs.index(["path", "claude"])
    assert f"box:       Delta text goes to claude, box {rig.tmp}/mail/agents/claude" in r.output


def test_t13_no_box_after_setup_is_exit_2_naming_the_step(rig):
    rig.answer("setup", 1, "box: claude FAILED: not a directory\n")
    rig.answer("path", 2, "", "no such box: claude\n")
    r = _install()
    assert r.exit_code == 2, r.output
    flat = " ".join(r.output.split())
    assert ("no box `claude` for Delta text: `message setup --box claude` did not make one "
            "(box: claude FAILED: not a directory); run it yourself, then `sessionphone bot install` "
            "again.") in flat
    _nothing_written(rig)


def test_t13_a_dc_mailto_off_this_host_skips_the_box(rig, monkeypatch):
    monkeypatch.setenv("DC_MAILTO", "someone@elsewhere.example")
    r = _install()
    assert r.exit_code == 0, r.output
    assert not [a for a in rig.argvs() if a[:1] in (["setup"], ["path"])]
    assert "box:       Delta text goes to someone@elsewhere.example, which is not a box on this host" in r.output
    assert "hear:" not in r.output


def test_t13_dc_mailto_names_the_box(rig, monkeypatch):
    monkeypatch.setenv("DC_MAILTO", "Ada@stranger")
    r = _install()
    assert r.exit_code == 0, r.output
    assert ["setup", "--box", "ada"] in rig.argvs() and ["path", "ada"] in rig.argvs()


def test_t13_setup_makes_the_box_too(rig, monkeypatch):
    from sessionphone import account
    monkeypatch.setattr(account, "setup", lambda *a: {
        "root": "/r", "addr": "bot@relay.example", "displayname": "Claude", "created": True,
        "invite_file": "/r/invite", "invite": "https://i.delta.chat/#x"})
    r = CliRunner().invoke(cli, ["setup", "--name", "Claude"])
    assert r.exit_code == 0, r.output
    assert ["setup", "--box", "claude"] in rig.argvs()
    assert "box:     Delta text goes to claude, box " in r.output
    assert f"hear:    {HEAR_CLAUDE}{UNASKED}" in r.output
    rig.answer("path", 2, "", "no such box\n")
    r = CliRunner().invoke(cli, ["setup", "--name", "Claude"])
    assert r.exit_code == 2 and "no box `claude` for Delta text" in r.output


# ---- T14: the hear line ------------------------------------------------------------

# Seat mail reaches a session only through the doorbell, which rings only for
# a session started as the agent (`claude --agent NAME`): the words say so.
HEAR_CLAUDE = ("To hear Delta messages, start Claude with: session start claude "
               "(it runs `claude --agent claude`: only a session started as claude hears the box; "
               "a plain `claude` gets no copy of its mail, and nothing says so). To send Delta "
               "text to another box, install again with DC_AGENT=NAME (the agent a session is "
               "started as) or DC_MAILTO=BOX (a box a session hears).")
# conftest's sessionmail gives no answer, so the hear line says nothing was measured
UNASKED = (" Whether this session hears it was not measured: `message subscription list` "
           "gave no answer (not asked in the tests).")
TERMINAL = seat.Reach(answered=True)       # no session, no seat: a Terminal


def test_t14_the_hear_line_is_exact(rig, monkeypatch):
    monkeypatch.setattr(seat, "_list_agents", lambda shell: {"claude", "worker"})
    assert seat.hear_line("claude", {}, TERMINAL) == HEAR_CLAUDE
    assert seat.hear_line("claude", {"CLAUDE_CODE_AGENT": "worker"}, TERMINAL) == HEAR_CLAUDE
    # CLAUDE_CODE_AGENT is never taken as proof: only sessionmail's answer is
    unasked = seat.Reach(answered=False, said="not asked in the tests")
    assert seat.hear_line("claude", {"CLAUDE_CODE_AGENT": "Claude"}, unasked) == HEAR_CLAUDE + UNASKED


def test_t14_no_agent_file_appends_the_precondition(monkeypatch):
    monkeypatch.setattr(seat, "_list_agents", lambda shell: {"worker"})
    assert seat.hear_line("claude", {}, TERMINAL) == \
        f'{HEAR_CLAUDE} (no agent file "claude" yet: see the README)'
    monkeypatch.setattr(seat, "_list_agents", lambda shell: None)      # no `session` at all
    assert seat.hear_line("claude", {}).endswith('(no agent file "claude" yet: see the README)')


def test_t14_install_prints_the_hear_line_after_mail(rig, monkeypatch):
    monkeypatch.setattr(seat, "_list_agents", lambda shell: {"claude"})
    lines = _install().output.splitlines()
    hear = lines.index(f"hear:      {HEAR_CLAUDE}{UNASKED}")
    assert lines.index(next(ln for ln in lines if ln.startswith("mail:"))) < hear


def test_t14_list_agents_reads_session_start_read_only(tmp_path, monkeypatch):
    """The real lookup (conftest stubs it): `session start --list-agents`, one name a line."""
    monkeypatch.setattr(seat, "_list_agents", REAL_LIST_AGENTS)
    fake = tmp_path / "bin" / "session"
    fake.parent.mkdir()
    rec = tmp_path / "argv"
    fake.write_text(f'#!/bin/sh\necho "$*" > {rec}\nprintf "claude\\nworker\\n"\n')
    fake.chmod(0o755)
    assert seat._list_agents({"PATH": str(fake.parent)}) == {"claude", "worker"}
    assert rec.read_text().strip() == "start --list-agents"
    assert seat._list_agents({"PATH": str(tmp_path / "none")}) is None


# ---- T15: SESSIONMAIL_ORIGIN is never carried --------------------------------------

def test_t15_the_origin_is_not_in_the_job_and_the_mail_line_says_so(rig, monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_ORIGIN", "stranger.example")
    r = _install()
    assert r.exit_code == 0, r.output
    assert "SESSIONMAIL_ORIGIN" not in _job_env(rig)
    flat = " ".join(r.output.split())
    assert "SESSIONMAIL_ORIGIN is set in this shell and NOT carried" in flat
    assert "postfix.origin" in flat


def test_t15_a_legacy_job_carrying_the_origin_names_where_it_belongs(rig, monkeypatch):
    monkeypatch.setattr(service, "legacy_plist_path", lambda name=None: rig.tmp / "legacy.plist")
    (rig.tmp / "legacy.plist").write_bytes(plistlib.dumps(
        {"Label": service.legacy_label("claude"),
         "EnvironmentVariables": {"SESSIONMAIL_ORIGIN": "stranger.example"}}))
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("claude")
    msg = str(e.value)
    assert "SESSIONMAIL_ORIGIN: 'stranger.example' -> dropped" in msg
    assert "sessionmail's settings key postfix.origin" in msg
    assert "re-run with SESSIONMAIL_ORIGIN" not in msg


# ---- T16: must-sign is kept ----------------------------------------------------------

def _listening(path: pathlib.Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.listen(1)
    return s


@pytest.mark.parametrize("case", ["no-signer", "pub-not-a-file", "dead-socket"])
def test_t16_a_send_with_no_usable_key_is_refused_and_never_retried_unsigned(rig, monkeypatch, case):
    env = {"PATH": os.environ["PATH"], "DC_MESSAGE": str(rig.exe),
           "SESSIONMAIL_REQUIRE_SIGNATURE": "0", "SESSION_MAIL_REQUIRE_SIGNATURE": "0",
           "CLAUDE_SESSION_PUB": str(rig.pub), "CLAUDE_SESSION_AGENT_SOCK": "/inherited.sock"}
    if case == "pub-not-a-file":
        env.update(DC_SIGNER_PUB=str(rig.tmp / "missing.pub"), DC_SIGNER_SOCK=str(rig.tmp / "x.sock"))
    elif case == "dead-socket":
        env.update(DC_SIGNER_PUB=str(rig.pub), DC_SIGNER_SOCK=str(rig.tmp / "dead.sock"))
    sent = seatmail.send("claude@stranger", "hi", "body", environ=env)
    assert (sent.ok, sent.kind) == (False, "identity")
    sends = [c for c in rig.calls() if c["argv"][:1] == ["send"]]
    assert len(sends) == 1                                    # no second, unsigned attempt
    for c in sends:
        assert c["env"]["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1"
        assert "SESSION_MAIL_REQUIRE_SIGNATURE" not in c["env"]
        assert c["env"].get("CLAUDE_SESSION_AGENT_SOCK") != "/inherited.sock"    # never borrowed
        if case == "no-signer":
            assert "CLAUDE_SESSION_PUB" not in c["env"]


def test_t16_the_fake_signs_with_a_live_agent_so_the_refusal_is_the_key(rig):
    """The control: a live socket and a real pub file do send. So the refusals
    above are the key's, not a fake that always refuses."""
    short = pathlib.Path("/tmp") / f"sp-{os.getpid()}.sock"
    if short.exists():
        short.unlink()
    s = _listening(short)
    try:
        env = {"PATH": os.environ["PATH"], "DC_MESSAGE": str(rig.exe),
               "DC_SIGNER_PUB": str(rig.pub), "DC_SIGNER_SOCK": str(short)}
        assert stat.S_ISSOCK(os.stat(short).st_mode)
        assert seatmail.send("claude@stranger", "hi", "body", environ=env).ok
    finally:
        s.close()
        short.unlink()


def test_t16_a_failed_agent_install_sets_no_signer(rig):
    rig.answer("install", 1, "", "message seat agent: FAILED - launchctl bootstrap failed\n")
    env = {"PATH": os.environ["PATH"]}
    with pytest.raises(seat.SeatFailed):
        seat.run_agent(env, "claude-chat", env)
    assert "DC_SIGNER_SOCK" not in env and "DC_SIGNER_PUB" not in env


def test_t16_a_refused_agent_install_sets_no_signer(rig):
    rig.answer("install", 2, "", NOT_MINTED)
    env = {"PATH": os.environ["PATH"]}
    with pytest.raises(seat.NoSigner):
        seat.run_agent(env, "claude-chat", env)
    assert "DC_SIGNER_SOCK" not in env and "DC_SIGNER_PUB" not in env


# ---- the mail drop: signing follows the carrier -------------------------------
# sessionmail's own resolver says the drop (a fresh Mac: nothing set, no
# `sendmail` key), so with no signer given or saved the bot does not sign:
# install needs no key, no registry and no seat step (seatmail.signing).

DROP_UNSIGNED = "unsigned (carrier drop: this bot writes into the box itself, from sessionmail's default)"


@pytest.fixture
def on_drop(monkeypatch):
    monkeypatch.setattr(settings, "mail_carrier", lambda exe, sendmail_value=None: {
        "kind": "drop", "path": None, "source": "default", "why": ""})


def test_drop_with_no_signer_installs_unsigned_with_no_seat_step(rig, on_drop, monkeypatch):
    _no_signer(monkeypatch)
    r = _install()
    assert r.exit_code == 0, r.output
    assert [a for a in rig.argvs() if a[:1] == ["seat"]] == []      # no capability page, no agent, no mint
    assert seatmail.PROBE_ARGS in rig.argvs()                       # the --message-id start check still runs
    env = _job_env(rig)
    assert not [k for k in env if k.startswith(("DC_SIGNER_", "PIP_CHAT_SIGNER_")) or "REQUIRE_SIGNATURE" in k]
    lines = r.output.splitlines()
    i = lines.index(next(ln for ln in lines if ln.startswith("installed:")))
    assert lines[i + 1] == f"seat:      claude-chat, {DROP_UNSIGNED}, no key"
    assert any(ln.startswith("box:") for ln in lines)               # the box is still made


def test_drop_with_no_signer_needs_no_capable_sessionmail(rig, on_drop, monkeypatch):
    """An older sessionmail, with no `Capability: install`, is no refusal on the drop."""
    _no_signer(monkeypatch)
    rig.answer("agent_help", 0, OLD_AGENT_HELP)
    r = _install()
    assert r.exit_code == 0, r.output
    assert [a for a in rig.argvs() if a[:1] == ["seat"]] == []


def test_drop_with_a_given_signer_still_signs(rig, on_drop):
    r = _install()
    assert r.exit_code == 0, r.output
    env = _job_env(rig)
    assert env["DC_SIGNER_SOCK"] == rig.sock and env["DC_SIGNER_PUB"] == str(rig.pub)
    assert (f"seat:      claude-chat, signed (a signer is given in DC_SIGNER_SOCK and DC_SIGNER_PUB, on "
            f"carrier drop), key {rig.pub}, agent on {rig.sock} (given: DC_SIGNER_*)") in r.output


def test_drop_with_a_saved_signer_still_signs(rig, on_drop, monkeypatch):
    pathlib.Path(rig.sock).write_text("")          # a saved path must still be there
    p = settings.file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"bots": {"claude": {"DC_SIGNER_SOCK": rig.sock,
                                                 "DC_SIGNER_PUB": str(rig.pub)}}}))
    _no_signer(monkeypatch)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(rig)["DC_SIGNER_PUB"] == str(rig.pub)
    assert "signed (a signer is given in DC_SIGNER_SOCK and DC_SIGNER_PUB, on carrier drop)" in r.output


def test_drop_with_half_a_signer_refuses(rig, on_drop, monkeypatch):
    monkeypatch.delenv("DC_SIGNER_PUB")
    r = _install()
    assert r.exit_code == 2, r.output
    assert "half a signer: DC_SIGNER_SOCK is set and DC_SIGNER_PUB is not." in " ".join(r.output.split())
    _nothing_written(rig)


@pytest.mark.parametrize("ans", [
    None,
    {"kind": "refused", "path": None, "source": "unreadable", "why": "mail.json could not be read"},
    {"kind": "refused", "path": None, "source": "host", "why": '"sendmail": "smtp" is not a carrier'},
], ids=["not-known", "refused-unreadable", "nowhere"])
def test_no_carrier_install_refuses_and_writes_nothing(rig, monkeypatch, ans):
    monkeypatch.setattr(settings, "mail_carrier", lambda exe, sendmail_value=None: ans)
    _no_signer(monkeypatch)
    r = _install()
    assert r.exit_code == 2, r.output
    flat = " ".join(r.output.split())
    assert flat.startswith("sessionphone: no mail can reach the agent: ")
    assert "the carrier this bot's own `message send` will use" in flat and "/usr/sbin/sendmail" not in flat
    assert [a for a in rig.argvs() if a[:1] == ["seat"]] == []
    _nothing_written(rig)


# ---- `bot install --registry DIR`: give the bot a key, on any carrier -----------
# With no signer given or saved, install runs the seat's agent; only when
# sessionmail says the seat is not minted does it mint it into DIR first. The
# signer it gets is saved, so later installs and starts sign, the drop included.

def _registry(tmp_path) -> str:
    reg = tmp_path / "registry"
    reg.mkdir(exist_ok=True)
    return str(reg)


def _inits(rig) -> list[list[str]]:
    return [a for a in rig.argvs() if a[:2] == ["seat", "init"]]


def _saved(name: str = "claude") -> dict:
    return json.loads(settings.file_path().read_text())["bots"][name]


def test_registry_with_no_key_mints_runs_the_agent_and_saves_the_signer(rig, on_drop, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    rig.answer("unminted", 2, "", NOT_MINTED)
    reg = _registry(tmp_path)
    r = _install("--registry", reg)
    assert r.exit_code == 0, r.output
    assert _inits(rig) == [["seat", "init", "--registry", reg, "claude-chat"]]
    assert _installs(rig) == [["seat", "agent", "--install", "claude-chat"]] * 2   # refused, then run
    env = _job_env(rig)
    assert env["DC_SIGNER_SOCK"] == rig.sock and env["DC_SIGNER_PUB"] == str(rig.pub)
    assert _saved()["DC_SIGNER_SOCK"] == rig.sock and _saved()["DC_SIGNER_PUB"] == str(rig.pub)
    lines = r.output.splitlines()
    i = lines.index(next(ln for ln in lines if ln.startswith("installed:")))
    assert lines[i + 1] == (
        "seat:      claude-chat, signed (a signer is given in DC_SIGNER_SOCK and DC_SIGNER_PUB, on "
        f"carrier drop), key {rig.pub}, agent {LABEL} on {rig.sock} (minted into {reg} by `message "
        "seat init`, run by `message seat agent --install`)")
    assert "from `message seat agent --install`, for --registry (saved)" in r.output
    assert "REQUIRE_SIGNATURE" not in " ".join(env)       # must-sign is the bot's own, at each send
    assert seatmail.child_env(env)["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1"


def test_registry_on_an_mta_mints_too(rig, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    rig.answer("unminted", 2, "", NOT_MINTED)
    reg = _registry(tmp_path)
    r = _install("--registry", reg)
    assert r.exit_code == 0, r.output
    assert len(_inits(rig)) == 1 and _saved()["DC_SIGNER_PUB"] == str(rig.pub)


def test_registry_with_a_key_already_there_never_mints(rig, on_drop, monkeypatch, tmp_path):
    """The seat's key is there: its agent answers at once, and nothing is minted."""
    _no_signer(monkeypatch)
    r = _install("--registry", _registry(tmp_path))
    assert r.exit_code == 0, r.output
    assert _inits(rig) == []
    assert _installs(rig) == [["seat", "agent", "--install", "claude-chat"]]
    assert "minted into" not in r.output and _saved()["DC_SIGNER_PUB"] == str(rig.pub)


def test_registry_rerun_is_idempotent_and_never_mints_again(rig, on_drop, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    rig.answer("unminted", 2, "", NOT_MINTED)
    reg = _registry(tmp_path)
    assert _install("--registry", reg).exit_code == 0
    pathlib.Path(rig.sock).write_text("")         # the agent's socket, there as launchd keeps it
    again = _install("--registry", reg)
    assert again.exit_code == 0, again.output
    assert len(_inits(rig)) == 1                  # minted once, ever
    assert "(given: DC_SIGNER_*)" in again.output  # the saved signer is used as given
    assert _job_env(rig)["DC_SIGNER_PUB"] == str(rig.pub)


def test_after_a_registry_install_later_installs_and_starts_sign_on_the_drop(rig, on_drop, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    rig.answer("unminted", 2, "", NOT_MINTED)
    assert _install("--registry", _registry(tmp_path)).exit_code == 0
    pathlib.Path(rig.sock).write_text("")
    later = _install()                            # no --registry, no DC_SIGNER_* in this shell
    assert later.exit_code == 0, later.output
    assert len(_inits(rig)) == 1
    env = _job_env(rig)
    assert env["DC_SIGNER_SOCK"] == rig.sock and env["DC_SIGNER_PUB"] == str(rig.pub)
    assert "signed (a signer is given in DC_SIGNER_SOCK and DC_SIGNER_PUB, on carrier drop)" in later.output
    # the bot that job starts: signed, must-sign on, the saved key handed to every send
    assert seatmail.signing(env)[0] == "signed"
    child = seatmail.child_env(env)
    assert child["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1" and child["CLAUDE_SESSION_PUB"] == str(rig.pub)


def test_registry_with_a_signer_given_uses_it_and_mints_nothing(rig, on_drop, tmp_path):
    r = _install("--registry", _registry(tmp_path))
    assert r.exit_code == 0, r.output
    assert [a for a in rig.argvs() if a[:1] == ["seat"]] == []
    assert "(given: DC_SIGNER_*)" in r.output


def test_a_refused_mint_installs_nothing(rig, on_drop, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    rig.answer("unminted", 2, "", NOT_MINTED)
    rig.answer("init", 2, "", "seat-init: REFUSED - no registry at /nowhere. Nothing was written.\n")
    reg = _registry(tmp_path)
    r = _install("--registry", reg)
    assert r.exit_code == 2, r.output
    flat = " ".join(r.output.split())
    assert f"`message seat init --registry {reg} claude-chat` refused (exit 2):" in flat
    assert "seat-init: REFUSED - no registry at /nowhere." in flat
    _nothing_written(rig)
    assert not settings.file_path().exists()


def test_a_failed_mint_is_exit_1(rig, on_drop, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    rig.answer("unminted", 2, "", NOT_MINTED)
    rig.answer("init", 1, "", "seat-init: FAILED - git commit failed. The key and the registry are back.\n")
    r = _install("--registry", _registry(tmp_path))
    assert r.exit_code == 1, r.output
    assert "seat-init: FAILED - git commit failed." in r.output
    _nothing_written(rig)


def test_registry_without_the_capability_refuses_on_the_drop(rig, on_drop, monkeypatch, tmp_path):
    _no_signer(monkeypatch)
    rig.answer("agent_help", 0, OLD_AGENT_HELP)
    r = _install("--registry", _registry(tmp_path))
    assert r.exit_code == 2, r.output
    flat = " ".join(r.output.split())
    assert flat.startswith("sessionphone: --registry asks for the bot's key, but `bot install` cannot run "
                           "the seat's agent itself with this sessionmail")
    assert _inits(rig) == [] and _installs(rig) == []
    _nothing_written(rig)


def test_install_help_names_registry_neutrally():
    out = " ".join(CliRunner().invoke(cli, ["bot", "install", "--help"]).output.split())
    assert "--registry DIR" in out
    assert "give the bot a key, on any mail carrier" in out
    assert not re.search(r"[A-Z]+-\d+|/Users/|@ms\d|mbailey", out)

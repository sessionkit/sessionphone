"""The CLI guard for the media pack (PHONE-10).

The light verbs (setup, qr, invite, ...) run with the six heavy modules
BLOCKED, and import none of them. The media verbs (bot install, bot run, web,
web enrol) refuse with exit 2 and a plain message, no traceback, and write
nothing. These tests run the CLI in a fresh process: the suite's own process
has the pack loaded (the dev group carries it), so a block there would prove
nothing.

The drift guard at the bottom walks the source (no import runs): a verb whose
code reaches a heavy module must call `media.require(<its own name>)` first,
and no light verb may.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

from sessionphone import media

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src"
CLI = SRC / "sessionphone" / "cli.py"
HEAVY = set(media.MODULES)
MEDIA_VERBS = {"bot install", "bot run", "web", "web enrol"}
LINK = "https://i.delta.chat/#FP&v=3&i=x&s=y&a=bot%40relay.example&n=Claude"

# The probe: block the pack (any import of it raises), fake Delta Chat and
# launchd, run the CLI, then record the exit code, the heavy modules loaded and
# every blocked attempt. It prints nothing of its own: the CLI's stdout and
# stderr are the probe's.
PROBE = r'''
import importlib.abc, json, os, subprocess, sys, types
HEAVY = set(json.loads(os.environ["PROBE_BLOCK"]))
attempts = []

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in HEAVY:
            attempts.append(name)
            raise ModuleNotFoundError(f"blocked: {name}", name=name)
        return None

sys.meta_path.insert(0, Block())

LINK = os.environ["PROBE_LINK"]
dc = types.ModuleType("deltachat_rpc_client")

class Account:
    cfg = {}
    def is_configured(self): return bool(self.cfg)
    def set_config(self, k, v): self.cfg[k] = v
    def get_config(self, k): return self.cfg.get(k)
    def init_transports(self, *a): self.cfg["configured"] = "1"
    def start_io(self): pass
    def get_qr_code(self): return LINK
    def list_transports(self): return [{"addr": "bot@relay.example"}]

class Rpc:
    def __init__(self, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

class DeltaChat:
    def __init__(self, rpc): pass
    def get_all_accounts(self): return []
    def add_account(self): return Account()

dc.Account, dc.Rpc, dc.DeltaChat = Account, Rpc, DeltaChat
sys.modules["deltachat_rpc_client"] = dc

from sessionphone import cli, service

loaded = {"on": False}
def launchctl(*args):
    verb = args[0]
    if verb == "bootout":
        loaded["on"] = False
    elif verb == "bootstrap":
        loaded["on"] = True
    elif verb == "print" and loaded["on"]:
        return subprocess.CompletedProcess(args, 0, "state = running\npid = 4242\n", "")
    elif verb == "print":
        return subprocess.CompletedProcess(args, 113, "", "Could not find service")
    return subprocess.CompletedProcess(args, 0, "", "")

service._launchctl = launchctl
service.named_python = lambda *a, **k: sys.executable   # never a hard link in the venv
# the suite's sessionmail sends by an MTA (conftest.suite_carrier): the tripwire
# `message` is a shell script, so its own resolver cannot be asked
from sessionphone import settings
settings.mail_carrier = lambda exe, sendmail_value=None: {
    "kind": "mta", "path": "/usr/sbin/sendmail", "source": "default", "why": ""}

try:
    cli.main(sys.argv[1:], prog_name="sessionphone")
    rc = 0
except SystemExit as exc:
    rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
heavy = sorted(m for m in sys.modules if m.split(".")[0] in HEAVY)
with open(os.environ["PROBE_OUT"], "w") as fh:
    json.dump({"rc": rc, "heavy": heavy, "attempts": attempts}, fh)
'''


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, rec: dict, root: pathlib.Path, home: pathlib.Path):
        self.stdout, self.stderr = proc.stdout, proc.stderr
        self.rc, self.heavy, self.attempts = rec["rc"], rec["heavy"], rec["attempts"]
        self.root, self.home = root, home

    def plists(self) -> list[pathlib.Path]:
        return sorted((self.home / "Library" / "LaunchAgents").glob("*.plist*"))


def _run(tmp_path: pathlib.Path, *argv: str, block=HEAVY, root_ready: bool = True) -> Run:
    """`sessionphone *argv` in a fresh process with `block` refused, under a
    scratch root and HOME. root_ready: a private root with a saved invite, as
    setup leaves it."""
    root, home, out = tmp_path / "root", tmp_path / "home", tmp_path / "probe.json"
    home.mkdir(exist_ok=True)
    if root_ready and not root.exists():
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        (root / "invite.txt").write_text(LINK + "\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_AGENT", "SESSIONPHONE_ROOT", "PYTHONPATH")
           and not k.startswith(("DC_", "PIP_CHAT_"))}
    # the suite's mail tripwire stays: install and setup run sessionmail (the bot's
    # seat and its box), and this fresh process has no Popen guard of its own
    probe_pub = tmp_path / "probe-chat.pub"
    probe_pub.write_text("ssh-ed25519 AAAAtripwire probe-chat seat\n")
    env.update(DC_MESSAGE=os.environ["DC_MESSAGE"], DC_SIGNER_SOCK=os.environ["DC_SIGNER_SOCK"],
               DC_SIGNER_PUB=str(probe_pub))
    env.update(HOME=str(home), SESSIONPHONE_ROOT=str(root), DC_HOST="host.example", DC_AGENT="probe",
               PROBE_BLOCK=json.dumps(sorted(block)), PROBE_OUT=str(out), PROBE_LINK=LINK,
               PYTHONDONTWRITEBYTECODE="1",
               # conftest's stt_tripwire: bot install ends with a hearing probe (PHONE-18),
               # and this fresh process must reach a closed port, never a live STT
               DC_STT_URL=os.environ["DC_STT_URL"])
    proc = subprocess.run([sys.executable, "-c", PROBE, *argv], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120, check=False,
                          stdin=subprocess.DEVNULL)
    assert out.exists(), f"the probe died before it recorded:\n{proc.stderr[-3000:]}"
    return Run(proc, json.loads(out.read_text()), root, home)


# ---- the light verbs run with the pack blocked ---------------------------------


def test_setup_runs_with_the_pack_blocked_and_imports_none_of_it(tmp_path):
    r = _run(tmp_path, "setup", "--name", "Claude", root_ready=False)
    assert r.rc == 0, r.stderr
    assert (r.root / "invite.txt").read_text().strip() == LINK
    assert f"INVITE: {LINK}" in r.stdout
    assert r.heavy == [] and r.attempts == []


@pytest.mark.parametrize("argv", [["qr"], ["invite"], ["callers"], ["channels"], ["channels", "list"]],
                         ids=" ".join)
def test_light_verbs_run_with_the_pack_blocked(tmp_path, argv):
    r = _run(tmp_path, *argv)
    assert r.rc == 0, r.stderr
    assert r.heavy == [] and r.attempts == []
    assert "Traceback" not in r.stderr
    if argv == ["invite"]:
        assert r.stdout.strip() == LINK
    if argv == ["qr"]:
        assert "<svg" in r.stdout


def test_bot_status_and_uninstall_stay_light(tmp_path):
    for argv in (["bot", "status"], ["bot"], ["bot", "uninstall"]):
        r = _run(tmp_path, *argv)
        assert r.rc in (0, 1), (argv, r.stderr)        # status: exit 1 means "not running"
        assert r.heavy == [] and r.attempts == [], argv
        assert "Traceback" not in r.stderr and "media pack" not in r.stderr, argv


@pytest.mark.parametrize("argv", [["bot", "install", "--help"], ["bot", "run", "-h"], ["web", "--help"],
                                  ["web", "enrol", "--help"], ["--help"]], ids=" ".join)
def test_help_never_refuses_with_the_pack_blocked(tmp_path, argv):
    r = _run(tmp_path, *argv)
    assert r.rc == 0, r.stderr
    assert "Usage:" in r.stdout and "media pack" not in r.stderr
    assert r.heavy == [] and r.attempts == []


# ---- the media verbs refuse plainly ----------------------------------------------


def _assert_plain_refusal(r: Run, verb: str) -> None:
    assert r.rc == 2, (r.rc, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr and "Traceback" not in r.stdout
    err = r.stderr
    assert err.startswith(f"sessionphone: `{verb}` needs the media pack"), err
    assert "missing: aiortc, aioice, av, numpy, websockets, cryptography" in err     # what
    assert "WebRTC audio" in err and "setup, qr and link do not need it" in err       # why
    assert "about 31 MB" in err                                                       # how big
    # how: this suite runs in the worktree's own .venv, beside bin/sessionphone: the plugin's hint
    assert f"{REPO / 'bin' / 'sessionphone'} {verb}" in err, err
    assert "Nothing was changed." in err
    assert r.heavy == []
    # the only reach for the pack was the guard's own find_spec, once per module: the
    # blocking finder sees that probe too, and nothing went on to import any of it
    assert r.attempts == list(media.MODULES)


def test_bot_install_refuses_first_and_writes_nothing(tmp_path):
    r = _run(tmp_path, "bot", "install", "--owner", "owner@example.org")
    _assert_plain_refusal(r, "bot install")
    assert r.plists() == []
    assert not (r.root / "callers.txt").exists()
    assert not (r.root / "channels.txt").exists()


def test_bot_install_with_the_pack_present_does_write_the_job(tmp_path):
    """The positive control for the test above: the same command, nothing
    blocked, writes the plist, the owner and the channels seed. So the refusal
    is what kept them unwritten, not a fault in the rig."""
    r = _run(tmp_path, "bot", "install", "--owner", "owner@example.org", block=())
    assert r.rc == 0, r.stderr
    assert [p.name.rpartition(".probe.")[2] for p in r.plists()] == ["plist"]
    assert (r.root / "callers.txt").exists() and (r.root / "channels.txt").exists()


@pytest.mark.parametrize("argv", [["bot", "run"], ["web"], ["web", "--port", "9999"], ["web", "enrol"]],
                         ids=" ".join)
def test_media_verbs_refuse_plainly_with_the_pack_blocked(tmp_path, argv):
    verb = " ".join(a for a in argv if not a.startswith("-") and not a.isdigit())
    r = _run(tmp_path, *argv)
    _assert_plain_refusal(r, verb)
    assert not (r.root / "web").exists()        # web enrol wrote no code


def test_one_missing_module_is_named_alone(tmp_path):
    r = _run(tmp_path, "bot", "run", block={"av"})
    assert r.rc == 2
    assert "(missing: av)." in r.stderr and "Traceback" not in r.stderr


# ---- media.missing() -----------------------------------------------------------


def test_missing_finds_the_whole_pack_without_importing_it(tmp_path):
    """In a fresh process with the pack installed (the dev group): nothing is
    missing, and find_spec loaded none of the six."""
    code = ("import json, sys; from sessionphone import media; "
            "print(json.dumps([media.missing(), sorted(m for m in sys.modules "
            "if m.split('.')[0] in media.MODULES)]))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         check=True, cwd=tmp_path).stdout
    assert json.loads(out) == [[], []]


def test_missing_counts_a_none_spec(monkeypatch):
    monkeypatch.setattr(media, "MODULES", ("json", "sessionphone_no_such_module"))
    assert media.missing() == ["sessionphone_no_such_module"]


def test_missing_counts_a_raised_import_error(monkeypatch):
    """A blocking meta-path finder makes find_spec RAISE (design review 2, N2)."""
    class Block:
        def find_spec(self, name, path=None, target=None):
            if name == "av":
                raise ModuleNotFoundError("blocked: av", name="av")

    monkeypatch.delitem(sys.modules, "av", raising=False)     # else find_spec answers from sys.modules
    monkeypatch.setattr(sys, "meta_path", [Block(), *sys.meta_path])
    monkeypatch.setattr(media, "MODULES", ("json", "av"))
    assert media.missing() == ["av"]


def test_missing_takes_a_loaded_module_without_a_spec_as_present(monkeypatch):
    mod = types.ModuleType("sessionphone_specless")
    mod.__spec__ = None             # find_spec raises ValueError for this
    monkeypatch.setitem(sys.modules, "sessionphone_specless", mod)
    monkeypatch.setattr(media, "MODULES", ("sessionphone_specless",))
    assert media.missing() == []


def test_missing_imports_nothing(monkeypatch):
    assert "this" not in sys.modules          # `import this` prints; nothing here ever loads it
    monkeypatch.setattr(media, "MODULES", ("this",))
    assert media.missing() == []
    assert "this" not in sys.modules


def test_require_raises_missing_with_the_names(monkeypatch):
    monkeypatch.setattr(media, "MODULES", ("json", "sessionphone_no_such_module"))
    with pytest.raises(media.Missing) as info:
        media.require("bot run")
    assert info.value.verb == "bot run" and info.value.names == ["sessionphone_no_such_module"]
    monkeypatch.setattr(media, "MODULES", ("json",))
    media.require("bot run")        # all present: no raise


# ---- the fetch hint fits the environment (design review 2, R2) ------------------


def _venv(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return path


def test_hint_in_the_plugins_own_venv_names_the_launcher(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "sessionphone").write_text("#!/bin/sh\n")
    (tmp_path / "pyproject.toml").write_text("")
    env = _venv(tmp_path / ".venv")
    how = media.how_to_fetch("bot install", str(env))
    assert f"`{tmp_path / 'bin' / 'sessionphone'} bot install`" in how
    assert "fetches the pack once" in how


def test_hint_in_sessionphones_own_uv_tool_env(tmp_path):
    env = _venv(tmp_path / "tools" / "sessionphone")
    (env / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "sessionphone" }]\n')
    assert "`uv tool install 'sessionphone[media]'`" in media.how_to_fetch("web", str(env))


def test_hint_in_an_editable_uv_tool_env_keeps_the_checkout(tmp_path):
    env = _venv(tmp_path / "tools" / "sessionphone")
    (env / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "sessionphone", editable = "/src/sessionphone" }]\n')
    assert "`uv tool install -e '/src/sessionphone[media]'`" in media.how_to_fetch("web", str(env))


def test_hint_in_a_kits_tool_env_never_sends_to_a_second_tool_env(tmp_path):
    env = _venv(tmp_path / "tools" / "sessionkit")
    (env / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "sessionkit" }]\n')
    how = media.how_to_fetch("bot install", str(env))
    assert "`uv tool install sessionkit --with 'sessionphone[media]'`" in how
    assert "uv tool install 'sessionphone[media]'" not in how


@pytest.mark.parametrize("receipt", [None, "not [ toml", '[tool]\nrequirements = []\n'])
def test_hint_anywhere_else_says_add_the_extra_to_this_env(tmp_path, receipt):
    env = _venv(tmp_path / "some-env")
    if receipt is not None:
        (env / "uv-receipt.toml").write_text(receipt)
    how = media.how_to_fetch("bot run", str(env))
    assert f"add `sessionphone[media]` to the environment this command runs from ({env})" in how


# ---- the drift guard: heavy imports only behind media.require --------------------


def _module_file(mod: str) -> pathlib.Path | None:
    p = SRC.joinpath(*mod.split("."))
    if (p / "__init__.py").exists():
        return p / "__init__.py"
    return p.with_suffix(".py") if p.with_suffix(".py").exists() else None


def _names(node: ast.Import | ast.ImportFrom, pkg: str) -> list[str]:
    """Absolute module names one import statement may load."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    base = node.module or ""
    if node.level:
        parts = pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)]
        base = ".".join(parts + ([base] if base else []))
    return [base] + [f"{base}.{a.name}" for a in node.names if _module_file(f"{base}.{a.name}")]


def _module_imports(mod: str) -> set[str]:
    """What importing `mod` runs: every import outside a function body
    (module level, under if/try, in class bodies). Function bodies run later."""
    f = _module_file(mod)
    pkg = mod if f.name == "__init__.py" else mod.rpartition(".")[0]
    out: set[str] = set()

    def walk(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                out.update(_names(node, pkg))
            walk(ast.iter_child_nodes(node))

    walk(ast.parse(f.read_text(encoding="utf-8")).body)
    return out


def _closure(mods) -> set[str]:
    """Every module name importing `mods` reaches, through sessionphone's sources."""
    seen: set[str] = set()
    stack = list(mods)
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        # importing a.b.c runs a and a.b first
        stack.extend(".".join(m.split(".")[:i]) for i in range(1, m.count(".") + 1))
        if m.split(".")[0] == "sessionphone" and _module_file(m):
            stack.extend(_module_imports(m))
    return seen


def _heavy(mods) -> set[str]:
    return {m.split(".")[0] for m in _closure(mods)} & HEAVY


def _commands() -> list[tuple[str, ast.FunctionDef]]:
    """(verb, function) for every click command and group in cli.py, in source
    order, the verb as typed: `bot install`, `web enrol`; main is ''."""
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    verbs: dict[str, str] = {}
    out = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for d in fn.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in ("command", "group")):
                if isinstance(d.func.value, ast.Name) and d.func.value.id == "click":
                    verb = ""                               # the top group, main
                else:
                    parent = verbs[d.func.value.id]
                    first = d.args[0].value if d.args and isinstance(d.args[0], ast.Constant) else None
                    name = first or fn.name.replace("_", "-")   # click's own default name
                    verb = f"{parent} {name}".strip()
                verbs[fn.name] = verb
                out.append((verb, fn))
    return out


def _require_calls(fn: ast.FunctionDef) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "require" and isinstance(n.func.value, ast.Name) and n.func.value.id == "media"]


def require_sites() -> dict[str, str]:
    """{verb: the literal it passes} for every cli.py command that calls
    media.require. For the launcher's own table test (do-003)."""
    out = {}
    for verb, fn in _commands():
        for call in _require_calls(fn):
            arg = call.args[0]
            out[verb] = arg.value if isinstance(arg, ast.Constant) else ast.unparse(arg)
    return out


def _reaches(fn: ast.FunctionDef) -> set[str]:
    """The heavy modules a command's own code reaches: its deferred imports'
    closures, and the launchd job's module when it installs one."""
    pkg = "sessionphone"
    mods: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods.update(_names(n, pkg))
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "install"
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "service"):
            mods.add("sessionphone.bridge.bot")     # the job it writes runs this module
    return _heavy(mods)


def test_the_cli_and_the_guard_load_light():
    assert _heavy(["sessionphone.cli"]) == set()
    tops = {m.split(".")[0] for m in _module_imports("sessionphone.media")}
    assert tops <= set(sys.stdlib_module_names) | {"__future__"}, tops


def test_the_job_bot_install_writes_runs_the_bot_module():
    """The drift guard counts `service.install` as reaching bridge.bot: pin that."""
    src = (SRC / "sessionphone" / "service.py").read_text(encoding="utf-8")
    assert '"-m", "sessionphone.bridge.bot"' in src
    assert _heavy(["sessionphone.bridge.bot"]) >= {"aiortc", "av", "numpy"}


def test_every_verb_that_reaches_the_pack_requires_it_and_no_other_does():
    reach = {verb: _reaches(fn) for verb, fn in _commands()}
    heavy_verbs = {verb for verb, mods in reach.items() if mods}
    sites = require_sites()
    assert heavy_verbs - set(sites) == set(), \
        f"these verbs reach the media pack without media.require: { {v: reach[v] for v in heavy_verbs - set(sites)} }"
    assert set(sites) == MEDIA_VERBS          # the launcher's table must match (do-003)
    assert set(sites) - heavy_verbs == set(), "a verb requires the pack but never reaches it"
    for verb, arg in sites.items():
        assert arg == verb, f"{verb} calls media.require({arg!r}): the message would name the wrong verb"


def test_require_runs_first_in_each_media_verb():
    """Before anything else runs: only the docstring, and web's own
    `if ctx.invoked_subcommand is not None: return`, may come first."""
    for verb, fn in _commands():
        if verb not in MEDIA_VERBS:
            continue
        body = list(fn.body)
        if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body.pop(0)
        if isinstance(body[0], ast.If) and "invoked_subcommand" in ast.unparse(body[0].test):
            body.pop(0)
        first = body[0]
        assert isinstance(first, ast.Try) and _require_calls(first), f"{verb}: {ast.unparse(first)[:120]}"
        handler = first.handlers[0]
        assert ast.unparse(handler.type) == "media.Missing"
        assert "_fail(exc, 2)" in ast.unparse(handler)

"""The default root is ~/.sessionkit/phone/state/, and a Mac whose bot still
lives in the old ~/.sessionphone keeps reading it there (PHONE-270).

One rule, paths.default_root: with no root configured (no DC_ROOT, no
SESSIONPHONE_ROOT, none kept in phone.json), the new root, unless it is not
there and the old one is: then the old one, said ONCE on stderr. An explicit
root is never re-pointed. Every reader asks the rule; one test here pins each
(paths.root, settings, service.job, dc_env, bin/dc-env, bin/dc-log,
bin/dc-log-web, the web verb and its --legs guard), and a scan proves no
second copy of it is kept.

Every home here is a tmp dir (PHONE-264): the real ~/.sessionphone is never
read or written.
"""
import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

from sessionphone import paths, service, settings
from sessionphone.web import call as webcall

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
SRC = REPO / "src"
NEW = pathlib.Path(".sessionkit", "phone", "state")
OLD = pathlib.Path(".sessionphone")
SCRUB_PREFIXES = ("DC_", "PIP_CHAT_")
SCRUB = ("SESSIONPHONE_ROOT", "CLAUDE_CODE_AGENT", "EMAIL")
DEFAULT_FILE = settings.FILE


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A tmp HOME with no root configured anywhere: no DC_ROOT, no
    SESSIONPHONE_ROOT (conftest sets one; it goes here), no phone.json. The
    once-per-process notice is forgotten, so each test hears its own."""
    for k in [k for k in os.environ if k.startswith(SCRUB_PREFIXES) or k in SCRUB]:
        monkeypatch.delenv(k)
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(settings, "FILE", DEFAULT_FILE)
    monkeypatch.setattr(paths, "_said_old", "")
    return h


def make(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, mode=0o700)
    return path


def child_env(h, **extra):
    """A child's environment: this one, scrubbed of every root and seat
    variable, HOME at H, the bot named (so no seat is asked)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(SCRUB_PREFIXES) and k not in SCRUB}
    env.update(HOME=str(h), DC_AGENT="ada", **extra)
    return env


def notices(err: str) -> list[str]:
    return [line for line in err.splitlines() if "using the old root" in line]


# ---- the rule ---------------------------------------------------------------

def test_the_default_is_under_the_kit(home_is_tmp):
    assert paths.DEFAULT_ROOT == "~/.sessionkit/phone/state"
    assert paths.DEFAULT_ROOT == paths.kit_dir("phone", "state")
    assert paths.OLD_ROOT == "~/.sessionphone"
    assert pathlib.Path("~", NEW).as_posix() == paths.DEFAULT_ROOT
    # conftest spells it before any sessionphone import: the same path
    assert os.environ["SESSIONPHONE_ROOT"] == str(home_is_tmp / NEW)


def test_a_fresh_install_resolves_the_new_root_and_says_nothing(home, capsys):
    assert paths.root_from() == (home / NEW, "the default")
    assert paths.root() == home / NEW
    assert capsys.readouterr().err == ""
    assert not (home / NEW).exists() and not (home / OLD).exists()   # nothing made


def test_only_the_old_root_resolves_the_old_root_and_says_so_once(home, capsys):
    make(home / OLD)
    assert paths.root() == home / OLD
    r, where = paths.root_from()
    assert r == home / OLD
    assert where == "the old default (~/.sessionkit/phone/state is not there yet)"
    err = capsys.readouterr().err
    assert len(notices(err)) == 1, err
    assert str(home / OLD) in err and str(home / NEW) in err
    assert paths.old_root_notice() == notices(err)[0]
    assert not (home / NEW).exists()                                 # nothing moved or made


def test_both_there_the_new_root_wins(home, capsys):
    make(home / OLD)
    make(home / NEW)
    assert paths.root() == home / NEW
    assert capsys.readouterr().err == ""
    assert paths.old_root_notice() == ""


def test_an_old_root_left_as_a_symlink_to_the_new_one_is_one_root(home, capsys):
    make(home / NEW)
    (home / OLD).symlink_to(home / NEW)
    assert paths.root() == home / NEW
    assert paths.root().resolve() == (home / OLD).resolve()
    assert capsys.readouterr().err == ""


def test_a_dangling_old_symlink_is_no_old_root(home, capsys):
    (home / OLD).symlink_to(home / "gone")
    assert paths.root() == home / NEW
    assert capsys.readouterr().err == ""


def test_an_old_file_is_no_old_root_unless_it_is_there(home):
    """exists, not is_dir: a ~/.sessionphone that is there in any shape is
    the bot's, and the tools that need it refuse it later, loudly."""
    (home / OLD).write_text("")
    assert paths.root() == home / OLD


@pytest.mark.parametrize("how", ["shell", "phone.json"])
def test_an_explicit_root_is_never_re_pointed(home, monkeypatch, how, capsys):
    make(home / OLD)            # the fallback would pick this: it must not
    if how == "shell":
        monkeypatch.setenv("SESSIONPHONE_ROOT", str(home / "mine"))
    else:
        f = pathlib.Path(DEFAULT_FILE).expanduser()
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"bots": {"ada": {"SESSIONPHONE_ROOT": "~/mine"}}}))
    assert paths.root() == home / "mine"
    assert capsys.readouterr().err == ""
    # an explicit OLD path is the user's choice, not a fallback: no notice
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(home / OLD))
    make(home / NEW)
    assert paths.root() == home / OLD
    assert capsys.readouterr().err == ""


# ---- every reader asks the rule ----------------------------------------------

def test_settings_resolved_root_and_place_follow_the_rule(home):
    r = settings.Resolved(name="ada", name_from="", given={}, origin={}, file={},
                          file_origin={}, locked={})
    assert r.root == home / NEW
    assert settings.place({})[0] == os.path.realpath(home / NEW)
    make(home / OLD)
    assert r.root == home / OLD
    assert settings.place({}) == (os.path.realpath(home / OLD),
                                  os.path.realpath(home / OLD / "accounts"))
    # given roots are the given ones
    assert settings.place({"DC_ROOT": str(home / "d")})[0] == os.path.realpath(home / "d")
    assert settings.Resolved(name="ada", name_from="", given={"SESSIONPHONE_ROOT": "~/x"}, origin={},
                             file={}, file_origin={}, locked={}).root == home / "x"


def test_service_job_renders_the_rule_into_the_plist(home):
    j = service.job("ada", python="/p", given={})
    assert j["EnvironmentVariables"]["SESSIONPHONE_ROOT"] == str(home / NEW)
    assert j["WorkingDirectory"] == str(home / NEW)
    make(home / OLD)
    j = service.job("ada", python="/p", given={})
    assert j["EnvironmentVariables"]["SESSIONPHONE_ROOT"] == str(home / OLD)
    assert j["WorkingDirectory"] == str(home / OLD)
    j = service.job("ada", python="/p", given={"SESSIONPHONE_ROOT": str(home / "x")})
    assert j["WorkingDirectory"] == str(home / "x")


def _dc_env_root(h, also="print(dc_env.root_of({}))", **extra):
    """dc_env.ROOT (bound at import), then ALSO's line: a fresh process, as
    the bot and the tools are."""
    return subprocess.run(
        [sys.executable, "-c", ("import sys; sys.path.insert(0, sys.argv[1])\n"
                                f"from sessionphone.bridge import dc_env\nprint(dc_env.ROOT)\n{also}"),
         str(SRC)], env=child_env(h, **extra), capture_output=True, text=True, timeout=60, check=False)


def test_dc_env_resolves_the_old_root_and_says_so_once(home):
    p = _dc_env_root(home)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == [str(home / NEW)] * 2 and p.stderr == ""
    make(home / OLD)
    p = _dc_env_root(home)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == [str(home / OLD)] * 2
    assert len(notices(p.stderr)) == 1, p.stderr
    p = _dc_env_root(home, also="", DC_ROOT=str(home / "d"))      # DC_ROOT given: no rule asked
    assert p.stdout.split() == [str(home / "d")] and notices(p.stderr) == [], p.stderr


def test_dc_env_with_an_unreadable_settings_file_uses_the_rule(home):
    make(home / OLD)
    f = pathlib.Path(DEFAULT_FILE).expanduser()
    f.parent.mkdir(parents=True)
    f.write_text("{not json")
    p = subprocess.run(
        [sys.executable, "-c", ("import sys; sys.path.insert(0, sys.argv[1])\n"
                                "from sessionphone.bridge import dc_env\nprint(dc_env.ROOT)\nprint(dc_env.BAD[-1])"),
         str(SRC)], env=child_env(home), capture_output=True, text=True, timeout=60, check=False)
    assert p.returncode == 0, p.stderr
    root, bad = p.stdout.splitlines()
    assert root == str(home / OLD)
    assert bad.endswith(f"Using {home / OLD} for now.")


def _sourced_dc_env(h, **extra):
    return subprocess.run(["bash", "-c", f'source "{BIN / "dc-env"}"; echo "$DC_ROOT"'],
                          env=child_env(h, **extra), capture_output=True, text=True,
                          timeout=60, check=False)


def test_bin_dc_env_resolves_the_old_root_and_says_so_once(home):
    p = _sourced_dc_env(home)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == str(home / NEW) and notices(p.stderr) == []
    make(home / OLD)
    p = _sourced_dc_env(home)
    assert p.stdout.strip() == str(home / OLD)
    said = notices(p.stderr)
    assert len(said) == 1, p.stderr
    assert said[0].startswith("bash: using the old root")       # the tool's name, then the package's line
    p = _sourced_dc_env(home, SESSIONPHONE_ROOT=str(home / "x"))
    assert p.stdout.strip() == str(home / "x") and notices(p.stderr) == []


def test_bin_dc_env_last_resort_is_the_new_root_and_keeps_no_copy_of_the_rule(home, tmp_path):
    """The package cannot be asked (its Python fails): the plain new default,
    never the fallback, which is paths.default_root's alone."""
    make(home / OLD)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    py = fake / "python3"
    py.write_text("#!/bin/sh\nexit 1\n")
    py.chmod(0o755)
    # a copy of bin/ beside no package and no venv, so dc-env falls to python3
    tools = tmp_path / "pkg" / "bin"
    tools.mkdir(parents=True)
    (tools / "dc-env").write_text((BIN / "dc-env").read_text())
    p = subprocess.run(["bash", "-c", f'source "{tools / "dc-env"}"; echo "$DC_ROOT"'],
                       env=child_env(home, PATH=f"{fake}:/usr/bin:/bin"),
                       capture_output=True, text=True, timeout=60, check=False)
    assert p.stdout.strip() == str(home / NEW), p.stderr
    assert notices(p.stderr) == []


def _tool(name, h, *args, **extra):
    return subprocess.run([str(BIN / name), *args], env=child_env(h, **extra), cwd=h,
                          capture_output=True, text=True, timeout=60, check=False)


def test_bin_dc_log_reads_the_old_root_and_says_so_once(home):
    (make(home / OLD) / "bot.log").write_text("THE OLD ROOT'S LOG\n")
    p = _tool("dc-log", home, "--raw")
    assert p.returncode == 0, p.stderr
    assert "THE OLD ROOT'S LOG" in p.stdout
    assert len(notices(p.stderr)) == 1, p.stderr


def test_bin_dc_log_web_writes_under_the_old_root_and_says_so_once(home):
    make(home / OLD)
    p = _tool("dc-log-web", home, "--once")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == str(home / OLD / "calllog.html")
    assert len(notices(p.stderr)) == 1, p.stderr
    assert not (home / NEW).exists()


def test_bin_dc_log_web_on_a_fresh_home_writes_under_the_new_root(home):
    p = _tool("dc-log-web", home, "--once")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == str(home / NEW / "calllog.html")
    assert (home / NEW).stat().st_mode & 0o777 == 0o700
    assert notices(p.stderr) == [] and not (home / OLD).exists()


def test_the_web_verb_and_its_legs_guard_resolve_the_old_root(home, capsys):
    from sessionphone import cli
    make(home / OLD)
    assert cli._webdir(None) == home / OLD / "web"
    # the guard's own root is paths.root(): the old one, so --legs on it refuses
    why = webcall.legs_refusal(home / OLD, explicit_root=True, demo=False, home=home)
    assert "this process's own default root" in why and str(home / OLD) in why
    assert len(notices(capsys.readouterr().err)) == 1


@pytest.mark.parametrize("under", [NEW, NEW / "web", OLD, OLD / "web",
                                   pathlib.Path(".SessionKit/Phone/State/web")])
def test_the_legs_guard_knows_both_scanned_roots(home, under):
    """A keeper scans the new root and the old one (sessionvoice
    keeper/phone_roots): --legs inside either puts a live seat on the line."""
    top = NEW if under.parts[0].casefold() == ".sessionkit" else OLD
    got = webcall.scanned_under(home / under, home)
    assert got is not None and [x.casefold() for x in got.relative_to(home).parts] == \
        [x.casefold() for x in top.parts]
    assert webcall.legs_refusal(home / under, explicit_root=True, demo=False, home=home,
                                own_root=home / "elsewhere")


def test_the_legs_guard_leaves_a_scratch_root_alone(home):
    for scratch in (home / "scratch", home / ".sessionkit" / "phone" / "stateful", home / ".sessionkit"):
        assert webcall.scanned_under(scratch, home) is None, scratch


# ---- one resolver: no second copy of the rule ---------------------------------

# a default root as a path (the old one, ~/.sessionphone, or the new one); a
# launchd label such as com.sessionphone.bot is not a path
LITERALS = ("~/.sessionphone", "/.sessionphone", "sessionkit/phone/state")


def _python_sources():
    for p in sorted((SRC / "sessionphone").rglob("*.py")):
        yield p
    for p in sorted(BIN.iterdir()):
        if p.is_file() and p.read_text(errors="replace").startswith("#!/usr/bin/env python3"):
            yield p


def _docstrings(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def test_the_root_literals_are_spelt_only_in_paths_py():
    """Code, not prose: a string the code reads that names a default root is
    in paths.py alone (docstrings and comments may say where the root is)."""
    found = []
    for p in _python_sources():
        if p == SRC / "sessionphone" / "paths.py":
            continue
        tree = ast.parse(p.read_text(), str(p))
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
                    and any(lit in node.value for lit in LITERALS)):
                found.append(f"{p.relative_to(REPO)}:{node.lineno}")
    assert found == []


def test_the_shell_tools_keep_only_dc_envs_documented_last_resort():
    found = []
    for p in sorted(BIN.iterdir()):
        if not p.is_file() or not p.read_text(errors="replace").startswith("#!/usr/bin/env bash"):
            continue
        for n, line in enumerate(p.read_text().splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            if any(lit in code for lit in LITERALS):
                found.append(f"{p.name}:{n}: {line.strip()}")
    assert found == ['dc-env:{}: DC_ROOT="${{DC_ROOT:-${{SESSIONPHONE_ROOT:-$HOME/.sessionkit/phone/state}}}}"'
                     .format(_line_of(BIN / "dc-env", 'DC_ROOT="${DC_ROOT:-'))]


def _line_of(path, start):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith(start):
            return n
    raise AssertionError(f"{start!r} not in {path}")

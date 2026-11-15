"""An install puts every dc-* tool on the PATH, a kit install too (PHONE-150, PHONE-314).

The RINGING mail names `dc-answer CHAT` and `dc-hangup CHAT`, and bridged mail
names `dc-say`. calls.py names the checkout's bin/ tool by absolute path when
there is one (PHONE-56) and falls back to the BARE name, so on an install the
bare name must be on the user's PATH.

PHONE-150/290 shipped the tools as wheel DATA scripts. `uv tool install X`
links only X's own executables, never a dependency's, and a data script cannot
be re-exported, so on a kit install (sessionphone is the kit's dependency)
`command -v dc-say` found nothing (PHONE-314). Now each user-facing tool is a
console script, all with one target, sessionphone.dctools:main, which execs
the real tool: package data in src/sessionphone/dcbin/ (relative symlinks into
bin/, the allow-list), beside its dc-env, with DC_PYTHON naming the interpreter
that holds sessionphone. The kit re-declares those console scripts.

The install tests run a REAL `uv tool install` into throwaway tool and bin
dirs, for both routes: sessionphone's own wheel, and a kit-shaped stub wheel
that depends on it and re-exports the dc-* console scripts. Each finds the
tools by bare name with only that bin dir and the system dirs on PATH, and
runs their pre-checks end to end.

The wheel ships EVERY dc-* tool the user-facing text tells a user or an agent
to run: a tool named in README.md, docs/, skills/ or src/ (--help text, the
bot's mail) that an install did not ship was found by `command -v` on no host
but a checkout. The regression test below ties the allow-list to that text.

Fakes only: a fake bot answers jobs (tests/test_dc_channels.py), scratch
DC_ROOT, never a Delta send. The wheels are built once per module with uv.
"""
import configparser
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from test_dc_channels import Bot, allow, strip

from sessionphone import dctools

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
DCBIN = ROOT / "src" / "sessionphone" / "dcbin"
# The exact set an install ships as package data: every dc-* tool in bin/,
# since the README, docs, skill or --help text names each one to a user or an
# agent. Widening it is a symlink in src/sessionphone/dcbin/, a console script
# in pyproject.toml AND an edit here. bin/sessionphone (the plugin launcher;
# the console script owns that name) and bin/bridge-check (a developer's gate)
# never ship.
SHIPPED = {
    "dc-answer", "dc-call", "dc-call-mix", "dc-delete", "dc-dm", "dc-env", "dc-fetch", "dc-hangup",
    "dc-invite", "dc-live", "dc-log", "dc-log-web", "dc-react", "dc-ring", "dc-say",
    "dc-send-file", "dc-xdc-realtime", "dc-xdc-update",
}
# dc-env is a library the tools source: shipped beside them, never a console script
ENTRY = SHIPPED - {"dc-env"}
TARGET = "sessionphone.dctools:main"
NEVER_SHIPPED = {"sessionphone", "bridge-check"}
BARE_PATH = "/usr/bin:/bin"

UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(UV is None, reason="uv is not on PATH (make test runs under uv)")


def uv(*args, cwd=ROOT, env=None):
    r = subprocess.run([UV, *args], cwd=cwd, capture_output=True, text=True, timeout=600,
                       check=False, env=env)
    assert r.returncode == 0, f"uv {' '.join(args)}\n{r.stdout}\n{r.stderr}"
    return r


@pytest.fixture(scope="module")
def wheels(tmp_path_factory):
    """{"direct": wheel from the tree, "sdist": wheel built FROM the sdist}."""
    d = tmp_path_factory.mktemp("dist")
    uv("build", "--sdist", "--out-dir", str(d / "sdist"))
    uv("build", "--wheel", "--out-dir", str(d / "direct"))
    sdist, = (d / "sdist").glob("*.tar.gz")
    uv("build", "--wheel", "--out-dir", str(d / "from-sdist"), str(sdist))
    direct, = (d / "direct").glob("*.whl")
    from_sdist, = (d / "from-sdist").glob("*.whl")
    return {"direct": direct, "sdist": from_sdist}


def data_scripts(whl):
    """Every file under <dist>.data/scripts/: none now, they would collide in bin/."""
    with zipfile.ZipFile(whl) as z:
        return [i.filename for i in z.infolist() if ".data/scripts/" in i.filename]


def package_tools(whl):
    """{name: (mode, bytes)} for every file under sessionphone/dcbin/."""
    with zipfile.ZipFile(whl) as z:
        return {Path(i.filename).name: (i.external_attr >> 16, z.read(i))
                for i in z.infolist()
                if i.filename.startswith("sessionphone/dcbin/") and not i.is_dir()}


def console_scripts(whl):
    """{name: target} from the wheel's entry_points.txt [console_scripts]."""
    with zipfile.ZipFile(whl) as z:
        name, = [n for n in z.namelist() if n.endswith(".dist-info/entry_points.txt")]
        cp = configparser.ConfigParser(delimiters=("=",))
        cp.optionxform = str
        cp.read_string(z.read(name).decode())
    return dict(cp["console_scripts"])


def declared_scripts():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]


# ---- what the tree declares and the wheel holds -----------------------------


def test_dcbin_is_relative_links_into_bin():
    """The allow-list is links, never copies: a copy would drift from bin/."""
    links = sorted(DCBIN.iterdir())
    assert {p.name for p in links} == SHIPPED
    for p in links:
        assert p.is_symlink() and os.readlink(p) == f"../../../bin/{p.name}", p
        assert p.resolve() == (BIN / p.name).resolve(), p


def test_every_bin_tool_ships_but_the_developer_ones():
    """A new bin/ tool is a choice: it ships (a link here) or is named dev-only."""
    assert {p.name for p in BIN.iterdir()} - NEVER_SHIPPED == SHIPPED


def test_pyproject_declares_every_tool_as_one_console_script():
    """Every user-facing tool, and only those, is a console script on one target."""
    scripts = declared_scripts()
    assert scripts == {"sessionphone": "sessionphone.cli:main", **{n: TARGET for n in ENTRY}}


def test_no_wheel_data_scripts_are_declared():
    """Data scripts of the same names would collide in bin/ with the console scripts."""
    backend = tomllib.loads((ROOT / "pyproject.toml").read_text()).get("tool", {}).get("uv", {})
    assert "data" not in backend.get("build-backend", {})
    assert not (ROOT / "wheel-bin").exists()


def test_the_shim_runs_exactly_the_shipped_tools():
    assert dctools.tools() == ENTRY


# ---- the regression: what the text names, the wheel ships --------------------

# Where a user or an agent is told to run a tool: the README, the docs, the
# skill, and the package's own text (--help, the bot's mail, its hints).
USER_TEXT = [ROOT / "README.md", ROOT / "docs", ROOT / "skills", ROOT / "src"]
TOOL_WORD = re.compile(r"(?<![\w-])dc-[a-z0-9]+(?:-[a-z0-9]+)*")   # dc-log-web whole, not xdc-*


def named_bin_tools(roots):
    """{tool: first file naming it} for each dc-<tool> word under ROOTS that is a bin/ tool."""
    found = {}
    for top in roots:
        for p in sorted([top] if top.is_file() else top.rglob("*")):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:      # an icon, a cache file: no prose to read
                continue
            for word in TOOL_WORD.findall(text):
                if (BIN / word).is_file():
                    found.setdefault(word, p)
    return found


def unshipped(named, allow):
    return {tool: str(path) for tool, path in named.items() if tool not in allow}


def test_every_bin_tool_the_user_text_names_ships():
    """THE REGRESSION: the README, docs, skill and --help named a dozen dc-* tools
    a wheel never installed, dc-send-file among them. A tool documented from now
    on fails here until dcbin/ links it."""
    named = named_bin_tools(USER_TEXT)
    assert {"dc-send-file", "dc-answer", "dc-log"} <= set(named)   # the walk reads the text
    allow = {p.name for p in DCBIN.iterdir()}
    assert unshipped(named, allow) == {}, "named to a user or an agent, but not in dcbin/"


def test_the_regression_check_catches_an_unshipped_tool(tmp_path):
    """The checker's own check: the word must name a bin/ tool, whole."""
    doc = tmp_path / "doc.md"
    doc.write_text("Send it with `dc-send-file 12 app.xdc`; xdc-update and dc-nope are not tools.\n")
    named = named_bin_tools([doc])
    assert set(named) == {"dc-send-file"}
    assert unshipped(named, SHIPPED - {"dc-send-file"}) == {"dc-send-file": str(doc)}
    assert unshipped(named, SHIPPED) == {}


@needs_uv
@pytest.mark.parametrize("how", ["direct", "sdist"])
def test_the_wheel_ships_exactly_the_tools_byte_for_byte(wheels, how):
    """uv_build follows dcbin/'s symlinks and keeps the exec bit (measured, PHONE-314)."""
    got = package_tools(wheels[how])
    assert set(got) == SHIPPED
    for name, (mode, body) in got.items():
        assert stat.S_IMODE(mode) == 0o755, (name, oct(mode))
        assert body == (BIN / name).read_bytes(), f"{name} in the {how} wheel differs from bin/{name}"
    assert data_scripts(wheels[how]) == []


@needs_uv
@pytest.mark.parametrize("how", ["direct", "sdist"])
def test_the_console_scripts_are_sessionphone_and_the_tools(wheels, how):
    """No name collision: bin/sessionphone (the plugin launcher) is not shipped,
    and dc-env (a library) has no console script."""
    assert console_scripts(wheels[how]) == {"sessionphone": "sessionphone.cli:main",
                                            **{n: TARGET for n in ENTRY}}


@needs_uv
@pytest.mark.parametrize("how", ["direct", "sdist"])
def test_the_wheel_ships_the_guide_trail_app(wheels, how):
    """PHONE-165: `sessionphone app build` on an installed host needs the app as
    package data, byte for byte, with no webxdc.js beside it."""
    app = ROOT / "src" / "sessionphone" / "apps" / "guide-trail"
    prefix = "sessionphone/apps/guide-trail/"
    with zipfile.ZipFile(wheels[how]) as z:
        got = {n[len(prefix):]: z.read(n) for n in z.namelist()
               if n.startswith(prefix) and not n.endswith("/")}
    assert sorted(got) == ["icon.png", "index.html", "manifest.toml"]
    for name, body in got.items():
        assert body == (app / name).read_bytes(), f"{name} in the {how} wheel differs"


# ---- the shim: sessionphone.dctools ------------------------------------------


@pytest.mark.parametrize("name", ["dc-env", "sessionphone", "dc-nope", "bridge-check", "python"])
def test_the_shim_refuses_a_name_it_does_not_ship(monkeypatch, capsys, name):
    monkeypatch.setattr(sys, "argv", [f"/somewhere/bin/{name}", "--help"])
    monkeypatch.setattr(os, "execv", lambda *a: pytest.fail(f"execv {a}"))
    assert dctools.main() == 2
    err = capsys.readouterr().err
    assert f"'{name}' is not a dc-* tool" in err and "dc-say" in err


def test_the_shim_runs_a_bash_tool_as_itself():
    """In a checkout (an editable install) the link resolves to bin/'s own file,
    so dc-env finds the checkout's .venv exactly as before."""
    assert dctools.command("dc-say", ["owner", "hi"]) == [str((BIN / "dc-say").resolve()), "owner", "hi"]


@pytest.mark.parametrize("tool", ["dc-log", "dc-log-web"])
def test_the_shim_runs_a_python_tool_on_its_own_interpreter(tool):
    """Never the first python3 on PATH, which may not hold sessionphone."""
    assert dctools.command(tool, ["-n", "5"]) == [sys.executable, str((BIN / tool).resolve()), "-n", "5"]


def test_the_shim_execs_with_dc_python_set(monkeypatch):
    seen = {}

    def execv(path, argv):
        seen.update(path=path, argv=argv, py=os.environ.get("DC_PYTHON"))
        raise SystemExit(0)

    monkeypatch.setattr(sys, "argv", ["/x/bin/dc-hangup", "12"])
    monkeypatch.setenv("DC_PYTHON", "/stale/python")
    monkeypatch.setattr(os, "execv", execv)
    with pytest.raises(SystemExit):
        dctools.main()
    assert seen == {"path": str((BIN / "dc-hangup").resolve()),
                    "argv": [str((BIN / "dc-hangup").resolve()), "12"], "py": sys.executable}


# ---- a REAL `uv tool install`, both routes ------------------------------------
# "own": `uv tool install sessionphone` (its own console scripts).
# "kit": the kit's shape (the findings' M2): a package that depends on
# sessionphone and re-exports its console scripts, sessionphone and every dc-*.

KIT_STUB = """\
[project]
name = "kitstub"
version = "0.0.1"
requires-python = ">=3.11"
dependencies = ["sessionphone @ {wheel}"]

[project.scripts]
kitstub = "kitstub:main"
sessionphone = "sessionphone.cli:main"
{tools}

[build-system]
requires = ["uv_build>=0.11.9,<0.12.0"]
build-backend = "uv_build"
"""


def kit_stub_wheel(wheel, d):
    src = d / "kitstub"
    (src / "src" / "kitstub").mkdir(parents=True)
    (src / "src" / "kitstub" / "__init__.py").write_text("def main():\n    return 0\n")
    tools = "\n".join(f'{n} = "{TARGET}"' for n in sorted(ENTRY))
    (src / "pyproject.toml").write_text(KIT_STUB.format(wheel=wheel.as_uri(), tools=tools))
    uv("build", "--wheel", "--out-dir", str(d / "kitdist"), str(src))
    whl, = (d / "kitdist").glob("*.whl")
    return whl


def tool_install(whl, d):
    """`uv tool install WHL` into throwaway tool and bin dirs. Offline first (the
    uv cache serves the dependencies on a warm host), then online."""
    env = {**os.environ, "UV_TOOL_DIR": str(d / "tools"), "UV_TOOL_BIN_DIR": str(d / "bin")}
    args = ["tool", "install", "--python", sys.executable, str(whl)]
    r = subprocess.run([UV, *args[:2], "--offline", *args[2:]], capture_output=True, text=True,
                       timeout=600, check=False, env=env)
    if r.returncode != 0:
        uv(*args, env=env)
    return d / "bin"


@pytest.fixture(scope="module", params=["own", "kit"])
def installed(request, wheels, tmp_path_factory):
    """The bin dir a real `uv tool install` linked the tools into, for one route."""
    d = tmp_path_factory.mktemp(f"install-{request.param}")
    whl = wheels["direct"] if request.param == "own" else kit_stub_wheel(wheels["direct"], d)
    return tool_install(whl, d)


def run_on(path, tool, root, *args, wait="5"):
    """TOOL by bare name, as the RINGING mail names it, with PATH=PATH only."""
    env = {"HOME": str(root), "PATH": path, "DC_ROOT": str(root), "DC_TAKEN_WAIT": wait}
    return subprocess.run([tool, *args], env=env, capture_output=True, text=True,
                          timeout=60, check=False)


@needs_uv
def test_command_v_finds_dc_say(installed, tmp_path):
    """THE BUG (PHONE-314): after a kit install `command -v dc-say` found nothing."""
    r = subprocess.run(["/bin/sh", "-c", "command -v dc-say"], capture_output=True, text=True,
                       env={"PATH": f"{installed}:{BARE_PATH}", "HOME": str(tmp_path)}, check=False)
    assert r.returncode == 0 and r.stdout.strip() == str(installed / "dc-say"), r


@needs_uv
def test_the_bin_dir_holds_every_tool_and_no_dc_env(installed):
    names = {p.name for p in installed.iterdir()}
    assert ENTRY <= names
    assert "dc-env" not in names
    for name in ENTRY:
        assert os.access(installed / name, os.X_OK), name


@needs_uv
@pytest.mark.parametrize("tool,field", [("dc-answer", "answer"), ("dc-hangup", "hangup")])
def test_an_install_answers_with_its_env_off_path(installed, tmp_path, tool, field):
    """With only the tool bin dir on PATH the tool is found, and its pre-check
    runs in the env that holds sessionphone (DC_PYTHON, from the shim)."""
    path = f"{installed}:{BARE_PATH}"
    with Bot(tmp_path / "outbox", lambda job: (True, "done")) as bot:
        r = run_on(path, tool, tmp_path, "12")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"{tool}: done"
    assert [strip(j) for j in bot.jobs] == [{field: 12}]


@needs_uv
def test_dc_say_replies_from_an_install(installed, tmp_path):
    allow(tmp_path)
    with Bot(tmp_path / "outbox", lambda job: (True, "sent")) as bot:
        r = run_on(f"{installed}:{BARE_PATH}", "dc-say", tmp_path, "owner", "hi")
    assert r.returncode == 0, r.stderr
    assert [strip(j) for j in bot.jobs] == [{"chat": "owner", "text": "hi"}]


@needs_uv
def test_dc_say_refuses_an_unlisted_chat_from_an_install(installed, tmp_path):
    """The pre-check runs end to end: a chat not on the list is refused, exit 3."""
    allow(tmp_path)
    r = run_on(f"{installed}:{BARE_PATH}", "dc-say", tmp_path, "nobody", "hi")
    assert r.returncode == 3, r.stderr
    assert not (tmp_path / "outbox").exists() or not list((tmp_path / "outbox").glob("*.json"))


@needs_uv
@pytest.mark.parametrize("args,code", [((), 1), (("--profile", "nope", "12"), 2)])
def test_exit_codes_come_through_unchanged(installed, tmp_path, args, code):
    r = run_on(f"{installed}:{BARE_PATH}", "dc-answer", tmp_path, *args)
    assert r.returncode == code, r.stderr


@needs_uv
def test_no_bot_is_not_taken_exit_3(installed, tmp_path):
    r = run_on(f"{installed}:{BARE_PATH}", "dc-hangup", tmp_path, "12", wait="1")
    assert r.returncode == 3 and "NOT TAKEN" in r.stderr, r.stderr


@needs_uv
@pytest.mark.parametrize("tool", sorted(ENTRY))
def test_every_tool_runs_help_from_the_bin_dir(installed, tmp_path, tool):
    """Each tool is found by its bare name and answers --help, with only the
    tool bin dir and the system dirs on PATH (needs nothing else: no bot, no ffmpeg)."""
    r = run_on(f"{installed}:{BARE_PATH}", tool, tmp_path, "--help")
    assert r.returncode == 0, r.stderr
    assert tool in r.stdout, r.stdout[:200]


LINE = "2030-06-09T10:00:00 10:00:00 SENT to chat 12: hello from the log"


def installed_env(link, home):
    """A stranger's shell on an install: no DC_ROOT, so the root and the log
    are the package's to resolve (the default root, below HOME)."""
    root = home / ".sessionkit" / "phone" / "state"
    root.mkdir(parents=True)
    (root / "bot.log").write_text(LINE + "\n")     # a bot not started on this version yet
    return root, {"HOME": str(home), "PATH": f"{link}:{BARE_PATH}", "DC_AGENT": "ada"}


@needs_uv
def test_dc_log_reads_the_log_on_an_install(installed, tmp_path):
    """Before: the shebang's python3 had no sessionphone, ModuleNotFoundError."""
    _root, env = installed_env(installed, tmp_path / "home")
    r = subprocess.run(["dc-log"], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert "text hello from the log" in r.stdout


@needs_uv
def test_dc_log_web_writes_the_page_on_an_install(installed, tmp_path):
    root, env = installed_env(installed, tmp_path / "home")
    r = subprocess.run(["dc-log-web", "--once"], env=env, capture_output=True, text=True,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    page = root / "calllog.html"
    assert r.stdout.strip() == str(page)
    assert "hello from the log" in page.read_text()


# ---- dc-env: which python runs the pre-check ---------------------------------
# Three layouts, no build: a copy of dc-answer + dc-env in HOME/bin, and a
# wrapper per candidate interpreter that records its name when it is asked to
# run sessionphone code, then execs this interpreter (it has sessionphone).


def wrapper(path, name, mark):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\ncase "$2" in *sessionphone.*) echo {name} >> "{mark}";; esac\n'
                    f'exec "{sys.executable}" "$@"\n')
    path.chmod(0o755)


def layout(tmp_path, venv, beside, hint=False):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    for name in ("dc-answer", "dc-env"):
        shutil.copy2(BIN / name, home / "bin" / name)
    mark = tmp_path / "mark"
    mark.touch()
    if venv:
        wrapper(home / ".venv" / "bin" / "python", "checkout", mark)
    if beside:
        wrapper(home / "bin" / "python", "beside", mark)
    wrapper(tmp_path / "path" / "python3", "python3", mark)
    root = tmp_path / "root"
    root.mkdir()
    env = {"HOME": str(root), "PATH": f"{tmp_path / 'path'}:{BARE_PATH}",
           "DC_ROOT": str(root), "DC_TAKEN_WAIT": "0"}
    if hint:
        wrapper(tmp_path / "env" / "python", "hint", mark)
        env["DC_PYTHON"] = str(tmp_path / "env" / "python")
    r = subprocess.run([str(home / "bin" / "dc-answer"), "12"], env=env, capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    return set(mark.read_text().split())


@pytest.mark.parametrize("venv,beside,hint,used", [
    (True, True, True, {"checkout"}),     # a checkout: .venv wins, as before PHONE-150
    (False, True, True, {"hint"}),        # the console script's interpreter (PHONE-314)
    (False, True, False, {"beside"}),     # an old wheel install: the env's own python
    (False, False, False, {"python3"}),   # neither: python3 on PATH, as before
], ids=["checkout", "console-script", "tool-install", "python3"])
def test_dc_env_picks_the_python(tmp_path, venv, beside, hint, used):
    assert layout(tmp_path, venv, beside, hint) == used


# ---- dc-log: which python reads the log ---------------------------------------
# The same layouts for a copy of dc-log, which is Python: the shebang
# always starts python3 first, then dc-log moves to dc-env's choice before it
# imports the package. Each wrapper records every run, so the order shows.


def recorder(path, name, mark):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho {name} >> "{mark}"\nexec "{sys.executable}" "$@"\n')
    path.chmod(0o755)


def dc_log_runs(tmp_path, venv, beside, *args, hint=False):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    shutil.copy2(BIN / "dc-log", home / "bin" / "dc-log")
    mark = tmp_path / "mark"
    mark.touch()
    if venv:
        recorder(home / ".venv" / "bin" / "python", "checkout", mark)
    if beside:
        recorder(home / "bin" / "python", "beside", mark)
    recorder(tmp_path / "path" / "python3", "python3", mark)
    root = tmp_path / "root"
    root.mkdir()
    (root / "bot.log").write_text(LINE + "\n")
    env = {"HOME": str(tmp_path / "user"), "PATH": f"{tmp_path / 'path'}:{BARE_PATH}",
           "DC_ROOT": str(root), "DC_AGENT": "ada"}
    if hint:
        recorder(tmp_path / "env" / "python", "hint", mark)
        env["DC_PYTHON"] = str(tmp_path / "env" / "python")
    r = subprocess.run([str(home / "bin" / "dc-log"), *args], env=env, capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout, mark.read_text().split()


@pytest.mark.parametrize("venv,beside,hint,ran", [
    (True, True, True, ["python3", "checkout"]),    # a checkout: its .venv, as dc-env picks
    (False, True, True, ["python3", "hint"]),       # the console script's interpreter
    (False, True, False, ["python3", "beside"]),    # an old wheel install: the env's own python
    (False, False, False, ["python3"]),             # neither: stay on python3
], ids=["checkout", "console-script", "tool-install", "python3"])
def test_dc_log_moves_to_dc_envs_python_once(tmp_path, venv, beside, hint, ran):
    out, got = dc_log_runs(tmp_path, venv, beside, hint=hint)
    assert got == ran
    assert "text hello from the log" in out


def test_dc_log_help_never_moves(tmp_path):
    """--help needs no package, so it runs on the first python3 and stays there."""
    out, got = dc_log_runs(tmp_path, False, True, "--help", hint=True)
    assert got == ["python3"]
    assert out.startswith("dc-log")

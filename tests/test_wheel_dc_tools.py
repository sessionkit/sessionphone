"""A wheel install can answer a call (PHONE-150).

The RINGING mail names `dc-answer CHAT` and `dc-hangup CHAT`, and bridged mail
names `dc-say`. calls.py names the checkout's bin/ tool by absolute path when
there is one (PHONE-56) and falls back to the BARE name, so on a wheel install
the bare name must be on the user's PATH. It was not: the wheel shipped no
dc-* at all, and `command -v dc-answer` found nothing.

The tools are bash and source their sibling dc-env, so they ship as wheel
SCRIPTS (`<dist>.data/scripts/`, from the wheel-bin/ allow-list of symlinks
into bin/), installed side by side in the env's bin/. dc-env finds the
interpreter beside it, so a tool reached through a symlink in another dir (the
`uv tool install` layout: ~/.local/bin/dc-answer -> <tool env>/bin/dc-answer),
with that env's bin/ off PATH, still runs its pre-check in the env that holds
sessionphone.

The wheel ships EVERY dc-* tool the user-facing text tells a user or an agent
to run: a tool named in README.md, docs/, skills/ or src/ (--help text, the
bot's mail) that a wheel did not ship was found by `command -v` on no host but
a checkout. The regression test below ties the allow-list to that text, so a
newly documented bin/ tool fails the gate until it ships.

dc-log and dc-log-web are Python, `#!/usr/bin/env python3`: on a wheel install
that is a python3 with no sessionphone, so dc-log moves itself to dc-env's
Python (the checkout's .venv, else the python beside it) before it imports
the package. The install tests run them end to end.

Fakes only: a fake bot answers jobs (tests/test_dc_channels.py), scratch
DC_ROOT, never a Delta send. The wheel is built once per module with uv.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from test_dc_channels import Bot, allow, strip

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
# The exact set a wheel ships: every dc-* tool in bin/, since the README, docs,
# skill or --help text names each one to a user or an agent. Widening it is a
# symlink in wheel-bin/ AND an edit here. bin/sessionphone (the plugin launcher;
# the console script owns that name) and bin/bridge-check (a developer's gate)
# never ship.
SHIPPED = {
    "dc-answer", "dc-call", "dc-call-mix", "dc-delete", "dc-dm", "dc-env", "dc-hangup",
    "dc-invite", "dc-live", "dc-log", "dc-log-web", "dc-react", "dc-ring", "dc-say",
    "dc-send-file", "dc-xdc-realtime", "dc-xdc-update",
}
NEVER_SHIPPED = {"sessionphone", "bridge-check"}
BARE_PATH = "/usr/bin:/bin"

UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(UV is None, reason="uv is not on PATH (make test runs under uv)")


def uv(*args, cwd=ROOT):
    r = subprocess.run([UV, *args], cwd=cwd, capture_output=True, text=True, timeout=300, check=False)
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


def scripts(whl):
    """{name: (mode, bytes)} for every file under <dist>.data/scripts/."""
    with zipfile.ZipFile(whl) as z:
        return {Path(i.filename).name: (i.external_attr >> 16, z.read(i))
                for i in z.infolist()
                if ".data/scripts/" in i.filename and not i.is_dir()}


def entry_points(whl):
    with zipfile.ZipFile(whl) as z:
        name, = [n for n in z.namelist() if n.endswith(".dist-info/entry_points.txt")]
        return z.read(name).decode()


# ---- what the wheel holds ---------------------------------------------------


def test_wheel_bin_is_relative_links_into_bin():
    """The allow-list is links, never copies: a copy would drift from bin/."""
    links = sorted((ROOT / "wheel-bin").iterdir())
    assert {p.name for p in links} == SHIPPED
    for p in links:
        assert p.is_symlink() and os.readlink(p) == f"../bin/{p.name}", p


def test_every_bin_tool_ships_but_the_developer_ones():
    """A new bin/ tool is a choice: it ships (a link here) or is named dev-only."""
    assert {p.name for p in BIN.iterdir()} - NEVER_SHIPPED == SHIPPED


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
    on fails here until wheel-bin/ links it."""
    named = named_bin_tools(USER_TEXT)
    assert {"dc-send-file", "dc-answer", "dc-log"} <= set(named)   # the walk reads the text
    allow = {p.name for p in (ROOT / "wheel-bin").iterdir()}
    assert unshipped(named, allow) == {}, "named to a user or an agent, but not in wheel-bin/"


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
    got = scripts(wheels[how])
    assert set(got) == SHIPPED
    for name, (mode, body) in got.items():
        assert stat.S_IMODE(mode) == 0o755, (name, oct(mode))
        assert body == (BIN / name).read_bytes(), f"{name} in the {how} wheel differs from bin/{name}"


@needs_uv
@pytest.mark.parametrize("how", ["direct", "sdist"])
def test_the_one_console_script_is_still_sessionphone(wheels, how):
    """No name collision: bin/sessionphone (the plugin launcher) is not shipped."""
    assert entry_points(wheels[how]).split() == ["[console_scripts]", "sessionphone", "=",
                                                 "sessionphone.cli:main"]


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


# ---- a wheel install, its env's bin/ off PATH --------------------------------


@pytest.fixture(scope="module")
def installed(wheels, tmp_path_factory):
    """The wheel in a fresh venv (no deps: the tools' pre-checks are stdlib), and
    a separate dir of symlinks to its tools, as `uv tool install` lays them out."""
    d = tmp_path_factory.mktemp("install")
    venv = d / "venv"
    uv("venv", "--quiet", str(venv))
    uv("pip", "install", "--quiet", "--offline", "--no-deps", "--python", str(venv / "bin" / "python"),
       str(wheels["direct"]))
    link = d / "link"
    link.mkdir()
    for name in SHIPPED:
        (link / name).symlink_to(venv / "bin" / name)
    return venv, link


def run_on(path, tool, root, *args, wait="5"):
    """TOOL by bare name, as the RINGING mail names it, with PATH=PATH only."""
    env = {"HOME": str(root), "PATH": path, "DC_ROOT": str(root), "DC_TAKEN_WAIT": wait}
    return subprocess.run([tool, *args], env=env, capture_output=True, text=True,
                          timeout=60, check=False)


@needs_uv
def test_the_venv_bin_holds_the_tools(installed):
    venv, _link = installed
    for name in SHIPPED:
        p = venv / "bin" / name
        assert p.is_file() and os.access(p, os.X_OK), p


@needs_uv
@pytest.mark.parametrize("tool,field", [("dc-answer", "answer"), ("dc-hangup", "hangup")])
def test_a_wheel_install_answers_with_its_venv_off_path(installed, tmp_path, tool, field):
    """THE BUG: with only the symlink dir on PATH the tool is found, and its
    pre-check runs in the venv that holds sessionphone (before the dc-env fix:
    ModuleNotFoundError, exit 2)."""
    _venv, link = installed
    path = f"{link}:{BARE_PATH}"
    with Bot(tmp_path / "outbox", lambda job: (True, "done")) as bot:
        r = run_on(path, tool, tmp_path, "12")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"{tool}: done"
    assert [strip(j) for j in bot.jobs] == [{field: 12}]


@needs_uv
def test_dc_say_replies_from_a_wheel_install(installed, tmp_path):
    _venv, link = installed
    allow(tmp_path)
    with Bot(tmp_path / "outbox", lambda job: (True, "sent")) as bot:
        r = run_on(f"{link}:{BARE_PATH}", "dc-say", tmp_path, "owner", "hi")
    assert r.returncode == 0, r.stderr
    assert [strip(j) for j in bot.jobs] == [{"chat": "owner", "text": "hi"}]


@needs_uv
@pytest.mark.parametrize("args,code", [((), 1), (("--profile", "nope", "12"), 2)])
def test_exit_codes_come_through_unchanged(installed, tmp_path, args, code):
    _venv, link = installed
    r = run_on(f"{link}:{BARE_PATH}", "dc-answer", tmp_path, *args)
    assert r.returncode == code, r.stderr


@needs_uv
def test_no_bot_is_not_taken_exit_3(installed, tmp_path):
    _venv, link = installed
    r = run_on(f"{link}:{BARE_PATH}", "dc-hangup", tmp_path, "12", wait="1")
    assert r.returncode == 3 and "NOT TAKEN" in r.stderr, r.stderr


@needs_uv
@pytest.mark.parametrize("tool", sorted(SHIPPED))
def test_every_shipped_tool_runs_help_from_the_link_dir(installed, tmp_path, tool):
    """Each tool is found by its bare name and answers --help, with only the link
    dir and the system dirs on PATH (needs nothing else: no bot, no ffmpeg)."""
    _venv, link = installed
    r = run_on(f"{link}:{BARE_PATH}", tool, tmp_path, "--help")
    assert r.returncode == 0, r.stderr
    assert tool in r.stdout, r.stdout[:200]


LINE = "2030-06-09T10:00:00 10:00:00 SENT to chat 12: hello from the log"


def installed_env(link, home):
    """A stranger's shell on a wheel install: no DC_ROOT, so the root and the log
    are the package's to resolve (the default root, below HOME)."""
    root = home / ".sessionkit" / "phone" / "state"
    root.mkdir(parents=True)
    (root / "bot.log").write_text(LINE + "\n")     # a bot not started on this version yet
    return root, {"HOME": str(home), "PATH": f"{link}:{BARE_PATH}", "DC_AGENT": "ada"}


@needs_uv
def test_dc_log_reads_the_log_on_a_wheel_install(installed, tmp_path):
    """Before: the shebang's python3 had no sessionphone, ModuleNotFoundError."""
    _venv, link = installed
    _root, env = installed_env(link, tmp_path / "home")
    r = subprocess.run(["dc-log"], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert "text hello from the log" in r.stdout


@needs_uv
def test_dc_log_web_writes_the_page_on_a_wheel_install(installed, tmp_path):
    _venv, link = installed
    root, env = installed_env(link, tmp_path / "home")
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


def layout(tmp_path, venv, beside):
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
    r = subprocess.run([str(home / "bin" / "dc-answer"), "12"], env=env, capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    return set(mark.read_text().split())


@pytest.mark.parametrize("venv,beside,used", [
    (True, True, {"checkout"}),     # a checkout: .venv wins, as before PHONE-150
    (False, True, {"beside"}),      # a wheel install: the env's own python
    (False, False, {"python3"}),    # neither: python3 on PATH, as before
], ids=["checkout", "tool-install", "python3"])
def test_dc_env_picks_the_python(tmp_path, venv, beside, used):
    assert layout(tmp_path, venv, beside) == used


# ---- dc-log: which python reads the log ---------------------------------------
# The same three layouts for a copy of dc-log, which is Python: the shebang
# always starts python3 first, then dc-log moves to dc-env's choice before it
# imports the package. Each wrapper records every run, so the order shows.


def recorder(path, name, mark):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho {name} >> "{mark}"\nexec "{sys.executable}" "$@"\n')
    path.chmod(0o755)


def dc_log_runs(tmp_path, venv, beside, *args):
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
    r = subprocess.run([str(home / "bin" / "dc-log"), *args], env=env, capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout, mark.read_text().split()


@pytest.mark.parametrize("venv,beside,ran", [
    (True, True, ["python3", "checkout"]),    # a checkout: its .venv, as dc-env picks
    (False, True, ["python3", "beside"]),     # a wheel install: the env's own python
    (False, False, ["python3"]),              # neither: stay on python3
], ids=["checkout", "tool-install", "python3"])
def test_dc_log_moves_to_dc_envs_python_once(tmp_path, venv, beside, ran):
    out, got = dc_log_runs(tmp_path, venv, beside)
    assert got == ran
    assert "text hello from the log" in out


def test_dc_log_help_never_moves(tmp_path):
    """--help needs no package, so it runs on the first python3 and stays there."""
    out, got = dc_log_runs(tmp_path, False, True, "--help")
    assert got == ["python3"]
    assert out.startswith("dc-log")

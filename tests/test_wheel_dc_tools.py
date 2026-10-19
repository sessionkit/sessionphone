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

Fakes only: a fake bot answers jobs (tests/test_dc_channels.py), scratch
DC_ROOT, never a Delta send. The wheel is built once per module with uv.
"""
import os
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
# The exact set a wheel ships: answer and decline a RINGING call, reply in text
# (dc_env.reply_hint), and the dc-env all three source; and dc-xdc-realtime
# (PHONE-165), so an installed bot's app can be fed. Widening it is a symlink
# in wheel-bin/ AND an edit here.
SHIPPED = {"dc-answer", "dc-env", "dc-hangup", "dc-say", "dc-xdc-realtime"}
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

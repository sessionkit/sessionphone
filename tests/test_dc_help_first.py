"""dc-* -h/--help answers before dc-env reads any live config (PHONE-333).

Every bash dc-* tool sources bin/dc-env, and dc-env asked the package's Python
(so sessionmail and phone.json) who the bot is, where its root is and where its
log is, all at source time, BEFORE the tool's own `dc_help "${1:-}"` ran. So
help touched live state, and said `bot claude (the default: ...)` on stderr
where no config exists. dc-env now answers -h/--help as the tool's $1 first.

Each tool runs as a stranger's would on a host with no phone config: bin/ copied
where no .venv (and no src/) sits beside it, HOME an empty dir, no DC_* or
SESSIONPHONE_* set, and a PATH of /usr/bin:/bin behind a dir of stubs.
`message`, `session` and `uv` log their call and exit 99; `python3`/`python`
do the same for `-c` (every dc-env lookup is a `-c`) but exec the real
interpreter for a script, so dc-log and dc-log-web still run. After help: exit
0, the tool's header on stdout, nothing on stderr, and the stub log empty.

The walk is a glob over bin/ (never a list) and, the PHONE-314 console-script
route, over the shipped dcbin/ names run through sessionphone.dctools.main. A
negative control runs the same check on a dc-env with the guard cut out: it
must fail for every tool that sources dc-env, or this test proves nothing.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
DCBIN = REPO / "src" / "sessionphone" / "dcbin"
TOOLS = sorted(p.name for p in BIN.glob("dc-*"))
CONSOLE_TOOLS = sorted(p.name for p in DCBIN.glob("dc-*") if p.name != "dc-env")
GUARD = "# Help first (PHONE-333)"

STUB = """#!/bin/sh
echo "{name} $*" >> "{log}"
echo "{name}: STUB CALLED - help must not look anything up" >&2
exit 99
"""
PY_STUB = """#!/bin/sh
if [ "${{1:-}}" = -c ]; then
  echo "{name} $*" >> "{log}"
  echo "{name} -c: STUB CALLED - help must not ask the package" >&2
  exit 99
fi
exec "{real}" "$@"
"""


def sources_dc_env(tool: Path) -> bool:
    return "/dc-env\"" in tool.read_text()


class Host:
    """A host with no phone config: copied bin/, empty HOME, stubs first on PATH."""

    def __init__(self, tmp: Path, dc_env_text: str | None = None):
        self.root = tmp / "plugin"
        self.bin = self.root / "bin"
        shutil.copytree(BIN, self.bin, symlinks=True)
        if dc_env_text is not None:
            (self.bin / "dc-env").write_text(dc_env_text)
        # the console-script route's allow-list: relative links into bin/, as shipped
        self.dcbin = self.root / "dcbin"
        self.dcbin.mkdir()
        for name in CONSOLE_TOOLS:
            (self.dcbin / name).symlink_to(Path("..") / "bin" / name)
        self.home = tmp / "home"
        self.home.mkdir()
        self.cwd = tmp / "cwd"
        self.cwd.mkdir()
        self.log = tmp / "stub-calls.log"
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        for name in ("message", "session", "uv"):
            self._stub(name, STUB.format(name=name, log=self.log))
        for name in ("python3", "python"):
            self._stub(name, PY_STUB.format(name=name, log=self.log, real=sys.executable))
        self.env = {"PATH": f"{self.stubs}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(self.home)}

    def _stub(self, name, body):
        p = self.stubs / name
        p.write_text(body)
        p.chmod(0o755)

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def run(self, argv):
        self.log.unlink(missing_ok=True)
        return subprocess.run(argv, env=self.env, cwd=self.cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=False)

    def direct(self, name, *args):
        return self.run([str(self.bin / name), *args])

    def console(self, name, *args):
        """sessionphone.dctools.main as its console script runs it, argv0 = NAME,
        on this copy's dcbin/. Its interpreter, told to the tool as DC_PYTHON,
        is the stub python3: a dc-env that asked it would be logged."""
        driver = ("import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
                  "from sessionphone import dctools\ndctools.DIR = Path(sys.argv[2])\n"
                  "sys.executable = sys.argv[3]\nsys.argv = sys.argv[4:]\nsys.exit(dctools.main())\n")
        return self.run([sys.executable, "-I", "-c", driver, str(REPO / "src"), str(self.dcbin),
                         str(self.stubs / "python3"), name, *args])


def help_problems(host: Host, name: str, r) -> list[str]:
    """Why R (NAME asked for help) is not help-first; [] if it is."""
    out = []
    if r.returncode != 0:
        out.append(f"{name}: exit {r.returncode}")
    first = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip()), "")
    if not re.match(rf"(usage:\s*)?{re.escape(name)}(\s|$)", first, re.IGNORECASE):
        out.append(f"{name}: stdout does not open with its usage line: {first[:80]!r}")
    if r.stderr:
        out.append(f"{name}: stderr {r.stderr.strip()[:200]!r}")
    if host.calls():
        out.append(f"{name}: looked something up: {host.calls().strip()[:200]!r}")
    return out


def test_the_walk_finds_the_tools():
    """A broken glob must not pass vacuously."""
    assert {"dc-say", "dc-hangup", "dc-live", "dc-log", "dc-log-web", "dc-env"} <= set(TOOLS)
    assert {"dc-say", "dc-log"} <= set(CONSOLE_TOOLS)
    assert sum(sources_dc_env(BIN / n) for n in TOOLS) >= 15


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("name", TOOLS)
def test_help_reads_no_config(tmp_path, name, flag):
    host = Host(tmp_path)
    assert help_problems(host, name, host.direct(name, flag)) == []


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("name", CONSOLE_TOOLS)
def test_help_through_the_console_script_reads_no_config(tmp_path, name, flag):
    host = Host(tmp_path)
    assert help_problems(host, name, host.console(name, flag)) == []


def test_only_the_first_argument_is_help(tmp_path):
    """-h deeper in the arguments is not help, as before: dc-env does its lookups."""
    host = Host(tmp_path)
    r = host.direct("dc-say", "12", "-h")
    assert host.calls(), "dc-say 12 -h skipped dc-env's lookups"
    assert not r.stdout.startswith("dc-say [")


def without_guard() -> str:
    """bin/dc-env as it was: the PHONE-333 block cut out."""
    text = (BIN / "dc-env").read_text()
    start = text.index(GUARD)
    end = text.index("esac\n", start) + len("esac\n")
    old = text[:start] + text[end:]
    assert GUARD not in old and old != text
    return old


def test_negative_control_the_old_order_fails(tmp_path):
    """With the guard cut out, every tool that sources dc-env fails the check:
    it still prints help (its own dc_help), but only after the lookups."""
    host = Host(tmp_path, dc_env_text=without_guard())
    sourcing = [n for n in TOOLS if sources_dc_env(BIN / n)]
    assert len(sourcing) >= 15
    for name in sourcing:
        r = host.direct(name, "-h")
        problems = help_problems(host, name, r)
        assert r.returncode == 0, name                       # help still came, late
        assert any("looked something up" in p for p in problems), (name, problems)

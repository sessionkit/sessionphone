"""Every file in bin/ answers -h and --help, exits 0, and writes nothing (PHONE-127 SC4).

The walk is over bin/ itself, never a list, so a new tool without help fails
here. Nothing is skipped. dc-env is a library the tools source, but it sits on
the PATH with them, so it answers too, and refuses any other direct run. A
file in bin/ that is not executable fails the walk: it could not answer.

Each run gets a scrubbed environment (PATH only, plus HOME, DC_ROOT and TMPDIR
at empty scratch dirs) and a scratch cwd. The PATH is the host's with one dir
first, holding only a `python3` that runs the suite's own interpreter. After
it, all four must still be empty: no job, no log, no page, no cache. Network is not observed here; the
one tool that could reach it, bin/sessionphone through `uv run`, answers help
from the venv's entry point without uv.
"""
import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
TOOLS = sorted(p for p in BIN.iterdir() if p.is_file())


def scratch(tmp_path):
    dirs = {k: tmp_path / k.lower() for k in ("HOME", "DC_ROOT", "TMPDIR", "CWD")}
    for d in dirs.values():
        d.mkdir()
    return dirs


def python3_dir(base: Path) -> Path:
    """A dir holding only `python3`: an exec of the interpreter running the suite.

    bin/dc-log and bin/dc-log-web start `#!/usr/bin/env python3`, so on a host
    PATH with no other python3 ahead of Apple's, they ran /usr/bin/python3 (the
    Command Line Tools one), which writes HOME/Library/Caches/com.apple.python
    and failed "writes nothing" there only (PHONE-190). First on the PATH the
    tool gets, this makes the test mean the same on any machine. It holds
    python3 alone, so the venv's own dc-* copies never shadow bin/'s."""
    d = base / "python3-bin"
    if not d.is_dir():
        d.mkdir()
        (d / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (d / "python3").chmod(0o755)
    return d


def ask_help(tool, flag, dirs):
    path = os.environ.get("PATH", "/usr/bin:/bin")
    env = {"PATH": f"{python3_dir(dirs['HOME'].parent)}{os.pathsep}{path}",
           "HOME": str(dirs["HOME"]), "DC_ROOT": str(dirs["DC_ROOT"]), "TMPDIR": str(dirs["TMPDIR"])}
    return subprocess.run([str(tool), flag], env=env, cwd=dirs["CWD"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60, check=False)


def help_problems(tool, flag, dirs):
    """Why TOOL FLAG does not count as help; [] if it does."""
    if not os.access(tool, os.X_OK):
        return [f"{tool.name} is not executable"]
    try:
        r = ask_help(tool, flag, dirs)
    except OSError as e:                     # e.g. no shebang: exec format error
        return [f"{tool.name} {flag} could not run: {e}"]
    except subprocess.TimeoutExpired:
        return [f"{tool.name} {flag} did not return: help must not wait"]
    out = []
    if r.returncode != 0:
        out.append(f"{tool.name} {flag} exit {r.returncode}: {r.stderr.strip()[-200:]}")
    first = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip()), "")
    if not re.match(rf"(usage:\s*)?{re.escape(tool.name)}(\s|$)", first, re.IGNORECASE):
        out.append(f"{tool.name} {flag}: stdout does not open with a usage line naming it: {first[:80]!r}")
    for k, d in dirs.items():
        made = sorted(str(p.relative_to(d)) for p in d.rglob("*"))
        if made:
            out.append(f"{tool.name} {flag} wrote under {k}: {made[:5]}")
    return out


def test_walk_finds_the_tools():
    """A broken walk (empty bin/, wrong dir) must not pass vacuously."""
    names = {t.name for t in TOOLS}
    assert {"dc-send-file", "dc-env", "bridge-check", "sessionphone"} <= names


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("tool", TOOLS, ids=lambda p: p.name)
def test_every_bin_tool_answers_help_and_writes_nothing(tmp_path, tool, flag):
    assert help_problems(tool, flag, scratch(tmp_path)) == []


def test_dc_env_run_without_help_refuses(tmp_path):
    dirs = scratch(tmp_path)
    r = ask_help(BIN / "dc-env", "--profile", dirs)
    assert r.returncode == 2
    assert "sourced" in r.stderr


def test_sessionphone_help_without_a_venv_still_answers(tmp_path):
    """No .venv beside bin/: the usage line, exit 0, and still no uv cache."""
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    tool = root / "bin" / "sessionphone"
    shutil.copy2(BIN / "sessionphone", tool)
    dirs = scratch(tmp_path)
    for flag in ("-h", "--help"):
        assert help_problems(tool, flag, dirs) == []


@pytest.mark.parametrize("body", [
    '#!/usr/bin/env bash\n# dc-new - a tool with no help\necho "did the work" > "$HOME/out"\n',
    '#!/usr/bin/env bash\n# dc-new - exits 0, prints the wrong thing\necho ok\n',
    '#!/usr/bin/env bash\n# dc-new - asks for help but fails\necho "dc-new CHAT"; exit 1\n',
    '# dc-new - no shebang\necho dc-new\n',
], ids=["writes", "no-usage", "non-zero", "no-shebang"])
def test_a_tool_without_help_fails_the_walk(tmp_path, body):
    """The walker's own check: a helpless tool dropped into a bin/ is caught."""
    (tmp_path / "bin").mkdir()
    tool = tmp_path / "bin" / "dc-new"
    tool.write_text(body)
    tool.chmod(0o755)
    assert help_problems(tool, "--help", scratch(tmp_path))


def test_a_non_executable_file_fails_the_walk(tmp_path):
    (tmp_path / "bin").mkdir()
    tool = tmp_path / "bin" / "dc-new"
    tool.write_text("#!/usr/bin/env bash\n# dc-new - help\necho dc-new\n")
    tool.chmod(0o644)
    assert help_problems(tool, "-h", scratch(tmp_path)) == ["dc-new is not executable"]


# --- PHONE-159: -h prints the header, never the code -------------------------
#
# dc_help used to print from line 2 to the first BLANK line, so a tool whose
# header comment ran straight into code printed the whole script. The walk is
# a glob over bin/ and wheel-bin/ (never a list), and wheel-bin/ runs through
# its own path, a symlink, so $0 is the link as it is on a user's PATH.

WHEEL_BIN = BIN.parent / "wheel-bin"
DC_TOOLS = sorted(BIN.glob("dc-*")) + sorted(WHEEL_BIN.glob("dc-*"))


def code_lines(tool):
    """The stripped CODE lines of TOOL, the lines help must never print.

    Bash: every non-blank line that does not start with '#'. Python: every
    non-blank line after the module docstring closes.
    """
    text = Path(tool).resolve().read_text()
    lines = text.splitlines()
    if "python" in lines[0]:
        tree = ast.parse(text)
        doc = tree.body[0] if tree.body and isinstance(tree.body[0], ast.Expr) else None
        start = doc.end_lineno if doc is not None and isinstance(doc.value, ast.Constant) else 1
        return {ln.strip() for ln in lines[start:] if ln.strip()}
    return {ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")}


def code_in_help(tool, flag, dirs):
    """Why TOOL FLAG is not header-only help; [] if it is."""
    r = ask_help(tool, flag, dirs)
    out = []
    if r.returncode != 0:
        out.append(f"{tool.name} {flag} exit {r.returncode}: {r.stderr.strip()[-200:]}")
    if not r.stdout.strip():
        out.append(f"{tool.name} {flag}: printed nothing")
    code = code_lines(tool)
    leaked = [ln.strip() for ln in r.stdout.splitlines() if ln.strip() in code]
    if leaked:
        out.append(f"{tool.name} {flag} printed {len(leaked)} code line(s), first: {leaked[0][:80]!r}")
    return out


def test_dc_walk_finds_bin_and_wheel_bin():
    """The glob must reach both dirs, and wheel-bin/ through its symlinks."""
    names = {(t.parent.name, t.name) for t in DC_TOOLS}
    assert {("bin", "dc-call"), ("bin", "dc-log"), ("bin", "dc-log-web"),
            ("wheel-bin", "dc-hangup"), ("wheel-bin", "dc-env")} <= names
    assert any(t.is_symlink() for t in DC_TOOLS if t.parent == WHEEL_BIN)


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("tool", DC_TOOLS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_dc_tool_help_prints_its_header_and_no_code(tmp_path, tool, flag):
    assert code_in_help(tool, flag, scratch(tmp_path)) == []


def test_help_that_prints_code_fails_the_walk(tmp_path):
    """The checker's own check: a tool whose -h prints itself is caught."""
    tool = tmp_path / "dc-new"
    tool.write_text('#!/usr/bin/env bash\n# dc-new CHAT - a tool\nset -eu\ncat "$0"\n')
    tool.chmod(0o755)
    assert any("code line" in p for p in code_in_help(tool, "-h", scratch(tmp_path)))


def test_header_runs_straight_into_code(tmp_path):
    """dc_help itself, on the shape that broke: no blank line after the
    header, a bare '#' inside it. The header prints, '# ' stripped, '#' an
    empty line, and nothing after it."""
    tool = tmp_path / "dc-new"
    tool.write_text(f'#!/usr/bin/env bash\n# dc-new CHAT - a tool\n#\n#   more usage\n'
                    f'. "{BIN / "dc-env"}"\ndc_help "${{1:-}}"\necho ran\n')
    tool.chmod(0o755)
    r = ask_help(tool, "-h", scratch(tmp_path))
    assert (r.returncode, r.stdout) == (0, "dc-new CHAT - a tool\n\n  more usage\n")

"""No person's name in a user-facing string of `sessionphone web`.

A name on the call (who signed in, who spoke) comes from ONE setting
(web/names.py), and wording that needs a role says one ("the operator's GO").
So no string constant in src/sessionphone/web/*.py, no text of the page, and
nothing `sessionphone web --help` / `web enrol --help` prints, nor any string
literal in cli.py's web commands, names a person.

The names are private, so this file does not spell them out: a guard that carried
its own word list would ship the very list it guards. The words come from the
private scan list `make scan` uses, read at run time by tests/_scanlist.py from the
file SESSIONKIT_SCRUB_RULES names. Unset, each guard here SKIPS LOUDLY, naming the
variable. Set, every row of the list is scanned, and a positive control plants a
word synthesised from each row into each kind of subject and asserts that the row
fires. A failure names rows by their NAME column only, never a matched string.

The web code names its tailnet by design. This file does not spell that text either: it
reads it from the web module at run time (by_design(): the v4 range, the program asked for
the tailnet name, the passkey origin), and the rows whose pattern matches one of those
samples are left out by WHAT THEY MATCH, never by row name: the names are the list's to
change. Every other row runs. The left-out set must be non-empty, every row in it must
have been left out for a sample by_design() returned, and every sample must be in the
shipped code and leave a row out, so the exclusion cannot silently grow.

Comments are not string constants, so they are out of scope by construction. A
docstring IS a constant and is checked.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

import _scanlist
import pytest
from click.testing import CliRunner

import sessionphone
from sessionphone import cli

SRC = Path(sessionphone.__file__).parent
WEB = SRC / "web"
PAGE = WEB / "page" / "index.html"
CLI_WEB = ("_webdir", "web", "web_enrol")     # cli.py's `sessionphone web` functions
CLI_CONSTS = ("_ROOT_HELP",)
HELP_ARGV = [["web", "--help"], ["web", "enrol", "--help"]]


@functools.cache
def by_design() -> tuple[str, ...]:
    """The tailnet text the web module ships, read from the module at run time, never typed
    here: the v4 range server.check_bind admits, the program server.tailnet_name runs to
    ask for this host's tailnet name, and the passkey origin passkeys' docstring names
    (its `<host>.<tailnet>` placeholder and the suffix after it). None of it is a person.
    Each piece is one narrow token, so a row it leaves out is left out for that token alone."""
    from sessionphone.web import passkeys, server

    v4 = str(server.TAILNET_V4)
    # The program: read from tailnet_name's source, not by calling it (the web conftest
    # stubs it, so no test runs the program): the first item of each argv list it passes
    # to subprocess.run.
    fn = next(n for n in ast.parse((WEB / "server.py").read_text()).body
              if isinstance(n, ast.FunctionDef) and n.name == "tailnet_name")
    ran = [c.args[0].elts[0].value for c in ast.walk(fn)
           if isinstance(c, ast.Call) and ast.unparse(c.func) == "subprocess.run"
           and c.args and isinstance(c.args[0], ast.List) and c.args[0].elts
           and isinstance(c.args[0].elts[0], ast.Constant) and isinstance(c.args[0].elts[0].value, str)]
    # The asserts below test plain booleans, so a red one prints no sample: the samples
    # match rows of the list, and a failure message may sit in a public CI log.
    one_program = len(ran) == 1 and bool(re.fullmatch(r"[\w-]+", ran[0]))
    assert one_program, "tailnet_name() no longer runs exactly one program"
    origins = sorted(set(re.findall(r"<host>\.<tailnet>(?:\.[a-z0-9-]+)+", passkeys.__doc__ or "")))
    one_origin = len(origins) == 1
    assert one_origin, "passkeys' docstring no longer names exactly one tailnet origin"
    return (ran[0], origins[0], v4)


def split_rows():
    """(kept, left_out) of the private list's rows, all tiers, split on by_design()."""
    return _scanlist.split(_scanlist.rows(), by_design())


def person_rows():
    """The private list's rows, all tiers, but those a by_design() sample matches. Skips
    loudly unset."""
    return split_rows()[0]


def strings(node: ast.AST) -> list[tuple[int, str]]:
    """(line, text) of every str constant under `node`: docstrings, f-string parts, decorators' help."""
    return [(n.lineno, n.value) for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def hits(found: list[tuple[int, str]], where: str, rows) -> list[str]:
    """`where:line: rows [...]` for each string a row matches. Row NAMES only, never the text."""
    out = []
    for line, s in found:
        names = _scanlist.hits(s, rows)
        if names:
            out.append(f"{where}:{line}: rows {names}")
    return out


def cli_web_nodes(tree: ast.Module) -> list[ast.AST]:
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in CLI_WEB)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in CLI_CONSTS for t in n.targets))]
    assert len(nodes) == len(CLI_WEB) + len(CLI_CONSTS), "a cli.py web site moved: update CLI_WEB"
    return nodes


# ---- the scanner's reach: placeholder words, no list needed --------------------

def test_the_scanner_reads_docstrings_f_strings_and_help_but_not_comments():
    """What strings() hands the list: a docstring, an f-string part and a help= are
    seen; a comment is not. Placeholder words stand in for the private ones."""
    tree = ast.parse('def f():\n    """waits for Carol\'s GO"""\n    # Dave said so\n'
                     '    return f"hi {x} IVY", g(help="ask theo")\n')
    placeholder = re.compile(r"\b(carol|dave|ivy|theo)\b", re.IGNORECASE)
    got = {placeholder.search(s)[0] for _, s in strings(tree) if placeholder.search(s)}
    assert got == {"Carol", "IVY", "theo"}


# ---- the left-out rows: by what they match, and no more -------------------------

def web_subject() -> str:
    """Everything the guards below scan, as one text: web/*.py, the page, cli.py's web
    commands and the web help."""
    parts = [(WEB / p.name).read_text() for p in sorted(WEB.glob("*.py"))]
    parts.append(PAGE.read_text())
    parts += [s for n in cli_web_nodes(ast.parse((SRC / "cli.py").read_text())) for _, s in strings(n)]
    parts += [CliRunner().invoke(cli.main, argv).output for argv in HELP_ARGV]
    return "\n".join(parts)


def test_every_by_design_sample_is_text_the_shipped_web_code_holds():
    """A sample is shipped text, not a wildcard: each one stands verbatim in the subject.
    Needs no list."""
    subject = web_subject()
    samples = by_design()
    count = len(samples)
    assert count == 3, "by_design() returns the v4 range, the program and the origin"
    narrow = [i for i, s in enumerate(samples) if not s or any(c.isspace() for c in s)]
    assert narrow == [], f"a sample is not one token, by position: {narrow}"
    absent = [i for i, s in enumerate(samples) if s not in subject]
    assert absent == [], f"a sample is not in the shipped web text, by position: {absent}"


def test_rows_are_left_out_only_for_a_named_sample_and_each_sample_earns_its_place():
    """The left-out set is non-empty, every row in it was left out for a by_design() sample
    it matches, and every sample leaves a row out (a sample that matches nothing is dead
    and must go). Rows are reported by NAME, and no particular name is asserted."""
    _kept, left_out = split_rows()
    assert left_out, "no row is left out: by_design() is dead, drop it"
    stray = sorted(name for name, why in left_out.items() if not why or not set(why) <= set(by_design()))
    assert stray == [], f"left out for no named sample, by row: {stray}"
    dead = [i for i, s in enumerate(by_design()) if not any(s in why for why in left_out.values())]
    assert dead == [], f"a by_design() sample leaves no row out, by position: {dead}"


# ---- the guards: every row of the private list ----------------------------------

@pytest.mark.parametrize("path", sorted(WEB.glob("*.py")), ids=lambda p: p.name)
def test_no_string_constant_in_web_names_a_person(path):
    rows = person_rows()
    found = hits(strings(ast.parse(path.read_text(), str(path))), f"web/{path.name}", rows)
    assert found == [], f"a private word, by row: {found}"


def test_the_page_names_no_person():
    rows = person_rows()
    found = [f"index.html:{i}: rows {names}" for i, line in enumerate(PAGE.read_text().splitlines(), 1)
             if (names := _scanlist.hits(line, rows))]
    assert found == [], f"a private word, by row: {found}"


def test_no_string_literal_in_the_cli_web_commands_names_a_person():
    rows = person_rows()
    found = [s for n in cli_web_nodes(ast.parse((SRC / "cli.py").read_text())) for s in strings(n)]
    assert found
    bad = hits(found, "cli.py", rows)
    assert bad == [], f"a private word, by row: {bad}"


@pytest.mark.parametrize("argv", HELP_ARGV, ids=" ".join)
def test_the_web_help_names_no_person(argv):
    rows = person_rows()
    r = CliRunner().invoke(cli.main, argv)
    assert r.exit_code == 0
    found = _scanlist.hits(r.output, rows)
    assert found == [], f"a private word, by row: {found}"


# ---- the positive control: the guards keep their power ---------------------------

def test_a_word_planted_from_each_row_is_caught_in_every_subject():
    """For each row of the list, a word synthesised from the row's own regex is planted
    into a docstring, an f-string part, a help=, a line of the page and a help text,
    and the same scan the guards run must name that row in each. The planted word
    comes from the list at run time, never from this repo, and is never printed."""
    rows = person_rows()
    page = PAGE.read_text()
    missed = []
    for name, word in _scanlist.controls(rows):
        src = (f'def f():\n    """waits for {word}\'s GO"""\n'
               f'    return f"hi {{x}} {word}", g(help="ask {word}")\n')
        found = strings(ast.parse(src))
        caught = {line for line, s in found if name in _scanlist.hits(s, rows)}
        if caught != {2, 3}:                      # the docstring line, and the return line
            missed.append(f"{name}: source")
        if sum(name in _scanlist.hits(s, rows) for _, s in found) < 3:
            missed.append(f"{name}: one of docstring, f-string, help=")
        planted_page = page.replace("</body>", f"<p>{word}</p>\n</body>", 1)
        if planted_page == page or not any(name in _scanlist.hits(line, rows)
                                           for line in planted_page.splitlines()):
            missed.append(f"{name}: page")
        if name not in _scanlist.hits(f"Usage: sessionphone web [OPTIONS]\n  ask {word}\n", rows):
            missed.append(f"{name}: help text")
    assert missed == [], f"the scan missed a planted word, by row: {missed}"


def test_the_demo_refusal_and_help_say_a_role():
    """The GO a DEMO waits for is the operator's, in the refusal and in the help."""
    from sessionphone.web.call import legs_refusal
    why = legs_refusal(Path("/nowhere"), explicit_root=False, demo=False)
    assert "the operator's GO" in why
    r = CliRunner().invoke(cli.main, ["web", "--help"])
    assert " ".join(r.output.split()).count("the operator's GO") == 2, r.output

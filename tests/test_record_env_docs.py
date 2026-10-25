"""The docs never send a launchd bot owner to an EARS_* variable it cannot see.

`sessionphone bot install` carries only DC_* and the mail-route keys into the
launchd job (service.job), so EARS_RECORD and the rest of ears' recording
settings reach a bot run by hand, never the installed one (PHONE-185). A doc
block that pairs an EARS_RECORD* name with "the bot's environment" must say so
in the same block, by naming launchd or `bot install`.

A block is a paragraph, a list item, a table row or one line of a code fence:
a wrapped sentence is read whole, so the caveat may sit on the next line.

The second check, :func:`unscoped`, reads for meaning rather than one phrase.
Any unit that offers an EARS_* variable as a place to set something needs a
launchd caveat in its own scope. That covers a table row under an Environment
column, prose that says to set it in the environment or the shell, and
`$EARS_CONF` given as the file. A table row's scope is the row, its header and
the paragraph that leads into the table, never a sibling row. Wherever
`$EARS_CONF` is offered, the caveat must also name the file the launchd bot
reads, ``recording.EARS_CONF_DEFAULT``, in the same sentence.
"""
from __future__ import annotations

import re
from pathlib import Path

import _changelog  # CHANGELOG.md plus the pending fragments in changelog.d/

from sessionphone import service
from sessionphone.bridge import recording

ROOT = Path(__file__).resolve().parents[1]

EARS = re.compile(r"\bEARS_RECORD\w*")
BOT_ENV = re.compile(r"\bbot[’']s\s+environment\b|\bbot\s+environment\b", re.IGNORECASE)
CAVEAT = re.compile(r"\blaunchd\b|\bbot\s+install\b", re.IGNORECASE)
STARTS_BLOCK = re.compile(r"^\s*(?:[-*+] |\d+\. |\|)")


def _docs() -> list[Path]:
    files = [ROOT / "README.md", *_changelog.paths()]
    files += sorted((ROOT / "docs").rglob("*.md"))
    files += sorted((ROOT / "skills").rglob("*.md"))
    return [f for f in files if f.is_file()]


def blocks(text: str) -> list[tuple[int, str]]:
    """(first line number, text) for each paragraph, list item, table row and
    code-fence line."""
    out: list[tuple[int, str]] = []
    cur: list[str] = []
    start = 0
    fence = False

    def flush() -> None:
        if cur:
            out.append((start, " ".join(s.strip() for s in cur)))
            cur.clear()

    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            flush()
            fence = not fence
            continue
        if fence:
            out.append((n, line))
            continue
        if not line.strip():
            flush()
            continue
        if STARTS_BLOCK.match(line) or line.lstrip().startswith("#"):
            flush()
        if not cur:
            start = n
        cur.append(line)
    flush()
    return out


def stale(text: str) -> list[tuple[int, str]]:
    """The blocks that tell a bot owner to set EARS_RECORD* in the bot's
    environment and never say the launchd job does not carry it."""
    return [(n, b) for n, b in blocks(text)
            if EARS.search(b) and BOT_ENV.search(b) and not CAVEAT.search(b)]


# -- the premise: the job really does carry no EARS_* ---------------------------------


def test_the_launchd_job_carries_no_ears_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path))
    monkeypatch.setenv("EARS_RECORD", "1")
    monkeypatch.setenv("EARS_RECORD_NOTICE", "0")
    monkeypatch.setenv("EARS_CONF", str(tmp_path / "ears.json"))
    monkeypatch.setenv("DC_PLANTED", "x")   # a control: a DC_* IS carried
    env = service.job("agent", python="/usr/bin/python3")["EnvironmentVariables"]
    assert env.get("DC_PLANTED") == "x"
    assert not [k for k in env if k.startswith("EARS_")]


# -- the checker can fail, and passes a caveated line ----------------------------------


def test_a_planted_bad_line_is_caught():
    bad = "- **To keep recording calls**, set `EARS_RECORD=1` in the bot's environment.\n"
    assert [n for n, _ in stale(bad)] == [1]


def test_a_bad_line_wrapped_over_two_lines_is_caught():
    bad = ("To turn either off for every call, set it in the bot's environment\n"
           "(`EARS_RECORD_NOTICE=0`) or in `ears.json`.\n")
    assert [n for n, _ in stale(bad)] == [1]


def test_a_bad_table_row_is_caught():
    bad = ("| Order | Where |\n| --- | --- |\n"
           "| 2 | `EARS_RECORD` in the bot's environment |\n")
    assert [n for n, _ in stale(bad)] == [3]


def test_the_caveat_in_the_same_block_clears_it():
    good = ("- `EARS_RECORD=1` in the bot's environment works only for a bot run\n"
            "  by hand: the launchd job does not carry it.\n")
    assert stale(good) == []


def test_a_caveat_in_another_block_does_not_clear_it():
    bad = ("- set `EARS_RECORD=1` in the bot's environment.\n"
           "- the launchd job carries DC_* only.\n")
    assert [n for n, _ in stale(bad)] == [1]


def test_a_dc_setting_in_the_bot_env_is_not_flagged():
    assert stale("- set `DC_NO_REPLY_S=8` in the bot's environment.\n") == []


# -- the shipped docs ------------------------------------------------------------------


def test_the_docs_are_scanned():
    # a proven non-zero beside the zero below: the scan reads every doc, and
    # sees the EARS_RECORD blocks in each of README, CHANGELOG and docs/
    docs = _docs()
    names = {f.relative_to(ROOT).as_posix() for f in docs}
    assert {"README.md", "CHANGELOG.md", "docs/configuration.md", "docs/settings.md"} <= names
    # and every pending fragment, which reaches CHANGELOG.md only at the release
    assert {f.relative_to(ROOT).as_posix() for f in _changelog.fragments()} <= names
    for f in ("README.md", "CHANGELOG.md", "docs/configuration.md", "docs/settings.md"):
        hits = [b for _, b in blocks((ROOT / f).read_text(encoding="utf-8")) if EARS.search(b)]
        assert hits, f


def test_no_doc_sends_a_launchd_bot_to_the_env():
    found = []
    for f in _docs():
        for n, b in stale(f.read_text(encoding="utf-8")):
            found.append(f"{f.relative_to(ROOT)}:{n}: {b[:100]}")
    assert found == [], "\n".join(found)


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_precedence_table_keeps_the_env_tier_and_says_who_sees_it():
    rows = [ln for ln in _readme().splitlines() if ln.startswith("| 2 | `EARS_RECORD`")]
    assert len(rows) == 1, rows
    row = rows[0]
    assert "by hand" in row and "`ears`" in row
    assert "launchd" in row and "bot install" in row


def test_the_readme_names_a_path_that_works_for_the_launchd_bot():
    hits = [b for _, b in blocks(_readme())
            if "sees no `EARS_*`" in b]
    assert len(hits) == 1, hits
    assert "`ears recording on`" in hits[0]
    assert '`"record": true`' in hits[0]


def test_the_changelog_names_a_path_that_works():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    keep = [b for _, b in blocks(text) if b.startswith("- **To keep recording calls**")]
    assert len(keep) == 1, keep
    assert "`ears recording on`" in keep[0] and '`"record": true`' in keep[0]
    assert "launchd" in keep[0]


# -- by meaning: every EARS_* env tier, and $EARS_CONF, carries its own caveat ---------

ANY_EARS = re.compile(r"\bEARS_[A-Z_]*[A-Z]\b|\bEARS_\*")
ENV_WORD = re.compile(r"\benvironment\b|\benv\b|\bshell\b|\bexport\b|\bset\b", re.IGNORECASE)
ENV_COLUMN = re.compile(r"\benvironment\b|\benv\b", re.IGNORECASE)
CONF = re.compile(r"\bEARS_CONF\b")
SCOPED_CAVEAT = re.compile(r"\blaunchd\b|\bbot\s+install\b|\bby\s+hand\b", re.IGNORECASE)
DEFAULT_CONF = recording.EARS_CONF_DEFAULT   # the docs must name the path the code reads
SENTENCE = re.compile(r"(?<=[.;])\s+|\|")


def units(text: str) -> list[tuple[int, str, str, str]]:
    """(line, kind, text, scope) for each paragraph or list item ("prose"),
    table row ("row", its text carrying the header) and code-fence line
    ("fence"). ``scope`` is where a caveat for the unit may sit: a prose
    block's own text; a row, its header and the table's lead-in paragraph."""
    lines = text.splitlines()
    out: list[tuple[int, str, str, str]] = []
    cur: list[str] = []
    start = 0
    lead = ""
    n = 0

    def flush() -> None:
        nonlocal lead
        if cur:
            t = " ".join(s.strip() for s in cur)
            out.append((start, "prose", t, t))
            lead = t
            cur.clear()

    while n < len(lines):
        line = lines[n]
        if line.lstrip().startswith("```"):
            flush()
            n += 1
            while n < len(lines) and not lines[n].lstrip().startswith("```"):
                out.append((n + 1, "fence", lines[n], f"{lines[n]} {lead}"))
                n += 1
            n += 1
            continue
        if line.lstrip().startswith("|"):
            flush()
            first = n
            while n < len(lines) and lines[n].lstrip().startswith("|"):
                n += 1
            table = lines[first:n]
            header = table[0]
            for k, row in enumerate(table[2:], first + 3):
                out.append((k, "row", f"{row} {header}", f"{row} {header} {lead}"))
            lead = " ".join(table)
            continue
        if not line.strip():
            flush()
            n += 1
            continue
        if STARTS_BLOCK.match(line) or line.lstrip().startswith("#"):
            flush()
        if not cur:
            start = n + 1
        cur.append(line)
        n += 1
    flush()
    return out


def offers_env(kind: str, text: str) -> bool:
    """Does this unit offer an EARS_* variable as a place to set something?"""
    if not ANY_EARS.search(text):
        return False
    if CONF.search(text):
        return True
    if kind == "row":
        return bool(ENV_COLUMN.search(text))
    if kind == "fence":
        return False  # a log sample shows a value; it offers nothing
    return bool(ENV_WORD.search(text))


def _conf_caveat(scope: str) -> bool:
    return any(SCOPED_CAVEAT.search(s) and DEFAULT_CONF in s for s in SENTENCE.split(scope))


def unscoped(text: str) -> list[tuple[int, str]]:
    """(line, why) for each unit that offers an EARS_* env tier with no launchd
    caveat in its scope ("env"), or offers $EARS_CONF without saying, in one
    sentence, that the launchd bot reads DEFAULT_CONF ("conf")."""
    bad = []
    for n, kind, t, scope in units(text):
        if not offers_env(kind, t):
            continue
        if not SCOPED_CAVEAT.search(scope):
            bad.append((n, "env"))
        elif CONF.search(t) and not _conf_caveat(scope):
            bad.append((n, "conf"))
    return bad


ENV_TABLE = ("| Key | Environment | Default |\n| --- | --- | --- |\n"
             "| `record` | `EARS_RECORD` | `false` |\n")


def test_a_planted_env_table_row_with_no_caveat_is_caught():
    assert unscoped("The bot reads these keys:\n\n" + ENV_TABLE) == [(5, "env")]


def test_a_lead_in_caveat_clears_the_table():
    good = ("Under launchd the Environment column is not carried.\n\n" + ENV_TABLE)
    assert unscoped(good) == []


def test_a_caveat_in_a_sibling_row_does_not_clear_a_row():
    bad = ("| What | Where |\n| --- | --- |\n"
           "| recording | `ears.json`, or `$EARS_CONF` |\n"
           "| the job | the launchd plist |\n")
    assert unscoped(bad) == [(3, "env")]


def test_ears_conf_without_the_default_path_is_caught():
    bad = "The file is `$EARS_CONF`. A bot under launchd reads `ears.json`.\n"
    assert unscoped(bad) == [(1, "conf")]
    good = f"The file is `$EARS_CONF`. A bot under launchd reads `{DEFAULT_CONF}`.\n"
    assert unscoped(good) == []


def test_set_in_your_shell_is_caught_without_the_bot_env_phrase():
    # the phrase check (stale) misses this; the meaning check does not
    bad = "- Set `EARS_RECORD_DAYS=30` in your shell to keep calls longer.\n"
    assert stale(bad) == []
    assert unscoped(bad) == [(1, "env")]


def test_a_value_example_and_a_log_sample_are_not_offers():
    assert unscoped("- Any other value (`EARS_RECORD=yes`) fails closed.\n") == []
    assert unscoped("```text\nCALL recording: on (env EARS_RECORD=1)\n```\n") == []


def test_every_doc_scopes_its_ears_env_tier():
    found = []
    for f in _docs():
        text = f.read_text(encoding="utf-8")
        for n, why in unscoped(text):
            found.append(f"{f.relative_to(ROOT)}:{n}: [{why}] {text.splitlines()[n - 1][:100]}")
    assert found == [], "\n".join(found)


def test_the_meaning_check_sees_the_recording_tables_in_docs():
    # a proven non-zero beside the zero above: the env-column rows of both
    # docs/ recording tables are read as offers, so their caveat is load-bearing
    for f in ("docs/configuration.md", "docs/settings.md"):
        text = (ROOT / f).read_text(encoding="utf-8")
        rows = [n for n, kind, t, _ in units(text) if kind == "row" and offers_env(kind, t)]
        assert len(rows) >= 4, (f, rows)


def test_the_readme_says_ears_recording_on_needs_ears_conf_unset():
    hits = [b for _, b in blocks(_readme()) if "sees no `EARS_*`" in b]
    assert len(hits) == 1, hits
    assert "`EARS_CONF` unset" in hits[0]

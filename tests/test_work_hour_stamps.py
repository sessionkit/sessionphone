"""PHONE-162: no weekday work-hour stamp ships in the exported tree.

A stamp is a time of day plus a weekday and a date, the shape seats write as
"Bob, voice <time> <day> <date>". One that starts on Mon-Fri at 09:00 or later
and before 17:00 must not ship. Drop the time of day and keep the date.

What is scanned is what `git archive` would ship: the tracked files
(`git ls-files`) minus the export-ignored ones (`git check-attr export-ignore`).
The WORKING-TREE content is read, so an uncommitted stamp fails too. Outside a
git checkout (an sdist, say) the scan skips, with a reason.

Every fixture here is built by concatenation, so this file never matches itself.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# STAMP_RE and start() are SK-42's, verbatim, as the SK-97 tool work-hour-stamps
# carries them (the release gate's measure). Keep them in step with that tool.
STAMP_RE = re.compile(r"[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?(-[0-9]{1,2}:[0-9]{2})?"
                      r" (Mon|Tue|Wed|Thu|Fri) 20[0-9]{2}-[0-9]{2}-[0-9]{2}")


def start(m):
    hh, mm = m.group(0).split(" ", 1)[0].split("-", 1)[0].split(":")[:2]
    return int(hh) * 60 + int(mm)


def work_hour(line):
    """The work-hour stamps in one line: starts 09:00 <= t < 17:00 (540-1020)."""
    return [m.group(0) for m in STAMP_RE.finditer(line) if 540 <= start(m) < 1020]


def git(root, *args, **kw):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          check=True, **kw).stdout


def checkout_root(root):
    """`root` if it is the top of a git checkout and git runs, else None."""
    if shutil.which("git") is None:
        return None
    try:
        top = git(root, "rev-parse", "--show-toplevel", text=True).strip()
    except subprocess.CalledProcessError:
        return None
    return root if Path(top).resolve() == Path(root).resolve() else None


def exported(root):
    """Tracked paths `git archive` would ship: export-ignore honoured."""
    paths = [p for p in git(root, "ls-files", "-z").decode().split("\0") if p]
    if not paths:
        return []
    out = git(root, "check-attr", "-z", "--stdin", "export-ignore",
              input="\0".join(paths).encode()).decode().split("\0")
    ignored = {out[i] for i in range(0, len(out) - 2, 3)
               if out[i + 2] not in ("unspecified", "unset")}
    return [p for p in paths if p not in ignored]


def scan(root):
    """(path, line number, stamp) for every work-hour stamp in the export."""
    hits = []
    for rel in exported(root):
        path = Path(root) / rel
        # a symlink ships as a link, not as its target's bytes; a deleted file ships nothing
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:  # binary, as the tool skips it
            continue
        for n, line in enumerate(data.decode("utf8", "replace").splitlines(), 1):
            hits += [(rel, n, s) for s in work_hour(line)]
    return hits


def stamp(time, day, date="2030-06-09"):
    """A stamp built by concatenation, so no literal one sits in this file."""
    return time + " " + day + " " + date


def test_no_work_hour_stamps_ship():
    root = checkout_root(ROOT)
    if root is None:
        pytest.skip("not a git checkout (an sdist?): no `git archive` tree to scan")
    hits = scan(root)
    assert not hits, (
        f"{len(hits)} weekday work-hour stamp(s) in the exported tree. "
        "Drop the time of day; keep the date:\n"
        + "\n".join(f"  {p}:{n}: {s}" for p, n, s in hits))


@pytest.mark.parametrize("time,day", [
    ("09" + ":00", "Mon"),
    ("10" + ":30", "Tue"),
    ("12" + ":53:07", "Wed"),
    ("10" + ":34-10:38", "Thu"),
    ("16" + ":59", "Fri"),
    ("9" + ":05", "Tue"),
])
def test_port_catches_a_work_hour_stamp(time, day):
    line = "(Bob, voice " + stamp(time, day) + ": a ruling)"
    assert work_hour(line) == [stamp(time, day)]


@pytest.mark.parametrize("text", [
    stamp("10" + ":30", "Sat"),  # weekend
    stamp("10" + ":30", "Sun"),
    stamp("08" + ":59", "Mon"),  # before 09:00
    stamp("17" + ":00", "Fri"),  # 17:00 is not before 17:00
    stamp("23" + ":10", "Wed"),
    "10" + ":30 Tue",  # no date
    "Tue 2030-06-09",  # the stripped form: no time of day
    "10" + ":30 2030-06-09",  # no weekday
])
def test_port_passes_what_is_not_a_work_hour_stamp(text):
    assert work_hour("(Bob, voice " + text + ")") == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_scan_reads_the_exported_working_tree(tmp_path):
    """Tracked, working-tree content counts; export-ignored, untracked and
    symlinked paths do not; path:line is reported."""
    hit = stamp("11" + ":15", "Mon")
    git(tmp_path, "init", "-q")
    (tmp_path / "ok.md").write_text("clean\n" + stamp("11" + ":15", "Sat") + "\n")
    (tmp_path / "shipped.md").write_text("one\ntwo\n")
    (tmp_path / "ignored.md").write_text(hit + "\n")
    (tmp_path / ".gitattributes").write_text("ignored.md export-ignore\n")
    (tmp_path / "link.md").symlink_to("ignored.md")
    git(tmp_path, "add", ".")
    assert scan(tmp_path) == []
    # an uncommitted stamp in a tracked file fails before it is committed
    (tmp_path / "shipped.md").write_text("one\n(Bob, voice " + hit + ")\n")
    (tmp_path / "untracked.md").write_text(hit + "\n")
    assert scan(tmp_path) == [("shipped.md", 2, hit)]


def test_outside_a_checkout_there_is_nothing_to_scan(tmp_path):
    assert checkout_root(tmp_path) is None


def test_this_file_never_matches_itself():
    assert not any(work_hour(line) for line in Path(__file__).read_text().splitlines())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([os.fspath(Path(__file__)), "-q"]))

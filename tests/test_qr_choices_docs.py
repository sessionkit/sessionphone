"""The docs offer the ways to see the invite QR as choices, in order.

SKILL.md's setup step 4 puts the ways to the person before it shows any: the
QR in a window first (the PNG, or the `qr` page), the plain link second, and
the terminal QR last, because Claude Code folds a long tool result and the
person then sees no code until Ctrl+O. The step also says what to do when the
phone waits at "Establishing connection, please wait". The README's `invite`
section and the CHANGELOG say the same, in what came after 0.2.1: the
Unreleased part before a release stamp, the new release's section after one.

The fold count and the PNG size are held to the code: the line count comes
from `qrprint.text` on an invite-shaped link, and the size from the QR's
module count, `qrprint.BORDER` and `qrprint.PNG_SCALE`.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from sessionphone import qrprint

ROOT = Path(__file__).resolve().parents[1]

# The shape of a real invite; every value is made up.
LINK = ("https://i.delta.chat/#0123456789ABCDEF0123456789ABCDEF01234567&v=3"
        "&i=AbCdEfGhIjKlMnOpQrStUvWx&s=ZyXwVuTsRqPoNmLkJiHgFeDc"
        "&a=bot%40relay.example&n=Test+Agent")

# Claude Code shows the first 3 lines of a long tool result and folds the
# rest into "+N lines (ctrl+o to expand)".
SHOWN = 3


def _flat(text: str) -> str:
    """Whitespace folded to single spaces, so a wrapped line still matches."""
    return " ".join(text.split())


def _step4() -> str:
    text = (ROOT / "skills" / "sessionphone" / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^4\. (.*?)^5\. ", text, re.MULTILINE | re.DOTALL)
    assert m, "SKILL.md has no setup step 4 followed by a step 5"
    return _flat(m.group(1))


def _readme_invite() -> str:
    """The README's `invite` section, up to the next `### ` heading."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^### The invite, as a QR[^\n]*\n(.*?)^### ", text,
                  re.MULTILINE | re.DOTALL)
    assert m, "README.md has no `### The invite, as a QR` section"
    return _flat(m.group(1))


# The last release these notes are NOT in; they came after it.
SHIPPED_AFTER = "## [0.2.1]"


def _since(text: str) -> str:
    """The CHANGELOG from the top down to `SHIPPED_AFTER`: [Unreleased] and any
    release cut since. `scripts/release.py` moves [Unreleased]'s entries under
    a new `## [X.Y.Z] - date` and leaves [Unreleased] empty, so reading
    [Unreleased] alone failed on every release commit; this part holds the
    notes before a stamp and after it."""
    m = re.search(r"^## \[Unreleased\]\n(.*?)^" + re.escape(SHIPPED_AFTER), text,
                  re.MULTILINE | re.DOTALL)
    assert m, f"CHANGELOG.md has no `## [Unreleased]` part above `{SHIPPED_AFTER}`"
    return _flat(m.group(1))


def _unreleased() -> str:
    return _since((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


def _stamped(text: str, version: str, tmp_path: Path, monkeypatch) -> str:
    """`text` as `scripts/release.py` stamps it for `version`: its own `stamp`."""
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    release.stamp(version, dry=False)
    return (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")


def _terminal_lines() -> int:
    """What `invite --qr` prints: the QR, a blank line, then the link."""
    return qrprint.text(LINK).count("\n") + 2


def test_step_4_offers_the_ways_as_choices_before_showing_any():
    step = _step4()
    assert "as choices" in step
    assert "before you show any" in step


def test_step_4_orders_the_window_first_the_link_second_the_terminal_qr_last():
    step = _step4()
    png = step.index("sessionphone invite --png")
    page = step.index("sessionphone qr >")
    link = step.index("`sessionphone invite` prints it on one line")
    terminal = step.index("sessionphone invite --qr")
    assert max(png, page) < link < terminal


def test_step_4_opens_the_files_with_open_and_names_xdg_open():
    step = _step4()
    assert "&& open ~/.sessionphone/invite.png" in step
    assert "&& open ~/.sessionphone/connect.html" in step
    assert "xdg-open" in step


def test_step_4_no_longer_routes_the_png_through_show_me():
    assert "show-me" not in _step4()


def test_step_4_says_the_terminal_qr_is_likely_folded_and_ctrl_o_expands_it():
    step = _step4()
    total = _terminal_lines()
    assert f"about {total} lines" in step
    assert f"+{total - SHOWN} lines (ctrl+o to expand)" in step
    assert "Ctrl+O" in step
    assert "--invert" in step


def test_step_4_says_scan_the_qr_code_above_only_to_forbid_it():
    step = _step4()
    hits = [m.start() for m in re.finditer(r"scan the QR code above", step, re.IGNORECASE)]
    assert hits, "step 4 should name the phrase it forbids"
    for i in hits:
        assert step[max(0, i - 12):i].rstrip().endswith('Never say "'), step[i - 40:i + 40]


def test_step_4_carries_the_handshake_hint():
    step = _step4()
    assert '"Establishing connection, please wait"' in step
    status = step.index("sessionphone bot status")
    relink = step.index("run `sessionphone link` again")
    assert status < relink
    assert "scan again" in step[relink:]


def test_readme_invite_section_names_the_fold_and_no_show_me():
    section = _readme_invite()
    total = _terminal_lines()
    assert "show-me" not in section
    assert f"about {total} lines" in section
    assert f"+{total - SHOWN} lines (ctrl+o to expand)" in section
    assert "xdg-open" in section


def test_readme_states_the_png_size_the_code_writes():
    modules = len(list(qrprint._make(LINK).matrix_iter(border=qrprint.BORDER)))
    side = modules * qrprint.PNG_SCALE
    m = re.search(r"about (\d+) px a side", _readme_invite())
    assert m, "README's --png bullet should give the PNG's side in px"
    assert abs(int(m.group(1)) - side) <= 10, (m.group(1), side)


CHOICES = [
    "the QR in a window first",
    "the plain link second",
    "the terminal QR (`invite --qr`) last",
    "Ctrl+O",
    "xdg-open",
    "sessionphone bot status",
]


@pytest.mark.parametrize("words", CHOICES)
def test_changelog_unreleased_names_the_choices(words):
    assert words in _unreleased()


@pytest.mark.parametrize("words", CHOICES)
def test_changelog_still_names_the_choices_after_a_release_stamp(words, tmp_path, monkeypatch):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    stamped = _stamped(text, "9.9.9", tmp_path, monkeypatch)
    assert re.search(r"^## \[Unreleased\]\n\n## \[9\.9\.9\] - ", stamped, re.MULTILINE)
    assert words in _since(stamped)


def test_changelog_reader_bites_in_both_shapes(tmp_path, monkeypatch):
    """A note dropped from the CHANGELOG is missed, stamped or not; one that
    lives only in an older release does not count."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    word = "the plain link second"
    wrapped = re.compile(r"\s+".join(map(re.escape, word.split())))   # it may wrap
    dropped, n = wrapped.subn("the link", text)
    assert n == 1, "the control needs the phrase exactly once"
    assert word in _since(text)
    assert word not in _since(dropped)
    assert word not in _since(_stamped(dropped, "9.9.9", tmp_path, monkeypatch))
    head, old = dropped.split(SHIPPED_AFTER, 1)
    assert word not in _since(head + SHIPPED_AFTER + old + "\n- " + word + "\n")

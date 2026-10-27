"""The skill is a short map: it points at the family's etiquette and links its long reference.

The family's etiquette for several agents in one seat has one home, the
sessionkit skill's card "Several agents in one seat". Every other family
skill carries one pointer line to it, word for word, after its lead
paragraph, and keeps no copy of the card. The card points back here for a
guest on a call, under the heading "Guests heard on a call", so that
heading stays.

The skill keeps the steps and the few commands to reach for. Long reference
lives in docs/, one subject to a page, and each page is linked from the
skill and links back to it.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "sessionphone" / "SKILL.md"

# The family's pointer line, word for word: the same text in every family skill.
POINTER = ('Several agents in one seat? See the sessionkit skill\'s card, '
           '"Several agents in one seat".')

# The pages the skill's long reference moved to.
PAGES = ["first-run.md", "bot-install.md", "call-hearing.md", "calls.md", "call-guests.md",
         "what-reaches-you.md", "callers.md", "channels.md", "bot-files.md"]

# The card's own lines. The skill points to the card and repeats none of them.
CARD_WORDS = ["Only the holder speaks", "speak when named", "--next", "--now", "--cc",
              "reply-all", "Reply to all", "a voice the holder does not use",
              "unsigned request"]


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _body() -> str:
    """The skill without its front matter."""
    return _text().split("\n---\n", 1)[1]


def _paragraphs() -> list[str]:
    return [p for p in re.split(r"\n\s*\n", _body().strip()) if p.strip()]


def test_the_pointer_line_follows_the_lead_paragraph_word_for_word():
    paras = _paragraphs()
    assert paras[0] == "# sessionphone"
    assert paras[1].startswith("The person rings their Claude Code session"), paras[1]
    assert paras[2] == POINTER


def test_the_pointer_line_is_there_once():
    assert _text().count(POINTER) == 1
    assert _text().count("Several agents in one seat") == 2   # the question and the card's name


def test_the_skill_keeps_no_copy_of_the_card():
    text = _text()
    found = [w for w in CARD_WORDS if w.lower() in text.lower()]
    assert found == [], found


def test_guests_heard_on_a_call_keeps_its_heading():
    assert re.search(r"^## Guests heard on a call$", _text(), re.MULTILINE)


def test_every_page_is_linked_from_the_skill_and_links_back():
    text = _text()
    for page in PAGES:
        path = ROOT / "docs" / page
        assert path.is_file(), page
        assert re.search(rf"^\[[a-z-]+\]: \.\./\.\./docs/{re.escape(page)}$", text, re.MULTILINE), page
        assert re.search(r"^\[skill\]: \.\./skills/sessionphone/SKILL\.md$",
                         path.read_text(encoding="utf-8"), re.MULTILINE), page


def test_each_page_makes_its_point_on_the_first_screen():
    """One subject a page, its point above the fold: a `# ` title, then the
    lead paragraph at once, before any list or section."""
    for page in PAGES:
        lines = (ROOT / "docs" / page).read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# "), page
        assert lines[1] == "" and lines[2].strip(), page
        assert not lines[2].startswith(("#", "-", "```")), page


def test_the_skill_stays_a_short_map():
    """It was 518 lines with the reference in it. The steps (step 4's choices
    in full) and the few commands fit in well under 300."""
    assert len(_text().splitlines()) < 300


def test_a_posted_file_keeps_its_contract_in_the_page_and_a_line_in_the_map():
    """The attachment line is agent-facing contract: the page it moved to has
    it whole, and the skill's map keeps one line beside the request bullet."""
    text = _text()
    line = "- **A posted file names its path:** read it at PATH, never search by NAME."
    assert text.count(line) == 1
    assert text.index("- **A request is a stranger.**") < text.index(line)
    page = " ".join((ROOT / "docs" / "what-reaches-you.md").read_text(encoding="utf-8").split())
    for words in ("`[attachment: NAME (MIME) at PATH]`", "read the file at PATH",
                  "never search the disk for it by NAME", "`- UNTRUSTED: ...`",
                  "open it only if your owner asks"):
        assert words in page, words

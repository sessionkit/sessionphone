"""PHONE-271: a session that started plain gets the box repair first, not --hear-here.

A first `session start` with no box `claude` starts plain and says so on one
stderr line (sessiontools, `start/default_seat.py`, `gate_line`'s NO_BOX form):

    starting plain: box claude is missing, so this session will NOT hear Delta
    messages or calls; repair: message setup --box claude

Asked to ring the user, the agent reached for `bot install --hear-here` and a
subscription: both end with this session. The "Hearing it" bullet (in
docs/bot-install.md, which the skill's setup step 7 links) now
says, in this order: look for the `starting plain:` line; give its repair, then
a new `session start`; only then the stopgap. It keeps the rule that
--hear-here runs only when the user asks, and invents no repair for the
`starting plain:` lines that carry none (held, macOS-only, could not check).

sessiontools is not a dependency here, so its line is pinned as text.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The NO_BOX line's two halves, as sessiontools prints them for seat SEAT.
PLAIN = "starting plain:"
REPAIR = "repair: message setup --box"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _hearing_it() -> str:
    """The "Hearing it." bullet, from its bold head to "Where Delta text lands."."""
    text = (ROOT / "docs" / "bot-install.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*Hearing it\.\*\*(.*?)\*\*Where Delta text lands\.\*\*", text, re.DOTALL)
    assert m, "no 'Hearing it.' bullet in docs/bot-install.md"
    return _flat(m.group(1))


def _plain_part() -> str:
    """The started-plain paragraph: from its head to the kept --hear-here rule."""
    bullet = _hearing_it()
    head = "**If this session started plain, repair first.**"
    assert head in bullet, bullet
    return bullet.split(head, 1)[1].split("`bot install --hear-here` lets this session", 1)[0]


def test_the_order_is_starting_plain_then_repair_then_a_new_session_then_the_stopgap():
    part = _plain_part()
    steps = [
        "(1) Look for session start's `starting plain:` line",
        f"(2) If the line is `{PLAIN} box <agent> is missing, ...; {REPAIR} <agent>`",
        "give the user that repair: `message setup --box <agent>`, or `sessionkit setup`",
        "Then tell them to start a new session with `session start`",
        ("(3) Only then offer `bot install --hear-here`, or a subscription "
         "(`message subscription add <agent>`), as a stopgap that ends with this session"),
    ]
    at = [part.find(s) for s in steps]
    assert -1 not in at, list(zip(steps, at))
    assert at == sorted(at), list(zip(steps, at))
    # the stopgap is named nowhere before the new session is
    assert "--hear-here" not in part[:at[3]]
    assert "subscription" not in part[:at[3]]


def test_it_says_where_the_line_is_and_that_this_session_stays_plain():
    part = _plain_part()
    assert "It is on the user's terminal, above this session" in part
    assert "`session start --dry-run` launches nothing and its `seat:` line" in part
    assert "This session stays plain; only a new one hears box `<agent>`" in part


def test_the_hear_here_rule_is_kept_and_follows_the_plain_part():
    bullet = _hearing_it()
    rule = ("`bot install --hear-here` lets this session hear it until the session ends; "
            "only run it when the user asks.")
    assert rule in bullet
    assert bullet.index("**If this session started plain, repair first.**") < bullet.index(rule)


def test_only_the_missing_box_line_carries_a_repair():
    """default_seat.py: held, LINUX, UNBOXED and UNSURE lines have no `repair:`."""
    part = _plain_part()
    assert part.count("; repair:") == 1, part          # the NO_BOX line, quoted once
    assert "Only the missing-box line carries a `repair:`" in part
    assert "so do not invent one: tell the user the line as it stands" in part
    # each other form is named for what it says, with no fix of its own
    for said in ("`held by ...; that one does`", "`macOS-only for now`", "`could not check`"):
        assert said in part, said
    after = part.split("so do not invent one", 1)[1]
    for fix in ("message setup", "sessionkit setup", "session start", "--hear-here"):
        assert fix not in after, fix

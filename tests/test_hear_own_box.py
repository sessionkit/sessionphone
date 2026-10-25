"""PHONE-246: with DC_MAILTO naming THIS session's own box, `bot install` must not say
this session will NOT hear it.

The live shape (measured read only, `message subscription list` in a Claude session):
sessionmail names the session on the HEAD line by its 8-hex SHORT id,
`<short-sid> (this session)`, and lists under it only the addresses that feed the
session (its seat, subscriptions). The session's own box is the FULL uuid
(`message path <uuid>` resolves it; `message path <short-sid>` has no box). The bot
mails DC_MAILTO, and its box is DC_MAILTO's local part, lowercased (seat.box_for).
That box reaches the session, and `pager status` says WAKE for it, yet the hear:
line said "this session will NOT hear" it: a false negative on the first-run path.

The fix: reach.parse keeps the head's short id, and Reach.hears(box) is also True
when BOX is the FULL session id the same shell carries (CLAUDE_CODE_SESSION_ID, then
CLAUDE_SESSION_ID) and its first 8 are the head's: never a bare prefix match.

Folded in (the default-seat ask): when the bot's name fell through to the default and
sessionmail says nobody hears its box (`message subscription list BOX`: no such box,
or `-- 0 hear it; no live listener on it`), install says so in one `default:` line.
Any other answer says nothing.
"""
import pytest
from test_first_seat import _install, rig  # noqa: F401 - rig is a fixture
from test_hear_measured import (  # noqa: F401 - asks is a fixture
    PLAIN,
    SUBSCRIBED,
    TERMINAL_ERR,
    _hear,
    asks,
)

from sessionphone import reach, seat

REAL_ASK = reach.ask          # taken at import: conftest stubs both in every test
REAL_UNOWNED = reach.unowned

SID = "11111111-2222-3333-4444-555555555555"
SHORT = SID[:8]
SEAT = "versa+inbox"   # a plus address: a seat, but never a durable one that names the bot

# `message subscription list` as a live sessionmail prints it for a Claude session:
# a session in a seat (its seat row, `your seat`; a plus address, so not durable) ...
LIVE_SEAT = (f"{SHORT} (this session)\n"
             f"└── {SEAT}   your seat: its row copies seat mail here (D12)\n"
             "-- 1 address reaches you, 1 level deep\n")
# ... and a plain `claude` (no seat, nothing subscribed)
LIVE_PLAIN = (f"{SHORT} (this session)\n"
              "-- 0 addresses reach you, 0 levels deep\n")

NOT_HEAR = "will NOT hear"


@pytest.mark.parametrize("answer", [LIVE_SEAT, LIVE_PLAIN], ids=["seat", "plain"])
@pytest.mark.parametrize("mailto", [f"{SID}@stranger", f"{SID.upper()}@stranger"],
                         ids=["uuid", "uuid-uppercase"])
def test_dc_mailto_naming_this_sessions_own_box_is_not_said_deaf(asks, monkeypatch, mailto, answer):  # noqa: F811
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("DC_MAILTO", mailto)
    asks.says(answer)
    asks.answer("path", 0, f"{asks.tmp}/mail/agents/{SID}\n")
    r = _install()
    assert r.exit_code == 0, r.output
    # the box the bot really derives: DC_MAILTO's local part, lowercased
    assert ["path", SID] in asks.argvs(), asks.argvs()
    assert f"box:       Delta text goes to {mailto}, box " in r.output
    hear = _hear(r.output)
    assert NOT_HEAR not in hear, hear
    assert NOT_HEAR not in r.output
    assert r.output.splitlines()[0].startswith("installed:"), r.output.splitlines()[0]
    assert hear == (f"this session hears box {SID} (its own session box): Delta messages and "
                    "calls reach it, while it runs"), hear


# ---- parse keeps the head's id; hears matches the full id, exactly ----------------

OTHER_SAME_PREFIX = f"{SHORT}-9999-8888-7777-666666666666"   # same 8 hex, another session
OTHER = "22222222-2222-3333-4444-555555555555"


def test_parse_keeps_the_heads_short_id_and_the_shells_full_id():
    r = reach.parse(0, LIVE_SEAT, "", SID.upper() + " ")
    assert r.short == SHORT and r.sid == SID and r.own == SID
    assert r.hears(SID) and r.hears(SID.upper()) and r.own_box(f" {SID} ")
    assert r.hears(SEAT)                       # the rows under the head, as before
    assert SID not in r.addresses                   # the own box is never an address row


@pytest.mark.parametrize("box", [OTHER_SAME_PREFIX, OTHER, SHORT, SID[:20], ""],
                         ids=["same-prefix", "other", "short-id", "part", "empty"])
def test_another_sessions_box_is_never_this_sessions_own(box):
    r = reach.parse(0, LIVE_PLAIN, "", SID)
    assert not r.hears(box) and not r.own_box(box)


@pytest.mark.parametrize("sid", ["", OTHER, OTHER_SAME_PREFIX[:8], "not-a-session-id-at-all-xxxxxxxxxxxx"],
                         ids=["absent", "another-session", "short-only", "not-a-uuid"])
def test_no_own_box_hear_without_the_shells_matching_full_id(sid):
    """No session id in the shell, or one sessionmail's head does not name: no own box."""
    r = reach.parse(0, LIVE_PLAIN, "", sid)
    assert r.own == "" and not r.hears(SID) and not r.hears(OTHER)


def test_a_seat_head_or_a_terminal_has_no_own_box():
    seat_only = reach.parse(0, "11111111 (seat)\n-- 0 addresses reach you, 0 levels deep\n", "", SID)
    assert seat_only.own == "" and not seat_only.hears(SID)
    terminal = reach.parse(2, "", TERMINAL_ERR, SID)
    assert terminal.own == "" and not terminal.hears(SID)


def test_shell_sid_reads_the_vars_sessionmail_reads_first_non_blank():
    assert reach.shell_sid({"CLAUDE_CODE_SESSION_ID": f" {SID.upper()} ", "CLAUDE_SESSION_ID": OTHER}) == SID
    assert reach.shell_sid({"CLAUDE_CODE_SESSION_ID": "  ", "CLAUDE_SESSION_ID": OTHER}) == OTHER
    assert reach.shell_sid({}) == ""


def test_ask_reads_the_session_id_from_the_shell_it_asked(tmp_path):
    fake = tmp_path / "bin" / "message"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nprintf "%s" "$OUT"\n')
    fake.chmod(0o755)
    shell = {"PATH": str(fake.parent), "OUT": LIVE_PLAIN}
    assert REAL_ASK({**shell, "CLAUDE_CODE_SESSION_ID": SID}).hears(SID)
    assert REAL_ASK({**shell, "CLAUDE_SESSION_ID": SID}).hears(SID)
    assert not REAL_ASK({**shell, "CLAUDE_CODE_SESSION_ID": OTHER_SAME_PREFIX}).hears(SID)
    assert not REAL_ASK(shell).hears(SID)


# ---- end to end: the negatives still say DEAF; --hear-here is not needed ----------

@pytest.mark.parametrize("box", [OTHER_SAME_PREFIX, OTHER], ids=["same-prefix", "other"])
def test_dc_mailto_naming_another_sessions_box_is_still_deaf(asks, monkeypatch, box):  # noqa: F811
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("DC_MAILTO", f"{box}@stranger")
    asks.says(LIVE_PLAIN)
    asks.answer("path", 0, f"{asks.tmp}/mail/agents/{box}\n")
    r = _install()
    assert r.exit_code == 0, r.output
    assert r.output.splitlines()[0].startswith(f"hear:      this session will NOT hear Delta messages "
                                               f"or calls: they go to box {box}"), r.output


def test_no_session_id_in_the_shell_is_no_own_box_hear(asks, monkeypatch):  # noqa: F811
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("DC_MAILTO", f"{SID}@stranger")
    asks.says(LIVE_PLAIN)
    asks.answer("path", 0, f"{asks.tmp}/mail/agents/{SID}\n")
    r = _install()
    assert r.exit_code == 0, r.output
    assert NOT_HEAR in r.output.splitlines()[0], r.output


def test_hear_here_is_not_needed_for_this_sessions_own_box(asks, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("DC_MAILTO", f"{SID}@stranger")
    asks.says(LIVE_PLAIN)
    asks.answer("path", 0, f"{asks.tmp}/mail/agents/{SID}\n")
    r = _install("--hear-here")
    assert r.exit_code == 0, r.output
    assert f"hear-here: not needed: this session already hears box {SID}" in r.output, r.output
    assert not [a for a in asks.argvs() if a[:2] == ["subscription", "add"]], asks.argvs()
    assert NOT_HEAR not in r.output


# ---- folded: the `default:` line when the default name's box has no owner --------

FALLTHROUGH = ("default:   no seat owns box claude: the bot's name came from the default, so nothing "
               "hears its mail yet. Start a bare `session start` first; it becomes claude.")
UNOWNED = "claude   seat box\n-- 0 hear it; no live listener on it\n"
NO_SUCH = ("message subscription list: no such box 'claude' under /scratch/mail/agents "
           "(`message box list` names them)\n")


@pytest.fixture
def boxes(asks, monkeypatch):  # noqa: F811
    """asks, with the real reach.unowned against the rig's fake sessionmail."""
    monkeypatch.setattr(reach, "unowned", REAL_UNOWNED)
    return asks


def _box_asks(rig) -> list[list[str]]:  # noqa: F811
    return [a for a in rig.argvs() if a[:2] == ["subscription", "list"] and len(a) > 2]


@pytest.mark.parametrize("rc,out,err", [(0, UNOWNED, ""), (2, "", NO_SUCH)], ids=["0-hear-it", "no-such-box"])
def test_a_default_name_whose_box_nobody_hears_says_so_in_one_line(boxes, rc, out, err):
    """A Terminal: the name falls through to claude, and sessionmail says nobody hears box claude."""
    boxes.says(rc=2, err=TERMINAL_ERR)
    boxes.answer("sub_list_box", rc, out, err)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _box_asks(boxes) == [["subscription", "list", "claude"]], boxes.argvs()
    lines = r.output.splitlines()
    assert lines.count(FALLTHROUGH) == 1, r.output
    assert lines[lines.index(FALLTHROUGH) - 1].startswith("hear:      To hear Delta messages"), r.output


def test_in_a_deaf_session_the_default_line_follows_the_warning_first(boxes, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    boxes.says(LIVE_PLAIN)
    boxes.answer("sub_list_box", 0, UNOWNED, "")
    r = _install()
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith("hear:      this session will NOT hear"), r.output
    assert lines[1] == FALLTHROUGH and lines[2].startswith("installed:"), r.output


@pytest.mark.parametrize("rc,out,err", [
    (0, ("claude   seat box\n└── 33333333   session in this seat: the seat row copies here (D12)\n"
         "-- 1 hears it; no live listener on it\n"), ""),
    (0, ("claude   seat box\n└── 33333333   LISTENER, not a subscription: `pager listen` (pid 7)\n"
         "-- 0 hear it; 1 live listener\n"), ""),
    (0, "claude   seat box\n-- 0 hear it; 2 more on 1 claimed tag; no live listener on it\n", ""),
    (0, "something else entirely\n", ""),
    (0, "", ""),
    (2, "", "Usage: message subscription list [BOX]\n"),
    (64, "", "unexpected: subscription list BOX\n"),
], ids=["a-seat-session", "a-listener", "claimed-tags", "unreadable", "empty", "usage", "failed"])
def test_any_answer_but_plainly_nobody_says_nothing(boxes, rc, out, err):
    boxes.says(rc=2, err=TERMINAL_ERR)
    boxes.answer("sub_list_box", rc, out, err)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _box_asks(boxes) == [["subscription", "list", "claude"]], boxes.argvs()
    assert "default:" not in r.output, r.output


def test_a_name_from_the_shell_never_asks_who_owns_the_box(boxes, monkeypatch):
    monkeypatch.setenv("DC_AGENT", "claude")
    boxes.says(rc=2, err=TERMINAL_ERR)
    boxes.answer("sub_list_box", 0, UNOWNED, "")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _box_asks(boxes) == [] and "default:" not in r.output, r.output


def test_a_session_that_hears_the_box_never_asks_who_owns_it(boxes, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    boxes.says(SUBSCRIBED)
    boxes.answer("sub_list_box", 0, UNOWNED, "")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _hear(r.output).startswith("this session hears box claude (by a subscription)"), r.output
    assert _box_asks(boxes) == [] and "default:" not in r.output, r.output


def test_dc_mailto_to_another_box_says_no_default_line(boxes, monkeypatch):
    """The default name, but the bot mails another box: a bare `session start` would not
    hear it, so the line would be a false fix. It is not said."""
    monkeypatch.setenv("DC_MAILTO", "elsewhere@stranger")
    boxes.says(rc=2, err=TERMINAL_ERR)
    boxes.answer("path", 0, f"{boxes.tmp}/mail/agents/elsewhere\n")
    boxes.answer("sub_list_box", 0, UNOWNED.replace("claude", "elsewhere"), "")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _box_asks(boxes) == [] and "default:" not in r.output, r.output


@pytest.mark.parametrize("rc,out,err,want", [
    (0, UNOWNED, "", True),
    (0, "claude   seat box\n-- 0  hear it;  no live listener on it  \n\n", "", True),
    (2, "", NO_SUCH, True),
    (2, "", "Usage: message subscription list [BOX]\n", False),
    (0, "claude   seat box\n-- 10 hear it; no live listener on it\n", "", False),
    (0, "-- 0 hear it; no live listener on it\nmore after the footer\n", "", False),
    (1, "", NO_SUCH, False),
])
def test_unowned_answer_reads_only_a_plain_nobody(rc, out, err, want):
    assert reach.unowned_answer(rc, out, err) is want


def test_unowned_asks_the_box_form_read_only(tmp_path):
    fake = tmp_path / "bin" / "message"
    fake.parent.mkdir()
    rec = tmp_path / "argv"
    fake.write_text(f'#!/bin/sh\necho "$*" > {rec}\nprintf "%s" "$OUT"\n')
    fake.chmod(0o755)
    assert REAL_UNOWNED(" Claude ", {"PATH": str(fake.parent), "OUT": UNOWNED})
    assert rec.read_text().strip() == "subscription list claude"
    assert not REAL_UNOWNED("claude", {"PATH": str(tmp_path / "none")})
    assert not REAL_UNOWNED("", {"PATH": str(fake.parent), "OUT": UNOWNED})


def test_fallthrough_needs_the_default_and_a_session_that_does_not_hear(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(reach, "unowned", lambda box, shell, env=None, exe=None: asked.append(box) or True)
    monkeypatch.setattr(seat.seatmail, "resolve_message", lambda shell: "/bin/message")
    deaf = reach.parse(0, PLAIN, "", SID)
    hears = reach.parse(0, SUBSCRIBED, "", SID)
    assert seat.fallthrough("claude", "the default", {}, deaf) == FALLTHROUGH.split(None, 1)[1]
    assert seat.fallthrough("claude", "the default (phone.json cannot be used)", {}, deaf)
    assert seat.fallthrough("claude", "DC_AGENT in this shell", {}, deaf) is None
    assert seat.fallthrough("claude", "the default", {}, hears) is None
    assert asked == ["claude", "claude"]
    monkeypatch.setattr(seat.seatmail, "resolve_message", lambda shell: None)
    assert seat.fallthrough("claude", "the default", {}, deaf) is None

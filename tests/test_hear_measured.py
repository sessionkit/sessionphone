"""Which box the bot mails, and whether the installing session hears it: measured.

`bot install` names the bot after what sessionmail says reaches the
installing session (`message subscription list`), never after a shell
variable alone: a durable seat (`session start ada`) names it; a plain
`claude` session (its session id only) and a task seat (a plus address, its
CLAUDE_CODE_AGENT a role) name none, so the recorded bot, else claude, is
used. The `hear:` line says whether THIS session hears the bot's box, as
sessionmail measures it: when it does not, it says so first, in plain words,
then the ways out. It warns, never refuses: the job is installed and the exit
is 0. `--hear-here` subscribes this session to the box, only when asked, and
says it ends with the session.

sessionmail here is test_first_seat's FAKE_MESSAGE, answering `subscription
list` with the text a real one prints for each kind of installer (measured
on a real sessionmail, read only, with synthetic environments).
"""
import os

import pytest
from click.testing import CliRunner
from test_first_seat import _install, _job_env, rig  # noqa: F401 - rig is a fixture

from sessionphone import reach, seat, settings
from sessionphone.cli import main as cli

REAL_ASK = reach.ask          # taken at import: conftest stubs it in every test
SID = "11111111-2222-3333-4444-555555555555"
TASK_SEAT = "proj+task-1.worker.abcd1234"

# `message subscription list`, as sessionmail prints it for each installer
PLAIN = ("11111111 (this session)\n"
         "-- 0 addresses reach you, 0 levels deep\n")
DURABLE = ("11111111 (this session)\n"
           "└── ada                 your seat: its row copies seat mail here (D12)\n"
           "-- 1 address reaches you, 1 level deep\n")
TASK = ("11111111 (this session)\n"
        f"└── {TASK_SEAT}   your seat: its row copies seat mail here (D12)\n"
        "-- 1 address reaches you, 1 level deep\n")
UNBORN = ("11111111 (this session)\n"
          "└── ada                 your seat: its row copies seat mail here (D12)\n"
          "-- 1 address reaches you, 1 level deep; this session has NO BOX yet, so none of "
          "this reaches it until `pager listen` births it\n")
SUBSCRIBED = ("11111111 (this session)\n"
              "└── claude                project box, session subscription 00:00 Mon\n"
              "-- 1 address reaches you, 1 level deep\n")
SEAT_ONLY = ("ada (seat)\n"
             "-- 0 addresses reach you, 0 levels deep\n")
TERMINAL_ERR = ("message subscription list: no session id and no seat in the environment "
                "(looked for CLAUDE_CODE_SESSION_ID, CLAUDE_SESSION_ID); name one with --for WHO\n")

DEAF_CLAUDE = "this session will NOT hear Delta messages or calls: they go to box claude"


@pytest.fixture
def asks(monkeypatch, rig):  # noqa: F811 - the fixture imported above
    """The real ask, against the rig's fake sessionmail; .says(text) sets its answer."""
    monkeypatch.setattr(reach, "ask", REAL_ASK)
    monkeypatch.setattr(seat, "_list_agents", lambda shell: {"claude", "ada"})

    def says(out: str = "", rc: int = 0, err: str = "", after: str | None = None):
        rig.answer("sub_list", rc, out, err)
        if after is not None:
            rig.answer("sub_list_after", 0, after, "")
        reach._ASKED.clear()
    def keyed(name: str):
        """The hand-made seat key must be the seat the bot sends as (<name>-chat),
        or install refuses before it writes; and the box sessionmail finds is NAME's."""
        pub = rig.tmp / "seat-keys" / f"{name}-chat" / "seat.pub"
        pub.parent.mkdir(parents=True, exist_ok=True)
        pub.write_text(f"ssh-ed25519 AAAAfake {name}-chat@stranger seat\n")
        monkeypatch.setenv("DC_SIGNER_PUB", str(pub))
        rig.answer("path", 0, f"{rig.tmp}/mail/agents/{name}\n")
    rig.says, rig.keyed = says, keyed
    return rig


def _hear(output: str) -> str:
    return next(ln for ln in output.splitlines() if ln.startswith("hear:")).split(None, 1)[1]


# ---- parse: the text sessionmail prints ---------------------------------------------

def test_parse_reads_each_installer_kind():
    plain = reach.parse(0, PLAIN, "")
    assert plain.answered and plain.session and not plain.seat and not plain.durable
    assert plain.addresses == frozenset()
    durable = reach.parse(0, DURABLE, "")
    assert durable.seat == durable.durable == "ada" and durable.hears("Ada")
    task = reach.parse(0, TASK, "")
    assert task.seat == TASK_SEAT and task.durable == "" and not task.hears("worker")
    terminal = reach.parse(2, "", TERMINAL_ERR)
    assert terminal.answered and not terminal.session and not terminal.hears("claude")
    seat_only = reach.parse(0, SEAT_ONLY, "")
    assert not seat_only.session and seat_only.durable == "ada" and not seat_only.hears("ada")
    assert reach.parse(0, UNBORN, "").boxed is False and reach.parse(0, DURABLE, "").boxed
    assert reach.parse(0, SUBSCRIBED, "").hears("claude")


@pytest.mark.parametrize("rc,out,err", [
    (2, "", "Usage: message [OPTIONS] COMMAND\nError: No such command 'subscription'.\n"),
    (64, "", "unexpected: subscription list\n"),
    (0, "something else entirely\n", ""),
    (0, "", ""),
])
def test_parse_an_answer_it_cannot_read_is_no_answer(rc, out, err):
    r = reach.parse(rc, out, err)
    assert not r.answered and r.said and not r.durable


@pytest.mark.parametrize("name", [SID, "11111111", "a+b", "ada@host", "-x", "née"])
def test_a_session_id_or_plus_address_is_never_a_durable_seat(name):
    assert reach.Reach(answered=True, session=True, seat=name).durable == ""


def test_ask_runs_message_subscription_list_read_only(tmp_path):
    fake = tmp_path / "bin" / "message"
    fake.parent.mkdir()
    rec = tmp_path / "argv"
    fake.write_text(f'#!/bin/sh\necho "$*" > {rec}\nprintf "%s" "$OUT"\n')
    fake.chmod(0o755)
    r = REAL_ASK({"PATH": str(fake.parent), "OUT": DURABLE})
    assert rec.read_text().strip() == "subscription list" and r.durable == "ada"
    assert REAL_ASK({"PATH": str(tmp_path / "none")}).said == "no `message` binary"


# ---- the name install gives the bot -------------------------------------------------

def test_a_durable_seat_names_the_bot_and_this_session_hears_it(asks, monkeypatch):
    """`session start ada`: the bot is ada, mails ada@, and the line says it is heard."""
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "ada")
    asks.says(DURABLE)
    asks.keyed("ada")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(asks)["DC_AGENT"] == "ada"
    assert ["setup", "--box", "ada"] in asks.argvs()
    assert _hear(r.output) == ("this session hears box ada (its seat ada): Delta messages "
                               "and calls reach it, while it runs")


def test_the_seat_names_the_bot_even_with_no_claude_code_agent(asks):
    asks.says(DURABLE)
    asks.keyed("ada")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(asks)["DC_AGENT"] == "ada"


def test_a_plain_claude_session_is_told_plainly_it_will_not_hear(asks):
    """A plain `claude` answers only to its session id: the bot is claude, and the
    line says FIRST that this session will not hear it, then the ways out."""
    asks.says(PLAIN)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(asks)["DC_AGENT"] == "claude"
    assert _hear(r.output) == (
        "this session will NOT hear Delta messages or calls: they go to box claude, and this "
        "session hears only its own session box. Ways out: start Claude with `session start "
        "claude` (only a session started as claude hears that box, today and in later "
        "sessions); or install again with DC_AGENT=NAME (the agent a session is started as) "
        "or DC_MAILTO=BOX (a box a session hears); or `sessionphone bot install "
        "--hear-here`, which lets this session hear box claude until this session ends.")


def test_a_task_seat_role_is_never_the_bots_name(asks, monkeypatch):
    """A task seat's CLAUDE_CODE_AGENT is a role, and its seat a plus address: neither
    names the bot, and the line never says this session hears it."""
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "worker")
    monkeypatch.setenv("EMAIL", f"{TASK_SEAT}@stranger")
    asks.says(TASK)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(asks)["DC_AGENT"] == "claude"
    hear = _hear(r.output)
    assert hear.startswith(DEAF_CLAUDE)
    assert f"this session hears only its own session box and its seat {TASK_SEAT}." in hear
    assert "this session is worker" not in r.output and "reach it" not in hear


def test_a_terminal_gets_the_advice_and_how_to_change_the_box(asks):
    asks.says("", 2, TERMINAL_ERR)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(asks)["DC_AGENT"] == "claude"
    hear = _hear(r.output)
    assert hear.startswith("To hear Delta messages, start Claude with: session start claude ")
    assert "To send Delta text to another box, install again with DC_AGENT=NAME" in hear
    assert "NOT hear" not in hear and "not measured" not in hear


def test_with_no_seat_the_recorded_bot_is_kept(asks, monkeypatch):
    """Today's order after the seat: the one bot phone.json records, then claude."""
    monkeypatch.setenv("DC_AGENT", "bob")
    asks.keyed("bob")
    assert _install().exit_code == 0
    monkeypatch.delenv("DC_AGENT")
    asks.says(PLAIN)
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(asks)["DC_AGENT"] == "bob"
    assert _hear(r.output).startswith(
        "this session will NOT hear Delta messages or calls: they go to box bob")


def test_dc_agent_wins_over_the_seat(asks, monkeypatch):
    monkeypatch.setenv("DC_AGENT", "bob")
    asks.says(DURABLE)
    asks.keyed("bob")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(asks)["DC_AGENT"] == "bob"
    assert _hear(r.output).startswith(
        "this session will NOT hear Delta messages or calls: they go to box bob")


def test_no_answer_from_sessionmail_keeps_the_old_name_and_says_nothing_was_measured(asks, monkeypatch):
    """A sessionmail that cannot list what reaches a session: CLAUDE_CODE_AGENT names
    the bot, as before, and the line never claims this session hears it."""
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "ada")
    asks.says("", 2, "Error: No such command 'subscription'.\n")
    asks.keyed("ada")
    r = _install()
    assert r.exit_code == 0, r.output
    assert _job_env(asks)["DC_AGENT"] == "ada"
    hear = _hear(r.output)
    assert "Whether this session hears it was not measured" in hear and "reach it" not in hear


def test_a_box_not_made_yet_is_said(asks, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "ada")
    asks.says(UNBORN)
    asks.keyed("ada")
    assert _hear(_install().output).endswith(
        "this session has no mail box yet, so nothing reaches it until its `pager listen` makes one")


def test_status_and_uninstall_pick_the_bot_as_install_does(asks, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "worker")
    monkeypatch.setenv("EMAIL", f"{TASK_SEAT}@stranger")
    asks.says(TASK)
    assert settings.bot_name(os.environ, "bot status", ask=True) == ("claude", "the default")
    asks.says(DURABLE)
    assert settings.bot_name(os.environ, "bot status", ask=True) == ("ada", settings.SEAT_FROM)
    r = CliRunner().invoke(cli, ["bot", "status"])
    assert "dev.sessionkit.phone.bot.ada" in r.output


def test_a_reader_that_asks_nothing_drops_a_task_seats_role():
    """paths.root() asks sessionmail nothing: a task seat's role still names no bot."""
    task = {"CLAUDE_CODE_AGENT": "worker", "EMAIL": f"{TASK_SEAT}@stranger"}
    assert settings.shell_name(task) is None
    assert settings.shell_name({"CLAUDE_CODE_AGENT": "Ada"}) == ("ada", "CLAUDE_CODE_AGENT in this shell")
    assert settings.shell_name({"CLAUDE_CODE_AGENT": "ada", "EMAIL": "someone@example.org"})[0] == "ada"


# ---- --hear-here: opt-in, and it says it ends with the session ----------------------

def test_hear_here_subscribes_this_session_and_says_it_ends(asks):
    asks.says(PLAIN, after=SUBSCRIBED)
    r = _install("--hear-here")
    assert r.exit_code == 0, r.output
    assert [a for a in asks.argvs() if a[:2] == ["subscription", "add"]] == [["subscription", "add", "claude"]]
    lines = r.output.splitlines()
    here = next(ln for ln in lines if ln.startswith("hear-here:"))
    assert here == ("hear-here: this session now hears box claude (`message subscription add "
                    "claude`); it ends with this session: a later session does not hear the box "
                    "unless it is started with `session start claude`, or runs --hear-here again")
    assert lines.index(here) < lines.index(next(ln for ln in lines if ln.startswith("hear:")))
    assert _hear(r.output) == ("this session hears box claude (by a subscription): Delta "
                               "messages and calls reach it, while it runs")


def test_hear_here_is_never_the_default(asks):
    asks.says(PLAIN)
    r = _install()
    assert r.exit_code == 0, r.output
    assert not [a for a in asks.argvs() if a[:2] == ["subscription", "add"]]
    assert "hear-here:" not in r.output


def test_hear_here_from_a_terminal_does_nothing_and_says_why(asks):
    asks.says("", 2, TERMINAL_ERR)
    r = _install("--hear-here")
    assert r.exit_code == 0, r.output
    assert not [a for a in asks.argvs() if a[:2] == ["subscription", "add"]]
    assert ("hear-here: not done: this shell is not a Claude session, so there is no session "
            "to subscribe to box claude") in r.output


def test_hear_here_refused_by_sessionmail_warns_and_exits_0(asks):
    asks.says(PLAIN)
    asks.answer("sub_add", 2, "", "message subscription add: refused: no such box claude\n")
    r = _install("--hear-here")
    assert r.exit_code == 0, r.output
    assert ("hear-here: not done: `message subscription add claude` said: message subscription "
            "add: refused: no such box claude (exit 2)") in r.output
    assert _hear(r.output).startswith(DEAF_CLAUDE)


def test_hear_here_when_already_heard_adds_nothing(asks, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "ada")
    asks.says(DURABLE)
    asks.keyed("ada")
    r = _install("--hear-here")
    assert "hear-here: not needed: this session already hears box ada" in r.output
    assert not [a for a in asks.argvs() if a[:2] == ["subscription", "add"]]


def test_hear_here_help_says_it_ends_with_this_session():
    r = CliRunner().invoke(cli, ["bot", "install", "--help"])
    assert r.exit_code == 0
    words = " ".join(r.output.split())
    assert "--hear-here" in words and "It ends with this session" in words
    assert "Never done unless asked" in words


# ---- the order: a session that will NOT hear is told FIRST (PHONE-214) -------------
# The super's PHONE-209 ruling: the DEAF warning and its ways out are install's
# first line, ahead of `installed:`. A line that confirms it hears, and a
# Terminal's advice (no session to warn), stay where they were, under `box:`.

def _at(lines: list[str], tag: str) -> int:
    return lines.index(next(ln for ln in lines if ln.startswith(tag)))


def test_order_plain_claude_the_warning_is_the_first_line(asks):
    asks.says(PLAIN)
    r = _install()
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith(f"hear:      {DEAF_CLAUDE}")
    assert "Ways out:" in lines[0]
    assert _at(lines, "hear:") < _at(lines, "installed:") < _at(lines, "mail:") < _at(lines, "box:")
    assert len([ln for ln in lines if ln.startswith("hear:")]) == 1


def test_order_a_task_seat_the_warning_is_the_first_line(asks, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "worker")
    monkeypatch.setenv("EMAIL", f"{TASK_SEAT}@stranger")
    asks.says(TASK)
    r = _install()
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith(f"hear:      {DEAF_CLAUDE}")
    assert _at(lines, "hear:") < _at(lines, "installed:")


def test_order_a_durable_seat_that_hears_keeps_its_line_under_box(asks, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "ada")
    asks.says(DURABLE)
    asks.keyed("ada")
    r = _install()
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith("installed:")
    assert _at(lines, "mail:") < _at(lines, "box:") < _at(lines, "hear:")
    assert lines[_at(lines, "box:") + 1] == ("hear:      this session hears box ada (its seat ada): "
                                             "Delta messages and calls reach it, while it runs")


def test_order_a_terminal_keeps_its_advice_under_box(asks):
    asks.says("", 2, TERMINAL_ERR)
    r = _install()
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith("installed:")
    assert _at(lines, "mail:") < _at(lines, "box:") < _at(lines, "hear:")
    assert lines[_at(lines, "hear:")].startswith("hear:      To hear Delta messages")


def test_order_hear_here_refused_the_warning_leads_then_why(asks):
    """--hear-here did not take: the session still will not hear, so the warning
    leads, and the hear-here: line saying why follows it."""
    asks.says(PLAIN)
    asks.answer("sub_add", 2, "", "message subscription add: refused: no such box claude\n")
    r = _install("--hear-here")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith(f"hear:      {DEAF_CLAUDE}")
    assert lines[1].startswith("hear-here: not done:")
    assert _at(lines, "hear-here:") < _at(lines, "installed:")


def test_order_hear_here_that_took_stays_under_box(asks):
    asks.says(PLAIN, after=SUBSCRIBED)
    r = _install("--hear-here")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith("installed:")
    assert _at(lines, "box:") < _at(lines, "hear-here:") < _at(lines, "hear:")


# ---- the bot's start line: advice, it cannot measure the installer ------------------

def test_the_bots_start_words_name_the_box_and_how_to_change_it():
    words = seat.hear_words("ada")
    assert words.startswith("To hear Delta messages, start Claude with: session start ada ")
    assert words.endswith("To send Delta text to another box, install again with DC_AGENT=NAME "
                          "(the agent a session is started as) or DC_MAILTO=BOX (a box a "
                          "session hears).")

"""PHONE-106 part 2a, PHONE-72 part 2b: every agent-bound mail goes by `message send`,
as the bot's seat, signed; the call mails (M5) too, by `--message-id`.

design/agent-contract.md §2.3, §3, §11; PHONE-72's design.md §2, §4. Fakes only:
a fake `message` (conftest fake_message) that records its argv and environment,
no real sendmail, message, seat key or bot.
"""
import os
import pathlib
import re
import subprocess
import types

import pytest

from sessionphone.bridge import bot, dc_env, seatmail

REPO = pathlib.Path(__file__).resolve().parents[1]
SEAT = "ada-chat"


@pytest.fixture
def seat(monkeypatch, seat_key):
    """The bot is seat ada-chat, and its key (DC_SIGNER_PUB) names that seat."""
    monkeypatch.setattr(dc_env, "ADDR", f"{SEAT}@example.net")
    monkeypatch.setattr(dc_env, "AGENT_NAME", "Ada")
    monkeypatch.setattr(dc_env, "MAILTO", "ada@example.net")
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setattr(bot, "log", lambda m: None)
    seat_key(f"{SEAT} seat")


@pytest.fixture
def fake_message(fake_message, seat):
    """conftest's fake `message` (argv, env and stdin of each send; --json's yes), as seat ada-chat."""
    return fake_message


# ---- the child's argv and environment (contract §2.3 table) ------------------

def test_a_chat_line_goes_by_message_send_as_the_seat_signed(fake_message, monkeypatch):
    # what a caller's shell might carry: a Claude session's key and id, an
    # inherited EMAIL, and a must-sign turned off. None of it may reach the send.
    monkeypatch.setenv("CLAUDE_SESSION_AGENT_SOCK", "/tmp/a-session-agent.sock")
    monkeypatch.setenv("CLAUDE_SESSION_PUB", "/tmp/a-session.pub")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "9cbba124-0000")
    monkeypatch.setenv("EMAIL", "bob@example.org")
    monkeypatch.setenv("TM_AGENT_ADDRESS", "sessionphone+phone-106.worker")
    monkeypatch.setenv("SESSIONMAIL_REQUIRE_SIGNATURE", "0")
    monkeypatch.setenv("SESSION_MAIL_REQUIRE_SIGNATURE", "off")

    assert bot.mail_in("Team", 12, "Bob", "hello there") is True

    argv = fake_message.argv()
    assert argv[:2] == ["send", "ada@example.net"]
    assert argv[argv.index("-s") + 1] == "[Team 12] Bob: hello there"
    assert argv[argv.index("-f") + 1] == "-"
    assert {"--raw", "--no-shorten", "--json"} <= set(argv)
    heads = [argv[i + 1] for i, a in enumerate(argv) if a == "--header"]
    assert heads == ["X-Delta-Chat-Bridge: 1", "X-Delta-Chat-Id: 12"]
    assert "--from" not in argv and not any(h.lower().startswith("message-id") for h in heads)

    env = fake_message.env()
    signer = pathlib.Path(os.environ["DC_SIGNER_PUB"])          # the tripwire's fake signer
    assert env["CLAUDE_SESSION_AGENT_SOCK"] == os.environ["DC_SIGNER_SOCK"]
    assert env["CLAUDE_SESSION_PUB"] == str(signer)
    assert not [k for k in env if k.startswith("CLAUDE_SESSION_")
                and k not in ("CLAUDE_SESSION_AGENT_SOCK", "CLAUDE_SESSION_PUB")]
    assert env["TM_AGENT_ADDRESS"] == env["EMAIL"] == SEAT
    assert env["SESSIONMAIL_FROM_NAME"] == "Ada's Delta Chat bridge"
    assert env["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1"
    assert "SESSION_MAIL_REQUIRE_SIGNATURE" not in env

    body = fake_message.body()
    assert body.startswith("hello there\n\n---\nchat: Team (id 12)\nfrom: Bob\n")


def test_the_profile_header_and_a_profile_room(fake_message, monkeypatch):
    monkeypatch.setattr(bot, "PROFILE", "public")
    assert bot.mail_in("Club", 7, "Jo", "hi") is True
    argv = fake_message.argv()
    assert "X-Delta-Chat-Profile: public" in argv
    assert argv[argv.index("-s") + 1] == "[public Club 7] Jo: hi"


def test_the_seat_is_the_local_part_of_dc_addr_whatever_the_host(monkeypatch):
    monkeypatch.setattr(dc_env, "ADDR", "carol-chat@1.0.0.ip6.arpa")
    env = seatmail.child_env({"PATH": "/usr/bin", "EMAIL": "x@y"})
    assert env["EMAIL"] == env["TM_AGENT_ADDRESS"] == "carol-chat"


def test_child_env_without_a_signer_sets_no_key(monkeypatch):
    """No DC_SIGNER_*: the child gets no key at all, and must-sign still on, so it refuses."""
    env = seatmail.child_env({"PATH": "/usr/bin", "CLAUDE_SESSION_AGENT_SOCK": "/s"})
    assert "CLAUDE_SESSION_AGENT_SOCK" not in env and "CLAUDE_SESSION_PUB" not in env
    assert env["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1"


def test_pip_chat_spellings_name_the_signer_too():
    env = seatmail.child_env({"PIP_CHAT_SIGNER_SOCK": "/s.sock", "PIP_CHAT_SIGNER_PUB": "~/seat.pub"})
    assert env["CLAUDE_SESSION_AGENT_SOCK"] == "/s.sock"
    assert env["CLAUDE_SESSION_PUB"] == os.path.expanduser("~/seat.pub")


# ---- mail_in returns whether the mail went; a refusal is never "bridged" -----

@pytest.mark.parametrize("rc,out,want,err", [
    (0, '{"sent": "yes", "id": "<1@h>", "notices": []}', True, ""),
    (2, '{"sent": "no", "kind": "identity", "message": "cannot be signed - NOTHING WAS SENT"}',
     False, "rc=2 kind=identity: cannot be signed - NOTHING WAS SENT"),
    (3, '{"sent": "unknown", "kind": "mta", "message": "sendmail exited 75"}',
     False, "rc=3 kind=mta sent=unknown: sendmail exited 75"),
    (1, "", False, "rc=1: usage"),
])
def test_mail_in_returns_whether_it_went(seat, monkeypatch, rc, out, want, err):
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    monkeypatch.setattr(bot.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=out, stderr="usage\n"))
    assert bot.mail_in("Team", 12, "Bob", "hi") is want
    if want:
        assert logged == ["bridged to mail: [Team 12] Bob: hi <1@h>"]
    else:                                   # never "bridged"; kept by the spool (do-003)
        assert logged[0].startswith(f"BRIDGE MAIL FAILED {err}, kept at ")
        assert not any(m.startswith("bridged") for m in logged)


def test_a_message_that_cannot_run_is_a_failed_send_of_kind_exec(seat, monkeypatch, tmp_path):
    """do-003: no longer raised to the caller; it is a failure the spool retries."""
    monkeypatch.setattr(seatmail, "MESSAGE", str(tmp_path / "gone"))
    assert bot.mail_in("Team", 12, "Bob", "hi") is False
    sent = seatmail.send("a@b", "s", "body")
    assert not sent.ok and sent.kind == "exec" and "FileNotFoundError" in sent.error


def test_no_message_binary_at_send_time_is_a_failed_send(seat, monkeypatch, tmp_path):
    monkeypatch.setenv("DC_MESSAGE", str(tmp_path / "nope"))
    assert bot.mail_in("Team", 12, "Bob", "hi") is False


# ---- the subject never trips message send's guard (contract §3) --------------

@pytest.mark.parametrize("text", [
    "run `rm -rf /` now", "echo $(whoami)", "`$(x)`", "a" * 30 + " $(" + "b" * 40, "$($($(",
])
def test_a_backtick_or_dollar_paren_never_makes_the_subject_refused(seat, monkeypatch, text):
    seen = []
    monkeypatch.setattr(bot.subprocess, "run", lambda argv, **k: seen.append(argv) or types.SimpleNamespace(
        returncode=0, stdout='{"sent": "yes", "id": "<1@h>"}', stderr=""))
    bot.mail_in("Team `x`", 12, "M$(e)", text)
    subject = seen[0][seen[0].index("-s") + 1]
    assert "`" not in subject and "$(" not in subject
    assert len(subject) <= 69 and subject.isascii()


def test_clean_subject():
    assert seatmail.clean_subject("a `b` $(c) $$(d)") == "a 'b' $ (c) $$ (d)"


def test_the_cut_comes_after_the_clean():
    """`$(` grows by one; a subject cut first, cleaned after, could pass 69."""
    long = "[Team 12] " + "x" * 58 + "$("
    assert len(long) == 70 and len(seatmail.clean_subject(long)[:69]) == 69


# ---- call mails (M5): message send --message-id, signed (PHONE-72 part 2b) --

@pytest.mark.parametrize("kw,flags", [
    ({"msgid": "call-12-1@host"}, ["--message-id", "call-12-1@host"]),
    ({"in_reply_to": "call-12-1@host"}, ["-r", "<call-12-1@host>"]),
    ({"msgid": "turn-1@host", "in_reply_to": "call-12-1@host", "supersedes": "prev-1@host"},
     ["--message-id", "turn-1@host", "-r", "<call-12-1@host>", "--supersedes", "<prev-1@host>"]),
])
def test_a_mail_on_a_bot_minted_id_goes_by_message_send_signed(fake_message, monkeypatch, kw, flags):
    monkeypatch.setenv("SESSIONMAIL_HOST", "host")
    assert bot.mail_in("Team", 12, "call", "RINGING", cc=["g@relay.example"], reply_to="r@x", **kw) is True
    (call,) = fake_message.calls()
    argv, env = call["argv"], call["env"]
    i = argv.index(flags[0])
    assert argv[i:i + len(flags)] == flags and argv[argv.index("-c") + 1] == "g@relay.example"
    heads = [argv[i + 1] for i, a in enumerate(argv) if a == "--header"]
    assert "Reply-To: r@x" in heads and "X-Delta-Chat-Kind: call" in heads
    owned = ("message-id", "in-reply-to", "references", "cc", "supersedes")
    assert not [h for h in heads if h.split(":")[0].lower() in owned]     # message send's own (sm _send.OWNED)
    assert env["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1" and env["EMAIL"] == env["TM_AGENT_ADDRESS"] == SEAT
    assert env["SESSIONMAIL_HOST"] == seatmail.id_host() == "host"
    assert env["SESSIONMAIL_FROM_NAME"] == "Ada's Delta Chat bridge"


@pytest.mark.parametrize("reply", [
    (2, {"sent": "no", "kind": "identity", "message": "cannot be signed"}),
    (3, {"sent": "unknown", "kind": "mta", "message": "sendmail exited 75"}),
    (2, {"sent": "no", "kind": "message-id", "message": "use the bare host"}),
])
def test_an_m5_refusal_is_never_spooled(fake_message, monkeypatch, undelivered_in_tmp, reply):
    """sessionmail has no duplicate check: a retried M5 could land twice under one id.
    So a refused M5 is logged and dropped, never kept, whatever its kind."""
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    fake_message.answer(reply)
    why = []
    assert bot.mail_in("Team", 12, "call", "x", msgid="call-turn-12-1@host", refusal=why) is False
    assert not [p for p in undelivered_in_tmp.rglob("*") if p.is_file()]
    assert bot.SPOOL.counts() == (0, 0)
    (line,) = logged
    assert line.startswith(f"BRIDGE MAIL FAILED rc={reply[0]} kind={reply[1]['kind']}")
    assert line.endswith("; a call mail, not kept") and why == [line[len("BRIDGE MAIL FAILED "):-len("; a call mail, not kept")]]


def test_an_m1_refusal_is_still_spooled(fake_message, undelivered_in_tmp):
    """The contrast: a mail with no bot-minted id is kept and retried (contract §2.4)."""
    fake_message.answer((2, {"sent": "no", "kind": "identity", "message": "cannot be signed"}))
    assert bot.mail_in("Team", 12, "Bob", "hi") is False
    assert bot.SPOOL.counts() == (1, 0)


def test_the_delta_call_path_makes_one_try(fake_message):
    """A world kind that the web path would retry: the Delta path tries ONCE (design §2(b))."""
    fake_message.answer(*[(3, {"sent": "no", "kind": "mta", "message": "no MTA"})] * 3)
    assert bot.mail_in("Team", 12, "call", "x", msgid="call-turn-12-1@host", in_reply_to="call-12-1@host") is False
    assert len(fake_message.calls()) == 1 and bot.SPOOL.counts() == (0, 0)   # nothing left to retry it


def _src_grep(pattern, root=REPO):
    """`git grep -n -E pattern -- src`, without git (PHONE-170).

    A `git archive` export has no .git, so the guards below walk root/src
    themselves: `path:lineno:text` lines, path relative to root (posix), sorted.
    Build artefacts (__pycache__, *.pyc) and binary files are skipped, as git
    grep skips binaries, so a stray artefact cannot change the result."""
    rx = re.compile(pattern)
    hits = []
    for path in sorted((root / "src").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        rel = path.relative_to(root).as_posix()
        for n, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if rx.search(text):
                hits.append(f"{rel}:{n}:{text}")
    return hits


def test_src_grep_needs_no_git(tmp_path, monkeypatch):
    """PHONE-170: the guards must run in a `git archive` tree, which has no .git.
    The old helper shelled out to `git grep` and git exited 128 there."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"_src_grep ran {a}"))
    pkg = tmp_path / "src" / "pkg"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "b.py").write_text("x = 1\nsendmail(x)\n")
    (pkg / "a.py").write_text("sendmail(1)\n")
    (pkg / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"sendmail(")
    (pkg / "logo.png").write_bytes(b"\x89PNG\0sendmail(")
    (tmp_path / "outside.py").write_text("sendmail(2)\n")       # not under src: not searched
    assert not (tmp_path / ".git").exists()
    assert _src_grep(r"sendmail\(", root=tmp_path) == ["src/pkg/a.py:1:sendmail(1)",
                                                        "src/pkg/b.py:2:sendmail(x)"]
    assert _src_grep(r"nothing-matches-this", root=tmp_path) == []


# PHONE-179: the bot NAMES its carrier (seatmail.carrier), so src may say
# sessionmail's setting, SESSIONMAIL_SENDMAIL, and hold sessionmail's default MTA
# path on ONE line, as words for the start line. Neither is ever run.
CARRIER_LINE = 'src/sessionphone/bridge/seatmail.py:DEFAULT_MTA = "/usr/sbin/sendmail"'


def test_zero_raw_sendmail_is_left_in_src():
    """PHONE-72 part 2b: no raw sendmail path is left. The exit was SM-716."""
    hits = _src_grep(r"sendmail\(|send_m5\(|/usr/sbin/sendmail|(?<!SESSIONMAIL_)SENDMAIL")
    assert [re.sub(r":\d+:", ":", h, count=1) for h in hits] == [CARRIER_LINE]
    assert not (REPO / "src/sessionphone/bridge/sendmail.py").exists()
    assert not hasattr(bot, "send_mail") and not hasattr(bot, "send_m5") and not hasattr(bot, "M5")


def test_every_message_send_in_src_is_seatmails():
    """One sender: nothing in src runs `message send` but seatmail (send_args and the
    probe). calls.py runs `message verify` twice, which sends nothing: the signature
    check (PHONE-155) and `verify --trust --require-blessing`, the registry rung
    (PHONE-156)."""
    runs = [line for line in _src_grep(r"subprocess\.run\(\[exe") if "seatmail.py" not in line]
    assert [line.split(":")[0] for line in runs] == ["src/sessionphone/bridge/calls.py"] * 2
    assert all('[exe, "verify",' in line for line in runs), runs


# ---- the id host (design §2(a)) ---------------------------------------------

@pytest.mark.parametrize("environ,want", [
    ({"SESSIONMAIL_HOST": "host", "SESSION_MAIL_HOST": "old"}, "host"),
    ({"SESSIONMAIL_HOST": "", "SESSION_MAIL_HOST": "old"}, "old"),     # empty counts as unset, as sessionmail's
    ({"SESSION_MAIL_HOST": ""}, "box"),
    ({}, "box"),
])
def test_id_host_is_sessionmails_own_host(monkeypatch, environ, want):
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "box.tail1234.ts.net")
    assert seatmail.id_host(environ) == want


def test_id_host_is_not_dc_host(monkeypatch):
    """DC_HOST still builds the addresses and the web Reply-To; the ids never take it."""
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "box.local")
    assert seatmail.id_host({"DC_HOST": "mac.example.net"}) == "box"


def test_child_env_pins_the_id_host_and_takes_a_seat_and_a_name(seat):
    env = seatmail.child_env({"SESSION_MAIL_HOST": "host", "PATH": "/usr/bin"}, seat="alice-chat@x.y",
                             from_name="Alice's web call")
    assert env["SESSIONMAIL_HOST"] == "host"
    assert env["EMAIL"] == env["TM_AGENT_ADDRESS"] == "alice-chat"
    assert env["SESSIONMAIL_FROM_NAME"] == "Alice's web call"
    assert seatmail.child_env({})["EMAIL"] == SEAT                 # the defaults are the bot's


def test_args_takes_an_id_bare_or_wrapped():
    a = seatmail.args("t@x", "s", msgid="<m@h>", in_reply_to="r@h", supersedes="<p@h>")
    assert a[a.index("--message-id") + 1] == "m@h"
    assert a[a.index("-r") + 1] == "<r@h>" and a[a.index("--supersedes") + 1] == "<p@h>"
    assert "--message-id" not in seatmail.args("t@x", "s")


def test_the_call_ids_are_minted_at_the_id_host_not_dc_host():
    src = (REPO / "src/sessionphone/bridge/calls.py").read_text()
    assert "@{dc_env.HOST}" not in src
    assert src.count("@{seatmail.id_host()}") == 3                 # the root, the turn, the preview
    web = (REPO / "src/sessionphone/web/turns.py").read_text()
    assert "ids = seatmail.id_host()" in web


# ---- the start probe (design §2(c), §4) -----------------------------------------

def test_the_probe_is_exactly_this_and_its_comment_stands_beside_it():
    """If a later sessionmail refuses the probe for any other kind (an address check
    on probe@invalid, say), every start refuses: the bad id must stay the ONLY bad input."""
    assert seatmail.PROBE_ARGS == ["send", "probe@invalid", "-s", "probe", "--no-body",
                                   "--message-id", "<x>", "--json"]
    src = (REPO / "src/sessionphone/bridge/seatmail.py").read_text()
    at = src.index("PROBE_ARGS = [")
    assert "THE BAD ID MUST STAY THE ONLY INVALID INPUT" in src[at - 700:at]


def test_the_probe_accepts_sessionmail_with_sm_716(fake_message):
    assert seatmail.probe() is None and fake_message.probes() == 1
    assert fake_message.calls() == []                               # it sent nothing


OLD = (2, "", ("Usage: message send [OPTIONS] TO...\nTry 'message send -h' for help.\n\n"
               "Error: No such option: --message-id\n"))


@pytest.mark.parametrize("rc,out,err,said", [
    (*OLD, "exit 2 (Usage: message send"),                                       # an old message: no JSON
    (2, '{"sent": "no", "kind": "address", "message": "probe@invalid"}', "", "exit 2, kind address, sent no"),
    (0, '{"sent": "yes", "id": "<x@h>"}', "", "exit 0, sent yes"),
    (3, '{"sent": "unknown", "kind": "message-id"}', "", "exit 3, kind message-id, sent unknown"),
    (2, '{"sent": "yes", "kind": "message-id"}', "", "exit 2, kind message-id, sent yes"),
    (1, "", "", "exit 1 ()"),
])
def test_anything_else_refuses_naming_the_binary(fake_message, rc, out, err, said):
    fake_message.probe(rc, out, err)
    why = seatmail.probe()
    assert why.startswith(f"{fake_message.exe} answered the --message-id probe with {said}")
    assert "needs sessionmail with `message send --message-id`" in why and "15f6230" in why and "never falls back" in why


def test_a_probe_that_hangs_refuses(fake_message, monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], seatmail.PROBE_TIMEOUT_S)
    monkeypatch.setattr(seatmail.subprocess, "run", hang)
    assert "did not answer the --message-id probe" in seatmail.probe()


def test_the_probe_runs_as_the_seat_under_must_sign(fake_message, monkeypatch, tmp_path):
    """It runs under child_env: an older message with a sign gate refuses the same way."""
    seen = []
    real = seatmail.subprocess.run
    monkeypatch.setattr(seatmail.subprocess, "run", lambda argv, **k: seen.append(k["env"]) or real(argv, **k))
    assert seatmail.probe(seat="web-chat") is None
    assert seen[0]["EMAIL"] == "web-chat" and seen[0]["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1"


def test_main_refuses_an_old_message(start, fake_message, capsys):
    fake_message.probe(*OLD)
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith(f"sessionphone bot: {fake_message.exe} answered the --message-id probe with exit 2")
    assert "needs sessionmail with `message send --message-id`" in err and not bot.OUTBOX.exists()


# ---- the key's seat (design §4 (b')) ------------------------------------------------

@pytest.mark.parametrize("comment", [f"{SEAT} seat", f"{SEAT}@example.net seat", f"{SEAT}@other.host seat"])
def test_the_keys_seat_matches_bare_or_full(fake_message, seat_key, comment):
    seat_key(comment)
    assert seatmail.check() is None


@pytest.mark.parametrize("comment", ["", SEAT, f"{SEAT} key", f"{SEAT} seat extra", "seat", "@host seat",
                                     f"seat {SEAT}"])
def test_a_key_comment_of_any_other_form_refuses(fake_message, seat_key, comment):
    pub = seat_key(comment)
    why = seatmail.check()
    assert why.startswith(f"DC_SIGNER_PUB={pub} has the comment {comment.strip()!r}, not '<address> seat'")
    assert fake_message.probes() == 0                       # refused before the probe


def test_another_seats_key_refuses_naming_both_seats(fake_message, seat_key):
    pub = seat_key("carol-chat@example.net seat")
    why = seatmail.check()
    assert why.startswith(f"the signing key is seat carol-chat (DC_SIGNER_PUB={pub}), but the mail goes "
                          f"From seat {SEAT} (DC_ADDR)")
    assert "never sent" in why


def test_main_refuses_another_seats_key(start, fake_message, seat_key, capsys):
    seat_key("carol-chat seat")
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "seat carol-chat" in err and f"From seat {SEAT}" in err


def test_the_web_door_names_its_own_seat(fake_message, seat_key):
    seat_key(f"{SEAT} seat")
    why = seatmail.check(seat="alice-chat", door="the web seat")
    assert f"the signing key is seat {SEAT}" in why and "From seat alice-chat (the web seat)" in why
    seat_key("alice-chat@example.net seat")
    assert seatmail.check(seat="alice-chat@example.net", door="the web seat") is None


# ---- the bot refuses to start with no message or no signer -------------------

@pytest.fixture
def start(monkeypatch, tmp_path, seat):
    """bot.main() up to its start-up checks; past them, the Rpc raises Reached."""
    class Reached(Exception):
        pass

    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot, "LOG", tmp_path / "bot.log")
    monkeypatch.setattr(bot, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(bot, "relink", lambda: None)
    monkeypatch.setattr(bot, "server_choice", lambda: ("/x", "venv", "0"))

    def rpc(**k):
        raise Reached
    monkeypatch.setattr(bot, "Rpc", rpc)
    return Reached


@pytest.mark.parametrize("unset,why", [
    (("DC_SIGNER_SOCK",), "no signer: DC_SIGNER_SOCK is not set"),
    (("DC_SIGNER_PUB",), "no signer: DC_SIGNER_PUB is not set"),
    (("DC_SIGNER_SOCK", "DC_SIGNER_PUB"), "no signer: DC_SIGNER_SOCK and DC_SIGNER_PUB are not set"),
])
def test_main_refuses_with_no_signer(start, monkeypatch, capsys, unset, why):
    for k in unset:
        monkeypatch.delenv(k)
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith(f"sessionphone bot: {why}") and "never sends its agent unsigned" in err
    assert f"message seat init --registry DIR {SEAT}" in err and "message seat agent" in err
    assert "Give the bot a key with `sessionphone bot install --registry DIR`" in err   # the one command
    assert err.index("bot install --registry DIR") < err.index("Or by hand")           # it comes first
    assert "the README's \"The bot's seat\" says how to make one" in err     # how to pick DIR
    assert not (bot.OUTBOX).exists()                         # refused before anything was made


def test_main_refuses_with_a_signer_pub_that_is_not_a_file(start, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DC_SIGNER_PUB", str(tmp_path / "missing.pub"))
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    assert f"DC_SIGNER_PUB={tmp_path / 'missing.pub'} is not a file" in capsys.readouterr().err


@pytest.mark.parametrize("how", ["missing", "not-executable"])
def test_main_refuses_with_no_message_binary(start, monkeypatch, capsys, tmp_path, how):
    exe = tmp_path / "message"
    if how == "not-executable":
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o644)
    monkeypatch.setenv("DC_MESSAGE", str(exe))
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert f"sessionphone bot: DC_MESSAGE={exe} is not an executable" in err


def test_main_refuses_with_no_message_on_path(start, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("DC_MESSAGE")
    monkeypatch.setenv("PATH", str(tmp_path))                # launchd's short PATH, and no message on it
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    assert "no `message` on PATH and DC_MESSAGE is not set" in capsys.readouterr().err


def test_main_starts_and_resolves_message_to_an_absolute_path(start, fake_message, monkeypatch):
    monkeypatch.setenv("DC_MESSAGE", "message")              # a bare name, found on PATH at start
    monkeypatch.setenv("PATH", f"{fake_message.exe.parent}{os.pathsep}{os.environ['PATH']}")
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    with pytest.raises(start):
        bot.main()
    assert seatmail.MESSAGE == str(fake_message.exe) and os.path.isabs(seatmail.MESSAGE)
    assert any(m.startswith(f"agent mail: {fake_message.exe} send as seat {SEAT}, signed (carrier "
                            "/usr/sbin/sendmail is an MTA, from sessionmail's default), call mails too")
               for m in logged)


@pytest.mark.parametrize("sendmail, carrier", [
    ("drop", "carrier drop (SESSIONMAIL_SENDMAIL=drop, no MTA)"),
    ("/opt/mta/sendmail", "carrier MTA /opt/mta/sendmail (SESSIONMAIL_SENDMAIL)"),
    (None, "carrier MTA /usr/sbin/sendmail (sessionmail's default)"),   # conftest.suite_carrier's
], ids=["drop", "path", "unset"])
def test_the_start_line_names_the_mail_carrier(start, fake_message, monkeypatch, sendmail, carrier):
    """PHONE-179: a deployed bot can be checked from its log, without its plist."""
    if sendmail is None:
        monkeypatch.delenv("SESSIONMAIL_SENDMAIL", raising=False)
    else:
        monkeypatch.setenv("SESSIONMAIL_SENDMAIL", sendmail)
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    with pytest.raises(start):
        bot.main()
    [line] = [m for m in logged if m.startswith("agent mail: ")]
    assert line.endswith(f"), {carrier}")


def test_the_start_line_names_unsigned_on_the_drop(start, fake_message, monkeypatch):
    """No signer, and sessionmail's own resolver says the drop: the bot starts
    and its start line names the mode and why."""
    from sessionphone import settings
    monkeypatch.setattr(settings, "mail_carrier", lambda exe, sendmail_value=None: {
        "kind": "drop", "path": None, "source": "default", "why": ""})
    monkeypatch.delenv("DC_SIGNER_SOCK")
    monkeypatch.delenv("DC_SIGNER_PUB")
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    with pytest.raises(start):
        bot.main()
    [line] = [m for m in logged if m.startswith("agent mail: ")]
    assert (f"send as seat {SEAT}, unsigned (carrier drop: this bot writes into the box itself, from "
            "sessionmail's default), call mails too") in line
    assert line.endswith("carrier drop (sessionmail's default)")


def test_the_default_mta_is_only_named_never_run():
    """In code (not prose), DEFAULT_MTA is read by seatmail.carrier_said()
    alone, and it only returns words: it calls nothing but env.get, the
    read-only question to sessionmail (settings.mail_file, of the `message`
    resolve_message finds) and string work."""
    import ast
    reads = []
    for path in sorted((REPO / "src").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for n in ast.walk(fn):
                name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
                if name == "DEFAULT_MTA":
                    reads.append((path.relative_to(REPO).as_posix(), fn.name))
    assert reads == [("src/sessionphone/bridge/seatmail.py", "carrier_said")]
    fn = next(n for n in ast.walk(ast.parse((REPO / "src/sessionphone/bridge/seatmail.py").read_text()))
              if isinstance(n, ast.FunctionDef) and n.name == "carrier_said")
    calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert calls <= {"e.get", "v.strip", "settings.mail_file", "resolve_message", "(probe or {}).get",
                     "settings.shown", "json.dumps", "isinstance", "value.startswith",
                     "settings.mail_carrier", "_said_of", "_from"}, calls     # _from: words only


def test_check_passes_with_message_and_a_signer(fake_message):
    assert seatmail.check() is None


def test_a_dead_signer_socket_does_not_stop_the_start(fake_message, monkeypatch, tmp_path):
    """The socket may come up after the bot: a dead agent is a send-time refusal (kind identity)."""
    monkeypatch.setenv("DC_SIGNER_SOCK", str(tmp_path / "not-yet.sock"))
    assert seatmail.check() is None


# ---- the message tripwire (conftest) -----------------------------------------

def test_dc_message_is_the_tripwire(message_tripwire):
    assert os.environ["DC_MESSAGE"].endswith("/message")
    assert seatmail.resolve_message() == os.environ["DC_MESSAGE"]


def test_a_send_that_reaches_the_tripwire_is_refused_and_recorded(seat, message_tripwire):
    assert bot.mail_in("Team", 12, "Bob", "hi") is False
    assert message_tripwire.read_text().count("| send ") == 1
    message_tripwire.write_text("")      # this test meant to reach it: clear, so teardown passes


def test_the_real_message_is_refused_by_any_name(message_tripwire):
    runs = [["message", "send", "x", "-s", "y", "--no-body"], "message send x -s y --no-body"]
    for args in runs:
        r = subprocess.run(args, shell=isinstance(args, str), capture_output=True, check=False)
        assert r.returncode == 3
    assert message_tripwire.read_text().count("| REAL ") == len(runs)
    message_tripwire.write_text("")


# ---- M1-M4: every caller in bot.py takes the seat path ------------------------

def test_no_m1_to_m4_caller_in_bot_py_threads_on_a_bot_minted_id():
    """mail_in routes by msgid / in_reply_to / supersedes (the call mails, M5).
    Every mail_in call in bot.py (chat live and swept, request, reaction, tap)
    passes none of them, so each goes by message send."""
    import ast
    tree = ast.parse((REPO / "src/sessionphone/bridge/bot.py").read_text())
    sites = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "mail_in"]
    assert len(sites) >= 5                      # M1 live, M1 sweep, M2, M3, M4
    for n in sites:
        assert not {k.arg for k in n.keywords} & {"msgid", "in_reply_to", "supersedes"}, n.lineno


def test_a_held_contact_request_goes_by_message_send(fake_message, monkeypatch):
    who = types.SimpleNamespace(display_name="Stranger", address="s@relay.example")
    msg = types.SimpleNamespace(sender=types.SimpleNamespace(get_snapshot=lambda: who),
                                text="let me in", file=None, quote=None)
    bot.request_in("Stranger", 44, msg)
    argv = fake_message.argv()
    assert argv[argv.index("-s") + 1] == "[Stranger 44] REQUEST Stranger: let me in"
    assert fake_message.env()["SESSIONMAIL_REQUIRE_SIGNATURE"] == "1"
    assert "NOT ACCEPTED" in fake_message.body()

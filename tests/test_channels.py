"""The allow-list (PHONE-102, design/allow-list.md rules 1, 4, 5): fakes only, never a Delta send."""
from __future__ import annotations

import json
import os
import types

import pytest

from sessionphone import channels

OWNER = "bob@relay.example"


class FakeContact:
    def __init__(self, address):
        self.address = address

    def get_snapshot(self):
        return types.SimpleNamespace(address=self.address)


class FakeChat:
    """Records any send: check() must never make one."""

    def __init__(self, chat_type, members, account):
        self.chat_type, self.members, self.account = chat_type, members, account

    def get_basic_snapshot(self):
        return types.SimpleNamespace(chat_type=self.chat_type)

    def get_contacts(self):
        return [FakeContact(a) for a in self.members]

    def __getattr__(self, name):
        if name.startswith("send"):
            return lambda *a, **k: self.account.sent.append((name, a, k))
        raise AttributeError(name)


class FakeAccount:
    def __init__(self, chats=None, fail=False):
        self.chats, self.fail, self.sent, self.created = chats or {}, fail, [], []

    def get_chat_by_id(self, chat_id):
        if self.fail:
            raise RuntimeError("rpc down")
        chat_type, members = self.chats[chat_id]
        return FakeChat(chat_type, members, self)

    def create_chat(self, *a):   # opening a chat is not checking one
        self.created.append(a)


@pytest.fixture(autouse=True)
def no_real_list(monkeypatch, tmp_path):
    """No test reads the real bot's list: the default path is an absent file under tmp."""
    monkeypatch.setenv("DC_CHANNELS_FILE", str(tmp_path / "default" / "channels.txt"))
    monkeypatch.delenv("PIP_CHAT_CHANNELS_FILE", raising=False)


@pytest.fixture
def account():
    # 12: the owner's 1:1. 13: a stranger's 1:1. 42: the family group.
    # 43: a group the owner is in, NOT listed. 44: the owner's 1:1 on an older core (int type).
    return FakeAccount({
        12: ("Single", [OWNER]),
        13: ("Single", ["stranger@relay.example"]),
        42: ("Group", [OWNER, "kid@relay.example"]),
        43: ("Group", [OWNER]),
        44: (100, [OWNER.upper()]),
        45: ("Mailinglist", [OWNER]),
    })


@pytest.fixture
def listed(tmp_path):
    p = tmp_path / "channels.txt"
    p.write_text(f"# NAME TARGET # note\nowner  {OWNER}  # owner\nfamily chat:42  # the family group\n")
    p.chmod(0o600)
    return p


@pytest.fixture
def logged():
    return []


# ---- the path, as the bot sees it ------------------------------------------


def test_the_path_is_dc_channels_file_else_the_bot_root(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DC_CHANNELS_FILE", raising=False)
    monkeypatch.delenv("PIP_CHAT_CHANNELS_FILE", raising=False)
    assert channels.channels_path() == channels.dc_env.ROOT / "channels.txt"
    monkeypatch.setenv("DC_CHANNELS_FILE", str(tmp_path / "other.txt"))
    assert channels.channels_path() == tmp_path / "other.txt"


def test_check_reads_the_env_path_by_default(monkeypatch, listed, account) -> None:
    monkeypatch.setenv("DC_CHANNELS_FILE", str(listed))
    assert channels.check(account, 42)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(listed.with_name("absent.txt")))
    assert not channels.check(account, 42)


# ---- rule 4: it fails closed -----------------------------------------------


def _refuses_everything(account, path, reason):
    for chat in (12, 13, 42, 43):
        v = channels.check(account, chat, path)
        assert not v and not v.ok
        assert v.detail.startswith("REFUSED") and str(path) in v.detail and reason in v.detail
    with pytest.raises(channels.Refused, match=reason):
        channels.resolve("owner", path)
    assert account.sent == []


def test_a_missing_file_refuses_every_check(tmp_path, account) -> None:
    _refuses_everything(account, tmp_path / "channels.txt", "NO channels file")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_file_refuses_every_check(listed, account) -> None:
    listed.chmod(0o000)
    try:
        _refuses_everything(account, listed, "cannot read")
    finally:
        listed.chmod(0o600)


@pytest.mark.parametrize("mode", [0o660, 0o620, 0o602, 0o666, 0o622])
def test_a_group_or_world_writable_file_refuses_every_check(listed, account, mode) -> None:
    listed.chmod(mode)
    _refuses_everything(account, listed, f"mode {mode:04o}, writable by group or world")


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o640, 0o644])
def test_a_file_only_the_owner_writes_is_read(listed, account, mode) -> None:
    listed.chmod(mode)
    assert channels.check(account, 42, listed)


def test_a_directory_in_place_of_the_file_refuses(tmp_path, account) -> None:
    d = tmp_path / "channels.txt"
    d.mkdir(mode=0o700)
    _refuses_everything(account, d, "cannot read")


def test_an_empty_file_lists_nothing_and_refuses(tmp_path, account) -> None:
    p = tmp_path / "channels.txt"
    p.write_text("# nobody yet\n")
    p.chmod(0o600)
    v = channels.check(account, 12, p)
    assert not v and f"chat 12 is not on the allow-list {p}" in v.detail
    assert "sessionphone channels add NAME chat:12" in v.detail


# ---- rule 1: what each TARGET passes -----------------------------------------


def test_a_chat_entry_passes_exactly_that_chat(listed, account) -> None:
    v = channels.check(account, 42, listed)
    assert v and v.name == "family"
    for other in (41, 43, 420, 4):
        assert not channels.check(account, other, listed)
    assert account.sent == []


def test_a_chat_entry_passes_without_asking_the_core(listed) -> None:
    assert channels.check(FakeAccount(fail=True), 42, listed)


def test_an_address_passes_its_one_to_one_chat(listed, account) -> None:
    v = channels.check(account, 12, listed)
    assert v and v.name == "owner" and OWNER in v.detail


def test_an_address_passes_on_an_older_core_and_ignores_case(listed, account) -> None:
    assert channels.check(account, 44, listed)


def test_an_address_never_passes_a_group_containing_that_contact(listed, account) -> None:
    for chat in (43, 45):   # a group with the owner in it; a mailing list
        v = channels.check(account, chat, listed)
        assert not v and f"chat {chat} is not on the allow-list {listed}" in v.detail
    assert account.sent == [] and account.created == []


def test_a_one_to_one_chat_with_someone_else_is_refused(listed, account) -> None:
    assert not channels.check(account, 13, listed)


def test_a_chat_the_core_cannot_read_is_refused(listed) -> None:
    v = channels.check(FakeAccount(fail=True), 12, listed)
    assert not v and "could not read chat 12" in v.detail and str(listed) in v.detail


def test_an_unknown_chat_is_refused(listed, account) -> None:
    assert not channels.check(account, 999, listed)   # the fake raises KeyError: fails closed


@pytest.mark.parametrize("bad", [0, -1, "x", "12x", "012", None, True, 1.5, ""])
def test_a_chat_id_that_is_not_one_is_refused(listed, account, bad) -> None:
    v = channels.check(account, bad, listed)
    assert not v and v.detail.startswith("REFUSED")


def test_a_chat_id_given_as_text_is_read_as_a_number(listed, account) -> None:
    assert channels.check(account, "42", listed)


# ---- malformed and duplicate lines: ignored, loudly, never widening ---------


def test_malformed_and_duplicate_lines_are_ignored_and_logged(tmp_path, account, logged) -> None:
    p = tmp_path / "channels.txt"
    p.write_text(
        "owner  chat:12\n"
        "Owner2 chat:13\n"              # NAME not lowercase
        "three  words chat:13\n"        # too many words
        "lonely\n"                      # no TARGET
        "zero   chat:0\n"               # not a chat id
        "neg    chat:-13\n"
        "who    nobody\n"               # neither form
        "owner  chat:13\n"              # a duplicate NAME: the first one wins
        "ok     chat:42  # fine\n"
    )
    p.chmod(0o600)
    entries = channels.load(p, logged.append).entries
    assert set(entries) == {"owner", "ok"} and entries["owner"].chat_id == 12
    assert len(logged) == 7 and all(m.startswith("IGNORED line") and str(p) in m for m in logged)
    assert any("already listed" in m for m in logged)
    assert not channels.check(account, 13, p, logged.append)   # nothing widened the list
    assert channels.check(account, 42, p, logged.append)


def test_a_bad_line_is_logged_to_stderr_by_default(tmp_path, capsys) -> None:
    p = tmp_path / "channels.txt"
    p.write_text("BAD chat:1\n")
    p.chmod(0o600)
    assert channels.load(p).entries == {}
    assert "IGNORED line 1" in capsys.readouterr().err


def test_a_bad_line_is_logged_once_however_often_the_file_is_read(tmp_path, logged) -> None:
    """The bot re-reads the list on every job: a line that stays bad is logged once."""
    p = tmp_path / "channels.txt"
    p.write_text("ok chat:1\nBAD chat:2\n")
    p.chmod(0o600)
    for _ in range(3):
        channels.load(p, logged.append)
    assert logged == [f"IGNORED line 2 of {p}: NAME 'BAD' is not lowercase [a-z0-9_-]+: 'BAD chat:2'"]


def test_a_fifo_in_place_of_the_file_refuses_without_hanging(tmp_path, account) -> None:
    p = tmp_path / "channels.txt"
    os.mkfifo(p, 0o600)
    listing = channels.load(p)
    assert listing.refusal == f"cannot read {p}: not a regular file"
    assert not channels.check(account, 12, p)


def test_a_refusal_carries_a_short_reason_for_the_log(tmp_path, listed, account) -> None:
    assert channels.check(account, 13, listed).reason == f"not in {listed}"
    assert channels.check(account, 12, tmp_path / "none.txt").reason == f"NO channels file at {tmp_path / 'none.txt'}"
    assert channels.check(account, 42, listed).reason == ""


def test_a_listing_already_read_is_used_as_is(listed, account) -> None:
    """check() and resolve() take the bot's one read per job, and read nothing more."""
    listing = channels.load(listed)
    listed.unlink()
    assert channels.check(account, 42, listing=listing)
    assert channels.resolve("family", listing=listing).chat_id == 42


# ---- resolve(name) ----------------------------------------------------------


def test_resolve_a_name(listed) -> None:
    owner = channels.resolve("owner", listed)
    assert owner.address == OWNER and owner.chat_id is None and owner.note == "owner"
    family = channels.resolve("family", listed)
    assert family.chat_id == 42 and family.address is None


def test_resolve_an_unknown_name_is_refused_naming_the_path(listed) -> None:
    with pytest.raises(channels.Refused, match="no channel 'nobody' in .*channels.txt"):
        channels.resolve("nobody", listed)


# ---- add(name, target) ------------------------------------------------------


def test_add_creates_the_file_0600(tmp_path, account) -> None:
    p = tmp_path / "channels.txt"
    e = channels.add("owner", OWNER.upper(), "owner, joined today", p)
    assert e == channels.Entry("owner", OWNER, "owner, joined today")
    assert p.stat().st_mode & 0o777 == 0o600
    assert p.read_text() == f"owner  {OWNER}  # owner, joined today\n"
    assert channels.check(account, 12, p)


def test_add_appends_and_tightens_the_mode(listed, account) -> None:
    listed.chmod(0o664)
    channels.add("stranger", "chat:13", path=listed)
    assert listed.stat().st_mode & 0o777 == 0o600
    assert channels.check(account, 13, listed) and channels.check(account, 42, listed)


def test_add_after_a_file_with_no_final_newline(tmp_path) -> None:
    p = tmp_path / "channels.txt"
    p.write_text("family chat:42")
    channels.add("owner", OWNER, path=p)
    assert set(channels.load(p).entries) == {"family", "owner"}


def test_add_refuses_a_duplicate_name(listed) -> None:
    before = listed.read_text()
    with pytest.raises(ValueError, match="already listed"):
        channels.add("owner", "chat:13", path=listed)
    assert listed.read_text() == before


@pytest.mark.parametrize("name,target", [
    ("Owner", OWNER), ("has space", OWNER), ("", OWNER), ("x#y", OWNER),
    ("ok", "nobody"), ("ok", "chat:0"), ("ok", "chat:x"), ("ok", ""), ("ok", "a@b c"),
])
def test_add_refuses_a_bad_name_or_target(tmp_path, name, target) -> None:
    p = tmp_path / "channels.txt"
    with pytest.raises(ValueError):
        channels.add(name, target, path=p)
    assert not p.exists()


def test_a_note_can_never_add_a_line(tmp_path, account) -> None:
    p = tmp_path / "channels.txt"
    channels.add("owner", OWNER, "hi\nevil chat:13\r\nmore", p)
    assert set(channels.load(p).entries) == {"owner"}
    assert not channels.check(account, 13, p)


# ---- an all-digit NAME (FOREMAN ruling) -------------------------------------
# A tool always reads an all-digit argument as a chat id, so a NAME "12" could
# never be reached: parse() ignores it (logged, which only narrows the list)
# and add() refuses it.


def test_an_all_digit_name_is_ignored_and_logged(tmp_path, account, logged) -> None:
    p = tmp_path / "channels.txt"
    p.write_text("12  chat:13\n007  bob@relay.example\nfamily  chat:42\n")
    p.chmod(0o600)
    listing = channels.load(p, logged.append)
    assert set(listing.entries) == {"family"}
    assert sum("all digits" in m for m in logged) == 2
    assert not channels.check(account, 13, listing=listing)


def test_add_refuses_an_all_digit_name(tmp_path) -> None:
    with pytest.raises(ValueError, match="all digits"):
        channels.add("12", "chat:13", path=tmp_path / "channels.txt")
    assert not (tmp_path / "channels.txt").exists()


def test_a_name_with_a_digit_is_still_a_name(listed) -> None:
    channels.add("kid2", "chat:7", path=listed)
    assert channels.resolve("kid2", listed).chat_id == 7


# ---- a NAME with a leading '-' (do-007, the do-003 review, advisory 1)
# A tool passes the NAME on a command line, where '-x' reads as an option. The
# pre-check now ends its options with '--' (bin/dc-env), and the list refuses
# such a NAME too: parse() ignores it (logged; only narrows), add() refuses it.


def test_a_leading_dash_name_is_ignored_and_logged(tmp_path, account, logged) -> None:
    p = tmp_path / "channels.txt"
    p.write_text("-x  chat:13\n--help  chat:13\nkid-2  chat:7\nfamily  chat:42\n")
    p.chmod(0o600)
    listing = channels.load(p, logged.append)
    assert set(listing.entries) == {"kid-2", "family"}        # a '-' inside a NAME is fine
    assert sum("starts with '-'" in m for m in logged) == 2
    assert not channels.check(account, 13, listing=listing)


@pytest.mark.parametrize("name", ["-x", "--help", "-"])
def test_add_refuses_a_leading_dash_name(tmp_path, name) -> None:
    with pytest.raises(ValueError, match="starts with '-'"):
        channels.add(name, "chat:13", path=tmp_path / "channels.txt")
    assert not (tmp_path / "channels.txt").exists()


def test_main_takes_a_dash_ref_after_double_dash(listed, capsys) -> None:
    """What bin/dc-env's dc_precheck now runs: a REF like '-x' is refused (3), not a usage error (2)."""
    rc = channels.main(["precheck", "--list", str(listed), "--tool", "dc-say", "--field", "chat_id",
                        "--", "chat", "-x"])
    out, err = capsys.readouterr()
    assert rc == 3 and out == ""
    assert f"dc-say: REFUSED: no channel '-x' in {listed}" in err


# ---- the tools' pre-check (rule 3): refuse-only -----------------------------


@pytest.mark.parametrize("ref, want", [("12", True), ("0", True), ("007", True), ("owner", False),
                                       ("", False), ("-1", False), ("1.5", False), ("１２", False),
                                       ("²", False), (" 12", False), (12, False)])
def test_is_chat_id_is_ascii_digits_only(ref, want) -> None:
    assert channels.is_chat_id(ref) is want


@pytest.mark.parametrize("field, ref, want", [
    ("chat_id", "12", {"chat_id": 12}),
    ("chat_id", "owner", {"chat": "owner"}),
    ("call", "12", {"call": 12}),
    ("call", "family", {"call": None, "chat": "family"}),
    ("hangup", "007", {"hangup": 7}),
    ("answer", "owner", {"answer": None, "chat": "owner"}),
])
def test_destination_keeps_a_number_in_its_field_and_a_name_in_chat(field, ref, want) -> None:
    assert channels.destination(field, ref) == want


def test_destination_quotes_any_name_safely() -> None:
    nasty = 'x", "chat_id": 13, "y": "\\'
    d = channels.destination("hangup", nasty)
    assert json.loads(json.dumps(d)) == {"hangup": None, "chat": nasty}


def _pc(kind, ref, path):
    return channels.precheck(kind, ref, path, lambda _m: None)


@pytest.mark.parametrize("kind", ["chat", "msg", "contact"])
@pytest.mark.parametrize("why, make", [
    ("NO channels file", lambda p: None),
    ("cannot read", lambda p: (p.write_text("family chat:42\n"), p.chmod(0))),
    ("writable by group or world", lambda p: (p.write_text("family chat:42\n"), p.chmod(0o660))),
])
def test_precheck_refuses_every_kind_when_the_list_is_unusable(tmp_path, kind, why, make) -> None:
    if why == "cannot read" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    p = tmp_path / "channels.txt"
    make(p)
    for ref in ("42", "family", "nobody"):
        refusal, gated = _pc(kind, ref, p)
        assert refusal and refusal.startswith("REFUSED") and why in refusal and str(p) in refusal
        assert gated


def test_precheck_refuses_an_unknown_name_naming_the_list(listed) -> None:
    refusal, _ = _pc("chat", "nobody", listed)
    assert refusal == (f"REFUSED: no channel 'nobody' in {listed}; nothing was sent. "
                       "Add it: sessionphone channels add nobody TARGET")


def test_precheck_lets_a_listed_name_through_to_the_bot(listed) -> None:
    assert _pc("chat", "family", listed) == (None, True)
    assert _pc("chat", "owner", listed) == (None, True)


def test_precheck_lets_a_listed_chat_id_through_to_the_bot(listed) -> None:
    assert _pc("chat", "42", listed) == (None, True)


def test_precheck_cannot_decide_a_number_while_an_address_is_listed(listed) -> None:
    """13 may be the 1:1 of `owner <addr>`: only the bot can read the chat."""
    assert _pc("chat", "13", listed) == (None, True)


def test_precheck_refuses_a_number_no_entry_could_pass(tmp_path) -> None:
    p = tmp_path / "channels.txt"
    p.write_text("family chat:42\n")        # no address entry: only chat 42 can pass
    p.chmod(0o600)
    refusal, _ = _pc("chat", "13", p)
    assert refusal == (f"REFUSED: chat 13 is not on the allow-list {p}; nothing was sent. "
                       "Add it: sessionphone channels add NAME chat:13")
    assert _pc("chat", "0", p)[0] == (f"REFUSED: '0' is not a chat id, so nothing on the allow-list {p} "
                                      "can pass it; nothing was sent.")


def test_precheck_cannot_decide_a_msg_id_or_a_contact(listed) -> None:
    assert _pc("msg", "5531", listed) == (None, True)
    assert _pc("contact", "Sam", listed) == (None, True)
    assert _pc("contact", "nobody-listed", listed) == (None, True)


def test_precheck_never_gates_ending_a_call_by_number(tmp_path) -> None:
    """Design row 9: hangup/answer by number work with no list at all."""
    assert _pc("end", "13", tmp_path / "absent.txt") == (None, False)


def test_precheck_resolves_a_name_for_ending_a_call(listed, tmp_path) -> None:
    assert _pc("end", "family", listed) == (None, True)
    assert "no channel 'nobody'" in _pc("end", "nobody", listed)[0]
    assert "NO channels file" in _pc("end", "family", tmp_path / "absent.txt")[0]


def test_precheck_knows_its_kinds(listed) -> None:
    with pytest.raises(ValueError, match="kind"):
        channels.precheck("bogus", "12", listed)


def test_precheck_never_sends(listed, account) -> None:
    """It reads only the file: it is given no account, so it cannot touch the core."""
    import inspect
    assert "account" not in inspect.signature(channels.precheck).parameters


# ---- main(): what a dc-* tool runs ------------------------------------------


def test_main_prints_wait_and_the_destination(listed, capsys) -> None:
    assert channels.main(["precheck", "--list", str(listed), "--field", "call", "chat", "family"]) == 0
    assert capsys.readouterr().out.splitlines() == ["wait", '{"call": null, "chat": "family"}']
    assert channels.main(["precheck", "--list", str(listed), "--field", "chat_id", "chat", "42"]) == 0
    assert capsys.readouterr().out.splitlines() == ["wait", '{"chat_id": 42}']


def test_main_prints_nowait_for_hangup_by_number(tmp_path, capsys) -> None:
    rc = channels.main(["precheck", "--list", str(tmp_path / "absent.txt"), "--field", "hangup", "end", "13"])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["nowait", '{"hangup": 13}']


def test_main_without_a_field_prints_no_destination(listed, capsys) -> None:
    assert channels.main(["precheck", "--list", str(listed), "msg", "5531"]) == 0
    assert capsys.readouterr().out.splitlines() == ["wait", "{}"]


def test_main_refuses_with_exit_3_naming_the_tool_and_the_list(tmp_path, capsys) -> None:
    p = tmp_path / "channels.txt"
    p.write_text("12  chat:12\n")           # all digits: ignored, and said why on refusal
    p.chmod(0o600)
    rc = channels.main(["precheck", "--list", str(p), "--tool", "dc-say", "--field", "chat_id", "chat", "12"])
    out, err = capsys.readouterr()
    assert rc == 3 and out == ""
    assert err.startswith(f"dc-say: REFUSED: chat 12 is not on the allow-list {p}")
    assert "dc-say: IGNORED line 1" in err and "all digits" in err


def test_main_usage_is_exit_2(capsys) -> None:
    with pytest.raises(SystemExit) as e:
        channels.main(["precheck", "bogus", "12"])
    assert e.value.code == 2

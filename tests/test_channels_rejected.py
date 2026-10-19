"""PHONE-102 do-008: `sessionphone channels rejected [--profile public]`.

The public profile's bot records each inbound it turned away in rejected.jsonl
beside that profile's channels.txt (who and why, never what). This verb lists
them newest first, one line per chat with a count, so the owner can let one in
with `channels add`. It never prints message text: the record holds none, and
a stranger's name is printed with its control characters made spaces.
"""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from sessionphone import channels
from sessionphone.cli import main as cli


def _clean_env(monkeypatch):
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)


@pytest.fixture
def root(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    r = tmp_path / "root"
    (r / "public").mkdir(parents=True, mode=0o700)
    r.chmod(0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    monkeypatch.setenv("DC_ROOT", str(r))
    return r


def _rec(ts, chat_id, kind="message", addr="bob@relay.example", name="Bob", reason=None):
    return channels.rejection("public", chat_id, kind, addr, name,
                              reason or f"chat {chat_id}: not in /x/public/channels.txt", ts=ts)


def _write(path, *recs):
    for r in recs:
        channels.record_rejected(path, r)


def test_lists_newest_first_deduped_by_chat_with_a_count(root):
    f = root / "public" / "rejected.jsonl"
    _write(f,
           _rec("2030-01-03T12:00:00+1100", 43),
           _rec("2030-01-03T12:01:00+1100", 50, kind="contact-request", addr="eve@relay.example", name="Eve"),
           _rec("2030-01-03T12:02:00+1100", 43, kind="reaction"),
           _rec("2030-01-03T12:03:00+1100", 43))
    r = CliRunner().invoke(cli, ["channels", "rejected", "--profile", "public"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith("2030-01-03T12:03:00+1100  chat 43  x3  message,reaction  Bob <bob@relay.example>")
    assert "not in /x/public/channels.txt" in lines[0]
    assert lines[1].startswith("2030-01-03T12:01:00+1100  chat 50  x1  contact-request  Eve <eve@relay.example>")
    assert lines[2] == (f"to let one in: DC_CHANNELS_FILE={root / 'public' / 'channels.txt'} "
                        "sessionphone channels add NAME ADDRESS|chat:N")
    assert len(lines) == 3


def test_reads_the_rotated_file_too(root):
    f = root / "public" / "rejected.jsonl"
    _write(f.with_name("rejected.jsonl.1"), _rec("2030-01-03T11:00:00+1100", 13))
    _write(f, _rec("2030-01-03T12:00:00+1100", 43), _rec("2030-01-03T12:05:00+1100", 13))
    r = CliRunner().invoke(cli, ["channels", "rejected", "--profile", "public"])
    assert r.exit_code == 0, r.output
    first, second = r.output.splitlines()[:2]
    assert "chat 13  x2" in first and first.startswith("2030-01-03T12:05:00")
    assert "chat 43  x1" in second


def test_never_prints_message_text_and_flattens_a_strangers_name(root):
    f = root / "public" / "rejected.jsonl"
    # a hand-edited record carrying a text field and a name with a newline in it:
    # the verb prints only who and why
    f.write_text(json.dumps({"ts": "t", "profile": "public", "chat_id": 9, "address": "x@y",
                             "name": "Eve\nchat 1  x99", "kind": "message", "reason": "r",
                             "text": "SECRET WORDS"}) + "\n")
    f.chmod(0o600)
    r = CliRunner().invoke(cli, ["channels", "rejected", "--profile", "public"])
    assert r.exit_code == 0, r.output
    assert "SECRET" not in r.output
    assert r.output.splitlines()[0] == "t  chat 9  x1  message  Eve chat 1 x99 <x@y>  - r"


def test_no_record_says_so(root):
    r = CliRunner().invoke(cli, ["channels", "rejected", "--profile", "public"])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == f"no rejections recorded ({root / 'public' / 'rejected.jsonl'})"


def test_default_profile_reads_beside_its_own_list(root):
    _write(root / "rejected.jsonl", _rec("t1", 5))
    r = CliRunner().invoke(cli, ["channels", "rejected"])
    assert r.exit_code == 0, r.output
    assert "chat 5  x1" in r.output and f"DC_CHANNELS_FILE={root / 'channels.txt'}" in r.output


def test_dc_outbox_public_names_the_public_profiles_dir(root, monkeypatch, tmp_path):
    elsewhere = tmp_path / "pub"
    elsewhere.mkdir()
    monkeypatch.setenv("DC_OUTBOX_PUBLIC", str(elsewhere / "outbox"))
    _write(elsewhere / "rejected.jsonl", _rec("t1", 77))
    r = CliRunner().invoke(cli, ["channels", "rejected", "--profile", "public"])
    assert r.exit_code == 0, r.output
    assert "chat 77" in r.output


def test_a_bad_line_is_skipped_and_counted(root):
    f = root / "public" / "rejected.jsonl"
    _write(f, _rec("t1", 5))
    with f.open("a") as fh:
        fh.write("not json\n[1, 2]\n")
    r = CliRunner().invoke(cli, ["channels", "rejected", "--profile", "public"])
    assert r.exit_code == 0
    assert "2 line(s)" in r.output and "chat 5  x1" in r.output


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_record_exits_2_naming_it(root):
    f = root / "public" / "rejected.jsonl"
    _write(f, _rec("t1", 5))
    f.chmod(0o000)
    try:
        r = CliRunner().invoke(cli, ["channels", "rejected", "--profile", "public"])
    finally:
        f.chmod(0o600)
    assert r.exit_code == 2
    assert str(f) in r.output


def test_an_unknown_profile_is_a_usage_error(root):
    r = CliRunner().invoke(cli, ["channels", "rejected", "--profile", "work"])
    assert r.exit_code == 2


def test_fold_keeps_every_kind_once_in_order():
    out = channels.fold_rejected([_rec("a", 1, "message"), _rec("b", 1, "reaction"),
                                  _rec("c", 1, "message"), _rec("d", 2, "webxdc")])
    assert [(x.last["ts"], x.count, x.kinds) for x in out] == [
        ("d", 1, ["webxdc"]), ("c", 3, ["message", "reaction"])]

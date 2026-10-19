"""Delete for everyone, all or nothing (PHONE-122, design.md section 2).

bridge/retract.delete_for_all checks EVERY id before any RPC that could send,
and one bad id refuses the whole call: nothing is sent, never a partial delete
(super's GO, Q1). Then ONE delete_messages_for_all for all the ids.

The client is real (deltachat_rpc_client's Account and Message); only the rpc
under it is a fake, answering in the server's own camelCase (fromId, isInfo,
chatId: the 2.62.0 schema), so a snapshot field name that drifted would fail
here. The gate is the bot's own gate over a real channels.txt. No network.
"""
import pathlib

import pytest
from deltachat_rpc_client import Account
from deltachat_rpc_client.rpc import JsonRpcError
from test_send_ack import outbox  # noqa: F401 - the one-pass outbox rig, a fixture

from sessionphone import channels, jobs
from sessionphone.bridge import bot, retract

SELF, BOB, INFO = 1, 10, 2       # core's contact ids: this account, a member, an info line


class FakeRpc:
    """The two unwrapped calls and get_message, over MSGS {id: (chat_id, from_id, is_info)}.
    Records every call, so a test can say exactly what reached core."""

    def __init__(self, msgs, core_error=None):
        self.msgs, self.core_error, self.calls = msgs, core_error, []

    def get_existing_msg_ids(self, account_id, ids):
        self.calls.append(("get_existing_msg_ids", account_id, list(ids)))
        return [i for i in ids if i in self.msgs]

    def get_message(self, account_id, msg_id):
        chat, sender, info = self.msgs[msg_id]
        return {"id": msg_id, "chatId": chat, "fromId": sender, "isInfo": info, "text": "hi"}

    def delete_messages_for_all(self, account_id, ids):
        self.calls.append(("delete_messages_for_all", account_id, list(ids)))
        if self.core_error:
            raise JsonRpcError({"code": -1, "message": self.core_error})

    @property
    def deletes(self):
        return [c for c in self.calls if c[0] == "delete_messages_for_all"]


class Manager:
    def __init__(self, rpc):
        self.rpc = rpc


MSGS = {
    5531: (12, SELF, False),        # the bot's own, in a listed chat
    5532: (12, SELF, False),
    5533: (12, BOB, False),        # another member's
    5534: (12, INFO, True),         # "member added"
    5535: (12, SELF, True),         # an info line under this account's id (a call status line)
    6001: (13, SELF, False),        # the bot's own, in another listed chat
    7001: (14, SELF, False),        # the bot's own, in an unlisted chat
}


def account(msgs=MSGS, **kw):
    return Account(manager=Manager(FakeRpc(dict(msgs), **kw)), id=3)


@pytest.fixture
def listing(tmp_path):
    p = tmp_path / "channels.txt"
    p.write_text("twelve  chat:12\nthirteen  chat:13\n")
    p.chmod(0o600)
    return channels.load(p)


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(bot, "log", lines.append)       # bot.gate logs through bot.log
    return lines


def delete(acct, ids, listing, logged):
    return retract.delete_for_all(acct, ids, listing, bot.gate, log=logged.append)


# ---- it goes: ONE rpc call with every id ----

def test_one_id_deletes_for_all_and_says_asked(listing, logged):
    a = account()
    done = delete(a, [5531], listing, logged)
    assert done == "deleted msg 5531 here and asked every member of chat 12 to delete it"
    assert a._rpc.deletes == [("delete_messages_for_all", 3, [5531])]
    assert "DELETED FOR ALL msg 5531 in chat 12" in logged


def test_several_ids_go_in_one_call_and_the_line_is_plural(listing, logged):
    a = account()
    done = delete(a, [5531, 5532], listing, logged)
    assert done == "deleted msgs 5531, 5532 here and asked every member of chat 12 to delete them"
    assert a._rpc.deletes == [("delete_messages_for_all", 3, [5531, 5532])]   # exactly one call


def test_a_repeated_id_is_sent_once(listing, logged):
    a = account()
    assert delete(a, [5531, 5531], listing, logged).startswith("deleted msg 5531 here")
    assert a._rpc.deletes == [("delete_messages_for_all", 3, [5531])]


def test_every_id_is_checked_before_the_delete(listing, logged):
    """The order the GO asks for: existence first, then each snapshot, and the
    delete is the LAST call core sees."""
    a = account()
    delete(a, [5531, 5532], listing, logged)
    assert [c[0] for c in a._rpc.calls] == ["get_existing_msg_ids", "delete_messages_for_all"]


# ---- every refusal: Refused, nothing sent, logged REFUSED delete ----

REFUSALS = [
    ([9999], "msg 9999: no such message on this profile"),
    ([5533], "msg 5533 was not sent by this account (from contact 10)"),
    ([5534], "msg 5534 is an info message"),
    ([5535], "msg 5535 is an info message"),
    ([5531, 6001], "are in more than one chat (chat 12: msg 5531; chat 13: msg 6001)"),
    ([7001], "chat 14 is not on the allow-list"),
]


@pytest.mark.parametrize("ids, why", REFUSALS)
def test_each_refusal_sends_nothing(listing, logged, ids, why):
    a = account()
    with pytest.raises(channels.Refused) as e:
        delete(a, ids, listing, logged)
    assert why in str(e.value) and str(e.value).startswith("REFUSED: ")
    assert a._rpc.deletes == []                                        # no delete call at all
    assert any(line.startswith(("REFUSED delete ", "REFUSED send to chat 14")) for line in logged)


@pytest.mark.parametrize("ids, why", REFUSALS[:4])
def test_one_bad_id_among_good_ones_refuses_the_whole_call(listing, logged, ids, why):
    """Q1: never a partial delete. A good id beside a bad one is not sent either."""
    a = account()
    with pytest.raises(channels.Refused) as e:
        delete(a, [5531, 5532, *ids], listing, logged)
    assert why in str(e.value) and "nothing was deleted" in str(e.value)
    assert a._rpc.deletes == []


def test_every_bad_id_is_named_not_only_the_first(listing, logged):
    a = account()
    with pytest.raises(channels.Refused) as e:
        delete(a, [5533, 5531, 5534], listing, logged)
    assert "msg 5533 was not sent by this account" in str(e.value)
    assert "msg 5534 is an info message" in str(e.value)
    with pytest.raises(channels.Refused) as e:
        delete(a, [9998, 5531, 9999], listing, logged)
    assert "msgs 9998, 9999: no such message" in str(e.value)


def test_the_gate_is_asked_with_what_delete(listing, logged):
    seen = []

    def gate(acct, chat_id, lst, what):
        seen.append((chat_id, lst, what))
        raise channels.Refused("REFUSED: by the gate")
    a = account()
    with pytest.raises(channels.Refused, match="by the gate"):
        retract.delete_for_all(a, [5531], listing, gate)
    assert seen == [(12, listing, "delete")] and a._rpc.deletes == []


def test_a_list_that_refuses_everything_refuses_a_delete(tmp_path, logged):
    a = account()
    with pytest.raises(channels.Refused, match="every send is refused"):
        delete(a, [5531], channels.load(tmp_path / "missing.txt"), logged)
    assert a._rpc.deletes == []


@pytest.mark.parametrize("raw", [[], None, 5531, "5531", ["5531"], [0], [-1], [True], [1.5], [5531, "x"]])
def test_a_delete_that_is_not_a_list_of_ids_is_refused(listing, logged, raw):
    """An outbox writes jobs by hand: the bot is the authority, not the tool."""
    a = account()
    with pytest.raises(channels.Refused, match="REFUSED: .*nothing was deleted"):
        delete(a, raw, listing, logged)
    assert a._rpc.calls == []                       # not even a lookup
    assert any(line.startswith("REFUSED delete job") for line in logged)


def test_cores_own_refusal_comes_through_verbatim(listing, logged):
    """design.md section 2 row 7: what core still refuses (an unencrypted
    chat), in its own words."""
    a = account(core_error="Cannot request deletion of unencrypted message for others")
    with pytest.raises(channels.Refused) as e:
        delete(a, [5531], listing, logged)
    assert str(e.value) == ("REFUSED: msg 5531 in chat 12: Delta Chat core refused: "
                            "Cannot request deletion of unencrypted message for others")
    assert not any(line.startswith("DELETED") for line in logged)


def test_core_text_of_a_plain_error():
    assert retract.core_text(JsonRpcError("boom")) == "boom"
    assert retract.core_text(JsonRpcError({"code": 1, "message": "Can delete only own messages for others"})) \
        == "Can delete only own messages for others"


# ---- the outbox loop: one small branch, acked like every job ----
# Through the real loop (bot.main, one pass, test_send_ack's rig), so a branch
# the dc-say `else` swallowed would fail here.

class LoopAccount(Account):
    """What main() asks of an account before its loop (test_send_ack's FakeAccount)."""

    def is_configured(self):
        return True

    def start_io(self):
        pass

    def get_config(self, key):
        return "carol@test"


@pytest.fixture
def loop(outbox, monkeypatch, tmp_path):  # noqa: F811 - the fixture imported above
    """test_send_ack's one-pass outbox rig, on an account with a fake rpc; its
    list (DC_CHANNELS_FILE, read per job) gains chat 12."""
    (tmp_path / "channels.txt").write_text("twelve  chat:12\nthirteen  chat:13\n")
    acct = LoopAccount(manager=Manager(FakeRpc(dict(MSGS))), id=3)
    monkeypatch.setattr(bot, "DeltaChat",
                        lambda rpc: type("DC", (), {"get_all_accounts": lambda self: [acct]})())
    outbox.acct = acct
    return outbox


def test_a_delete_job_through_the_outbox_loop(loop):
    ok, bad, mixed = loop({"delete": [5531, 5532]}, {"delete": [5533]}, {"delete": [5531, 9999]})
    assert ok == {"ok": True, "detail": "deleted msgs 5531, 5532 here and asked every member "
                                        "of chat 12 to delete them"}
    assert bad["ok"] is False and "msg 5533 was not sent by this account" in bad["detail"]
    assert mixed["ok"] is False and "msg 9999: no such message" in mixed["detail"]
    assert loop.acct._rpc.deletes == [("delete_messages_for_all", 3, [5531, 5532])]
    assert loop.sent == []                          # no dc-say: the else branch never ran
    assert "DELETED FOR ALL msgs 5531, 5532 in chat 12" in loop.logged
    assert any(line.startswith("REFUSED delete msg 5533") for line in loop.logged)


def test_cores_refusal_through_the_loop_is_ok_false(loop):
    loop.acct._rpc.core_error = "Can delete only from same chat."
    ack, = loop({"delete": [5531]})
    assert ack == {"ok": False,
                   "detail": "REFUSED: msg 5531 in chat 12: Delta Chat core refused: "
                             "Can delete only from same chat."}


def test_delete_is_a_new_key_not_a_legacy_one():
    """An older bot is refused at dc_shape ("restart the bot"), never handed a
    job it would read as a dc-say with no text."""
    assert "delete" in jobs.KEYS and "delete" not in jobs.LEGACY_KEYS
    why = jobs.check(pathlib.Path("/nonexistent"), {"delete": [1], "ack": "/x"})   # no stamp
    assert "does not read job key(s) 'delete'" in why and "restart the bot" in why

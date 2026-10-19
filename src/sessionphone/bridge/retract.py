"""retract - delete for everyone a message the bot sent.

`dc-delete MSG_ID [MSG_ID...]` queues {"delete": [ids]}; the bot's outbox
loop hands it here. Every id is checked BEFORE anything is sent, and one bad
id refuses the whole call: never a partial delete. Then ONE
delete_messages_for_all RPC for all of them.

A refusal raises channels.Refused, so the outbox loop acks it ok:false and the
tool exits 3. The bot's own checks refuse before anything is sent:
  - the job's "delete" is not a non-empty list of positive integer ids;
  - an id with no message on this profile (already deleted, or another profile's);
  - an info/system message ("member added", a call status line);
  - a message this account did not send (from_id != 1);
  - ids in more than one chat (core's rule too);
  - a chat the allow-list does not permit: GATE, the same gate dc-react uses.
Core's own refusal (e.g. an unencrypted chat) is exit 3 too, its text verbatim,
and the bot claims nothing for it: what core did before refusing is core's to say.

A delete-for-all is itself an outbound send (core mails a Chat-Delete
message to every member), so it sits behind the allow-list like every send.
"Asked", in the done line, because the bot sees only its own side: each
member's app applies the request when it next fetches.

The client has no wrapper for delete_messages_for_all or get_existing_msg_ids
(rpc-server 2.62.0); Rpc dispatches any method name, so both go through
account._rpc.
"""
from __future__ import annotations

from deltachat_rpc_client.rpc import JsonRpcError

from sessionphone import channels

SELF = 1    # DC_CONTACT_ID_SELF: core's from_id for a message this account sent


def _ids(ids: list[int]) -> str:
    return ", ".join(str(i) for i in ids)


def _msgs(ids: list[int]) -> str:
    """'msg 5' or 'msgs 5, 6'."""
    return f"msg{'s' if len(ids) > 1 else ''} {_ids(ids)}"


def _refuse(log, why: str, detail: str, tail: str = "; nothing was deleted."):
    if log is not None:
        log(f"REFUSED delete {why}")
    raise channels.Refused(f"REFUSED: {detail}{tail}")


def parse_ids(raw: object, log=None) -> list[int]:
    """The job's "delete": a non-empty list of positive integers (bool is not one),
    in order, a repeated id once. Refused otherwise: an outbox writes jobs by hand,
    so the bot is the authority, not the tool."""
    if not isinstance(raw, list) or not raw:
        _refuse(log, f"job: \"delete\" is {raw!r}",
                f'a delete job needs "delete", a non-empty list of message ids, not {raw!r}')
    bad = [i for i in raw if type(i) is not int or i <= 0]
    if bad:
        _refuse(log, f"job: not message ids: {bad!r}",
                f"{', '.join(repr(i) for i in bad)} {'is' if len(bad) == 1 else 'are'} not "
                "a message id (a positive integer)")
    return list(dict.fromkeys(raw))


def core_text(e: Exception) -> str:
    """Core's own words from a JsonRpcError: its error object's message, else str(e)."""
    arg = e.args[0] if e.args else None
    if isinstance(arg, dict) and arg.get("message"):
        return str(arg["message"])
    return str(e)


def delete_for_all(account, ids, listing, gate, log=None) -> str:
    """Delete IDS on this device and ask every member of their chat to delete them.

    GATE is the bot's gate(account, chat_id, listing, what): it raises
    channels.Refused when the allow-list does not permit the chat. LOG, when
    given, gets one line per refusal and one for the delete. -> the done line.
    Each check raises channels.Refused before any RPC that could send; core's own
    refusal comes from that RPC, and the bot claims nothing about what it did."""
    ids = parse_ids(ids, log)
    rpc = account._rpc
    have = set(rpc.get_existing_msg_ids(account.id, ids))
    missing = [i for i in ids if i not in have]
    if missing:
        _refuse(log, f"{_msgs(missing)}: no such message",
                f"{_msgs(missing)}: no such message on this profile (already deleted, or "
                "another profile's)")

    problems, chats = [], {}
    for i in ids:
        snap = account.get_message_by_id(i).get_snapshot()
        if snap.is_info:
            problems.append(f"msg {i} is an info message, not one this account sent")
        elif snap.from_id != SELF:
            problems.append(f"msg {i} was not sent by this account (from contact {snap.from_id})")
        chats.setdefault(snap.chat_id, []).append(i)
    if problems:
        _refuse(log, "; ".join(problems),
                "; ".join(problems) + ", and only this account's own messages can be deleted "
                "for everyone")
    if len(chats) > 1:
        where = "; ".join(f"chat {c}: {_msgs(m)}" for c, m in chats.items())
        _refuse(log, f"{_msgs(ids)}: more than one chat ({where})",
                f"{_msgs(ids)} are in more than one chat ({where}); delete one chat at a time")
    (chat_id,) = chats
    gate(account, chat_id, listing, "delete")       # logs and raises its own refusal

    try:
        rpc.delete_messages_for_all(account.id, ids)
    except JsonRpcError as e:
        text = core_text(e)
        _refuse(log, f"{_msgs(ids)} in chat {chat_id}: core: {text}",
                f"{_msgs(ids)} in chat {chat_id}: Delta Chat core refused: {text}",
                tail="")     # core's words, verbatim: what it did before refusing is its to say
    if log is not None:
        log(f"DELETED FOR ALL {_msgs(ids)} in chat {chat_id}")
    them = "them" if len(ids) > 1 else "it"
    return f"deleted {_msgs(ids)} here and asked every member of chat {chat_id} to delete {them}"

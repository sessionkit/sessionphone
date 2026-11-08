"""The chatlist surface the startup sweep reads, for the bridge's fake accounts.

bot.unseen_incoming() lists the sweep's work by walking the chatlist, not by
core's get_fresh_messages(). A fake account that keeps

- ``self.msgs``:  message id -> a message with ``.id`` and ``.snap``;
- ``self.chats``: chat id -> a chat with ``.info`` (its basic snapshot);
- ``self.fresh``: the messages core still holds unseen (InFresh);

mixes in ``Chatlist`` and gets the calls the listing makes. A message's state
is read from ``self.fresh`` at listing time, so a test that puts a message
back in ``fresh`` ("core lists it again") still means what it says.

A chat's ``info`` may carry ``archived``, ``is_muted``, ``is_device_chat``
and ``is_self_talk``; a chat with ``blocked = True`` is left out of the
chatlist, as core leaves it out. A snapshot may carry ``is_info``,
``from_id`` and ``timestamp``; unset, they are False, an ordinary contact
(10) and the message id.

``get_fresh_messages()`` is core's badge query: it leaves out muted chats and
contact requests, as the real one does. The sweep no longer calls it; it is
here so that a sweep that went back to it fails these tests.
"""
from deltachat_rpc_client.const import MessageState

CONTACT = 10        # an ordinary contact id, above SpecialContactId.LAST_SPECIAL


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ListedChat:
    """A chatlist entry: the Chat calls unseen_incoming() makes."""

    def __init__(self, account, chat_id):
        self.account, self.id = account, chat_id

    def _msgs(self):
        return sorted((m for m in self.account.msgs.values() if m.snap.chat_id == self.id),
                      key=lambda m: m.id)

    def get_fresh_message_count(self):
        return sum(1 for m in self.account.fresh if m.snap.chat_id == self.id)

    def get_basic_snapshot(self):
        info = self.account.chats[self.id].info
        return Obj(**{"is_device_chat": False, "is_self_talk": False, "archived": False,
                      "is_muted": False, **vars(info)})

    def get_messages(self):
        """Oldest first, as core lists them; each snapshot's state from fresh."""
        out = []
        for m in self._msgs():
            snap = m.snap
            snap.state = MessageState.IN_FRESH if m in self.account.fresh else MessageState.IN_SEEN
            for k, v in (("is_info", False), ("from_id", CONTACT), ("timestamp", m.id)):
                if not hasattr(snap, k):
                    setattr(snap, k, v)
            out.append(m)
        return out


class Chatlist:
    """Mix-in for a fake account with msgs, chats and fresh."""

    def get_chatlist(self, query=None, contact=None, archived_only=False,
                     for_forwarding=False, no_specials=False, alldone_hint=False,
                     snapshot=False):
        assert no_specials and not snapshot and query is None and contact is None, \
            "the fake chatlist knows only the listing the sweep asks for"
        out = []
        for cid, chat in sorted(self.chats.items()):
            if getattr(chat, "blocked", False):
                continue
            if bool(getattr(chat.info, "archived", False)) == bool(archived_only):
                out.append(ListedChat(self, cid))
        return out

    def get_fresh_messages(self):
        """Core's badge query, newest first: no muted chats, no requests."""
        out = []
        for m in self.fresh:
            info = self.chats[m.snap.chat_id].info
            if getattr(info, "is_muted", False) or getattr(info, "is_contact_request", False):
                continue
            out.append(m)
        return sorted(out, key=lambda m: -m.id)

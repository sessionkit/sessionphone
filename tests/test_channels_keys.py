"""Address channels are bound to a key fingerprint: channels.txt's `key:FPR`,
the gate that compares keys, the writers, and the one-time start migration.
Fakes only (tests/_keyfakes.py): no account, relay or server."""
from __future__ import annotations

import types

import _keyfakes as kf
import pytest

from sessionphone import channels, keygate
from sessionphone.bridge import bot

BOB = "bob@example.org"


@pytest.fixture(autouse=True)
def no_real_list(monkeypatch, tmp_path):
    monkeypatch.setenv("DC_CHANNELS_FILE", str(tmp_path / "default" / "channels.txt"))
    monkeypatch.delenv("PIP_CHAT_CHANNELS_FILE", raising=False)


class Chat:
    def __init__(self, cid, chat_type, members):
        self.id, self.chat_type, self.members = cid, chat_type, members

    def get_basic_snapshot(self):
        return types.SimpleNamespace(chat_type=self.chat_type)

    def get_contacts(self):
        return list(self.members)


class Account(kf.Account):
    """kf.Account, plus chats: id -> (type, [Contact]). A contact's 1:1 is
    chat 100 + its id."""

    def __init__(self, contacts=(), chats=None, fail=False):
        super().__init__(contacts)
        self.chats = {cid: Chat(cid, t, m) for cid, (t, m) in (chats or {}).items()}
        for c in self.contacts:
            self.chats.setdefault(100 + c.id, Chat(100 + c.id, "Single", [c]))
            c.create_chat = (lambda c=c: self.chats[100 + c.id])
        self.fail = fail

    def get_chat_by_id(self, chat_id):
        if self.fail:
            raise RuntimeError("rpc down")
        return self.chats[chat_id]


def write(tmp, text, marker=True):
    p = tmp / "channels.txt"
    p.write_text((channels.MARKER + "\n" if marker else "") + text)
    p.chmod(0o600)
    return p


def pinned(tmp, fpr=kf.FPR_K1):
    return write(tmp, f"# the owner\nowner  {BOB}  key:{fpr}  # owner, Bob\nfamily  chat:42\n")


REAL = kf.Contact(10, BOB, kf.K1)          # the contact listed, holding the pinned key
OTHER = kf.Contact(11, BOB, kf.K2)         # the same address, another key


# ---- parse -----------------------------------------------------------------


def test_an_address_line_with_a_key_is_read_with_it(tmp_path):
    have = channels.parse(f"owner  {BOB}  key:{kf.FPR_K1.lower()}  # n\n", tmp_path / "c", lambda m: None)
    assert have["owner"] == channels.Entry("owner", BOB, "n", kf.FPR_K1)


def test_an_address_line_with_no_key_is_read_unpinned(tmp_path):
    assert channels.parse(f"owner  {BOB}\n", tmp_path / "c", lambda m: None)["owner"].key is None


@pytest.mark.parametrize("line", [
    f"family  chat:42  key:{kf.FPR_K1}",          # a chat id takes no key
    f"owner  {BOB}  key:1234",                     # a key too short
    f"owner  {BOB}  {kf.FPR_K1}",                  # no key: prefix
    f"owner  {BOB}  key:{kf.FPR_K1}  extra",       # four words
])
def test_a_bad_keyed_line_is_ignored_and_logged(tmp_path, line):
    logged = []
    assert channels.parse(line + "\n", tmp_path / "c", logged.append) == {}
    assert logged and "IGNORED" in logged[0]


# ---- the gate --------------------------------------------------------------


def test_a_same_address_contact_with_another_key_is_refused(tmp_path):
    p = pinned(tmp_path)
    v = channels.check(Account([REAL, OTHER]), 111, p)
    assert not v and "not the listed one" in v.reason and BOB in v.reason
    assert v.detail.startswith("REFUSED") and "nothing was sent" in v.detail


def test_the_contact_holding_the_pinned_key_passes(tmp_path):
    p = pinned(tmp_path)
    v = channels.check(Account([REAL, OTHER]), 110, p)
    assert v and v.name == "owner"


def test_the_pinned_key_at_a_new_address_passes_and_says_so(tmp_path):
    p = pinned(tmp_path)
    moved = kf.Contact(12, "bob@new.example.org", kf.K1)
    logged = []
    v = channels.check(Account([moved]), 112, p, log=logged.append)
    assert v and v.name == "owner" and "now writes bob@new.example.org" in v.detail
    assert any("now writes" in line for line in logged)


def test_an_unpinned_address_entry_refuses_and_names_the_pin(tmp_path):
    p = write(tmp_path, f"owner  {BOB}  # owner\n", marker=False)
    v = channels.check(Account([REAL]), 110, p)
    assert not v and "sessionphone channels pin owner" in v.reason


def test_an_address_contact_is_refused(tmp_path):
    p = pinned(tmp_path)
    plain = kf.Contact(13, BOB, None)
    v = channels.check(Account([plain]), 113, p)
    assert not v and "no key" in v.reason and plain.vcards == 0


def test_a_contact_with_no_key_in_its_vcard_is_refused(tmp_path):
    p = pinned(tmp_path)
    c = kf.Contact(14, BOB, kf.K1, card=kf.vcard(BOB, None))
    assert not channels.check(Account([c]), 114, p)


def test_a_contact_read_error_refuses(tmp_path):
    p = pinned(tmp_path)
    c = kf.Contact(15, BOB, kf.K1, vcard_error=RuntimeError("rpc down"))
    v = channels.check(Account([c]), 115, p)
    assert not v and "could not read chat 115" in v.detail
    assert not channels.check(Account([REAL], fail=True), 110, p)


def test_a_group_with_the_pinned_contact_never_passes(tmp_path):
    p = pinned(tmp_path)
    acct = Account([REAL], chats={43: ("Group", [REAL])})
    assert not channels.check(acct, 43, p)


def test_a_chat_entry_behaves_as_before(tmp_path):
    p = pinned(tmp_path)
    acct = Account([REAL], chats={42: ("Group", [REAL, OTHER])})
    v = channels.check(acct, 42, p)
    assert v and v.name == "family"
    assert channels.check(Account(fail=True), 42, p)        # no core read for a chat entry


def test_a_stranger_is_simply_not_listed(tmp_path):
    p = pinned(tmp_path)
    v = channels.check(Account([kf.Contact(16, "eve@example.org", kf.K2)]), 116, p)
    assert not v and v.reason == f"not in {p}"


def test_precheck_still_never_decides_an_address_entry(tmp_path):
    p = write(tmp_path, f"owner  {BOB}  # owner\n", marker=False)
    assert channels.precheck("chat", "110", p) == (None, True)
    assert channels.precheck("chat", "owner", p) == (None, True)


# ---- through the bot -------------------------------------------------------


@pytest.fixture
def at_bot(monkeypatch, tmp_path):
    p = pinned(tmp_path)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(p))
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)
    return p, logged


def test_bot_inbound_ok_rejects_and_records_another_key(at_bot, monkeypatch):
    p, _ = at_bot
    monkeypatch.setattr(bot, "PROFILE", "public")
    acct = Account([REAL, OTHER])
    assert not bot.inbound_ok(acct, 111, "message", BOB, "Someone")
    recs, _ = channels.read_rejected(channels.rejected_path(p))
    assert recs[-1]["chat_id"] == 111 and "not the listed one" in recs[-1]["reason"]
    assert bot.inbound_ok(acct, 110, "message", BOB, "Someone")


def test_bot_chat_channel_names_no_channel_for_another_key(at_bot):
    acct = Account([REAL, OTHER])
    ch = bot.chat_channel(acct, 111)
    assert ch.name is None
    assert "X-Delta-Chat-Channel" not in bot.contract_headers("message", 1, BOB, ch)
    assert "dc-say owner" not in bot.channel_hint(111, ch)
    assert bot.chat_channel(acct, 110).name == "owner"


def test_bot_gate_refuses_an_outbox_send_to_another_key(at_bot):
    p, _ = at_bot
    listing = channels.load(p)
    with pytest.raises(channels.Refused):
        bot.gate(Account([REAL, OTHER]), 111, listing, "text")
    assert bot.gate(Account([REAL, OTHER]), 110, listing, "text") == 110


def test_an_in_call_post_to_another_key_is_refused(at_bot, monkeypatch):
    from sessionphone.bridge import calls
    logged = []
    monkeypatch.setattr(calls, "log", logged.append)
    acct = Account([REAL, OTHER])
    sent = []
    acct.chats[111].send_text = sent.append
    assert calls.post_to_chat(acct, 111, "hello", "transcript") is False
    assert sent == [] and "REFUSED transcript to chat 111" in logged[-1]


# ---- resolving a NAME --------------------------------------------------------


def test_a_name_resolves_to_the_pinned_contact_not_the_newest(at_bot):
    p, _ = at_bot
    entry = channels.load(p).entries["owner"]
    acct = Account([REAL, OTHER])           # OTHER is the more recent contact with BOB
    assert bot.listed_contact(acct, "owner", entry) is REAL
    assert bot.resolve_chat(acct, "owner", channels.load(p)) == 110


def test_a_name_whose_key_no_contact_holds_is_refused(at_bot):
    p, _ = at_bot
    with pytest.raises(channels.Refused, match="no contact of this profile holds"):
        bot.listed_contact(Account([OTHER]), "owner", channels.load(p).entries["owner"])


def test_a_name_whose_contact_moved_is_found_by_its_key(at_bot):
    p, _ = at_bot
    moved = kf.Contact(12, "bob@new.example.org", kf.K1)
    assert bot.listed_contact(Account([OTHER, moved]), "owner", channels.load(p).entries["owner"]) is moved


def test_an_unpinned_name_is_refused_and_names_the_pin(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(bot, "log", logged.append)       # never the real bot.log
    p = write(tmp_path, f"owner  {BOB}  # owner\n", marker=False)
    with pytest.raises(channels.Refused, match="sessionphone channels pin owner"):
        bot.listed_contact(Account([REAL]), "owner", channels.load(p).entries["owner"])
    assert logged and "no key is pinned" in logged[-1]


# ---- the writers -----------------------------------------------------------


def test_add_with_a_key_writes_the_keyed_line_and_the_marker(tmp_path):
    p = tmp_path / "channels.txt"
    e = channels.add("bob", BOB, "Bob", p, key=" ".join([kf.FPR_K1[i:i + 4] for i in range(0, 40, 4)]))
    assert e.key == kf.FPR_K1
    text = p.read_text()
    assert text.startswith(channels.MARKER) and f"bob  {BOB}  key:{kf.FPR_K1}  # Bob" in text
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_add_refuses_a_bad_key_or_a_key_on_a_chat_and_writes_nothing(tmp_path):
    p = tmp_path / "channels.txt"
    with pytest.raises(ValueError, match="not a key fingerprint"):
        channels.add("bob", BOB, "", p, key="1234")
    with pytest.raises(ValueError, match="takes no key"):
        channels.add("family", "chat:42", "", p, key=kf.FPR_K1)
    assert not p.exists()


def test_a_keyed_write_leaves_the_marker_off_while_an_old_line_is_unpinned(tmp_path):
    p = write(tmp_path, f"owner  {BOB}  # owner\n", marker=False)
    channels.add("eve", "eve@example.org", "", p, key=kf.FPR_K2)
    assert not keygate.marked(p.read_text())      # the start migration must still pin the owner


def test_pin_rewrites_the_line_in_place(tmp_path):
    p = write(tmp_path, f"# top\nowner  {BOB}   # owner, Bob\nfamily  chat:42  # fam\n", marker=False)
    assert channels.pin("owner", kf.FPR_K1.lower(), p) is None
    text = p.read_text()
    assert text == (f"{channels.MARKER}\n# top\nowner  {BOB}  key:{kf.FPR_K1}   # owner, Bob\n"
                    "family  chat:42  # fam\n")
    assert channels.pin("owner", kf.FPR_K2, p) == kf.FPR_K1
    assert channels.load(p).entries["owner"].key == kf.FPR_K2


def test_pin_refuses_an_unknown_name_and_a_chat_entry(tmp_path):
    p = pinned(tmp_path)
    with pytest.raises(KeyError):
        channels.pin("nobody", kf.FPR_K1, p)
    with pytest.raises(ValueError, match="takes no key"):
        channels.pin("family", kf.FPR_K1, p)


def test_seed_owner_writes_the_key(tmp_path):
    p = tmp_path / "channels.txt"
    e = channels.seed_owner(BOB, "owner, Bob", p, key=kf.FPR_K1)
    assert e == channels.Entry("owner", BOB, "owner, Bob", kf.FPR_K1)
    assert channels.load(p).entries["owner"].key == kf.FPR_K1


def test_seed_owner_pins_an_unpinned_listed_owner_in_place(tmp_path):
    p = write(tmp_path, f"me  {BOB}  # owner\n", marker=False)
    assert channels.seed_owner(BOB, "owner, Bob", p, key=kf.FPR_K1) is None
    assert channels.load(p).entries["me"].key == kf.FPR_K1


def test_seed_owner_never_replaces_another_key(tmp_path):
    p = pinned(tmp_path, kf.FPR_K2)
    before = p.read_text()
    with pytest.raises(ValueError, match="channels pin owner"):
        channels.seed_owner(BOB, "owner, Bob", p, key=kf.FPR_K1)
    assert p.read_text() == before


# ---- the one-time start migration -------------------------------------------


OLD = f"# NAME TARGET # note\nowner  {BOB}  # owner, joined 2030\nfamily  chat:42  # fam\nkid  kid@example.org\n"


def old_file(tmp_path, text=OLD):
    return write(tmp_path, text, marker=False)


def test_migrate_pins_the_owner_line_with_one_candidate(tmp_path):
    p = old_file(tmp_path)
    logged = []
    ds = channels.migrate(Account([REAL, kf.Contact(20, "kid@example.org", kf.K2)]), p, logged.append)
    assert [(d.name, d.kind) for d in ds] == [("owner", "pinned"), ("kid", "unpinned")]
    text = p.read_text()
    assert text.startswith(channels.MARKER + "\n# NAME TARGET # note\n")
    assert f"owner  {BOB}  key:{kf.FPR_K1}  # owner, joined 2030\n" in text
    assert "family  chat:42  # fam\nkid  kid@example.org\n" in text    # the rest byte for byte
    assert p.with_name("channels.txt.pre-keys").read_text() == OLD
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert any("1 pinned, 1 unpinned, 0 ambiguous" in line for line in logged)
    assert any("sessionphone channels pin kid --key" in line for line in logged)


def test_the_owner_still_passes_the_channel_gate_after_the_upgrade(tmp_path):
    """The owner line written before the upgrade (address only) passes after the first start."""
    p = old_file(tmp_path)
    acct = Account([REAL])
    assert not channels.check(acct, 110, p)              # an old line alone does not pass
    channels.migrate(acct, p, lambda m: None)
    v = channels.check(acct, 110, p)
    assert v and v.name == "owner"


def test_migrate_with_no_candidate_leaves_it_unpinned(tmp_path):
    p = old_file(tmp_path)
    logged = []
    ds = channels.migrate(Account([]), p, logged.append)
    assert ds[0].kind == "unpinned" and channels.load(p).entries["owner"].key is None
    assert any("UNPINNED bob@example.org" in line and "channels pin owner" in line for line in logged)
    assert any("WARNING" in line and "no address line is pinned" in line for line in logged)


def test_migrate_with_two_keys_on_the_address_pins_neither(tmp_path):
    p = old_file(tmp_path)
    logged = []
    acct = Account([REAL, OTHER])
    ds = channels.migrate(acct, p, logged.append)
    assert ds[0].kind == "ambiguous"
    assert any(kf.FPR_K1 in line and kf.FPR_K2 in line for line in logged)
    assert not channels.check(acct, 110, p) and not channels.check(acct, 111, p)


def test_migrate_never_runs_on_a_marked_file(tmp_path):
    p = write(tmp_path, f"owner  {BOB}  # owner\n")
    before = p.read_text()
    assert channels.migrate(Account([REAL]), p, lambda m: None) is None
    assert p.read_text() == before


def test_a_second_start_is_a_no_op_and_keeps_the_backup(tmp_path):
    p = old_file(tmp_path)
    channels.migrate(Account([REAL]), p, lambda m: None)
    after = p.read_text()
    backup = p.with_name("channels.txt.pre-keys")
    backup.write_text("kept")
    assert channels.migrate(Account([REAL]), p, lambda m: None) is None
    assert p.read_text() == after and backup.read_text() == "kept"


def test_only_the_exact_address_is_a_candidate(tmp_path):
    p = old_file(tmp_path, f"owner  {BOB}  # owner\n")
    ds = channels.migrate(Account([kf.Contact(30, "x.bob@example.org", kf.K2)]), p, lambda m: None)
    assert ds[0].kind == "unpinned"


def test_a_file_that_cannot_be_written_is_left_alone(tmp_path):
    p = old_file(tmp_path)
    p.chmod(0o666)
    logged = []
    assert channels.migrate(Account([REAL]), p, logged.append) is None
    assert p.read_text() == OLD and "NOT migrated" in logged[-1]

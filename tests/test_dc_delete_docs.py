"""The docs quote what dc-delete really says (PHONE-122 do-002).

README's `dc-delete` section shows the done lines and a refusal; these tests
make the code write them and check the README quotes them byte for byte. They
also pin the two things the GO asked the docs to say plainly: the bot only
ASKS, and whether a time limit applies is not measured (never "there is
none"). SKILL.md and the CHANGELOG name the verb.
"""
import pathlib

import pytest
from deltachat_rpc_client import Account

from sessionphone import channels
from sessionphone.bridge import bot, retract

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
FLAT = " ".join(README.split())
# the section, from its heading to the next "## " heading
SECTION = README.split("\n## Deleting for everyone: `dc-delete`\n", 1)[1].split("\n## ", 1)[0]


@pytest.fixture
def listing(tmp_path):
    p = tmp_path / "channels.txt"
    p.write_text("twelve  chat:12\n")
    p.chmod(0o600)
    return channels.load(p)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(bot, "log", lambda line: None)     # bot.gate logs through bot.log


MSGS = {5531: (12, 1, False), 5532: (12, 1, False), 5533: (12, 7, False)}


class FakeRpc:
    """Just enough of rpc-server 2.62.0 for delete_for_all (tests/bridge/test_retract.py
    has the full fake): MSGS {id: (chat_id, from_id, is_info)}, camelCase snapshots."""

    def get_existing_msg_ids(self, account_id, ids):
        return [i for i in ids if i in MSGS]

    def get_message(self, account_id, msg_id):
        chat, sender, info = MSGS[msg_id]
        return {"id": msg_id, "chatId": chat, "fromId": sender, "isInfo": info, "text": "hi"}

    def delete_messages_for_all(self, account_id, ids):
        pass


class Manager:
    rpc = FakeRpc()


def account():
    return Account(manager=Manager(), id=3)


def quoted(prefix):
    return [line for line in SECTION.splitlines() if line.startswith(prefix)]


def test_readme_quotes_the_done_lines_the_bot_writes(listing):
    one = retract.delete_for_all(account(), [5531], listing, bot.gate)
    two = retract.delete_for_all(account(), [5531, 5532], listing, bot.gate)
    assert quoted("dc-delete: deleted ") == [f"dc-delete: {one}", f"dc-delete: {two}"]


def test_readme_quotes_a_refusal_the_bot_writes(listing):
    with pytest.raises(channels.Refused) as e:
        retract.delete_for_all(account(), [5533], listing, bot.gate)
    # dc_ack prints a refused ack as "<tool>: FAILED - <detail>"
    assert quoted("dc-delete: FAILED - ") == [f"dc-delete: FAILED - {e.value}"]


def test_readme_says_it_only_asks_and_the_time_limit_is_unmeasured():
    flat = " ".join(SECTION.split())
    assert "can only ASK every member's app" in flat
    assert "may already have shown the message" in flat
    assert "This cannot unsend what was read." in flat
    assert "time limit on the request is **not measured**" in flat
    for claim in ("no time limit", "there is no limit", "any age"):
        assert claim not in flat.lower()


def test_readme_claims_nothing_for_cores_own_refusal():
    # retract.py drops "nothing was deleted" from core's refusal (tail=""): the docs must not add it
    flat = " ".join(SECTION.split())
    assert "what core did before it refused is core's to say, and it is not measured" in flat
    assert "nothing is ever deleted in part" not in flat
    # an outbox's bot.log line for core's refusal claims nothing either
    assert "Nothing was deleted in either case." not in flat
    assert "unless the line is core's own refusal (`REFUSED delete ... core: ...`)" in flat


def test_readme_channels_and_outbox_key():
    assert "`dc-react`, `dc-xdc-update` and `dc-delete` take a message id" in FLAT
    assert "### For an outbox: the `delete` job key" in SECTION
    assert '{"delete": [5531, 5532], "ack": "/path/to/ack.json"}' in SECTION
    # it sits after Voice messages (and its outbox keys), before Watching a call
    order = [README.index(h) for h in ("\n## Voice messages", "\n### For an outbox: the `group` job key",
                                       "\n## Deleting for everyone", "\n## Watching a call")]
    assert order == sorted(order)


def test_skill_and_changelog_name_the_verb():
    skill = (ROOT / "skills/sessionphone/SKILL.md").read_text()
    where = skill.split("## Where the bot may send", 1)[1].split("\n## ", 1)[0]
    assert "`dc-delete MSG_ID...`" in where and "ASKS" in where
    # dc-delete shipped in 0.2.0: its section, not [Unreleased] (only what came after)
    unreleased = (ROOT / "CHANGELOG.md").read_text().split("## [0.2.0]", 1)[1].split("\n## [", 1)[0]
    added = unreleased.split("### Added", 1)[1].split("\n### ", 1)[0]
    lines = [line for line in added.splitlines() if "dc-delete" in line]
    assert len(lines) == 1 and "(PHONE-122)" in lines[0]
    # one of the [0.2.0] ### Added bullets; not the last since PHONE-126: the next feature appends after it
    bullets = [line for line in added.splitlines() if line.startswith("- ")]
    assert lines[0] in bullets

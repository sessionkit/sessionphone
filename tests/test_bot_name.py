"""The bot's Delta Chat name follows its agent, unless the user set one.

A bot replaced with `bot install --replace claude` by `versa` kept the name
`setup --name Claude` once gave the account, so it came online as "Claude".
Now the bot settles its name at its start, the one time it holds the account:
DC_NAME when set, else the agent's name title-cased, but only over a name
nobody chose (empty, or the name of the agent the bot is or replaced). Any other
name is the user's and is kept. When the name changes, the saved invite is
written again, since it carries the name (`n=`) and `invite`/`qr` read it.

Fakes only: a fake account, the suite's fake launchd; no Delta Chat core.
"""
import os
import pathlib
import plistlib
import stat
import subprocess
import sys
import urllib.parse

import pytest
from click.testing import CliRunner
from test_bot_online_as import FakeAccount as OnlineAccount
from test_bot_online_as import Online, stub_main
from test_one_bot_one_root import rig  # noqa: F401 - the --replace rig, a fixture

from sessionphone import account, displayname, service
from sessionphone.bridge import bot, dc_env
from sessionphone.cli import main as cli

FP = "OPENPGP4FPR:0123456789ABCDEF0123456789ABCDEF01234567"


def link(name: str) -> str:
    """An invite as core writes one, carrying NAME as its n=."""
    return (f"https://i.delta.chat/#{FP[12:]}&v=3&i=aaaa&s=bbbb&a=bot%40relay.example.org"
            f"&n={urllib.parse.quote(name)}")


class Acct(OnlineAccount):
    """displayname as core keeps it; get_qr_code carries the current name."""

    def __init__(self, name: str = ""):
        super().__init__([{"addr": "bot@relay.example.org"}], {"displayname": name})
        self.sets: list[tuple[str, str]] = []
        self.qr = 0

    def set_config(self, key, value):
        self.sets.append((key, value))
        self._config[key] = value

    def get_qr_code(self):
        self.qr += 1
        return link(self._config.get("displayname") or "")


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    r.mkdir(mode=0o700)
    return r


def saved_invite(root: pathlib.Path, name: str) -> pathlib.Path:
    p = root / "invite.txt"
    p.write_text(link(name) + "\n")
    return p


def invite_name(root: pathlib.Path) -> str:
    return account.invite_name((root / "invite.txt").read_text().strip())


# ---- the rule -------------------------------------------------------------------------------

@pytest.mark.parametrize("agent, want", [("versa", "Versa"), ("claude", "Claude"),
                                         ("my-bot", "My-Bot")])
def test_the_default_is_the_agent_title_cased(agent, want):
    assert displayname.default_for(agent) == want


def test_an_empty_name_gets_the_agent_title_cased():
    assert displayname.wanted("", "versa") == "Versa"
    assert displayname.wanted("   ", "versa") == "Versa"


def test_the_replaced_agents_name_gives_way_to_the_new_one():
    assert displayname.wanted("Claude", "versa", replaced=("claude",)) == "Versa"


def test_a_default_is_matched_without_regard_to_case():
    assert displayname.wanted("claude", "versa", replaced=("claude",)) == "Versa"
    assert displayname.wanted("versa", "versa") == "Versa"


def test_a_hand_set_name_survives_a_replace():
    assert displayname.wanted("Ada Lovelace", "versa", replaced=("claude",)) is None


def test_served_is_narrow_an_older_agent_is_not_remembered():
    # only the current agent and the one --replace retired count as defaults
    assert displayname.wanted("Claude", "versa") is None
    assert displayname.wanted("Claude", "versa", replaced=("ada",)) is None


def test_dc_name_beats_both_a_default_and_a_hand_set_name():
    assert displayname.wanted("Claude", "versa", "Robin", ("claude",)) == "Robin"
    assert displayname.wanted("Ada Lovelace", "versa", "Robin", ("claude",)) == "Robin"
    assert displayname.wanted("", "versa", "  Robin ") == "Robin"


def test_the_change_line_quotes_for_a_shell():
    assert displayname.change_line("Versa") == "DC_NAME='Versa' sessionphone bot install"
    assert displayname.change_line("Bob's bot") == "DC_NAME='Bob'\\''s bot' sessionphone bot install"


# ---- what is stored, and where --------------------------------------------------------------

def test_the_record_of_a_replace_is_one_file_under_the_root_0600(root):
    displayname.record_replaced(root, {"Claude": "/r"})
    p = root / "name-replaced"
    assert p.read_text() == "claude\n"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert displayname.read_replaced(root) == ("claude",)
    assert [x.name for x in root.iterdir()] == ["name-replaced"]     # no .part left


def test_a_later_replace_overwrites_the_record(root):
    displayname.record_replaced(root, ["claude"])
    displayname.record_replaced(root, ["ada"])
    assert displayname.read_replaced(root) == ("ada",)


def test_no_replace_writes_no_record(root):
    displayname.record_replaced(root, [])
    assert not (root / "name-replaced").exists()


def test_no_record_reads_as_none(root):
    assert displayname.read_replaced(root) == ()


# ---- the bot's start: displayname.at_start --------------------------------------------------

def test_claude_becomes_versa_after_a_replace(root):
    saved_invite(root, "Claude")
    displayname.record_replaced(root, ["claude"])
    acct = Acct("Claude")
    said = displayname.at_start(acct, root, "versa")
    assert acct.sets == [("displayname", "Versa")]
    assert said == "name: 'Claude' -> 'Versa' (the agent's name); the saved invite is updated"
    assert not (root / "name-replaced").exists()       # nothing remembered beyond it


def test_the_saved_invite_carries_the_new_name(root):
    saved_invite(root, "Claude")
    displayname.record_replaced(root, ["claude"])
    displayname.at_start(Acct("Claude"), root, "versa")
    assert invite_name(root) == "Versa"
    assert stat.S_IMODE((root / "invite.txt").stat().st_mode) == 0o600


def test_invite_and_qr_read_the_new_name(root, monkeypatch):
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.delenv("DC_ROOT", raising=False)
    saved_invite(root, "Claude")
    displayname.record_replaced(root, ["claude"])
    displayname.at_start(Acct("Claude"), root, "versa")
    assert account.display_name() == "Versa"           # what `qr` names the page after
    assert account.invite() == link("Versa")           # what `invite` shows


def test_a_hand_set_name_survives_the_replace_and_says_how_to_change_it(root):
    saved_invite(root, "Ada Lovelace")
    displayname.record_replaced(root, ["claude"])
    acct = Acct("Ada Lovelace")
    said = displayname.at_start(acct, root, "versa")
    assert acct.sets == [] and acct.qr == 0
    assert invite_name(root) == "Ada Lovelace"
    assert said.startswith("name: kept 'Ada Lovelace'")
    assert said.endswith("to change it: DC_NAME='Versa' sessionphone bot install")
    assert not (root / "name-replaced").exists()


def test_dc_name_beats_a_hand_set_name_at_start(root):
    saved_invite(root, "Ada Lovelace")
    acct = Acct("Ada Lovelace")
    said = displayname.at_start(acct, root, "versa", "Robin")
    assert acct.sets == [("displayname", "Robin")]
    assert said == "name: 'Ada Lovelace' -> 'Robin' (DC_NAME); the saved invite is updated"
    assert invite_name(root) == "Robin"


def test_dc_name_beats_the_agents_name_after_a_replace(root):
    saved_invite(root, "Claude")
    displayname.record_replaced(root, ["claude"])
    acct = Acct("Claude")
    displayname.at_start(acct, root, "versa", "Robin")
    assert acct.sets == [("displayname", "Robin")]
    assert invite_name(root) == "Robin"


def test_an_empty_name_gets_the_agents_at_start(root):
    saved_invite(root, "")
    acct = Acct("")
    said = displayname.at_start(acct, root, "versa")
    assert acct.sets == [("displayname", "Versa")]
    assert said == "name: set 'Versa' (the agent's name); the saved invite is updated"
    assert invite_name(root) == "Versa"


def test_the_right_name_already_changes_nothing_and_says_nothing(root):
    p = saved_invite(root, "Versa")
    before = p.read_bytes()
    acct = Acct("Versa")
    assert displayname.at_start(acct, root, "versa") is None
    assert acct.sets == [] and acct.qr == 0
    assert p.read_bytes() == before


def test_a_stale_saved_invite_is_brought_in_line(root):
    saved_invite(root, "Claude")
    acct = Acct("Versa")
    said = displayname.at_start(acct, root, "versa")
    assert acct.sets == []
    assert said == "name: the saved invite said 'Claude'; it is updated to 'Versa'"
    assert invite_name(root) == "Versa"


def test_with_no_saved_invite_the_name_still_changes_and_an_invite_is_saved(root):
    acct = Acct("")
    displayname.at_start(acct, root, "versa")
    assert acct.sets == [("displayname", "Versa")]
    assert invite_name(root) == "Versa"


def test_a_core_failure_keeps_the_record_for_the_next_start(root):
    displayname.record_replaced(root, ["claude"])

    class Refusing(Acct):
        def set_config(self, key, value):
            raise RuntimeError("core said no")

    with pytest.raises(RuntimeError):
        displayname.at_start(Refusing("Claude"), root, "versa")
    assert displayname.read_replaced(root) == ("claude",)


# ---- bot.main(): the name is settled before the "online as" line ----------------------------

def start_lines(monkeypatch, tmp_path, acct, root, agent="versa", chosen="") -> list[str]:
    lines = []

    def log(msg):
        lines.append(msg)
        if msg.startswith("online as"):
            raise Online(msg)

    stub_main(monkeypatch, tmp_path, acct, log)
    monkeypatch.setattr(dc_env, "ROOT", root)
    monkeypatch.setattr(dc_env, "AGENT", agent)
    monkeypatch.setattr(dc_env, "NAME", chosen)
    with pytest.raises(Online):
        bot.main()
    return lines


def test_the_bot_comes_online_as_versa_after_replacing_claude(monkeypatch, tmp_path, root):
    saved_invite(root, "Claude")
    displayname.record_replaced(root, ["claude"])
    lines = start_lines(monkeypatch, tmp_path, Acct("Claude"), root)
    assert "name: 'Claude' -> 'Versa' (the agent's name); the saved invite is updated" in lines
    assert lines[-1] == "online as Versa <bot@relay.example.org> - listening"


def test_the_bot_comes_online_as_dc_name(monkeypatch, tmp_path, root):
    lines = start_lines(monkeypatch, tmp_path, Acct("Ada Lovelace"), root, chosen="Robin")
    assert lines[-1] == "online as Robin <bot@relay.example.org> - listening"


def test_a_failing_name_never_stops_the_bot(monkeypatch, tmp_path, root):
    class Refusing(Acct):
        def set_config(self, key, value):
            raise RuntimeError("core said no")

    lines = start_lines(monkeypatch, tmp_path, Refusing(""), root)
    assert "name: not changed: core said no" in lines
    assert lines[-1].startswith("online as")


# ---- bot install: the record, and one `name:` line --------------------------------------------

def test_install_line_for_each_case(root):
    assert displayname.install_line(root, "versa") == (
        "name:      Versa, the agent's name, set by the bot at its start unless the account "
        "has a name of its own (DC_NAME sets one)")
    saved_invite(root, "Claude")
    assert displayname.install_line(root, "versa", replaced=("claude",)) == (
        "name:      Claude -> Versa, the agent's name, set by the bot at its start "
        "(DC_NAME sets another)")
    assert displayname.install_line(root, "claude") == (
        "name:      Claude, the agent's name (DC_NAME sets another)")
    assert displayname.install_line(root, "versa", "Robin") == (
        "name:      Robin (DC_NAME), set by the bot at its start")
    saved_invite(root, "Ada Lovelace")
    assert displayname.install_line(root, "versa", replaced=("claude",)) == (
        "name:      kept 'Ada Lovelace': it is not the agent's name, so it is taken as yours. "
        "To give the bot its agent's name: DC_NAME='Versa' sessionphone bot install")


def _install(*args):
    return CliRunner().invoke(cli, ["bot", "install", *args])


def _name_line(output: str) -> str:
    [line] = [x for x in output.splitlines() if x.startswith("name:")]
    return line


def test_install_replace_records_the_old_agent_under_the_root(rig):  # noqa: F811
    rig.as_("claude")
    assert _install().exit_code == 0
    saved_invite(rig.root, "Claude")
    rig.as_("versa")
    r = _install("--replace", "claude")
    assert r.exit_code == 0, r.output
    assert displayname.read_replaced(rig.root) == ("claude",)
    assert _name_line(r.output) == ("name:      Claude -> Versa, the agent's name, set by the bot "
                                    "at its start (DC_NAME sets another)")


def test_install_says_it_keeps_a_hand_set_name_and_prints_the_dc_name_line(rig):  # noqa: F811
    rig.as_("claude")
    assert _install().exit_code == 0
    saved_invite(rig.root, "Ada Lovelace")
    rig.as_("versa")
    r = _install("--replace", "claude")
    assert r.exit_code == 0, r.output
    assert "kept 'Ada Lovelace'" in _name_line(r.output)
    assert "DC_NAME='Versa' sessionphone bot install" in _name_line(r.output)


def test_a_plain_install_records_nothing(rig):  # noqa: F811
    rig.as_("claude")
    assert _install().exit_code == 0
    assert not (rig.root / "name-replaced").exists()


def test_dc_name_rides_in_the_job_and_is_saved(rig, monkeypatch):  # noqa: F811
    rig.as_("versa")
    monkeypatch.setenv("DC_NAME", "Robin")
    r = _install()
    assert r.exit_code == 0, r.output
    env = plistlib.loads(service.plist_path("versa").read_bytes())["EnvironmentVariables"]
    assert env["DC_NAME"] == "Robin"
    assert _name_line(r.output) == "name:      Robin (DC_NAME), set by the bot at its start"


def test_a_record_that_cannot_be_written_is_said_not_raised(rig, monkeypatch):  # noqa: F811
    rig.as_("claude")
    assert _install().exit_code == 0
    rig.as_("versa")

    def fail(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(displayname, "record_replaced", fail)
    r = _install("--replace", "claude")
    assert r.exit_code == 0, r.output
    assert "warning: could not record the replaced bot" in r.output


# ---- the setting itself -----------------------------------------------------------------------

def test_dc_env_reads_dc_name(tmp_path):
    # a fresh interpreter: dc_env binds NAME at import, and reloading it here
    # would rebind every other setting under the rest of the suite
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    env.update(DC_NAME="  Robin ", SESSIONPHONE_ROOT=str(tmp_path), HOME=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", "from sessionphone.bridge import dc_env; print(dc_env.NAME)"],
                         env=env, capture_output=True, text=True, check=True).stdout
    assert out == "Robin\n"


def test_bin_dc_env_mirrors_dc_name():
    src = (pathlib.Path(__file__).resolve().parents[1] / "bin/dc-env").read_text()
    assert 'DC_NAME="${DC_NAME:-}"' in src and "export DC_NAME" in src


def test_the_setting_is_named_in_the_docs():
    top = pathlib.Path(__file__).resolve().parents[1]
    for doc in ("README.md", "docs/settings.md", "skills/sessionphone/references/profile.md",
                "CHANGELOG.md"):
        assert "DC_NAME" in (top / doc).read_text(), doc
    assert "name-replaced" in (top / "README.md").read_text()     # what is stored, and where

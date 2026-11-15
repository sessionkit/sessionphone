"""A bot's logs live in a folder of their own, per bot and per profile.

Under the default root that folder is the kit's, ~/.sessionkit/phone/log/<bot>[/<profile>]/,
out of the root. Under any other root (a test bot, a second bot) it is in that
root, <root>/log/<bot>[/<profile>]/, so such a bot never writes into the default
bot's log tree (PHONE-305; the tests for that rule are at the end).

Both of a bot's logs moved: bot.log, the bot's own, and bot.launchd.log, the
job's StandardOutPath and StandardErrorPath. One rule says where
(sessionphone.paths.log_dir), and the bot, the plist, `bot status`, bin/dc-env
and bin/dc-log all ask it. The folder is per bot, and a named profile has its
own inside it, so two bots or two profiles never write one file. DC_LOG still
wins. A job installed before the move keeps the paths its plist names until
`bot install` runs again, and its old logs are never moved or deleted:
service.log_paths reads the older plist, and a reader finds the old bot.log
while it is the only one there. `bot uninstall` removes the plist and no log.

Fakes only: a tmp HOME (conftest.home_is_tmp), a tmp root, conftest's
FakeLaunchd as service._launchctl, plist_path in tmp. No account, no relay, no
real launchd, no mail.
"""
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from _kit_log import kit_log, root_log
from click.testing import CliRunner

from sessionphone import paths, service
from sessionphone.bridge import bot, dc_env
from sessionphone.cli import main as cli

OWNER = "owner@phone.example.org"
SRC = Path(__file__).resolve().parents[1] / "src"


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    """Bot ada's default profile on a tmp root, ready for `bot install`. -> the root."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    given_seat("ada-chat")     # install's signer, given: no seat is made
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "callers.txt").write_text(f"{OWNER}\n")
    (root / "callers.txt").chmod(0o600)
    (root / "channels.txt").write_text(f"owner  {OWNER}\n")
    (root / "channels.txt").chmod(0o600)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setenv("DC_HOST", "mac.example.net")
    monkeypatch.setenv("DC_AGENT", "ada")
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "legacy_plist_path", lambda name=None: tmp_path / "legacy.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(service, "ears_src", lambda: None)
    return root


def _plist(tmp_path) -> dict:
    return plistlib.loads((tmp_path / "job.plist").read_bytes())


def _older_plist(tmp_path, root, **env):
    """A plist as `bot install` wrote it before the move: launchd's log in the root."""
    j = {"Label": service.label("ada"),
         "EnvironmentVariables": {"SESSIONPHONE_ROOT": str(root), "DC_AGENT": "ada", **env},
         "StandardOutPath": str(root / "bot.launchd.log"),
         "StandardErrorPath": str(root / "bot.launchd.log")}
    (tmp_path / "job.plist").write_bytes(plistlib.dumps(j))


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ---- the per-bot log folder --------------------------------------------------------

def test_the_log_folder_is_the_bots_own_under_the_kit(home_is_tmp):
    base = home_is_tmp / ".sessionkit" / "phone" / "log"
    assert paths.log_dir("ada") == base / "ada"
    assert paths.log_dir("ada", "public") == base / "ada" / "public"
    assert paths.bot_log("ada") == base / "ada" / "bot.log" == kit_log("ada")
    assert paths.launchd_log("ada", "public") == base / "ada" / "public" / "bot.launchd.log"
    assert not base.exists()            # asking makes nothing


def test_the_job_writes_launchds_log_in_the_bots_folder(monkeypatch, home_is_tmp):
    monkeypatch.setattr(service, "ears_src", lambda: None)
    given = {"SESSIONPHONE_ROOT": str(home_is_tmp / ".sessionkit/phone/state")}     # the default root
    j = service.job("ada", python="/bin/true", given=given)
    assert j["StandardOutPath"] == j["StandardErrorPath"] == str(kit_log("ada", name="bot.launchd.log"))
    j = service.job("ada", python="/bin/true", given={**given, "DC_PROFILE": "public"})
    assert j["StandardErrorPath"] == str(kit_log("ada", "public", "bot.launchd.log"))
    assert "/state/" not in str(j["StandardErrorPath"])     # out of the root


def _bot_log_bound(env: dict, profile: str) -> str:
    """bot.LOG as a bot started with ENV (and DC_PROFILE=PROFILE) binds it, in a fresh process."""
    probe = "import sys; sys.path.insert(0, sys.argv[1]); from sessionphone.bridge import bot; print(bot.LOG)"
    base = {k: v for k, v in os.environ.items() if not k.startswith(("DC_", "PIP_CHAT_"))}
    return subprocess.run([sys.executable, "-c", probe, str(SRC)],
                          env={**base, **env, "DC_PROFILE": profile, "DC_AGENT": "ada", "DC_HOST": "host.example"},
                          capture_output=True, text=True, timeout=60, check=True).stdout.strip()


def test_the_bot_writes_its_log_in_its_folder(home_is_tmp):
    """Under the default root, the kit's folder; under any other, the root's (PHONE-305)."""
    default = {"DC_ROOT": str(home_is_tmp / ".sessionkit/phone/state")}
    for profile, want in (("", kit_log("ada")), ("public", kit_log("ada", "public"))):
        assert _bot_log_bound(default, profile) == str(want)


def test_the_bots_first_line_makes_its_log_folder_private(monkeypatch, home_is_tmp):
    log = kit_log("ada", "public")
    monkeypatch.setattr(bot, "LOG", log)
    bot.log("the first line on a new host")
    assert log.read_text().endswith(" the first line on a new host\n")
    for d in (home_is_tmp / ".sessionkit", home_is_tmp / ".sessionkit/phone", log.parent.parent.parent,
              log.parent.parent, log.parent):
        assert _mode(d) == 0o700, d


def test_make_log_dir_leaves_a_folder_that_is_there_as_it_is(tmp_path):
    there = tmp_path / "there"
    there.mkdir(mode=0o755)
    there.chmod(0o755)
    paths.make_log_dir(there / "a" / "b")
    assert _mode(there) == 0o755
    assert _mode(there / "a") == _mode(there / "a" / "b") == 0o700


def test_install_makes_launchds_log_folder_before_the_job_loads(rig, tmp_path, launchd, monkeypatch):
    folder = root_log(rig, "ada").parent
    seen = []

    def bootstrap_sees_the_folder(*args):
        if args[0] == "bootstrap":
            seen.append(folder.is_dir())
        return launchd(*args)
    monkeypatch.setattr(service, "_launchctl", bootstrap_sees_the_folder)
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert seen and all(seen)
    assert _mode(folder) == 0o700
    assert _plist(tmp_path)["StandardErrorPath"] == str(root_log(rig, "ada", name="bot.launchd.log"))
    assert f"log:       {root_log(rig, 'ada')}" in r.output
    assert "DC_LOG" not in _plist(tmp_path)["EnvironmentVariables"]    # the rule decides, not a pin


# ---- DC_LOG still wins -------------------------------------------------------------

def test_dc_log_wins_for_the_bot_and_its_readers(rig, tmp_path, monkeypatch):
    mine = tmp_path / "elsewhere" / "mine.log"
    assert dc_env.log_file({"DC_LOG": str(mine)}, agent="ada") == mine
    assert dc_env.log_file({"PIP_CHAT_LOG": str(mine)}, agent="ada") == mine    # the old name too
    _write(root_log(rig, "ada"), "the log the rule names\n")
    assert dc_env.log_for_reader({"DC_LOG": str(mine)}, agent="ada") == mine    # even when absent
    monkeypatch.setenv("DC_LOG", str(mine))
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert _plist(tmp_path)["EnvironmentVariables"]["DC_LOG"] == str(mine)
    assert service.log_paths("ada")[0] == mine
    assert f"log:       {mine}" in r.output


# ---- an older plist: its logs are still found, where it writes them -----------------

def test_log_paths_reads_an_older_plist(rig, tmp_path):
    old_log = _write(rig / "bot.log", "the old bot's line\n")
    _older_plist(tmp_path, rig)
    assert service.log_paths("ada") == (old_log, rig / "bot.launchd.log")


def test_log_paths_reads_an_older_profile_plist(rig, tmp_path):
    old_log = _write(rig / "x" / "bot.log", "the old profile bot's line\n")
    _write(rig / "bot.log", "the default profile's\n")
    _older_plist(tmp_path, rig, DC_PROFILE="x")
    assert service.log_paths("ada") == (old_log, rig / "bot.launchd.log")


def test_an_older_plist_whose_bot_writes_the_new_log_is_read_there(rig, tmp_path):
    """The package was upgraded under a job not installed again: the restarted
    bot writes the new log, and that one is read; launchd's stays the plist's."""
    _write(rig / "bot.log", "before the upgrade\n")
    new = _write(root_log(rig, "ada"), "after the upgrade\n")
    _older_plist(tmp_path, rig)
    assert service.log_paths("ada") == (new, rig / "bot.launchd.log")


def test_bot_status_of_an_older_job_shows_its_old_logs(rig, tmp_path, launchd):
    _older_plist(tmp_path, rig)
    _write(rig / "bot.launchd.log", "sessionphone bot: REFUSED to start: the old job's refusal\n")
    s = CliRunner().invoke(cli, ["bot", "status"])
    assert f"log:     {rig / 'bot.launchd.log'}" in s.output, s.output
    assert "the old job's refusal" in s.output


def test_with_no_plist_log_paths_is_the_new_folder(rig):
    assert service.log_paths("ada") == (root_log(rig, "ada"), root_log(rig, "ada", name="bot.launchd.log"))


# ---- nothing moves, nothing is deleted ---------------------------------------------

def _all_logs(root):
    return {p: p.read_bytes() for p in (
        _write(root / "bot.log", "old bot.log\n"),
        _write(root / "bot.launchd.log", "old bot.launchd.log\n"),
        _write(root_log(root, "ada"), "new bot.log\n"),
        _write(root_log(root, "ada", name="bot.launchd.log"), "new bot.launchd.log\n"))}


def test_install_over_an_older_job_leaves_its_old_logs_where_they_are(rig, tmp_path):
    _older_plist(tmp_path, rig)
    old = {p: p.read_bytes() for p in (_write(rig / "bot.log", "old bot.log\n"),
                                        _write(rig / "bot.launchd.log", "old bot.launchd.log\n"))}
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert {p: p.read_bytes() for p in old} == old
    assert _plist(tmp_path)["StandardErrorPath"] == str(root_log(rig, "ada", name="bot.launchd.log"))


def test_uninstall_removes_the_plist_and_no_log(rig, tmp_path, launchd):
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    logs = _all_logs(rig)
    r = CliRunner().invoke(cli, ["bot", "uninstall"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "job.plist").exists()
    assert {p: p.read_bytes() for p in logs} == logs     # every log, old and new, as it was


def test_uninstall_of_an_older_job_removes_no_log(rig, tmp_path, launchd):
    _older_plist(tmp_path, rig)
    logs = _all_logs(rig)
    service.uninstall("ada")
    assert not (tmp_path / "job.plist").exists()
    assert {p: p.read_bytes() for p in logs} == logs


# ---- two bots, two profiles: never one file ----------------------------------------

def test_two_profiles_of_one_bot_never_share_a_log(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ears_src", lambda: None)
    given = {"SESSIONPHONE_ROOT": str(tmp_path / "root")}
    default = service.job("ada", python="/bin/true", given=given)
    public = service.job("ada", python="/bin/true", given={**given, "DC_PROFILE": "public"})
    assert default["StandardErrorPath"] != public["StandardErrorPath"]
    a, b = dc_env.log_file({}, agent="ada"), dc_env.log_file({"DC_PROFILE": "public"}, agent="ada")
    assert a != b and a.parent != b.parent                              # each in its own folder
    assert {a.parent, b.parent} == {paths.log_dir("ada"), paths.log_dir("ada", "public")}


def test_two_bots_never_share_a_log(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ears_src", lambda: None)
    given = {"SESSIONPHONE_ROOT": str(tmp_path / "root")}
    ada, bob = service.job("ada", python="/bin/true", given=given), service.job("bob", python="/bin/true", given=given)
    assert ada["StandardErrorPath"] != bob["StandardErrorPath"]
    assert dc_env.log_file({}, agent="ada") != dc_env.log_file({}, agent="bob")


# ---- PHONE-305: a bot under a root of its own keeps its logs in that root -----------
# The default root (DEFAULT_ROOT or OLD_ROOT, by path) keeps the kit's folder; any
# other root, from DC_ROOT, SESSIONPHONE_ROOT or a root phone.json keeps, writes
# <root>/log/<bot>[/<profile>]/, and nothing under ~/.sessionkit/phone/log.

def _kit_log_tree(home: Path) -> Path:
    return home / ".sessionkit" / "phone" / "log"


@pytest.fixture
def own_root(monkeypatch, tmp_path, home_is_tmp):
    """A root that is not the default, in this shell's SESSIONPHONE_ROOT. -> it."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    root = tmp_path / "testroot" / "state"
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setattr(service, "ears_src", lambda: None)
    return root


def test_a_root_not_the_default_keeps_both_logs_under_it(own_root, home_is_tmp):
    assert paths.log_dir("ada", root=own_root) == own_root / "log" / "ada"
    assert dc_env.log_file(agent="ada") == root_log(own_root, "ada")
    assert dc_env.log_for_reader(agent="ada") == root_log(own_root, "ada")
    j = service.job("ada", python="/bin/true")
    assert j["StandardOutPath"] == j["StandardErrorPath"] == str(root_log(own_root, "ada", name="bot.launchd.log"))
    assert service.log_paths("ada") == (root_log(own_root, "ada"), root_log(own_root, "ada", name="bot.launchd.log"))
    assert not _kit_log_tree(home_is_tmp).exists()


def test_a_bot_under_its_own_root_writes_nothing_in_the_kits_log_tree(own_root, home_is_tmp, monkeypatch):
    """The bot's first line, and the folder install makes for launchd's log, both land under the root."""
    monkeypatch.setattr(bot, "LOG", dc_env.log_file(agent="ada"))
    bot.log("a test bot's first line")
    assert root_log(own_root, "ada").read_text().endswith(" a test bot's first line\n")
    paths.make_log_dir(Path(service.job("ada", python="/bin/true")["StandardErrorPath"]).parent)
    assert root_log(own_root, "ada").parent.is_dir() and _mode(root_log(own_root, "ada").parent) == 0o700
    assert not _kit_log_tree(home_is_tmp).exists()


def test_install_under_its_own_root_writes_nothing_in_the_kits_log_tree(rig, tmp_path, home_is_tmp):
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    assert _plist(tmp_path)["StandardErrorPath"] == str(root_log(rig, "ada", name="bot.launchd.log"))
    assert f"log:       {root_log(rig, 'ada')}" in r.output
    assert root_log(rig, "ada").parent.is_dir()
    assert not _kit_log_tree(home_is_tmp).exists()


def test_dc_root_wins_over_sessionphone_root_for_the_logs(own_root, tmp_path, home_is_tmp):
    other = tmp_path / "dc-root"
    assert dc_env.log_file({"DC_ROOT": str(other), "SESSIONPHONE_ROOT": str(own_root)},
                           agent="ada") == root_log(other, "ada")
    j = service.job("ada", python="/bin/true", given={"SESSIONPHONE_ROOT": str(own_root), "DC_ROOT": str(other)})
    assert j["StandardErrorPath"] == str(root_log(other, "ada", name="bot.launchd.log"))
    for profile in ("", "public"):        # the bot itself, as it binds LOG at import
        assert _bot_log_bound({"DC_ROOT": str(other)}, profile) == str(root_log(other, "ada", profile))
    assert not _kit_log_tree(home_is_tmp).exists()


def test_a_root_phone_json_keeps_is_followed_too(monkeypatch, tmp_path, home_is_tmp):
    import json

    from sessionphone import settings
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("SESSIONPHONE_ROOT")
    monkeypatch.setenv("DC_AGENT", "ada")
    root = tmp_path / "kept-root"
    Path(settings.FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.FILE).write_text(json.dumps({"bots": {"ada": {"SESSIONPHONE_ROOT": str(root)}}}))
    assert paths.root() == root                                   # the file's root is the root
    assert dc_env.log_file(agent="ada") == root_log(root, "ada")
    assert dc_env.log_for_reader(agent="ada") == root_log(root, "ada")
    assert service.log_paths("ada")[1] == root_log(root, "ada", name="bot.launchd.log")   # no plist
    assert not _kit_log_tree(home_is_tmp).exists()


def test_a_named_profile_has_its_own_folder_under_the_root(own_root):
    a, b = dc_env.log_file(agent="ada"), dc_env.log_file({"DC_PROFILE": "public"}, agent="ada")
    assert b == root_log(own_root, "ada", "public") and a.parent != b.parent
    j = service.job("ada", python="/bin/true", given={"SESSIONPHONE_ROOT": str(own_root), "DC_PROFILE": "public"})
    assert j["StandardErrorPath"] == str(root_log(own_root, "ada", "public", "bot.launchd.log"))


def test_dc_log_still_wins_under_a_root_of_its_own(own_root, tmp_path):
    mine = tmp_path / "mine.log"
    assert dc_env.log_file({"DC_LOG": str(mine)}, agent="ada") == mine
    assert dc_env.log_for_reader({"DC_LOG": str(mine)}, agent="ada") == mine


def test_an_old_log_in_its_own_root_is_still_read_while_it_is_the_only_one(own_root):
    old = _write(own_root / "bot.log", "the bot before this version\n")
    assert dc_env.log_for_reader(agent="ada") == old
    new = _write(root_log(own_root, "ada"), "the bot on this version\n")
    assert dc_env.log_for_reader(agent="ada") == new
    assert old.read_text() == "the bot before this version\n"      # never moved or deleted


@pytest.mark.parametrize("spelt", ["~/.sessionkit/phone/state", "{home}/.sessionkit/phone/state",
                                   "{home}/.sessionkit/phone/state/", "~/.sessionphone",
                                   "{home}/./.sessionkit/phone/../phone/state"])
def test_the_default_root_named_any_way_keeps_the_kits_folder(spelt, home_is_tmp):
    root = spelt.format(home=home_is_tmp)
    assert paths.is_default_root(root)
    assert paths.log_dir("ada", root=root) == _kit_log_tree(home_is_tmp) / "ada"
    assert dc_env.log_file({"SESSIONPHONE_ROOT": root}, agent="ada") == kit_log("ada")
    j = service.job("ada", python="/bin/true", given={"SESSIONPHONE_ROOT": root, "DC_PROFILE": "public"})
    assert j["StandardErrorPath"] == str(kit_log("ada", "public", "bot.launchd.log"))


def test_a_link_to_the_default_root_is_the_default(home_is_tmp, tmp_path):
    real = home_is_tmp / ".sessionkit" / "phone" / "state"
    real.mkdir(parents=True)
    link = tmp_path / "link-to-default"
    link.symlink_to(real)
    assert paths.is_default_root(link)
    assert paths.bot_log("ada", root=link) == kit_log("ada")
    # and the default root reached through a link is the default too
    (tmp_path / "home-link").symlink_to(home_is_tmp)
    assert paths.is_default_root(tmp_path / "home-link" / ".sessionkit" / "phone" / "state")


def test_a_root_beside_or_inside_the_default_is_not_the_default(home_is_tmp):
    for other in (home_is_tmp / ".sessionkit" / "phone-287-test" / "state",
                  home_is_tmp / ".sessionkit" / "phone" / "state" / "x",
                  home_is_tmp / ".sessionkit" / "phone"):
        assert not paths.is_default_root(other), other
        assert paths.log_dir("ada", root=other) == other / "log" / "ada"


def test_no_root_to_judge_is_the_kits_folder(monkeypatch, home_is_tmp):
    from sessionphone import settings
    assert paths.log_dir("ada") == paths.log_dir("ada", root=None) == _kit_log_tree(home_is_tmp) / "ada"

    def bad():
        raise settings.Bad("phone.json cannot be read")
    monkeypatch.delenv("SESSIONPHONE_ROOT")
    monkeypatch.setattr(dc_env.paths, "root", bad)
    assert dc_env.log_root({}) is None
    assert dc_env.log_file({}, agent="ada") == kit_log("ada")
    assert dc_env.log_for_reader({}, agent="ada") == kit_log("ada")

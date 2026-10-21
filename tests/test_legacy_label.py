"""PHONE-193: the shipped launchd label is dev.sessionkit.phone.bot.<agent>.

Every label the kit ships is dev.sessionkit.<package>.<job>. sessionphone
0.1.0 installed its bot as com.sessionphone.bot.<agent>, so an upgraded host
would keep that old job running beside the new one: two bots on one account.
`bot install` now retires the legacy job for the SAME agent (that one exact
label, never a pattern): booted out if launchd has it, its plist removed if it
exists, one printed line per act. Before it writes or boots out anything, it
refuses if the old plist's EnvironmentVariables hold a setting the new job
would drop or change (the carry-over guard): a rename must never silently move
a bot's calls dir or its recording switch. Only service.REGENERATED_KEYS,
which install always rebuilds from the host, may differ. A legacy job loaded
with its plist gone refuses too (nothing to compare), and one that launchd
still lists after its bootout stops the install before the new job starts. `bot uninstall`
removes both jobs; `bot status` names the new label and any legacy job.

launchd is tests/conftest.py's FakeLaunchd and HOME is a tmp dir: no test
here runs the real launchctl or touches a real ~/Library.
"""
import os
import pathlib
import plistlib

import pytest
from click.testing import CliRunner

from sessionphone import service
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

OLD = "com.sessionphone.bot.ada"        # the 0.1.0 label, spelled out once, on purpose
NEW = "dev.sessionkit.phone.bot.ada"
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def rig(monkeypatch, tmp_path, launchd, given_seat):
    for k in [k for k in os.environ if k.startswith("DC_")]:
        monkeypatch.delenv(k)
    given_seat("ada-chat")     # install's signer, given: no seat is made
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for k in ("XDG_BIN_HOME", "XDG_DATA_HOME", "UV_TOOL_BIN_DIR"):   # no ears shim found
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DC_AGENT", "ada")
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(service.paths, "ensure_private", lambda *a, **k: None)
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    callers = tmp_path / "callers.txt"
    callers.write_text("owner@phone.example.org\n")
    monkeypatch.setenv("DC_CALLERS_FILE", str(callers))
    chans = tmp_path / "channels.txt"
    chans.write_text("owner  owner@phone.example.org\n")
    chans.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(chans))
    launchd.loaded = False             # no new job yet unless a test says so
    launchd.home = home
    return launchd


def agents(rig) -> pathlib.Path:
    return rig.home / "Library" / "LaunchAgents"


def write_legacy(rig, **env_changes) -> pathlib.Path:
    """A legacy plist as 0.1.0 wrote it from this same shell (the job it would
    write now, under the old label), with ENV_CHANGES applied (None drops)."""
    j = service.job("ada", python="/venv/libexec/session-phone")
    j["Label"] = OLD
    for k, v in env_changes.items():
        if v is None:
            j["EnvironmentVariables"].pop(k, None)
        else:
            j["EnvironmentVariables"][k] = v
    p = agents(rig) / f"{OLD}.plist"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(plistlib.dumps(j))
    return p


def legacy_calls(rig) -> list[tuple]:
    return [c for c in rig.calls if c[-1].endswith("/" + OLD)]


def install(rig) -> list[str]:
    said: list[str] = []
    service.install("ada", say=said.append)
    return said


# ---- the label ------------------------------------------------------------------

def test_the_label_and_plist_are_the_kit_spelling(rig):
    assert service.label("ada") == NEW
    assert service.plist_path("ada") == agents(rig) / f"{NEW}.plist"
    assert service.legacy_label("ada") == OLD
    assert service.legacy_plist_path("ada") == agents(rig) / f"{OLD}.plist"


def test_the_old_spelling_lives_in_src_only_as_the_legacy_constant():
    hits = [(p.relative_to(SRC), n, line.strip())
            for p in sorted(SRC.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1)
            if "com.sessionphone.bot" in line]
    assert [(str(p), line) for p, _, line in hits] == [
        ("sessionphone/service.py", 'LEGACY_LABEL_PREFIX = "com.sessionphone.bot"')], hits


def test_install_writes_the_new_label(rig):
    p = service.install("ada")
    assert p == agents(rig) / f"{NEW}.plist"
    assert plistlib.loads(p.read_bytes())["Label"] == NEW
    assert rig.calls[-1] == ("print", f"{service.domain()}/{NEW}")


# ---- install retires the legacy job: the five cases, and the re-run --------------

def test_old_only_loaded_is_booted_out(rig):
    rig.legacy = {OLD}
    old = write_legacy(rig)
    assert install(rig) == [f"retired legacy launchd job {OLD} (booted out)",
                            f"retired legacy launchd job {OLD} (plist removed)"]
    assert rig.legacy == set() and rig.loaded
    assert ("bootout", f"{service.domain()}/{OLD}") in rig.calls
    assert not old.exists()


def test_old_loaded_with_its_plist_gone_refuses_and_boots_nothing_out(rig):
    """Its env cannot be compared, so the guard cannot clear it."""
    rig.legacy = {OLD}
    chans = rig.home.parent / "channels.txt"
    before = chans.read_bytes()
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    msg = str(e.value)
    assert f"legacy job {OLD} is loaded but its plist is gone: its env cannot be compared" in msg
    assert "`sessionphone bot uninstall` first" in msg
    assert rig.legacy == {OLD}
    assert not [c for c in rig.calls if c[0] in ("bootout", "bootstrap")]
    assert not agents(rig).exists() or not list(agents(rig).iterdir())
    assert chans.read_bytes() == before                     # the list untouched


def test_the_orphan_check_comes_before_the_owner_is_written(rig, tmp_path):
    rig.legacy = {OLD}                         # loaded, no plist
    callers = tmp_path / "callers.txt"
    before = callers.read_bytes()
    with pytest.raises(service.LegacyCarryOver, match="its plist is gone"):
        service.install("ada", owner="new-owner@phone.example.org")
    assert callers.read_bytes() == before and rig.legacy == {OLD}
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2 and "its plist is gone" in r.output
    assert uninstall(rig) == (0, [f"retired legacy launchd job {OLD} (booted out)"])   # the way through
    assert install(rig) == []


def test_old_only_plist_is_removed_and_nothing_booted_out(rig):
    old = write_legacy(rig)
    assert install(rig) == [f"retired legacy launchd job {OLD} (plist removed)"]
    assert not old.exists()
    assert {c[0] for c in legacy_calls(rig)} == {"print"}      # looked, booted nothing out
    assert rig.loaded


def test_new_only_retires_nothing(rig):
    rig.loaded = True                      # a reinstall of the new job
    assert install(rig) == []
    assert {c[0] for c in legacy_calls(rig)} == {"print"}   # looked only
    assert rig.loaded and ("bootout", f"{service.domain()}/{NEW}") in rig.calls


def test_both_retire_the_old_and_replace_the_new(rig):
    rig.loaded, rig.legacy = True, {OLD}
    old = write_legacy(rig)
    assert install(rig) == [f"retired legacy launchd job {OLD} (booted out)",
                            f"retired legacy launchd job {OLD} (plist removed)"]
    assert not old.exists() and rig.legacy == set() and rig.loaded
    verbs = [(c[0], c[-1].rsplit("/", 1)[-1]) for c in rig.calls if c[0] in ("bootout", "bootstrap")]
    # the old job goes BEFORE the new one is written and loaded
    assert verbs[0] == ("bootout", OLD) and verbs[-1][0] == "bootstrap"


def test_neither_is_a_plain_fresh_install(rig):
    assert install(rig) == []
    assert {c[0] for c in legacy_calls(rig)} == {"print"}   # looked only
    assert rig.loaded


def test_a_rerun_after_retiring_is_a_no_op_for_the_legacy_job(rig):
    rig.legacy = {OLD}
    write_legacy(rig)
    assert len(install(rig)) == 2
    before = len(rig.calls)
    assert install(rig) == []
    assert [c[0] for c in legacy_calls(rig)[-1:]] == ["print"]
    assert not any(c[0] == "bootout" and c[-1].endswith(OLD) for c in rig.calls[before:])


def test_only_this_agents_exact_legacy_job_is_retired(rig):
    """Never a pattern: another agent's legacy job, and a label phone never
    wrote, are left alone."""
    rig.legacy = {"com.sessionphone.bot.bob"}
    other = agents(rig) / "com.sessionphone.bot.bob.plist"
    hand = agents(rig) / "com.sessionphone.bot.ada.extra.plist"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    hand.write_bytes(b"x")
    assert install(rig) == []
    assert other.exists() and hand.exists() and rig.legacy == {"com.sessionphone.bot.bob"}


def sticky_legacy(rig, monkeypatch, rounds: int | None = None):
    """launchd that answers the legacy bootout 0 but keeps listing the old job:
    for ROUNDS more bootouts, or for good when ROUNDS is None."""
    left = [rounds]

    def launchctl(*args):
        if args[0] == "bootout" and args[-1].endswith("/" + OLD) and left[0] != 0:
            rig.calls.append(args)
            if left[0] is not None:
                left[0] -= 1
            return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return rig(*args)
    monkeypatch.setattr(service, "_launchctl", launchctl)


def test_a_legacy_job_that_lingers_stops_the_install_before_the_new_job(rig, monkeypatch):
    """Never bootstrap the new job while launchd lists the old."""
    rig.legacy = {OLD}
    old = write_legacy(rig)
    before = old.read_bytes()
    sticky_legacy(rig, monkeypatch)
    said: list[str] = []
    with pytest.raises(service.LegacyStillLoaded) as e:
        service.install("ada", say=said.append)
    msg = str(e.value)
    assert f"the legacy launchd job {OLD} is still going" in msg
    assert f"the new job {NEW} was NOT started and its plist was not written" in msg
    assert f"its plist {old} is kept" in msg and "Re-run `sessionphone bot install`" in msg
    assert said == []                                       # nothing retired, nothing said as if it were
    assert old.read_bytes() == before and rig.legacy == {OLD}
    assert not (agents(rig) / f"{NEW}.plist").exists()
    assert not [c for c in rig.calls if c[0] == "bootstrap"]
    assert not [c for c in rig.calls if c[0] == "bootout" and c[-1].endswith("/" + NEW)]
    r = CliRunner().invoke(cli, ["bot", "install"])          # the operator's view: non-zero
    assert r.exit_code == 1 and "is still going" in r.output and "installed:" not in r.output


def test_a_rerun_once_the_lingering_legacy_job_is_gone_completes(rig, monkeypatch):
    rig.legacy = {OLD}
    old = write_legacy(rig)
    sticky_legacy(rig, monkeypatch, rounds=1)
    with pytest.raises(service.LegacyStillLoaded):
        service.install("ada")
    assert install(rig) == [f"retired legacy launchd job {OLD} (booted out)",
                            f"retired legacy launchd job {OLD} (plist removed)"]
    assert not old.exists() and rig.legacy == set() and rig.loaded


def test_a_lingering_legacy_job_says_what_this_run_wrote(rig, monkeypatch, tmp_path):
    (tmp_path / "channels.txt").write_text("")            # empty: --owner seeds the owner line
    rig.legacy = {OLD}
    write_legacy(rig)
    sticky_legacy(rig, monkeypatch)
    with pytest.raises(service.LegacyStillLoaded) as e:
        service.install("ada", owner="owner@phone.example.org")
    assert (f"Kept from this run: owner@phone.example.org approved in {tmp_path / 'callers.txt'}; "
            f"channel owner owner@phone.example.org listed in {tmp_path / 'channels.txt'}.") in str(e.value)


# ---- the carry-over guard --------------------------------------------------------

def assert_nothing_written(rig, old: pathlib.Path, before: bytes) -> None:
    assert old.read_bytes() == before                         # the legacy plist untouched
    assert not (agents(rig) / f"{NEW}.plist").exists()        # no new plist
    assert not (agents(rig) / f"{NEW}.plist.part").exists()
    assert not [c for c in rig.calls if c[0] in ("bootout", "bootstrap")]   # nothing booted


def test_a_key_the_new_job_drops_refuses_and_writes_nothing(rig):
    rig.legacy = {OLD}
    old = write_legacy(rig, EARS_RECORD="1")       # hand-edited: install never carries EARS_*
    before = old.read_bytes()
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    msg = str(e.value)
    assert "EARS_RECORD: '1' -> dropped (install never carries EARS_RECORD)" in msg
    assert OLD in msg and "nothing was written" in msg and "`sessionphone bot uninstall` first" in msg
    assert "with the old values in the env" not in msg      # the env cannot keep a key never carried
    assert_nothing_written(rig, old, before)
    assert rig.legacy == {OLD}


def test_a_dropped_carried_key_names_the_fix_and_the_fix_works(rig, monkeypatch):
    old = write_legacy(rig, DC_CALLS_DIR="/srv/calls-kept")
    before = old.read_bytes()
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    assert "DC_CALLS_DIR: '/srv/calls-kept' -> dropped (re-run with DC_CALLS_DIR=/srv/calls-kept in the env)" \
        in str(e.value)
    assert "Re-run `sessionphone bot install` with the old values in the env" in str(e.value)
    assert_nothing_written(rig, old, before)
    monkeypatch.setenv("DC_CALLS_DIR", "/srv/calls-kept")      # the operator's re-run
    assert install(rig) == [f"retired legacy launchd job {OLD} (plist removed)"]
    j = plistlib.loads((agents(rig) / f"{NEW}.plist").read_bytes())
    assert j["EnvironmentVariables"]["DC_CALLS_DIR"] == "/srv/calls-kept"


def test_a_changed_value_refuses_naming_old_and_new(rig, monkeypatch):
    monkeypatch.setenv("DC_CALLS_DIR", "/srv/calls-new")
    old = write_legacy(rig, DC_CALLS_DIR="/srv/calls-old")
    before = old.read_bytes()
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    assert "DC_CALLS_DIR: '/srv/calls-old' -> '/srv/calls-new'" in str(e.value)
    assert_nothing_written(rig, old, before)


def test_a_changed_root_refuses(rig, monkeypatch, tmp_path):
    """SESSIONPHONE_ROOT is the bot's account and calls: never regenerated.
    install carries it from the shell (paths.root), so the fix is the old root
    in the env, never uninstall-first (which would move the bot's account)."""
    was = str(tmp_path / "old-root")
    old = write_legacy(rig, SESSIONPHONE_ROOT=was)
    before = old.read_bytes()
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    msg = str(e.value)
    assert f"SESSIONPHONE_ROOT: '{was}' -> " in msg
    assert f"(re-run with SESSIONPHONE_ROOT={was} in the env)" in msg
    assert "install never carries" not in msg
    assert "Re-run `sessionphone bot install` with the old values in the env" in msg
    assert_nothing_written(rig, old, before)
    monkeypatch.setenv("SESSIONPHONE_ROOT", was)               # the operator's re-run
    assert install(rig) == [f"retired legacy launchd job {OLD} (plist removed)"]
    j = plistlib.loads((agents(rig) / f"{NEW}.plist").read_bytes())
    assert j["EnvironmentVariables"]["SESSIONPHONE_ROOT"] == was


def test_a_changed_agent_gets_its_own_line_not_the_env_rerun(rig, monkeypatch, given_seat):
    """job() always sets DC_AGENT to the install name (service.job), so "re-run with
    DC_AGENT=<old>" would only refuse again: a hand-edited legacy DC_AGENT gets its
    own line, and both ways it names work. The install name is service.agent():
    DC_AGENT, else CLAUDE_CODE_AGENT, else claude; `bot install` has no name option."""
    given_seat("bob-chat")     # this test installs as bob
    old = write_legacy(rig, DC_AGENT="Bob")
    before = old.read_bytes()
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    msg = str(e.value)
    assert "DC_AGENT: 'Bob' -> 'ada' (the install name: see the last line)" in msg
    assert msg.splitlines()[-1] == (
        f"This legacy job's DC_AGENT is Bob, not ada: install as bob (`sessionphone bot "
        f"uninstall` first, it removes {OLD}, then `DC_AGENT=Bob sessionphone bot install`: "
        "install's name is DC_AGENT, else CLAUDE_CODE_AGENT, else claude), or "
        "`sessionphone bot uninstall` first to accept the change, then `sessionphone bot install`.")
    assert "re-run with DC_AGENT" not in msg                  # the advice that loops
    assert "with the old values in the env" not in msg
    assert_nothing_written(rig, old, before)
    # the line's first way: uninstall (as ada), then install with DC_AGENT=Bob
    assert service.uninstall("ada") == [f"retired legacy launchd job {OLD} (plist removed)"]
    monkeypatch.setenv("DC_AGENT", "Bob")
    assert service.agent() == "bob"
    service.install()
    j = plistlib.loads((agents(rig) / "dev.sessionkit.phone.bot.bob.plist").read_bytes())
    assert j["EnvironmentVariables"]["DC_AGENT"] == "bob"
    assert not old.exists()


def test_a_changed_agent_and_a_carried_key_keep_both_lines(rig, monkeypatch, given_seat):
    """Both ways go through `bot uninstall`, which removes the plist this guard
    compares, so the install the line names must carry the other moved key itself:
    following it must not silently move the bot's calls dir."""
    given_seat("bob-chat")     # this test installs as bob
    write_legacy(rig, DC_AGENT="bob", DC_CALLS_DIR="/srv/calls-kept")
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    msg = str(e.value)
    assert "(re-run with DC_CALLS_DIR=/srv/calls-kept in the env)" in msg
    assert "with the old values in the env" not in msg      # the env alone cannot keep bob
    last = msg.splitlines()[-1]
    assert last.startswith("This legacy job's DC_AGENT is bob, not ada: install as bob")
    assert "then `DC_AGENT=bob DC_CALLS_DIR=/srv/calls-kept sessionphone bot install`" in last
    assert last.endswith(
        "to accept the change, then `DC_CALLS_DIR=/srv/calls-kept sessionphone bot install`.")
    # follow the first way: the calls dir moves with the bot
    service.uninstall("ada")
    monkeypatch.setenv("DC_AGENT", "bob")
    monkeypatch.setenv("DC_CALLS_DIR", "/srv/calls-kept")
    service.install()
    j = plistlib.loads((agents(rig) / "dev.sessionkit.phone.bot.bob.plist").read_bytes())
    assert j["EnvironmentVariables"]["DC_CALLS_DIR"] == "/srv/calls-kept"


def test_a_changed_agent_and_an_uncarried_key_say_it_is_dropped(rig):
    write_legacy(rig, DC_AGENT="bob", EARS_RECORD="1")
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    last = str(e.value).splitlines()[-1]
    assert "then `DC_AGENT=bob sessionphone bot install`" in last
    assert last.endswith(" Either way EARS_RECORD is dropped: install never carries it.")


@pytest.mark.parametrize("was", ["Ada", "", "  "])
def test_an_agent_that_installs_as_no_other_name_names_no_other_install(rig, was):
    """'Ada' installs as ada, and an empty DC_AGENT is no name (agent() falls through),
    so "install as <old>" would name no real way: only the uninstall line is given."""
    old = write_legacy(rig, DC_AGENT=was)
    before = old.read_bytes()
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    last = str(e.value).splitlines()[-1]
    assert last == (
        f"This legacy job's DC_AGENT is {was!r}, which install keeps as no name but ada (it "
        f"lowercases the name; an empty one is no name): `sessionphone bot uninstall` first "
        f"(it removes {OLD}), then `sessionphone bot install`.")
    assert "install as" not in last and "DC_AGENT=" not in last
    assert_nothing_written(rig, old, before)


def test_every_offending_key_is_named(rig):
    old = write_legacy(rig, EARS_RECORD="1", DC_ROOT="/srv/r", EARS_RECORD_DAYS="0")
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    msg = str(e.value)
    assert "carries 3 settings" in msg
    assert msg.index("DC_ROOT") < msg.index("EARS_RECORD:") < msg.index("EARS_RECORD_DAYS")
    assert old.exists()


def test_an_exempt_key_that_differs_does_not_refuse(rig):
    assert "PATH" in service.REGENERATED_KEYS
    write_legacy(rig, PATH="/usr/bin:/bin")
    assert install(rig) == [f"retired legacy launchd job {OLD} (plist removed)"]


def test_a_changed_ears_src_refuses_and_the_old_value_in_the_env_passes(rig, monkeypatch):
    """DC_EARS_SRC picks which ears code the bot imports: not exempt."""
    monkeypatch.setattr(service, "ears_src", lambda: "/tool/ears/site-packages")
    old = write_legacy(rig, DC_EARS_SRC="/hand/ears/src")
    before = old.read_bytes()
    with pytest.raises(service.LegacyCarryOver) as e:
        service.install("ada")
    assert ("DC_EARS_SRC: '/hand/ears/src' -> '/tool/ears/site-packages' "
            "(re-run with DC_EARS_SRC=/hand/ears/src in the env)") in str(e.value)
    assert_nothing_written(rig, old, before)
    monkeypatch.setenv("DC_EARS_SRC", "/hand/ears/src")       # the operator's re-run
    assert install(rig) == [f"retired legacy launchd job {OLD} (plist removed)"]
    j = plistlib.loads((agents(rig) / f"{NEW}.plist").read_bytes())
    assert j["EnvironmentVariables"]["DC_EARS_SRC"] == "/hand/ears/src"


def test_a_legacy_env_that_is_not_a_dictionary_refuses(rig):
    old = agents(rig) / f"{OLD}.plist"
    old.parent.mkdir(parents=True)
    old.write_bytes(plistlib.dumps({"Label": OLD, "EnvironmentVariables": ["DC_ROOT"]}))
    before = old.read_bytes()
    with pytest.raises(service.LegacyCarryOver, match="could not be read"):
        service.install("ada")
    assert_nothing_written(rig, old, before)


def test_the_exempt_list_is_small_and_every_entry_says_why():
    assert set(service.REGENERATED_KEYS) == {"PATH"}
    assert all(why.strip() for why in service.REGENERATED_KEYS.values())


def test_an_unreadable_legacy_plist_refuses(rig):
    old = agents(rig) / f"{OLD}.plist"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"not a plist")
    with pytest.raises(service.LegacyCarryOver, match="could not be read"):
        service.install("ada")
    assert_nothing_written(rig, old, b"not a plist")


def test_the_guard_comes_before_the_owner_is_written(rig, monkeypatch, tmp_path):
    """--owner writes the callers file: a refused install must not have."""
    callers = tmp_path / "callers.txt"
    before = callers.read_bytes()
    write_legacy(rig, EARS_RECORD="1")
    with pytest.raises(service.LegacyCarryOver):
        service.install("ada", owner="new-owner@phone.example.org")
    assert callers.read_bytes() == before


def test_bot_install_prints_each_retire_and_refuses_with_exit_2(rig, monkeypatch):
    rig.legacy = {OLD}
    write_legacy(rig)
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[:3] == [f"retired legacy launchd job {OLD} (booted out)",
                         f"retired legacy launchd job {OLD} (plist removed)",
                         f"installed: {agents(rig) / f'{NEW}.plist'}"]
    write_legacy(rig, EARS_RECORD="1")
    r = CliRunner().invoke(cli, ["bot", "install"])
    assert r.exit_code == 2 and "EARS_RECORD" in r.output and "installed:" not in r.output


# ---- uninstall -------------------------------------------------------------------

def uninstall(rig) -> tuple[int, list[str]]:
    r = CliRunner().invoke(cli, ["bot", "uninstall"])
    return r.exit_code, r.output.splitlines()


def test_uninstall_removes_the_new_and_the_legacy_job(rig):
    service.install("ada")
    rig.legacy = {OLD}
    write_legacy(rig)
    assert uninstall(rig) == (0, [f"removed launchd job {NEW} (booted out, plist removed)",
                                  f"retired legacy launchd job {OLD} (booted out, plist removed)"])
    assert not rig.loaded and rig.legacy == set()
    assert not list(agents(rig).iterdir())


def test_uninstall_of_a_legacy_job_alone(rig):
    write_legacy(rig)
    assert uninstall(rig) == (0, [f"retired legacy launchd job {OLD} (plist removed)"])


def test_uninstall_of_the_new_job_alone(rig):
    service.install("ada")
    assert uninstall(rig) == (0, [f"removed launchd job {NEW} (booted out, plist removed)"])


def test_uninstall_says_nothing_was_installed_only_when_neither_was(rig):
    assert uninstall(rig) == (0, ["no launchd job was installed"])
    rig.legacy = {OLD}                         # loaded, no plist: still a job
    assert uninstall(rig) == (0, [f"retired legacy launchd job {OLD} (booted out)"])


# ---- status ----------------------------------------------------------------------

def status(rig) -> tuple[int, list[str]]:
    r = CliRunner().invoke(cli, ["bot", "status"])
    return r.exit_code, r.output.splitlines()


def test_status_names_the_new_label_and_no_legacy_when_there_is_none(rig):
    service.install("ada")
    rc, lines = status(rig)
    assert rc == 0 and lines[0] == f"job:     {NEW} (installed)"
    assert not any(line.startswith("legacy:") for line in lines)
    assert service.status()["legacy"] is None


def test_status_names_a_loaded_legacy_job_and_the_command_that_retires_it(rig):
    rig.legacy = {OLD}
    rc, lines = status(rig)
    assert rc == 1                                         # the new job is not running
    # loaded with no plist: install refuses it, so status names uninstall
    assert lines[1] == (f"legacy:  {OLD} (loaded), the job sessionphone 0.1.0 installed: "
                        "`sessionphone bot uninstall` retires it, then `sessionphone bot install`")


def test_status_names_a_legacy_plist(rig):
    old = write_legacy(rig)
    rig.legacy = {OLD}
    _, lines = status(rig)
    assert lines[1] == (f"legacy:  {OLD} (loaded and plist {old}), the job sessionphone 0.1.0 "
                        "installed: `sessionphone bot install` retires it")
    st = service.status()
    assert st["legacy"] == {"label": OLD, "plist": str(old), "installed": True, "loaded": True}

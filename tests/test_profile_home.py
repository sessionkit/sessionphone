"""A named profile's home is <root>/<profile>/: two bots on one root never
share an account's files.

Chat ids belong to one account (chat 17 on the private account is another
chat on the public one), so a public bot that read the private list let a
private entry open a public chat. Every per-account file now resolves under
the bot's home (dc_env.home); the default profile's home is the root itself,
and not one of its paths moves (the PIN below). The bot refuses to start on
the default profile's list or callers, on a home another live bot holds, and
on an outbox backlog older than DC_OUTBOX_STALE_S.

Fakes only: a tmp root, no account, no relay, no launchd, no mail, no speech.
"""
import os
import pathlib
import subprocess
import sys
import time

import pytest
from _kit_log import kit_log

from sessionphone import channels, settings
from sessionphone.bridge import bot, dc_env

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
DC_ENV_SH = pathlib.Path(__file__).resolve().parents[1] / "bin" / "dc-env"

# Every path() key the bridge reads, with its file under the home, exactly as
# master read them before profiles had homes: the PIN is that, for the default
# profile, each one is still <root>/<rel>, byte for byte.
KEYS = (("ACCOUNTS", "accounts"), ("OUTBOX", "outbox"), ("CALLS_DIR", "calls"),
        ("LIVE", "call-live.json"), ("ACKS", "acks"),
        ("CALLERS_FILE", "callers.txt"), ("CHANNELS_FILE", "channels.txt"),
        ("SEEN", "bridged-ids.txt"), ("UNDELIVERED", "undelivered"),
        ("GUESTS", "call-guests.txt"), ("VOICES", "voices.json"),
        ("WAIT_MUSIC", "wait-music.opus"), ("EARS_SOCK", "call-ears.sock"),
        ("MOUTH_SOCK", "call-mouth.sock"))
ROOT_WIDE = {"VOICES", "WAIT_MUSIC"}     # not about an account: shared by every profile
# The bot's log is out of the root: ~/.sessionkit/phone/log/<bot>[/<profile>]/
# (dc_env.log_file, _kit_log), per bot and per profile, never in KEYS.

PROBE = r"""
import sys
sys.path.insert(0, sys.argv[1])
from sessionphone.bridge import dc_env, earleg, mouthleg, spool
from sessionphone import channels
for k, rel in [a.split("=", 1) for a in sys.argv[2:]]:
    print(f"{k} {dc_env.path(k, rel)}")
print(f"LOG {dc_env.log_file()}")
print(f"channels_file {dc_env.channels_file()}")
print(f"callers_file {dc_env.callers_file()}")
print(f"channels_path {channels.channels_path()}")
print(f"spool {spool.directory()}")
print(f"earleg {earleg.SOCK}")
print(f"mouthleg {mouthleg.SOCK}")
print(f"say_cmd {dc_env.say_cmd()}")
"""


def _clean_env(root, profile=None, **extra):
    """A bot's environment: no DC_*, PIP_CHAT_* or SESSIONPHONE_ROOT of the
    shell running the tests, then DC_ROOT, DC_AGENT and the profile."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DC_", "PIP_CHAT_")) and k != "SESSIONPHONE_ROOT"}
    env.update(DC_ROOT=str(root), DC_AGENT="ada", DC_HOST="host.example")
    if profile is not None:
        env["DC_PROFILE"] = profile
    env.update(extra)
    return env


def resolved(root, profile=None, **extra) -> dict:
    """What a bot started with this environment resolves, read at IMPORT in a
    fresh process (the module values are frozen there, as in the bot)."""
    out = subprocess.run([sys.executable, "-c", PROBE, str(SRC), *(f"{k}={r}" for k, r in KEYS)],
                         env=_clean_env(root, profile, **extra), capture_output=True, text=True,
                         check=True).stdout
    return dict(line.split(" ", 1) for line in out.splitlines())


# ---- the PIN: the default profile does not move a byte -------------------------------

def test_the_default_profile_resolves_every_path_exactly_as_before(tmp_path):
    got = resolved(tmp_path)
    for k, rel in KEYS:
        assert got[k] == f"{tmp_path}/{rel}", k
    assert got["channels_file"] == f"{tmp_path}/channels.txt"
    assert got["callers_file"] == f"{tmp_path}/callers.txt"
    assert got["channels_path"] == f"{tmp_path}/channels.txt"
    assert got["spool"] == f"{tmp_path}/undelivered"
    assert got["earleg"] == f"{tmp_path}/call-ears.sock"
    assert got["mouthleg"] == f"{tmp_path}/call-mouth.sock"
    assert got["say_cmd"] == "dc-say"
    assert got["LOG"] == str(kit_log("ada"))     # the one path that moved: out of the root


def test_an_empty_profile_is_the_default_profile(tmp_path):
    assert resolved(tmp_path, "") == resolved(tmp_path)


# ---- two profiles, one root: nothing shared ------------------------------------------

def test_a_named_profile_keeps_every_per_account_file_in_its_home(tmp_path):
    got = resolved(tmp_path, "public")
    for k, rel in KEYS:
        want = tmp_path / rel if k in ROOT_WIDE else tmp_path / "public" / rel
        assert got[k] == str(want), k
    assert got["channels_file"] == got["channels_path"] == f"{tmp_path}/public/channels.txt"
    assert got["callers_file"] == f"{tmp_path}/public/callers.txt"
    assert got["spool"] == f"{tmp_path}/public/undelivered"
    # the sockets were already per profile: the same place, now by the home
    assert got["earleg"] == f"{tmp_path}/public/call-ears.sock"
    assert got["mouthleg"] == f"{tmp_path}/public/call-mouth.sock"
    assert got["say_cmd"] == f"DC_OUTBOX={tmp_path}/public/outbox dc-say"
    assert got["LOG"] == str(kit_log("ada", "public"))     # its own log folder, in the bot's


def test_two_bots_on_one_root_share_no_list_callers_or_outbox(tmp_path):
    private, public = resolved(tmp_path), resolved(tmp_path, "public")
    per_account = [k for k, _ in KEYS if k not in ROOT_WIDE] + ["LOG"]
    for k in per_account + ["channels_file", "callers_file", "spool", "earleg", "mouthleg"]:
        assert private[k] != public[k], k
    for k in ("OUTBOX", "channels_file", "callers_file", "SEEN"):
        assert not pathlib.Path(public[k]).is_relative_to(pathlib.Path(private["OUTBOX"])), k


def test_an_explicit_setting_still_wins_on_a_named_profile(tmp_path):
    mine = tmp_path / "elsewhere" / "out"
    got = resolved(tmp_path, "public", DC_OUTBOX=str(mine), DC_CHANNELS_FILE=str(tmp_path / "x.txt"))
    assert got["OUTBOX"] == str(mine)
    assert got["channels_file"] == got["CHANNELS_FILE"] == str(tmp_path / "x.txt")
    assert got["LOG"] == str(kit_log("ada", "public"))
    mine = resolved(tmp_path, "public", DC_LOG=str(tmp_path / "own.log"))
    assert mine["LOG"] == str(tmp_path / "own.log")             # DC_LOG still wins


def test_home_reads_the_environment_it_is_given(tmp_path):
    assert dc_env.home({"DC_ROOT": str(tmp_path)}) == tmp_path
    assert dc_env.home({"DC_ROOT": str(tmp_path), "DC_PROFILE": "public"}) == tmp_path / "public"
    assert dc_env.home({"DC_PROFILE": " public "}, root=tmp_path) == tmp_path / "public"
    assert dc_env.channels_file({"DC_ROOT": str(tmp_path), "DC_PROFILE": "public"}) == \
        tmp_path / "public" / "channels.txt"
    assert dc_env.callers_file({"DC_ROOT": str(tmp_path)}) == tmp_path / "callers.txt"


# ---- the gate: chat N on one list never passes chat N on the other -------------------

class _Chat:
    def __init__(self, cid):
        self.id = cid

    def get_basic_snapshot(self):
        return type("S", (), {"name": f"chat{self.id}", "chat_type": "Group", "is_contact_request": False})()

    def get_contacts(self):
        return []


class _Account:
    def get_chat_by_id(self, cid):
        return _Chat(cid)


@pytest.fixture
def two_lists(tmp_path):
    """The private list names chat 17; the public list names chat 20 only."""
    (tmp_path / "public").mkdir(mode=0o700)
    (tmp_path / "channels.txt").write_text("private  chat:17\n")
    (tmp_path / "public" / "channels.txt").write_text("public  chat:20\n")
    for f in (tmp_path / "channels.txt", tmp_path / "public" / "channels.txt"):
        f.chmod(0o600)
    return tmp_path


def test_chat_n_listed_on_one_profile_never_passes_on_the_other(two_lists):
    priv = dc_env.channels_file({"DC_ROOT": str(two_lists)})
    pub = dc_env.channels_file({"DC_ROOT": str(two_lists), "DC_PROFILE": "public"})
    acct = _Account()
    assert channels.check(acct, 17, path=priv)
    assert not channels.check(acct, 17, path=pub)
    assert channels.check(acct, 20, path=pub)
    assert not channels.check(acct, 20, path=priv)


def test_the_public_bot_rejects_inbound_from_the_private_lists_chat(two_lists, monkeypatch):
    logs = []
    monkeypatch.setattr(dc_env, "ROOT", two_lists)
    monkeypatch.setattr(dc_env, "PROFILE", "public")
    monkeypatch.setattr(bot, "PROFILE", "public")
    monkeypatch.setattr(bot, "log", logs.append)
    monkeypatch.delenv("DC_CHANNELS_FILE", raising=False)
    monkeypatch.delenv("PIP_CHAT_CHANNELS_FILE", raising=False)
    assert bot.inbound_ok(_Account(), 17, "message", "x@relay.example", "X") is False
    assert bot.inbound_ok(_Account(), 20, "message", "x@relay.example", "X") is True
    (line,) = [ln for ln in logs if ln.startswith("REJECTED inbound")]
    # the rejection line names the profile and the list it read
    assert f"REJECTED inbound message from chat 17 (profile public, list {two_lists}/public/channels.txt)" in line
    assert (two_lists / "public" / channels.REJECTED_NAME).exists()
    assert not (two_lists / channels.REJECTED_NAME).exists()


# ---- callers: a profile with none of its own refuses calls, and says so --------------

def test_a_profile_with_no_callers_file_refuses_calls_even_when_the_root_has_one(tmp_path):
    (tmp_path / "callers.txt").write_text("owner@relay.example\n")
    env = {"DC_ROOT": str(tmp_path), "DC_PROFILE": "public"}
    said = dc_env.callers_problem(env)
    assert said.startswith(f"NOBODY may ring this agent: {tmp_path}/public/callers.txt does not exist")
    assert dc_env.callers_problem({"DC_ROOT": str(tmp_path)}) is None   # the default profile is unchanged
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); from sessionphone.bridge import calls; "
             "print(calls.CALLERS_FILE); print(sorted(calls.approved_callers()))")
    out = subprocess.run([sys.executable, "-c", probe, str(SRC)], env=_clean_env(tmp_path, "public"),
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert out == [f"{tmp_path}/public/callers.txt", "[]"]


# ---- the start refusals ---------------------------------------------------------------

@pytest.fixture
def profile_bot(tmp_path, monkeypatch):
    """bot as a `public` profile bot on a tmp root, no DC_* files named."""
    for k in ("DC_CHANNELS_FILE", "DC_CALLERS_FILE", "PIP_CHAT_CHANNELS_FILE", "PIP_CHAT_CALLERS_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(dc_env, "ROOT", tmp_path)
    monkeypatch.setattr(dc_env, "PROFILE", "public")
    monkeypatch.setattr(bot, "PROFILE", "public")
    return tmp_path


def test_a_profile_bot_on_its_own_files_starts(profile_bot):
    assert bot.check_profile_files() is None


@pytest.mark.parametrize("var, rel, what", [("DC_CHANNELS_FILE", "channels.txt", "allow-list"),
                                            ("DC_CALLERS_FILE", "callers.txt", "callers file")])
def test_a_profile_bot_on_the_default_profiles_file_is_refused(profile_bot, monkeypatch, var, rel, what):
    monkeypatch.setenv(var, str(profile_bot / rel))
    why = bot.check_profile_files()
    assert why.startswith(f"profile public's {what} is {profile_bot / rel}, the default profile's own")
    assert f"Unset {var} (this profile then reads {profile_bot / 'public' / rel})" in why


def test_the_default_profile_is_never_refused_for_its_own_files(tmp_path, monkeypatch):
    monkeypatch.setattr(dc_env, "ROOT", tmp_path)
    monkeypatch.setattr(dc_env, "PROFILE", "")
    monkeypatch.setattr(bot, "PROFILE", "")
    assert bot.check_profile_files() is None


HOLD = r"""
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0); os.write(fd, f"{os.getpid()}\n".encode())
print("held", flush=True)
time.sleep(60)
"""


def test_a_second_bot_on_one_home_is_refused_naming_the_holders_pid(tmp_path):
    home = tmp_path / "public"
    home.mkdir()
    other = subprocess.Popen([sys.executable, "-c", HOLD, str(home / bot.LOCK_NAME)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "held"
        why = bot.take_home_lock(home)
        assert why.startswith(f"another bot (pid {other.pid}) already runs on {home}: it holds "
                              f"{home / bot.LOCK_NAME}")
        assert "DC_PROFILE=NAME, or another DC_ROOT" in why
    finally:
        other.kill()        # our own child, by its pid
        other.wait()
    assert bot.take_home_lock(home) is None     # the kernel let it go with the process
    assert (home / bot.LOCK_NAME).read_text() == f"{os.getpid()}\n"


def test_two_profiles_on_one_root_lock_two_homes(tmp_path):
    """Two bots, one root, two profiles: each locks its own home. The other
    bot is a child process; this one takes the root's lock beside it."""
    (tmp_path / "public").mkdir()
    other = subprocess.Popen([sys.executable, "-c", HOLD, str(tmp_path / "public" / bot.LOCK_NAME)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "held"
        assert bot.take_home_lock(tmp_path) is None
    finally:
        other.kill()
        other.wait()


def test_this_process_starting_again_on_its_own_home_is_no_second_bot(tmp_path):
    assert bot.take_home_lock(tmp_path) is None
    assert bot.take_home_lock(tmp_path) is None


# ---- the stale backlog ---------------------------------------------------------------

def _job(outbox, name, age, now):
    f = outbox / name
    f.write_text("{}")
    os.utime(f, (now - age, now - age))
    return f


def test_an_old_backlog_refuses_start_naming_count_age_path_and_both_ways(tmp_path):
    now = time.time()
    _job(tmp_path, "1.json", 7300, now)
    _job(tmp_path, "2.json", 4000, now)
    _job(tmp_path, "3.json", 10, now)        # fresh: not counted
    _job(tmp_path, ".bot.json", 99999, now)  # a dotfile is no job
    (tmp_path / ".bot").write_text("stamp")
    why = bot.check_stale_outbox(tmp_path, 3600, now)
    assert why.startswith(f"2 jobs in {tmp_path} are older than 3600s (DC_OUTBOX_STALE_S), the oldest 2h 1m:")
    assert f"`rm {tmp_path}/*.json`" in why and f"`touch {tmp_path}/*.json`" in why
    assert "\n" not in why      # one line


def test_younger_jobs_and_an_empty_or_missing_outbox_start(tmp_path):
    now = time.time()
    assert bot.check_stale_outbox(tmp_path / "none", 3600, now) is None
    assert bot.check_stale_outbox(tmp_path, 3600, now) is None
    _job(tmp_path, "1.json", 3500, now)
    assert bot.check_stale_outbox(tmp_path, 3600, now) is None


def test_the_setting_moves_the_bound(tmp_path):
    now = time.time()
    _job(tmp_path, "1.json", 120, now)
    assert bot.check_stale_outbox(tmp_path, 3600, now) is None
    assert bot.check_stale_outbox(tmp_path, 60, now).startswith(f"1 job in {tmp_path} is older than 60s")
    probe = "import sys; sys.path.insert(0, sys.argv[1]); from sessionphone.bridge import bot; print(bot.OUTBOX_STALE_S)"
    for given, want in ((None, "3600.0"), ("60", "60.0")):
        extra = {} if given is None else {"DC_OUTBOX_STALE_S": given}
        out = subprocess.run([sys.executable, "-c", probe, str(SRC)], env=_clean_env(tmp_path, **extra),
                             capture_output=True, text=True, check=True).stdout.strip()
        assert out == want


def test_main_refuses_an_old_backlog_before_it_opens_the_account(tmp_path, monkeypatch, capsys):
    out = tmp_path / "outbox"
    out.mkdir()
    now = time.time()
    _job(out, "1.json", 7200, now)
    log = tmp_path / "bot.log"
    monkeypatch.setattr(dc_env, "ROOT", tmp_path)
    monkeypatch.setattr(dc_env, "PROFILE", "")
    monkeypatch.setattr(bot, "PROFILE", "")
    monkeypatch.setattr(dc_env, "take_mail_host", lambda: "test")
    monkeypatch.setattr(dc_env, "HOST_PROBLEM", None)
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "check_stt", lambda: None)
    monkeypatch.setattr(bot.seatmail, "check", lambda: None)
    monkeypatch.setattr(bot, "OUTBOX", out)
    monkeypatch.setattr(bot, "LOG", log)
    monkeypatch.setattr(bot, "Rpc", lambda **k: pytest.fail("the account was opened"))
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith(f"sessionphone bot: REFUSED to start: 1 job in {out} is older than 3600s")
    assert "REFUSED to start: 1 job in" in log.read_text()
    assert (out / "1.json").exists()        # a person decides: nothing is dropped or sent


# ---- install: two profiles on one root are not rivals --------------------------------

def test_place_is_the_profile_home(tmp_path):
    root = os.path.realpath(tmp_path)
    assert settings.place({"DC_ROOT": str(tmp_path)}) == (root, os.path.join(root, "accounts"))
    assert settings.place({"DC_ROOT": str(tmp_path), "DC_PROFILE": "public"}) == \
        (os.path.join(root, "public"), os.path.join(root, "public", "accounts"))


def test_rivals_two_profiles_on_one_root_are_not_two_bots_on_one_home_are(tmp_path, monkeypatch):
    found = [(tmp_path / "phone.json", {"bots": {
        "ada": {"DC_ROOT": str(tmp_path)},
        "bob": {"DC_ROOT": str(tmp_path), "DC_PROFILE": "public"}}})]
    monkeypatch.setattr(settings, "names", lambda f: ["ada", "bob"])
    monkeypatch.setattr(settings, "merged", lambda f, n: (f[0][1]["bots"][n], {}, {}))
    assert settings.rivals(found, "bob", {"DC_ROOT": str(tmp_path), "DC_PROFILE": "public"}) == {}
    assert settings.rivals(found, "eve", {"DC_ROOT": str(tmp_path), "DC_PROFILE": "public"}) == \
        {"bob": settings.shown(os.path.realpath(tmp_path / "public"))}
    assert set(settings.rivals(found, "eve", {"DC_ROOT": str(tmp_path)})) == {"ada"}


# ---- bin/dc-env mirrors dc_env -------------------------------------------------------

SH = r"""
. "$1"
printf '%s\n' "$(dc_outbox)" "$(dc_channels)" "$(dc_live)" "$DC_LOG" "$DC_CALLS_DIR" "$DC_UNDELIVERED" \
  "$DC_ACKS" "$(dc_outbox public)" "$(dc_channels public)" "$(dc_live public)"
"""


@pytest.mark.parametrize("profile", [None, "public"])
def test_dc_env_resolves_what_the_bot_resolves(tmp_path, profile):
    out = subprocess.run(["bash", "-c", SH, "dc-env-test", str(DC_ENV_SH)], env=_clean_env(tmp_path, profile),
                         capture_output=True, text=True, check=True).stdout.splitlines()
    py = resolved(tmp_path, profile)
    assert out[:7] == [py["OUTBOX"], py["channels_file"], py["LIVE"], py["LOG"], py["CALLS_DIR"],
                       py["UNDELIVERED"], py["ACKS"]]
    public = resolved(tmp_path, "public")
    assert out[7:] == [public["OUTBOX"], public["channels_file"], public["LIVE"]]


def test_dc_say_on_a_profiles_hint_checks_that_profiles_live_call(tmp_path):
    """The fallback line a profile's call mail carries is DC_OUTBOX=<home>/outbox
    dc-say --call: it must find <home>/call-live.json, not the default's."""
    out = subprocess.run(["bash", "-c", '. "$1"; dc_live', "x", str(DC_ENV_SH)],
                         env=_clean_env(tmp_path, DC_OUTBOX=f"{tmp_path}/public/outbox"),
                         capture_output=True, text=True, check=True).stdout
    assert out == f"{tmp_path}/public/call-live.json"
    out = subprocess.run(["bash", "-c", '. "$1"; dc_live', "x", str(DC_ENV_SH)],
                         env=_clean_env(tmp_path, DC_LIVE=f"{tmp_path}/mine.json"),
                         capture_output=True, text=True, check=True).stdout
    assert out == f"{tmp_path}/mine.json"


@pytest.mark.parametrize("profile, rel", [(None, "bot.pid"), ("public", "public/bot.pid")])
def test_dc_env_names_the_profiles_own_pid_file_as_the_bot_does(tmp_path, profile, rel):
    """dc-live reads $DC_PID_FILE to know the bot runs. Under DC_PROFILE=public
    it must be the public bot's (<home>/bot.pid), the file that bot writes,
    never the default bot's <root>/bot.pid. The default profile's is the root's."""
    env = _clean_env(tmp_path, profile)
    sh = subprocess.run(["bash", "-c", '. "$1"; printf %s "$DC_PID_FILE"', "x", str(DC_ENV_SH)],
                        env=env, capture_output=True, text=True, check=True).stdout
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from sessionphone.bridge import dc_env; print(dc_env.PID_FILE)")
    py = subprocess.run([sys.executable, "-c", code, str(SRC)],
                        env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert sh == py == str(tmp_path / rel)


# ---- a call's TTS fallback queues into its own profile's outbox -----------------------

@pytest.mark.parametrize("profile", ["", "public"])
def test_a_calls_failed_tts_text_lands_in_its_profiles_outbox(tmp_path, monkeypatch, profile):
    """When TTS fails on a call, speak() queues the words as a chat job. On a
    named profile that job goes to <root>/<profile>/outbox, the outbox its own
    bot reads: in the default bot's <root>/outbox, the default account would
    send it to ITS chat with that id, another chat. No DC_OUTBOX is set here,
    so the default resolution is what is tested."""
    import asyncio
    import json

    from sessionphone.bridge import calls
    for var in ("DC_OUTBOX", "PIP_CHAT_OUTBOX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(dc_env, "ROOT", tmp_path)
    monkeypatch.setattr(dc_env, "PROFILE", profile)
    logged = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logged.append, lambda p: p, lambda *a, **k: None))

    def refused(text, voice):
        raise RuntimeError("tts curl rc=7 bytes=0")
    monkeypatch.setattr(calls, "tts_pcm48", refused)
    s = object.__new__(calls.CallSession)
    s.chat_id, s.lines, s.voice = 17, [], calls.PipVoiceTrack()
    asyncio.run(s.speak("Hi Ada."))

    mine = tmp_path / profile / "outbox" if profile else tmp_path / "outbox"
    jobs = [json.loads(p.read_text()) for p in mine.glob("*.json")]
    assert jobs == [{"chat_id": 17, "text": "(unspoken, TTS failed) Hi Ada.", "no_speak": True}], logged
    if profile:
        assert not (tmp_path / "outbox").exists(), "the default bot's outbox got a profile's job"

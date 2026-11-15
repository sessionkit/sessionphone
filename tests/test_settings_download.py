"""The download policy's three host-only keys: download_limit, download_types
and save_dir, under bots.<agent> in phone.json.

They are read from the host layer alone (phone.json, then phone.d/*.json,
under _locked), never from the environment; every value is a JSON string; a
value the bot cannot use is said, naming its file and key, and that key alone
takes its default. `bot install --set KEY=VALUE` writes one, keeping the
bot's other keys, and a lock refuses it. The job never carries them. The
package ships phone.schema.json, and install puts it beside phone.json.

Fakes only: FakeLaunchd (conftest), a plist in tmp_path, the tripwire
`message` as the signer's sessionmail, a scratch HOME.
"""
import json
import os
import pathlib
import plistlib
import re
import stat

import pytest
from click.testing import CliRunner

from sessionphone import service, settings
from sessionphone.bridge import dc_env
from sessionphone.cli import main as cli

OWNER = "owner@phone.example.org"
DEFAULT_FILE = settings.FILE            # read at import, before conftest moves it

FIRST = {
    "DC_AGENT": "ada", "DC_HOST": "mac.example.net",
    "DC_ADDR": "ada-chat@mac.example.net", "DC_MAILTO": "ada@mac.example.net",
    "DC_STT_URL": "http://127.0.0.1:9/v1/audio/transcriptions", "DC_STT_MODEL": "whisper-1",
    "DC_TTS_URL": "http://127.0.0.1:9/v1/audio/speech", "DC_VOICE": "af_sky", "DC_ANSWER": "auto",
    "SESSIONMAIL_SENDMAIL": "drop", "SESSIONMAIL_HOST": "mac",
}


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A scratch HOME, phone.json under it, and no DC_* in the environment."""
    for k in [k for k in os.environ if k.startswith(("DC_", "PIP_CHAT_"))]:
        monkeypatch.delenv(k)
    for k in settings.HOST_ONLY:
        monkeypatch.delenv(k, raising=False)
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(settings, "FILE", DEFAULT_FILE)
    return h


@pytest.fixture
def rig(monkeypatch, tmp_path, home, launchd, given_seat):
    """An install that would load: test_install_remembers.py's rig."""
    for k in [k for k in os.environ if k.startswith("SESSIONMAIL_")]:
        monkeypatch.delenv(k)
    monkeypatch.delenv("CLAUDE_CODE_AGENT", raising=False)
    pub = given_seat("ada-chat")
    (pub.parent / "agent.sock").touch()
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    monkeypatch.setenv("SESSIONMAIL_HOME", str(home / ".sessionmail"))
    monkeypatch.setenv("SESSIONMAIL_ROOT", str(home / ".mail" / "agents"))
    for k, v in FIRST.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(service, "plist_path", lambda name=None: tmp_path / "job.plist")
    monkeypatch.setattr(service, "named_python", lambda: "/venv/libexec/session-phone")
    monkeypatch.setattr(service, "ears_src", lambda: None)
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "mac.example.net")
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "mac.local")
    return tmp_path / "job.plist"


def _main() -> pathlib.Path:
    return pathlib.Path(os.environ["HOME"]) / ".sessionkit" / "phone" / "settings" / "phone.json"


def _write(p: pathlib.Path, data: dict) -> pathlib.Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return p


def _dropin(name: str, data: dict) -> pathlib.Path:
    return _write(_main().with_name("phone.d") / name, data)


def _install(*args):
    return CliRunner().invoke(cli, ["bot", "install", *args])


def _job_env(plist):
    return plistlib.loads(plist.read_bytes())["EnvironmentVariables"]


# ---- the defaults and the host layer ------------------------------------------------

def test_the_defaults_with_no_file(home, tmp_path):
    pol = settings.policy("ada", tmp_path / "state")
    assert pol.limit == 16_000_000
    assert pol.types == frozenset(settings.KINDS)
    assert pol.save_dir == tmp_path / "state" / "attachments"
    assert pol.origin == {k: "the default" for k in settings.HOST_ONLY}
    assert pol.problems == ()


def test_the_kinds_are_the_designs():
    assert settings.KINDS == ("image", "sticker", "voice", "audio", "video", "file", "webxdc", "vcard")


def test_phone_json_values_reach_the_reader(home, tmp_path):
    _write(_main(), {"bots": {"ada": {"DC_HOST": "mac.example.net", "download_limit": "1000000",
                                      "download_types": "image, voice", "save_dir": "~/ada-files"},
                              "bob": {"download_limit": "5"}}})
    got = settings.host_values("ada")
    assert {k: v for k, (v, _f) in got.items()} == {
        "download_limit": "1000000", "download_types": "image, voice", "save_dir": "~/ada-files"}
    assert {f for _v, f in got.values()} == {_main()}
    pol = settings.policy("ada", tmp_path)
    assert pol.limit == 1_000_000
    assert pol.types == frozenset({"image", "voice"})
    assert pol.save_dir == home / "ada-files"
    assert pol.origin == {k: "~/.sessionkit/phone/settings/phone.json" for k in settings.HOST_ONLY}
    assert pol.problems == ()


def test_a_drop_in_wins_over_phone_json_and_is_named(home, tmp_path):
    _write(_main(), {"bots": {"ada": {"download_limit": "1000000"}}})
    _dropin("20-x.json", {"bots": {"ada": {"download_limit": "2000000"}}})
    pol = settings.policy("ada", tmp_path)
    assert pol.limit == 2_000_000
    assert pol.origin["download_limit"] == "~/.sessionkit/phone/settings/phone.d/20-x.json"


def test_a_drop_in_loses_to_a_lock(home, tmp_path):
    _write(_main(), {"bots": {"ada": {"download_limit": "1000000"}},
                     "_locked": ["bots.ada.download_limit"]})
    _dropin("20-x.json", {"bots": {"ada": {"download_limit": "99999999", "save_dir": "/srv/x"}}})
    pol = settings.policy("ada", tmp_path)
    assert pol.limit == 1_000_000
    assert pol.origin["download_limit"] == "~/.sessionkit/phone/settings/phone.json"
    assert pol.save_dir == pathlib.Path("/srv/x")       # an unlocked key still comes from the drop-in


@pytest.mark.parametrize("var", ["DC_DOWNLOAD_LIMIT", "download_limit", "DC_DOWNLOAD_TYPES",
                                 "download_types", "DC_SAVE_DIR", "save_dir"])
def test_the_environment_changes_nothing(home, tmp_path, monkeypatch, var):
    """Host-only: a shell's variable, an agent's included, is never read."""
    _write(_main(), {"bots": {"ada": {"download_limit": "1000000"}}})
    before = settings.policy("ada", tmp_path)
    monkeypatch.setenv(var, "/tmp/elsewhere" if "DIR" in var.upper() else "1")
    assert settings.policy("ada", tmp_path) == before
    assert set(settings.host_values("ada")) == {"download_limit"}


# ---- values the bot cannot use ------------------------------------------------------

@pytest.mark.parametrize("key,value,why", [
    ("download_limit", "-1", "not a number of bytes"),
    ("download_limit", "16MB", "not a number of bytes"),
    ("download_limit", "", "not a number of bytes"),
    ("download_limit", "١٢", "not a number of bytes"),       # digits, but not decimal ASCII
    ("download_types", "", "empty"),
    ("download_types", "pictures", "names no kind this bot knows"),
    ("save_dir", "relative/dir", "not an absolute path"),
    ("save_dir", "", "empty"),
])
def test_a_bad_value_is_said_with_file_and_key_and_falls_back_alone(home, tmp_path, key, value, why):
    good = {"download_limit": "1000000", "download_types": "image", "save_dir": "/srv/files"}
    _write(_main(), {"bots": {"ada": {**good, key: value}}})
    pol = settings.policy("ada", tmp_path)
    assert len(pol.problems) == 1, pol.problems
    said = pol.problems[0]
    assert f"~/.sessionkit/phone/settings/phone.json `bots.ada.{key}`" in said
    assert json.dumps(value) in said and why in said and "the default is used" in said
    assert pol.origin[key] == "the default"
    defaults = settings.policy("nobody", tmp_path)
    assert getattr(pol, {"download_limit": "limit", "download_types": "types",
                         "save_dir": "save_dir"}[key]) == getattr(
        defaults, {"download_limit": "limit", "download_types": "types", "save_dir": "save_dir"}[key])
    # the other two keys keep their file values
    for other in set(settings.HOST_ONLY) - {key}:
        assert pol.origin[other] == "~/.sessionkit/phone/settings/phone.json"


def test_an_unknown_kind_is_dropped_and_the_rest_kept(home, tmp_path):
    _write(_main(), {"bots": {"ada": {"download_types": "image,pictures,voice"}}})
    pol = settings.policy("ada", tmp_path)
    assert pol.types == frozenset({"image", "voice"})
    assert pol.origin["download_types"] == "~/.sessionkit/phone/settings/phone.json"
    assert len(pol.problems) == 1
    assert "`bots.ada.download_types`" in pol.problems[0] and "unknown kind pictures dropped" in pol.problems[0]


def test_none_means_no_kind_and_zero_means_no_limit(home, tmp_path):
    _write(_main(), {"bots": {"ada": {"download_types": "none", "download_limit": "0"}}})
    pol = settings.policy("ada", tmp_path)
    assert pol.types == frozenset() and pol.limit == 0 and pol.problems == ()
    assert "download_types: none" in "\n".join(pol.lines())
    assert "download_limit: 0 (no limit)" in "\n".join(pol.lines())


@pytest.mark.parametrize("value", [16000000, ["image"], None, {"x": "y"}])
def test_a_value_that_is_not_a_string_is_the_files_shape_error(home, tmp_path, value):
    """The file's existing shape error: host_values raises it, and the policy
    reader catches it and runs on the three defaults; it never stops the bot."""
    _write(_main(), {"bots": {"ada": {"download_limit": value, "save_dir": "/srv/x"}}})
    with pytest.raises(settings.Bad, match=r"bots\.ada\.download_limit`, which is not a string"):
        settings.host_values("ada")
    pol = settings.policy("ada", tmp_path)
    assert (pol.limit, pol.types, pol.save_dir) == (16_000_000, frozenset(settings.KINDS),
                                                    tmp_path / "attachments")
    assert pol.origin == {k: "the default" for k in settings.HOST_ONLY}
    assert len(pol.problems) == 1 and "not a string" in pol.problems[0]
    assert "phone.json" in pol.problems[0]


def test_the_summary_line_names_each_source(home, tmp_path):
    _write(_main(), {"bots": {"ada": {"download_limit": "16000000"}}})
    _dropin("20-x.json", {"bots": {"ada": {"save_dir": "~/ada-files"}}})
    assert settings.policy("ada", tmp_path).summary() == (
        "download policy: limit 16000000 (16 MB) (~/.sessionkit/phone/settings/phone.json), "
        "types all (the default), save_dir ~/ada-files (~/.sessionkit/phone/settings/phone.d/20-x.json)")


# ---- install: --set, the lock, the job ----------------------------------------------

def test_set_writes_json_strings_and_keeps_the_other_keys(rig):
    _write(_main(), {"bots": {"ada": {"DC_HOST": "mac.example.net", "DC_NOTE": "hand-written",
                                      "save_dir": "/srv/kept"}}})
    r = _install("--owner", OWNER, "--set", "download_limit=16000000",
                 "--set", "download_types=image,voice")
    assert r.exit_code == 0, r.output
    saved = json.loads(_main().read_text())["bots"]["ada"]
    assert saved["download_limit"] == "16000000"          # the JSON string, never an int
    assert saved["download_types"] == "image,voice"
    assert saved["save_dir"] == "/srv/kept"               # a hand-written host-only key survives
    assert saved["DC_NOTE"] == "hand-written"             # and so does every other key
    assert settings._shape(json.loads(_main().read_text())) is None
    assert "  download_limit  16000000  --set (saved), host-only" in r.output
    assert "  save_dir  /srv/kept  ~/.sessionkit/phone/settings/phone.json, host-only" in r.output


def test_a_second_install_keeps_what_set_wrote(rig, monkeypatch):
    assert _install("--owner", OWNER, "--set", "download_limit=1000000").exit_code == 0
    r = _install()
    assert r.exit_code == 0, r.output
    assert json.loads(_main().read_text())["bots"]["ada"]["download_limit"] == "1000000"
    r = _install("--set", "download_limit=2000000")
    assert r.exit_code == 0, r.output
    assert json.loads(_main().read_text())["bots"]["ada"]["download_limit"] == "2000000"
    assert "--set (saved; was 1000000)" in r.output


def test_the_job_never_carries_them_and_install_does_not_list_them_as_ignored(rig, monkeypatch):
    _write(_main(), {"bots": {"ada": {"download_types": "image", "save_dir": "/srv/x",
                                      "SOMETHING_ELSE": "y"}}})
    monkeypatch.setenv("download_limit", "5")             # a shell's: never carried, never read
    r = _install("--owner", OWNER, "--set", "download_limit=1000000")
    assert r.exit_code == 0, r.output
    env = _job_env(rig)
    for k in settings.HOST_ONLY:
        assert k not in env
        assert f"ignored {k}" not in r.output
    assert "ignored SOMETHING_ELSE" in r.output           # any other unkept key is still listed
    res = settings.resolve(os.environ)
    assert not set(settings.HOST_ONLY) & set(res.given)
    assert set(res.host) == {"download_limit", "download_types", "save_dir"}


def _refused(r, *words):
    assert r.exit_code == 2, r.output
    for w in words:
        assert w in r.output, r.output


def test_set_against_a_lock_is_refused_naming_the_file_writing_nothing(rig):
    p = _write(_main(), {"bots": {"ada": {"download_limit": "16000000"}},
                         "_locked": ["bots.ada.download_limit"]})
    before = p.read_bytes()
    r = _install("--owner", OWNER, "--set", "download_limit=1000000")
    _refused(r, "download_limit is locked by ~/.sessionkit/phone/settings/phone.json at 16000000",
             "--set gives 1000000", "nothing was written")
    assert p.read_bytes() == before
    assert not rig.exists()                               # the job was not written either


def test_set_against_a_lock_on_the_whole_bot_is_refused(rig):
    _write(_main(), {"bots": {"ada": {"download_types": "image"}}, "_locked": ["bots.ada"]})
    _refused(_install("--owner", OWNER, "--set", "download_types=video"), "locked by")


def test_set_against_a_lock_with_no_value_is_refused(rig):
    _dropin("10-lock.json", {"_locked": ["bots.ada.save_dir"]})
    r = _install("--owner", OWNER, "--set", "save_dir=/srv/x")
    _refused(r, "save_dir is locked by ~/.sessionkit/phone/settings/phone.d/10-lock.json",
             "gives it no value")


def test_set_of_the_locked_value_itself_is_no_conflict(rig):
    _write(_main(), {"bots": {"ada": {"download_limit": "16000000"}},
                     "_locked": ["bots.ada.download_limit"]})
    r = _install("--owner", OWNER, "--set", "download_limit=16000000")
    assert r.exit_code == 0, r.output


def test_set_of_a_key_a_drop_in_sets_is_refused(rig):
    _dropin("20-x.json", {"bots": {"ada": {"save_dir": "/srv/x"}}})
    _refused(_install("--owner", OWNER, "--set", "save_dir=/srv/y"),
             "it is set in ~/.sessionkit/phone/settings/phone.d/20-x.json")


@pytest.mark.parametrize("pair,words", [
    ("download_limit=lots", ["--set download_limit=lots", "not a number of bytes"]),
    ("download_types=image,pictures", ["unknown kind pictures"]),
    ("save_dir=rel/dir", ["not an absolute path"]),
    ("DC_HOST=x", ["install sets only download_limit, download_types, save_dir",
                   "A DC_* setting is given in this shell"]),
    ("colour=blue", ["--set colour"]),
    ("download_limit", ["give KEY=VALUE"]),
])
def test_a_set_install_cannot_do_is_refused_writing_nothing(rig, pair, words):
    r = _install("--owner", OWNER, "--set", pair)
    _refused(r, *words)
    assert not _main().exists() and not rig.exists()


# ---- bot status ----------------------------------------------------------------------

def test_bot_status_shows_the_three_keys_and_their_sources(rig):
    _write(_main(), {"bots": {"ada": {"download_limit": "1000000", "download_types": "image,bogus"}},
                     "_locked": ["bots.ada.download_limit"]})
    assert _install("--owner", OWNER).exit_code == 0
    r = CliRunner().invoke(cli, ["bot", "status"])
    lines = r.output.splitlines()
    root = pathlib.Path(os.environ["SESSIONPHONE_ROOT"])
    assert "download_limit: 1000000 (1 MB)  (~/.sessionkit/phone/settings/phone.json)" in lines
    assert "download_types: image  (~/.sessionkit/phone/settings/phone.json)" in lines
    assert f"save_dir:       {settings.shown(root / 'attachments')}  (the default)" in lines
    assert any(ln.startswith("download: ") and "`bots.ada.download_types`" in ln and "bogus" in ln
               for ln in lines), r.output


# ---- the schema ----------------------------------------------------------------------

def _schema() -> dict:
    return json.loads(settings.schema_source())


def test_the_package_ships_a_schema_declaring_the_three_keys():
    s = _schema()
    assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    keys = s["properties"]["bots"]["additionalProperties"]["properties"]
    assert set(keys) == set(settings.HOST_ONLY)
    assert all(v["type"] == "string" for v in keys.values())
    assert keys["download_limit"]["default"] == str(settings.DEFAULT_LIMIT)
    assert keys["download_types"]["default"] == ",".join(settings.KINDS)


def test_the_schema_carries_no_override_or_env_keyword():
    """SM-990 is not ruled: no x-override keyword is shipped; and these keys
    are host-only, so no x-env names a variable for them."""
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)
    assert not [k for k in walk(_schema()) if k.startswith("x-")]


@pytest.mark.parametrize("key,value", [
    ("download_limit", "0"), ("download_limit", "16000000"), ("download_limit", "-1"),
    ("download_limit", "16MB"), ("download_types", "none"), ("download_types", "image, voice"),
    ("download_types", "image,pictures"), ("download_types", ""), ("save_dir", "/srv/x"),
    ("save_dir", "~/x"), ("save_dir", "rel"),
])
def test_the_schema_patterns_agree_with_the_reader(key, value):
    """The schema accepts exactly the values --set accepts (no note, no default)."""
    pattern = _schema()["properties"]["bots"]["additionalProperties"]["properties"][key]["pattern"]
    val, note = settings.parse_host(key, value)
    assert bool(re.search(pattern, value)) == (val is not None and note is None)


def test_install_puts_the_schema_beside_phone_json(rig):
    r = _install("--owner", OWNER)
    assert r.exit_code == 0, r.output
    p = _main().with_name(".phone.schema.json")
    assert p.read_text() == settings.schema_source()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not settings.install_schema()                  # the same text: unchanged
    p.write_text("{}")
    assert settings.install_schema() and p.read_text() == settings.schema_source()

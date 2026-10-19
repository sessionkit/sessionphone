"""`sessionphone web` and `sessionphone web enrol` (PHONE-133 build-001), and the
scratch-root guard every web test runs under (design §4)."""
from __future__ import annotations

import json
import os
import stat

from click.testing import CliRunner

from sessionphone import cli, paths
from sessionphone.web import passkeys as pk


def test_every_web_test_runs_on_a_scratch_root(scratch_root, tmp_path):
    assert os.environ["SESSIONPHONE_ROOT"] == str(scratch_root)
    assert paths.root() == scratch_root and tmp_path in scratch_root.parents


def test_enrol_prints_a_code_the_root_accepts(tmp_path):
    root = tmp_path / "r"
    r = CliRunner().invoke(cli.main, ["web", "enrol", "--root", str(root)])
    assert r.exit_code == 0, r.output
    code = r.output.split("enrol code:")[1].split()[0]
    ring = pk.Keyring(root / "web")
    assert stat.S_IMODE(ring.enrol.stat().st_mode) == 0o600
    assert stat.S_IMODE(ring.dir.stat().st_mode) == 0o700
    assert code not in ring.enrol.read_text()
    assert ring.check_enrol_code(code) == ""


def test_enrol_takes_the_groups_root_too(tmp_path):
    root = tmp_path / "g"
    r = CliRunner().invoke(cli.main, ["web", "--root", str(root), "enrol"])
    assert r.exit_code == 0, r.output
    assert (root / "web" / "enrol.json").exists()


def test_enrol_defaults_to_the_sessionphone_root(scratch_root):
    r = CliRunner().invoke(cli.main, ["web", "enrol"])
    assert r.exit_code == 0, r.output
    assert json.loads((scratch_root / "web" / "enrol.json").read_text())["wrong"] == 0


def test_with_no_root_set_the_default_is_home_dot_sessionphone(tmp_path, monkeypatch):
    """What the guard is for: with SESSIONPHONE_ROOT unset the root is ~/.sessionphone.
    HOME is pointed at tmp, so this proves the resolution without touching the real one."""
    monkeypatch.delenv("SESSIONPHONE_ROOT")
    monkeypatch.setenv("HOME", str(tmp_path))
    r = CliRunner().invoke(cli.main, ["web", "enrol"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / ".sessionphone" / "web" / "enrol.json").exists()


def test_web_refuses_a_public_bind(tmp_path):
    r = CliRunner().invoke(cli.main, ["web", "--root", str(tmp_path), "--host", "0.0.0.0"])
    assert r.exit_code == 2 and "refused" in r.output
    assert not (tmp_path / "web").exists()


def test_web_refuses_a_tailnet_bind_without_tls(tmp_path):
    r = CliRunner().invoke(cli.main, ["web", "--root", str(tmp_path), "--host", "100.95.47.50"])
    assert r.exit_code == 2 and "--tls" in r.output


def test_web_help_names_the_flags_and_the_rule():
    r = CliRunner().invoke(cli.main, ["web", "--help"])
    assert r.exit_code == 0
    for flag in ("--port", "--root", "--origin", "--tls", "--host", "enrol", "never `funnel`"):
        assert flag in r.output
    assert "[default: 8775]" in r.output

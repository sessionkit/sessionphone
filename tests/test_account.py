"""The setup verbs that need no network: the root, the callers file, the invite, the page."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from sessionphone import account, paths
from sessionphone.bridge import dc_env
from sessionphone.cli import main

K1 = "753E8B231F10DDFB82D742DE9D43B582A752BA4C"     # tests/fixtures/openpgp/test-v4-key-1.b64
LINK = "https://i.delta.chat/#ABCDEF0123&v=3&i=x&s=y&a=bot%40relay.example&n=Test%20Agent"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "phone"
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(r))
    return r


def test_root_follows_the_environment(root) -> None:
    assert paths.root() == root
    assert dc_env.callers_file() == root / "callers.txt"
    assert dc_env.accounts_dir() == root / "accounts"


def test_create_makes_it_private(root) -> None:
    paths.ensure_private(create=True)
    assert root.stat().st_mode & 0o777 == 0o700


def test_a_missing_root_is_not_configured(root) -> None:
    with pytest.raises(paths.NotConfigured):
        paths.ensure_private()


def test_a_wide_root_is_refused(root) -> None:
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(paths.Unsafe):
        paths.ensure_private()


def test_no_callers_file_means_nobody(root) -> None:
    assert account.read_callers() == []


def test_callers_add_is_idempotent_lowercase_and_private(root) -> None:
    paths.ensure_private(create=True)
    assert account.add_caller("Owner@Example.org", "owner") is True
    assert account.add_caller("owner@example.org") is False
    assert account.read_callers() == ["owner@example.org"]
    assert dc_env.callers_file().stat().st_mode & 0o777 == 0o600
    assert dc_env.callers_file().read_text() == "owner@example.org  # owner\n"


def test_callers_file_comments_and_blank_lines(root) -> None:
    paths.ensure_private(create=True)
    dc_env.callers_file().write_text("# who may ring\n\nA@x.org  # owner\nb@y.org\n")
    assert account.read_callers() == ["a@x.org", "b@y.org"]


def test_a_non_address_is_refused(root) -> None:
    paths.ensure_private(create=True)
    with pytest.raises(ValueError):
        account.add_caller("not an address")


def test_invite_name_decodes_n(root) -> None:
    assert account.invite_name(LINK) == "Test Agent"
    assert account.invite_name("https://i.delta.chat/#ABC&v=3") == ""


def test_display_name_comes_from_the_saved_invite(root) -> None:
    paths.ensure_private(create=True)
    dc_env.invite_file().write_text(LINK + "\n")
    assert account.display_name() == "Test Agent"


def test_qr_page_fills_every_placeholder(root) -> None:
    page = account.qr_page("Test Agent", link=LINK)
    assert "{{" not in page
    assert "Connect to Test Agent" in page
    assert LINK in page
    assert "<svg" in page


def test_qr_page_refuses_a_template_with_no_placeholder(root, tmp_path) -> None:
    bad = tmp_path / "bad.html"
    bad.write_text("<p>hello</p>")
    with pytest.raises(ValueError):
        account.qr_page("x", template=bad, link=LINK)


def test_cli_callers_before_setup_exits_2(root) -> None:
    result = CliRunner().invoke(main, ["callers"])
    assert result.exit_code == 2
    assert "sessionphone setup" in result.output


def test_cli_callers_add_then_list(root) -> None:
    paths.ensure_private(create=True)
    runner = CliRunner()
    added = runner.invoke(main, ["callers", "add", "Owner@Example.org", "--note", "owner", "--key", K1])
    assert added.exit_code == 0, added.output
    assert f"approved: owner@example.org  key:{K1}" in added.output
    listed = runner.invoke(main, ["callers"])
    assert listed.output.splitlines() == [f"callers: {dc_env.callers_file()}",
                                          f"owner@example.org  key:{K1}"]


def test_cli_invite_before_setup_exits_2(root) -> None:
    assert CliRunner().invoke(main, ["invite"]).exit_code == 2

from click.testing import CliRunner

from sessionphone.cli import main


def test_help_lists_the_setup_verbs() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for verb in ("setup", "qr", "link", "invite", "callers"):
        assert verb in result.output

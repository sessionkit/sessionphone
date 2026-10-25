"""With nothing naming the bot, its name is the harness vendor's word (PHONE-247).

The bot's last-rung name used to be the literal "claude" (settings.DEFAULT_AGENT).
Now it is asked of sessiontools: `session agent default` prints the vendor's
word, the one the default seat is named by, so the bot and the default seat
are named alike. A sessiontools without the verb (0.3.0), a non-zero exit,
output that is not one word, a timeout or no `session` at all give "claude",
so the phone works with sessiontools 0.3.0.

Both readers ask the one function (settings.default_agent): install's pick
(settings.pick_name) and dc_env.agent_from, which bin/dc-env asks for the
dc-* tools. So the bot and the tools cannot disagree: each case below runs
all of them in one environment.

`session` is a fake on a tmp PATH; HOME is a tmp dir; sessionmail is a fake
`message` with no answer. Nothing real is asked.
"""
import os
import pathlib
import subprocess
import sys

import pytest

from sessionphone import settings
from sessionphone.bridge import dc_env

# conftest's no_vendor stands in for it in every test; these tests ask the real one.
REAL_DEFAULT_AGENT = settings.default_agent
REPO = pathlib.Path(__file__).resolve().parents[1]
DC_ENV = REPO / "bin" / "dc-env"

# What each fake `session` does when asked `agent default`. Every case but the
# first is a failure, and gives "claude".
CASES = {
    "verb present: its word is read": ("echo codex\n", "codex"),
    "the word is stripped and lowered": ("printf '  Codex  \\n'\n", "codex"),
    "verb missing, as session's own dispatcher says it (exit 2)": (
        "echo \"session: unknown verb 'agent'\" >&2\necho 'usage: session VERB ...'\nexit 2\n", "claude"),
    "verb missing, as click says it (exit 2)": (
        ("echo 'Usage: session [OPTIONS] COMMAND' >&2\n"
         "echo \"Error: No such command 'agent'.\" >&2\nexit 2\n"), "claude"),
    "non-zero exit with a word on stdout": ("echo codex\nexit 1\n", "claude"),
    "empty output": ("exit 0\n", "claude"),
    "only blank lines": ("printf '\\n\\n'\n", "claude"),
    "two words": ("echo 'codex agent'\n", "claude"),
    "two lines": ("printf 'codex\\nclaude\\n'\n", "claude"),
    "not a name": ("echo '../codex'\n", "claude"),
}


def fake_session(where: pathlib.Path, body: str) -> pathlib.Path:
    """A `session` in WHERE that answers `agent default` with BODY, logging each ask."""
    where.mkdir(parents=True, exist_ok=True)
    exe = where / "session"
    exe.write_text('#!/bin/sh\n'
                   'echo "$*" >> "$(dirname "$0")/asked"\n'
                   'if [ "$*" != "agent default" ]; then echo "fake session: not this: $*" >&2; exit 3; fi\n'
                   + body)
    exe.chmod(0o755)
    return exe


def asks(where: pathlib.Path) -> list[str]:
    log = where / "asked"
    return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def vendor(monkeypatch):
    """The real default_agent, its answers forgotten before and after."""
    monkeypatch.setattr(settings, "default_agent", REAL_DEFAULT_AGENT)
    settings.forget_default_agent()
    yield
    settings.forget_default_agent()


def shell_with(bindir: pathlib.Path | None) -> dict:
    path = os.pathsep.join(p for p in (str(bindir) if bindir else "", "/usr/bin:/bin") if p)
    return {"PATH": path, "HOME": "/nonexistent"}


# ---- in process: default_agent, pick_name and agent_from -----------------------

@pytest.mark.parametrize("case", list(CASES))
def test_default_agent_reads_the_vendor_word_or_falls_back(vendor, tmp_path, case):
    body, want = CASES[case]
    fake_session(tmp_path / "bin", body)
    shell = shell_with(tmp_path / "bin")
    assert settings.default_agent(shell) == want
    assert asks(tmp_path / "bin") == ["agent default"]


@pytest.mark.parametrize("case", list(CASES))
def test_install_and_the_dc_tools_pick_one_name(vendor, tmp_path, case):
    """pick_name (the bot) and dc_env.agent_from (the dc-* tools) agree."""
    body, want = CASES[case]
    fake_session(tmp_path / "bin", body)
    shell = shell_with(tmp_path / "bin")
    assert settings.pick_name(shell, []) == (want, "the default")
    assert dc_env.agent_from(shell) == (want, "the default")


def test_no_session_binary_gives_claude(vendor, tmp_path):
    shell = shell_with(tmp_path / "empty-bin")
    assert settings.default_agent(shell) == "claude"
    assert settings.pick_name(shell, []) == ("claude", "the default")
    assert dc_env.agent_from(shell) == ("claude", "the default")


def test_a_timeout_gives_claude(vendor, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "VENDOR_TIMEOUT_S", 0.3)
    fake_session(tmp_path / "bin", "exec sleep 10\n")
    shell = shell_with(tmp_path / "bin")
    assert settings.default_agent(shell) == "claude"
    assert settings.pick_name(shell, []) == ("claude", "the default")
    assert dc_env.agent_from(shell) == ("claude", "the default")


def test_a_session_that_cannot_be_run_gives_claude(vendor, tmp_path, monkeypatch):
    fake_session(tmp_path / "bin", "echo codex\n")

    def cannot(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(settings.subprocess, "run", cannot)
    assert settings.default_agent(shell_with(tmp_path / "bin")) == "claude"


def test_an_unusable_phone_json_gives_the_vendor_word_too(vendor, tmp_path):
    """agent_from's except branch (phone.json cannot be used) names the bot
    as the last rung does, and BAD says so with that name."""
    fake_session(tmp_path / "bin", "echo codex\n")
    pathlib.Path(settings.FILE).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(settings.FILE).write_text("{not json")
    del dc_env.BAD[:]
    name, came = dc_env.agent_from(shell_with(tmp_path / "bin"))
    assert name == "codex"
    assert came.startswith("the default (") and came.endswith("cannot be used)")
    assert dc_env.BAD and dc_env.BAD[-1].endswith("Using codex for now.")
    del dc_env.BAD[:]


def test_a_name_given_is_never_overridden_by_the_vendor(vendor, tmp_path):
    fake_session(tmp_path / "bin", "echo codex\n")
    shell = {**shell_with(tmp_path / "bin"), "DC_AGENT": "ada"}
    assert settings.pick_name(shell, []) == ("ada", "DC_AGENT in this shell")
    assert dc_env.agent_from(shell) == ("ada", "DC_AGENT in this shell")
    assert settings.pick_name(shell_with(tmp_path / "bin"), ["bea"])[0] == "bea"
    assert asks(tmp_path / "bin") == []


def test_asked_once_per_session_binary(vendor, tmp_path):
    """agent_from is called by several dc_env helpers: one ask per process."""
    fake_session(tmp_path / "bin", "echo codex\n")
    shell = shell_with(tmp_path / "bin")
    for _ in range(3):
        assert settings.default_agent(shell) == "codex"
    assert asks(tmp_path / "bin") == ["agent default"]
    settings.forget_default_agent()
    assert settings.default_agent(shell) == "codex"
    assert asks(tmp_path / "bin") == ["agent default", "agent default"]


def test_the_suite_never_asks_the_real_vendor():
    """conftest's no_vendor: without the `vendor` fixture, the fallback."""
    assert settings.default_agent is not REAL_DEFAULT_AGENT
    assert settings.default_agent({"PATH": "/nonexistent"}) == settings.DEFAULT_AGENT == "claude"


# ---- out of process: the bot's pick, dc_env.py and bin/dc-env, in one HOME -------

NO_ANSWER = '#!/bin/sh\necho "fake message: no answer" >&2\nexit 1\n'
INSTALL = """import os
from sessionphone import settings
print(settings.bot_name(os.environ, None, ask=True)[0])
"""
PYTHON = "from sessionphone.bridge import dc_env\nprint(dc_env.AGENT)\n"
SHELL = 'source "$1"; printf "%s\\n" "$DC_AGENT"'


@pytest.mark.parametrize("case", ["verb present: its word is read",
                                  "verb missing, as session's own dispatcher says it (exit 2)",
                                  "empty output"])
def test_the_bot_and_the_dc_tools_agree_out_of_process(tmp_path, case):
    body, want = CASES[case]
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    fake_session(bindir, body)
    message = tmp_path / "mail" / "message"
    message.parent.mkdir()
    message.write_text(NO_ANSWER)
    message.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin", "DC_MESSAGE": str(message),
           "LANG": os.environ.get("LANG", "C.UTF-8")}

    def run(argv):
        r = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True,
                           timeout=60, check=False)
        assert r.returncode == 0, r.stderr[-2000:]
        return r.stdout.splitlines()[0]

    got = {"install": run([sys.executable, "-c", INSTALL]),
           "dc_env.py": run([sys.executable, "-c", PYTHON]),
           "bin/dc-env": run(["bash", "-c", SHELL, "dc-tool", str(DC_ENV)])}
    assert got == dict.fromkeys(got, want)

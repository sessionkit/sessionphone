"""A bot named only in argv mails as that agent (PHONE-142).

The launchd job runs `python -m sessionphone.bridge.bot AGENT`, and a
hand-made plist may carry the agent there and nowhere else: no DC_AGENT in its
env. bot.py copies argv[1] into DC_AGENT, but dc_env freezes AGENT, ADDR and
MAILTO at import. Since 17be14d (PHONE-102) bot.py's first sessionphone import,
`account`, imports dc_env, so the copy came too late and the bot mailed as and
to `claude`. These tests start the bot module in a fresh process, as launchd
does, with the agent only in argv, and pin who it is.
"""
import ast
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
BOT = REPO / "src/sessionphone/bridge/bot.py"
SEAT = ("CLAUDE_CODE_AGENT", "SESSIONPHONE_ROOT")

# runpy, not -m: run_name keeps main() (and so a live bot) from starting,
# while the module still runs top to bottom with sys.argv as launchd sets it.
PROBE = ("import json, runpy, sys; sys.argv = ['bot.py', *sys.argv[1:]]; "
         "runpy.run_module('sessionphone.bridge.bot', run_name='argv_probe'); "
         "from sessionphone.bridge import dc_env; "
         "print(json.dumps([dc_env.AGENT, dc_env.ADDR, dc_env.MAILTO]))")


def _bot_identity(tmp_path, *argv, **env):
    """-> [AGENT, ADDR, MAILTO] as a bot started `bot.py *argv` with env sees them."""
    full = {k: v for k, v in os.environ.items()
            if k not in SEAT and not k.startswith(("DC_", "PIP_CHAT_"))}
    # a hand-made plist's env: a mail domain and a root, no agent anywhere
    full.update(DC_HOST="host.example", DC_ROOT=str(tmp_path / "root"), DC_CALLS="off", **env)
    r = subprocess.run([sys.executable, "-c", PROBE, *argv], cwd=REPO, env=full,
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_agent_named_only_in_argv_is_who_the_bot_is(tmp_path):
    assert _bot_identity(tmp_path, "carol") == ["carol", "carol-chat@host.example", "carol"]   # MAILTO bare (PHONE-248)


def test_dc_agent_in_the_environment_still_wins_over_argv(tmp_path):
    assert _bot_identity(tmp_path, "carol", DC_AGENT="alice") == \
        ["alice", "alice-chat@host.example", "alice"]


def test_with_no_agent_anywhere_the_bot_is_claude(tmp_path):
    assert _bot_identity(tmp_path) == ["claude", "claude-chat@host.example", "claude"]


def test_the_argv_fill_comes_before_every_sessionphone_import():
    """Not import order by luck: bot.py fills DC_AGENT from argv before it
    imports anything of sessionphone's, so no module can freeze the agent first."""
    body = ast.parse(BOT.read_text()).body
    fill = next(i for i, n in enumerate(body)
                if isinstance(n, ast.If) and "sys.argv" in ast.unparse(n.test)
                and 'os.environ["DC_AGENT"]' in ast.unparse(n).replace("'", '"'))
    ours = [n.lineno for n in body[:fill]
            if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "sessionphone"
            or isinstance(n, ast.Import) and any(a.name.split(".")[0] == "sessionphone" for a in n.names)]
    assert ours == [], f"bot.py imports sessionphone at line(s) {ours}, before the argv fill"

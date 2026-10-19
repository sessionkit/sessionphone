"""Every Rpc(...) in the package names its server.

A bare Mac has no `deltachat-rpc-server` on PATH, and a stale one on PATH
speaks an older protocol, so each Rpc must be given `server_path()` - the
server installed beside this interpreter. The bot's own Rpc did not, and the
launchd bot from `sessionphone bot install` crash-looped every 30 s on a bare
Mac (review finding B1, 2030-06-09).
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sessionphone"


def rpc_calls():
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Rpc":
                yield path.relative_to(SRC), node


def test_there_are_rpc_calls_to_check() -> None:
    """Not vacuous: the scan finds the setup's and the bot's."""
    found = {str(path) for path, _ in rpc_calls()}
    assert {"account.py", "bridge/bot.py"} <= found


def test_every_rpc_names_its_server() -> None:
    missing = [f"{path}:{node.lineno}" for path, node in rpc_calls()
               if "rpc_server_path" not in {kw.arg for kw in node.keywords}]
    assert missing == []


def test_the_server_is_found_from_the_package_not_the_interpreter(monkeypatch) -> None:
    """Renamed interpreter, nothing beside it: still the venv's server (PHONE-80)."""
    import sys

    from sessionphone import account
    monkeypatch.setattr(sys, "executable", "/nowhere/libexec/session-phone")
    path, where, version = account.server_choice()
    assert where == "package" and path.endswith("deltachat_rpc_server/deltachat-rpc-server")
    assert version[0].isdigit()
    assert account.server_path() == path


def test_no_server_in_the_venv_says_path(monkeypatch) -> None:
    import importlib.util
    import sys

    from sessionphone import account
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(sys, "prefix", "/nowhere")
    monkeypatch.setattr(sys, "executable", "/nowhere/bin/python")
    assert account.server_choice()[:2] == ("deltachat-rpc-server", "PATH")

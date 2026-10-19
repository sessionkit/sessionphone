"""The media pack: what the bot, calls and `sessionphone web` need, and setup does not.

The six modules below are the `media` extra in pyproject.toml.
`setup`, `qr`, `link` and the other light verbs run on the four base deps
alone, so a first run downloads about 10 MB, not 52.5 MB. The verbs that do
need the pack (`bot install`, `bot run`, `web` and `web enrol`) call
`require()` first. When the pack is missing they refuse with exit 2 and a plain
message that says how to fetch it, before anything is written: no launchd job,
no channels seed, and no traceback.

`bot install` and `bot run` are calls-only media verbs: with `DC_CALLS=off`
the bot they start never imports the pack, so `require()` lets them pass. A
calls-on bot without the pack still refuses, as before.

The bot itself asks too, because launchd runs it as `python -m
sessionphone.bridge.bot`, never through the CLI. `missing()` answers at start.
`gone()` answers under a live bot, whose loaded modules hide a pack that has
been removed from disk since. `calls_refused()` is the words it refuses a
call with.

This module is imported at CLI load, so it must stay light: the standard
library only, and it never imports the modules it looks for.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import pathlib
import sys

# the import names of the `media` extra's six requirements (pyproject.toml);
# each import name is also its distribution's name
MODULES = ("aiortc", "aioice", "av", "numpy", "websockets", "cryptography")

# The pack's download beyond the light four: 30.8 MB of wheels for cp313 on an
# arm64 Mac at macOS 14 or later, measured from uv.lock (scripts/lock-weight.py).
# bin/sessionphone says the same size before it fetches.
SIZE = "about 31 MB"

WHY = "the bot, calls and the web page use it for WebRTC audio and passkeys"

# The verbs that need the pack only for calls: the bot they start runs chat
# without it when DC_CALLS=off. The call sites in cli.py stay as
# they are; the condition lives here.
CALLS_ONLY = ("bot install", "bot run")


class Missing(Exception):
    """A media verb ran in an environment without the media pack. Exit code 2."""

    def __init__(self, verb: str, names: list[str], how: str):
        self.verb, self.names, self.how = verb, list(names), how
        super().__init__(
            f"`{verb}` needs the media pack, and this environment does not have it "
            f"(missing: {', '.join(names)}).\n"
            f"  why:   {WHY}; setup, qr and link do not need it.\n"
            f"  size:  {SIZE} to download, once.\n"
            f"  fetch: {how}\n"
            f"Nothing was changed.")


def missing() -> list[str]:
    """The pack's modules this interpreter cannot find, in MODULES order.

    `importlib.util.find_spec` on a top-level name looks for the module and
    imports nothing. A None result is missing; so is a raised ImportError,
    which is what a meta-path finder that blocks a module raises instead of
    returning None (measured). A ValueError means the module is already in
    sys.modules with no spec, so it is present.
    """
    out = []
    for name in MODULES:
        try:
            spec = importlib.util.find_spec(name)
        except ImportError:
            spec = None
        except ValueError:
            continue
        if spec is None:
            out.append(name)
    return out


def gone() -> list[str]:
    """The pack's modules a FRESH import could not find now, in MODULES order.

    `missing()` cannot see a pack removed under a live process: find_spec
    returns the spec of a module already in sys.modules and never looks at
    disk. This asks every finder on sys.meta_path, the way a first import
    would, and skips sys.modules. A raised ImportError is missing, as in
    `missing()`; so is no finder finding it. Imports nothing.
    """
    importlib.invalidate_caches()     # a FileFinder's cached listing must not hide a deletion
    return [name for name in MODULES if _fresh_spec(name) is None]


def _fresh_spec(name: str):
    for finder in sys.meta_path:
        find = getattr(finder, "find_spec", None)
        if find is None:
            continue
        try:
            spec = find(name, None)
        except ImportError:
            return None
        if spec is not None:
            return spec
    return None


def calls_off(environ=None) -> bool:
    """Is DC_CALLS off? Only "off" is off, as the bot reads it (bot.py CALLS).

    DC_CALLS alone: `bot install` carries DC_* into the job and nothing else
    (service.job), so the deprecated PIP_CHAT_CALLS would pass the check here
    and start a calls-on bot. Read here, not through dc_env, which looks up
    the host when it is imported."""
    e = os.environ if environ is None else environ
    return e.get("DC_CALLS") == "off"


def calls_refused(names: list[str], prefix: str | None = None) -> str:
    """Why a bot without the pack refuses a call: one paragraph, for a job's
    ack and for bot.log. The fix names `bot install`, which fetches the pack
    (through the plugin's launcher) and restarts the bot's job."""
    return (f"calls need the media pack, and this bot's environment does not have it "
            f"(missing: {', '.join(names)}). fetch: {how_to_fetch('bot install', prefix)} "
            f"Then restart the bot. Chat still works.")


def _receipt(prefix: pathlib.Path) -> dict | None:
    """The `[tool]` table of a uv tool env's receipt, or None if this is not one."""
    f = prefix / "uv-receipt.toml"
    if not f.is_file():
        return None
    import tomllib  # stdlib; only when there is a receipt to read
    try:
        return tomllib.loads(f.read_text(encoding="utf-8")).get("tool") or {}
    except (OSError, ValueError):
        return {}


def how_to_fetch(verb: str, prefix: str | None = None) -> str:
    """One line on how to add the pack, fitted to the environment this runs in.

    - The plugin's own venv (`<root>/.venv` beside `<root>/bin/sessionphone`):
      run the verb through the plugin's launcher, which fetches the pack once.
    - sessionphone's own uv tool env: reinstall the tool with `[media]` (as an
      editable from the same checkout, when it is one).
    - another tool's uv env that carries sessionphone (sessionkit's, say):
      reinstall THAT tool with the pack in it. Never a second tool env for a
      command the kit already puts on the PATH.
    - anything else: add `sessionphone[media]` to this environment.
    """
    env = pathlib.Path(prefix or sys.prefix)
    root = env.parent
    launcher = root / "bin" / "sessionphone"
    if env.name == ".venv" and launcher.is_file() and (root / "pyproject.toml").is_file():
        return (f"run `{launcher} {verb}`: the plugin's launcher fetches the pack once, "
                f"then runs the verb.")
    tool = _receipt(env)
    if tool is not None:
        reqs = [r for r in tool.get("requirements") or [] if isinstance(r, dict)]
        first = reqs[0] if reqs else {}
        name = str(first.get("name") or "")
        if name == "sessionphone":
            editable = first.get("editable")
            spec = f"-e '{editable}[media]'" if editable else "'sessionphone[media]'"
            return f"`uv tool install {spec}` (this is sessionphone's own uv tool env, {env})."
        if name:
            return (f"this sessionphone came with the `{name}` tool ({env}): reinstall {name} with the "
                    f"pack in the same env, `uv tool install {name} --with 'sessionphone[media]'`.")
    return (f"add `sessionphone[media]` to the environment this command runs from ({env}), "
            f"e.g. `pip install 'sessionphone[media]'` with that environment's pip.")


def require(verb: str) -> None:
    """Refuse `verb` (raise Missing) unless every module of the pack can be found.

    Call it FIRST in a media verb, before anything is written. `bot install`
    checks here for the job it writes: the job runs this venv's own
    interpreter (`procname.named_python()` hard-links it), so a check in this
    process answers for the job.

    `bot install` and `bot run` pass with DC_CALLS=off (CALLS_ONLY): the bot
    they start then never imports the pack. Any other DC_CALLS keeps the
    refusal.
    """
    if verb in CALLS_ONLY and calls_off():
        return
    names = missing()
    if names:
        raise Missing(verb, names, how_to_fetch(verb))

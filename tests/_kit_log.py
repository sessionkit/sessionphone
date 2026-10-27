"""Where a bot's logs are written, spelled out for the tests.

Literal on purpose: the suite pins the layout ~/.sessionkit/phone/log/<bot>[/<profile>]/
itself, so a change to sessionphone.paths.log_dir that moves a log fails here.
HOME is the test's own tmp home (conftest.home_is_tmp), read when called.
"""
import os
from pathlib import Path


def kit_log(bot: str, profile: str = "", name: str = "bot.log") -> Path:
    """The log NAME (bot.log, or bot.launchd.log) of bot BOT's PROFILE."""
    d = Path(os.environ["HOME"]) / ".sessionkit" / "phone" / "log" / bot
    return (d / profile if profile else d) / name

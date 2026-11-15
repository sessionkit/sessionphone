"""Where a bot's logs are written, spelled out for the tests.

Literal on purpose: the suite pins the layout itself, so a change to
sessionphone.paths.log_dir that moves a log fails here. Under the default root
a bot's logs are in the kit's folder, ~/.sessionkit/phone/log/<bot>[/<profile>]/
(kit_log); under any other root they are in that root,
<root>/log/<bot>[/<profile>]/ (root_log, PHONE-305).
HOME is the test's own tmp home (conftest.home_is_tmp), read when called.
"""
import os
from pathlib import Path


def kit_log(bot: str, profile: str = "", name: str = "bot.log") -> Path:
    """The log NAME (bot.log, or bot.launchd.log) of bot BOT's PROFILE, under
    the default root."""
    d = Path(os.environ["HOME"]) / ".sessionkit" / "phone" / "log" / bot
    return (d / profile if profile else d) / name


def root_log(root, bot: str, profile: str = "", name: str = "bot.log") -> Path:
    """The log NAME of bot BOT's PROFILE under ROOT, a root that is not the default."""
    d = Path(root) / "log" / bot
    return (d / profile if profile else d) / name

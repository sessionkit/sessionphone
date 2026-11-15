"""The call sockets' index: where a reader finds this bot's call legs.

A bot's call sockets (call-mouth.sock, call-ears.sock) are in its home, under
its root, and the root can come from the bot's job (DC_ROOT), this shell
(SESSIONPHONE_ROOT), phone.json or the default. Another program on the Mac,
the mouth or the ears, cannot work that out. So while a leg serves, the bot
keeps a symlink to the socket it actually bound in one fixed folder of the kit
(paths.run_dir, paths.run_entry):

    ~/.sessionkit/phone/run/<bot>[.<profile>]/call-mouth.sock -> <the bound socket>
    ~/.sessionkit/phone/run/<bot>[.<profile>]/call-ears.sock  -> <the bound socket>

run/ and each entry folder are 0700. The link is made after the bind and
replaces a stale one; at the bot's exit it is removed only if it still points
at this bot's socket, never one another bot put there. It links the path the
leg bound, so DC_MOUTH_SOCK and DC_EARS_SOCK are covered too.

Only the bridge's legs publish (mouthleg.start, earleg.start). The web demo's
legs (web/call.py --legs) call serve() and publish nothing.

The index is a help to readers, never a condition of serving: whatever fails
here is logged and the leg goes on.
"""
import atexit
import os
import pathlib

from sessionphone import paths
from sessionphone.bridge import dc_env

MOUTH = "call-mouth.sock"
EARS = "call-ears.sock"


def _part(word: str) -> bool:
    """Is WORD fit to be one folder name? Never empty, '.', '..' or holding a /."""
    return bool(word) and word not in (".", "..") and os.sep not in word and "\0" not in word


def _private_dirs(entry: pathlib.Path) -> None:
    """Make ENTRY and every missing folder above it 0700 (paths.make_log_dir
    keeps that rule), then leave run/ and ENTRY 0700 even if they were there."""
    paths.make_log_dir(entry)
    os.chmod(entry.parent, 0o700)
    os.chmod(entry, 0o700)


def publish(name, sock, bot=None, profile=None, log=print):
    """Link run/<bot>[.<profile>]/NAME to SOCK, the socket a leg just bound.
    BOT is dc_env.AGENT and PROFILE dc_env.PROFILE unless given. A link already
    there (a bot that died, or this one before a restart) is replaced in one
    rename. -> the link's path, or None when it could not be made (logged).
    Never raises."""
    try:
        bot = dc_env.AGENT if bot is None else bot
        profile = dc_env.PROFILE if profile is None else profile
        if not _part(bot) or (profile and not _part(profile)):
            log(f"call socket index: not published: bot {bot!r} profile {profile!r} "
                "is not a folder name")
            return None
        target = os.path.abspath(os.fspath(sock))
        entry = paths.run_entry(bot, profile)
        _private_dirs(entry)
        link = entry / name
        tmp = entry / f".{name}.{os.getpid()}.tmp"
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        os.symlink(target, tmp)
        try:
            os.replace(tmp, link)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        log(f"call socket index: {link} -> {target}")
        return link
    except Exception as e:  # noqa: BLE001 - the index never stops a leg
        log(f"call socket index: {name} not published ({type(e).__name__}: {e}); "
            "the leg serves on")
        return None


def unpublish(link, sock):
    """Remove LINK if it still points at SOCK; a link another bot put there, or
    anything that is not a link, is left alone. -> True when removed. Never
    raises."""
    try:
        if os.readlink(link) != os.path.abspath(os.fspath(sock)):
            return False
        os.unlink(link)
        return True
    except (OSError, ValueError):
        return False


def published(name, sock, log=print, at_exit=atexit.register):
    """publish() SOCK as NAME, and remove the link at this process's exit if it
    is still ours (unpublish). -> the link, or None."""
    link = publish(name, sock, log=log)
    if link is not None:
        at_exit(unpublish, link, sock)
    return link

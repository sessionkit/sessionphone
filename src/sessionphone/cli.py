"""sessionphone - ring your Claude Code session from your phone over Delta Chat.

    sessionphone setup --name NAME   make this agent's account, print the invite
    sessionphone qr                  the connect page (HTML + QR) on stdout
    sessionphone link                wait for the person to scan; they become the owner
    sessionphone invite [--qr [--invert]] [--png FILE]  print the saved invite link (or its QR)
    sessionphone callers [add|pin ADDR [--key FPR]]  who may ring this agent
    sessionphone avatar [PATH | --clear]  show, set or clear the bot's picture
    sessionphone channels [list|add NAME TARGET|seed]  where the bot may send
    sessionphone channels rejected [--profile public]  who the public bot turned away
    sessionphone bot install [--owner ADDR [--owner-key FPR]] [--forget KEY]  run the resident bot under launchd
    sessionphone bot status|uninstall|run
    sessionphone bot migrate [--dry-run]  move the old default root into ~/.sessionkit/phone/state
    sessionphone web [--port --root --origin --tls --host --legs --mail-to --heard-dir]
                                     a browser tab as a call, passkey first
    sessionphone web enrol           a one-time code (and its QR) to enrol a passkey on the web page

The order on a fresh Mac is setup, qr, link (backgrounded, BEFORE they
scan), then the resident bot. `link` and the bot cannot hold the account
at the same time.
"""
from __future__ import annotations

import os
import pathlib
import plistlib
import shlex
import sys

import click

from sessionphone import (
    account,
    avatar,
    channels,
    displayname,
    hearing,
    keygate,
    media,
    paths,
    seat,
    service,
    settings,
)
from sessionphone.bridge import dc_env

CTX = {"help_option_names": ["-h", "--help"]}


def _fail(exc: Exception, code: int) -> None:
    click.echo(f"sessionphone: {exc}", err=True)
    sys.exit(code)


class _Main(click.Group):
    """The verbs, with one refusal shared by all of them: a phone.json that
    cannot be used, found where a verb asks for the root (paths.root reads
    the root it keeps), is exit 2 naming the file, never a traceback."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except settings.Bad as exc:
            _fail(exc, 2)


@click.group(cls=_Main, context_settings=CTX, invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """sessionphone - ring your Claude Code session from your phone over Delta Chat."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command(context_settings=CTX)
@click.option("--name", required=True, help="the name people see in their chat list")
@click.option("--status", default="", help="the profile line under the name")
@click.option("--relay", default=None, help="a DCACCOUNT: QR for one chatmail relay (default: the fastest public one)")
def setup(name: str, status: str, relay: str | None) -> None:
    """Make this agent's account on a public chatmail relay; print the invite link.

    Then it makes the box Delta text goes to (DC_MAILTO, else the bare <agent>)
    with sessionmail's `message setup --box <agent>`, proves it with `message
    path`, and prints the one step that makes a Claude session hear it.
    Last, BEFORE the phone is linked, its `hearing:` lines say whether the bot
    will hear the caller: it sends a half-second test clip to the STT the bot
    will use (DC_STT_URL in this shell, which `bot install` carries into the
    job, else the default), saying so first. When nothing answers it names
    the install step: under Session Kit `ears services install --only
    speech`, else VoiceMode's. Said, never a refusal: the exit code is the same.
    Idempotent: run it again and it keeps the account, sets the name, and
    prints the same invite. Exit 0 done, 1 it failed (the reason is printed),
    2 the box could not be made (the step to run is printed).
    """
    try:
        info = account.setup(name, status, relay)
    except Exception as exc:  # noqa: BLE001 - the operator gets the message, not a traceback
        _fail(exc, 1)
        return
    click.echo(f"root:    {info['root']}")
    click.echo(f"address: {info['addr']}")
    click.echo(f"name:    {info['displayname']} ({'created' if info['created'] else 'already existed'})")
    click.echo(f"saved:   {info['invite_file']}")
    click.echo(f"INVITE: {info['invite']}")
    env = dict(os.environ)
    try:
        seat.pin_message(env, os.environ)
        box = seat.ensure_box(env, os.environ)
    except (seat.NoSigner, seat.NoBox) as exc:
        _fail(exc, 2)
        return
    click.echo(f"box:     {box.line()}")
    if box.path is not None:
        click.echo(f"hear:    {seat.hear_line(box.name, os.environ)}")
    _setup_hearing(name)


def _setup_hearing(name: str) -> None:
    """setup's last lines: will the bot hear the caller? Asked BEFORE the phone
    is linked, so a missing speech server is found while there is
    time to install it, not at `bot install`, the last step. Judged on the
    environment `bot install` will give the job (service.job: its PATH and the
    DC_* set in this shell), by the same probe and lines as install's. Its
    check-again line is setup itself: before a phone is linked, `bot install`
    refuses before it probes. Said, never a refusal: the exit code stays 0."""
    env = {"PATH": service.BASE_PATH,
           **{k: v for k, v in os.environ.items() if k.startswith("DC_") and v}}
    v = hearing.probe(env, say=click.echo)
    w = hearing.weights_for(v, shell=os.environ)
    again = f"sessionphone setup --name {shlex.quote(name)}"
    for line in hearing.lines(v, env, shell=os.environ, weights=w, again=again, before_link=True):
        click.echo(line)


@main.command(context_settings=CTX)
@click.option("--qr", "as_qr", is_flag=True,
              help="print the invite as a QR to scan off the terminal, a blank line, then the link")
@click.option("--invert", is_flag=True,
              help="with --qr: dark modules as blocks, for a light-theme terminal")
@click.option("--png", "png_file", type=click.Path(path_type=pathlib.Path), default=None,
              metavar="FILE", help="write the QR to FILE as a PNG and print its absolute path, the last line")
def invite(as_qr: bool, invert: bool, png_file: pathlib.Path | None) -> None:
    """Print the saved invite link: what a phone scans to reach this agent.

    With --qr, the link as a QR in Unicode half-blocks, no colour codes. By
    default a dark module is a space, the right way round on a dark-theme
    terminal; on a light theme add --invert. With --png FILE, the same QR as
    a PNG (overwritten if it exists; its directory must exist), for `show-me`.
    Exit 0 done, 1 a bad flag or the PNG could not be written, 2 not set up
    or the root is unsafe.
    """
    from sessionphone import qrprint  # segno: only when asked

    if invert and not as_qr:
        _fail(Exception("--invert needs --qr"), 1)
        return
    try:
        link = account.invite()
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    written = None
    if png_file is not None:
        try:    # paths.*, not Path.*: on 3.14 a dir below an unsearchable one reads "absent"
            why = (None if paths.is_dir(png_file.parent) else
                   "not a directory" if paths.exists(png_file.parent) else "no such directory")
        except OSError as exc:
            why = f"cannot be read ({exc.strerror or exc})"
        if why:
            _fail(Exception(f"{why}: {png_file.parent} (for the PNG {png_file})"), 1)
            return
        try:
            written = qrprint.png(link, png_file)
        except OSError as exc:
            _fail(Exception(f"could not write {png_file}: {exc.strerror or exc}"), 1)
            return
    if as_qr:
        click.echo(qrprint.text(link, invert=invert), nl=False)
        click.echo()
        click.echo(link)
    elif written is None:
        click.echo(link)
    if written is not None:
        click.echo(str(written))


@main.command(context_settings=CTX)
@click.option("--name", default=None, help="the agent's name on the page (default: the account's display name)")
@click.option("--template", type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path), default=None,
              help="an HTML template with {{AGENT_NAME}} {{INVITE}} {{QR_SVG}} (default: the page the package ships)")
def qr(name: str | None, template: pathlib.Path | None) -> None:
    """Print the connect page, a QR of the invite link, ready for `> connect.html`."""
    try:
        agent_name = name or account.display_name() or "this agent"
        click.echo(account.qr_page(agent_name, template), nl=False)
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
    except ValueError as exc:
        _fail(exc, 1)


@main.command(context_settings=CTX)
@click.option("--timeout", type=float, default=600.0, show_default=True,
              help="seconds to wait for someone to scan; 0 waits forever")
@click.option("--no-greet", is_flag=True, help="don't send the owner a 'linked' message")
def link(timeout: float, no_greet: bool) -> None:
    """Hold the account until someone joins by the invite; approve the first as owner.

    Run it backgrounded BEFORE they scan: the join is a handshake, and with
    nothing holding the account their phone waits at "Establishing
    connection". Exit 0 someone joined, 124 nobody within the timeout,
    2 no account.
    """
    try:
        click.echo(f"account: {dc_env.accounts_dir()}")
        info = account.link(timeout or None, greet=not no_greet)
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    except Exception as exc:  # noqa: BLE001 - same reason as setup
        _fail(exc, 1)
        return
    if not info["joined"]:
        click.echo(f"sessionphone: nobody joined within {timeout:.0f}s", err=True)
        sys.exit(124)
    who = f"{info['name']} <{info['addr']}>" if info["name"] else info["addr"]
    if info["owner"]:
        click.echo(f"joined: {who}, now the OWNER and the first approved caller ({info['callers']}), "
                   f"bound to key {info.get('key')}")
        if info.get("tightened") is not None:
            _tightened(pathlib.Path(info["channels"]), info["tightened"])
        if info.get("listed"):
            click.echo(f"listed: {info.get('listed_as') or 'owner'}, the bot may send to them "
                       f"({info['channels']})")
            if info.get("loose") is not None:
                _loose(pathlib.Path(info["channels"]), info["loose"])
        elif info.get("channels_problem"):
            click.echo(f"sessionphone: NOT listed in {info['channels']}: {info['channels_problem']}",
                       err=True)
    elif info.get("key_problem"):
        click.echo(f"sessionphone: joined: {who}, but NOT approved to call: {info['key_problem']}",
                   err=True)
    else:
        click.echo(f"joined: {who}, linked for text; NOT approved to call "
                   f"(`sessionphone callers add {info['addr']} --key FPR` on the owner's word)")


def _callers_file() -> pathlib.Path:
    """The callers file the bot reads, from this shell's environment
    (dc_env.callers_file). Under the root, the root must be private; elsewhere
    its directory must exist. As _channels_file."""
    f = dc_env.callers_file()
    if f.parent == paths.root():
        paths.ensure_private()
    elif not f.parent.is_dir():
        raise paths.NotConfigured(f"no directory {f.parent} for {f}")
    return f


@main.group(context_settings=CTX, invoke_without_command=True)
@click.pass_context
def callers(ctx: click.Context) -> None:
    """Who may ring this agent: the owner first, one line each, `ADDR  key:FPR`.

    The bot answers a call only from a contact holding a key a line is
    pinned to. A line with no key is refused until one is pinned
    (`sessionphone callers pin ADDR`). DC_CALLERS grants nothing. An empty
    list means nobody: it fails closed.
    """
    if ctx.invoked_subcommand is not None:
        return
    try:
        f = _callers_file()
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    rows = account.read_caller_entries(f)
    if not rows:
        click.echo(f"nobody may ring yet ({f}); `sessionphone link` makes the owner")
        return
    click.echo(f"callers: {f}")
    for e in rows:
        if e.key:
            click.echo(f"{e.addr}  key:{e.key}")
        else:
            click.echo(f"{e.addr}  UNPINNED: refused until pinned (`sessionphone callers pin {e.addr}`)")


def _key_for(addr: str, key: str | None, pin_how: str = keygate.PIN_HOW) -> tuple[str, str | None]:
    """-> (key, the line to print about where it came from). --key wins; else
    the attended pin pass (PIN_HOW is the command its refusal names). Exits 1,
    having written nothing, when there is no one key to pin."""
    if key is not None:
        norm = keygate.normalize(key)
        if norm is None:
            _fail(ValueError(f"--key {key!r} is not a key fingerprint (40 or 64 hex digits); "
                             f"nothing was written"), 1)
        return norm, None
    click.echo(f"account: {dc_env.accounts_dir()}")
    try:
        d = account.find_key(addr, pin_how=pin_how)
    except account.AccountBusy as exc:
        _fail(Exception(f"{exc}; nothing was written"), 1)
    if d.kind != "pinned":
        _fail(Exception(f"{d.line}; nothing was written"), 1)
    return d.key, d.line


@callers.command("add", context_settings=CTX)
@click.argument("addr")
@click.option("--note", default="", help="who this is, kept as a comment")
@click.option("--key", default=None, metavar="FPR",
              help="the key fingerprint the caller's contact holds (their phone shows it under "
                   "the contact's profile, Encryption); without it the account is opened to find "
                   "the one key contact with ADDR, which fails while the bot holds the account")
def callers_add(addr: str, note: str, key: str | None) -> None:
    """Approve ADDR, bound to its key, to ring this agent."""
    try:
        f = _callers_file()
        addr = addr.strip().lower()
        if "@" not in addr:
            raise ValueError(f"not an address: {addr!r}")
        have = next((e.key for e in account.read_caller_entries(f) if e.addr == addr and e.key), None)
        if have and key is None:        # nothing to find: it is pinned already
            click.echo(f"already approved: {addr}  key:{have} in {f}")
            return
        key, said = _key_for(addr, key)
        added = account.add_caller(addr, note, path=f, key=key)
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    except ValueError as exc:
        _fail(exc, 1)
        return
    if said:
        click.echo(said)
    click.echo(f"{'approved' if added else 'already approved'}: {addr}  key:{key} in {f}")
    # Callers and channels are two lists. A caller whose chat is
    # not listed hears the call but gets no in-call chat text.
    click.echo(f"hint: to let the bot write to them too (in-call chat text, replies), "
               f"list the chat: `sessionphone channels add NAME {addr}`")


@callers.command("pin", context_settings=CTX)
@click.argument("addr")
@click.option("--key", default=None, metavar="FPR",
              help="the key fingerprint to pin (the phone shows it under the contact's profile, "
                   "Encryption); without it the account is opened to find the one key contact "
                   "with ADDR, which fails while the bot holds the account")
def callers_pin(addr: str, key: str | None) -> None:
    """Pin a listed ADDR to a key, in place; a key it had is replaced."""
    try:
        f = _callers_file()
        addr = addr.strip().lower()
        if addr not in account.read_callers(f):
            raise ValueError(f"{addr} is not listed in {f}; "
                             f"`sessionphone callers add {addr} --key FPR` lists it")
        key, said = _key_for(addr, key)
        before = keygate.pin_entry(f, addr, key)
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    except ValueError as exc:
        _fail(exc, 1)
        return
    if said:
        click.echo(said)
    was = f" (was {before})" if before and before != key else ""
    click.echo(f"pinned: {addr}  key:{key}{was} in {f}")


# ---- avatar: the bot's picture ---------------------------------------------


@main.command("avatar", context_settings=CTX)
@click.argument("path", required=False, type=click.Path(readable=False, path_type=pathlib.Path))
@click.option("--clear", is_flag=True, help="remove the picture")
@click.option("--profile", default=None, metavar="NAME",
              help="not supported yet: only the default profile's picture is set")
def avatar_(path: pathlib.Path | None, clear: bool, profile: str | None) -> None:
    """Show, set or clear the bot's Delta Chat picture.

    With no PATH, print the picture's blob path, or "none". PATH must be a
    PNG, JPEG, GIF or BMP (judged by its first bytes), checked before the
    account is touched. While the resident bot holds the account the change
    is handed to it as a job, and this waits for its answer. It sends
    nothing: the picture shows after this account's next message.
    --clear also records that you chose no picture, so the bot does not
    set the default one at its start; setting a PATH removes that record.
    Exit 0 done or shown; 1 not configured; 2 refused (the file, Delta
    Chat's own refusal, or --profile); 3 the bot did not take it (older
    than this, it refused, or `link` holds the account); 4 NOT CONFIRMED
    (the bot took it and wrote no ack).
    """
    # (the function is avatar_ so that it does not shadow the avatar module)
    if profile is not None:
        _fail(Exception("the public profile is not supported yet; nothing was changed"), 2)
        return
    if clear and path is not None:
        _fail(Exception("give PATH or --clear, not both; nothing was changed"), 2)
        return
    try:
        if path is None and not clear:
            blob = avatar.show()
            click.echo(blob or "none")
            return
        blob, how = avatar.apply(None if clear else path)
    except avatar.Failed as exc:
        _fail(exc, exc.code)
        return
    except Exception as exc:  # noqa: BLE001 - same reason as setup
        _fail(exc, 1)
        return
    via = " (by the running bot)" if how == "bot" else ""
    click.echo(f"avatar: {'cleared' if clear else blob or 'set'}{via}")
    try:        # the user's choice wins: the bot never sets the default over it
        avatar.record_choice(clear)
    except OSError as exc:
        click.echo(f"warning: could not record that the picture was {'cleared' if clear else 'set'} "
                   f"({avatar.marker()}: {exc.strerror or exc}); the bot may set the default "
                   "picture at its next start", err=True)
    click.echo(f"note:   {avatar.SHOWS}")


# ---- channels: where the bot may send --------------------------------------


def _channels_file() -> pathlib.Path:
    """The list the bot reads, from this shell's environment. Under the root,
    the root must be private, as for `callers`; elsewhere its directory must exist."""
    f = dc_env.channels_file()
    if f.parent == paths.root():
        paths.ensure_private()
    elif not f.parent.is_dir():
        raise paths.NotConfigured(f"no directory {f.parent} for {f}")
    return f


def _tightened(path: pathlib.Path, mode: int) -> None:
    """Say it out loud when a loose list was made 0600: the bot refused it, and
    the owner should see what it holds before the bot trusts it again."""
    click.echo(f"sessionphone: tightened {path} from {mode:04o} to 0600; check its entries "
               f"(`sessionphone channels`)", err=True)


def _loose(path: pathlib.Path, mode: int) -> None:
    """Say it out loud when the list is left loose: a list writable by group or
    world refuses EVERY send."""
    refused = mode & 0o022
    click.echo(f"sessionphone: WARNING: {path} is mode {mode:04o}, not 0600"
               + ("; the bot REFUSES EVERY SEND until you check its entries and "
                  f"`chmod 600 {path}`" if refused else f"; `chmod 600 {path}`"), err=True)


@main.group("channels", context_settings=CTX, invoke_without_command=True)
@click.pass_context
def channels_(ctx: click.Context) -> None:
    """Where the bot may send: one `NAME TARGET  # note` per line.

    TARGET is an address (that contact's 1:1 chat, never a group they are in),
    bound to a key fingerprint (`key:FPR`): it passes only while the chat's
    contact holds that key; an address line with no key is refused until one
    is pinned (`sessionphone channels pin NAME`). Or chat:N (one chat by id,
    the only way to list a group). Every dc-* tool
    takes a NAME wherever it takes a chat id. A send to a chat not listed here
    is refused. A missing, unreadable or loose (group/world-writable) list
    refuses every send: it fails closed. Exit 2 when the list is refused.
    """
    # (the function is channels_ so that it does not shadow the channels module)
    if ctx.invoked_subcommand is None:
        _list_channels()


@channels_.command("list", context_settings=CTX)
def channels_list() -> None:
    """Print the list, as bare `sessionphone channels` does. Exit 2 when it is refused."""
    _list_channels()


def _list_channels() -> None:
    try:
        f = _channels_file()
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    ignored: list[str] = []
    listing = channels.load(f, log=ignored.append)
    for line in ignored:
        click.echo(f"sessionphone: {line}", err=True)
    if listing.refusal and not f.exists():
        click.echo(f"no channels yet ({f}): every send is refused. `sessionphone link` lists the "
                   "owner, or `sessionphone channels add owner ADDR`")
        return
    if listing.refusal:
        _fail(Exception(f"{listing.refusal}: every send is refused"), 2)
        return
    if not listing.entries:
        click.echo(f"no channels in {f}: every send is refused. `sessionphone channels add NAME TARGET`")
        return
    width = max(len(n) for n in listing.entries)
    twidth = max(len(e.target) for e in listing.entries.values())
    for e in listing.entries.values():
        line = f"{e.name:<{width}}  {e.target:<{twidth}}"
        if e.key:
            line += f"  key:{e.key}"
        elif e.address is not None:
            line += f"  UNPINNED: refused until pinned (`sessionphone channels pin {e.name}`)"
        line += f"  # {e.note}" if e.note else ""
        click.echo(line.rstrip())


_CHANNEL_KEY_HELP = ("the key fingerprint the contact holds (their phone shows it under the "
                     "contact's profile, Encryption); without it the account is opened to find the "
                     "one key contact with the address, which fails while the bot holds the account")


@channels_.command("add", context_settings=CTX)
@click.argument("name")
@click.argument("target")
@click.option("--note", default="", help="who or what this is, kept as a comment")
@click.option("--key", default=None, metavar="FPR", help=_CHANNEL_KEY_HELP + "; an address TARGET only")
def channels_add(name: str, target: str, note: str, key: str | None) -> None:
    """List NAME -> TARGET (an address bound to its key, or chat:N). Refuses a NAME already listed."""
    try:
        f = _channels_file()
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    said = None
    try:
        bad = channels.name_problem(name)
        if bad:
            raise ValueError(bad)
        norm = channels.parse_target(target or "")
        if norm is None:
            raise ValueError(f"TARGET {target!r} must be an address or chat:N")
        if not norm.startswith("chat:"):    # an address is bound to a key before it is written
            text = f.read_text(encoding="utf-8") if f.exists() else ""   # unreadable: exit 2 first
            if name in channels.parse(text, f, lambda _m: None):
                raise ValueError(f"NAME {name!r} is already listed in {f}")
            key, said = _key_for(norm, key, pin_how=channels.PIN_HOW.format(name=name))
        before = channels.loose_mode(f)
        entry = channels.add(name, target, note, f, key=key)
    except ValueError as exc:
        _fail(exc, 1)
        return
    except OSError as exc:      # e.g. a 0000 list: say which file, never a bare traceback
        _fail(Exception(f"cannot add to {f}: {exc.strerror or exc}; nothing was listed"), 2)
        return
    if before is not None:
        _tightened(f, before)
    if said:
        click.echo(said)
    click.echo(f"listed: {entry.name}  {entry.target}" + (f"  key:{entry.key}" if entry.key else "") + f" ({f})")


@channels_.command("pin", context_settings=CTX)
@click.argument("name")
@click.option("--key", default=None, metavar="FPR", help=_CHANNEL_KEY_HELP)
def channels_pin(name: str, key: str | None) -> None:
    """Pin the listed address channel NAME to a key, in place; a key it had is replaced."""
    try:
        f = _channels_file()
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    try:
        listing = channels.load(f, log=lambda _m: None)
        if listing.refusal:
            raise OSError(listing.refusal)
        entry = listing.entries.get(name)
        if entry is None:
            raise ValueError(f"no channel {name!r} in {f}; `sessionphone channels add {name} ADDR "
                             "--key FPR` lists it")
        if entry.address is None:
            raise ValueError(f"channel {name!r} is {entry.target}, a chat id, and takes no key")
        key, said = _key_for(entry.address, key, pin_how=channels.PIN_HOW.format(name=name))
        before = channels.pin(name, key, f)
    except ValueError as exc:
        _fail(exc, 1)
        return
    except OSError as exc:
        _fail(Exception(f"cannot pin in {f}: {getattr(exc, 'strerror', None) or exc}; nothing was written"), 2)
        return
    if said:
        click.echo(said)
    was = f" (was {before})" if before and before != key else ""
    click.echo(f"pinned: {name}  {entry.address}  key:{key}{was}")


@channels_.command("seed", context_settings=CTX)
def channels_seed() -> None:
    """List the owner as `owner`, as `bot install` does, for a bot already running.

    The upgrade step for a bot root that has no channels.txt yet.
    It runs install's own seed (the callers file's owner line, written into a
    missing or empty list) and nothing else: no launchd job is written or
    touched, so it is safe for a bot `bot install` did not make. DC_CHANNELS_FILE,
    DC_CALLERS_FILE and DC_ROOT in this shell name the bot's files. A list that
    already has a channel is left alone (exit 0). Exit 2, writing nothing, when
    there is no owner line to seed from, or the list is loose or unreadable: it
    is never tightened quietly. The bot itself never seeds.
    """
    retry = "sessionphone channels seed"
    try:
        f = _channels_file()
        entry = service.write_channels(service.plan_channels(dict(os.environ), retry=retry))
    except (paths.NotConfigured, paths.Unsafe, service.NoChannels) as exc:
        _fail(exc, 2)
        return
    except OSError as exc:      # e.g. a directory that cannot be made: say which file
        _fail(Exception(f"cannot seed {dc_env.channels_file()}: {exc.strerror or exc}; "
                        "nothing was listed"), 2)
        return
    if entry is None:
        click.echo(f"channels:  {f} already lists a channel; left alone")
        return
    click.echo(f"channels:  {entry.name} {entry.target}, listed in {f}")


def _profile_channels_file(profile: str) -> pathlib.Path:
    """The list a PROFILE's bot reads, as bin/dc-env's dc_channels finds it: the
    default profile's is `_channels_file()`; a named one's sits beside its outbox
    (DC_OUTBOX_PUBLIC, else <root>/public/outbox)."""
    if not profile:
        return _channels_file()
    out = os.environ.get("DC_OUTBOX_PUBLIC")
    if out:
        return pathlib.Path(out).expanduser().parent / "channels.txt"
    r = dc_env.env("ROOT")
    return (pathlib.Path(r).expanduser() if r else paths.root()) / "public" / "channels.txt"


@channels_.command("rejected", context_settings=CTX)
@click.option("--profile", type=click.Choice(["public"]), default=None,
              help="the public profile's bot (the default profile is never gated inbound)")
def channels_rejected(profile: str | None) -> None:
    """Who the PUBLIC profile's bot turned away, newest first, one line per chat.

    On the public profile an inbound message, contact request, reaction or
    webxdc tap from a chat not on its channels.txt is never passed to the
    agent; the bot records who and why in rejected.jsonl beside that list
    (never what they said). This lists them, deduped by chat with a count, so
    you can let one in with `sessionphone channels add`. Exit 2 when the
    record cannot be read.
    """
    try:
        f = _profile_channels_file(profile or "")
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    where = channels.rejected_path(f)
    try:
        recs, bad = channels.read_rejected(where)
    except OSError as exc:
        _fail(Exception(f"cannot read {where}: {exc.strerror or exc}"), 2)
        return
    if bad:
        click.echo(f"sessionphone: {bad} line(s) in {where} did not parse; skipped", err=True)
    if not recs:
        click.echo(f"no rejections recorded ({where})")
        return
    for r in channels.fold_rejected(recs):
        last = r.last
        who = channels.clean(last.get("name")) or "?"
        addr = channels.clean(last.get("address"))
        sender = f"{who} <{addr}>" if addr else who
        times = f"x{r.count}"
        click.echo(f"{channels.clean(last.get('ts'), 40) or '?'}  chat {channels.clean(last.get('chat_id'), 20)}"
                   f"  {times}  {','.join(channels.clean(k, 20) or '?' for k in r.kinds)}  {sender}"
                   f"  - {channels.clean(last.get('reason')) or '?'}")
    click.echo(f"to let one in: DC_CHANNELS_FILE={f} sessionphone channels add NAME ADDRESS|chat:N")


@main.group(context_settings=CTX, invoke_without_command=True)
@click.pass_context
def bot(ctx: click.Context) -> None:
    """The resident bot: it holds the account, answers calls, bridges chat.

    With no verb, the same as `bot status`. Calls need it running: the
    phone rings for about two minutes, and nothing else answers.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(bot_status)


@bot.command("install", context_settings=CTX)
@click.option("--owner", default=None, metavar="ADDR",
              help="approve ADDR as the owner, the first who may ring (written into the callers file), "
                   "and list them as `owner` in channels.txt if it has no channel yet. ADDR is the "
                   "owner's Delta Chat address (the one their app shows), not a mail address")
@click.option("--owner-key", "owner_key", default=None, metavar="FPR",
              help="bind --owner's line to this key fingerprint: the owner's own, as Delta Chat on "
                   "the owner's phone lists it (this agent's contact profile, Encryption). Without it, "
                   "on a callers file that keeps keys, --owner is refused")
@click.option("--forget", "forget", multiple=True, metavar="KEY",
              help="drop KEY (a DC_* name, or SESSIONPHONE_ROOT) from the settings this bot "
                   "keeps, for this run and every later one. Repeatable")
@click.option("--registry", default=None, metavar="DIR",
              type=click.Path(file_okay=False, dir_okay=True),
              help="give the bot a key, on any mail carrier: mint its seat into the seat "
                   "registry DIR if it has no key yet (`message seat init --registry DIR`), run "
                   "the seat's key agent, and save the signer, so later installs and starts sign. "
                   "A key already there is never minted again")
@click.option("--hear-here", "hear_here", is_flag=True,
              help="also let THIS Claude session hear the bot's box, by a session subscription "
                   "(`message subscription add BOX`). It ends with this session: a later session "
                   "hears the box only when started as the agent (`session start AGENT`), or "
                   "after --hear-here again. Never done unless asked")
@click.option("--replace", "replace", multiple=True, metavar="NAME",
              help="retire bot NAME, which phone.json records on this bot's root (one Delta "
                   "Chat account): boot out its launchd job, remove its plist, and forget it in "
                   "phone.json. Without it, install refuses while another bot is recorded on "
                   "the same root. Repeatable")
@click.option("--force", is_flag=True,
              help="restart the bot even while a call is live: the call drops. Without it, "
                   "install refuses while one is up")
def bot_install(owner: str | None, forget: tuple[str, ...], registry: str | None,
                hear_here: bool = False, replace: tuple[str, ...] = (),
                owner_key: str | None = None, force: bool = False) -> None:
    """Run the bot as this user's launchd job, now and at every login.

    DC_* variables set in this shell are carried into the job, and so are
    sessionmail's mail-route settings (SESSIONMAIL_SENDMAIL, _RELAY_DOMAIN,
    _HOME, _ROOT and _HOST), never any other SESSIONMAIL_*. SESSIONMAIL_ORIGIN
    is not carried: the reader verifies with its own, so it belongs in
    sessionmail's settings key postfix.origin.
    Install remembers them: each DC_* and SESSIONPHONE_ROOT it was given is
    saved, once the job is loaded, in ~/.sessionkit/phone/settings/phone.json,
    one map per bot, and a later install with an empty shell uses them. A value
    set in this shell wins over the file; --forget KEY removes one. The bot is
    DC_AGENT, else this session's seat as sessionmail names it (`message
    subscription list`) when a later session can be started as it, never a
    session id or a plus address, else the one bot the file records, else
    the harness vendor's word (`session agent default`), else claude; with
    two or more recorded and no name, install refuses. Only
    when sessionmail gives no answer is CLAUDE_CODE_AGENT taken instead of
    the seat. The
    mail-route settings are not saved there: sessionmail keeps them in its own
    mail.json, and the `settings:` block says, for each one this shell set,
    whether it is kept and how. A saved DC_SIGNER_* path that has gone is
    refused (exit 2, writing nothing). The `settings:` block names each value
    the job carries and where it came from.
    Whether the bot signs follows the carrier its own `message send` will
    use, as sessionmail resolves it; the `seat:` line names the mode and
    why. On the mail drop with no signer set or saved, it does not sign: the
    bot writes into the box itself, so no key or seat step is needed. On an
    MTA, or with a signer set or saved, its seat, <agent>-chat, signs every
    mail it sends its agent. A carrier sessionmail refuses, or cannot name,
    is refused (exit 2, writing nothing). --registry DIR gives the bot a
    key on any carrier: with no signer set or saved, install mints the seat
    into the seat registry DIR if it has no key yet (`message seat init
    --registry DIR <agent>-chat`), runs its key agent, and saves the signer,
    so later installs and starts sign; a key already there is never minted
    again. With no DC_SIGNER_SOCK and DC_SIGNER_PUB set or saved, install
    then runs the seat's key agent under launchd itself (`message
    seat agent --install <agent>-chat`) and the job carries the socket and
    .pub it reports. A seat not minted yet, or a refusal, is exit 2 and
    writes nothing; a failure is exit 1 and names the recovery (--uninstall,
    then --install). A sessionmail whose `message seat agent --help` lacks
    `Capability: install` cannot do that: set both DC_SIGNER_* by hand (the
    README's "The bot's seat"); without both, install refuses (exit 2,
    writing nothing) and names the steps. DC_MESSAGE is pinned to this shell's
    `message`. The bot's own start check then runs on the job's environment,
    and the box Delta text goes to is made (`message setup --box <agent>`):
    a job that would refuse to start is never loaded.
    Run it again after a plugin update, or to change them. `link` must not be
    running. The carrier is sessionmail's: SESSIONMAIL_SENDMAIL, else the
    `sendmail` key in its mail.json, else its default, the mail drop; the
    `mail:` line names it.
    Refused (exit 2, before the old job is touched) when nobody may ring the
    bot: no --owner and no address in the callers file (link the phone, or
    give --owner the owner's Delta Chat address); while DC_CALLERS is set,
    which grants nothing; or when it could send nowhere: channels.txt is
    missing or empty and there is no owner to list in it (--owner, else the
    callers file's owner line).
    Refused (exit 2, writing nothing, booting nothing out) while a call is
    live on the bot it would restart: `a call is live; hang up or pass
    --force`, then the call. A call is live when the bot writes its
    call-live.json and that bot is running (its bot.pid, as dc-live reads it)
    and started before the call. A file left by a bot that has gone, or by an
    earlier bot, is no call. The files are the ones the installed job's plist
    names. --force restarts it anyway, and the call drops.
    Refused (exit 2, writing nothing) when DC_STT_URL names no model: its
    kind is unknown and DC_STT_MODEL is unset (tag the URL or set the model).
    Refused too (exit 2, writing nothing) when this environment lacks the
    media pack the job runs on; the message says how to fetch it.
    One root is one Delta Chat account: while phone.json records another
    bot on this bot's root (its SESSIONPHONE_ROOT, DC_ROOT or DC_ACCOUNTS),
    install refuses (exit 2, writing nothing) and names it, with the ways
    past it. --replace NAME retires that bot: its launchd job is booted out,
    its plist removed, and phone.json forgets it once the new job is loaded,
    one `replaced bot` line each. Exit 1, the new job not started, when
    launchd still lists it after its bootout: run install again.
    A job left by sessionphone 0.1.0 for the same agent (its old label) is
    retired first: booted out, its plist removed, one line each. Refused
    (exit 2, writing nothing, booting nothing out) when that old plist
    carries a setting the new job would drop or change (the message names
    each one, old -> new, and the fix), or when the old job is loaded with
    its plist gone. Exit 1, the new job not started, when launchd still lists
    the old job after its bootout: run install again.
    The `hear:` line says whether THIS session will hear Delta messages and
    calls, as sessionmail measures it, never guessed. When it will not, it
    says so first, then the ways out: `session start <agent>`, DC_AGENT or
    DC_MAILTO, or --hear-here; and that warning is install's FIRST line,
    ahead of `installed:` (a line that confirms it hears, or a Terminal's
    advice, stays under `box:`). A warning only: the job is installed either
    way, and the exit code does not change. From a shell that is no Claude
    session it gives the advice, and how to send Delta text to another box.
    A DC_MAILTO naming this session's own box (its session id) is heard, as
    "its own session box". When the bot's name came from the default and
    sessionmail says nobody hears its box (`message subscription list
    <agent>`: no such box, or 0 hear it and no live listener), a `default:`
    line follows `hear:`, naming the fix: start a bare `session start` first.
    When a default picture ships, the `picture:` line says the bot sets it
    at its start if the account has none and it was not cleared
    (`sessionphone avatar --clear`); nothing ships yet, so no line.
    The `name:` line says the bot's Delta Chat name: DC_NAME, else the
    agent's name, title-cased, which the bot sets at its start over an empty
    name or the name of the agent it is or replaced (--replace). Any other
    name was set by hand and is kept; the line says so, with the DC_NAME line
    that changes it.
    Its `hearing:` line says whether the bot will hear the caller: it
    sends a half-second test clip to the STT at DC_STT_URL, saying so first.
    Its last line, `speaking:`, says whether the bot can speak its replies:
    on the speech-server route it needs ffmpeg, looked for where the bot
    looks (DC_FFMPEG, the job's PATH, the Homebrew paths). With none, it says
    NO: every spoken reply would go to the caller as chat text, and it shows
    the fix.
    """
    try:
        media.require("bot install")    # FIRST: a refusal writes no plist, no channels seed
    except media.Missing as exc:
        _fail(exc, 2)
        return
    seeded: list[channels.Entry] = []
    seated: list[tuple[seat.Seat, seat.Box]] = []
    settled: list[settings.Resolved] = []
    unsaved: settings.NotSaved | None = None
    try:
        p = service.install(owner=owner, owner_key=owner_key, seeded=seeded.append, say=click.echo,
                            seated=lambda made, box: seated.append((made, box)),
                            forget=forget, settled=settled.append, registry=registry,
                            replace=replace, force=force)
    except settings.NotSaved as exc:   # the job is loaded: say all of it, then why, exit 1
        p, unsaved = exc.plist, exc
    except (paths.NotConfigured, paths.Unsafe, service.NoMailHost, service.NoSTTModel,
            service.ProfileFiles, service.NoCallers, service.NoChannels, service.LegacyCarryOver,
            service.NoSigner, service.NoBox, service.CallLive, settings.Bad) as exc:
        _fail(exc, 2)
        return
    except Exception as exc:  # noqa: BLE001 - the operator gets launchctl's (or sessionmail's) reason
        _fail(exc, 1)
        return
    res = settled[0] if settled else None
    name, env = (res.name, res.env) if res else (None, None)
    st = service.status(name)
    first, later = _hear_lines(seated, hear_here, res.name if res else "", res.name_from if res else "")
    for line in first:              # this session will NOT hear: the warning leads
        click.echo(line)
    click.echo(f"installed: {p}")
    for made, _box in seated:
        click.echo(f"seat:      {made.line()}")
    if owner:
        bound = f", bound to key {keygate.normalize(owner_key)}" if owner_key else ""
        click.echo(f"owner:     {owner.strip().lower()}, approved in {dc_env.callers_file(env)}{bound}")
    for e in seeded:
        click.echo(f"channels:  {e.name} {e.target}, listed in {dc_env.channels_file(env)}")
    click.echo(f"running:   {'pid ' + str(st['pid']) if st['pid'] else 'not yet - see `sessionphone bot status`'}")
    click.echo(f"log:       {service.log_paths(name)[0]}")
    said = avatar.install_line()
    if said:
        click.echo(said)
    if res:
        click.echo(displayname.install_line(dc_env.home(res.env, res.root), res.name, res.env.get("DC_NAME", ""),
                                            tuple(res.replace)))
    probe = settings.mail_file(env.get("DC_MESSAGE")) if res else None
    if res:
        for line in settings.block(res, probe):
            click.echo(line)
    _mail(p, probe)
    for _made, box in seated:
        click.echo(f"box:       {box.line()}")
        for line in later.get(box.name, ()):
            click.echo(line)
    _hearing(p)
    _speaking(p)
    if unsaved is not None:
        _fail(unsaved, 1)


def _hear_lines(seated, hear_here: bool, name: str = "", came: str = ""
                ) -> tuple[list[str], dict[str, list[str]]]:
    """`bot install`'s hear-here: and hear: lines for each box made, measured
    once, --hear-here first (it subscribes, then the hear line measures again).
    -> (first, later): when sessionmail says THIS session will NOT hear a box
    (seat.deaf), its hear: warning and ways out, then its hear-here: line, go
    in FIRST, ahead of `installed:`; else they stay in LATER, under the box's
    own `box:` line, as before. After them, for the bot's own box (NAME) when
    its name CAME from the default, the `default:` line (seat.fallthrough)
    when sessionmail says nobody hears it."""
    first: list[str] = []
    later: dict[str, list[str]] = {}
    for _made, box in seated:
        lines = later.setdefault(box.name, [])
        if box.path is None:
            if hear_here:
                lines.append(f"hear-here: not done: box {box.name} is not on this host")
            continue
        here = f"hear-here: {seat.hear_here(box.name, os.environ)}" if hear_here else None
        r = seat.reach(os.environ)
        hear = f"hear:      {seat.hear_line(box.name, os.environ, r)}"
        said = seat.fallthrough(box.name, came, os.environ, r) if name and box.name == name else None
        tail = [f"default:   {said}"] if said else []
        if seat.deaf(box.name, r):
            first += [hear, *([here] if here else []), *tail]
        else:
            lines += [*([here] if here else []), hear, *tail]
    return first, later


def _job_env(plist: pathlib.Path) -> tuple[dict | None, Exception | None]:
    """The environment launchd runs the bot with, read back from the plist just
    installed: -> (env, None), or (None, why it could not be read)."""
    try:
        with open(plist, "rb") as fh:
            return plistlib.load(fh).get("EnvironmentVariables") or {}, None
    except (OSError, plistlib.InvalidFileException, AttributeError) as exc:
        return None, exc


def _mail(plist: pathlib.Path, probe=None) -> None:
    """`bot install`'s `mail:` line: the SESSIONMAIL_* the job carried and the
    carrier the bot will send by (service.mail_line), judged on the plist as
    written, not on this shell. Said, never a refusal."""
    env, exc = _job_env(plist)
    if env is None:
        click.echo(f"mail:      not confirmed. The job's environment could not be read back from "
                   f"{plist} ({exc}), so its mail carrier is not known.")
        return
    click.echo(f"mail:      {service.mail_line(env, shell=os.environ, probe=probe)}")


def _hearing(plist: pathlib.Path) -> None:
    """The last lines of `bot install`: will the bot hear the caller?

    Judged on the environment launchd runs the bot with, read back from the
    plist just installed (`job()` carries DC_* and service.MAIL_KEYS only), by
    asking that STT to transcribe a test clip (hearing.py). When the offered
    route gives no answer, the speech weights' reader says whether its models
    are downloading or not there yet (hearing.weights_for). Said, never a
    refusal: the exit code stays 0, and the job is already installed and loaded."""
    env, exc = _job_env(plist)
    if env is None:
        click.echo(f"hearing:   not confirmed. The job's environment could not be read back from "
                   f"{plist} ({exc}), so DC_STT_URL was not checked.")
        return
    v = hearing.probe(env, say=click.echo)
    w = hearing.weights_for(v, shell=os.environ)
    for line in hearing.lines(v, env, shell=os.environ, weights=w):
        click.echo(line)


def _speaking(plist: pathlib.Path, head: str = hearing.SPEAK_HEAD) -> None:
    """`bot install`'s (and `bot status`'s) `speaking:` lines: can the bot speak
    its replies? Judged on the job's environment read back from the plist, as
    hearing is (hearing.ffmpeg_for: DC_FFMPEG, the job's PATH, the Homebrew
    paths), never this shell's PATH. Said, never a refusal. A plist that
    cannot be read back says nothing here: `hearing:` has said so already."""
    env, _exc = _job_env(plist)
    if env is None:
        return
    for line in hearing.speaking_lines(env, head=head):
        click.echo(line)


def _status_hearing(plist: pathlib.Path) -> None:
    """`bot status`'s ONE `hearing:` line: does the STT the job
    carries answer? A bot whose STT died after install is otherwise never
    reported. The probe is capped (hearing.STATUS_MAX_TIME_S), and the line
    itself names what was asked, so nothing is said before it. Said, never a
    refusal: the exit code is the job's alone."""
    env, exc = _job_env(plist)
    if env is None:
        click.echo(f"{hearing.STATUS_HEAD}not confirmed. The job's environment could not be read "
                   f"back from {plist} ({exc}), so DC_STT_URL was not checked.")
        return
    v = hearing.probe(env, say=lambda _line: None, max_time=hearing.STATUS_MAX_TIME_S,
                      run_timeout=hearing.STATUS_RUN_TIMEOUT_S)
    click.echo(hearing.status_line(v, env))


def _which_bot(verb: str) -> tuple[str, str | None]:
    """The bot `sessionphone VERB` acts on, as install picks it
    (settings.bot_name): DC_AGENT, else this session's durable seat, else
    the one bot phone.json records, else the harness vendor's word
    (settings.default_agent, else claude). -> (name, its `bot:` line), the line
    None when this shell's own variables named it (settings.named_by_shell:
    it is what was asked for). A settings file that cannot be used, or two
    bots and no name, refuses (exit 2), naming the way past it."""
    try:
        name, came = settings.bot_name(os.environ, verb, ask=True)
    except settings.Bad as exc:
        _fail(Exception(f"{exc} Or name the bot in this shell, which reads no file: "
                        f"`DC_AGENT=NAME sessionphone {verb}`."), 2)
    return name, None if settings.named_by_shell(came) else f"bot:     {name} ({came})"


@bot.command("uninstall", context_settings=CTX)
@click.option("--force", is_flag=True,
              help="stop the bot even while a call is live: the call drops. Without it, "
                   "uninstall refuses while one is up")
def bot_uninstall(force: bool = False) -> None:
    """Stop the bot and remove its launchd job, and any job sessionphone 0.1.0
    left for it under the old label. The account stays.

    Refused (exit 2, stopping and removing nothing) while a call is live on
    the bot, as `bot install` judges it: `a call is live; hang up or pass
    --force`, then the call. --force stops it anyway, and the call drops.

    The bot is the one install would pick: DC_AGENT, else this session's
    durable seat, else the one bot ~/.sessionkit/phone/settings/phone.json
    records, else the harness vendor's word (`session agent default`), else
    claude; with two or more recorded and no name, it refuses (exit 2). With
    no name in this shell, its first line, `bot:`, says which it picked."""
    name, picked = _which_bot("bot uninstall")
    if picked:
        click.echo(picked)
    try:
        said = service.uninstall(name, force=force)
    except service.CallLive as exc:
        _fail(exc, 2)
        return
    for line in said or ["no launchd job was installed"]:
        click.echo(line)


@bot.command("migrate", context_settings=CTX)
@click.option("--dry-run", "dry_run", is_flag=True,
              help="check, and say each step and where the account will open, changing nothing")
def bot_migrate(dry_run: bool = False) -> None:
    """Move the bot's old default root, ~/.sessionphone, to the new one,
    ~/.sessionkit/phone/state, once on this Mac, keeping its Delta Chat keys.

    Refused (exit 2, nothing moved) while a bot runs on the old root (it
    holds the root's .bot.lock, or its bot.pid names a running bot) or a
    call is live on it: stop the bot first (`sessionphone bot uninstall`).
    Refused too when the new root is already there, when the old path is a
    link somewhere else, or when the two are on different filesystems.

    The root is moved by rename, never copied: the account and its keys
    keep their files and their 0700. The old path is left as a link to the
    new root, so anything still holding the old name keeps working; it is
    never deleted. The launchd plists whose root is the old one (their
    DC_ROOT, else SESSIONPHONE_ROOT) are re-rendered to name the new one,
    and a plist with any other root is left as it is. A loaded job reads
    its plist at its next load (`sessionphone bot install`, or the next
    login); until then it runs on the old path, the link.

    Only the old DEFAULT root moves: a root set by SESSIONPHONE_ROOT, DC_ROOT
    or phone.json is never touched. Each line names a step: old, new, where
    the account opens (account:), the move, the link, each plist. With
    --dry-run they say what would be done, and nothing is changed. Exit 1
    when a step after the checks fails; the message says what was done."""
    from sessionphone import migrate
    try:
        migrate.migrate(dry_run=dry_run, say=click.echo)
    except migrate.Refused as exc:
        _fail(exc, 2)
    except migrate.Failed as exc:
        _fail(exc, 1)


def _undelivered(name: str | None) -> None:
    """`undelivered: N waiting, M dead (<dir>)`: the bot's spool of agent mail
    that failed, call turns included, as dc-live counts it. Said on
    every status, 0 too; the spool is the one the installed job names."""
    from sessionphone.bridge import spool
    try:
        d = service.undelivered_dir(name)
    except settings.Bad as e:               # the root cannot be known: say so, never fail status
        click.echo(f"undelivered: unknown ({e})")
        return
    waiting, dead = spool.Spool(log=lambda _m: None, root=d).counts()
    click.echo(f"undelivered: {waiting} waiting, {dead} dead ({d})")


@bot.command("status", context_settings=CTX)
def bot_status() -> None:
    """Is the bot installed, loaded and running? Exit 0 running, 1 not.

    With the job installed, a `hearing:` line says whether the STT at the
    job's DC_STT_URL answers a half-second test clip (capped at a few
    seconds: a server still loading its model reads `not confirmed`). On NO
    it names the URL that did not answer and points at `bot install`, which
    says how to fix it. Then a `speaking:` line says whether the bot can speak
    its replies, judged on the plist's environment as `bot install` judges it:
    with no ffmpeg where the bot looks, it says NO, that spoken replies go to
    the caller as chat text, and the fix. Neither changes the exit code.

    An `undelivered:` line counts the agent mail waiting in the bot's spool
    and dead there (call turns included), from the spool its plist names.

    Its last line is the last line of the bot's log; before the bot has
    written one (a bot that refused to start), the last line of launchd's log
    for the job, where that refusal went.

    The bot is the one install would pick (as `bot uninstall` says; with no
    name in this shell, a `bot:` line says which), and its log is the one its
    plist names, else the one under the root: this shell's SESSIONPHONE_ROOT,
    else the one phone.json keeps, else the default (paths.root)."""
    name, picked = _which_bot("bot status")
    st = service.status(name)
    click.echo(f"job:     {st['label']} ({'installed' if st['installed'] else 'not installed'})")
    if picked:
        click.echo(picked)
    old = st["legacy"]
    if old:
        held = " and ".join(w for w, on in (("loaded", old["loaded"]), (f"plist {old['plist']}", old["installed"]))
                            if on)
        # loaded with its plist gone, install refuses (nothing to compare): uninstall retires it
        how = (f"`{service.INSTALL}` retires it" if old["installed"]
               else f"`sessionphone bot uninstall` retires it, then `{service.INSTALL}`")
        click.echo(f"legacy:  {old['label']} ({held}), the job sessionphone 0.1.0 installed: {how}")
    if st["pid"]:
        click.echo(f"running: pid {st['pid']}")
    else:
        click.echo(f"running: no{' (last exit ' + st['last_exit'] + ')' if st['last_exit'] else ''}")
    if st["installed"]:
        _status_hearing(pathlib.Path(st["plist"]))
        _speaking(pathlib.Path(st["plist"]), head="speaking: ")
    _undelivered(name)
    log, line, launchd = service.last_log(name)
    click.echo(f"log:     {log}")
    if line is None:
        click.echo("last:    no log yet")
    elif launchd:
        click.echo(f"last ({log.name}): {line}")
    else:
        click.echo(f"last:    {line}")
    sys.exit(0 if st["pid"] else 1)


@bot.command("run", context_settings=CTX)
def bot_run() -> None:
    """Run the bot in the foreground, for a test. Ctrl-C stops it.

    The bot is the one install would pick (as `bot uninstall` says):
    DC_AGENT, else this session's durable seat, else the one bot
    ~/.sessionkit/phone/settings/phone.json records, else the harness
    vendor's word (`session agent default`), else claude; with two or more
    recorded and no name, it refuses (exit 2). With no name in this
    shell, its first line, `bot:`, says which it picked."""
    try:
        media.require("bot run")
    except media.Missing as exc:
        _fail(exc, 2)
        return
    name, picked = _which_bot("bot run")
    if picked:
        click.echo(picked)
    # bot.py takes argv[1] as the agent when DC_AGENT is unset; here argv[1] is "bot"
    os.environ["DC_AGENT"] = name
    dc_env.bind_agent()     # AGENT, ADDR, MAILTO: this bot's, whatever read them first
    from sessionphone.bridge import bot as bridge_bot  # heavy: WebRTC, only when asked

    bridge_bot.main()


# ---- sessionphone web: a browser tab as a call device ----------------------

def _webdir(root: pathlib.Path | None) -> pathlib.Path:
    """`<root>/web/`: --root, else paths.root() (SESSIONPHONE_ROOT, else the
    root phone.json keeps, else the default)."""
    return (root.expanduser() if root else paths.root()) / "web"


_ROOT_HELP = (f"the sessionphone root (default: $SESSIONPHONE_ROOT, else the one phone.json keeps, "
              f"else {paths.DEFAULT_ROOT}); state goes in ROOT/web/")


@main.group("web", context_settings=CTX, invoke_without_command=True)
@click.option("--port", type=int, default=8775, show_default=True, help="the port to listen on")
@click.option("--root", type=click.Path(file_okay=False, path_type=pathlib.Path), default=None, help=_ROOT_HELP)
@click.option("--origin", "origins", multiple=True, metavar="URL",
              help="one more origin allowed to open the socket (repeatable), e.g. a scratch https://localhost:P")
@click.option("--tls", nargs=2, type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
              default=None, metavar="CERT KEY", help="terminate TLS here (else use `tailscale serve`)")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="the address to bind: 127.0.0.1, or this host's tailnet IP with --tls; nothing else")
@click.option("--legs", is_flag=True,
              help="serve call-ears.sock and call-mouth.sock under ROOT/web/ so the ears and the mouth "
                   "can open a call as call:N (default: echo, no sockets)")
@click.option("--demo", is_flag=True,
              help="allow --legs on a live root (the default one, or one a keeper scans): the DEMO, "
                   "a live seat on the line; it waits for the operator's GO")
@click.option("--mail-to", "mail_to", default=None, metavar="BOX",
              help="with --legs: the seat each finished turn of a web call is mailed to, a bare box on "
                   "this host or box@host (default: $DC_MAILTO, else the bare $DC_AGENT; with neither, "
                   "--legs is refused)")
@click.option("--heard-dir", "heard_dir", type=click.Path(file_okay=False, path_type=pathlib.Path), default=None,
              metavar="DIR", help="with --legs: the ears' heard log dir the turns are read from (default: "
                                  "$EARS_HEARD_DIR, else $VOICEMODE_BASE_DIR/logs/conversations, "
                                  "else ~/.voicemode/logs/conversations)")
@click.option("--caller-name", "caller_name", default=None, metavar="NAME",
              help="the caller's name: the sign-in's user, each turn mail's speaker and a new passkey's "
                   "label (default: $DC_CALLER_NAME, else 'owner'); a label only, it grants nothing")
@click.pass_context
def web(ctx: click.Context, port: int, root: pathlib.Path | None, origins: tuple[str, ...],
        tls: tuple[pathlib.Path, pathlib.Path] | None, host: str, legs: bool, demo: bool,
        mail_to: str | None, heard_dir: pathlib.Path | None, caller_name: str | None) -> None:
    """A browser tab into this agent, signed in with a passkey. Tailnet only.

    Runs in the foreground; Ctrl-C stops it. It binds 127.0.0.1 and the
    tailnet reaches it through a foreground `tailscale serve
    --https=PORT http://127.0.0.1:PORT` (never `funnel`). Every passkey is
    enrolled with a one-time code printed here, on this machine.

    With no --legs a call is a closed loop: you hear yourself about 1 s
    later. --legs lets this host's ears and mouth open it as call:N; on a
    live root that needs --demo, the DEMO, which waits for the operator's GO. With
    --legs each finished turn of the caller's is mailed to the seat
    (--mail-to), Reply-To the call's box, so a reply is spoken into the call.
    """
    if ctx.invoked_subcommand is not None:
        return      # `web enrol` guards itself: here, it would refuse `web enrol --help` too
    try:
        media.require("web")
    except media.Missing as exc:
        _fail(exc, 2)
        return
    from sessionphone.web import server  # websockets: only when asked

    sys.exit(server.run(_webdir(root), port=port, host=host, tls=tls, origins=origins, legs=legs,
                        demo=demo, explicit_root=root is not None, mail_to=mail_to, heard_dir=heard_dir,
                        caller_name=caller_name, echo=click.echo))


@web.command("enrol", context_settings=CTX)
@click.option("--root", type=click.Path(file_okay=False, path_type=pathlib.Path), default=None, help=_ROOT_HELP)
@click.pass_context
def web_enrol(ctx: click.Context, root: pathlib.Path | None) -> None:
    """Print a one-time code that enrols ONE passkey on the web page.

    Run it on this machine: being here is what the code proves. A running
    server reads it from disk, so it need not restart. It replaces any
    earlier code.

    Beside the code, a QR a phone's camera opens: the page at this host's
    tailnet address (with the port of a group --origin on that host, else
    --port), else the group's first --origin that is not localhost, with the
    code in the URL's #fragment, which no server or proxy sees. Give the group
    the server's --port and --origin when they are not the defaults.
    """
    try:
        media.require("web enrol")
    except media.Missing as exc:
        _fail(exc, 2)
        return
    import time

    from sessionphone.web import enrol_qr, passkeys
    from sessionphone.web import server as web_server

    params = ctx.parent.params if ctx.parent else {}
    root = root or params.get("root")
    ring = passkeys.Keyring(_webdir(root))
    code = ring.new_enrol_code()
    until = time.strftime("%H:%M:%S", time.localtime(time.time() + passkeys.ENROL_TTL))
    click.echo(f"enrol code:  {code}")
    click.echo(f"valid until {until} ({passkeys.ENROL_TTL // 60} minutes), for one passkey; "
               f"{passkeys.ENROL_TRIES} wrong tries burn it.")
    # the QR: echo only, never a log, and never the URL as text. tailnet_name is
    # read off the module when run, so a test stubs it
    origin, why = enrol_qr.phone_origin(web_server.tailnet_name(), params.get("port") or web_server.DEFAULT_PORT,
                                        tuple(params.get("origins") or ()))
    for line in enrol_qr.lines(code, origin, why):
        click.echo(line)


# ---- sessionphone app: the webxdc apps this package ships -------------------

def _dc_root() -> pathlib.Path:
    """The bot's root, read now: DC_ROOT, else paths.root() (SESSIONPHONE_ROOT,
    else the root phone.json keeps, else the default)."""
    root = dc_env.env("ROOT")
    return pathlib.Path(root).expanduser() if root else paths.root()


@main.group("app", context_settings=CTX, invoke_without_command=True)
@click.pass_context
def app_(ctx: click.Context) -> None:
    """The webxdc apps sessionphone ships: list them, or build one into a .xdc.

    Send a built app with `dc-send-file CHAT_ID PATH`; the ack names its msg
    id, which `dc-xdc-realtime` takes.
    """
    if ctx.invoked_subcommand is None:
        _app_list()


def _app_list() -> None:
    from sessionphone import apps

    for name in apps.names():
        click.echo(name)


@app_.command("list", context_settings=CTX)
def app_list() -> None:
    """The apps that ship, one name per line."""
    _app_list()


@app_.command("build", context_settings=CTX)
@click.argument("name")
@click.option("-o", "--output", "out", type=click.Path(dir_okay=False, path_type=pathlib.Path), default=None,
              metavar="PATH", help="where to write the .xdc (default: $DC_ROOT/apps/NAME.xdc)")
def app_build(name: str, out: pathlib.Path | None) -> None:
    """Build the app NAME into a .xdc and print its path.

    The zip has index.html at its root and no webxdc.js (the messenger
    provides it). The same tree always gives the same bytes. Then:

        dc-send-file CHAT_ID "$(sessionphone app build guide-trail)"
    """
    from sessionphone import apps

    try:
        apps.app_dir(name)                     # an unknown name is refused before anything is made
        if out is None:
            root = _dc_root()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)   # never a wider root than setup makes
            out = apps.default_path(name, root)
        written = apps.build(name, out)
    except apps.UnknownApp as exc:
        _fail(exc, 2)
        return
    except OSError as exc:
        _fail(exc, 1)
        return
    click.echo(str(written))

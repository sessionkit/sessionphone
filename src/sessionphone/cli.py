"""sessionphone - ring your Claude Code session from your phone over Delta Chat.

    sessionphone setup --name NAME   make this agent's account, print the invite
    sessionphone qr                  the connect page (HTML + QR) on stdout
    sessionphone link                wait for the person to scan; they become the owner
    sessionphone invite [--qr [--invert]] [--png FILE]  print the saved invite link (or its QR)
    sessionphone callers [add ADDR]  who may ring this agent
    sessionphone avatar [PATH | --clear]  show, set or clear the bot's picture
    sessionphone channels [list|add NAME TARGET|seed]  where the bot may send
    sessionphone channels rejected [--profile public]  who the public bot turned away
    sessionphone bot install [--owner ADDR]  run the resident bot under launchd (calls, chat)
    sessionphone bot status|uninstall|run
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
import sys

import click

from sessionphone import account, avatar, channels, hearing, media, paths, service
from sessionphone.bridge import dc_env

CTX = {"help_option_names": ["-h", "--help"]}


def _fail(exc: Exception, code: int) -> None:
    click.echo(f"sessionphone: {exc}", err=True)
    sys.exit(code)


@click.group(context_settings=CTX, invoke_without_command=True)
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

    Idempotent: run it again and it keeps the account, sets the name, and
    prints the same invite. Exit 0 done, 1 it failed (the reason is printed).
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
              help="an HTML template with {{AGENT_NAME}} {{INVITE}} {{QR_SVG}} (default: the skill's qr-page.html)")
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
        click.echo(f"joined: {who}, now the OWNER and the first approved caller ({info['callers']})")
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
    else:
        click.echo(f"joined: {who}, linked for text; NOT approved to call "
                   f"(`sessionphone callers add {info['addr']}` on the owner's word)")


@main.group(context_settings=CTX, invoke_without_command=True)
@click.pass_context
def callers(ctx: click.Context) -> None:
    """Who may ring this agent: the owner first, one address per line.

    The bot answers a call only from an address in this list. DC_CALLERS
    grants nothing. An empty list means nobody: it fails closed.
    """
    if ctx.invoked_subcommand is not None:
        return
    try:
        paths.ensure_private()
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    rows = account.read_callers()
    if not rows:
        click.echo(f"nobody may ring yet ({paths.callers_path()}); `sessionphone link` makes the owner")
        return
    for addr in rows:
        click.echo(addr)


@callers.command("add", context_settings=CTX)
@click.argument("addr")
@click.option("--note", default="", help="who this is, kept as a comment")
def callers_add(addr: str, note: str) -> None:
    """Approve ADDR to ring this agent."""
    try:
        paths.ensure_private()
        added = account.add_caller(addr, note)
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    except ValueError as exc:
        _fail(exc, 1)
        return
    click.echo(f"{'approved' if added else 'already approved'}: {addr.strip().lower()}")
    # Callers and channels are two lists. A caller whose chat is
    # not listed hears the call but gets no in-call chat text.
    click.echo(f"hint: to let the bot write to them too (in-call chat text, replies), "
               f"list the chat: `sessionphone channels add NAME {addr.strip().lower()}`")


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

    TARGET is an address (that contact's 1:1 chat, never a group they are in)
    or chat:N (one chat by id, the only way to list a group). Every dc-* tool
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
        line = f"{e.name:<{width}}  {e.target:<{twidth}}" + (f"  # {e.note}" if e.note else "")
        click.echo(line.rstrip())


@channels_.command("add", context_settings=CTX)
@click.argument("name")
@click.argument("target")
@click.option("--note", default="", help="who or what this is, kept as a comment")
def channels_add(name: str, target: str, note: str) -> None:
    """List NAME -> TARGET (an address, or chat:N). Refuses a NAME already listed."""
    try:
        f = _channels_file()
    except (paths.NotConfigured, paths.Unsafe) as exc:
        _fail(exc, 2)
        return
    try:
        before = channels.loose_mode(f)
        entry = channels.add(name, target, note, f)
    except ValueError as exc:
        _fail(exc, 1)
        return
    except OSError as exc:      # e.g. a 0000 list: say which file, never a bare traceback
        _fail(Exception(f"cannot add to {f}: {exc.strerror or exc}; nothing was listed"), 2)
        return
    if before is not None:
        _tightened(f, before)
    click.echo(f"listed: {entry.name}  {entry.target} ({f})")


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
def bot_install(owner: str | None) -> None:
    """Run the bot as this user's launchd job, now and at every login.

    DC_* variables set in this shell are carried into the job, and so are
    sessionmail's mail-route settings (SESSIONMAIL_SENDMAIL, _ORIGIN,
    _RELAY_DOMAIN, _HOME, _ROOT and _HOST), never any other SESSIONMAIL_*.
    Run it again after a plugin update, or to change them. `link` must not be
    running. On a Mac with no MTA (no postfix), export
    SESSIONMAIL_SENDMAIL=drop first: its `mail:` line names the carrier.
    Refused (exit 2, before the old job is touched) when nobody may ring the
    bot: no --owner and no address in the callers file (link the phone, or
    give --owner the owner's Delta Chat address); while DC_CALLERS is set,
    which grants nothing; or when it could send nowhere: channels.txt is
    missing or empty and there is no owner to list in it (--owner, else the
    callers file's owner line).
    Refused (exit 2, writing nothing) when DC_STT_URL names no model: its
    kind is unknown and DC_STT_MODEL is unset (tag the URL or set the model).
    Refused too (exit 2, writing nothing) when this environment lacks the
    media pack the job runs on; the message says how to fetch it.
    A job left by sessionphone 0.1.0 for the same agent (its old label) is
    retired first: booted out, its plist removed, one line each. Refused
    (exit 2, writing nothing, booting nothing out) when that old plist
    carries a setting the new job would drop or change (the message names
    each one, old -> new, and the fix), or when the old job is loaded with
    its plist gone. Exit 1, the new job not started, when launchd still lists
    the old job after its bootout: run install again.
    Its last line, `hearing:`, says whether the bot will hear the caller: it
    sends a half-second test clip to the STT at DC_STT_URL, saying so first.
    """
    try:
        media.require("bot install")    # FIRST: a refusal writes no plist, no channels seed
    except media.Missing as exc:
        _fail(exc, 2)
        return
    seeded: list[channels.Entry] = []
    try:
        p = service.install(owner=owner, seeded=seeded.append, say=click.echo)
    except (paths.NotConfigured, paths.Unsafe, service.NoMailHost, service.NoSTTModel,
            service.NoCallers, service.NoChannels, service.LegacyCarryOver) as exc:
        _fail(exc, 2)
        return
    except Exception as exc:  # noqa: BLE001 - the operator gets launchctl's reason
        _fail(exc, 1)
        return
    st = service.status()
    click.echo(f"installed: {p}")
    if owner:
        click.echo(f"owner:     {owner.strip().lower()}, approved in {dc_env.callers_file()}")
    for e in seeded:
        click.echo(f"channels:  {e.name} {e.target}, listed in {dc_env.channels_file()}")
    click.echo(f"running:   {'pid ' + str(st['pid']) if st['pid'] else 'not yet - see `sessionphone bot status`'}")
    click.echo(f"log:       {paths.root() / 'bot.log'}")
    _mail(p)
    _hearing(p)


def _job_env(plist: pathlib.Path) -> tuple[dict | None, Exception | None]:
    """The environment launchd runs the bot with, read back from the plist just
    installed: -> (env, None), or (None, why it could not be read)."""
    try:
        with open(plist, "rb") as fh:
            return plistlib.load(fh).get("EnvironmentVariables") or {}, None
    except (OSError, plistlib.InvalidFileException, AttributeError) as exc:
        return None, exc


def _mail(plist: pathlib.Path) -> None:
    """`bot install`'s `mail:` line: the SESSIONMAIL_* the job carried and the
    carrier the bot will send by (service.mail_line), judged on the plist as
    written, not on this shell. Said, never a refusal."""
    env, exc = _job_env(plist)
    if env is None:
        click.echo(f"mail:      not confirmed. The job's environment could not be read back from "
                   f"{plist} ({exc}), so its mail carrier is not known.")
        return
    click.echo(f"mail:      {service.mail_line(env)}")


def _hearing(plist: pathlib.Path) -> None:
    """The last lines of `bot install`: will the bot hear the caller?

    Judged on the environment launchd runs the bot with, read back from the
    plist just installed (`job()` carries DC_* and service.MAIL_KEYS only), by
    asking that STT to transcribe a test clip (hearing.py). Said, never a
    refusal: the exit code stays 0, and the job is already installed and loaded."""
    env, exc = _job_env(plist)
    if env is None:
        click.echo(f"hearing:   not confirmed. The job's environment could not be read back from "
                   f"{plist} ({exc}), so DC_STT_URL was not checked.")
        return
    v = hearing.probe(env, say=click.echo)
    for line in hearing.lines(v, env, shell=os.environ):
        click.echo(line)


@bot.command("uninstall", context_settings=CTX)
def bot_uninstall() -> None:
    """Stop the bot and remove its launchd job, and any job sessionphone 0.1.0
    left for it under the old label. The account stays."""
    for line in service.uninstall() or ["no launchd job was installed"]:
        click.echo(line)


@bot.command("status", context_settings=CTX)
def bot_status() -> None:
    """Is the bot installed, loaded and running? Exit 0 running, 1 not."""
    st = service.status()
    click.echo(f"job:     {st['label']} ({'installed' if st['installed'] else 'not installed'})")
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
    click.echo(f"log:     {paths.root() / 'bot.log'}")
    sys.exit(0 if st["pid"] else 1)


@bot.command("run", context_settings=CTX)
def bot_run() -> None:
    """Run the bot in the foreground, for a test. Ctrl-C stops it."""
    try:
        media.require("bot run")
    except media.Missing as exc:
        _fail(exc, 2)
        return
    # bot.py takes argv[1] as the agent when DC_AGENT is unset; here argv[1] is "bot"
    os.environ.setdefault("DC_AGENT", service.agent())
    from sessionphone.bridge import bot as bridge_bot  # heavy: WebRTC, only when asked

    bridge_bot.main()


# ---- sessionphone web: a browser tab as a call device ----------------------

def _webdir(root: pathlib.Path | None) -> pathlib.Path:
    """`<root>/web/`: --root, else SESSIONPHONE_ROOT, else ~/.sessionphone."""
    return (root.expanduser() if root else paths.root()) / "web"


_ROOT_HELP = "the sessionphone root (default: $SESSIONPHONE_ROOT, else ~/.sessionphone); state goes in ROOT/web/"


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
@click.option("--mail-to", "mail_to", default=None, metavar="ADDR",
              help="with --legs: the seat each finished turn of a web call is mailed to "
                   "(default: $DC_MAILTO, else $DC_AGENT@HOST; with neither, --legs is refused)")
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
    """The bot's root, read now: DC_ROOT, else SESSIONPHONE_ROOT, else ~/.sessionphone."""
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

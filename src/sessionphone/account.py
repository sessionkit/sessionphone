"""The agent's own Delta Chat account: make it, show the way in, link the owner.

Ported from the deltachat plugin (mbailey/deltachat, `agent.py`) into
sessionphone, which is standalone. What is new here is `link` and the callers
file: the first person to join by the invite becomes the owner and the first
approved caller, because the bot answers calls only from that allowlist of
approved callers. Owner-from-the-join is PROPOSED.
"""
from __future__ import annotations

import pathlib
import queue
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from importlib import resources
from typing import Any

from deltachat_rpc_client import Account, DeltaChat, Rpc

from sessionphone import channels, keygate, paths
from sessionphone.bridge import dc_env
from sessionphone.paths import NotConfigured


def server_path() -> str:
    """The rpc-server this venv's `deltachat-rpc-server` package ships, never
    whichever is on PATH.

    Measured: a 2.60 server on PATH answered a 2.61
    client's `init_transports` with "Method not found", and a Mac that has
    never seen Delta Chat has no server on PATH at all.

    It is found from the package, not from beside sys.executable. The bots run
    as <venv>/libexec/session-phone, and there the lookup beside the
    interpreter once missed and PATH's 2.60.0 ran instead of the pinned 2.62.0.
    """
    return server_choice()[0]


def server_choice() -> tuple[str, str, str]:
    """-> (path, where, version). In order: the binary inside the package
    (what its bin/ wrapper execs), then <sys.prefix>/bin, then beside the
    interpreter, and only then PATH. where == "PATH" means none was found,
    and the caller should say so."""
    import importlib.metadata
    import importlib.util
    import os
    try:
        version = importlib.metadata.version("deltachat-rpc-server")
    except importlib.metadata.PackageNotFoundError:
        version = "not installed"
    spec = importlib.util.find_spec("deltachat_rpc_server")
    found = []
    if spec and spec.origin:
        found.append((pathlib.Path(spec.origin).parent / "deltachat-rpc-server", "package"))
    found.append((pathlib.Path(sys.prefix) / "bin" / "deltachat-rpc-server", "venv bin"))
    found.append((pathlib.Path(sys.executable).parent / "deltachat-rpc-server", "beside the interpreter"))
    for path, where in found:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path), where, version
    return "deltachat-rpc-server", "PATH", "unknown"


def _open(create: bool = False) -> Iterator[Account]:
    """Yield the account the bot run with this shell's environment opens
    (dc_env.accounts_dir: DC_ACCOUNTS, else <home>/accounts). A generator, so the
    rpc-server child outlives the caller's work and is shut down after it.
    The root it sits in must be private, as for every verb; `setup` makes
    the CLI's root and the bot's root both 0700 (the invite goes in the bot's
    home, dc_env.invite_file)."""
    where = dc_env.accounts_dir()
    bot_root = dc_env.root_of()
    if create and bot_root != paths.root():
        paths.ensure_private(create)
    paths.ensure_private(create, at=bot_root)
    where.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Rpc(accounts_dir=str(where), rpc_server_path=server_path()) as rpc:
        dc = DeltaChat(rpc)
        existing = dc.get_all_accounts()
        if existing:
            account = existing[0]
        elif create:
            account = dc.add_account()
        else:
            raise NotConfigured(f"no account in {where} - run `sessionphone setup` first")
        if not create and not account.is_configured():
            raise NotConfigured(f"the account in {where} is not configured - run `sessionphone setup`")
        yield account


def address(account: Account) -> str:
    """The account's own address: first transport, else the legacy config key."""
    try:
        transports = account.list_transports()
    except Exception:  # noqa: BLE001 - older cores have no transports; fall through
        transports = []
    if transports:
        return str(transports[0].get("addr", ""))
    return str(account.get_config("addr") or "")


def setup(display_name: str, status: str = "", relay_qr: str | None = None) -> dict[str, Any]:
    """Make this agent's account on a PUBLIC chatmail relay; save and return the invite.

    No credentials are asked for: `init_transports` makes an account on the
    chatmail relay that answers fastest, which is what makes this work on
    someone else's Mac. A `DCACCOUNT:` QR names a specific relay instead.
    Idempotent: a second run keeps the account and prints the invite again.
    """
    for account in _open(create=True):
        fresh = not account.is_configured()
        if fresh:
            account.set_config("bot", "1")
            account.init_transports(relay_qr) if relay_qr else account.init_transports()
        account.set_config("displayname", display_name)
        if status:
            account.set_config("selfstatus", status)
        account.start_io()
        link = account.get_qr_code()
        saved = dc_env.invite_file()
        saved.write_text(link + "\n")
        return {
            "addr": address(account),
            "displayname": display_name,
            "invite": link,
            "invite_file": str(saved),
            "root": str(paths.root()),
            "created": fresh,
        }
    raise NotConfigured("unreachable")


def invite() -> str:
    """The saved invite link, without starting anything: the one `setup` saved
    for the account the bot run with this shell's environment opens
    (dc_env.invite_file)."""
    paths.ensure_private(at=dc_env.root_of())
    f = dc_env.invite_file()
    if not f.exists():
        raise NotConfigured(f"no invite at {f} - run `sessionphone setup` first")
    return f.read_text().strip()


def invite_name(link: str) -> str:
    """The `n=` name inside an invite link, decoded; empty if it has none.

    The fragment is `FINGERPRINT&v=3&i=..&s=..&a=ADDR&n=NAME`, and `n` is
    the display name `setup` gave the account.
    """
    fragment = urllib.parse.urlsplit(link).fragment
    _, _, params = fragment.partition("&")
    return urllib.parse.parse_qs(params).get("n", [""])[0]


def display_name() -> str:
    """The name `setup` gave the account, read from the saved invite.

    The invite is read first because opening the account fails while the
    bot or `link` holds it (measured with `deltachat qr`).
    """
    try:
        name = invite_name(invite())
    except NotConfigured:
        name = ""
    if name:
        return name
    for account in _open():
        return str(account.get_config("displayname") or "")
    return ""


# ---- the callers file -------------------------------------------------------


def read_caller_entries(path: pathlib.Path | None = None, log=None) -> list[keygate.CallerEntry]:
    """The callers file's entries (address, pinned key or None, note), in
    file order: what the call gate reads. No file means nobody. PATH defaults
    to the file the bot reads (dc_env.callers_file, this shell's environment)."""
    return keygate.read_caller_entries(path or dc_env.callers_file(), log)


def read_callers(path: pathlib.Path | None = None) -> list[str]:
    """The listed addresses, lowercased, in file order, pinned or not (a line
    `ADDR  key:FPR` gives ADDR). No file means nobody."""
    out = []
    for e in read_caller_entries(path):
        if e.addr not in out:
            out.append(e.addr)
    return out


def read_owner(path: pathlib.Path | None = None) -> str | None:
    """The address on the callers line whose note starts `owner` (what `link`
    and `bot install --owner` write), lowercased; None if there is none."""
    for e in read_caller_entries(path):
        if e.note.lower().startswith("owner"):
            return e.addr
    return None


def add_caller(addr: str, note: str = "", path: pathlib.Path | None = None,
               key: str | None = None) -> bool:
    """Approve ADDR to ring this agent, bound to KEY (a fingerprint). Returns
    False if nothing changed. Without KEY the line is UNPINNED: refused until
    a key is pinned to it (only `bot install --owner` on a file from an older
    version writes one, for the bot's first start to pin)."""
    path = path or dc_env.callers_file()
    if key is not None:
        norm = keygate.normalize(key)
        if norm is None:
            raise ValueError(f"not a key fingerprint (40 or 64 hex digits): {key!r}")
        key = norm
    return keygate.add_entry(path, addr, key, note)


class AccountBusy(Exception):
    """The account could not be opened to read a contact's key (the bot or
    `link` holds it, or there is none). Its str() says to give --key."""


def find_key(addr: str, pin_how: str = keygate.PIN_HOW) -> keygate.Decision:
    """The attended pin pass for ADDR: open the account and look for the one
    key contact with that address. -> a Decision: "pinned" with its key only
    when exactly one has it. Raises AccountBusy, having read nothing."""
    addr = addr.strip().lower()
    entry = keygate.CallerEntry(addr, None)
    try:
        for acct in _open():
            return keygate.decide(acct, entry, unattended=False, pin_how=pin_how)
    except NotConfigured as exc:
        raise AccountBusy(f"{exc}; give --key FPR ({keygate.SHOWS})") from None
    except Exception as exc:  # noqa: BLE001 - the lock: the bot holds the account
        raise AccountBusy(f"could not open the account to read {addr}'s key, most likely because "
                          f"the bot holds it ({exc}); give --key FPR ({keygate.SHOWS}), or stop "
                          f"the bot and run this again") from None
    raise AccountBusy("no account")


# ---- link: the person scans, and becomes the owner -------------------------


GREETING = "Linked. You're my owner, so you're the one person who can ring me."


def link(timeout: float | None = 600.0, greet: bool = True) -> dict[str, Any]:
    """Hold the account until someone joins by the invite; approve the first as owner.

    The join is a handshake that needs this side online: with nothing
    holding the account the phone sits at "Establishing connection" (measured:
    stuck after 120 s with no listener, done in 4 s
    with one). So this blocks, and the guide runs it backgrounded BEFORE the
    person scans.

    The first joiner under an empty callers file is written as `# owner`,
    bound to the key fingerprint of the contact that completed the join.
    Anyone who joins later is linked for text but NOT approved to call:
    that takes `sessionphone callers add`, by the owner's word.

    The owner is also listed as `owner` in channels.txt, BEFORE the greeting,
    so even that first send goes to a listed chat. If the list cannot take
    them (its `owner` NAME is someone else's), no greeting is sent and
    `channels_problem` says why.

    The callers file and the account are the ones the bot run with this
    shell's environment reads (dc_env.callers_file, dc_env.accounts_dir): the result
    names both.
    """
    callers_file = dc_env.callers_file()
    for account in _open():
        account.start_io()
        wake: queue.Queue = queue.Queue(maxsize=1)

        def waiter(account: Account = account, wake: queue.Queue = wake) -> None:
            try:
                while True:
                    ev = account.wait_for_event()
                    if ev["kind"] == "SecurejoinInviterProgress" and ev["progress"] == 1000:
                        wake.put(ev["contact_id"])
                        return
            except Exception as exc:  # noqa: BLE001 - a broken rpc must WAKE the caller, never hang it
                wake.put(exc)

        threading.Thread(target=waiter, daemon=True).start()
        try:
            got = wake.get(timeout=timeout)
        except queue.Empty:
            return {"joined": False, "timeout": timeout}
        if isinstance(got, Exception):
            raise got
        contact = account.get_contact_by_id(got)
        snap = contact.get_snapshot()
        addr = str(snap.address).lower()
        was_empty = not read_callers(callers_file)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        owner = False
        note = f"owner, {snap.display_name or 'no name'}, joined {stamp}"
        # the owner is bound to the key that completed the join, never to the
        # address alone: the joiner had the invite's secret, and core checked
        # that key signed the join
        try:
            key, key_problem = keygate.contact_fingerprint(contact), None
        except Exception as exc:  # noqa: BLE001 - no key read: no owner written
            key, key_problem = None, f"could not read the joiner's key ({exc})"
        if key is None and key_problem is None:
            key_problem = "the joiner's contact holds no key the bridge can read"
        if was_empty:
            if key:
                owner = add_caller(addr, note, path=callers_file, key=key)
            else:
                key_problem += "; NOT approved to call, nothing written to the callers file"
        listed, tightened, problem, listed_as, loose = False, None, None, None, None
        chans = dc_env.channels_file()
        if owner:
            # >>> channels-seed: the owner is listed BEFORE the greeting
            before = channels.loose_mode(chans)
            try:
                chans.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                entry = channels.seed_owner(addr, note, chans, key=key)
                if entry is not None:
                    tightened = before      # add() made it 0600: the CLI says so out loud
                    listed_as = entry.name
                else:                       # already listed, perhaps under another NAME
                    listed_as = channels.name_of(addr, chans)
                listed = True
                # untouched when already listed: a list still loose is said out loud
                loose = channels.loose_mode(chans)
            except (ValueError, OSError) as exc:
                problem = f"{exc}; NOT greeted, so nothing went to an unlisted chat"
            # <<< channels-seed
        if greet and owner and listed:
            contact.create_chat().send_text(GREETING)
            time.sleep(3)  # let the core hand it to the relay before the server stops
        return {
            "joined": True,
            "addr": addr,
            "name": str(snap.display_name or ""),
            "owner": owner,
            "key": key,
            "key_problem": key_problem if was_empty and not owner else None,
            "callers": str(callers_file),
            "account": str(dc_env.accounts_dir()),
            "channels": str(chans),
            "listed": listed,
            "listed_as": listed_as,
            "tightened": tightened,
            "loose": loose,
            "channels_problem": problem,
        }
    raise NotConfigured("unreachable")


# ---- the connect page -------------------------------------------------------


# The connect page ships as package data beside this module, so a checkout, a
# wheel and a kit install all read the same bytes. There is no second copy.
QR_PAGE = "pages/qr-page.html"


QR_BORDER = 4    # the quiet zone, in modules: the QR spec's minimum
QR_LIGHT = "#fff"
QR_DARK = "#000"


def qr_svg(link: str) -> str:
    """The invite link as an inline SVG: pure Python, no browser needed.

    Dark modules on an explicit white fill that covers the 4-module quiet
    zone, so the code reads the same on a dark page. A `viewBox` and no
    width or height, so the page's CSS scales the whole code to its box;
    without one, a box smaller than the drawing crops it."""
    import io

    import segno

    buf = io.BytesIO()
    segno.make(link, error="m", micro=False).save(
        buf, kind="svg", xmldecl=False, svgns=True, scale=6, border=QR_BORDER,
        dark=QR_DARK, light=QR_LIGHT, omitsize=True)
    return buf.getvalue().decode("utf-8")


def default_page() -> str:
    """The connect page's template, read from the package itself."""
    return resources.files("sessionphone").joinpath(QR_PAGE).read_text(encoding="utf-8")


def qr_page(agent_name: str, template: pathlib.Path | None = None, link: str | None = None) -> str:
    """The connect page: the template with `{{AGENT_NAME}}`, `{{INVITE}}` and
    `{{QR_SVG}}` filled. A template with no invite placeholder is refused
    rather than printed unchanged, which would look like success."""
    link = link or invite()
    text = template.read_text(encoding="utf-8") if template else default_page()
    if "{{INVITE}}" not in text and "{{QR_SVG}}" not in text:
        raise ValueError(f"template has no {{{{INVITE}}}} or {{{{QR_SVG}}}} placeholder: {template or QR_PAGE}")
    return (
        text.replace("{{AGENT_NAME}}", agent_name)
        .replace("{{INVITE}}", link)
        .replace("{{QR_SVG}}", qr_svg(link))
    )

"""`sessionphone web`: one asyncio server for the page and its WebSocket.

It binds 127.0.0.1 only; the tailnet reaches it through a foreground `tailscale
serve --https=PORT http://127.0.0.1:PORT` (never `funnel`). With `--tls CERT
KEY` it terminates TLS itself, and only then may it bind a tailnet address
(100.64.0.0/10 or fd7a:115c:a1e0::/48), never 0.0.0.0 or a public one.

Routes: `GET /` (the page), `/ws` (the WebSocket). Anything else is a 404.

A socket is LOCKED until a passkey ceremony passes (`auth-ok`). A locked
socket may send only `enrol-check` and `auth-finish`; ANY other message, or
anything that is not a JSON object, is answered `refused` ("sign in first") and
the socket is closed. Nothing that makes a call exists before `auth-ok`: the
call handler (`on_call`) is reached only from an unlocked socket.

The origin allow-list is the settings app's (settings@19ecdee web.py:122-151):
only the page's own origins may connect, so another page in the browser cannot
drive this socket. Checked before any challenge is offered.

A socket that DROPS (a locked phone, a lost network: no close frame, 1006) is
one JSON row in the server's sink, `{"conn": "closed", "code": 1006, "during":
"auth"|"idle"|"call"}`, never a library traceback. A clean close
(1000, 1001), or one this server began (a 1008 refusal), adds no row.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import ssl
import subprocess
import time
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from websockets.frames import Frame, Opcode
from websockets.http11 import Request, Response

from sessionphone.web import enrol_qr, names, passkeys

DEFAULT_PORT = 8775           # not 8890, 2022 or 8880, which other local services use
RECHECK_S = 30.0              # how often a missed origin may look the tailnet name up again
MAX_MESSAGE = 256 * 1024      # an SDP offer is a few KB; a passkey message far less
AGENT = "sessionphone web"
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
LOOPBACK = ("127.0.0.1", "::1")

CallHandler = Callable[[ServerConnection, dict, dict], Awaitable[None]]
CloseHandler = Callable[[ServerConnection, dict], Awaitable[None]]


class BindRefused(ValueError):
    """The address is not loopback, or is a tailnet address without --tls."""


def check_bind(host: str, tls: bool) -> str:
    """The host to bind, or BindRefused. Loopback always; a tailnet address only
    with --tls (the server then terminates TLS itself); nothing else, ever."""
    if host in LOOPBACK:
        return host
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused(f"bind {host!r}: give an address, 127.0.0.1 or (with --tls) this host's tailnet IP")
    if ip in TAILNET_V4 or ip in TAILNET_V6:
        if tls:
            return host
        raise BindRefused(f"bind {host}: a tailnet address needs --tls CERT KEY "
                          "(else bind 127.0.0.1 and use `tailscale serve`)")
    raise BindRefused(f"bind {host}: refused, tailnet or localhost only (never 0.0.0.0 or a public address)")


def tailnet_name() -> str:
    """This host's tailnet DNS name, '' if tailscale does not answer.
    settings@19ecdee web.py:122-128."""
    try:
        r = subprocess.run(["tailscale", "status", "--self", "--json"],
                           capture_output=True, text=True, timeout=5, check=False)
        return json.loads(r.stdout)["Self"]["DNSName"].rstrip(".")
    except Exception:  # noqa: BLE001 - no tailscale, no tailnet origin
        return ""


def page_bytes() -> bytes | None:
    try:
        return resources.files("sessionphone.web").joinpath("page/index.html").read_bytes()
    except (FileNotFoundError, OSError):
        return None


def _log(rec: dict) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **rec}), flush=True)


class _WsLog(logging.LoggerAdapter):
    """websockets' own logger for this server, less what the page sends.

    At DEBUG websockets logs each frame's text, and the page's `enrol-check` and
    enrol `auth-finish` carry the enrol code: a frame IN is logged by its kind and
    size only, so the code is never logged, whatever the level. All else as is."""

    def log(self, level, msg, *args, **kwargs):
        if msg == "< %s" and args and isinstance(args[0], Frame) \
                and args[0].opcode in (Opcode.TEXT, Opcode.BINARY, Opcode.CONT):
            args = (f"{args[0].opcode.name} [{len(args[0].data)} bytes, not logged]",)
        super().log(level, msg, *args, **kwargs)

    def process(self, msg, kwargs):
        return msg, kwargs        # never replace the `extra` the connection's own adapter gives


WS_LOG = _WsLog(logging.getLogger("websockets.server"))


class WebServer:
    """The server. `start()` binds; `origins` is known once the port is."""

    def __init__(self, webdir: Path, *, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                 tls: tuple[Path, Path] | None = None, extra_origins: tuple[str, ...] = (),
                 lookup: Callable[[], str] = tailnet_name, log: Callable[[dict], None] = _log,
                 on_call: CallHandler | None = None, on_close: CloseHandler | None = None,
                 desk=None, caller: str | None = None):
        self.host = check_bind(host, tls is not None)
        self.caller = names.caller_name(caller)   # auth-ok's user and a new passkey's label
        self.port = port
        self.tls = tls
        self.keyring = passkeys.Keyring(webdir)
        self.extra = {o.rstrip("/") for o in extra_origins}
        self.lookup = lookup
        self.log = log
        self.desk = desk                    # the calls (web/call.py CallDesk), or None
        self.on_call = desk.on_call if desk is not None else on_call
        self.on_close = desk.on_close if desk is not None else on_close
        self._sessions: dict = {}
        self.origins: set[str] = set()
        self.tailnet = ""                   # the tailnet name the last lookup found ('' none): the QR's origin
        self._looked = 0.0
        self._server = None

    @property
    def sessions(self) -> dict:
        """Live calls by id: never one before auth-ok."""
        return self.desk.sessions if self.desk is not None else self._sessions

    # ---- origins ----
    def _own_origins(self) -> set[str]:
        scheme = "https" if self.tls else "http"
        out = {f"{scheme}://localhost:{self.port}"} | self.extra
        ts = self.tailnet = self.lookup()
        if ts:
            out.add(f"https://{ts}:{self.port}")
        return out

    async def allowed(self, origin: str) -> bool:
        """Is this one of the page's own origins? On a miss, look the tailnet name
        up again, at most once every RECHECK_S (tailscale may come up late)."""
        if origin in self.origins:
            return True
        if time.monotonic() - self._looked >= RECHECK_S:
            self._looked = time.monotonic()
            self.origins |= await asyncio.to_thread(self._own_origins)
        return origin in self.origins

    # ---- HTTP: the page, and the door to /ws ----
    def http(self, connection: ServerConnection, request: Request) -> Response | None:
        path = request.path.split("?", 1)[0]
        if path == "/ws":
            return None
        if path in ("/", "/index.html"):
            body = page_bytes()
            if body is not None:
                return Response(200, "OK", Headers({
                    "Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    # refuse to be framed: the page is opened top-level, never in an iframe
                    "Content-Security-Policy": "frame-ancestors 'none'",
                    "X-Frame-Options": "DENY"}), body)
        return connection.respond(404, "not found\n")

    # ---- the WebSocket ----
    async def _offer(self, ws: ServerConnection, st: dict) -> None:
        rp = passkeys.rp_of(st["origin"])
        o, st["chal"] = passkeys.offer(self.keyring.mine(rp), rp, self.caller)
        await ws.send(json.dumps(o))

    async def _refuse(self, ws: ServerConnection, why: str, **more) -> None:
        await ws.send(json.dumps({"type": "refused", "why": why, **more}))
        await ws.close(1008, why[:100])

    async def _unlock(self, ws: ServerConnection, st: dict, m: dict) -> None:
        """One message on a LOCKED socket. settings@19ecdee web.py:213-266, less the
        resume token, the cookie and trust-on-first-use."""
        origin, rp = st["origin"], passkeys.rp_of(st["origin"])
        kind = m.get("type")
        if kind == "enrol-check":   # before the ceremony: never registers, never unlocks
            if self.keyring.mine(rp):
                why = "this address has a passkey: sign in with it"
            else:
                why = self.keyring.check_enrol_code(m.get("code"), consume=False)
            self.log({"auth": "enrol-check", "origin": origin, "result": why or "ok"})
            await ws.send(json.dumps({"type": "enrol-failed", "why": why} if why else {"type": "enrol-ok"}))
            return
        if kind != "auth-finish":
            await self._refuse(ws, "sign in first")
            return
        chal, st["chal"] = st["chal"], None          # a challenge is used once
        mode = m.get("mode")
        if mode == "enrol":
            if self.keyring.mine(rp):
                why = "this address has a passkey: sign in with it"
            else:                                    # checked again and SPENT here
                why = self.keyring.check_enrol_code(m.get("code"))
                if not why:
                    cred, why = passkeys.register(m, chal, origin)
                    if cred:
                        self.keyring.save(self.keyring.load() + [cred])
        elif mode == "login":
            why = passkeys.login(m, chal, origin, self.keyring.load())
        else:
            why = "every passkey needs the enrol code: choose enrol"
        self.log({"auth": mode, "origin": origin, "result": why or "ok"})
        if why:
            await ws.send(json.dumps({"type": "auth-failed", "why": why}))
            await self._offer(ws, st)
            return
        st["authed"] = True
        await ws.send(json.dumps({"type": "auth-ok", "user": self.caller, "since": time.strftime("%H:%M")}))

    async def handler(self, ws: ServerConnection) -> None:
        origin = ws.request.headers.get("Origin", "") if ws.request else ""
        st = {"origin": origin, "authed": False, "chal": None}
        try:
            await self._session(ws, st)
        except ConnectionClosed as exc:            # never up to websockets: it logs a traceback
            self._closed(ws, st, exc)
        finally:                                   # the socket went: so does its call
            if self.on_close is not None:
                await self.on_close(ws, st)

    def _closed(self, ws: ServerConnection, st: dict, exc: ConnectionClosed) -> None:
        """The socket closed under _session. ONE row when the peer dropped it or
        closed it with an error code; none for a clean close (1000, 1001) or for
        a close this server sent first (a refusal has logged its own row)."""
        if isinstance(exc, ConnectionClosedOK):    # 1000 or 1001 both ways: not news
            return
        if exc.sent is not None and not exc.rcvd_then_sent:   # this server closed first
            return
        call = st.get("call")
        during = ("call" if call is not None and getattr(call, "ended", None) is None
                  else "idle" if st["authed"] else "auth")
        self.log({"conn": "closed", "code": ws.close_code, "during": during})

    async def _session(self, ws: ServerConnection, st: dict) -> None:
        origin = st["origin"]
        await ws.send(json.dumps({"type": "hello", "agent": AGENT}))
        if not origin or not await self.allowed(origin):
            self.log({"auth": "origin", "origin": origin, "result": "refused"})
            await self._refuse(ws, "origin")
            return
        await self._offer(ws, st)
        async for raw in ws:
            try:
                m = json.loads(raw)
            except (TypeError, ValueError):
                m = None
            if not isinstance(m, dict):
                if not st["authed"]:
                    await self._refuse(ws, "sign in first")
                    return
                await ws.send(json.dumps({"type": "error", "why": "not a JSON object"}))
                continue
            if not st["authed"]:
                await self._unlock(ws, st, m)
                continue
            # ---- unlocked: only from here may anything make a call ----
            if m.get("type") in ("call", "ice", "hangup") and self.on_call is not None:
                await self.on_call(ws, st, m)
                continue
            await ws.send(json.dumps({"type": "error", "why": f"unknown message {m.get('type')!r}"}))

    # ---- lifecycle ----
    async def start(self):
        ctx = None
        if self.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(self.tls[0]), str(self.tls[1]))
        self._server = await serve(self.handler, self.host, self.port, ssl=ctx,
                                   process_request=self.http, max_size=MAX_MESSAGE, logger=WS_LOG)
        self.port = self._server.sockets[0].getsockname()[1]
        self._looked = time.monotonic()
        self.origins = await asyncio.to_thread(self._own_origins)
        return self._server

    def bound(self) -> list[str]:
        """Every address a listening socket of this server holds."""
        return [s.getsockname()[0] for s in self._server.sockets] if self._server else []

    def needs_enrol(self) -> bool:
        """True when some allowed origin's RP has no passkey yet."""
        return any(not self.keyring.mine(passkeys.rp_of(o)) for o in self.origins)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def prepare(webdir: Path) -> None:
    """`<root>/web/`, 0700, its parents made 0700 if they are new."""
    webdir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    webdir.mkdir(mode=0o700, exist_ok=True)
    webdir.chmod(0o700)


def run(webdir: Path, *, port: int, host: str, tls: tuple[Path, Path] | None,
        origins: tuple[str, ...], legs: bool = False, demo: bool = False, explicit_root: bool = False,
        mail_to: str | None = None, heard_dir: Path | None = None, caller_name: str | None = None,
        echo: Callable[[str], None] = print, until: Callable[[], Awaitable] | None = None) -> int:
    """The foreground server for `sessionphone web`: 0 on Ctrl-C, 2 on a refused
    bind, a refused --legs or a refused echo-mode flag, 1 when the port cannot
    be had. No launchd: this is the demo's process. Every refusal comes before
    anything is made on disk.

    With --legs each finished turn of a web call is mailed to the seat:
    `mail_to`, else DC_MAILTO, else <DC_AGENT>@HOST, read from the
    heard log in `heard_dir` (else as ears-mail finds it). --legs is refused
    with no seat named, or on a junk host, after the --legs refusals above it.
    With no --legs, --mail-to and --heard-dir are refused (exit 2), not ignored:
    echo mode mails no one.
    `caller_name` is --caller-name: with $DC_CALLER_NAME and the role word
    behind it (web/names.py), read ONCE here and handed to the server (auth-ok,
    a new passkey's label) and the desk (each turn's mail).
    When an enrol code is printed, its QR is printed beside it, through `echo`
    only (web/enrol_qr.py): the URL `<origin>/#code=<code>` for the
    origin a phone can open, never localhost, never in the server's log.
    `until` stands in for Ctrl-C in tests: the server stops when it returns."""
    from sessionphone.web import turns
    from sessionphone.web.call import (  # aiortc: only when serving
        CallDesk,
        legs_path_refusal,
        legs_refusal,
    )

    seat, heard = None, None
    if not legs and (mail_to is not None or heard_dir is not None):
        given = [f for f, v in (("--mail-to", mail_to), ("--heard-dir", heard_dir)) if v is not None]
        echo(f"sessionphone web: {' and '.join(given)} without --legs: echo mode mails no one "
             "(add --legs, or drop the flag)")
        return 2
    if legs:
        why = legs_refusal(webdir.parent, explicit_root=explicit_root, demo=demo) or legs_path_refusal(webdir)
        if not why:
            seat, why = turns.named_seat(mail_to)
        if not why:
            # the second door: the turns go signed by `message send
            # --message-id` as the web seat, so the binary, the signer, the KEY'S SEAT
            # (it must be the web seat) and the --message-id probe are all checked now
            from sessionphone.bridge import seatmail
            why = seatmail.check(seat=seatmail.local_part(seat.addr),
                                 door="the web seat: DC_ADDR, else <DC_AGENT or the To's box>-chat") or ""
        if why:
            echo(f"sessionphone web: {why}")
            return 2
        heard = turns.heard_dir(heard_dir)

    def desk_log(m) -> None:              # a call's row beside the auth rows; a leg's line as before
        if isinstance(m, dict):
            _log(m)
        else:
            echo(f"  {m}")

    caller = names.caller_name(caller_name)
    desk = CallDesk(webdir, legs=legs, seat=seat, heard=heard, log=desk_log, caller=caller)
    try:
        srv = WebServer(webdir, port=port, host=host, tls=tls, extra_origins=origins, log=_log, desk=desk,
                        caller=caller, lookup=tailnet_name)
    except BindRefused as exc:
        echo(f"sessionphone web: {exc}")
        return 2
    prepare(webdir)

    async def main() -> None:
        await srv.start()
        scheme = "https" if tls else "http"
        echo(f"sessionphone web: {scheme}://{srv.host}:{srv.port}/  (websocket /ws)")
        echo(f"  origins allowed: {sorted(srv.origins)}")
        echo(f"  passkeys: {srv.keyring.store} ({len(srv.keyring.load())})")
        if not tls:
            echo(f"  tailnet: beside this, in the foreground: tailscale serve --https={srv.port} "
                 f"http://127.0.0.1:{srv.port}   (never funnel; Ctrl-C removes it)")
        await desk.start()
        echo(f"  calls: {'LEGS under ' + str(webdir) + (' (DEMO)' if demo else '') if legs else 'echo (no legs: nothing on this host can hear a call)'}")
        if seat is not None:
            for line in turns.start_lines(seat, heard):
                echo(f"  {line}")
        if srv.needs_enrol():
            code = srv.keyring.new_enrol_code()
            echo(f"  enrol code: {code}  (one passkey, {passkeys.ENROL_TTL // 60} minutes; "
                 "`sessionphone web enrol` prints a fresh one)")
            # the QR beside it: echo only, never _log; the URL is never printed as text
            origin, why = enrol_qr.phone_origin(srv.tailnet, srv.port, origins)
            for line in enrol_qr.lines(code, origin, why, indent="  "):
                echo(line)
        try:
            await (until() if until is not None else asyncio.Future())   # until Ctrl-C
        finally:
            await desk.stop()
            await srv.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        echo(f"sessionphone web: cannot listen on {srv.host}:{port}: {exc.strerror or exc}")
        return 1
    return 0

"""`sessionphone web`'s server over a REAL socket on 127.0.0.1:0 (PHONE-133 build-001).

No browser: the protocol is JSON, so a `websockets` client and the software
passkey drive it (design §5). The tailnet lookup is stubbed: no test runs
`tailscale`. Nothing here dials :8890, :2022 or :8880.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import ssl
import urllib.request

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
)
from websockets.frames import Close

from sessionphone.web import passkeys as pk
from sessionphone.web import server as srvmod
from sessionphone.web.server import BindRefused, WebServer, check_bind


async def recv(ws) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def closed(ws) -> int:
    """Wait for the server to close; its close code."""
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    return ws.close_code


class Rig:
    """One server on 127.0.0.1:0 in a scratch dir, and what it logged and was asked."""

    def __init__(self, tmp_path, **kw):
        self.logs: list[dict] = []
        self.calls: list[dict] = []

        async def on_call(ws, st, m):
            self.calls.append(m)
            await ws.send(json.dumps({"type": "spy", "got": m["type"]}))

        self.srv = WebServer(tmp_path / "web", port=0, lookup=lambda: "", log=self.logs.append,
                             on_call=on_call, **kw)

    @property
    def origin(self):
        return f"http://localhost:{self.srv.port}"

    def ws(self, origin=None, **kw):
        scheme = "wss" if self.srv.tls else "ws"
        return connect(f"{scheme}://127.0.0.1:{self.srv.port}/ws",
                       origin=self.origin if origin is None else origin, **kw)


def serve(tmp_path, body, **kw):
    """Run `body(rig)` against a started server, then close it."""
    async def main():
        rig = Rig(tmp_path, **kw)
        await rig.srv.start()
        try:
            return await body(rig)
        finally:
            await rig.srv.close()
    return asyncio.run(main())


async def enrol(rig, softkey) -> object:
    """A full enrol over the socket: offer, pre-check, ceremony, auth-ok. The key."""
    code = rig.srv.keyring.new_enrol_code()
    key = softkey(rig.origin)
    async with rig.ws() as ws:
        assert (await recv(ws))["type"] == "hello"
        o = await recv(ws)
        assert o["mode"] == "enrol" and o["rpId"] == "localhost"
        await ws.send(json.dumps({"type": "enrol-check", "code": code}))
        assert (await recv(ws)) == {"type": "enrol-ok"}
        await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                  **key.create(o["challenge"])}))
        assert (await recv(ws))["type"] == "auth-ok"
    return key


# ---- origins --------------------------------------------------------------

def test_a_foreign_origin_is_refused_before_any_challenge(tmp_path):
    async def body(rig):
        async with rig.ws(origin="https://evil.example") as ws:
            assert (await recv(ws))["type"] == "hello"
            assert (await recv(ws)) == {"type": "refused", "why": "origin"}
            assert await closed(ws) == 1008
        assert rig.logs[-1] == {"auth": "origin", "origin": "https://evil.example", "result": "refused"}
    serve(tmp_path, body)


def test_no_origin_is_refused(tmp_path):
    async def body(rig):
        async with connect(f"ws://127.0.0.1:{rig.srv.port}/ws") as ws:
            await recv(ws)
            assert (await recv(ws))["why"] == "origin"
            assert await closed(ws) == 1008
    serve(tmp_path, body)


def test_the_tailnet_origin_is_allowed(tmp_path):
    async def body(rig):
        rig.srv.lookup = lambda: "host.example.ts.net"
        rig.srv._looked = 0.0                 # the next miss may look again
        async with rig.ws(origin=f"https://host.example.ts.net:{rig.srv.port}") as ws:
            await recv(ws)
            o = await recv(ws)
            assert o["type"] == "auth" and o["rpId"] == "host.example.ts.net"
    serve(tmp_path, body)


def test_the_tailnet_root_origin_is_not_ours(tmp_path):
    """https://<ts-name> with no port is 443, which belongs to another service."""
    async def body(rig):
        rig.srv.lookup = lambda: "host.example.ts.net"
        rig.srv._looked = 0.0
        async with rig.ws(origin="https://host.example.ts.net") as ws:
            await recv(ws)
            assert (await recv(ws))["why"] == "origin"
    serve(tmp_path, body)


def test_an_extra_origin_is_allowed(tmp_path):
    async def body(rig):
        async with rig.ws(origin="https://scratch.example:9999") as ws:
            await recv(ws)
            assert (await recv(ws))["type"] == "auth"
    serve(tmp_path, body, extra_origins=("https://scratch.example:9999/",))


# ---- the lock -------------------------------------------------------------

@pytest.mark.parametrize("first", [
    {"type": "call", "sdp": "v=0"},
    {"type": "ice", "candidate": None},
    {"type": "hangup"},
    {"type": "anything"},
])
def test_a_locked_socket_is_refused_and_closed_and_no_call_is_made(tmp_path, first):
    async def body(rig):
        async with rig.ws() as ws:
            await recv(ws)
            assert (await recv(ws))["type"] == "auth"
            await ws.send(json.dumps(first))
            assert (await recv(ws)) == {"type": "refused", "why": "sign in first"}
            assert await closed(ws) == 1008
        assert rig.calls == [] and rig.srv.sessions == {}
    serve(tmp_path, body)


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "null", b"\x00\x01"])
def test_a_locked_socket_sending_garbage_is_closed(tmp_path, raw):
    async def body(rig):
        async with rig.ws() as ws:
            await recv(ws)
            await recv(ws)
            await ws.send(raw)
            assert (await recv(ws))["why"] == "sign in first"
            assert await closed(ws) == 1008
        assert rig.calls == []
    serve(tmp_path, body)


def test_a_failed_sign_in_then_call_is_still_refused(tmp_path, softkey):
    async def body(rig):
        rig.srv.keyring.new_enrol_code()
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": "WRONGWRONG",
                                      **softkey(rig.origin).create(o["challenge"])}))
            assert (await recv(ws))["type"] == "auth-failed"
            assert (await recv(ws))["type"] == "auth"          # a fresh offer
            await ws.send(json.dumps({"type": "call", "sdp": "v=0"}))
            assert (await recv(ws))["why"] == "sign in first"
        assert rig.calls == [] and rig.srv.keyring.load() == []
    serve(tmp_path, body)


# ---- enrol, login, and what opens after -----------------------------------

def test_enrol_then_login_then_auth_ok_opens_the_call_handler(tmp_path, softkey):
    async def body(rig):
        key = await enrol(rig, softkey)
        [cred] = rig.srv.keyring.load()
        assert cred["id"] == key.id and cred["rp_id"] == "localhost"
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            assert o["mode"] == "login" and o["allow"] == [key.id]
            await ws.send(json.dumps({"type": "auth-finish", "mode": "login", **key.get(o["challenge"])}))
            assert (await recv(ws))["type"] == "auth-ok"
            await ws.send(json.dumps({"type": "call", "sdp": "v=0"}))
            assert (await recv(ws)) == {"type": "spy", "got": "call"}
        assert [m["type"] for m in rig.calls] == ["call"]
        assert [x["result"] for x in rig.logs if x.get("auth") in ("enrol", "login")] == ["ok", "ok"]
    serve(tmp_path, body)


def test_register_without_the_code_is_refused(tmp_path, softkey):
    """No trust-on-first-use: an empty store does not let the first visitor in."""
    async def body(rig):
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "register",
                                      **softkey(rig.origin).create(o["challenge"])}))
            r = await recv(ws)
            assert r["type"] == "auth-failed" and "enrol code" in r["why"]
        assert rig.srv.keyring.load() == []
    serve(tmp_path, body)


def test_enrol_with_no_code_pending_is_refused(tmp_path, softkey):
    async def body(rig):
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "enrol-check", "code": "ANYTHING22"}))
            assert "no enrol code" in (await recv(ws))["why"]
            await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": "ANYTHING22",
                                      **softkey(rig.origin).create(o["challenge"])}))
            assert (await recv(ws))["type"] == "auth-failed"
        assert rig.srv.keyring.load() == []
    serve(tmp_path, body)


def test_a_wrong_precheck_leaves_the_store_and_counts_a_try(tmp_path):
    async def body(rig):
        rig.srv.keyring.new_enrol_code()
        async with rig.ws() as ws:
            await recv(ws)
            await recv(ws)
            await ws.send(json.dumps({"type": "enrol-check", "code": "WRONGWRONG"}))
            assert "4 tries left" in (await recv(ws))["why"]
        assert rig.srv.keyring.load() == []
    serve(tmp_path, body)


def test_once_this_address_has_a_passkey_enrol_is_closed(tmp_path, softkey):
    async def body(rig):
        await enrol(rig, softkey)
        code = rig.srv.keyring.new_enrol_code()
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            assert o["mode"] == "login"
            await ws.send(json.dumps({"type": "enrol-check", "code": code}))
            assert "has a passkey" in (await recv(ws))["why"]
            await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                      **softkey(rig.origin).create(o["challenge"])}))
            assert "has a passkey" in (await recv(ws))["why"]
        assert len(rig.srv.keyring.load()) == 1
        assert rig.srv.keyring.check_enrol_code(code, consume=False) == ""   # left unspent
    serve(tmp_path, body)


def test_a_challenge_is_used_once(tmp_path, softkey):
    async def body(rig):
        key = await enrol(rig, softkey)
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            good = key.get(o["challenge"])
            await ws.send(json.dumps({"type": "auth-finish", "mode": "login", "id": "nobody",
                                      **{k: v for k, v in good.items() if k != "id"}}))
            assert (await recv(ws))["type"] == "auth-failed"
            await recv(ws)                                     # the fresh offer
            await ws.send(json.dumps({"type": "auth-finish", "mode": "login", **good}))
            r = await recv(ws)
            assert r["type"] == "auth-failed" and "challenge" in r["why"]
    serve(tmp_path, body)


def test_an_assertion_from_another_socket_does_not_open_this_one(tmp_path, softkey):
    async def body(rig):
        key = await enrol(rig, softkey)
        async with rig.ws() as a, rig.ws() as b:
            await recv(a)
            oa = await recv(a)
            await recv(b)
            await recv(b)
            await b.send(json.dumps({"type": "auth-finish", "mode": "login", **key.get(oa["challenge"])}))
            assert (await recv(b))["type"] == "auth-failed"
    serve(tmp_path, body)


def test_unlocked_unknown_messages_are_answered_not_dropped(tmp_path, softkey):
    async def body(rig):
        key = await enrol(rig, softkey)
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "login", **key.get(o["challenge"])}))
            await recv(ws)
            await ws.send(json.dumps({"type": "teleport"}))
            assert (await recv(ws))["type"] == "error"
            await ws.send("[1]")
            assert (await recv(ws))["type"] == "error"
    serve(tmp_path, body)


# ---- inside a frame: crossOrigin (PHONE-139, design §5.5) -----------------

def test_an_enrol_made_inside_a_frame_is_refused_and_logged(tmp_path, softkey):
    async def body(rig):
        code = rig.srv.keyring.new_enrol_code()
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                      **softkey(rig.origin).create(o["challenge"], cross=True)}))
            r = await recv(ws)
            assert r["type"] == "auth-failed" and r["why"].startswith("crossOrigin is true")
            assert (await recv(ws))["type"] == "auth"          # a fresh offer, still locked
            await ws.send(json.dumps({"type": "call", "sdp": "v=0"}))
            assert (await recv(ws))["why"] == "sign in first"
        assert rig.srv.keyring.load() == [] and rig.calls == []
        [rec] = [x for x in rig.logs if x.get("auth") == "enrol"]
        assert rec["origin"] == rig.origin and rec["result"].startswith("crossOrigin is true")
    serve(tmp_path, body)


def test_a_login_made_inside_a_frame_is_refused_and_logged(tmp_path, softkey):
    async def body(rig):
        key = await enrol(rig, softkey)
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "login",
                                      **key.get(o["challenge"], cross=True)}))
            r = await recv(ws)
            assert r["type"] == "auth-failed" and r["why"].startswith("crossOrigin is true")
            assert (await recv(ws))["type"] == "auth"
            await ws.send(json.dumps({"type": "call", "sdp": "v=0"}))
            assert (await recv(ws))["why"] == "sign in first"
        assert rig.calls == []
        [rec] = [x for x in rig.logs if x.get("auth") == "login"]
        assert rec["origin"] == rig.origin and rec["result"].startswith("crossOrigin is true")
    serve(tmp_path, body)


def test_crossorigin_false_signs_in_as_before(tmp_path, softkey):
    """What a top-level page's browser sends: crossOrigin false, both ceremonies."""
    async def body(rig):
        code = rig.srv.keyring.new_enrol_code()
        key = softkey(rig.origin)
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                      **key.create(o["challenge"], cross=False)}))
            assert (await recv(ws))["type"] == "auth-ok"
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "login",
                                      **key.get(o["challenge"], cross=False)}))
            assert (await recv(ws))["type"] == "auth-ok"
        assert [x["result"] for x in rig.logs if x.get("auth") in ("enrol", "login")] == ["ok", "ok"]
    serve(tmp_path, body)


# ---- HTTP -----------------------------------------------------------------

def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.mark.parametrize("path", ["/", "/index.html", "/?x=1"])
def test_the_page_is_served(tmp_path, path):
    async def body(rig):
        status, headers, data = await asyncio.to_thread(_get, rig.srv.port, path)
        assert status == 200 and b"<html" in data
        assert headers["Content-Type"].startswith("text/html") and headers["Cache-Control"] == "no-store"
    serve(tmp_path, body)


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_the_page_refuses_to_be_framed_on_every_route(tmp_path, path):
    """Both routes, asserted apart, so splitting them later cannot drop a header."""
    async def body(rig):
        status, headers, _ = await asyncio.to_thread(_get, rig.srv.port, path)
        assert status == 200
        assert headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert headers["X-Frame-Options"] == "DENY"
        # the headers the page already had are kept
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
    serve(tmp_path, body)


def test_ws_is_unchanged_by_the_frame_headers(tmp_path):
    """/ws is left to the WebSocket handshake: no page headers on a plain GET, and the
    upgrade still opens and offers a challenge."""
    async def body(rig):
        status, headers, _ = await asyncio.to_thread(_get, rig.srv.port, "/ws")
        assert status != 200
        assert "Content-Security-Policy" not in headers and "X-Frame-Options" not in headers
        async with rig.ws() as ws:
            assert (await recv(ws))["type"]
    serve(tmp_path, body)


@pytest.mark.parametrize("path", ["/x", "/../etc/passwd", "/page/index.html", "/passkeys.json", "/ws/x"])
def test_anything_else_is_404(tmp_path, path):
    async def body(rig):
        status, _, _ = await asyncio.to_thread(_get, rig.srv.port, path)
        assert status == 404
    serve(tmp_path, body)


# ---- the bind -------------------------------------------------------------

def test_the_server_binds_loopback_only(tmp_path):
    async def body(rig):
        assert rig.srv.bound() == ["127.0.0.1"]
    serve(tmp_path, body)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "8.8.8.8", "host.example.ts.net", ""])
def test_check_bind_refuses_anything_but_loopback_or_tailnet(host):
    for tls in (False, True):
        with pytest.raises(BindRefused):
            check_bind(host, tls)


@pytest.mark.parametrize("host", ["100.95.47.50", "fd7a:115c:a1e0::4c3a:2f33"])
def test_a_tailnet_address_needs_tls(host):
    with pytest.raises(BindRefused, match="--tls"):
        check_bind(host, False)
    assert check_bind(host, True) == host


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_is_always_fine(host):
    assert check_bind(host, False) == host


def test_the_server_refuses_a_public_bind_at_construction(tmp_path):
    with pytest.raises(BindRefused):
        WebServer(tmp_path / "web", host="0.0.0.0")


# ---- TLS: the scratch HTTPS origin ----------------------------------------

def _self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    c, k = tmp_path / "cert.pem", tmp_path / "key.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return c, k


def test_tls_enrol_and_login_on_a_scratch_https_origin(tmp_path, softkey):
    cert, key_pem = _self_signed(tmp_path)
    client = ssl.create_default_context(cafile=str(cert))

    async def body(rig):
        origin = f"https://localhost:{rig.srv.port}"
        assert origin in rig.srv.origins
        code = rig.srv.keyring.new_enrol_code()
        key = softkey(origin)
        async with connect(f"wss://localhost:{rig.srv.port}/ws", origin=origin, ssl=client) as ws:
            await recv(ws)
            o = await recv(ws)
            await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                      **key.create(o["challenge"])}))
            assert (await recv(ws))["type"] == "auth-ok"
        assert rig.srv.keyring.load()[0]["rp_id"] == "localhost"
    serve(tmp_path, body, tls=(cert, key_pem))


# ---- run(): the foreground process ----------------------------------------

def test_run_refuses_a_public_bind_with_exit_2(tmp_path):
    out: list[str] = []
    assert srvmod.run(tmp_path / "web", port=0, host="0.0.0.0", tls=None, origins=(), echo=out.append) == 2
    assert "refused" in out[0] and not (tmp_path / "web").exists()


def test_needs_enrol_until_every_origin_has_a_passkey(tmp_path, softkey):
    async def body(rig):
        assert rig.srv.needs_enrol()
        await enrol(rig, softkey)
        assert not rig.srv.needs_enrol()
    serve(tmp_path, body)


def test_offer_names_only_this_rps_passkeys(tmp_path):
    ring = pk.Keyring(tmp_path / "web")
    ring.save([{"id": "other", "rp_id": "host.example.ts.net", "spki": "", "alg": -7}])

    async def body(rig):
        async with rig.ws() as ws:
            await recv(ws)
            o = await recv(ws)
            assert o["mode"] == "enrol" and o["allow"] == []
    serve(tmp_path, body)


# ---- a dropped socket: one clean line, no traceback (PHONE-137) -----------
#
# A phone that locks, or a tab that is closed, drops the TCP connection with no
# close frame. That must log ONE JSON row in the server's own sink,
# {"conn": "closed", "code": 1006, "during": "auth"|"call"}, and websockets must
# log no "connection handler failed" traceback. A clean close (1000) logs as before.

def drop(ws) -> None:
    """Drop the client's TCP connection with NO close frame: a locked phone."""
    ws.transport.abort()


async def handled(srv) -> None:
    """Until every connection handler has finished, the library's logging included."""
    for _ in range(200):
        if not srv._server.handlers:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{len(srv._server.handlers)} connection handler(s) still running")


def conn_rows(rows) -> list[dict]:
    return [r for r in rows if "conn" in r]


def assert_no_traceback(caplog, capsys) -> None:
    """websockets logged no failure, and nothing printed a traceback."""
    bad = [r for r in caplog.records if r.name.startswith("websockets") and (r.levelno >= 40 or r.exc_info)]
    assert bad == [], [f"{r.name}: {r.getMessage()}" for r in bad]
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err and "ConnectionClosed" not in out + err, out + err


def test_a_drop_during_auth_logs_one_conn_line_and_no_traceback(tmp_path, caplog, capsys):
    async def body(rig):
        ws = await rig.ws()
        assert (await recv(ws))["type"] == "hello"
        assert (await recv(ws))["type"] == "auth"
        drop(ws)
        await handled(rig.srv)
        [row] = conn_rows(rig.logs)
        assert row["conn"] == "closed" and row["code"] == 1006 and row["during"] == "auth", row
    serve(tmp_path, body)
    assert_no_traceback(caplog, capsys)


def test_a_drop_during_auth_is_one_json_line_on_stdout(tmp_path, caplog, capsys):
    """The pane's view: the server's default sink (_log) prints exactly ONE JSON
    line for the drop, and the line carries the time like every auth row."""
    async def main():
        srv = WebServer(tmp_path / "web", port=0, lookup=lambda: "")
        await srv.start()
        try:
            ws = await connect(f"ws://127.0.0.1:{srv.port}/ws", origin=f"http://localhost:{srv.port}")
            await recv(ws)
            await recv(ws)
            drop(ws)
            await handled(srv)
        finally:
            await srv.close()
    asyncio.run(main())
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err, out + err
    [line] = [x for x in out.splitlines() if '"conn"' in x]
    row = json.loads(line)
    assert row["conn"] == "closed" and row["code"] == 1006 and row["during"] == "auth" and row["ts"], row
    bad = [r for r in caplog.records if r.name.startswith("websockets") and (r.levelno >= 40 or r.exc_info)]
    assert bad == [], [r.getMessage() for r in bad]


def test_a_drop_after_a_failed_sign_in_is_still_during_auth(tmp_path, softkey, caplog, capsys):
    async def body(rig):
        rig.srv.keyring.new_enrol_code()
        ws = await rig.ws()
        await recv(ws)
        o = await recv(ws)
        await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": "WRONGWRONG",
                                  **softkey(rig.origin).create(o["challenge"])}))
        assert (await recv(ws))["type"] == "auth-failed"
        await recv(ws)
        drop(ws)
        await handled(rig.srv)
        [row] = conn_rows(rig.logs)
        assert row["during"] == "auth" and row["code"] == 1006, row
    serve(tmp_path, body)
    assert_no_traceback(caplog, capsys)


def test_a_clean_close_during_auth_logs_as_before(tmp_path, caplog, capsys):
    """SC4: a 1000 close is not news. No conn row, no traceback, the auth rows unchanged."""
    async def body(rig):
        async with rig.ws() as ws:
            await recv(ws)
            await ws.send(json.dumps({"type": "enrol-check", "code": "NOPE"}))
            await recv(ws)
            await recv(ws)
        await handled(rig.srv)
        assert conn_rows(rig.logs) == [], rig.logs
        assert [r.get("auth") for r in rig.logs] == ["enrol-check"]
    serve(tmp_path, body)
    assert_no_traceback(caplog, capsys)


def test_a_drop_after_sign_in_with_no_call_is_during_idle(tmp_path, softkey, caplog, capsys):
    """Foreman ruling (PHONE-137 fix-001): an authed socket with no call is "idle"."""
    async def body(rig):
        code = rig.srv.keyring.new_enrol_code()
        ws = await rig.ws()
        await recv(ws)
        o = await recv(ws)
        await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                  **softkey(rig.origin).create(o["challenge"])}))
        assert (await recv(ws))["type"] == "auth-ok"
        drop(ws)
        await handled(rig.srv)
        [row] = conn_rows(rig.logs)
        assert row == {"conn": "closed", "code": 1006, "during": "idle"}, row
    serve(tmp_path, body)
    assert_no_traceback(caplog, capsys)


def test_a_refusal_the_server_sent_adds_no_conn_row(tmp_path, caplog, capsys):
    """A 1008 the server began logs only what the refusal logs, even when the
    client then drops instead of answering the close."""
    async def body(rig):
        ws = await rig.ws(origin="https://evil.example")
        assert (await recv(ws))["type"] == "hello"
        assert (await recv(ws))["why"] == "origin"
        drop(ws)
        await handled(rig.srv)
        assert [r.get("auth") for r in rig.logs] == ["origin"] and conn_rows(rig.logs) == [], rig.logs
    serve(tmp_path, body)
    assert_no_traceback(caplog, capsys)


def test_a_client_close_with_an_error_code_is_one_conn_row(tmp_path, caplog, capsys):
    """Not a drop, but not clean either: the peer's own code is the row's code."""
    async def body(rig):
        ws = await rig.ws()
        await recv(ws)
        await recv(ws)
        await ws.close(4000, "gone")
        await handled(rig.srv)
        [row] = conn_rows(rig.logs)
        assert row == {"conn": "closed", "code": 4000, "during": "auth"}, row
    serve(tmp_path, body)
    assert_no_traceback(caplog, capsys)


class _Ws:
    """What _closed reads of a socket: the code the library settled on."""

    def __init__(self, exc: ConnectionClosed):
        self.close_code = exc.rcvd.code if exc.rcvd is not None else 1006


@pytest.mark.parametrize(("exc", "rows"), [
    # a drop: no close frame either way
    (ConnectionClosedError(None, None), [{"conn": "closed", "code": 1006, "during": "auth"}]),
    # this server sent 1008 and the peer never answered: the refusal's own row is enough
    (ConnectionClosedError(None, Close(1008, "origin")), []),
    # this server sent 1008 first, then the peer answered it
    (ConnectionClosedError(Close(1008, "origin"), Close(1008, "origin"), False), []),
    # the peer closed first with an error code
    (ConnectionClosedError(Close(4000, ""), Close(4000, ""), True),
     [{"conn": "closed", "code": 4000, "during": "auth"}]),
    # clean, both ways (1000 normal, 1001 going away: a tab closed)
    (ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True), []),
    (ConnectionClosedOK(Close(1001, ""), Close(1001, ""), True), []),
])
def test_closed_logs_a_row_only_when_the_peer_broke_it(tmp_path, exc, rows):
    logs: list[dict] = []
    srv = WebServer(tmp_path / "web", port=0, lookup=lambda: "", log=logs.append)
    srv._closed(_Ws(exc), {"authed": False}, exc)
    assert logs == rows


@pytest.mark.parametrize(("st", "during"), [
    ({"authed": False}, "auth"),
    ({"authed": True}, "idle"),
    ({"authed": True, "call": type("C", (), {"ended": None})()}, "call"),
    ({"authed": True, "call": type("C", (), {"ended": "connection failed"})()}, "idle"),
])
def test_closed_names_what_the_socket_was_doing(tmp_path, st, during):
    logs: list[dict] = []
    srv = WebServer(tmp_path / "web", port=0, lookup=lambda: "", log=logs.append)
    exc = ConnectionClosedError(None, None)
    srv._closed(_Ws(exc), st, exc)
    assert [r["during"] for r in logs] == [during]


# ---- the caller's name: ONE setting (PHONE-135 SC2, SC3) ----------------------

async def _login(rig, key) -> dict:
    """Sign in with `key`; the frame the server answers with."""
    async with rig.ws() as ws:
        await recv(ws)
        o = await recv(ws)
        assert o["mode"] == "login" and "user" not in o
        await ws.send(json.dumps({"type": "auth-finish", "mode": "login", **key.get(o["challenge"])}))
        return await recv(ws)


async def _enrol_offer(rig) -> dict:
    async with rig.ws() as ws:
        await recv(ws)
        return await recv(ws)


def test_auth_ok_names_the_role_word_by_default(tmp_path, softkey):
    async def body(rig):
        assert rig.srv.caller == "owner"
        assert (await _enrol_offer(rig))["user"]["displayName"] == "owner (sessionphone)"
        key = await enrol(rig, softkey)
        ok = await _login(rig, key)
        assert ok["type"] == "auth-ok" and ok["user"] == "owner"
    serve(tmp_path, body)


def test_auth_ok_and_the_passkey_label_read_the_setting(tmp_path, softkey):
    async def body(rig):
        o = await _enrol_offer(rig)
        assert o["user"]["name"] == "Ada" and o["user"]["displayName"] == "Ada (sessionphone)"
        ok = await _login(rig, await enrol(rig, softkey))
        assert ok["user"] == "Ada"
    serve(tmp_path, body, caller="Ada")


def test_auth_ok_reads_dc_caller_name_when_no_name_is_handed_in(tmp_path, softkey, monkeypatch):
    monkeypatch.setenv("DC_CALLER_NAME", "Bea")

    async def body(rig):
        assert (await _enrol_offer(rig))["user"]["name"] == "Bea"
        assert (await _login(rig, await enrol(rig, softkey)))["user"] == "Bea"
    serve(tmp_path, body)


def test_a_passkey_enrolled_under_one_name_signs_in_under_another(tmp_path, softkey):
    """SC3, no orphaning: a passkey is matched by its id and rp_id, never the
    label, so a server restarted with a new caller name still lets it in."""
    got = {}

    async def as_ada(rig):
        assert (await _enrol_offer(rig))["user"]["name"] == "Ada"
        got["key"] = await enrol(rig, softkey)
        [cred] = rig.srv.keyring.load()
        assert set(cred) >= {"id", "rp_id", "spki", "alg"} and "Ada" not in json.dumps(cred)

    serve(tmp_path, as_ada, caller="Ada")

    async def as_bea(rig):
        key = got["key"]
        key.origin = rig.origin                  # the same RP (localhost), a new port: a restart
        ok = await _login(rig, key)
        assert ok["type"] == "auth-ok" and ok["user"] == "Bea", ok
        assert [x["result"] for x in rig.logs if x.get("auth") == "login"] == ["ok"]
        assert len(rig.srv.keyring.load()) == 1  # nothing re-enrolled, nothing dropped

    serve(tmp_path, as_bea, caller="Bea")

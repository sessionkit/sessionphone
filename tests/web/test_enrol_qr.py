"""The enrol code as a QR on this machine's terminal (PHONE-136).

SC1: the printed QR is `<origin>/#code=<code>`, nothing in the path or query.
Pinned without a decoder: the printed half-block text is read back into its
module matrix and compared with segno's own matrix for that URL.
SC2: an enrol by QR leaves the code in neither the server's log rows nor
websockets' loggers, even at DEBUG. SC3: a code used once, typed or from the
fragment, is refused the second time.

No test runs `tailscale`: conftest's `no_tailscale` stubs `server.tailnet_name`,
and a test that wants a tailnet sets a name. Nothing dials :8890, :2022 or :8880.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest
import segno
from click.testing import CliRunner
from websockets.asyncio.client import connect

from sessionphone import cli
from sessionphone.web import enrol_qr
from sessionphone.web import passkeys as pk
from sessionphone.web import server as srvmod

TAILNET = "host.example.ts.net"
BLOCKS = {" ": (1, 1), "▀": (0, 1), "▄": (1, 0), "█": (0, 0)}   # segno's compact: 1 is dark


def qr_rows(lines: list[str]) -> list[str]:
    """The printed QR's lines: only half-block characters, never a word."""
    return [x for x in lines if x.strip() and set(x) <= set(BLOCKS)]


def printed_matrix(lines: list[str]) -> list[list[int]]:
    """Read the compact half-block text back into module rows (two rows a line)."""
    top, bottom = [], []
    for line in lines:
        top.append([BLOCKS[c][0] for c in line])
        bottom.append([BLOCKS[c][1] for c in line])
    return [row for pair in zip(top, bottom) for row in pair]


def want_matrix(url: str) -> list[list[int]]:
    """segno's matrix for `url`, with the quiet zone the terminal print has."""
    return [[int(bool(v)) for v in row] for row in segno.make(url, error="m").matrix_iter(border=4)]


def assert_qr_is(lines: list[str], url: str) -> None:
    got, want = printed_matrix(qr_rows(lines)), want_matrix(url)
    if len(want) % 2:                     # an odd height: the last line's lower half is padding
        assert got[-1] == [1] * len(want[0])
        got = got[:-1]
    assert got == want, f"the printed QR is not {url}"


def fragment_code(url: str) -> str:
    """What the page reads: the `code` in the fragment. The path is `/`, no query."""
    u = urlsplit(url)
    assert u.path == "/" and u.query == "", url
    [code] = parse_qs(u.fragment)["code"]
    return code


# ---- the URL: the code in the fragment only (SC1) -------------------------

def test_the_url_carries_the_code_in_the_fragment_only():
    url = enrol_qr.enrol_url(f"https://{TAILNET}:8775", "ABCDEFGHJK")
    assert url == f"https://{TAILNET}:8775/#code=ABCDEFGHJK"
    u = urlsplit(url)
    assert (u.path, u.query, u.fragment) == ("/", "", "code=ABCDEFGHJK")
    assert "ABCDEFGHJK" not in u.scheme + u.netloc + u.path + u.query


def test_a_trailing_slash_on_the_origin_is_one_url():
    assert enrol_qr.enrol_url("https://h:1/", "X") == enrol_qr.enrol_url("https://h:1", "X") == "https://h:1/#code=X"


@pytest.mark.parametrize("origin", ["https://h/enrol", "https://h?code=1", "https://h#x", "ftp://h",
                                    "h:8775", "https://", "https://u:p@h"])
def test_only_a_bare_origin_makes_a_url(origin):
    """So nothing can ever put the code, or anything, in the path or the query."""
    with pytest.raises(ValueError):
        enrol_qr.enrol_url(origin, "ABCDEFGHJK")


# ---- the QR's origin: one a phone can open, never localhost ---------------

def test_the_tailnet_origin_comes_first():
    assert enrol_qr.phone_origin(TAILNET, 8775, ("https://phone.example:8443",)) == (f"https://{TAILNET}:8775", "")


def test_an_origin_on_the_tailnet_host_gives_the_qr_its_port():
    """README, Try it: the backend on 18775 behind `tailscale serve --https=8775`,
    with --origin naming the browser-facing port. The QR must carry 8775."""
    got = enrol_qr.phone_origin(TAILNET, 18775, ("https://phone.example:1", f"https://{TAILNET}:8775/"))
    assert got == (f"https://{TAILNET}:8775", "")


def test_with_no_tailnet_the_first_origin_a_phone_can_open():
    got = enrol_qr.phone_origin("", 8775, ("https://localhost:1", "not a url", "https://phone.example:8443/",
                                           "https://other.example"))
    assert got == ("https://phone.example:8443", "")


@pytest.mark.parametrize("origins", [(), ("https://localhost:8775",), ("http://127.0.0.1:9", "https://[::1]:9"),
                                     ("https://qr.localhost:9",), ("junk",)])
def test_never_localhost_in_a_qr_and_one_line_says_why(origins):
    origin, why = enrol_qr.phone_origin("", 8775, origins)
    assert origin == "" and why.startswith("no QR: ") and "--origin" in why and "\n" not in why
    assert enrol_qr.lines("ABCDEFGHJK", origin, why) == [why]
    assert enrol_qr.lines("ABCDEFGHJK", origin, why, indent="  ") == ["  " + why]


def test_the_qr_text_is_segnos_matrix_for_the_url():
    url = enrol_qr.enrol_url(f"https://{TAILNET}:8775", "ABCDEFGHJK")
    out = enrol_qr.lines("ABCDEFGHJK", f"https://{TAILNET}:8775", "")
    assert out == qr_rows(out)                         # the QR alone: no text line, no URL
    assert_qr_is(out, url)
    assert_qr_is(out, f"https://{TAILNET}:8775/#code=ABCDEFGHJK")


def test_the_qr_is_never_indented():
    """A margin of spaces would print DARK modules into the quiet zone."""
    out = enrol_qr.lines("ABCDEFGHJK", f"https://{TAILNET}:8775", "", indent="  ")
    assert all(not x.startswith(" ") for x in out) and qr_rows(out) == out


# ---- `sessionphone web enrol` prints it (R1, SC1) --------------------------

def enrol_cli(args, tmp_path):
    r = CliRunner().invoke(cli.main, ["web", *args, "enrol", "--root", str(tmp_path / "r")])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    code = lines[0].split()[2]                          # tests/browser/run.sh: awk '/^enrol code:/{print $3}'
    assert lines[0] == f"enrol code:  {code}"           # byte-compatible with before
    return code, lines, r.output


def test_web_enrol_prints_the_qr_of_the_tailnet_fragment_url(tmp_path, monkeypatch):
    monkeypatch.setattr(srvmod, "tailnet_name", lambda: TAILNET)
    code, lines, out = enrol_cli(["--port", "9001"], tmp_path)
    assert_qr_is(lines, f"https://{TAILNET}:9001/#code={code}")
    assert lines[1].startswith("valid until ")
    assert out.count(code) == 1                         # the code line only: the URL is never text
    assert "#code" not in out and "://" not in out
    assert pk.Keyring(tmp_path / "r" / "web").check_enrol_code(code) == ""


def test_web_enrol_reads_the_groups_default_port(tmp_path, monkeypatch):
    monkeypatch.setattr(srvmod, "tailnet_name", lambda: TAILNET)
    code, lines, _ = enrol_cli([], tmp_path)
    assert_qr_is(lines, f"https://{TAILNET}:{srvmod.DEFAULT_PORT}/#code={code}")


def test_web_enrol_with_no_tailnet_uses_the_groups_origin(tmp_path):
    code, lines, _ = enrol_cli(["--origin", "https://localhost:5", "--origin", "https://phone.example:8443"], tmp_path)
    assert_qr_is(lines, f"https://phone.example:8443/#code={code}")


def test_web_enrol_with_no_address_a_phone_can_open_prints_no_qr_and_says_why(tmp_path):
    code, lines, out = enrol_cli(["--origin", "https://localhost:5"], tmp_path)
    assert qr_rows(lines) == [] and len(lines) == 3, out
    assert lines[2].startswith("no QR: ") and "tailscale did not answer" in lines[2]
    assert out.count(code) == 1 and "localhost" not in out


# ---- `sessionphone web` prints it, and an enrol by QR logs no code --------

def test_run_prints_the_qr_and_an_enrol_by_it_logs_no_code(tmp_path, monkeypatch, softkey, caplog, capsys):
    """R1 + SC1 for `sessionphone web`, and SC2: the phone opens the QR's URL (the
    fragment is never sent: the GET is `/`), checks the code, makes the passkey.
    The server's rows (its stdout sink) and websockets' loggers at DEBUG hold no
    code; the code is on the machine's screen (echo) and nowhere else."""
    caplog.set_level(logging.DEBUG, logger="websockets")
    monkeypatch.setattr(srvmod, "tailnet_name", lambda: TAILNET)
    seen, out = {}, []
    real_start = srvmod.WebServer.start

    async def spy_start(self):
        seen["srv"] = self
        return await real_start(self)

    monkeypatch.setattr(srvmod.WebServer, "start", spy_start)

    async def phone():
        srv = seen["srv"]
        origin = f"https://{TAILNET}:{srv.port}"
        [line] = [x for x in out if x.startswith("  enrol code: ")]
        printed = line.split()[2]
        url = enrol_qr.enrol_url(origin, printed)
        assert_qr_is(out, url)                         # SC1: what is on the screen IS this URL
        code = fragment_code(url)                      # what the page fills in
        assert code == printed
        # the browser's GET: urllib, like a browser, never sends the fragment
        def get():                                     # off the loop: the server answers on it
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/#code={code}", timeout=5) as r:
                return r.status
        assert await asyncio.to_thread(get) == 200
        key = softkey(origin)
        async with connect(f"ws://127.0.0.1:{srv.port}/ws", origin=origin) as ws:
            assert json.loads(await ws.recv())["type"] == "hello"
            o = json.loads(await ws.recv())
            assert o["mode"] == "enrol" and o["rpId"] == TAILNET
            await ws.send(json.dumps({"type": "enrol-check", "code": code}))
            assert json.loads(await ws.recv()) == {"type": "enrol-ok"}
            await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                      **key.create(o["challenge"])}))
            assert json.loads(await ws.recv())["type"] == "auth-ok"
        seen["code"] = code

    rc = srvmod.run(tmp_path / "web", port=0, host="127.0.0.1", tls=None, origins=(), echo=out.append,
                    until=phone)
    assert rc == 0, out
    code = seen["code"]
    logged, err = capsys.readouterr()                  # echo went to `out`: stdout is the server's rows
    rows = [json.loads(x) for x in logged.splitlines() if x.startswith("{")]
    assert {"auth": "enrol-check", "origin": f"https://{TAILNET}:{seen['srv'].port}", "result": "ok"}.items() \
        <= next(r for r in rows if r.get("auth") == "enrol-check").items()
    assert any(r.get("auth") == "enrol" and r.get("result") == "ok" for r in rows), rows
    assert code not in logged + err and "#code" not in logged + err
    # the server's websockets loggers (websockets.client here is the test's phone, not the server)
    ws_lines = [r.getMessage() for r in caplog.records
                if r.name.startswith("websockets") and r.name != "websockets.client"]
    assert any(m.startswith("< GET / HTTP/1.1") for m in ws_lines), ws_lines    # DEBUG was on: the access log
    assert any("not logged" in m for m in ws_lines), ws_lines                   # and the frames were seen
    assert not [m for m in ws_lines if code in m or "#code" in m]
    assert not [m for m in out if "#code" in m]                                 # the URL is never text:
    assert [m for m in out if code in m] == [m for m in out if m.startswith("  enrol code: ")]  # one code line


def test_run_with_no_address_a_phone_can_open_says_why_in_one_line(tmp_path, capsys):
    out = []
    rc = srvmod.run(tmp_path / "web", port=0, host="127.0.0.1", tls=None, origins=(), echo=out.append,
                    until=lambda: asyncio.sleep(0))
    assert rc == 0, out
    i = next(i for i, x in enumerate(out) if x.startswith("  enrol code: "))
    assert out[i + 1].startswith("  no QR: ") and qr_rows(out) == [], out


def test_run_prints_no_qr_when_no_code_is_printed(tmp_path, monkeypatch, softkey):
    """Every origin has a passkey: no code, so no QR either."""
    monkeypatch.setattr(srvmod, "tailnet_name", lambda: TAILNET)
    monkeypatch.setattr(srvmod.WebServer, "needs_enrol", lambda self: False)
    out = []
    rc = srvmod.run(tmp_path / "web", port=0, host="127.0.0.1", tls=None, origins=(), echo=out.append,
                    until=lambda: asyncio.sleep(0))
    assert rc == 0 and not [x for x in out if "enrol code" in x or "no QR" in x] and qr_rows(out) == [], out


# ---- websockets' DEBUG log names a frame from the page, never its text ----

def test_the_ws_logger_redacts_only_frames_in(caplog):
    from websockets.frames import Frame, Opcode
    caplog.set_level(logging.DEBUG, logger="websockets.server")
    srvmod.WS_LOG.debug("< %s", Frame(Opcode.TEXT, b'{"code": "SECRETCODE"}'))
    srvmod.WS_LOG.debug("> %s", Frame(Opcode.TEXT, b'{"type": "enrol-ok"}'))
    srvmod.WS_LOG.debug("< GET %s HTTP/1.1", "/")
    got = [r.getMessage() for r in caplog.records]
    assert got[0] == "< TEXT [22 bytes, not logged]"
    assert "enrol-ok" in got[1] and got[2] == "< GET / HTTP/1.1"
    assert not [m for m in got if "SECRETCODE" in m]


# ---- one-time after a scan (SC3) ------------------------------------------

async def _enrol_with(port: int, origin: str, code: str, key) -> tuple[dict, dict]:
    """enrol-check, then auth-finish, with `code`: the two replies."""
    async with connect(f"ws://127.0.0.1:{port}/ws", origin=origin) as ws:
        await ws.recv()
        o = json.loads(await ws.recv())
        assert o["mode"] == "enrol", o
        await ws.send(json.dumps({"type": "enrol-check", "code": code}))
        check = json.loads(await asyncio.wait_for(ws.recv(), 5))
        await ws.send(json.dumps({"type": "auth-finish", "mode": "enrol", "code": code,
                                  **key.create(o["challenge"])}))
        return check, json.loads(await asyncio.wait_for(ws.recv(), 5))


@pytest.mark.parametrize(("first", "second"), [("fragment", "typed"), ("typed", "fragment"),
                                               ("fragment", "fragment")])
def test_a_code_used_once_is_refused_the_second_time(tmp_path, softkey, first, second):
    """The second try is on ANOTHER address (a second RP), so 'this address has a
    passkey' cannot be what refuses it: the code is spent. The fragment's code is
    the same code as the printed one: there is no second, longer-lived path."""
    async def main():
        logs = []
        srv = srvmod.WebServer(tmp_path / "web", port=0, lookup=lambda: TAILNET, log=logs.append)
        await srv.start()
        try:
            a, b = f"http://localhost:{srv.port}", f"https://{TAILNET}:{srv.port}"
            code = srv.keyring.new_enrol_code()
            ways = {"typed": " " + code.lower() + " ",
                    "fragment": fragment_code(enrol_qr.enrol_url(b, code))}
            check, done = await _enrol_with(srv.port, a, ways[first], softkey(a))
            assert check == {"type": "enrol-ok"} and done["type"] == "auth-ok"
            assert srv.keyring.mine(pk.rp_of(b)) == []          # b has no passkey: only the code can refuse
            check, done = await _enrol_with(srv.port, b, ways[second], softkey(b))
            assert check["type"] == "enrol-failed" and "no enrol code is pending" in check["why"]
            assert done["type"] == "auth-failed" and "no enrol code is pending" in done["why"]
            assert srv.keyring.mine(pk.rp_of(b)) == [] and len(srv.keyring.load()) == 1
            assert code not in json.dumps(logs)
        finally:
            await srv.close()
    asyncio.run(main())

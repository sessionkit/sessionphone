"""The enrol code as a QR code on this machine's terminal.

`sessionphone web` and `sessionphone web enrol` print a QR beside the
one-time enrol code. A phone's camera opens the page with the code filled in.

The QR encodes ONE URL, built here and nowhere else: `<origin>/#code=<code>`.
The code is in the FRAGMENT only, never the path or the query. A browser never
sends a fragment, so the GET is `/` and the code reaches no server log and no
proxy. The page reads it, strips it from the address bar, and fills it in.

The QR and the URL go to the terminal (echo) only, never to the server's log,
and the URL is never printed as text: the QR beside the code line carries it.

The origin in the QR must be one a phone can open: the tailnet origin
`https://<tailnet name>:<port>` when tailscale answers (an `--origin` on that
host as given, for its port), else the first `--origin` that is not localhost.
Else there is no QR, and one line says why. Never localhost in a QR.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from urllib.parse import quote, urlsplit

from sessionphone import qrprint

FRAGMENT_KEY = "code"       # the page reads location.hash for this key (page/index.html)


def _is_loopback(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def enrol_url(origin: str, code: str) -> str:
    """`<origin>/#code=<code>`: the code in the fragment, nothing in the path or
    the query. ValueError for an origin that is not scheme://host[:port]."""
    u = urlsplit(origin.rstrip("/"))
    if u.scheme not in ("http", "https") or not u.hostname or u.path or u.query or u.fragment \
            or u.username or u.password:
        raise ValueError(f"not an origin: {origin!r} (want scheme://host[:port])")
    return f"{u.scheme}://{u.netloc}/#{FRAGMENT_KEY}={quote(code, safe='')}"


def phone_origin(tailnet: str, port: int, origins: Sequence[str] = ()) -> tuple[str, str]:
    """(origin, ''): the origin a phone can open; or ('', why there is none).

    The tailnet origin when tailscale answered (`tailnet` is its name, '' when
    it did not): an --origin on that host as given, since it carries the port
    `tailscale serve` listens on when the backend's --port differs (README, Try
    it), else `https://<tailnet>:<port>`. With no tailnet, the first of
    `origins` (--origin) that is a real origin and not localhost. Never
    localhost: a phone cannot open this machine's."""
    given = []
    for o in origins:
        try:
            enrol_url(o, "")
        except ValueError:
            continue
        if not _is_loopback(urlsplit(o).hostname or ""):
            given.append(o.rstrip("/"))
    if tailnet:
        same = [o for o in given if (urlsplit(o).hostname or "").rstrip(".") == tailnet.lower()]
        return (same[0] if same else f"https://{tailnet}:{port}"), ""
    if given:
        return given[0], ""
    named = "; no --origin is one a phone can open" if origins else ""
    return "", (f"no QR: no address a phone can open (tailscale did not answer{named}); "
                "type the code, or pass --origin https://HOST:PORT")


def qr_text(url: str) -> str:
    """The QR for `url`, as compact half-block text (two rows a line), with its
    quiet zone: the one shared printer (qrprint), default polarity."""
    return qrprint.text(url)


def lines(code: str, origin: str, why: str, indent: str = "") -> list[str]:
    """What to echo after the `enrol code:` line: the QR, never indented (a
    space is a DARK module in the compact print, so a margin would spoil the
    quiet zone), or the one line saying why there is none, indented."""
    if not origin:
        return [indent + why]
    return qr_text(enrol_url(origin, code)).rstrip("\n").split("\n")

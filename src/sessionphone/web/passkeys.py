"""Passkey (WebAuthn) checks for `sessionphone web`: who may open a call.

COPIED from the settings app's lock (settings@19ecdee `webauth.py`), never
shared with it: sessionphone keeps its own store under `<root>/web/`, its own
port and its own process. Each copied function names its source lines, so a
fix there can be found here.

Pure functions plus one small store, no network: `server.py` calls them over
its WebSocket. The browser hands over the new credential's public key itself
(`getPublicKey()`, a SubjectPublicKeyInfo), so no CBOR is parsed. Attestation
is "none".

A passkey belongs to one relying party (RP), a host name: the tailnet name
(https://<host>.<tailnet>.ts.net:PORT) or localhost. WebAuthn refuses an IP
address.

ONE CHANGE FROM SETTINGS: there is no trust-on-first-use. Every registration
needs the one-time enrol code printed on the machine itself (settings'
"enrol"), because this page opens a voice line into a seat and the first
visitor could be any device on the tailnet.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

UP, UV = 0x01, 0x04          # authenticatorData flags: user present, user verified
ES256, RS256 = -7, -257
ENROL_TTL = 600              # seconds an enrol code is good for
ENROL_TRIES = 5              # wrong tries that burn it
ENROL_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # no 0/O, 1/I/L: read off a screen, typed on a phone
ENROL_LEN = 10


def user_record(name: str) -> dict:
    """The WebAuthn user record's labels for a new passkey: the caller's name
    (web/names.py), and " (sessionphone)" to tell it apart from settings' in the
    keychain. Only a label: user.id is random for each enrol (offer), and a
    passkey is matched by its id and rp_id alone (login), so a rename orphans
    nothing."""
    return {"name": name, "displayName": f"{name} (sessionphone)"}


def b64u(b: bytes) -> str:
    """settings webauth.py:52-53."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    """settings webauth.py:56-57."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def rp_of(origin: str) -> str:
    """The RP id an origin's passkeys belong to: its host name. settings webauth.py:60-61."""
    return urlparse(origin).hostname or ""


def _write_private(p: Path, text: str) -> None:
    """Write `p` atomically, 0600 from the first byte, its directory 0700.
    settings webauth.py:82-91."""
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(p)


class Keyring:
    """The passkeys and the pending enrol code, in one 0700 directory
    (`<root>/web/`): `passkeys.json` and `enrol.json`, each 0600. The paths are
    parameters, never a fixed settings path."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.store = self.dir / "passkeys.json"
        self.enrol = self.dir / "enrol.json"

    # ---- the store: settings webauth.py:64-79 ----
    def load(self) -> list:
        try:
            return json.loads(self.store.read_text()).get("credentials", [])
        except FileNotFoundError:
            return []

    def save(self, creds: list) -> None:
        _write_private(self.store, json.dumps({"credentials": creds}, indent=2) + "\n")

    def mine(self, rp_id: str) -> list:
        """The ids of this RP's passkeys."""
        return [c["id"] for c in self.load() if c.get("rp_id") == rp_id]

    # ---- the enrol code: settings webauth.py:94-153 ----
    def _drop(self) -> None:
        try:
            self.enrol.unlink()
        except FileNotFoundError:
            pass

    def new_enrol_code(self, now: float | None = None) -> str:
        """A fresh one-time enrol code, good for ENROL_TTL seconds; it replaces any
        earlier one. Only its salted hash, expiry and wrong-try count go to disk."""
        now = time.time() if now is None else now
        code = "".join(secrets.choice(ENROL_ALPHABET) for _ in range(ENROL_LEN))
        salt = secrets.token_hex(16)
        _write_private(self.enrol, json.dumps({"salt": salt, "hash": _enrol_hash(code, salt),
                                               "expires": now + ENROL_TTL, "wrong": 0}) + "\n")
        return code

    def check_enrol_code(self, code, now: float | None = None, consume: bool = True) -> str:
        """'' when `code` is the pending enrol code, which is then CONSUMED; else why
        not. A wrong try counts: the ENROL_TRIES-th burns the code.

        consume=False is the PRE-CHECK, made before the passkey ceremony so a
        mistyped code never leaves a passkey on the device: a right code says ''
        and stays pending, its try count unchanged; a wrong one counts exactly as
        above. The finish checks again with consume=True, so a pre-check alone
        never enrols. The code is read from disk on every try, so
        `sessionphone web enrol` works beside a running server."""
        now = time.time() if now is None else now
        again = "run `sessionphone web enrol` on this machine for a new one"
        try:
            rec = json.loads(self.enrol.read_text())
            salt, want = rec["salt"], rec["hash"]
            expires, wrong = float(rec["expires"]), int(rec.get("wrong", 0))
        except FileNotFoundError:
            return f"no enrol code is pending: {again}"
        except (KeyError, TypeError, ValueError):
            self._drop()                     # unreadable: fail closed, and clear it
            return f"no enrol code is pending: {again}"
        if now >= expires or wrong >= ENROL_TRIES:
            self._drop()
            return f"the enrol code has {'expired' if now >= expires else 'burnt'}: {again}"
        got = _enrol_norm(code)
        if got and hmac.compare_digest(_enrol_hash(got, salt), want):
            if consume:
                self._drop()                 # single use
            return ""
        wrong += 1
        if wrong >= ENROL_TRIES:
            self._drop()
            return f"wrong enrol code, {wrong} tries: it is burnt, {again}"
        _write_private(self.enrol, json.dumps({**rec, "wrong": wrong}) + "\n")
        return f"wrong enrol code ({ENROL_TRIES - wrong} tries left)"


def _enrol_hash(code: str, salt: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt) + code.encode()).hexdigest()


def _enrol_norm(code) -> str:
    """What was typed, as the code is printed: upper case, no spaces or dashes."""
    return "".join(str(code or "").split()).replace("-", "").upper()


def offer(mine: list, rp_id: str, name: str) -> tuple[dict, bytes]:
    """(what the page needs before the tap, the challenge bytes). Login when this
    RP has a passkey, else enrol: a registration that the page must send with the
    one-time code. Never "register" (no trust-on-first-use). From settings
    webauth.py:156-167. `name` labels an enrol's user record (user_record)."""
    c = secrets.token_bytes(32)
    out = {"type": "auth", "challenge": b64u(c), "rpId": rp_id, "allow": list(mine),
           "mode": "login" if mine else "enrol"}
    if out["mode"] == "enrol":
        out["user"] = {"id": b64u(secrets.token_bytes(16)), **user_record(name)}
    return out, c


def _client_data(cdj: bytes, kind: str, chal: bytes | None, origin: str) -> str:
    """'' if clientDataJSON is right for this ceremony, else why not.
    settings webauth.py:170-182, plus the crossOrigin refusal."""
    try:
        cd = json.loads(cdj)
    except ValueError:
        return "clientDataJSON is not JSON"
    if not isinstance(cd, dict):
        return "clientDataJSON is not an object"
    if cd.get("type") != kind:
        return f"type {cd.get('type')!r}, wanted {kind!r}"
    try:
        theirs = unb64u(str(cd.get("challenge", "")))
    except ValueError:
        return "challenge does not match"
    if not chal or not hmac.compare_digest(theirs, chal):
        return "challenge does not match"
    if cd.get("origin") != origin:
        return f"origin {cd.get('origin')!r}, wanted {origin!r}"
    # Refuse a ceremony made inside a frame. The browser sets crossOrigin true
    # when the page that ran it was embedded in another origin's page; the call
    # page is only ever opened top-level (and sends frame-ancestors 'none').
    # False or absent passes as before; any other value fails closed. Reverse
    # this only if the call page is ever embedded.
    cross = cd.get("crossOrigin")
    if cross is True:
        return "crossOrigin is true: signed in inside a frame, open the call in its own tab"
    if cross is not None and cross is not False:
        return f"crossOrigin {cross!r} is not a boolean"
    return ""


def _auth_data(ad: bytes, rp_id: str) -> str:
    """settings webauth.py:185-192."""
    if len(ad) < 37:
        return "authenticatorData too short"
    if ad[:32] != hashlib.sha256(rp_id.encode()).digest():
        return f"rpIdHash is not {rp_id}'s"
    if not (ad[32] & UP and ad[32] & UV):
        return "no user presence and verification (Touch ID, Face ID or PIN)"
    return ""


def register(msg: dict, chal: bytes | None, origin: str) -> tuple[dict | None, str]:
    """(credential, '') when the new passkey checks out, else (None, why).
    settings webauth.py:195-213, plus: the key's type must match its algorithm."""
    rp_id = rp_of(origin)
    try:
        cdj, ad = unb64u(msg["clientDataJSON"]), unb64u(msg["authenticatorData"])
        spki, alg, cid = unb64u(msg["publicKey"]), int(msg["alg"]), str(msg["id"])
    except (KeyError, TypeError, ValueError) as x:
        return None, f"malformed: {type(x).__name__}"
    why = _client_data(cdj, "webauthn.create", chal, origin) or _auth_data(ad, rp_id)
    if why:
        return None, why
    if alg not in (ES256, RS256):
        return None, f"algorithm {alg} not supported"
    try:
        pub = serialization.load_der_public_key(spki)
    except (ValueError, TypeError):
        return None, "public key does not load"
    want = ec.EllipticCurvePublicKey if alg == ES256 else rsa.RSAPublicKey
    if not isinstance(pub, want):
        return None, f"public key is not a {'P-256' if alg == ES256 else 'RSA'} key"
    if not cid:
        return None, "malformed: no id"
    return {"id": cid, "rp_id": rp_id, "spki": b64u(spki), "alg": alg,
            "added": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "name": str(msg.get("name") or "")[:80]}, ""


def login(msg: dict, chal: bytes | None, origin: str, creds: list) -> str:
    """'' when the assertion is good, else why not. settings webauth.py:216-239."""
    rp_id = rp_of(origin)
    try:
        cdj, ad, sig = unb64u(msg["clientDataJSON"]), unb64u(msg["authenticatorData"]), unb64u(msg["signature"])
        cid = msg["id"]
    except (KeyError, TypeError, ValueError) as x:
        return f"malformed: {type(x).__name__}"
    cred = next((c for c in creds if c.get("id") == cid and c.get("rp_id") == rp_id), None)
    if cred is None:
        return "unknown passkey"
    why = _client_data(cdj, "webauthn.get", chal, origin) or _auth_data(ad, rp_id)
    if why:
        return why
    pub = serialization.load_der_public_key(unb64u(cred["spki"]))
    signed = ad + hashlib.sha256(cdj).digest()
    try:
        if cred["alg"] == ES256:
            pub.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        else:
            pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, TypeError, ValueError):
        return "signature does not verify"
    return ""

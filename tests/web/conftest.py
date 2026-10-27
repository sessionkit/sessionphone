"""Rigs for `sessionphone web` (PHONE-133). Every later web slice inherits them.

THE SCRATCH-ROOT GUARD (design §4, Test guard). tests/conftest.py only DELETES
SESSIONPHONE_ROOT; it sets no scratch root. So without this file, a test here
resolves `paths.root()` to the REAL default root, where a host's live call-ears
keeper and the mouth look for `call-ears.sock` / `call-mouth.sock` (sv
keeper/call-ears globs <root>/call-ears.sock and <root>/*/call-ears.sock). A
socket there is a live seat's ears within seconds. Hence, for every test here:

- `SESSIONPHONE_ROOT` is a fresh tmp dir (autouse);
- the real default roots, ~/.sessionkit/phone/state and the old ~/.sessionphone
  (a keeper scans both), are checked for leg sockets before and after, and a
  NEW one fails the test that made it (autouse).

Also a SOFTWARE PASSKEY: a P-256 (or RSA) key from `cryptography` that makes
real clientDataJSON, authenticatorData and signatures, as a browser would.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import pathlib
import pwd
import secrets

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from sessionphone import paths
from sessionphone.web.passkeys import ES256, RS256, UP, UV, b64u, rp_of

REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir        # not $HOME: a test may point that elsewhere
# both default roots, as paths spells them: the new one and the old one
REAL_ROOTS = tuple(os.path.join(REAL_HOME, *pathlib.PurePosixPath(r).parts[1:])
                   for r in (paths.DEFAULT_ROOT, paths.OLD_ROOT))


def real_leg_sockets() -> set[str]:
    """Every leg socket under the REAL default roots, where the keeper looks."""
    out = set()
    for root in REAL_ROOTS:
        for pat in ("call-*.sock", "*/call-*.sock"):
            out.update(glob.glob(os.path.join(glob.escape(root), pat)))
    return out


@pytest.fixture(autouse=True)
def scratch_root(tmp_path, monkeypatch):
    """SESSIONPHONE_ROOT is scratch for every web test; the real root gains no socket."""
    root = tmp_path / "sp-root"
    monkeypatch.setenv("SESSIONPHONE_ROOT", str(root))
    before = real_leg_sockets()
    yield root
    new = real_leg_sockets() - before
    if new:
        pytest.fail(f"a web test made a leg socket under the REAL root, where the live keeper looks: {sorted(new)}")


@pytest.fixture(autouse=True)
def no_tailscale(monkeypatch):
    """No web test runs `tailscale`: `server.tailnet_name` (read when `run()` and
    `web enrol` run, PHONE-136) answers '' unless the test sets a name."""
    from sessionphone.web import server
    monkeypatch.setattr(server, "tailnet_name", lambda: "")


ABSENT = object()   # SoftKey(cross=ABSENT): no crossOrigin key in clientDataJSON


class SoftKey:
    """A passkey in software. `create()` answers an enrol offer, `get()` a login."""

    def __init__(self, origin: str, alg: int = ES256):
        self.origin, self.rp, self.alg = origin, rp_of(origin), alg
        self.key = (ec.generate_private_key(ec.SECP256R1()) if alg == ES256
                    else rsa.generate_private_key(public_exponent=65537, key_size=2048))
        self.id = b64u(secrets.token_bytes(16))
        self.count = 0

    def spki(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)

    def _ad(self, rp: str | None, flags: int) -> bytes:
        self.count += 1
        return hashlib.sha256((rp if rp is not None else self.rp).encode()).digest() \
            + bytes([flags]) + self.count.to_bytes(4, "big")

    def _cdj(self, kind: str, challenge: str, origin: str | None, cross=ABSENT) -> bytes:
        cd = {"type": kind, "challenge": challenge, "origin": origin if origin is not None else self.origin}
        if cross is not ABSENT:          # a browser's crossOrigin: True when run inside a frame
            cd["crossOrigin"] = cross
        return json.dumps(cd).encode()

    def create(self, challenge: str, *, kind="webauthn.create", origin=None, rp=None,
               flags=UP | UV, alg=None, spki=None, cross=ABSENT, **extra) -> dict:
        """What the page sends for an enrol: auth-finish's credential fields."""
        return {"id": self.id, "clientDataJSON": b64u(self._cdj(kind, challenge, origin, cross)),
                "authenticatorData": b64u(self._ad(rp, flags)),
                "publicKey": b64u(spki if spki is not None else self.spki()),
                "alg": alg if alg is not None else self.alg, **extra}

    def get(self, challenge: str, *, kind="webauthn.get", origin=None, rp=None, flags=UP | UV,
            cid=None, tamper=False, cross=ABSENT, **extra) -> dict:
        """What the page sends for a login: an assertion over the challenge."""
        cdj, ad = self._cdj(kind, challenge, origin, cross), self._ad(rp, flags)
        signed = ad + hashlib.sha256(cdj).digest()
        if self.alg == RS256:
            sig = self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            sig = self.key.sign(signed, ec.ECDSA(hashes.SHA256()))
        if tamper:
            sig = sig[:-1] + bytes([sig[-1] ^ 0x01])
        return {"id": cid or self.id, "clientDataJSON": b64u(cdj), "authenticatorData": b64u(ad),
                "signature": b64u(sig), **extra}


@pytest.fixture
def softkey():
    """SoftKey(origin, alg=ES256): a software passkey for that origin."""
    return SoftKey

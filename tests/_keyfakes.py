"""Fakes for the key-fingerprint tests: Delta Chat contacts that hold a
key, built from the public fixtures in tests/fixtures/openpgp/. No real
account, server or relay."""
from __future__ import annotations

import pathlib
import types

FIX = pathlib.Path(__file__).parent / "fixtures" / "openpgp"


def b64(name: str) -> str:
    return (FIX / name).read_text().strip()


V4 = b64("rfc9580-a1-v4-packet.b64")
V4_OLD = bytes.fromhex((FIX / "rfc9580-a1-v4-packet-old-format.hex").read_text().strip())
V6 = b64("rfc9580-a3-v6-certificate.b64")
K1 = b64("test-v4-key-1.b64")
K2 = b64("test-v4-key-2.b64")

FPR_V4 = "C959BDBAFA32A2F89A153B678CFDE12197965A9A"
FPR_V6 = "CB186C4F0609A697E4D52DFA6C722B0C1F1E27C18A56708F6525EC27BAD9ACC9"
FPR_K1 = "753E8B231F10DDFB82D742DE9D43B582A752BA4C"
FPR_K2 = "1A6302103BABBD68294D5F4EC5BB94C86CF1C259"


def vcard(addr: str, key_b64: str | None, fold: bool = False) -> str:
    """A one-contact vCard as core writes it (make_vcard)."""
    lines = ["BEGIN:VCARD", "VERSION:4.0", f"EMAIL:{addr}", "FN:Someone"]
    if key_b64 is not None:
        key = f"KEY:data:application/pgp-keys;base64\\,{key_b64}"
        if fold:        # RFC 6350 3.2: 75-octet lines, continuation begins with a space
            key = "\r\n ".join(key[i:i + 75] for i in range(0, len(key), 75))
        lines.append(key)
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


class Contact:
    """A contact: an address, and the key it holds (None: an address contact)."""

    def __init__(self, cid: int, addr: str | None, key_b64: str | None = None,
                 key_contact: bool | None = None, card: str | None = None, vcard_error: Exception | None = None):
        self.id = cid
        self.addr = addr
        self.key_b64 = key_b64
        self.key_contact = key_b64 is not None if key_contact is None else key_contact
        self.card = card
        self.vcard_error = vcard_error
        self.vcards = 0

    def get_snapshot(self):
        return types.SimpleNamespace(address=self.addr, is_key_contact=self.key_contact,
                                     display_name="Someone", contact=self)

    def make_vcard(self) -> str:
        self.vcards += 1
        if self.vcard_error is not None:
            raise self.vcard_error
        return self.card if self.card is not None else vcard(self.addr or "", self.key_b64)


class Account:
    """An account: its contacts, and the call messages they sent (msg id -> contact)."""

    def __init__(self, contacts=(), calls=None, fail: bool = False):
        self.contacts = list(contacts)
        self.calls = dict(calls or {})
        self.fail = fail

    def get_message_by_id(self, msg_id):
        if self.fail:
            raise RuntimeError("rpc down")
        sender = self.calls[msg_id]
        return types.SimpleNamespace(get_snapshot=lambda: types.SimpleNamespace(sender=sender))

    def get_contacts(self, query=None):
        """As core 2.62 lists key contacts for a query: the address matched
        case-insensitively (and display names), LIKE-style, so the caller must
        filter for the exact address."""
        q = (query or "").lower()
        return [c for c in self.contacts if c.key_contact and q in (c.addr or "").lower()]

    def get_contact_by_id(self, cid):
        return next(c for c in self.contacts if c.id == cid)

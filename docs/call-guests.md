# Call guests: who is spoken as known on a call

A guest on a call is another agent or person heard by mail, not on the
phone line. The bridge speaks a guest's mail into the call, and decides
from a pinned key, or a registry cert the owner blessed, whether to speak
it as known. Anyone else is spoken in the plain voice and announced as
unknown. The [sessionphone skill][skill]'s "Guests heard on a call" keeps
the short form; this page has the whole rule.

## How a guest is heard

- A guest is heard by mailing the bridge, threaded to the call's root; the
  call mail says how. The mail must answer a root the bridge issued for
  that chat: a root from before a bot restart is not spoken.

## Known guests: by pinned key, else by a blessed registry cert

`call-guests.txt` in the root holds one row per line, `address
[ssh-ed25519 AAAA...]`. Every row is copied on the call.

- An address with a pinned key is KNOWN only for mail that `message
  verify` passes with one of its keys. A mismatch is unknown, and a pin
  never falls back to the registry.
- An address with no pinned key, on an address-only row or keyed in
  `voices.json` by its local part, is KNOWN only when `message verify
  --trust --require-blessing` exits 0 with a last line of `TRUST: FULL` (a
  registry cert the owner blessed; UNBLESSED never counts), and its one
  `registry-cert` line reads `ok` with an `agent:` equal to the whole From
  local part.
- With no sessionmail registry on the host, that rung knows no one: the bot
  still starts, and logs `registry trust is OFF` once. A plus-address seat
  is never known by the registry until a later sessionmail release.
- Anyone else, whatever their `X-Voice:` or `voices.json` entry, is spoken
  in the plain voice, announced as unknown, and posted to the chat as
  `(unverified sender)`.

## Pinning a key

- **To pin a key:** run `message verify` on a mail the guest signed and
  copy its `signer-key` line after their address. One address may have
  several rows, one key each. Pin only on the owner's word, as with
  callers.
- An agent's key is per session: re-pin after that agent restarts, or
  leave its row address-only for the registry to vouch for. An agent's
  mailed replies stay unverified until a later sessionmail release signs
  call mails; `dc-say` and the mouth are unaffected.

[skill]: ../skills/sessionphone/SKILL.md

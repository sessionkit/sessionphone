# OpenPGP public keys for the key-fingerprint tests

Public keys only. No secret key is kept here, and none was kept anywhere.

| file | what | fingerprint |
|---|---|---|
| `rfc9580-a1-v4-packet.b64` | RFC 9580 Appendix A.1: the sample v4 Ed25519 Public-Key packet (new-format header) | `C959BDBAFA32A2F89A153B678CFDE12197965A9A` |
| `rfc9580-a1-v4-packet-old-format.hex` | the same packet with an old-format header (`0x98`), hex | `C959BDBAFA32A2F89A153B678CFDE12197965A9A` |
| `rfc9580-a3-v6-certificate.b64` | RFC 9580 Appendix A.3: the sample v6 certificate (transferable public key) | `CB186C4F0609A697E4D52DFA6C722B0C1F1E27C18A56708F6525EC27BAD9ACC9` |
| `test-v4-key-1.b64` | a throwaway v4 key made for these tests, user ID `dave1 <dave@example.org>` | `753E8B231F10DDFB82D742DE9D43B582A752BA4C` |
| `test-v4-key-2.b64` | a second throwaway v4 key, same address, user ID `dave2 <dave@example.org>` | `1A6302103BABBD68294D5F4EC5BB94C86CF1C259` |

The RFC fingerprints are the ones RFC 9580 publishes. The two test keys were
made with gpg for this purpose; their keyring was deleted, so only these
public halves exist.

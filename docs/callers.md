# Callers: who may ring the bot

The bot answers a call only from a contact that holds the key pinned to a
line of `callers.txt`. The owner is first, and every other caller is added
only on the owner's word. An empty list means nobody may ring: it fails
closed. The [sessionphone skill][skill] keeps the three verbs; this page
has the rest.

```bash
sessionphone callers                       # the list; the owner is first
sessionphone callers add ADDR --key FPR    # only on the owner's word
sessionphone callers pin ADDR --key FPR    # bind a listed line to a key
```

## Which file and account the verbs use

These verbs, and `link`, use the callers file and account the bot uses,
from your shell's environment: for a bot run with `DC_PROFILE`, `DC_ROOT`,
`DC_CALLERS_FILE` or `DC_ACCOUNTS`, put the same settings in front of the
command. Each names the file it touched (`callers: ...` or `in ...`), and
the account it opened (`account: ...`).

## A caller is a key, not an address

- The bot answers a call only from a contact that holds the key pinned to a
  line of this list (`ADDR  key:FPR`); the address is the label. A line
  with no key is refused until it is pinned.
- FPR is what the owner's phone shows under that person's profile,
  Encryption: take it from the owner, never from the person who asks to
  ring.
- Without `--key` the verb finds the one key contact with that address,
  which needs the bot stopped; it prints the fingerprint to compare, and
  writes nothing when there are none or several.
- When the log says `AMBIGUOUS`, two keys write from that address: the bot
  pins neither, so ask the owner which key is theirs and pin that one.
- The first start after an upgrade pins the owner line once and keeps the
  old file as `callers.txt.pre-keys`.
- [README.md][readme] has the whole contract. An empty list means nobody:
  it fails closed. Anyone else who scans your invite can chat, but can't
  ring you until the owner approves them.

[skill]: ../skills/sessionphone/SKILL.md
[readme]: ../README.md

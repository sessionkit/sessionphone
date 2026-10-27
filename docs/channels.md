# Channels: where the bot may send

The bot sends only to chats listed in `channels.txt`, the owner's list. A
chat not listed is refused and nothing is sent. Never edit the list to let
yourself send. The [sessionphone skill][skill] keeps the short form; this
page has the rest.

- **The list.** The bot sends only to chats on `channels.txt` (`NAME
  TARGET  # note`; TARGET is an address, that contact's 1:1, or `chat:N`).
- **An address line is bound to a key** (`NAME ADDR key:FPR`): it passes
  only while the chat's contact holds that key, and a line with no key is
  refused until `sessionphone channels pin NAME --key FPR` pins one.
- **Every `dc-*` tool takes a NAME** where it takes a chat (`dc-say owner
  'hi'`).
- **A chat not listed is REFUSED:** exit 3, the list's path on stderr,
  nothing sent (exit 4 = the bot took the job and never confirmed it).
- `sessionphone channels add NAME TARGET` lists one.
- A missing or loose list refuses every send. Do not edit it to let
  yourself send: it is the owner's list. [README.md][readme] has the whole
  contract.

## Taking a message back

`dc-delete MSG_ID...` takes back your own message (the `msg M` your send
printed; all ids in one chat). It only ASKS each member's app to delete it:
it cannot unsend what was read, and any time limit is unmeasured.

[skill]: ../skills/sessionphone/SKILL.md
[readme]: ../README.md

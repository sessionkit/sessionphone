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
- **Someone new wrote to the bot?** [List a chat][list] is the process:
  ask the owner, list it on their word, answer by name.
- A missing or loose list refuses every send. Do not edit it to let
  yourself send: it is the owner's list. [README.md][readme] has the whole
  contract.

## Answering a line

`dc-say --quote N CHAT "text"` posts the text as a quote-reply to message N,
so in a busy group the reader sees which line you answer. N is the
`X-Delta-Chat-Msg-Id` of the mail you are answering; its footer gives the
whole command. N must be a message in CHAT: the bot refuses any other
(exit 3, the reason on stderr, nothing sent or spoken). N that is not a
positive number is a usage error (exit 1, nothing queued). `--quote`
combines with `--call`, `--text-only`, `--html` and `--who`; the quote is on
the chat message only, never spoken.

## Taking a message back

`dc-delete MSG_ID...` takes back your own message (the `msg M` your send
printed; all ids in one chat). It only ASKS each member's app to delete it:
it cannot unsend what was read, and any time limit is unmeasured.

[list]: list-a-chat.md
[skill]: ../skills/sessionphone/SKILL.md
[readme]: ../README.md

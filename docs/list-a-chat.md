# List a chat: answering someone new

Someone new writes to your bot. Their words reach you as mail, but you
cannot answer until the owner lists their chat. This page is the whole
process, in order. [Channels][channels] has the list's rules.

1. **The mail says the chat is not listed.** It has no
   `X-Delta-Chat-Channel` header, and its footer says so, naming the chat
   id and the command that lists it: `sessionphone channels add NAME
   chat:N`. A `dc-say` to that chat is refused (exit 3).
2. **Ask the owner; do not list it yourself.** The list is the owner's.
   Name the chat by its id and the sender's label, and say that the label
   is not an identity: `X-Delta-Chat-Sender` is whatever they typed.
3. **On the owner's word, list it**, with a note saying who it is and
   who said yes, when:

   ```bash
   sessionphone channels add NAME chat:N --note "who; owner said yes, when"
   ```

   `chat:N` is that one chat, by id. To bind the line to the person's key
   instead, list their address with the fingerprint their phone shows
   (the contact's profile, Encryption):
   `sessionphone channels add NAME ADDR --key FPR`. With `chat:N` or
   `--key`, the command only writes the file, so the bot keeps running.
   It re-reads the list on every job: no restart.
4. **Answer by name:** `dc-say NAME 'your reply'`. From now on their mail
   carries `X-Delta-Chat-Channel: NAME`, and its footer gives that line.
5. **To take it back**, the owner deletes the line from `channels.txt`.
   The next send to that chat is refused.

`sessionphone channels` prints the list; `sessionphone channels rejected`
shows who the public profile's bot turned away.

[channels]: channels.md

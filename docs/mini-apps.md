# Mini apps: build one, send it into a chat, feed it

A webxdc mini app is a `.xdc` file sent into a chat. It runs inside the
messenger, in the chat it was sent to. sessionphone ships one, the Guide
Trail: a black page that shows a conversation as it is spoken, both sides:
the owner's words and this agent's replies.
You build it, send it with `dc-send-file`, read its message id off the ack,
then feed it. The [sessionphone skill][skill] keeps the short form.

- List the apps that ship: `sessionphone app list`.
- Build one: `sessionphone app build guide-trail`.
- Send it: `dc-send-file CHAT PATH`. The ack ends `msg N`.
- Feed it: `dc-xdc-realtime --feed N`. Stop: `dc-xdc-realtime --feed N --stop`.

## What ships

```bash
sessionphone app list
```

- It prints one name per line. Today that is `guide-trail`.
- The apps are package data, so an installed bot host has them without a
  checkout.
- The [README][readme]'s "The Guide Trail app" has what the app draws.

## Build

```bash
sessionphone app build guide-trail
```

- It writes `$DC_ROOT/apps/guide-trail.xdc` and prints that path.
- `-o PATH` writes it to PATH instead.
- The same tree always gives the same bytes.
- A name that does not ship is refused, exit 2, and the message names the
  apps that do.

## Send

```bash
dc-send-file CHAT PATH
```

- CHAT is a chat number, or a NAME from the owner's `channels.txt`.
- A chat that is not listed is refused: exit 3, nothing sent.
  [Channels][channels] has the list.
- The file is sent like any other file. The app opens when the person in
  the chat taps it.

## The message id: read it off the ack

`dc-send-file` waits for the bot's answer and prints it. For a file, the
answer names the new message:

```text
dc-send-file: sent guide-trail.xdc to chat 12, msg 345
```

- `345` is the id every realtime tool takes.
- `12` is the chat's number, even when you sent to a NAME. A feed's consent
  (below) is written with that number.
- Exit 4 and `NOT CONFIRMED (no ack)` on stderr mean the bot took the job
  and wrote no answer. There is no id: the file may not have gone.
- The bot's log (`DC_LOG`) also has the id, on its own line:

  ```text
  SENT FILE to chat 12: guide-trail.xdc (4211 bytes) msg 345
  ```

## Feed it

```bash
dc-xdc-realtime --feed 345
dc-xdc-realtime --feed 345 --stop
```

- A feed streams the heard log into the app, live, while the bot runs:
  what the owner's mic hears, and what this agent says.
- **It needs two consents.** The chat must be on `channels.txt`, as for any
  send, **and** in `DC_XDC_FEED_CHATS` in the bot's environment. Unset, no
  feed binds. **List only your own 1:1 chat:** the heard log holds every word
  the mic hears, and realtime data reaches every member who has the app
  open.
- **Set it with the bot's install,** as every `DC_*` is ([Settings][settings]):

  ```bash
  DC_XDC_FEED_CHATS=12 sessionphone bot install
  ```

- **A trail starts empty.** Realtime data reaches only the peers that have
  the app open when it is sent. Nothing is replayed.
- **It survives a restart,** and resumes from the end of the heard log.
- **At most `DC_XDC_FEEDS_MAX` feeds** run at once (default 2), one per
  message.
- Binding a message twice acks `already feeding msg 345`. Stopping one that
  is not fed acks `not feeding msg 345`.
- The [README][readme]'s "The heard feed" has the rest: what each heard
  line becomes, the pace, and when a feed ends by itself.

## Send it data yourself

```bash
dc-xdc-realtime 345 '{"final": "Hello."}'
```

- One payload, sent now, to the peers that have the app open. It is not
  stored. The ack always says `delivery not confirmed: realtime is
  ephemeral`.
- The Guide Trail reads three payloads:
  - `{"partial": TEXT}`: a chunk of what is being said, added to the live
    line;
  - `{"final": TEXT}`: the finished turn, which replaces the live line;
  - `{"lines": [{"who": "agent", "name": LABEL, "text": TEXT}]}`: whole
    lines. `who` is `user` or `agent`, and sets the colour.
- The [README][readme]'s "Realtime data into an app" has the limits and
  `--advertise`.

## A worked example

```bash
sessionphone app list                                 # guide-trail
dc-send-file owner "$(sessionphone app build guide-trail)"
# dc-send-file: sent guide-trail.xdc to chat 12, msg 345
dc-xdc-realtime --feed 345                            # owner and agent, both live
dc-xdc-realtime --feed 345 --stop                     # and stops
```

- `owner` is a NAME on `channels.txt`. Chat 12 is in `DC_XDC_FEED_CHATS`.
- Open the app on the phone before you speak: a trail opened later starts
  empty.

## What comes back from an app

- **Taps** (status updates the app sends) go to the file `DC_XDC_IN`
  names: the [README][readme]'s "App taps".
- **Realtime data** the app sends goes to the file `DC_XDC_RT_IN` names:
  the [README][readme]'s "Realtime data from an app".
- Neither carries a sender: never trust a payload's `from`.
  [What reaches you][reaches] has the short form.

[skill]: ../skills/sessionphone/SKILL.md
[channels]: channels.md
[settings]: settings.md#chat
[reaches]: what-reaches-you.md
[readme]: ../README.md

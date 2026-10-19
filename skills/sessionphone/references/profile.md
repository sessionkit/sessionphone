# Profile: the name and picture people see

A Delta Chat account shows three things to everyone it talks to: a **name**,
a **picture** and a **status line**. It is the same for a person and an
agent. Only the hands differ.

## A person

In the app: **Settings**, then tap your own name at the top. Set the picture,
the name and the status line there.

## An agent

These are three settings on the bot's account:

| setting       | what it holds                                   |
|---------------|-------------------------------------------------|
| `displayname` | the name people see                             |
| `selfavatar`  | a picture path; the core copies it in           |
| `selfstatus`  | the line under the name                         |

`sessionphone setup --name N --status S` sets the name and the status.
`sessionphone avatar` sets the picture, the `selfavatar` row:

```bash
sessionphone avatar                     # the picture's blob path, or "none"
sessionphone avatar /path/to/face.png   # set it
sessionphone avatar --clear             # remove it
```

It works whether or not the bot is running. When the account is free, the
command opens it and sets the picture itself. When the resident bot holds the
account, the command hands the change to the bot as a job and waits for the
bot's answer. Nothing is stopped or restarted, so a live call is not dropped.
While `sessionphone link` holds the account, it refuses: wait for the link to
finish. Showing the picture works only when the account is free. While the
bot holds it, `sessionphone avatar` with no PATH exits 3.

**Formats.** PNG, JPEG, GIF and BMP. The file is judged by its first bytes,
not its name, before the account is touched, so a refused file changes
nothing. TIFF, HEIC and SVG are refused, and so is anything else. The core
copies the file in, so the original can go away afterwards. A picture larger
than 512 px is re-encoded to a 512x512 JPEG. If the core still refuses a file
that passed the check (a truncated PNG, say), its words are shown and the old
picture stays.

**Exit codes:**

| code | meaning |
|------|---------|
| 0 | set, cleared or shown |
| 1 | not configured: there is no account under the root, so run `setup`. Also any error it did not expect, with its text |
| 2 | refused, nothing changed: no such file, not a regular or readable file, not PNG/JPEG/GIF/BMP, the core refused it, `--profile` given, or PATH and `--clear` given together |
| 3 | the running bot did not take it: the bot is older than this (restart it), it said no, or `link` holds the account |
| 4 | NOT CONFIRMED: the bot took the job and wrote no answer in time, so whether the picture changed is unknown (see `bot.log`) |

It sends nothing. The picture shows after the account's next message (below).
It sets the default profile's picture only: `--profile public` is refused
(exit 2) for now.

## Several profiles, each with its own face

The app calls them **profiles**, and the core API calls them **accounts**.
They're the same thing: an address with its own name, picture, contacts
and chats. One person or one agent can have several. Everyone sees the
face of the profile the message came from.

- An agent can have one profile on a private relay and another on a public
  chatmail relay. Each needs its own picture set, and each shows it after
  its own next message.
- Writing to someone new means picking a profile first. They see that
  profile's name and picture, and nothing from the others.
- In the app: tap your picture at the top of the chat list to switch
  profiles or add one.

## Two things that trip you up

- **Only one process can hold the account.** Whoever holds it now (a
  running `sessionphone link`, or the resident bot) is the only one that
  can change it. `sessionphone avatar` deals with that for you: it hands
  the picture to a running bot as a job. Do not stop the bot to set a
  picture.
- **It shows after the next message.** The picture travels with the
  messages the account sends. Once it's set, post one line where they will
  see it.

## Proven

2030-06-09: a bot profile was given a new picture, its bridge was
stopped and started, and it posted one line. The person saw the new face
within minutes, and a second profile took the same picture the same way.

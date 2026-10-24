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

**Another agent's bot.** `DC_AGENT` picks the bot, as `bot install` picks it,
and the command acts on the root that bot's install kept (in
`~/.sessionkit/phone/settings/phone.json`):

```bash
DC_AGENT=<agent> sessionphone avatar /path/to/face.png
DC_AGENT=<agent> DC_NAME='Name' sessionphone bot install   # its name (below)
```

Without `DC_AGENT` the bot is this session's, else the one bot the file
records, else `claude`. With two or more recorded and none named, the command
refuses (exit 1) and changes nothing, so name one, `claude` included.
`SESSIONPHONE_ROOT=DIR` points at a root directly, and wins over the file.

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

**The default picture.** sessionphone has a slot for a default picture,
`images/default-avatar.png` (or `.jpg`) inside the package. None ships yet.
When one does, the bot sets it at its start, the one time it holds the
account and can see the picture there. It does that only when the account has
no picture and the user has not chosen none:

| the account at the bot's start            | what the bot does | its `bot.log` line |
|-------------------------------------------|-------------------|--------------------|
| no picture                                | sets the default  | `picture: default set (default-avatar.png)` |
| a picture (yours, or the default already) | nothing           | `picture: kept (the account has one)` |
| cleared with `sessionphone avatar --clear` | nothing          | `picture: none (cleared with ...)` |
| no default shipped                        | nothing           | (no line) |

`sessionphone avatar --clear` leaves a file, `avatar-none`, under the root.
That is how the bot knows the empty picture was a choice. `sessionphone
avatar PATH` removes the file. `bot install` prints one `picture:` line
saying this when a default ships, and none when nothing ships.

**The name follows the agent.** The bot sets its name at its start: `DC_NAME`
when it is set, else the agent's name, title-cased (`versa` -> `Versa`). The
agent's name goes only over a name nobody chose: an empty one, or the name of
the agent the bot is or replaced with `bot install --replace`. Any other name,
such as `setup --name` gave it, is the user's and is kept.

```bash
DC_NAME='Ada Lovelace' sessionphone bot install   # give the bot this name
```

| the name at the bot's start                      | what the bot does | its `bot.log` line |
|--------------------------------------------------|-------------------|--------------------|
| `DC_NAME` is set and differs                     | sets `DC_NAME`    | `name: 'Claude' -> 'Ada' (DC_NAME); ...` |
| empty, or the name of the agent it is or replaced | sets the agent's name | `name: 'Claude' -> 'Versa' (the agent's name); ...` |
| the agent's name already                         | nothing           | (no line) |
| any other name                                   | keeps it          | `name: kept 'Ada Lovelace', ...; to change it: DC_NAME=...` |

When the name changes, the bot writes the saved invite (`invite.txt` under the
root) again: the invite carries the name, and `invite` and `qr` read it.
`bot install --replace OLD` leaves the old agent's name in `name-replaced`
under the root for the bot's next start, which removes it. `bot install`
prints one `name:` line saying what the bot will do.

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

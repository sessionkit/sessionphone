# Configuration

What sessionphone configures, and where each piece lives.

- **Everything lives under one root:** `~/.sessionphone`, or
  `$SESSIONPHONE_ROOT`. The bot can be pointed elsewhere with `DC_ROOT`.
  The root is mode 0700. `sessionphone setup` makes it, and every other verb
  refuses a root with a wider mode.
- **The bot reads `DC_*` environment variables.** `sessionphone bot install`
  copies every `DC_*` set in your shell into the bot's launchd job. To change
  one, set it and run `bot install` again.
- **`bot install` remembers them** in the phone tool's host settings file,
  `~/.sessionkit/phone/settings/phone.json`, one map per bot, so a later
  install from an empty shell installs the same job. See
  [Settings][settings].
- **A few plain files are re-read while the bot runs:** the callers file, the
  channels list, and the recording keys in `ears.json`. An edit to them takes
  effect without a restart.
- **There is no seat or agent override.** See [Settings][settings].

Each part has its own section:

- [The bot][the-bot]: the launchd job and the `DC_*` it carries
- [Agent mail][agent-mail]: how the bot signs and sends mail to its agent
- [Callers and channels][callers-and-channels]: who may ring, and where the bot may send
- [Call recording][call-recording]: `ears.json` and its `record_*` keys
- [The web call][the-web-call]: `sessionphone web` and its passkeys
- [Settings][settings-section]: every key, with its default

## The bot

The bot is the resident process that holds the Delta Chat account. It runs as
your launchd job.

| What | Where |
| --- | --- |
| the job's plist, mode 0600 | `~/Library/LaunchAgents/dev.sessionkit.phone.bot.<agent>.plist` |
| the job's label | `dev.sessionkit.phone.bot.<agent>`, in `gui/<uid>` |
| the account and keys | `<root>/accounts/` |
| the outbox, where the `dc-*` tools leave jobs | `<root>/outbox/` (`DC_OUTBOX`) |
| the invite link `setup` saved | `<root>/invite.txt` |

`<agent>` is `DC_AGENT`, else `CLAUDE_CODE_AGENT`, else the one bot
`~/.sessionkit/phone/settings/phone.json` records, else `claude`, in lower
case.

**What the job carries.** `bot install` writes the plist with this
environment:

- `PATH`: a fixed, short path (`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`)
- `SESSIONPHONE_ROOT`: the root the installing shell had, else the one
  `phone.json` saved for this bot
- `PYTHONUNBUFFERED=1`
- every `DC_*` in the installing shell that is not empty, and every one
  `phone.json` saved for this bot that the shell does not set
- `DC_AGENT`, always set to the agent's name
- `DC_EARS_SRC`, when ears is installed as a uv tool and your shell does not
  set it
- `DC_MESSAGE`, pinned to the `message` on your shell's `PATH` when your shell
  does not set it
- `DC_SIGNER_SOCK` and `DC_SIGNER_PUB`: yours when your shell sets (or
  `phone.json` saves) both; else the `SEAT_SOCKET` and `SEAT_PUB` that
  `message seat agent --install <agent>-chat` prints when install runs the
  seat's agent (see [Agent mail][agent-mail])
- sessionmail's mail-route settings, each one your shell sets:
  `SESSIONMAIL_SENDMAIL`, `SESSIONMAIL_RELAY_DOMAIN`, `SESSIONMAIL_HOME`,
  `SESSIONMAIL_ROOT` and `SESSIONMAIL_HOST` (see [Agent mail][agent-mail]).
  `SESSIONMAIL_ORIGIN` is not carried: set it in sessionmail's settings key
  `postfix.origin`, which the bot and its reader both read

**What install saves.** Once the job is loaded, `bot install` saves each
`DC_*` and `SESSIONPHONE_ROOT` the shell gave in
`~/.sessionkit/phone/settings/phone.json`, under `bots.<agent>`: never
`DC_AGENT`, `DC_CALLERS`, a pinned `DC_MESSAGE`, a found `DC_EARS_SRC`, a
`DC_SIGNER_*` that `message seat agent --install` reported, or the mail-route
settings, which are sessionmail's own (its `mail.json`).
`--forget KEY` drops a saved one. Its `settings:` block names each value the
job carries and where it came from. The rules are in
[Settings][settings].

Nothing else in your shell reaches the bot. `EARS_*` is not carried, so a
bot under launchd reads recording from `ears.json` alone (see
[Call recording][call-recording]). Nor is any other `SESSIONMAIL_*` (the
installing seat's own, such as `SESSIONMAIL_FROM_NAME`), or the old
`SESSION_MAIL_*` spelling.

**When it is read.** The bot reads its `DC_*` once, when it starts. `bot
install` replaces the job and starts it again, so that is how a change takes
effect. Run it again after a plugin update as well.

**What install refuses.** It writes nothing, and exits 2, when:

- the root is missing, or its mode is wider than 0700;
- `DC_HOST` is unset and sessionmail's host for this machine is empty or
  `localhost`;
- only one of `DC_SIGNER_SOCK` and `DC_SIGNER_PUB` is set; or the bot
  signs (an MTA, or `--registry`), neither is set, and this sessionmail's
  `message seat agent --help` has no `Capability: install` line: the message names the steps that make the seat by hand,
  and says what this sessionmail lacks;
- install ran `message seat agent --install` and it refused: the seat is not
  minted yet (the message names `message seat init --registry DIR
  <agent>-chat`), or another refusal, quoted as sessionmail wrote it. When it
  fails instead (exit 1), install exits 1, writing nothing of the bot, and
  names the recovery: `message seat agent --uninstall <agent>-chat`, then
  install again;
- the bot's own start check would refuse: no `message`, or a key minted for
  another seat;
- `DC_STT_URL` names no model and `DC_STT_MODEL` is unset;
- nobody may ring the bot, or `DC_CALLERS` is set;
- the channels list is empty and there is no owner to seed it from;
- the environment lacks the media pack.

`sessionphone bot run` runs the same bot in the foreground, from your own
shell's environment.

## Agent mail

The bot mails its agent through sessionmail's `message send`, as its own
seat, `<agent>-chat`. Whether it signs follows the carrier this bot's own
`message send` will use ([Bot mail signing][bot-mail-signing]):

- **On the mail drop with no key, it does not sign.** It writes into the box
  itself, and `bot install` needs no key, registry or seat step.
- **On an MTA it must sign**, and a send with no usable key is refused.
- **A key given or saved is always used**, on any carrier.

To give it a key, run `sessionphone bot install --registry DIR`. It mints
the seat into the seat registry DIR when the seat has no key yet
(`message seat init --registry DIR <agent>-chat`), runs `message seat agent
--install <agent>-chat`, which holds the key in an ssh-agent under launchd,
and saves the signer. The README's "The bot's seat" shows how to make DIR.
A sessionmail without `Capability: install` on that page needs `message
seat agent <agent>-chat SOCKET` under launchd by hand. These `DC_*` settings
point the bot at the seat:

| Setting | What it is |
| --- | --- |
| `DC_MESSAGE` | the `message` binary, an absolute path: launchd's `PATH` is short. Install pins it |
| `DC_SIGNER_SOCK` | the socket of the seat's key agent (`SEAT_SOCKET` from `message seat agent --install`, or your own `message seat agent`'s) |
| `DC_SIGNER_PUB` | the seat's `.pub` (`SEAT_PUB`), or a copy of it kept apart from the key |
| `DC_ADDR` | the bot's own From seat; its local part is the seat the key signs as. Not the agent's box |
| `DC_MAILTO` | where Delta text goes: the agent's box, `<agent>@<host>` by default |

The bot refuses to start when there is no `message` binary, a carrier
sessionmail refuses or cannot read, a carrier of a kind the bot does not
know (or a sessionmail it cannot ask which carrier it will use), no signer
when it signs, a key minted for another seat, or no Delta-text box it can make.

sessionmail picks how that mail travels, from its own settings, which `bot
install` carries into the job from your shell:

| Setting | What it is |
| --- | --- |
| `SESSIONMAIL_SENDMAIL` | the carrier. `drop` writes straight into this host's mailboxes, with no MTA; anything else names the MTA. Unset, sessionmail reads the `sendmail` key in its `mail.json` (never `carrier`), else uses its default, `drop` |
| `SESSIONMAIL_RELAY_DOMAIN` | the relay for general delivery |
| `SESSIONMAIL_HOME` | this host's seat-files root |
| `SESSIONMAIL_ROOT` | the maildir root the drop writes to |
| `SESSIONMAIL_HOST` | the sender's host, and the host of the bot's Message-IDs |

Whether postfix is installed does not pick the carrier: a job with no
`sendmail` setting uses the drop even on a Mac that has postfix. `bot
install`'s `mail:` line says what the job carried and which carrier the bot
will use, and the bot says it again at each start in `bot.log`.

Message-IDs are built on sessionmail's host: `SESSIONMAIL_HOST`, else
`SESSION_MAIL_HOST`, else this machine's short name. That is not `DC_HOST`.
The job carries `SESSIONMAIL_HOST` when your shell sets it, never
`SESSION_MAIL_HOST`.

Mail to the agent that fails to send waits in `<root>/undelivered/`
(`DC_UNDELIVERED`), and the bot retries it.

## Callers and channels

Two plain files, both in the root by default. Both are read again on each
use, so an edit takes effect at once.

| File | Default | Override | What it holds |
| --- | --- | --- | --- |
| callers | `<root>/callers.txt` | `DC_CALLERS_FILE` | who may ring the bot, one address per line |
| channels | `<root>/channels.txt` | `DC_CHANNELS_FILE` | where the bot may send, one `NAME TARGET` per line |

- `sessionphone link` writes the owner into the callers file;
  `sessionphone callers add` adds more.
- `sessionphone channels add` and `channels seed` write the channels list.
  Keep it 0600: the bot refuses a list that group or world can write.
- `DC_CALLERS` grants nothing. Callers come from the file only, and `bot
  install` refuses while `DC_CALLERS` is set.
- A bot run `--profile public` gates inbound too: it reads its own list
  (`<root>/public/channels.txt`), and logs each sender it rejects to
  `rejected.jsonl` beside that list.

Mail into a live call has two files of its own, in the root:

| File | Override | What it holds |
| --- | --- | --- |
| `call-guests.txt` | `DC_GUESTS` | guests who may mail into a call, with an optional pinned key |
| `voices.json` | `DC_VOICES` | which voice reads each sender's mail aloud |

## Call recording

Calls keep no audio unless recording is on. The switch is the one the ears
use, so one setting covers the microphone and the calls.

The file is `~/.sessionkit/voice/settings/ears.json`, or `$EARS_CONF`. It is
one JSON object shared with the other ears settings. The bot reads four keys
from it, and each can also be set in the environment. **A bot under launchd
sees none of these variables**: `bot install` does not carry `EARS_*`, so the
Environment column and `$EARS_CONF` work only for a bot run by hand, and a
bot under launchd reads `~/.sessionkit/voice/settings/ears.json`.

| Key | Environment | Default | What it does |
| --- | --- | --- | --- |
| `record` | `EARS_RECORD` | `false` | keep each call's audio |
| `record_days` | `EARS_RECORD_DAYS` | `7` | days kept audio is kept; `0` keeps it for good |
| `record_notice` | `EARS_RECORD_NOTICE` | `true` | post a line in the call's chat saying whether it is kept |
| `record_spoken_notice` | `EARS_RECORD_SPOKEN_NOTICE` | `true` | on a kept call, say so before the greeting |

- The file is followed while the bot runs: an edit takes effect on the next
  call.
- The environment beats the file, and the bot's `--record` or `--no-record`
  beats both. A bot under launchd has neither, so set these in `ears.json`.
- An invalid `record` fails closed: nothing is kept, and `bot.log` says why.

Each call gets its own directory under `<root>/calls/` (`DC_CALLS_DIR`).
The full rules are in the README's [Call recording][recording] section.

### Upgrading from 0.1.0

Up to 0.1.0 the ears settings file lived at `~/.settings/ears/ears.json`.
sessionphone no longer reads that file, and nothing falls back to it. The
kit's host step moves it to `~/.sessionkit/voice/settings/ears.json`.

While the old file is there and the new one is not, the file sets nothing,
and `bot.log` says so once in one line that names both paths. The bot
only checks that the old file exists: it never opens it. A bot run by hand
with `$EARS_CONF` set skips the check, because then you chose the file, while
a bot under launchd never sees `$EARS_CONF` and reads
`~/.sessionkit/voice/settings/ears.json`.

**The bot's launchd job has a new label.** 0.1.0 installed the bot as
`com.sessionphone.bot.<agent>`, in
`~/Library/LaunchAgents/com.sessionphone.bot.<agent>.plist`. It is now
`dev.sessionkit.phone.bot.<agent>`, in
`~/Library/LaunchAgents/dev.sessionkit.phone.bot.<agent>.plist`. Run
`sessionphone bot install` again after the upgrade: it retires the old job
for the same agent before it starts the new one, so two bots never hold one
account.

- **What install does to the old job.** If launchd has it loaded, install
  boots it out. If its plist exists, install removes it. Each act prints one
  line, `retired legacy launchd job com.sessionphone.bot.<agent> (booted out)`
  or `(plist removed)`. Only that one label, for the agent being installed,
  is touched; any other job is left alone.
- **Install refuses when the old job carries a setting the new one would
  lose.** Before it writes or boots out anything, install compares the old
  plist's `EnvironmentVariables` with the environment the new job would
  carry. If a key is dropped, or has a different value, install exits 2 and
  names each key, old value -> new value. `PATH` is the one exception,
  because install always writes it fresh. So an upgrade cannot silently move
  the bot's root, its calls folder, its ears code or its recording settings.
  - For a key install carries from your shell (`DC_*`,
    `SESSIONPHONE_ROOT`, sessionmail's mail-route `SESSIONMAIL_*`), run
    install again with the old value set in the shell.
  - `DC_AGENT` is the exception: install always writes the name it installs
    as, so the shell cannot keep an old one. If the old plist's `DC_AGENT`
    was edited to another name, install says so. To keep that name, run
    `sessionphone bot uninstall` first, then install with `DC_AGENT` set to
    it. Install takes its name from `DC_AGENT`, else `CLAUDE_CODE_AGENT`,
    else `claude`. To accept the change, run `sessionphone bot uninstall`
    first, then `sessionphone bot install`. Uninstall removes the old plist
    that install checks against, so either way the install line install
    prints also sets every other changed key it carries. Use that line.
  - A key install never carries can't move to the new job. If you mean to
    drop it, or to change any of these settings, run
    `sessionphone bot uninstall` first, then `sessionphone bot install`.
  - An old plist that can't be read refuses too, because what it carries
    can't be known.
- **To see a refusal before it happens,** print the old job's environment
  with `plutil -p ~/Library/LaunchAgents/com.sessionphone.bot.<agent>.plist`
  and set the same `DC_*`, `SESSIONPHONE_ROOT` and `SESSIONMAIL_*` values in
  the shell you install from.
- **Install refuses when the old job is loaded but its plist is gone**
  (exit 2, nothing written, nothing booted out): its settings can't be
  compared. Run `sessionphone bot uninstall`, then `sessionphone bot install`.
- **If launchd still lists the old job after the bootout,** install stops
  with exit 1. It does not start the new job, and it does not write the new
  plist. Run `sessionphone bot install` again: it is safe to repeat.
- **`sessionphone bot uninstall`** removes the new job and the old one, one
  line for each. It says `no launchd job was installed` only when neither
  is there.
- **`sessionphone bot status`** names the new label, and prints a `legacy:`
  line while the old job is loaded or its plist exists. The line names the
  command that retires the old job: `sessionphone bot install`, or
  `sessionphone bot uninstall` first when the old plist is gone.

## The web call

`sessionphone web` serves a browser tab as a call device. It runs in the
foreground, from your own shell, so it reads that shell's environment, not
the bot's job.

| What | Where |
| --- | --- |
| the web root | `<root>/web/`, or `DIR/web/` with `--root DIR` |
| the passkeys, mode 0600 | `<root>/web/passkeys.json` |
| the pending enrol code, mode 0600 | `<root>/web/enrol.json` |
| with `--legs`, the call's sockets | `<root>/web/call-ears.sock`, `<root>/web/call-mouth.sock` |

It binds `127.0.0.1:8775` by default (`--host`, `--port`). With `--legs` each
finished turn is mailed to a seat, and it reads:

- the seat: `--mail-to`, else `DC_MAILTO`, else `<DC_AGENT>@<host>`;
- the host: `DC_HOST`, else this machine's DNS name;
- the From: `DC_ADDR`, `DC_AGENT_NAME`, and the same `DC_SIGNER_*` and
  `DC_MESSAGE` as the bot;
- the caller's name: `--caller-name`, else `DC_CALLER_NAME`, else `owner`;
- the heard log: `--heard-dir`, else `EARS_HEARD_DIR`, else
  `$VOICEMODE_BASE_DIR/logs/conversations`, else
  `~/.voicemode/logs/conversations`.

## Settings

Every key sessionphone reads, its type, its default and what it does, is on
one page: [Settings][settings].

[settings]: settings.md
[recording]: ../README.md#call-recording-off-unless-it-is-turned-on
[the-bot]: #the-bot
[agent-mail]: #agent-mail
[callers-and-channels]: #callers-and-channels
[call-recording]: #call-recording
[the-web-call]: #the-web-call
[settings-section]: #settings
[bot-mail-signing]: bot-mail-signing.md

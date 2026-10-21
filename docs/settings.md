# Settings

Every key sessionphone reads: its name, type, default and what it does.

- **The bot reads its `DC_*` settings from its environment.**
  `sessionphone bot install` copies each `DC_*` set in your shell into the
  bot's launchd job, and the bot reads them once, when it starts. To change
  one, set it in your shell and run `sessionphone bot install` again.
- **`bot install` remembers them in the phone tool's host settings file,
  `~/.sessionkit/phone/settings/phone.json`.** Once the job is loaded, it
  saves each `DC_*` and `SESSIONPHONE_ROOT` your shell gave, under
  `bots.<agent>`, so a later install from an empty shell installs the same
  job. See [What bot install remembers][what-bot-install-remembers].
- **There is no seat or agent override.** One bot has one set of `DC_*`, the
  set its job was installed with. The kit's guide to the cascade is
  [the kit's settings guide][kit-settings], `docs/settings.md` in the
  sessionkit repo.
- **sessionmail's mail-route settings ride along.** `bot install` also
  carries six `SESSIONMAIL_*` keys, which say how the bot's mail travels.
  They are sessionmail's, not sessionphone's. See [Mail route][mail-route].
- **Recording is the exception: it lives in a file.** The `record*` keys are
  read from ears' `ears.json`, which the bot follows while it runs. See
  [Recording][recording].
- **`PIP_CHAT_<NAME>` is a deprecated alias** for each `DC_<NAME>`. It is
  still read, with a warning on stderr. Rename it.
- **An empty value counts as unset.**

Where each piece lives, and what it is for, is on the
[Configuration][configuration] page.

The keys, by what they set:

- [Identity and mail][identity-and-mail]
- [Mail route][mail-route]
- [Paths][paths]
- [Chat][chat]
- [Calls][calls]
- [Hearing][hearing]
- [Voice][voice]
- [Recording][recording]
- [The web call][the-web-call]
- [The dc-* tools][the-dc-tools]
- [Read from the environment only][read-from-the-environment-only]

`<root>` is the bot's root: `DC_ROOT`, else `SESSIONPHONE_ROOT`, else the
root `bot install` keeps in `phone.json` (below), else `~/.sessionphone`.
`<agent>` is `DC_AGENT` as below, and `<Name>` is `DC_AGENT_NAME`.

## Identity and mail

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `DC_AGENT` | name | `CLAUDE_CODE_AGENT`, else `claude` | who the bot speaks for, in lower case. It names the launchd job |
| `DC_AGENT_NAME` | text | `<agent>`, capitalised | the name in greetings and in the mail's From |
| `DC_HOST` | host | sessionmail's host: `SESSIONMAIL_HOST`, else the `host` in sessionmail's `mail.json`, else the first part of this machine's name | the domain the default addresses are built on. The bot refuses to start when it is unset and that host is empty or `localhost` |
| `DC_ADDR` | address | `<agent>-chat@<host>` | the bot's own From seat. Its local part is the seat the key signs as. Do not set it to the agent's box: the signer and the From would be different seats, and the bot refuses |
| `DC_MAILTO` | address | `<agent>@<host>` | where Delta text goes: the agent's box, where chat and call turns are mailed |
| `DC_MESSAGE` | path | `message` on `PATH` | sessionmail's `message` binary. `bot install` pins it to an absolute path: launchd's `PATH` is short |
| `DC_SIGNER_SOCK` | path | `SEAT_SOCKET` from `message seat agent --install` | the socket of the seat's key agent. Unset on the mail drop, the bot does not sign. When it signs (an MTA, or `--registry`) with neither `DC_SIGNER_*` set, `bot install` runs that command and uses what it prints (saved only with `--registry`); a sessionmail without `Capability: install` refuses instead |
| `DC_SIGNER_PUB` | path | `SEAT_PUB` from `message seat agent --install` | the seat's `.pub`, or a copy kept apart from the key. Set both `DC_SIGNER_*` to use a seat agent you run yourself |
| `DC_BOT_BOX` | path | `~/.mail/agents/<agent>-chat` | the bot's own mailbox, which it reads for mail into a call |
| `DC_SAY` | command | `dc-say` | the reply command a bridged mail suggests |

## What bot install remembers

The file is `~/.sessionkit/phone/settings/phone.json`: the host layer of the
tool `phone`, in the kit's `phone` package. It is written 0600, in a folder
made 0700, and only after the job is loaded, so a refused or failed install
leaves the last settings that worked.

```json
{"bots": {"ada": {"DC_HOST": "mac.example.net", "DC_VOICE": "af_sky"}}}
```

- **One map per bot**, keyed by the setting's own variable name.
- **Each value resolves in the family's order:** your shell, then the file,
  then the default. An empty value in your shell is no value.
- **A value your shell gives is saved**, and replaces the saved one. The
  `settings:` block says `this shell (saved; was ...)`.
- **What is never saved:** `DC_AGENT` (it is the map's key), `DC_CALLERS`,
  a `DC_MESSAGE` pinned from your `PATH`, a `DC_EARS_SRC` found from the ears
  uv tool, and sessionmail's mail-route settings (below).
- **`bot install --forget KEY`** drops a saved value, for this run and every
  later one. It is repeatable.
- **Which bot:** `DC_AGENT`, else `CLAUDE_CODE_AGENT`, else the one bot the
  file records, else `claude`. With two or more recorded and no name in your
  shell, install refuses (exit 2) and names them.
- **Drop-ins:** `phone.d/*.json` beside the file are read after it, in name
  order, a later file winning. Install never writes them. A value your shell
  gives for a key a drop-in sets is used for that run and not saved.
- **`_locked`**, in any of the files, is a list of dotted paths
  (`bots.<agent>.<KEY>`, or `bots.<agent>`). A locked key is ignored in every
  file read after it, and install refuses a different value from your shell.
- **Refused (exit 2, writing nothing):** a file that cannot be read or is not
  this shape, and a saved `DC_SIGNER_SOCK` or `DC_SIGNER_PUB` whose path is
  gone. Give it again in your shell, or `--forget` it. When your sessionmail
  has `Capability: install` and your shell sets no `DC_SIGNER_*`, the refusal
  also names `--forget DC_SIGNER_SOCK --forget DC_SIGNER_PUB`: with neither
  saved, install runs `message seat agent --install` itself. A lock on a key no
  file gives a value still holds: the bot takes its default, and install
  refuses a value from your shell.
- **Not saved after all:** if the file cannot be written once the job is
  loaded, install still prints every line, with `(NOT saved)` in the
  `settings:` header, then says why and exits 1. The bot is running; the next
  install will not remember this run's values.

### Who else reads it

Install is not the only reader. Each of these takes the same order (your
shell, then the file, then the default) and says which it took:

- **`bot status` and `bot uninstall`** act on the bot install would pick.
  When your shell names none, a `bot:` line says which, and where from. A name in your
  shell is taken before any file is read, so `DC_AGENT=NAME sessionphone bot
  uninstall` works past a file that cannot be read. With no name in your
  shell, a file that cannot be read, or two bots recorded, refuses (exit 2).
- **The root** (`SESSIONPHONE_ROOT`), for every verb and the `dc-*` tools:
  your shell's, else the one the file keeps for that bot, else
  `~/.sessionphone`. A lock is not enforced here: only install, which writes,
  refuses a value your shell gives.

## Mail route

These are sessionmail's own settings. sessionphone does not read most of
them: `bot install` carries each one your shell sets into the job, so the
bot's `message send` sees them. To change one, set it and run `bot install`
again. No other `SESSIONMAIL_*` is carried, and neither is the old
`SESSION_MAIL_*` spelling.

`bot install` does not save them in `phone.json`: sessionmail keeps them in
its own `~/.sessionkit/mail/settings/mail.json` (`sendmail`, `root`, `host`,
`postfix.relay_domain`). Install asks the installed sessionmail whether it
reads that file. For each one your shell set, its `settings:` block names the
`mail.json` key to write, or says that this sessionmail reads it from the
environment only. `SESSIONMAIL_HOME` has no key: export it again at each
install.

| Key | Default | What it does |
| --- | --- | --- |
| `SESSIONMAIL_SENDMAIL` | unset: the `sendmail` key in sessionmail's `mail.json` (a key named `carrier` is not read), else sessionmail's own default (`drop` today); a sessionmail that reads the environment only uses `/usr/sbin/sendmail` | the carrier, which also decides whether the bot signs ([Bot mail signing][bot-mail-signing]). `drop` writes straight into this host's mailboxes, with no MTA. A Mac with no postfix needs `drop` |
| `SESSIONMAIL_ORIGIN` | unset | the domain postfix appends to a bare address. NOT carried into the job: the reader verifies against its own, so set it in sessionmail's settings key `postfix.origin` |
| `SESSIONMAIL_RELAY_DOMAIN` | unset | the relay for general delivery |
| `SESSIONMAIL_HOME` | unset | this host's seat-files root |
| `SESSIONMAIL_ROOT` | unset | the maildir root the drop writes to |
| `SESSIONMAIL_HOST` | the `host` in sessionmail's `mail.json`, else this machine's short name | the sender's host, and the host of the bot's Message-IDs. sessionphone reads it too |

The defaults are sessionmail's; its own docs are the authority on them.

sessionphone never opens `mail.json` itself. When the job carries no
`SESSIONMAIL_HOST` or `SESSIONMAIL_SENDMAIL`, the bot asks its own `message`
(the sessionmail it sends with) what it reads, once at start, and names the
answer on its start line in `bot.log`: `ids at <host>, from <where>` and
`carrier <carrier> (<where>)`. `bot install`'s `mail:` line names the same
carrier. A change in `mail.json` reaches the bot at its next start.

## Paths

Each one is a path, and each defaults to a place under `<root>`.

| Key | Default | What it holds |
| --- | --- | --- |
| `SESSIONPHONE_ROOT` | the root `bot install` keeps, else `~/.sessionphone` | the root, for every verb and the bot |
| `DC_ROOT` | `SESSIONPHONE_ROOT`'s root | the bot's root, when it differs |
| `DC_ACCOUNTS` | `<root>/accounts` | the Delta Chat account and its keys |
| `DC_OUTBOX` | `<root>/outbox` | the jobs the `dc-*` tools leave for the bot |
| `DC_ACKS` | `<root>/acks` | where the tools wait for the bot's answer to a job |
| `DC_LOG` | `<root>/bot.log` | the bot's log |
| `DC_SEEN` | `<root>/bridged-ids.txt` | the message ids already mailed, so a restart never mails one again |
| `DC_UNDELIVERED` | `<root>/undelivered` | agent mail that failed to send, waiting for a retry |
| `DC_CALLERS_FILE` | `<root>/callers.txt` | who may ring the bot |
| `DC_CHANNELS_FILE` | `<root>/channels.txt` | where the bot may send |
| `DC_GUESTS` | `<root>/call-guests.txt` | who may mail into a call |
| `DC_VOICES` | `<root>/voices.json` | which voice reads each sender's mail into a call |
| `DC_CALLS_DIR` | `<root>/calls` | one directory per call |
| `DC_LIVE` | `<root>/call-live.json` | the live call's state |
| `DC_EARS_SOCK` | `<root>/call-ears.sock` | the socket the ears open a call on |
| `DC_MOUTH_SOCK` | `<root>/call-mouth.sock` | the socket the mouth speaks into a call on |
| `DC_INVITE` | unset | a file the bot writes its live invite link to at start |
| `DC_PROFILE` | unset | a second profile's name, such as `public`. Its sockets go under `<root>/<profile>/` |
| `DC_OUTBOX_PUBLIC` | `<root>/public/outbox` | the public profile's outbox, read by `sessionphone channels`. Its list sits beside it |

## Chat

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `DC_ACCEPT_REQUESTS` | `on`/`off` | `off` | accept a stranger's contact request. Off, it is mailed in as a flagged notice and never answered |
| `DC_IMAGE_TEXT` | `on`/`off` | `on` | read the text in a posted image (macOS Vision) and mail it with the image, fenced as untrusted |
| `DC_XDC_IN` | path | unset | a file each app tap is written to, one JSON line each. Unset, each tap is mailed |
| `DC_XDC_RT_IN` | path | unset | a file for an app's realtime data. Unset, the data is counted and dropped. Never the same file as `DC_XDC_IN` |
| `DC_XDC_MATCH` | `key=value,...` | unset | only a JSON payload that matches ANY listed pair is a tap. Values compare as strings |
| `DC_XDC_FEED_CHATS` | chat ids, comma-separated | none | the chats the heard log may be read out into. List only your own 1:1 chat |
| `DC_XDC_FEEDS_MAX` | number | `2` | how many feeds may run at once |

## Calls

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `DC_CALLS` | `on`/`off` | `on` | answer and place calls. Off, the bot is chat only and needs no media pack |
| `DC_ANSWER` | `auto`/`ring` | `auto` | `auto` answers at once. `ring` mails the agent and waits for `dc-answer`; with no answer, it answers as voicemail |
| `DC_RING_WAIT` | seconds | `25` | how long `ring` waits for the agent |
| `DC_GREETING` | text | `<Name> here. Go ahead.` | said when a call is answered |
| `DC_GREETING_OUT` | text | `Hi, <Name> here, ringing you back.` | said when the bot's own call is answered |
| `DC_GREETING_VOICEMAIL` | text | `<Name> can't pick up right now. Leave a message and it reaches <Name> as mail.` | said when a call goes to voicemail |
| `DC_NO_REPLY_S` | seconds | `0` (off) | after this long with no reply to a turn, say the holding line, once |
| `DC_HOLDING_LINE` | text | `I heard you, give me a moment.` | the holding line |
| `DC_HOLD_TURN` | `1`/`0` | `1` | keep a turn open while the caller is still speaking. `0` lets a turn go out while they talk on |
| `DC_PREVIEWS` | `1`/`0` | `0` | mail the turn so far as a preview while the caller speaks. Needs ears |
| `DC_BARGE_IN` | `1`/`0` | `0` | stop speaking when the caller speaks |
| `DC_BARGE_IN_KEEP_TEXT` | `1`/`0` | `1` | on a cut, post the unspoken rest into the chat as text |
| `DC_HEARD` | `1`/`on`/`true`/`yes` | off | write a call's words into VoiceMode's heard log, for the chats `DC_HEARD_CHATS` lists |
| `DC_HEARD_CHATS` | chat ids, comma-separated | none | the chats whose calls may write the heard log |
| `DC_HEARD_PY` | path | `~/.voicemode/bin/voicemode-heard-hook` | the hook that writes the heard log |
| `DC_STUN_RTO` | seconds | `2.0` | the STUN retry timeout. A public relay's round trip needs more than the library's own 0.5 |

`DC_BARGE_IN` and `DC_BARGE_IN_KEEP_TEXT` have no `PIP_CHAT_` alias.

## Hearing

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `DC_STT_URL` | URL | `mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions` | the speech-to-text server for voice notes and calls. Any OpenAI-compatible `/v1/audio/transcriptions` |
| `DC_STT_MODEL` | text | unset: suits the URL's kind | the model asked for. `bot install` refuses a URL of unknown kind without it |
| `DC_EARS` | `1`/`0` | `0` | hear calls through ears, in the bot's own process, instead of chunks |
| `DC_EARS_BACKEND` | `auto`/`apple`/`nemotron`/`whisper` | `auto` | the ears' recogniser |
| `DC_EARS_TURNS` | `silence`/`smart` | `silence` | how the ears decide a turn has ended |
| `DC_EARS_SRC` | path | set by `bot install` when ears is a uv tool | where the bot imports ears from |

## Voice

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `DC_VOICE` | voice | `af_sky` | the bot's voice: a Kokoro id, or a clone directory under `DC_VOICES_ROOT` |
| `DC_VOICES_ROOT` | path | `~/.voicemode/voices` | where clone voices live |
| `DC_TTS` | `auto`/`server`/`apple` | `auto` | `auto` uses the speech server when it answers, else macOS `say` |
| `DC_KOKORO_URL` | URL | `http://127.0.0.1:8890/v1/audio/speech` | the server for a Kokoro voice |
| `DC_KOKORO_MODEL` | text | `mlx-community/Kokoro-82M-bf16` | the Kokoro model |
| `DC_TTS_URL` | URL | `http://127.0.0.1:8890/v1/audio/speech` | the server for a clone voice |
| `DC_TTS_MODEL` | text | `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16` | the clone model |
| `DC_APPLE_VOICE` | text | the system voice | the voice macOS `say` uses |
| `DC_FFMPEG` | path | `ffmpeg` on `PATH`, else the Homebrew paths | the `ffmpeg` binary |

## Recording

These are ears' keys, shared with the microphone. The bot reads them from
the first layer that is set: its own `--record`/`--no-record`, then the
environment, then `ears.json` (`$EARS_CONF`, else
`~/.sessionkit/voice/settings/ears.json`), then the default. `bot install`
does not carry `EARS_*`, `EARS_CONF` included, so a bot under launchd skips
the environment and reads `~/.sessionkit/voice/settings/ears.json`.

| `ears.json` key | Environment | Type | Default | What it does |
| --- | --- | --- | --- | --- |
| `record` | `EARS_RECORD` | boolean | `false` | keep each call's audio. An invalid value keeps nothing |
| `record_days` | `EARS_RECORD_DAYS` | whole number | `7` | how many days kept audio stays. `0` keeps it for good |
| `record_notice` | `EARS_RECORD_NOTICE` | boolean | `true` | post a line in the call's chat saying whether it is kept |
| `record_spoken_notice` | `EARS_RECORD_SPOKEN_NOTICE` | boolean | `true` | on a kept call, say so before the greeting |

In the file the values are JSON booleans and integers. In the environment,
`1`, `true`, `on`, `0`, `false` or `off`, in any case.

## The web call

`sessionphone web` reads its own shell, not the bot's job. Each of these is
also a flag, and the flag wins.

| Key | Flag | Default | What it does |
| --- | --- | --- | --- |
| `DC_MAILTO` | `--mail-to` | `<DC_AGENT>@<host>`; with neither set, `--legs` is refused | the seat each finished turn is mailed to, with `--legs` |
| `DC_CALLER_NAME` | `--caller-name` | `owner` | the caller's name, and a new passkey's label. It grants nothing |
| `EARS_HEARD_DIR` | `--heard-dir` | `$VOICEMODE_BASE_DIR/logs/conversations`, else `~/.voicemode/logs/conversations` | the heard log the turns are read from |
| `SESSIONPHONE_ROOT` | `--root` | `~/.sessionphone` | the root. The web state goes in its `web/` |

## The dc-* tools

The `dc-*` tools run in your own shell, not the bot's job, and read these
from it. They also read the paths above (`DC_ROOT`, `DC_OUTBOX`, `DC_LOG`,
and so on), to find the bot's root. With neither `DC_ROOT` nor
`SESSIONPHONE_ROOT` set, they ask the package for the root `bot install`
keeps, so they and the bot read one root. If that question fails, they use
`~/.sessionphone`.

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `DC_TAKEN_WAIT` | seconds | `10` | how long a tool waits for the bot to take its job. `0` does not wait, except for a job the bot gates, which is always waited for |
| `DC_XDC_SUMMARY` | text | unset | `dc-xdc-update`: the short text shown beside the app's icon in the chat |
| `DC_WEB_OUT` | path | `<root>/calllog.html` | `dc-log-web`: the page it writes |
| `DC_WEB_LINES` | number | `120` | `dc-log-web`: how many log lines the page shows |
| `DC_BOT` | text | `sessionphone.bridge.bot <agent>` | `dc-live`: the process it looks for |

## Read from the environment only

These are read from the process's own environment. `bot install` does not
carry them.

| Key | Default | What it does |
| --- | --- | --- |
| `SESSION_MAIL_HOST` | this machine's short name | the old spelling of `SESSIONMAIL_HOST`, read after it. Not carried, so a bot under launchd never sees it |
| `EARS_CONF` | `~/.sessionkit/voice/settings/ears.json` | the ears settings file. Not carried, so a bot under launchd reads `~/.sessionkit/voice/settings/ears.json` |
| `ECHO_SPOOL` | `~/.voicemode/playout.jsonl` | where any process may say it is about to play audio, so a call drops it as echo |
| `ECHO_VOICEMODE_EVENTS` | `~/.voicemode/logs/events` | VoiceMode's event log, read for the same reason |

[kit-settings]: https://github.com/sessionkit/sessionkit/blob/HEAD/docs/settings.md
[configuration]: configuration.md
[recording]: #recording
[identity-and-mail]: #identity-and-mail
[paths]: #paths
[chat]: #chat
[calls]: #calls
[hearing]: #hearing
[voice]: #voice
[the-web-call]: #the-web-call
[mail-route]: #mail-route
[what-bot-install-remembers]: #what-bot-install-remembers
[the-dc-tools]: #the-dc--tools
[read-from-the-environment-only]: #read-from-the-environment-only
[bot-mail-signing]: bot-mail-signing.md

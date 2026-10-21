# Changelog

All notable changes to sessionphone are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The one
version lives in `pyproject.toml`; `scripts/release.py` bumps it with
`uv version`, mirrors it into `.claude-plugin/*.json` when present, and
stamps this file.

## [Unreleased]

## [0.2.1] - 2026-10-21

### Signing follows the mail carrier, and the mail drop needs no key (PHONE-203)

**One rule decides whether the bot signs.**

- Whether the bot signs its mail to the agent is one rule, judged at install
  and at every start from the carrier the bot's own `message send` will use,
  as sessionmail's own resolver answers it for the job's environment (its
  `SESSIONMAIL_SENDMAIL`, else the `sendmail` key in sessionmail's
  `mail.json`, else sessionmail's default, the mail drop). There is no flag.
- An MTA carrier: signed, as before. Must-sign is on, and a send with no
  usable key is refused.
- The mail drop with no signer set or saved: unsigned. The bot writes each
  mail into the box itself, as its owner, so no key, no registry and no seat
  step are needed: `bot install` skips the seat's agent, and the start check
  skips the signer.
- A signer set or saved is used on any carrier, the drop included.
- A carrier sessionmail refuses, a `mail.json` it cannot read, or a
  sessionmail that cannot be asked: the bot refuses to start and `bot
  install` refuses (exit 2, writing nothing). It never falls back to
  unsigned, nor to a guessed MTA.
- The `seat:` line and the bot's start line name the mode and why, for
  example `signed (carrier /usr/sbin/sendmail is an MTA, from SESSIONMAIL_SENDMAIL)`
  or `unsigned (carrier drop: this bot writes into the box itself, from
  sessionmail's default)`. With no `sendmail` answer from `mail.json`, the
  start line's carrier is now sessionmail's resolver's, not an assumed
  `/usr/sbin/sendmail`.

**`bot install --registry DIR` gives the bot a key.**

- With no signer set or saved, `bot install --registry DIR` runs the seat's
  key agent (`message seat agent --install <agent>-chat`). Only when
  sessionmail says the seat has no key yet does it first mint it into the
  seat registry DIR (`message seat init --registry DIR <agent>-chat`), then
  run the agent. A key already there is never minted again, so a re-run is
  safe. It works on any carrier, the drop included.
- The signer it gets is saved, so later installs (without `--registry`)
  and starts sign. The `settings:` block says so; the `seat:` line names
  the registry when this run minted the seat.
- A refused mint is exit 2 and a failed one exit 1, quoting sessionmail's
  line; nothing of the bot is written. `--registry` is the only way install
  mints: with no registry, a seat not minted is refused, naming
  `--registry DIR`.

**On the mail drop: the box first, no Cc that sinks a mail, warnings once.**

- The bot makes the agent's box at every start, before its first send: it
  asks `message path NAME`, and only when sessionmail says there is no box
  does it run `message setup --box NAME`. The drop never makes a box, and a
  send to one that is not there is refused, so a box that cannot be made now
  refuses the start (exit 2), naming the step, instead of losing every mail.
- A Cc goes only to a box sessionmail says is there (for a plus address,
  `base+tag`, the box `base` counts, as the drop delivers it there), and on
  the mail drop only on this host. Under the drop one recipient it cannot
  deliver to refuses the whole mail, so a call guest with no box no longer
  costs the agent its own copy. A Cc left off is logged once per run, with
  why.
- sessionmail's warnings on a send (the UNSIGNED and MagicDNS notices) reach
  the bot's log once per run, never once per mail. Before, they were not
  logged at all.
- The hear line, and a new `agent box:` line at every bot start, say that
  `session start NAME` runs `claude --agent NAME`, and that only a session
  started as that agent hears the box: a plain `claude` gets no copy of its
  mail, and nothing says so.
- Where the job carries `SESSIONMAIL_ROOT`, sessionmail's box tools
  (`message path`, `message setup`) also get `SESSION_MAIL_ROOT` with the
  same value: today they read only that older spelling, while `message
  send` reads `SESSIONMAIL_ROOT` first. A workaround, until sessionmail
  reads both.
- `sessionphone web --legs` makes the seat's box at start too, before the
  first turn, the way the bot does: under the drop a turn to a box that is
  not there was refused, so every turn of the call was lost, and said only
  per turn. A box it cannot make refuses the start (exit 2), naming the box
  and the step. A plus address (`base+tag`) whose base box is there needs
  no box of its own, at the bot's start or the web's.
- The web's start line names whether its turns are signed and why, as
  `seatmail.signing()` rules (for example `unsigned (carrier drop: ...)`).
  Before, it said `signed` on every carrier. A new `turns box:` line names
  the seat's box.
- With no signer on an MTA, the start refusal names `sessionphone bot
  install --registry DIR` as the way to give the bot a key, before the
  steps by hand.

**Docs.**

- New `docs/bot-mail-signing.md`: on the mail drop the bot's mail is not
  signed, and why; on an MTA or for a fleet, `bot install --registry DIR`;
  and the two conditions for a mail to reach the agent at all (the box
  exists, and the agent runs as `claude --agent NAME`). The README's "The
  bot's seat" links to it and now gives the key by `--registry DIR`.
- The README, `docs/configuration.md`, the skill and `bot install --help` no
  longer say to export `SESSIONMAIL_SENDMAIL=drop` before `bot install`: the
  drop is sessionmail's default. They name the `mail.json` key `sendmail`.

### `bot install` runs the bot's seat agent itself

- With no `DC_SIGNER_SOCK` and `DC_SIGNER_PUB` set or saved, and a
  sessionmail whose `message seat agent --help` has a line that is exactly
  `Capability: install`, `bot install` runs `message seat agent --install
  <agent>-chat` (sessionmail's default socket). It reads the `SEAT_*` lines
  that prints, split as the shell would and never evaluated, and the job's
  `DC_SIGNER_SOCK` and `DC_SIGNER_PUB` are its `SEAT_SOCKET` and `SEAT_PUB`.
  They are not saved: each install asks again, and a re-run finds the agent
  already there. The README's seat section is now: mint the seat, then
  `sessionphone bot install`.
- The seat must be minted first (`message seat init --registry DIR
  <agent>-chat`). When it is not, install refuses (exit 2, writing nothing),
  quoting sessionmail's refusal and naming that step. Any other refusal is
  quoted as sessionmail wrote it, exit 2, writing nothing.
- When `message seat agent --install` fails (exit 1), install exits 1,
  writing nothing of the bot, shows sessionmail's `FAILED` line and the log
  lines it quotes, and names the recovery: `message seat agent --uninstall
  <agent>-chat`, then `--install` again, or `bot install` again.
- A sessionmail without that capability line (or with no `seat` noun) keeps
  the steps by hand: with no signer set, install refuses as before, naming
  them, and says this sessionmail lacks `Capability: install`.
- The `seat:` line names the agent's launchd label when install ran it, and
  the `settings:` block says the signer came from `message seat agent
  --install`. (Whether the bot signs at all now follows the mail carrier:
  see "Signing follows the mail carrier" above.)

### `bot install` remembers its settings between runs

- `bot install` saves each `DC_*` and `SESSIONPHONE_ROOT` it was given in
  the phone tool's host settings file, `~/.sessionkit/phone/settings/phone.json`
  (0600, one map per bot), once the job is loaded. A later install from a
  shell that sets none of them installs the same job. The order is the
  shell, then the file (and `phone.d/*.json` after it, with `_locked`
  honoured), then the default.
- Which bot: `DC_AGENT`, else `CLAUDE_CODE_AGENT`, else the one bot the file
  records, else `claude`. With two or more recorded and no name, install
  refuses (exit 2) and names them.
- A `settings:` block names each value the job carries and where it came
  from: this shell (saved, or replacing a saved value), the file, the
  default, or derived on this run (a pinned `DC_MESSAGE`, a found
  `DC_EARS_SRC`), which is never saved.
- `bot install --forget KEY` (repeatable) drops a saved value.
- Refused (exit 2, writing nothing): a settings file that cannot be read or
  is not that shape, a locked key the shell changes, and a saved
  `DC_SIGNER_SOCK` or `DC_SIGNER_PUB` whose path is gone.
- sessionmail's mail-route settings are still carried for the run, and are
  not saved: they are sessionmail's, in its own `mail.json`. Install asks the
  installed sessionmail whether it reads that file, and says for each one the
  shell set whether it is kept and the step that keeps it. When it does, a
  job that carries no `SESSIONMAIL_SENDMAIL` names on its `mail:` line the
  carrier sessionmail reads there, or says that sessionmail cannot read the
  file or that its `sendmail` is not a carrier, so the bot's mail goes nowhere.

### Every reader honours what `bot install` keeps

- `bot status` and `bot uninstall` act on the bot install would pick: a name
  in the shell, else the one bot `phone.json` records, else `claude`. With
  no name in the shell, a `bot:` line says which they picked. Two bots recorded and no name, or a settings
  file that cannot be read, refuses (exit 2) and names the way past it; a
  name in the shell is taken before any file is read.
- The root every verb uses is the shell's `SESSIONPHONE_ROOT`, else the root
  `phone.json` keeps for that bot, else `~/.sessionphone`. An empty
  `SESSIONPHONE_ROOT` counts as unset. `dc-env` and `dc-log` read the same
  root, and fall back to `~/.sessionphone` when it cannot be read.
- The mail host the bot builds its addresses and Message-IDs on is
  `SESSIONMAIL_HOST`, else the `host` in sessionmail's `mail.json`, else this
  machine's short name. A host kept only in `mail.json` was overridden by
  the machine's name on every send. The bot asks its sessionmail once at
  start, and its start line says where the host came from.
- The carrier the bot names is the shell's `SESSIONMAIL_SENDMAIL`, else what
  sessionmail reads (its `mail.json`, else its own default), and the start
  line says which. `bot install`'s `mail:` line uses the same words: an MTA
  named in `mail.json` now reads `MTA <path> (...)`.
- A failed save of `phone.json` after the job is loaded prints every line of
  the install, with `(NOT saved)` in the `settings:` header, then the reason,
  and exits 1.
- A lock on a key no file gives a value now says so ("which gives it no
  value (the bot takes its default)"), and no longer says "at None".
- A saved `DC_SIGNER_SOCK` or `DC_SIGNER_PUB` whose path is gone still
  refuses (exit 2), saying to give it again or `--forget` it. When the
  installed sessionmail has `Capability: install` and the shell sets no
  `DC_SIGNER_*`, it also names `bot install --forget DC_SIGNER_SOCK --forget
  DC_SIGNER_PUB`, which lets install run `message seat agent --install`
  itself. Without that capability its words are unchanged.

### `bot install` checks the bot would start before it loads anything

- `bot install` refuses (exit 2, writing nothing) when `DC_SIGNER_SOCK` or
  `DC_SIGNER_PUB` is unset, with the bot's own start-up refusal: it names
  the steps that make the seat. (Since then, install runs the seat's agent
  itself when sessionmail can: see above.)
- `bot install` pins `DC_MESSAGE` to the installing shell's `message`, as an
  absolute path, unless the shell sets it.
- `bot install` runs the bot's own start check (the `message` binary, the
  signer, the key's seat, the `--message-id` probe) on the job's environment
  before it loads anything. A job that would refuse to start is no longer
  installed to loop under launchd. A `seat:` line names the seat it signs as.
- `bot install` and `sessionphone setup` make the agent's box (`message setup
  --box <agent>`), check it with `message path`, and print a `hear:` line:
  `To hear Delta messages, start Claude with: session start <agent>`.
- `bot status` prints the last line of the bot's log, and falls back to
  `bot.launchd.log` when the bot has written none.
- The default mail host is sessionmail's own (`SESSIONMAIL_HOST`, else the
  first part of this machine's name), not a DNS lookup. A Mac whose DNS name
  is a reverse-DNS name now installs, and mails a box sessionmail delivers.
- `SESSIONMAIL_ORIGIN` is no longer carried into the job: the reader verifies
  against its own origin, so a value only the job held made every mail
  verify bad. Set it in sessionmail's settings key `postfix.origin`. The
  `mail:` line says when the shell sets it. A legacy job that carried it is
  refused, naming where it belongs.
- README: the seat section gives the steps that work with today's
  sessionmail (a seeded seat registry, `seat init --registry`, `seat
  agent`), names the default agent (`claude`, so the seat is `claude-chat`),
  says which of `DC_ADDR` (the bot's own From seat) and `DC_MAILTO` (where
  Delta text goes) is which, and how a Claude session hears the box.

## [0.2.0] - 2026-10-21

### The bot's launchd job is `dev.sessionkit.phone.bot.<agent>` (PHONE-193)

Upgrade note: run `sessionphone bot install` again after upgrading. It
retires the job 0.1.0 installed.

- `sessionphone bot install` now creates the launchd job
  `dev.sessionkit.phone.bot.<agent>`, with its plist at
  `~/Library/LaunchAgents/dev.sessionkit.phone.bot.<agent>.plist`. 0.1.0
  used the label `com.sessionphone.bot.<agent>`, and that label is no longer
  written. Every launchd label the kit ships is now
  `dev.sessionkit.<package>.<job>`.
- `bot install` retires the old `com.sessionphone.bot.<agent>` job for the
  same agent before it starts the new one. It boots out the old job if it is
  loaded, removes its plist if it exists, and prints one line for each.
  It touches no other label.
- `bot install` refuses (exit 2, writing nothing and booting nothing out)
  when the old plist's environment holds a key the new job would drop, or
  give a different value. `PATH` is exempt. The message names each key, old
  -> new, and the fix: run install again with the old value in the shell, or
  `bot uninstall` first if the change is meant. A changed `DC_AGENT` gets a
  line of its own, since install always writes the name it installs as: to
  keep the old name, `bot uninstall` first, then install with `DC_AGENT` set
  to it. That install line also sets the other changed keys install carries,
  since uninstall removes the plist install compares. Install also refuses when the
  old job is loaded with its plist gone; the fix is `bot uninstall` first. If
  launchd still lists the old job after its bootout, install exits 1 without
  starting the new job. Run it again.
- `bot uninstall` removes the old job as well as the new one. `bot status`
  prints a `legacy:` line while the old job is loaded or its plist exists.
- See "Upgrading from 0.1.0" in `docs/configuration.md`.

### Supported Python is 3.11 to 3.14 (PHONE-191)

- `requires-python` is now `>=3.11,<3.15`, so an install picks Python 3.14
  or older, the versions sessionphone is tested on. Python 3.15 is not yet
  supported.
- On Python 3.14, a file or folder that cannot be read is no longer taken
  for a missing one, which 3.14's own path checks would do. The call mail
  leg, the cleanup of transient call audio and `avatar` now stop with the
  error, as they do on 3.13. The callers file reports "cannot be read",
  not "does not exist". `avatar`, the voice-message check and
  `invite --png` refuse with "cannot read" on every Python, where 3.13
  showed a traceback.

### Settings are read from `~/.sessionkit`

- The ears settings file is now `~/.sessionkit/voice/settings/ears.json`,
  and the mail leg's registry hint names
  `~/.sessionkit/mail/settings/mail.json`. Neither is read from the old
  home-directory settings folder any more, and there is no fallback to it.
- An ears settings file left in the old folder is still not read, but
  `bot.log` now says so: one line, once, naming both paths. See "Upgrading
  from 0.1.0" in `docs/configuration.md`.

### The bot's launchd job carries sessionmail's mail-route settings (PHONE-179)

`sessionphone bot install` carried only `PATH`, `SESSIONPHONE_ROOT`,
`DC_AGENT`, `PYTHONUNBUFFERED` and `DC_*` into the launchd job. So
`SESSIONMAIL_SENDMAIL=drop` never reached the bot, its `message send` took
`/usr/sbin/sendmail`, and on a Mac with no postfix a call spoke while every
turn the caller said was lost.

- The job now carries sessionmail's mail-route settings, each when set and
  not empty in the shell that runs install: `SESSIONMAIL_SENDMAIL`,
  `SESSIONMAIL_ORIGIN`, `SESSIONMAIL_RELAY_DOMAIN`, `SESSIONMAIL_HOME`,
  `SESSIONMAIL_ROOT` and `SESSIONMAIL_HOST`. It is a named list: the
  installing seat's own `SESSIONMAIL_*` (`SESSIONMAIL_FROM_NAME`,
  `SESSIONMAIL_SUBSCRIBE`, the pager settings) never reach the bot, nor does
  the old `SESSION_MAIL_*` spelling. Nothing else is newly carried.
  sessionphone does not pick the carrier: that is sessionmail's.
- `bot install` prints a `mail:` line, read back from the plist it wrote: the
  `SESSIONMAIL_*` it carried (`SESSIONMAIL_SENDMAIL` with its value) and the
  carrier the bot will use. It says so when none was carried, and how to get
  the mail drop.
- The bot's `agent mail:` start line in `bot.log` ends with its carrier:
  `carrier drop (...)` or `carrier MTA <path> (...)`.
- README and the skill: with no postfix, export `SESSIONMAIL_SENDMAIL=drop`
  BEFORE `bot install`, and run install again after changing it.

### BEHAVIOUR CHANGE: calls keep no audio unless recording is on (PHONE-177)

Before, every call kept its audio in its call directory, and nothing removed
it: `full-in.wav`, `full-out.wav`, every `caller-NNN.wav` chunk, and with
`DC_EARS=1` `caller-call.wav` and `caller-call.json`. Now a call keeps no audio
unless recording is on, and recording is **off by default**.

- The switch is the ears' one: `--record` or `--no-record` on the bot's
  command line, then `EARS_RECORD` (`1`/`true`/`on` or `0`/`false`/`off`),
  then `"record"` in `$EARS_CONF` or `~/.sessionkit/voice/settings/ears.json`, then off.
  The launchd job from `bot install` carries no `EARS_*`, so it reads
  `~/.sessionkit/voice/settings/ears.json`.
  Any other value keeps recording off and is named in `bot.log`. The file is
  followed while the bot runs.
- **To keep recording calls**, set `"record": true` in `ears.json` (or run
  `ears recording on`). `EARS_RECORD=1` works only for a bot run by hand:
  the launchd job does not carry it.
  When recording is on, every voice the call carries is kept, guests included.
- With it off, the audio a live call needs is deleted once used: each chunk
  when its transcription returns, and `caller-call.wav` (still written during
  an ears call, for the speaker side-car) 10 seconds after hang-up. A bot that
  stopped mid-call deletes that audio at its next start.
- Every call logs `CALL recording: on|off (<where it was set>), retention ...`
  when it starts.
- `record_days` (`EARS_RECORD_DAYS`, else `"record_days"`, else 7; 0 = never)
  prunes kept audio, only while recording is on, and only the files a call's
  new `recording.json` names. Call directories made before this release are
  never pruned or deleted.
- `dc-call-mix` and `ears eval --call` need a call recorded with the switch on.

### BEHAVIOUR CHANGE: a call no longer speaks a holding line unless `DC_NO_REPLY_S` is set (PHONE-174)

The no-reply watchdog (PHONE-114) is now **off by default**: `DC_NO_REPLY_S`
defaults to `0` instead of `8`. Before, a call turn with no reply after 8
seconds heard `DC_HOLDING_LINE` ("I heard you, give me a moment." by default).
Now a call says nothing while it waits, and the bot logs
`CALL no-reply watchdog: off` at start. The `taken=` verdict lines are logged
as before.

- **To keep the old behaviour**, set `DC_NO_REPLY_S=8` (or any window in
  seconds above 0) in the bot's environment. `DC_HOLDING_LINE` still sets
  the words spoken.
- A `DC_NO_REPLY_S` that is not a number now falls back to `0` (off), and
  the start line says so.

### BEHAVIOUR CHANGE: mail spoken into a call trusts only pinned, signed senders (PHONE-155)

A mail threaded to a live call is still read aloud. What changed is who counts
as **known**, and which roots reach a call. Before, any sender could pick any
voice with `X-Voice:` (the agent's own included), and a `From:` listed in
`voices.json` was enough to be spoken in that voice.

- **(a) Address-only guests are no longer known.** A guest is known only when
  their mail is signed (`message verify` exits 0) by a key pinned on their row
  of `call-guests.txt`: `address ssh-ed25519 AAAA...`. Every existing guest is
  an address-only row, so every existing guest is now announced in the plain
  voice as "who I do not have a voice for", and the chat post reads
  `Name <address> (unverified sender): ...`. An unknown sender's `X-Voice:` and
  `voices.json` entry are ignored. To restore a guest, pin their key (README,
  "Mail into a call: who is known"). Address-only rows are still copied on the
  call thread, as before.
- **(b) An agent's mailed replies stay unknown until SM-716** (sessionmail)
  signs call mails. They are still spoken, announced as unverified. The mouth
  and `dc-say` are unaffected.
- **(c) A pinned agent key goes stale when that agent restarts.** Session keys
  are per session, so a pin for an agent's session must be replaced after the
  agent restarts. A human's key is stable.
- **(d) A reply to a root from before a bot restart is not spoken.** The roots
  the bridge issued are held in memory only. A mail reaches a call by the live
  call's root, matched whole, or by a root the bridge issued for that chat; a
  chat keeps its earlier roots only across a turnover, when its next call
  starts within `TURNOVER_S` = 120 s of its last call ending (newest 8 roots
  per chat). A `call-<chat>-<digits>@` string the bridge never issued no longer
  reaches the call.
- A missing, failing or hung `message verify` fails closed: the sender is
  unknown.

### Upgrade notice (read this first)

**After this upgrade EVERY bot refuses to send until its `channels.txt`
exists** (every bot the owner runs). The bot sends only to chats on
that allow-list, a missing list refuses every send, and there is no off switch
(PHONE-102). Write the list first, before the upgraded bot runs: by hand (the
format is below), or, once upgraded, `sessionphone bot install --owner ADDR`
seeds `owner` (and refuses, naming the file, if it has nothing to seed), and
`sessionphone channels add NAME ADDRESS|chat:N` lists each chat the bot may
use, a group only as `chat:N`. The owner writes each bot's list first.
**A bot that is already running** (a hand-written launchd job included): run
`sessionphone channels seed` with that bot's `DC_*` in the shell. It lists the
callers file's owner line as `owner`, the same seed `bot install` writes, and
never touches launchd. Then **restart each bot after upgrading**, so the
running bot is the code the tools on disk expect: until it is restarted, a
`dc-*` send by channel NAME to it exits 3 and says "restart the bot". A bot whose list is missing
now mails its agent (below).
Until then the `dc-*` tools exit 3, `REFUSED: NO channels file at ...`, and
nothing is sent. A public-profile bot has its own list, and its own inbound
gate (below).

**After this upgrade EVERY bot refuses to start (exit 2) without a `message`
binary and a signer** (PHONE-106). Chat now reaches the agent as mail signed by
the bot's own sessionmail seat, never unsigned. Before restarting a bot:

1. Mint its seat: `message seat init --registry DIR <agent>-chat`.
2. Run the seat's agent under launchd: `message seat agent <agent>-chat SOCKET`.
3. Set `DC_SIGNER_SOCK` (that SOCKET), `DC_SIGNER_PUB` (a copy of the seat's
   `.pub`, kept apart from the key) and `DC_MESSAGE` (sessionmail's `message`,
   as an absolute path).
4. Run `sessionphone bot install` again. It carries every `DC_*` into the job.

The README's "Setting up the bot's seat" has the table.

**pip and `uv tool` installs: ask for `sessionphone[media]` when you
upgrade** (PHONE-10). The bot's and the web page's packages are now the
`media` extra (below). The plugin's launcher fetches them by itself. An env
that you install or re-install without `[media]` loses them. A bot that runs
from that env keeps running until its next restart, and then fails to start.
Use `uv tool install 'sessionphone[media]'`, `pip install
'sessionphone[media]'` or `make install`. **A sessionkit install needs
SK-76**, the kit's change to ask for `sessionphone[media]`. Ship it with the
first PyPI release that carries this change.

**A bot with no `DC_STT_URL` that relied on `ears serve` at `:2023` now posts what callers say to mlx-audio at `:8890`** (PHONE-18), and its next `bot install` says `hearing: NO`; the one line that keeps `:2023` is under "Changed".

### Added

- **Configuration, settings and logs docs** (PHONE-183).
  `docs/configuration.md` says what sessionphone configures and where each
  piece lives. `docs/settings.md` lists every key, with its type and default,
  and says plainly that `DC_*` are environment carried in by `bot install`,
  not the kit's settings cascade. The README gains `## Configuration` and
  `## Logs`, which ends in the Health line, `sessionphone bot status`.
  `tests/test_config_docs.py` fails when src/ reads a setting the page
  does not name.
- **`bot install` says whether the bot will hear the caller** (PHONE-18). Its
  last line is `hearing:`, before the first call. It says
  `probing URL (read-only)`, then sends the bot's own transcription request,
  with a half-second test clip, to the STT at `DC_STT_URL` as the launchd job
  carries it. `yes` means a 2xx with a JSON `text`. `NO` means the call will
  connect but the bot will not hear the caller. `not confirmed` means curl
  timed out, or nothing could be checked (the job's plist could not be read
  back, or the clip could not be written). Each verdict names the URL, the
  setting it came from, what was sent and what came back. On `NO` it points
  to VoiceMode's mlx-audio on `127.0.0.1:8890` and its own command,
  `voicemode service install mlx-audio`. sessionphone does not install it.
  The verdict never comes from ears, and the exit code does not change.
  `setup` never probes. The skill's step 7 has the agent tell the user the
  cost and run VoiceMode's command only on their explicit yes.
- **A posted image arrives with its text** (PHONE-34). When a chat message
  carries an image, the bridge mail holds the image line and then the words
  read off the picture by on-device OCR (macOS Vision, no network call, no
  new Python dependency). The words sit in a fence marked UNTRUSTED: every
  line starts with a `|` and a space, so a picture cannot forge the closing
  marker, and the text is cut at 2000 characters. An agent treats it as
  data, never as instructions. The bot
  compiles a small Swift helper, `imgtext`, with `swiftc` the first time it
  starts and again when an upgrade changes the helper's source, then warms
  it off the event thread. Each image is capped at 25 MB,
  50 MP and 10 s. No text gives `[image text: none found by on-device OCR]`.
  A failure is one log line, and the mail goes out with the image line
  alone. There is no caption: nothing on-device gives an honest one today.
  - **A contact request's image is not read**, held or auto-accepted. Its
    body says `[image text: not read: sent as a contact request]`. Under
    `DC_ACCEPT_REQUESTS=on` a stranger's second image IS read, and so are
    the images of a group member added later (named in the contract, not
    closed).
  - **`DC_IMAGE_TEXT=off`** turns it off: images mail as their image line
    alone, and the helper is never built. Default on.
  - **The host needs macOS and `swiftc`** (Xcode Command Line Tools) at
    every start, not just the first. Off macOS, or with no `swiftc`, the
    feature is off, with one log line at start.
  - `make test-vision` runs the real-Vision test. It is opt-in and not part
    of `make test`.
- **Enrol by QR code** (PHONE-136). `sessionphone web` and `sessionphone web
  enrol` print a terminal QR beside the one-time enrol code. It holds
  `<origin>/#code=<code>`, the code in the URL fragment only, so it reaches no
  server log and no proxy. The page fills the code in and removes the fragment
  from the address bar and the history. The origin is this host's tailnet one,
  else the first `--origin` that is not localhost, else no QR and one line
  says why. The `enrol code:` line is unchanged, and the URL is never printed as
  text. websockets' DEBUG log now names a frame from the page by its kind and
  size, never its text, so the code is not logged at any level. `segno` was
  already a dependency.
- **The web call page refuses to be framed** (PHONE-139). `GET /` and
  `GET /index.html` now send `Content-Security-Policy: frame-ancestors 'none'`
  and `X-Frame-Options: DENY`, so another app can open the call in a new tab
  but never embed it. The `/ws` upgrade is unchanged.
- **The web call refuses a passkey ceremony made inside a frame** (PHONE-139).
  An enrol or a login whose clientDataJSON says `crossOrigin: true` is refused,
  and the auth log says why (`crossOrigin is true: signed in inside a frame`).
  A value that is not a boolean is refused too. `crossOrigin: false`, or no
  `crossOrigin` at all, signs in exactly as before.
- **"Open it from another app"** (PHONE-139), a README section: another app
  (Talk's phone button, EARS-317) opens the call with a plain top-level link to
  the browser-facing URL, `https://<host>.<tailnet>.ts.net:8775/`, which carries
  nothing, and never in an iframe. `make browser` now drives it: a scratch page
  on another origin opens the call with `target="_blank"`, and the new tab signs
  in and calls; a framed copy is refused by the browser and cannot sign in.
- **The agent contract** (PHONE-106 part 2a, folded from PHONE-72), documented
  in the README as the one supported hand-off. Part 2b, the call mails,
  remains on PHONE-72.
- **Chat goes to the agent as signed mail.** Chat messages, held contact
  requests, reactions, and taps when `DC_XDC_IN` is unset are sent by
  sessionmail's `message send` as the bot's seat `<agent>-chat`. They are signed
  and never unsigned. The send gets `DC_SIGNER_SOCK`/`DC_SIGNER_PUB` as its key,
  and every `CLAUDE_SESSION_*` the bot inherited is stripped. A subject's
  backtick becomes `'` and its `$(` becomes `$ (`. The channels alert goes the
  same way.
- **Headers an agent can filter on.** The new headers are:
  - `X-Delta-Chat-Kind` (`message`, `request`, `reaction`, `webxdc`, `call`);
  - `X-Delta-Chat-Msg-Id` (message, request and reaction);
  - `X-Delta-Chat-Sender` (the Delta contact's address);
  - `X-Delta-Chat-Channel` (the `channels.txt` NAME, when the chat is listed).

  The reply hint now reads `dc-say <NAME>`. For a chat that is not listed, it
  says so and how to list it, instead of giving a hint that would be refused.
- **A mail that fails is kept, not lost**, in `DC_UNDELIVERED`, else
  `<DC_ROOT>/undelivered/`.
  - Retried, by the refusal's kind: `identity`, `mta`, `undeliverable`, and
    the bot's own `exec` and `timeout`. The backoff starts at 60 s and doubles
    to at most 1 h, oldest first, and a failing mail never blocks the next.
  - A pass stops at the first `timeout` or `mta`, so a hung MTA holds the
    agent's replies for one timeout at most.
  - Any other kind, a mail still failing after 24 h, and an unreadable record
    go to `undelivered/dead/`. Nothing there is deleted.
  - Logged as `BRIDGE MAIL FAILED/RETRY/HELD/SENT/DEAD`. `dc-live` and
    `bin/bridge-check` report the waiting and dead counts.
- **`xdc-in.jsonl` line format v1.** `ts`, `msg_id`, `serial` and `payload`
  are frozen. `chat_id` (int) and `info` (when the update has one) are
  appended after them.

- A `dc-*` tool never queues a job the running bot would misread. The bot
  stamps the job keys it reads in `<outbox>/.bot` at start; a tool whose job
  has a key the running bot does not read exits 3, says "restart the bot",
  and writes nothing. With no stamp (a bot older than this release, or none
  running), a job to a chat number still goes, and a channel NAME or
  `--voice-message` is refused until the bot is restarted (PHONE-129).
- `sessionphone channels seed`: the upgrade step for a bot root with no
  `channels.txt`. It runs `bot install`'s own seed (the callers file's owner
  line, into a missing or empty list) and never touches launchd. A list with
  a channel is left alone; no owner line, or a loose or unreadable list,
  exits 2 and writes nothing. The bot never seeds by itself (PHONE-129).
- A channels list that refuses every send (missing, unreadable, or writable
  by group or world) now mails the bot's agent (`DC_MAILTO`) ONCE, by
  sendmail, never by Delta: the path, the reason and the exact fix command.
  One mail per reason per bot start, again when the reason changes, and not
  again within the hour for the same reason; a refused mail is retried every
  5 minutes. A `dc-*` tool that exits 3 for the whole list says on stderr
  whether the agent was mailed, or why not, from `<outbox>/.channels-alert`
  (PHONE-129).
- Named channels with an allow-list: the bot sends only where its owner
  said it may. `channels.txt` in the bot's root (`DC_CHANNELS_FILE`
  overrides), one `NAME TARGET  # note` per line; TARGET is an address (that
  contact's 1:1 chat, never a group) or `chat:N`. Every `dc-*` tool takes a
  NAME wherever it takes a chat; a number works only if it is listed. A send
  to an unlisted chat is refused, loudly, and nothing goes out; the list
  failing closed (missing, unreadable, or writable by group or world) refuses
  every send. `sessionphone channels [list|add]` manages it; `link` and `bot
  install` seed the owner. Every path that puts words in a chat is gated: the
  outbox, the in-call posts, mail-to-chat, the transcript, and the barge-in
  remainder (PHONE-102).
- Exit codes for a gated `dc-*` send: **0** sent, **3** refused (or no bot
  took the job), **4 NOT CONFIRMED** (the bot took the job and wrote no ack,
  so it may not have gone; a bot older than this release lands here). No
  refused send exits 0.
- The public profile gates inbound with the same list: a message, contact
  request, reaction or webxdc tap from an unlisted chat is not mailed to the
  agent or accepted. It is recorded in `rejected.jsonl` beside that list (who
  and why, never the text) and listed by `sessionphone channels rejected
  --profile public`. The private profile's inbound is unchanged. A rejected
  message is still marked seen, so when the bot's account sends read
  receipts, the stranger may see their message as **read**.
- `dc-send-file --voice-message` sends the agent's own audio as a Delta voice message, and an outbox asks for one with `"voice_message": true` on a file job (PHONE-118).
- An outbox `group` job makes a Delta group of contacts the profile knows, and sends nothing: its ack names the new chat and how to list it, so the first words go by `dc-say` through the allow-list (PHONE-94).
- **A web call's turns wake the seat** (PHONE-138). With
  `sessionphone web --legs`, each finished turn of the caller's is mailed to
  the seat with a Delta call turn's headers, Reply-To `calls+web-<N>@HOST`,
  so a plain reply is spoken into the call.
  - The server follows the ears' heard log for its own `call:N`.
  - Each turn is mailed exactly once: by `seq`, with a fixed Message-ID.
    An echo of the call's own speech is dropped and logged.
  - New flags: `--mail-to ADDR` (default `$DC_MAILTO`, else
    `$DC_AGENT@HOST`) and `--heard-dir DIR`.
  - The mail is M5, unsigned, through the one shared sendmail sender, until
    SM-716.
  - **Do not run `ears-mail --to seats` beside it**, or each turn arrives
    twice.
- `dc-delete MSG_ID...` deletes for everyone a message the bot sent: deleted here, and every member's app is asked to delete it (it cannot unsend what was read); one chat, all or nothing, refused (exit 3) for a message this account did not send or a chat the allow-list does not permit; an outbox writes `"delete": [ids]` (PHONE-122).
- `sessionphone avatar [PATH | --clear]` shows, sets or clears the bot's Delta Chat picture, and works while the bot runs (the bot takes it as a job); the picture shows after the bot's next message (PHONE-126).
- **A guest with no pinned key can be known through sessionmail's registry**
  (PHONE-156). When the `From:` has no pinned key in `call-guests.txt` and
  is on an address-only row, or its local part is a `voices.json` key, the
  bridge runs `message verify --trust --require-blessing`. The sender is known
  only on exit 0 with a last line of exactly `TRUST: FULL`, which means a
  registry cert the owner blessed. UNBLESSED never counts. There must also be
  one `ok` `registry-cert` line whose `agent:` is the whole `From:` local part.
  A pinned row is unchanged and never falls back to the registry. With no
  registry, this rung knows no one: the bot still starts, each such mail is
  unknown, and a run logs `registry trust is OFF` once. A plus-address seat is
  never known by the registry until sessionmail SM-803.
  `SESSIONMAIL_MHS_ORACLE` is never passed to `message verify`.
- `dc-xdc-realtime MSG_ID PAYLOAD_JSON` sends realtime data into a webxdc app the bot sent: compact JSON bytes, at most 128000 (bigger is refused, never cut), to every peer that has the app open now, and to no one else; nothing confirms delivery. The first send to a message advertises first; `--advertise` advertises again. The outbox jobs `webxdc_realtime` and `webxdc_realtime_advertise` are gated like `webxdc_update` and handed to one realtime sender thread, so a slow send never holds up messages, taps or calls; if that thread dies the bot says so once and refuses later realtime jobs, and keeps running. The tool ships in the wheel (PHONE-165).
- `sessionphone app list` and `sessionphone app build guide-trail [-o PATH]`: the Guide Trail, a webxdc app that shows a conversation's words as they are spoken, now ships in the package. `build` writes a deterministic `.xdc` (default `$DC_ROOT/apps/guide-trail.xdc`, `index.html` at its root, no `webxdc.js`) for `dc-send-file`. It reads `{"partial"}`, `{"final"}` and `{"lines": [{"who": "user"|"agent", "name", "text"}]}` payloads and renders every string as text, never markup (PHONE-165).
- `dc-xdc-realtime --feed MSG_ID` streams the heard log live into a webxdc app the bot sent (the Guide Trail): the owner's mic partials and turns, and this agent's sayings, as `{partial}`, `{final}` and `{lines}`; `--feed MSG_ID --stop` stops it. A feed binds only a chat listed in the new `DC_XDC_FEED_CHATS` (unset by default, so no feed binds; list only your own 1:1 chat), at most `DC_XDC_FEEDS_MAX` (default 2) at once. Bindings survive a restart (`xdc-feeds.json`, beside the outbox) and resume from the end of the log, never replaying; a feed ends when its app msg is deleted or after 3 failed sends. The feed runs on its own thread and sends through the one realtime sender, so it never holds up messages, taps or calls. In the Guide Trail, words that arrive after the 40-row cap pushed the live line off the page start a new live row instead of vanishing (PHONE-165).
- `DC_XDC_RT_IN=PATH`: realtime data that a webxdc app sends back to the bot is written to `PATH`, one JSON line per datum, `{ts, msg_id, seq, via: "realtime", payload, chat_id}` with no `serial`, and is never mailed. Unset, the default, it is counted and dropped. Only an app this bot joined (advertised on, sent realtime data to, or feeds) is heard; a datum must be UTF-8 JSON, state payloads and `DC_XDC_MATCH` drop as for taps, the public profile's inbound gate applies (asked once a minute per msg, kind `webxdc-realtime`), and at most 20 a second per msg are taken. It is a sibling of `DC_XDC_IN`: `xdc-in.jsonl` and its v1 line are unchanged, and a `DC_XDC_RT_IN` that names the same file is refused at start (PHONE-165).
- `sessionphone invite --qr` prints the invite as a QR to scan off the terminal (Unicode half-blocks, no colour codes), then the link; `--invert` for a light-theme terminal; `--png FILE` writes it as a PNG and prints the path, for `show-me`. `sessionphone invite` with no flags is unchanged (PHONE-164).
- **Every call says whether it is kept** (PHONE-177). `record_notice`
  (`EARS_RECORD_NOTICE`, else `"record_notice"` in `ears.json`, else on) posts
  one line into the call's chat when it connects, on every call: "This call is
  not being kept: audio is deleted when the call ends." with recording off,
  "This call is being recorded and kept for N days." with it on, or "This call
  is being recorded and kept." when `record_days` is 0.
  `record_spoken_notice` (`EARS_RECORD_SPOKEN_NOTICE`, else
  `"record_spoken_notice"`, else on) says "This call is being recorded and
  kept." before the greeting, only on a call that is being kept; a speech
  failure is logged and the greeting goes on. The two are independent. Values
  are read as `record`'s, but any other value turns the notice on, and
  `bot.log` names the layer. Neither has a command-line flag.

### Changed

- The tests that guard against a person's name no longer spell out any name.
  The `sessionphone web` guard reads the private word list that `make scan`
  uses, from the file `SESSIONKIT_SCRUB_RULES` names. Without that variable
  those tests skip, and the run's summary counts them and names the variable.
  The recording-name guard checks a shape instead: a recording file is named
  for the caller (`caller-*`), never for anyone else.
- Release metadata for 0.2.0 (PHONE-148): Mike Bailey is the author, the GitHub repository is the homepage, and the classifier is Alpha.
- The README's examples (JSON lines, `channels.txt`) and the code comments that quote them use synthetic UTC times and made-up ids. No example carries a local zone offset or a real time.
- **`DC_STT_URL` defaults to VoiceMode's mlx-audio on `:8890`, not
  `ears serve` on `:2023`** (PHONE-18). The default is now
  `mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions`, the server the
  bot's voice already uses. It is the same STT for voice notes and for what a
  caller says. A bot whose job sets no `DC_STT_URL` and relied on
  `ears serve` posts to `:8890` once it runs this version (for the plugin,
  from its next `bot install`), and that install says `hearing: NO` unless
  mlx-audio answers there. To keep `:2023`, install with this one line:

  ```bash
  DC_STT_URL=http://127.0.0.1:2023/v1/audio/transcriptions sessionphone bot install
  ```
- **An image sent with typed text now shows its image line** (PHONE-34).
  The bridge used to mail only the typed text and drop the
  `[attachment: NAME (MIME)]` line whenever a message had text
  (`bot.py:277`), so a captioned picture never showed. The body is now the
  typed text, then the image line, then the image's text when there is
  some. A filter that keys on the body of a captioned image sees the new
  lines. Other files with typed text are unchanged.

- **Setup is the small first download, and the media pack comes at first
  use** (PHONE-10). On a slow link, the first thing a new user meets is now
  10.0 MB, not 52.5 MB (81% less). These are the wheels for CPython 3.13 on
  an arm64 Mac at macOS 14 or later, read from `uv.lock`.
  - Six requirements move out of the required dependencies into a new
    `media` extra: aiortc, aioice, av, numpy, websockets and cryptography.
    `pyproject.toml` and `uv.lock` change. No dependency is added and no
    version changes. The `dev` group carries the pack, so `uv sync` and
    `make test` keep the whole stack.
  - `bin/sessionphone` passes `--no-dev` on every run, so a user never gets
    the dev tools. `setup`, `qr`, `link` and the other light verbs fetch four
    packages: click, deltachat-rpc-client, deltachat-rpc-server and segno. A
    Mac with no Python 3.11 or later also fetches Python (25.3 MB, measured
    for CPython 3.13).
  - The media pack (30.8 MB) is fetched once, at the first `bot install`,
    `bot run`, `web` or `web enrol`. One line before it says what it is
    fetching and why. When a media verb is the first command of all, that
    fetch also brings the light four: 40.8 MB, or 66.1 MB with Python. If
    the fetch fails, the launcher says so and exits 2. Nothing else changes,
    and setup, qr and link still work. Run the same command again: uv keeps
    the packages it finished (inferred, not measured).
  - A media verb run without the pack refuses (exit 2) with a plain message.
    It says what is missing, why, how big it is and how to fetch it for the
    environment it runs in. It refuses before anything is written: no
    launchd job, no channels seed and no traceback.
  - pip and `uv tool` users ask for `sessionphone[media]` to run the bot or
    the web page. `make install` now runs `uv tool install -e '.[media]'`.
    A sessionkit install needs SK-76 (the upgrade notice above).

- **`sessionphone web` names the caller from one setting** (PHONE-135).
  `--caller-name NAME`, else `$DC_CALLER_NAME`, else `owner`, is the `user`
  in the `auth-ok` frame, the speaker in each turn mail's subject and
  `from:` line, and a new passkey's WebAuthn name. It was a fixed person's
  name. A passkey already enrolled still signs in after a rename. The DEMO
  refusals and the `--demo` help now say it waits for "the operator's GO".

- **`sessionphone web --legs` refuses to start (exit 2) without a seat.**
  It refuses when no `--mail-to`, `$DC_MAILTO` or `$DC_AGENT` names one, or
  when the To is not `BOX@HOST`. It also refuses when no `DC_HOST` is set and this
  machine's name is not a mail domain. `CLAUDE_CODE_AGENT` is not taken as
  the seat (PHONE-138).
- **A state update is never a tap.** On both sinks (`DC_XDC_IN` and
  tap-as-mail), a payload object whose `kind` is `state` or ends in `-state`
  is dropped and logged `XDC STATE`. It runs after the echo rule and before
  `DC_XDC_MATCH`, and it cannot be turned off, so the `-state` suffix is
  reserved for state. This also catches a push made before a restart, or by
  another process or device, which the echo rule cannot see (PHONE-106).
- The mail leg skips the bridge's own mail by its local part, `<agent>-chat`,
  whatever host the From is spelt with (PHONE-106).
- The README no longer says there is no postfix. There is no PUBLIC mail
  server: mail goes through the host's MTA, the one sessionmail uses.
- Tap-as-mail (`DC_XDC_IN` unset) is deprecated. `DC_XDC_IN` is the
  supported integration point. Nothing changes for a bot that still uses it.

- **A gated send waits for the bot's answer even under `DC_TAKEN_WAIT=0`**:
  it blocks up to 10 s, and with no bot it ends `NOT TAKEN`, exit 3, the job
  left in the outbox (remove it before you resend). It used to say `queued`
  and exit 0. `DC_TAKEN_WAIT=0` still applies to `dc-hangup` and `dc-answer`
  by chat number.
- A NAME may not start with `-` or be all digits (a tool reads an all-digit
  argument as a chat id).
- **Call recordings are named for the role, not a person.** In each call dir
  the far party's audio is now `caller-NNN.wav` (one per chunk),
  `caller-call.wav` (the whole call, with `DC_EARS=1`) and `caller-call.json`
  (its clock). These were `mike-NNN.wav`, `mike-call.wav` and
  `mike-call.json`. Calls recorded before the upgrade keep their old names.
  Nothing in sessionphone reads these files by name; a tool that does should
  read `caller-*` first, then the old name. **Depends on sessionvoice:** its
  speaker side-car reads `mike-call.wav` today, so it must read
  `caller-call.*` first (then `mike-call.*`), landed before or with this.
  Otherwise it names no call lines.
- A contact request from a stranger is mailed to the agent with "ask your
  owner before accepting", not a person's name.
- The fence stops mistakes, not hostile code: anything running as the owner's
  user can edit `channels.txt` or open the account directly. The bot logs each
  change to the list.
- The design contract's example launchd labels and a test fixture's job name
  use neutral handles: `com.example.phone-taps`, `com.example.phone-outbox`
  and `agent` (PHONE-157).
- The README's Quick start and Status now say what works on master, what does
  not, and what a voice call needs (an STT server, with ears optional); the
  skill names `bot install`'s STT refusal too, the repo gains `AGENTS.md` and a
  one-line `CLAUDE.md`, and the design notes drop private paths and host names
  (PHONE-44).
- The call mails, a Delta call's and a web call's, now go signed by
  `message send --message-id` as the bot's seat, never by raw sendmail
  (`bridge/sendmail.py` is gone); their ids use sessionmail's host and their
  From its domain, and the bot and `web --legs` refuse to start when
  `message` lacks `--message-id` or the key is another seat's (PHONE-72).

### Fixed

- **The suite is green in a git clone of the release export** (PHONE-182).
  The tests for the export-ignored CLAUDE.md and maintainers' notes now skip
  when the export dropped them, instead of failing because a `.git` is present.
- **The package has a description on PyPI** (PHONE-180). `pyproject.toml`
  named no `readme`, so the wheel and the sdist carried no long description,
  the sdist shipped no README, and `twine check --strict` failed on both.
  Now `readme = "README.md"`: both carry the README as Markdown, and both
  pass.
- **An ears call keeps the caller on `full-in.wav`** (PHONE-178). With
  `DC_EARS=1` and recording on, `full-in.wav` closed at 0.0s on every call:
  the ears path hands each frame to the tape as bytes, the tape expected an
  array, and the error was dropped without a word. `full-out.wav` was kept,
  so `dc-call-mix` mixed the call with no caller in it. The tape now takes
  either form, and a write that fails is named in `bot.log`, once per file.
  `caller-call.wav` was not affected.
- **Mail into a live call no longer trusts its own headers** (PHONE-155, SK-80
  P1). `X-Voice:` and a `From:` in `voices.json` made any sender known, in any
  voice; a forged `call-<chat>-<digits>@` root, or any id containing the live
  root, reached the call. See the behaviour change at the top.
- **`bot install` refuses a `DC_STT_URL` it can name no model for**
  (PHONE-151). Before this, a URL whose kind the bridge does not know (no
  `provider+` tag, no known port), with no `DC_STT_MODEL`, was installed
  anyway. The bot then refused to start, and launchd restarted it every 30 s.
  Now install exits 2 before it writes anything (no plist, no callers or
  channels change), as it does for a junk mail host, and names both settings:
  tag the URL or set `DC_STT_MODEL`. It is the same check the bot runs at
  start, so the two cannot disagree.
- **A name that matches no contact, or several, is told in words**
  (PHONE-64). `dc-ring nobody@example.org` printed `FAILED - ValueError:
  call_to: 0 contacts match 'nobody@example.org'`, and `dc-dm` and a group
  job's members did the same. Now the tool says who was not found and what
  to do next (check the spelling or address, or add the contact first), or
  lists the contacts that match as `Name <address>` and says to name one by
  its address or a `channels.txt` entry. It still exits 3. Any other error
  keeps its class name.
- **bot.log's ears line only when previews or ears are on** (PHONE-152).
- **A bot without the media pack refuses calls plainly and keeps chat**
  (PHONE-145). Before this, a bot started without the pack died at import
  with a `ModuleNotFoundError` traceback, chat-only bots (`DC_CALLS=off`)
  included. Its launchd job then restarted it every 30 s, and it died the
  same way each time. A bot whose pack was removed while it ran acked a
  `dc-call` as "dialling", then logged a traceback, and hung up on an
  incoming caller without a word. Now a chat-only bot never loads the pack,
  and `bot install` and `bot run` work without it when `DC_CALLS=off`. A bot
  with calls on and no pack comes online and says so once. It answers each
  call job with `ok: false` in plain words that say how to fetch the pack
  and that the bot must then be restarted. It refuses an incoming call the
  same way it refuses an unapproved caller. A pack removed under a live bot
  is checked before each call starts, and anything that slips past that
  check is logged in one line, not a traceback. A `dc-ring` job (`call_to`)
  on a calls-off profile is now refused like the other call jobs; before
  this it got past the refusal.

- **The RINGING mail names a `dc-answer` you can run** (PHONE-56). Its
  "Pick up" and "Decline" lines, and its subject, used to name `dc-answer`
  and `dc-hangup` by bare name. The package puts no `dc-*` tool on any PATH,
  so a reader without the repo's `bin/` on PATH got `command not found`, and
  the call went to voicemail. The mail now names the bridge checkout's own
  `bin/dc-answer` and `bin/dc-hangup` by absolute path, shell-quoted, with
  `--profile` after it as before. It no longer names whatever copy comes
  first on your PATH (e.g. a personal `~/bin/dc-hangup`). A bridge with no `bin/`
  beside its source (a wheel install) still names the bare tool.
- **A wheel install can answer a call** (PHONE-150). The wheel shipped no
  `dc-*` tool, so the bare `dc-answer` that a wheel install's RINGING mail
  names was `command not found`. Now the wheel installs `dc-answer`,
  `dc-hangup`, `dc-say` (the reply that bridged mail names) and the `dc-env`
  they source into the environment's `bin/`. `uv tool install sessionphone`
  links all four into `~/.local/bin`. They are `bin/`'s own files, not ports
  or wrappers. `dc-env` now runs its checks with the Python beside it when
  there is no checkout `.venv`, so a tool reached through `~/.local/bin`
  still finds `sessionphone`. A checkout runs exactly as before.
  **`make install` now puts the same four names in `~/.local/bin`** (as
  copies taken at install time; the RINGING mail from that bot still names
  the checkout's `bin/` by absolute path). If `~/.local/bin` already holds a
  `dc-answer`, `dc-hangup`, `dc-say` or `dc-env` that no uv tool owns,
  `uv tool install` refuses: `error: Executable already exists: dc-answer
  (use --force to overwrite)`. Move your own copy, or pass `--force` if it
  is a stale one you mean to replace.
- **A web call's dropped socket is one log row, not a traceback**
  (PHONE-137). When a phone locks or the network goes mid-session, the socket
  drops with no close frame. `sessionphone web` now logs one JSON row,
  `{"conn": "closed", "code": 1006, "during": "auth"|"idle"|"call"}`, and the
  call still ends. It used to print the websockets "connection handler failed"
  traceback. A clean close, or a refusal the server sent, adds no row.
- **A web call's start and end are JSON rows beside the auth rows**
  (PHONE-137): `{"call": N, "event": "started", "mode": "echo"|"legs"}` and
  `{"call": N, "event": "ended", "why": ...}`, stamped with `ts`. They replace
  the plain `WEB call N started` / `ended` lines, so one pane tells the whole
  story.
- **`sessionphone web` refuses `--mail-to` and `--heard-dir` without
  `--legs`** (exit 2, PHONE-137). Echo mode mails no one; it used to ignore
  them silently.
- `dc-log` reads the bot's own log: `DC_LOG`, else `<root>/bot.log`, with the
  root from the env contract (`DC_ROOT`, else `SESSIONPHONE_ROOT`, else
  `~/.sessionphone`). With no agent variables set it used to read a fixed
  agent's tree, `~/.<agent>/delta-chat/bot/bot.log`. If you set `DC_LOG`, nothing
  changes for you. The default moved from `<root>/bot/bot.log` to
  `<root>/bot.log`, the file the bot writes: if only the old file is there,
  `dc-log` reads it and says so on stderr, so it never goes silently empty.
- `dc-send-file` refuses a `PATH` in a missing directory by the name you gave
  it, and queues nothing. It used to turn `missing-dir/x.ogg` into `/x.ogg`
  and, if that file existed, send it. `dc-call-mix CALL_DIR` names a missing
  directory too, and with no argument it names the calls directory it searched.
- `dc-log-web` writes its page to `DC_WEB_OUT`, else `<root>/calllog.html`.
  The old default was a directory from the author's own setup; set
  `DC_WEB_OUT` to keep a page where you serve it. `-h`/`--help` now prints
  usage and writes nothing (it used to run the loop and write the page), and
  an unknown argument exits 2.
- Every file in `bin/` answers `-h`/`--help` with usage, exits 0, and writes
  nothing. `bridge-check --help` used to run the whole check, and
  `dc-call-mix --help` went looking for a call. `dc-env`, the file the tools
  source, now answers when run directly and refuses any other direct run.
  `sessionphone --help` now reads from the venv without `uv run`, so it no
  longer fills uv's cache. A test walks `bin/`, so a new tool without help
  fails it.
- On the public profile, the `XDC SKIP` and "update without a serial" log
  lines no longer carry any of a stranger's payload.
- First contact no longer fetches CPython 3.13 (25.3 MB) on a Mac that
  already has Python 3.11 or later. The repo shipped a `.python-version`
  pinning 3.13, a scaffold default, so uv ignored an installed 3.14. It is
  gone; uv now picks an installed Python inside `requires-python = ">=3.11"`,
  the floor the suite was measured green on (3.11 to 3.14). A test fails if
  a `.python-version` ships again (PHONE-146).
- Every STT request now names its model, so an mlx-audio (Parakeet)
  endpoint transcribes voice notes and call audio instead of refusing
  them with a 422. `DC_STT_MODEL` sets the model. Unset, it suits the
  endpoint's kind, which a `provider+` prefix on `DC_STT_URL` names
  (`mlx-audio+http://...`, stripped before use) or the port suggests
  (8890 mlx-audio, 2022 whisper, 2023 apple/ears). The mlx-audio,
  whisper and openai defaults are VoiceMode's (VM-2470); the apple row
  (`apple-speechtranscriber`, ears' model id) is one VM-2470's table
  lacks yet. With no kind and no model, the bot refuses to
  start and names `DC_STT_URL` and `DC_STT_MODEL`; `whisper-1` is never
  sent as a catch-all. An STT failure is logged with its HTTP status and the
  server's body, including a 200 with no text. Before, a refusal left
  no log line at all (PHONE-110).
- **A call turn with no real word no longer wakes the seat** (PHONE-158):
  ".", "Music.", "Um", "Uh." are dropped on the bridge and web legs, one
  `NOISE ... | dropped:` log line each; "yes", "go", "Hmm, yes" still mail.
- **`dc-* -h` prints the tool's usage, not its code** (PHONE-159): help ends
  at the first line that is not a comment, not at the first blank line, so
  the 11 tools whose header ran straight into code no longer print the whole
  script. A test runs `-h` on every `dc-*` in `bin/` and `wheel-bin/`.
- **On a call with pauses or lost packets, what the caller says now stays in
  time** (PHONE-167). Before, every short pause or lost packet put the
  recording of the caller, and what the agent hears, a little further behind
  the call, so a long call could run more than a minute late. Now the gap is
  filled with exactly as much silence as the pause lasted.
- **The docs link each other relatively, so they open offline** (PHONE-194).
  The README and `docs/` linked each other by full GitHub URLs on a branch
  called main, which the public repo does not have, so every one of those
  links was a 404. They are now relative links, which work in a clone, an
  editor or an unpacked archive. The one link to another sessionkit repo
  names `HEAD`, not a branch, so GitHub opens that repo's default branch
  whatever it is called. On the PyPI page a relative link does not work yet.

## [0.1.0] - 2026-10-10

### Added

- sessionphone, born 2030-06-09.
- Lines the bridge speaks into a call go in VoiceMode's heard log as a
  `saying` row when they start and a `said` row when they end, the same
  pair the mouth writes. A heard.py without `said()` is noted once and
  skipped. Ported from the bridge this repo was copied from.
- `DC_XDC_IN=PATH` sends webxdc taps to a JSON-lines file instead of mail.
  Each tap is one line, `{"ts", "msg_id", "serial", "payload"}`, written with
  a single `os.write`. The file is 0600 and its directory 0700. A failed write
  is logged, and the tap is lost rather than mailed. Unset (the default), each
  tap is mailed to `DC_MAILTO` as before. `DC_XDC_MATCH=key=value[,...]`
  optionally narrows which updates count as taps, for either sink.
  `bin/dc-xdc-update` reads `DC_XDC_SUMMARY` and sends it as the update's
  webxdc `summary`. Ported from an earlier bot (PHONE-22, gaps 1 and 3).
- Ring first, answer second: `DC_ANSWER=ring` mails the agent `RINGING`
  and holds the call for `dc-answer CHAT_ID` (new) up to `DC_RING_WAIT`
  seconds (default 25), then answers as voicemail with
  `DC_GREETING_VOICEMAIL`. The hold comes before the peer connection
  exists, so no ICE check goes into an unanswered call and a ringing call
  is never "live" to `dc-say`, the mouth or the ears. `dc-answer` waits
  for the bot's yes or no. `dc-hangup` declines a ringing call. A call
  that ends or is declined while it rings is never accepted, and the
  agent gets a `MISSED` mail. `DC_ANSWER=auto`, the default, answers at
  once, as before.

### Fixed

- A call that ended while its peer connection was being made was still
  accepted (then failed with InvalidStateError). The bot now remembers the
  close and never accepts it.
- A `DC_RING_WAIT` that is not a number falls back to 25 and says so in
  the log, instead of stopping the bot on import.
- The bot names its RPC server (`server_path()`, beside this interpreter), as
  setup already did. On a bare Mac the launchd bot from `sessionphone bot
  install` found no server on PATH and crash-looped every 30 s (review B1).
  `tests/test_rpc_server_path.py` pins every `Rpc(...)` in the package.
- A TTS-failure fallback is posted in the chat as text and is never spoken
  into a live call. The fallback job carries `no_speak`, and the outbox loop
  skips `speak_if_live` for it. Before this, the marker text "(unspoken, TTS
  failed)" was voiced, nesting one level deeper on each failure. Ported
  from the bridge this repo was copied from; that fix postdates the copy.
- A restart no longer replays a webxdc app's whole history. A status update
  is now fetched from the event's own `status_update_serial` (measured on
  rpc-server 2.62.0), not from an in-memory counter that is 0 after every
  restart. An event with no usable serial is logged and nothing is fetched.
  There is never a fallback to serial 0. One bot mailed 48 and then 51
  replayed taps (PHONE-22, gap 2).
- A live incoming message is `remember()`ed and then marked seen, whether it
  is bridged or held (gap 4). A held contact request is mailed with its full
  `message_text()`, so a voice note arrives as its transcript and a reply
  keeps its quote (gap 5). It is remembered, so the next startup sweep does
  not mail it a second time (gap 6).
- A dc tool reports what the bot did, not just that a job was queued. Each
  tool waits for a bot to take its job (`DC_TAKEN_WAIT`, 10 s; 0 = don't
  wait). If no bot takes it, the tool says NOT TAKEN, exits 3 and leaves the
  job in place. The bot then answers every job in an ack file under
  `DC_ACKS`: ok with what it did ("sent to chat 13, msg 77"), or failed with
  the reason. It writes the ack before it unlinks the job. The tool prints
  that answer and exits 3 on a failure. A bot that writes no ack is reported
  as NOT CONFIRMED, never as sent. Before this, two messages to a contact sat in an
  outbox no bot read while the tool said "queued", and one was reported to
  the user as sent. A send the bot logged as SEND
  FAILED was reported the same way. `dc-call`, `dc-hangup` and `dc-invite`
  fail when there is no live call to act on, or one already in the way.
  Every job is written whole and renamed into the outbox.

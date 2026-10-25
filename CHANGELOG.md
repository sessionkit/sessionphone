# Changelog

All notable changes to sessionphone are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The one
version lives in `pyproject.toml`; `scripts/release.py` bumps it with
`uv version`, mirrors it into `.claude-plugin/*.json` when present, and
stamps this file.

Changes since the last release are in `changelog.d/`, one file each, not in
this file; `changelog.d/README.md` says how to write one. `make changelog`
prints them as the next release will read. The release moves them in here,
under the new version.

## [Unreleased]

## [0.3.1] - 2026-10-25

### Changelog entries are written one file per change

- A change now writes its changelog entry to its own file,
  `changelog.d/<TASK-ID>-<slug>.md`, and no longer edits `CHANGELOG.md`.
  Two changes in flight never clash on one file, and an entry can no longer
  merge cleanly under a version that has already been released.
  `changelog.d/README.md` says how to write one.
- `make release` collects the pending entries under the new version, in
  file-name order with numbers compared as numbers, then removes them in the
  release commit. It refuses a malformed entry, or a file under
  `changelog.d/` it would not read, before it bumps the version.
- `make changelog` prints `[Unreleased]` as the next release will write it,
  pending entries included. It writes nothing.

### What sessionphone depends on, in one place

- `docs/dependencies.md` lists sessionphone's direct dependencies and the
  tools a user meets: what needs each one, by command, whether it is
  required or optional, how it gets onto the Mac and how to upgrade it. It
  links `uv.lock` for the full resolved set.
- Its ffmpeg entry says what fails without it: with the speech server up,
  a call's spoken replies go to the chat as text and the caller hears
  nothing.
- The README has a short `## Requirements` section: the operating systems,
  and a link to that page.
- `.github/dependabot.yml` asks for a daily check of the Python
  dependencies in `uv.lock`.

### Check callers.txt and channels.txt before the key upgrade

- The first start on the key-bound version pins only the owner line: in
  `callers.txt` the line whose note starts `owner`, in `channels.txt` the
  line named `owner` or noted `owner`. A list written by hand may have no
  such line, and then nobody can ring until a key is pinned by hand. Add
  `# owner` to the owner's line before you upgrade.
- The upgrade skips a list that group or others can write, and every address
  line in it stays refused, the owner's included. 0.3.0 never checked the
  mode of `callers.txt`, so a loose one that rings today stops ringing. Run
  `chmod 600` on both lists before you upgrade.
- One `callers.txt` shared by two bots (`DC_CALLERS_FILE`) is upgraded once,
  by whichever bot starts first, counting key contacts in its own account
  only. The other bot's owner stays unpinned. Give each bot its own file, or
  write each owner's `key:FPR` on its line by hand, with both bots stopped
  and upgraded.
- The README's "Callers" section says all this under "Before you upgrade,
  check each list", with how to tell which lines will stay unpinned.

### The callers verbs and `link` use the bot's own callers file and account

- `sessionphone callers` (the list, `add` and `pin`) and `sessionphone link`
  wrote `<SESSIONPHONE_ROOT>/callers.txt` and opened
  `<SESSIONPHONE_ROOT>/accounts`, ignoring `DC_CALLERS_FILE`, `DC_ROOT`,
  `DC_PROFILE` and `DC_ACCOUNTS`, which the bot honours. On a bot run with any
  of them, an approved or pinned caller landed in a file the bot never read,
  and `link`, or a pin without `--key`, used another account.
- They now resolve both exactly as the bot does, through one shared
  resolver: `DC_CALLERS_FILE`, else `<home>/callers.txt`; `DC_ACCOUNTS`, else
  `<home>/accounts`. Run them with the bot's settings to reach that bot.
- Each names the file it read or wrote (`callers: <file>`, or `in <file>` on
  the result line), and `link` and a lookup without `--key` print
  `account: <folder>`.
- `sessionphone setup` opens that same account, so it and `sessionphone
  invite` keep the account's invite in `<home>/invite.txt`, where the bot
  rewrites it, and no longer over the default profile's.
- The bot's "nobody may ring" and "`DC_CALLERS` is IGNORED" lines no longer
  say to hand-edit a callers file outside the root: they name the
  `sessionphone` command, with the settings it needs in front.

### A release no longer re-locks the dependencies

- The version bump runs `uv version` with `--frozen`, on a dry run and a
  real one, so a release resolves nothing and changes no dependency pin.
  A release cut before a dependency's new version is on PyPI no longer
  fails, and a local package index in the environment can no longer end up
  in the committed `uv.lock`.
- The release writes the new version onto the project's own line in
  `uv.lock`, and changes nothing else in it, so `uv lock --check` passes
  after a release. It refuses, before it writes anything, a `uv.lock` that
  does not hold exactly one such line.
- A release refuses an existing tag before it bumps the version, so a
  refused release leaves the tree clean.
- `make release` runs the script with `python3`, not `uv run`, which locked
  the project before the script started.

### Install no longer says a session will not hear its own box

- With `DC_MAILTO` naming the installing session's own box (its session
  id), `bot install`'s `hear:` line said `this session will NOT hear` it,
  and put that warning first, though the session did hear the box. The
  line now says `this session hears box <id> (its own session box)`, and
  `--hear-here` says it is not needed. Another session's box, even one
  whose id starts the same, is still reported as not heard.
- When the bot's name came from the default and nothing hears its box,
  install now says so in one `default:` line, which names the fix: start
  a bare `session start` first. It asks sessionmail who hears the box,
  read only, and says nothing when the answer is not plainly "nobody".
- The skill and the README's quick start now say in plain words where
  Delta text lands (the bot's own box, named after its agent), how a
  session hears it, and what `DC_MAILTO` changes.

### With nothing naming it, the bot takes the harness vendor's name

- When no name is given (no `DC_AGENT`, no durable seat, no bot recorded in
  `phone.json`), the bot was always named `claude`. Its name now comes from
  the harness vendor, as `session agent default` prints it, so the bot and
  the default seat are named alike. The `dc-*` tools pick the same name.
- phone 0.3.1 works with sessiontools 0.3.0: when `session agent default`
  is missing, fails, prints no single word or does not answer within a few
  seconds, the name falls back to `claude`, as before.

### The agent's box is a bare name, so it survives a renamed machine

- Delta text went to `<agent>@<host>` by default, with this machine's name
  baked in. Once the machine was renamed, every mail to that address was
  refused, and `setup` and `bot install` printed the host-qualified address
  as the one to use.
- `DC_MAILTO` now defaults to the bare agent name, such as `claude`.
  sessionmail adds this machine's host when it delivers, so the box keeps
  working after a rename. A `DC_HOST` no longer changes that default.
- A `DC_MAILTO` that names a host is kept exactly as given: name a host only
  for a box on another machine. A bare `DC_MAILTO` is a box on this machine,
  so the bot makes it at start instead of refusing it on the mail drop.
- `sessionphone web --legs` accepts a bare `--mail-to` or `DC_MAILTO`, and
  its default seat is the bare `DC_AGENT`.
- The `hear:` hints, the README, the settings docs and the skill now show
  bare box names (`DC_MAILTO=BOX`).

### A call turn the mail system refuses is kept and retried

- A call mail that `message send` refused was logged and dropped, so every
  turn of a call could be lost while the caller kept talking. Call mails are
  now kept in the undelivered spool and retried like any other agent mail.
  A call's preview is still not kept: the turn carries its whole text.
- A call mail is never delivered twice. Before a retry, the bridge reads the
  agent's box, read-only, for the mail's own Message-ID, and does not send
  it again if it is there. A mail that may already have gone, to a box the
  bridge cannot read, goes to `undelivered/dead/` instead of being resent.
- `sessionphone bot status` now prints an `undelivered:` line with the mail
  waiting and dead in the bot's spool, call turns included.

### The caller hears when a call turn does not reach the agent

- When a turn's mail was refused, the call said nothing, and the caller
  kept talking into silence. Now the call says "I couldn't reach the
  agent." once for that turn: never once per chunk, and not again when the
  bridge retries it. A call that is hanging up says nothing.

### Reinstalling the bot no longer drops a live call

- `sessionphone bot install` and `bot uninstall` stop the bot, and the call
  it holds dropped with no warning. While a call is up they now refuse,
  exit 2, with `a call is live; hang up or pass --force`, then the call.
  Nothing is written or stopped.
- `--force` restarts or stops the bot anyway, and the call drops.
- A `call-live.json` left by a bot that was stopped mid-call is not counted
  as a call, so it never blocks a later install.

### Setup checks speech-to-text before the phone is linked

- `sessionphone setup` now ends with a `hearing:` check: it sends a
  half-second test clip to the speech-to-text server the bot will use
  (`DC_STT_URL`, else the default) before the phone is linked. When nothing
  answers, it names the install step: under Session Kit
  `ears services install --only speech`, standalone VoiceMode's. Before, a
  missing speech server was first reported at `bot install`, the last step,
  after the link. Setup's exit codes do not change.
- `sessionphone bot status` now gives one `hearing:` line for the job's
  speech-to-text server, capped at a few seconds. A server that stopped
  answering after install is reported, with the URL that did not answer.

### `dc-say --text-only` posts to the chat without speaking into a call

- A plain `dc-say` is read aloud into a call that is live in the chat, so a
  report meant for the chat was spoken into the owner's call.
- `dc-say --text-only CHAT "text"` now posts to the chat and never speaks.
  The ack ends `NOT spoken (chat only)`.
- `--text-only` with `--call` is a usage error (exit 1), and nothing is
  queued. A `dc-say` without the flag behaves as before.

### A call reply waits for the caller to finish speaking

- A reply spoken into a call (a mouth line, a `dc-say`, a mail into the
  call) started the moment it arrived, even in the middle of the caller's
  sentence. Each reply the caller talked over made a new turn, which drew
  the next reply, and the replies stacked up.
- A reply now waits until the caller has been quiet for `DC_FLOOR_GAP`
  seconds (0.8 by default), then plays. It waits `DC_FLOOR_CAP` seconds at
  most (6 by default), so noise on the line cannot keep the agent silent.
  `DC_FLOOR_GAP=0` turns the wait off.
- Each reply that waited logs one `FLOOR` line: how long it was held, and
  whether quiet or the cap released it. The bridge's own lines (the
  greeting, the holding line, the notices) do not wait.
- The skill now says how an agent takes turns on a call: one `dc-say`
  per caller turn, a sentence or two; no reply to a fragment that asks
  nothing ("No.", "Okay."); never a second `dc-say` before the caller
  speaks again, and anything more goes as text in the chat, with
  `dc-dm` (a `dc-say` to that chat is spoken into the call).

### A bot stopped mid-call no longer leaves a dead call reported as live

- When launchd stopped the bot during a call (a bootout, or an install over
  a running bot), the call was never ended and `call-live.json` stayed. The
  next bot left it there, so `dc-live` and `dc-say --call` reported the dead
  call as live until the next call.
- On SIGTERM the bot now ends each call it holds, so the phone hangs up too,
  and removes `call-live.json` before it exits, well inside launchd's grace.
- At start, once it holds the account, the bot removes a `call-live.json`
  older than itself and says so once in `bot.log`.
- `dc-live` no longer believes a `call-live.json` whose call began before
  the running bot started: it says no call is live (exit 1, the reason on
  stderr), and `dc-live --id` prints nothing.
- The bot writes `call-live.json` whole (a temporary file renamed in), so a
  stop in the middle of the write cannot leave a torn file.
  A temporary file left by a bot killed in that moment is removed at the
  next start.

### Upgrade from 0.3.0: its caller check trusted the sender's address

- In 0.3.0 the caller check trusted the sender's address, and an address
  could be faked. A call could reach the bot from someone the owner never
  approved.
- Upgrade to 0.3.1, which checks the caller's key instead. Until you can
  upgrade, stop the bot.
- Read the upgrade notes first: "Before you upgrade, check each list" in the
  "Callers" section of `README.md`, and the entry "Approved callers are bound
  to a key fingerprint".

### A kept call always tells the caller it is kept

- When a call was kept with the chat line off and the spoken notice failed
  (no ffmpeg, a speech server down, a render error), the caller got no
  recording notice at all. The same happened with the chat line on when it
  could not be sent and speech failed too.
- On a kept call where neither notice reached the caller, the notice is now
  posted once as chat text before the greeting, in the chat line's own words.
  A notice cut short by a barge-in counts as not delivered. With the chat
  line posted, nothing more is sent.
- A chat line refused by `channels.txt` is not retried, and one not confirmed
  within 5 seconds is not sent again. Each case where a notice did not get
  through writes one `recording notice:` line to `bot.log`, ending in
  `delivered as text` or saying the caller was `NOT delivered` the notice and
  the call is still kept. What is recorded does not change.

### Every ring is logged the moment the bot sees it

- A call the bot answered wrote its first line, `CALL INCOMING`, only after
  the call's session had looked up the chat and caller names. If a call was
  missed and `bot.log` had no line for it, there was no telling whether the
  ring never reached the bot or the session stalled before that line.
- The bot now logs `CALL EVENT IncomingCall msg <id> chat <chat_id>
  video=<False|True>` for every ring, first, before it answers, refuses or
  ignores the call and before any lookup. The line carries no caller
  address.
- README's "Call troubleshooting" has a new part, "A missed call", on reading
  the two lines together.

### A long call turn reaches the agent whole

- A turn of more than 4000 characters was cut at 4000, with "..." added, and
  the rest was dropped. The agent read the "..." as the caller trailing off,
  and never learned what else was said. The cut came from the log-line
  helper, `one_line`, reused on the paths that carry heard words.
- Heard words are now flattened to one line and never cut, by a new helper,
  `flat`, on every path that feeds an agent: the Delta call's turn mail,
  preview mail and heard-log turn and partials (both the chunk path and the
  ears path), and the web call's turn mail and the echo check before it. The
  call's transcript keeps the whole turn as well.
- The caps that stay are display only, and none of them feeds an agent: the
  bridge's log lines (`one_line`, 400 characters by default), the turn mail
  Subject (44 characters of the turn, 34 of a preview, at most 69 in all),
  the web turn Subject (44, at most 69), and the heard feed into a webxdc app
  (`MAX_TEXT`, 4000 characters per payload).

### A call greets by the bot's own name

- A call greets by the bot's display name, the name `setup --name` gave it
  and people see in their chat list. A bot whose agent was the default said
  "Claude here" instead. `DC_AGENT_NAME` and `DC_GREETING*` still win, and the
  mail's From keeps the agent's name.

### A dc-say ack says "spoken" only when the line was spoken

- With a call live, a `dc-say` ack said `spoken into the call` as soon as
  the speech was scheduled. When the speech then failed (no ffmpeg, the
  speech server down), the ack still said it, and the failure was only in
  `bot.log`.
- The ack now waits up to 3 s for the speech. It says `spoken into the call`
  only after every sentence was rendered and handed to the call, and
  `NOT spoken: tts failed: <reason>` (or `NOT spoken in full: ...`) when it
  was not. A line still rendering is acked `queued to speak`; if it fails
  later, the bot mails the agent one line that starts `NOT spoken`.
- The 3 s is one wait per pass of the outbox, shared by every speaking
  `dc-say` in it, so a burst of them still acks inside `dc-say --call`'s
  8 s and every job is still taken inside 10 s.

### Approved callers are bound to a key fingerprint

- A `callers.txt` line is now `ADDR  key:FPR  # note`. The bot answers a
  call when the caller's Delta Chat contact holds the key with that
  fingerprint; the address is the label. A line with no `key:` is refused
  until a key is pinned to it.
- `sessionphone link` binds the owner line to the key of the contact that
  joined by the invite. `sessionphone callers add ADDR --key FPR` and the new
  `sessionphone callers pin ADDR --key FPR` take the fingerprint the phone
  shows under the contact's profile, Encryption; without `--key` they open
  the account (with the bot stopped) and pin only when exactly one key
  contact has that address. `sessionphone bot install --owner ADDR` takes
  `--owner-key FPR`. `sessionphone callers` shows each line's key, or
  UNPINNED.
- Upgrading: at its first start, the bot pins the owner line of a callers
  file from an older version to the owner's one key contact. It keeps the
  original as `callers.txt.pre-keys`, writes a marker line on top, and runs
  only once per file. Any other line, or an owner address with no key
  contact or with more than one, is left unpinned, and the log names the
  `callers pin` command for it.

### Address channels are bound to a key fingerprint

- A `channels.txt` address line is now `NAME  ADDR  key:FPR  # note`. The
  bot writes to, and on the public profile takes in, the 1:1 chat of a
  contact only while that contact holds the key with that fingerprint; the
  address is the label. An address line with no `key:` is refused until a
  key is pinned to it. `chat:N` lines are unchanged and take no key.
- A channel NAME (dc-say, dc-dm, dc-ring) opens the chat of the contact that
  holds the pinned key, found by its address first and else among all the
  profile's contacts.
- `sessionphone link` lists the owner bound to the joiner's key.
  `sessionphone channels add NAME ADDR --key FPR` and the new
  `sessionphone channels pin NAME --key FPR` take the fingerprint; without
  `--key` they open the account (with the bot stopped) and pin only when
  exactly one key contact has that address. `bot install` and
  `channels seed` copy the callers owner line's key. `sessionphone channels`
  shows each address line's key, or UNPINNED.
- Upgrading: at its first start the bot pins the owner line of a
  `channels.txt` from an older version, as it does in `callers.txt`, keeping
  `channels.txt.pre-keys`.

### The docs say what each identity is

- `X-Delta-Chat-Sender` is documented as the address a message carries, a
  label for people, not a key and not an identity. `X-Delta-Chat-Channel` is
  the header that carries a checked answer, and the README, the skill and
  `docs/` say so.
- The README has a new "Callers" section: the `ADDR  key:FPR` line, what a
  line with no key does, the one-time upgrade (the `.pre-keys` backup, the
  marker, what is pinned and what is not), and how to resolve an `UNPINNED`
  or `AMBIGUOUS` line. "Channels" gains the same for `NAME ADDR key:FPR`.

### `bot install` says when the bot cannot speak: no ffmpeg

- On a Mac with no `ffmpeg`, `bot install` printed `hearing: yes` and
  nothing else, yet every spoken reply failed and went to the caller as
  chat text. The bot needs ffmpeg to turn the speech server's replies into
  call audio, and nothing checked for it before the first call.
- `bot install` now ends with a `speaking:` line. It looks for ffmpeg
  where the bot looks, on the job's environment: `DC_FFMPEG`, then the
  job's `PATH`, then `/opt/homebrew/bin/ffmpeg` and `/usr/local/bin/ffmpeg`.
  With none, or a `DC_FFMPEG` that names no program, it says `NO`, that
  spoken replies go as chat text, and the fix: `brew install ffmpeg`, then
  `sessionphone bot install` again. `DC_TTS=apple` needs no ffmpeg and says
  so. `bot status` prints the same line. Neither exit code changes.

### Under Session Kit, `bot install` gives both forms of `ears services install`

- When hearing is `NO` under Session Kit and the kit has no speech server,
  the `hearing:` line gave the bare `ears services install`. ears refuses
  that form (exit 2) while it names no seat to wake, so a Mac that only
  runs the bot was sent straight into the refusal.
- The line now gives both forms, one line each, after a line saying which
  Mac it fits: `ears services install --no-agent` for a Mac that only runs
  the bot, with no agent seat for ears to wake, and
  `ears services install --agent NAME` for a Mac with an agent seat that
  ears should wake when you speak. The skill's step 7 says the same.

## [0.3.0] - 2026-10-24

### A second profile on one root keeps its own files

- A bot run with `DC_PROFILE` (such as `public`) on a root that another bot
  also uses read the default profile's `channels.txt` and `callers.txt`, and
  took jobs from the default profile's outbox. Chat ids belong to one
  account, so an entry for chat 17 on the private list let in chat 17 on the
  public account, which is another chat. The `dc-*` tools and
  `sessionphone channels rejected --profile public` already looked under
  `<root>/public/`; only the bot did not.
- A named profile's home is now `<root>/<profile>/`, and everything it keeps
  about its account lives there: the channels list, callers, outbox, seen
  ids (`DC_SEEN`), acks, undelivered mail, calls (`DC_CALLS_DIR`), the live
  call (`DC_LIVE`), the log, guests, sockets, the saved invite and name, the
  picture's "none" marker, and with no `DC_ACCOUNTS`, the account itself. An
  explicit `DC_<NAME>` still wins. The voice map and the wait music stay at
  the root, shared. The default profile is unchanged: its home is the root,
  and every path it reads is the same as before.
- A named profile with no `callers.txt` of its own refuses every call, and
  says so at start, naming the file.
- The bot refuses to start (exit 2, one line) when a named profile's list or
  callers file is the default profile's own, or when another bot already
  runs on the same home (it holds `<home>/.bot.lock`, and the line names its
  pid).
- The bot refuses to start when its outbox holds jobs older than
  `DC_OUTBOX_STALE_S` (default 3600 seconds): jobs queued while no bot read
  that outbox. The line names how many, the oldest one's age, the path, and
  how to drop them or send them. This holds for every bot, the default
  profile too.
- The rejection line in the bot's log names the profile and the list it read.
- `bot install` compares bots by home, so two profiles on one root are no
  longer rivals; two bots on one home still are.
- `bin/dc-env` follows `DC_PROFILE` in the shell as the bot does. `dc-say
  --call` checks the live call of the bot its job goes to, beside that
  bot's outbox (as the channels pre-check finds its list), so the fallback
  line in a profile's call mail keeps working now that the live call moves
  with the profile. A TTS failure on a profile's
  call now queues its text into that profile's outbox.
- Before restarting a named-profile bot on this version, look in
  `<root>/<profile>/outbox`: jobs a tool queued there were never taken
  before, and the stale-job refusal stops the bot until a person decides.

### Under the mail drop, an address off this host is refused at start

- The mail drop delivers on this host only. With the bot's `DC_MAILTO`, or
  the To of `sessionphone web --legs`, on another host, the bot and the web
  call started anyway, and then every mail of the run was refused. They now
  refuse at start with one line that names the address and what to do: set
  `DC_MAILTO` (or `--mail-to`) to a box on this host, or use a carrier that
  reaches it. Nothing is started. `bot install` and `account setup` refuse
  the same address. On an MTA an address off this host starts as before.
- "This host" is one rule everywhere: the start's box check and the Cc guard
  now judge an address as the web seat's default host does, so a reverse-DNS
  name (`….arpa`) is never this host, even one under the short host. A
  tailnet name under the short host still is.

### A call speaks one line at a time

- On a live call, a guest's reply read aloud by the bot could land between
  two parts of a sentence the mouth was speaking, and the other way round.
  Two replies at once could also take turns sentence by sentence.
- Now one line owns the call's voice until it ends: a mouth line from its
  start to its end, or one whole reply from the bot. A line that arrives
  while another is playing waits, then plays whole, in arrival order.
- A barge-in, or the mouth's cut, drops the playing line and every line
  waiting behind it. A reply that is cut no longer says its later sentences,
  and a mouth line cut by a barge-in no longer plays the rest of its audio.
  A mouth that stops before its line ends is still a cut, and a hang-up
  ends every line, so no line is left holding the call.
- The echo guard is told when a waiting line really starts to play, so a
  reply that waited behind the mouth is not taken for the caller talking.
- Sounds and the first-call wait music are unchanged.

### A wheel install prints the same connect page as a checkout

- `sessionphone qr` looked for the skill's `qr-page.html` relative to the
  package, which works only from a checkout. A wheel install
  (`pip`, `uv tool install`, a kit install) found nothing there and printed
  a second page kept in the code, which had drifted from the first: shorter
  steps and a different layout.
- There is now one page, `src/sessionphone/pages/qr-page.html`. It ships as
  package data and is read with `importlib.resources`, so a checkout, a
  wheel and a kit install print the same bytes. The copy in the code and the
  copy in the skill are gone. `qr --template` still takes your own page.
- A test builds the wheel, installs it into a fresh venv, and checks that
  the installed package prints the same page as the checkout.

### `dc-live` finds the bot by its pid file, not by searching command lines

- `dc-live` decided the bot was running with `pgrep -f "$DC_BOT"`, a regex
  over every process's command line. That was wrong in five of six measured
  cases. A `tail -f` of a file named after the bot, a grep, another agent
  whose name starts with this one's, and a `.` matching any character all
  counted as the bot. The foreground `sessionphone bot run` never counted,
  so `dc-live` said the bot was NOT RUNNING while it ran, even mid-call. A
  bot on another root counted too.
- The bot now writes `<root>/bot.pid` (`DC_PID_FILE`) once it holds the
  account: its pid, its start time and command line as `ps` gives them, and
  its root. It removes the file at a clean exit. `dc-live` counts the bot as
  running only when that pid is alive, `ps` still gives the same start time
  and command line for it, the command line names sessionphone, and the
  root is this one. A stale file (a dead pid, or one given to another
  process) reads NOT running, and the second stderr line says why.
- If the file cannot be written, the bot logs one line and runs on.
- `DC_BOT` is deprecated and ignored. When it is set, `dc-live` says so on
  stderr. `bin/dc-env` and `bridge/dc_env.py` name the same `DC_PID_FILE`.
- **Restart the bot after upgrading.** A bot started before this version
  writes no `bot.pid`, and `dc-live` reports it as not running until it is
  restarted.

### `web --legs` takes the mail drop's host, not a reverse-DNS name

- With no `DC_HOST`, `sessionphone web --legs` built the seat's host from
  this machine's DNS name. On macOS 27, a Mac with no forward record gets
  the IPv6 loopback's reverse-DNS name (`….ip6.arpa`), and `--legs` refused
  to start until `DC_HOST` was set by hand. The host is now the one the
  mail drop gives a bare From: the DNS name when it is sessionmail's short
  host or a name under it (a tailnet name), else the short host,
  lowercased. An unrelated DNS name now gives the short host too.
- `--legs` still refuses when that gives no host, `localhost`, or a
  reverse-DNS name, and the refusal says where the host came from. The
  bot's own host, its default addresses and its Message-IDs are unchanged.

### The carrier's source is named truthfully, and a deaf session is warned first

- The `seat:` and `mail:` lines of `bot install`, the bot's `agent mail:`
  start line and the settings block's `SESSIONMAIL_SENDMAIL` row said the
  carrier came from sessionmail's `mail.json` `sendmail` whenever it came
  from a file, including the template sessionmail installs. On a fresh
  install `mail.json` holds no such key, so the line named a source that
  was not used. They now say `mail.json` only when that file sets the key,
  and ``sessionmail's installed default, ~/.sessionkit/mail/settings/mail.template.json
  `sendmail` `` when the template gave it.
- When the installing session will NOT hear the bot's box, `bot install`
  prints that `hear:` warning and its ways out first, ahead of `installed:`.
  A line saying it does hear, and a Terminal's advice, stay under `box:`.

### The bot's name follows its agent

- The bot's Delta Chat name is now the agent's name, title-cased (`versa` ->
  `Versa`), unless it was set by hand. A new setting, `DC_NAME`, gives it any
  other name and always wins.
- The bot sets the name at its start and logs one `name:` line, old -> new.
  It then writes the saved invite again, so `invite` and `qr` show the new
  name.
- The agent's name goes only over a name nobody chose: an empty one, or the
  name of the agent the bot is or replaced. `bot install --replace OLD`
  leaves OLD's name in `name-replaced` under the root, and the bot's start
  removes it. A bot that came online as `Claude` and is replaced by `versa`
  is now `Versa`.
- Any other name is kept as the user's. `bot install` prints one `name:`
  line saying so, with the `DC_NAME=... sessionphone bot install` line that
  changes it.

### The skill says how to change a bot's picture and name

- SKILL.md has a short section, "The bot's picture and name": the four
  lines to run, for this bot and for another agent's (`DC_AGENT=<agent>`).
  `references/profile.md` says how `DC_AGENT` picks the bot and its root,
  and that with two bots recorded the command refuses until one is named.

### A slot for a default bot picture

- sessionphone can now ship a default picture for the bot,
  `src/sessionphone/images/default-avatar.png` (or `.jpg`). None ships yet:
  the slot is empty, and a test holds it empty.
- When one ships, the bot sets it at its start, but only when the account
  has no picture and the user has not run `sessionphone avatar --clear`. It
  logs one `picture:` line. A picture already there is never replaced.
- `sessionphone avatar --clear` now leaves a file, `avatar-none`, under the
  root, so the default is not put back. `sessionphone avatar PATH` removes
  it.
- `bot install` prints one `picture:` line saying so when a default ships,
  and none when nothing ships.

### `make check` runs the bridge tests

- `make check` now ends with `make bridge`, which runs `bin/bridge-check`:
  the bridge compiles, imports in a bare environment, and every file in
  `tests/bridge` passes. `make test` still does not collect `tests/bridge`
  (most of its files are scripts), so before this a red bridge test was
  seen only by someone who ran `bin/bridge-check` by hand.
- The list-alert tests follow the bot's start: it asks `message path` for
  its box before its first send. Their stand-in `message` answers that
  check, and a new test pins the order.

### The first call says the voice model is downloading, then plays music until it can hear

- While the speech-to-text or text-to-speech models are still missing or
  downloading (`ears services weights --json`), a call that connects hears
  a line said by macOS `say`: "Please wait for the voice model to download,
  about 2.9 GB. This only happens once." The size is left out when it is
  not known. Then the music file `DC_WAIT_MUSIC` names
  (`<root>/wait-music.opus` by default, any audio file the bot can decode)
  plays, looped and turned down. With no file, the line repeats every 20 s
  with the download's progress, and nothing plays in between.
- The music stops at once and for good at the first of: the bot's own
  speech-to-text answers a test clip (asked every 3 s), the greeting or any
  other line is queued, the call ends, or 20 minutes pass. It is never in
  the transcript, and a barge-in leaves it alone. The greeting is not
  delayed. With the models in, or an `ears` that cannot say, a call is as
  before. New setting `DC_FIRST_CALL_WAIT` (`on`/`off`, default `on`).
  No audio ships in the package.

### The hearing line says the speech models are downloading, with progress

- When the speech server on `127.0.0.1:8890` gives `bot install` no
  answer, the `hearing:` line asks `ears services weights --json`
  (read-only, the filesystem only) where the speech models stand. While
  they download it reads `downloading 412 MB of 2.9 GB` (or
  `downloading, 412 MB so far` when the total is not known) and says the
  first call waits for it, instead of a bare `not confirmed` after a
  30-second timeout. When they are not downloaded yet it says so, about how
  large they are, and that the first request fetches them. Under Session
  Kit it points at `ears services status` to watch the download.
- The first call waits only for the speech-to-text and text-to-speech
  models: the voices pack missing alone is no wait. An `ears` without the
  verb, one that fails, or one that answers in another schema leaves the
  line as it was. New: `hearing.weights_state()`, the reader any part of
  sessionphone can use to tell whether a first call will wait.

### Install says whether this session will hear the bot, and names the bot after a seat that lasts

- `sessionphone bot install` now asks sessionmail what reaches the session
  it runs in (`message subscription list`, read only). With no `DC_AGENT`,
  the bot is named after that session's seat when a later session can be
  started as it (`session start ada` names it `ada`). A session id or a
  `+` address never names it, so a plain `claude` session, or a seat a task
  tool started (its `CLAUDE_CODE_AGENT` a role such as `worker`), gets the
  bot the file records, else `claude`. Before, such a seat named the bot
  after its role, a box nobody hears. `bot status` and `bot uninstall` pick
  the bot the same way. When sessionmail gives no answer, `CLAUDE_CODE_AGENT`
  is taken as before.
- The `hear:` line is measured, never guessed. When this session will not
  hear Delta messages or calls, the line says so first, names the box they
  go to, and gives the ways out: `session start <agent>`, `DC_AGENT` or
  `DC_MAILTO`, or `--hear-here`. It is a warning: the bot is installed and
  the exit code is 0. It says the session hears the box only when
  sessionmail says so; the old `this session is <agent>` line, which a task
  seat got from its role alone, is gone. From a Terminal it gives the
  advice and how to send Delta text to another box, and so does the bot's
  `agent box:` line at every start.
- New: `bot install --hear-here` lets this session hear the bot's box now,
  by a session subscription (`message subscription add <agent>`). It ends
  with the session, and install never does it unless asked.

### One bot to a root, and every reader picks the bot as install does

- `sessionphone bot install` refuses (exit 2, writing nothing) while
  phone.json records another bot on the same root, one Delta Chat account,
  and names it. Before, a renamed install left the old bot's job running on
  that account, still mailing its own box, and said nothing. New: `bot
  install --replace NAME` retires that bot (its launchd job booted out, its
  plist removed, before the new job starts) and phone.json forgets it once
  the new job is loaded.
- `sessionphone bot run`, the `dc-*` tools (`bin/dc-env`) and the bot's
  settings (`bridge/dc_env.py`) now pick the bot the way install does:
  `DC_AGENT`, else this session's durable seat, else the one bot phone.json
  records, else `claude`. Before, they took `CLAUDE_CODE_AGENT`, else
  `claude`, so a foreground bot or a tool from a plain shell could act as
  another bot than the one installed. With no name in your shell, `bot run`
  prints a `bot:` line and each tool says which bot on stderr.

### The skill no longer offers VoiceMode's speech server under Session Kit

- On `hearing: NO` or `not confirmed`, the skill first asks whether this is
  Session Kit (`command -v sessionkit` prints a path). Under the kit it runs
  `ears services status` (read-only), reads its `speech` row, and never
  offers `voicemode service install mlx-audio`: the kit's
  `ears services install` already loads an mlx-audio server on
  `127.0.0.1:8890`, and a second mlx-audio job leaves one of the two
  crash-looping. A first-run model download can read `not confirmed` for a
  minute or more: wait, then run `bot install` again. Standalone, the
  VoiceMode offer is unchanged.
- SKILL.md's `DC_STT_URL` default and the README's "What a voice call
  needs" name the server on 8890 as the kit's own or VoiceMode's, not
  VoiceMode's only.
- The `hearing:` line that `bot install` ends with makes the same split.
  When `sessionkit` is on the PATH `bot install` ran with, a `NO` at the
  server on `127.0.0.1:8890` points at `ears services status` (read-only),
  names `ears services install` for an empty port only, and never names
  `voicemode service install mlx-audio`. Standalone, the line is unchanged.

### The media pack text splits Session Kit from standalone

- The skill says that under Session Kit the media pack is already installed
  (the kit asks for `sessionphone[media]`), so `bot install` fetches nothing
  and the agent never asks about it. Standalone, the pack still comes once
  at the first `bot install`, and the agent runs the command without asking
  first.
- The README's "Other ways to install" no longer says the kit asks for a
  bare `sessionphone`: the kit asks for `sessionphone[media]` 0.2.1 or
  later.

### The connect page's QR scans in dark mode and in any window size (PHONE-212)

- `sessionphone qr`'s code is now dark on an explicit white fill, with the
  full 4-module quiet zone (it was 2, and transparent). In dark mode the
  page showed black on near-black.
- The SVG carries a `viewBox` and no fixed size, so the page scales the
  whole code to its box. Before, a box smaller than the drawing cropped it:
  the built-in page (what a wheel install prints) cut off two finder
  patterns, and the skill's page did the same in a phone-width window.
- Both pages keep a white card behind the code in dark mode, give the
  link a colour that reads on the dark background, and both say
  what to do if the phone waits at "Establishing connection, please wait":
  the bot must be running to answer the scan, so check
  `sessionphone bot status`, then run `sessionphone link` and scan again.
- The dev group now includes zxing-cpp and Pillow, so `make test` decodes
  the generated QR with a real decoder instead of skipping that test.
  They are for development only: the package's dependencies are unchanged.

### The skill offers the ways to see the QR as choices (PHONE-212)

- Setup's "show them the code" step now offers the ways as choices, in
  this order: the QR in a window first (the PNG from `invite --png`, or
  the `qr` page, opened with `open` on macOS or `xdg-open` on Linux), the
  plain link second, and the terminal QR (`invite --qr`) last. Claude Code
  will likely fold the terminal QR ("+30 lines (ctrl+o to expand)"), so
  the person sees no code until Ctrl+O expands it.
- The skill no longer opens the PNG with `show-me`, which has no image
  view. It tells the agent never to say "scan the QR code above" unless
  the code was shown.
- The skill says what to do if the phone waits at "Establishing
  connection, please wait": check `sessionphone bot status`, then run
  `sessionphone link` and scan again.
- The README's `invite` section says the same about the fold, and gives
  the PNG's size as about 730 px a side.

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

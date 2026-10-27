# Starting the bot: what bot install does and says

`sessionphone bot install` starts the bot, the one process that holds the
agent's Delta Chat account, as this user's launchd job. It is the last step
of setting up, after `link` has exited. Read its `seat:`, `hear:` and
`box:` lines before you ring anyone. The [sessionphone skill][skill]'s
setup step 7 links here; whether the bot will hear the caller, and the
caller you, is [Call hearing][hearing].

- `link` must have exited first: only one process can hold the account.
- `sessionphone bot status` should say `running: pid N` once it is up.
- Ring the owner with `dc-ring OWNER_ADDRESS`, and say first what you will
  do.

## Its seat, and whether it signs

- The bot mails you as its own sessionmail seat, `<agent>-chat` (with
  nothing naming the agent, the harness vendor's word: `claude-chat`
  today). Whether it signs follows the carrier its own `message send` will
  use ([Bot mail signing][bot-mail-signing]). Do not pick the carrier: it
  is sessionmail's (`SESSIONMAIL_SENDMAIL`, else the `sendmail` key in its
  `mail.json`, else its default, `drop`).
- **On the mail drop (no mail server), it does not sign.** Run
  `sessionphone bot install`: no key, no registry, no seat step.
- **On an MTA it must sign.** Run `sessionphone bot install --registry
  DIR`, where DIR is a seat registry (the README's "The bot's seat" shows
  how to make one). It mints the seat there if it has no key, runs its key
  agent and saves the signer. If install says the seat is not minted, it
  names this step. If it fails (exit 1), run `message seat agent
  --uninstall <agent>-chat`, then install again. If it says this
  sessionmail lacks `Capability: install`, follow the README's steps by
  hand (`message seat agent <agent>-chat SOCKET` under launchd, then
  `DC_SIGNER_SOCK` and `DC_SIGNER_PUB`).
- Install pins `DC_MESSAGE`, makes your box (`message setup --box
  <agent>`) and checks the bot would start, before it loads anything. Its
  `seat:` line names the mode and why, and its `mail:` line the carrier.
- `bot install` carries sessionmail's mail-route settings
  (`SESSIONMAIL_SENDMAIL`, `_RELAY_DOMAIN`, `_HOME`, `_ROOT`, `_HOST`) into
  the job, never the seat's own and never `SESSIONMAIL_ORIGIN`. After
  changing one, run `bot install` again.
- While a call is up, install and uninstall refuse (exit 2, `a call is
  live; hang up or pass --force`): they would drop it. Run it again after
  the hang-up. Pass `--force` only when the user asks for the call to drop.

## Hearing it

- **Hearing it.** Delta text goes to the box `<agent>`. Only a Claude
  session started as `session start <agent>` (which runs `claude --agent
  <agent>`) hears that box; a plain `claude` gets no copy, and nothing
  tells it so. Install asks sessionmail whether this session hears it, and
  its `hear:` line says so. If it says `this session will NOT hear`, tell
  the user that line and its ways out; do not claim this session hears it.
  **If this session started plain, repair first.** Work in this order.
  (1) Look for session start's `starting plain:` line. It is on the user's
  terminal, above this session; `session start --dry-run` launches nothing
  and its `seat:` line says what a new launch would say now. (2) If the
  line is `starting plain: box <agent> is missing, ...; repair: message
  setup --box <agent>`, give the user that repair: `message setup --box
  <agent>`, or `sessionkit setup`, which makes the box. Then tell them to
  start a new session with `session start`. This session stays plain; only
  a new one hears box `<agent>`. (3) Only then offer `bot install
  --hear-here`, or a subscription (`message subscription add <agent>`), as
  a stopgap that ends with this session. Only the missing-box line carries
  a `repair:`. The other `starting plain:` lines have none, so do not
  invent one: tell the user the line as it stands. `held by ...; that one
  does` means another session holds the seat and hears the box;
  `macOS-only for now` and `could not check` say why this session cannot.
  `bot install --hear-here` lets this session hear it until the session
  ends; only run it when the user asks. If a `default:` line follows (`no
  seat owns box <agent>`), tell the user that line too: the bot took the
  default name and nothing hears its box yet.
- **Where Delta text lands.** Every Delta message and call turn goes to
  that one box, the bot's own, named after its agent (box `claude` unless
  `DC_AGENT` is set or this session was started as another agent;
  install's `box:` line names it). `DC_MAILTO` changes only that: set, the
  bot mails the box it names instead, and the `hear:` line then says
  whether this session hears that box. Set it only when the user asks for
  another box, never just to quiet the warning. A box named by this
  session's own id is heard only while this session runs: the next session
  has another id.

## The launchd job, and when install refuses

- `sessionphone bot install` runs the bot as this user's launchd job, now
  and at every login. `sessionphone bot status` should say `running: pid
  N`. If it doesn't, read its `last:` line, the log's last line.
- It refuses (exit 2, naming the file) when nobody may ring the bot: no
  address in `callers.txt`. There are two ways forward. Run `sessionphone
  link` and let the user scan the invite (the first to join becomes the
  owner). Or run `sessionphone bot install --owner ADDR --owner-key FPR`,
  where ADDR is the user's Delta Chat address (their Delta Chat profile
  shows it), not their mail address, and FPR is the key fingerprint their
  phone shows under their contact's profile, Encryption. Each caller is
  bound to a key, not to an address. `DC_CALLERS` grants nothing: install
  refuses while it is set, and approved callers come from `callers.txt`
  only.
- It also refuses when `channels.txt` is empty and there is no owner to
  list in it (`--owner ADDR` fixes both).

## Ringing the owner

- `dc-ring OWNER_ADDRESS` rings the owner. The phone rings for up to two
  minutes. Say what you'll do before you ring, so the call isn't a
  surprise.

[skill]: ../skills/sessionphone/SKILL.md
[hearing]: call-hearing.md
[bot-mail-signing]: bot-mail-signing.md

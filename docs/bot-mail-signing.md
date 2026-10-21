# Bot mail signing

Whether the bot signs its mail to your Claude follows the mail carrier. There is no switch for it.

- **On a Mac with no mail server, the bot's mail is not signed.** The bot
  writes each message straight into your own mailbox, as you, through
  sessionmail's mail drop. Nothing on this Mac could tell a signed file
  from an unsigned one, since anything that could forge the file could
  also use the key. So there is no key to make and no registry to set up.
- **With a mail server, or for a fleet, give the bot a key:**
  `sessionphone bot install --registry DIR`. It mints the bot's seat into
  that seat registry, runs its key agent, and saves the signer. From then
  on the bot signs every mail, on any carrier.
- **Two conditions decide whether a mail reaches your Claude at all**,
  signed or not:
  1. **The agent's box exists.** The mail drop never makes a box, and it
     refuses a mail to a box that is not there. The bot makes the box at
     install and checks it at every start, before its first send. When it
     cannot make the box, it refuses to start (exit 2).
  2. **Your Claude was started as the agent.** Mail to the agent's box
     reaches a session only through the pager's doorbell, and the doorbell
     rings only for a session started as that agent:
     `session start NAME`, which runs `claude --agent NAME`. A plain
     `claude` gets no copy of the mail, and nothing tells it so.

## The rule

The bot decides from the carrier this bot's own `message send` will use, as
its sessionmail resolves it. That is sessionmail's own order:

1. `SESSIONMAIL_SENDMAIL`, when it is set. `bot install` carries it from
   your shell into the bot's launchd job, so the job keeps the value it was
   installed with.
2. Else the `sendmail` key in sessionmail's `mail.json`,
   `~/.sessionkit/mail/settings/mail.json`. The key is `sendmail`; a key
   named `carrier` is not read.
3. Else sessionmail's default, which is `drop` (from sessionmail 0.2.0 on).

Whether postfix is installed does not decide it. A job with no `sendmail`
setting uses the drop, even on a Mac that has postfix.

| The carrier, and the signer | The bot |
| --- | --- |
| `drop`, no signer set or saved | sends **unsigned**. `bot install` needs no key, no registry and no seat step |
| `drop`, a signer set or saved | **signs**: a bot keyed on purpose keeps signing |
| an MTA (an absolute path, for example `/usr/sbin/sendmail`) | **signs, and must**: a send with no usable key is refused, and nothing is sent |
| a value sessionmail refuses, a `mail.json` it cannot read, a carrier of a kind the bot does not know, or a sessionmail the bot cannot ask (its `message` has no carrier resolver) | **refuses to start** (exit 2). It never falls back to unsigned, and never guesses an MTA |

A signer is `DC_SIGNER_SOCK` and `DC_SIGNER_PUB`, both set in your shell or
both saved by an earlier install.

On an MTA, a mail system stands between the bot and the box, so another
account or host could land a forged From there. That is where a signature
earns its place.

## Giving the bot a key: `--registry DIR`

- **DIR is a seat registry**: a git directory whose committed
  `allowed_signers` holds a seed line. The README's
  [The bot's seat][readme-seat] shows how to make one. sessionmail's
  `docs/message-seat.md` ("The trust registry") describes it.
- **What install does with it.** With no signer set or saved, it runs the
  seat's key agent (`message seat agent --install <agent>-chat`). Only when
  sessionmail says the seat has no key yet does it mint one first
  (`message seat init --registry DIR <agent>-chat`), then run the agent
  again.
- **It saves the signer**, so later installs and starts sign without
  `--registry`.
- **A re-run is safe.** A key already there is never minted again.
- **`--registry` is the only way install mints.** On an MTA with no key and
  no `--registry`, install refuses (exit 2), writes nothing, and names
  `sessionphone bot install --registry DIR`.

## What the bot tells you

- **`bot install`'s `seat:` line** names the mode and why. For example:
  - `claude-chat, unsigned (carrier drop: this bot writes into the box itself, from sessionmail's default), no key`
  - `signed (carrier /usr/sbin/sendmail is an MTA, from sessionmail's default)`
- **The `agent mail:` line in `bot.log`**, at every start, names the same
  mode, the reason and the carrier.
- **The `agent box:` line in `bot.log`**, at every start, names the box and
  the step that hears it (`session start NAME`, which runs
  `claude --agent NAME`).
- **sessionmail's warnings come once per run.** An unsigned send makes
  sessionmail warn (the UNSIGNED notice, and the MagicDNS notice when its
  suffix is unknown). The bot logs each distinct warning once per run, not
  once per mail. A `message send` you run by hand still prints them on
  every send: that output is sessionmail's.
- **A Cc that could sink the mail is left off.** Under the drop, one
  recipient it cannot deliver to refuses the whole mail, the agent's own
  copy included. So the bot Cc's only a box sessionmail says is there (for
  `base+tag`, the box `base` counts), and under the drop only on this host.
  Each Cc left off is logged once per run, with why.

[readme-seat]: ../README.md#the-bots-seat

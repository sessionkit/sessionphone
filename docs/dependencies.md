# Dependencies

This page lists sessionphone's direct dependencies and the tools a user
meets: what needs each one, whether you need it, how it gets onto your Mac,
and how you upgrade it. The README's [requirements][readme-requirements]
link here. The full resolved set, every package at the version this repo is tested with, is in [`uv.lock`][uv-lock].

- **Required:** macOS, Claude Code, uv, Python 3.11 to 3.14, Apple's
  Command Line Tools, sessionmail's `message`, and the Delta Chat app on
  your phone. The plugin's launcher, or `uv tool install
  'sessionphone[media]'`, brings the Python packages, the media pack
  included.
- **Optional:** the speech server, ears, `session`, Tailscale and
  `swiftc`. macOS brings the few other tools the bot runs.
- **Pending a ruling:** Homebrew and ffmpeg. Whether they are required is
  not decided yet; their entries say what uses each one today ([pending a
  ruling][pending]).
- **Upgrade:** `uv tool upgrade sessionphone` for the package and its
  Python packages, `claude plugin update sessionphone@sessionphone` for the
  plugin, then `sessionphone bot install` again so the bot runs the new
  version ([upgrade][upgrade]).

Required means a default command fails without it. Optional means a feature
you turn on needs it, or a command still works without it but does less.
sessionphone uses neither tmux nor notmuch.

## Required

| Dependency | What needs it | How you get it | How you upgrade it |
|---|---|---|---|
| macOS | Everything. Tested on macOS 27 on Apple silicon. `bot install` writes a launchd job, and the bot's fallback voice is macOS `say`. | Your Mac. | Software Update. |
| [Claude Code][claude-code] | The plugin: its skill, which walks the agent through setup, and its `bin/sessionphone`, on the agent's PATH while the plugin is enabled. The session you ring is a Claude Code session. | Its installer. | Claude Code's own update. |
| [uv][uv] | The plugin's `bin/sessionphone`, which builds its own environment with `uv run` the first time it runs and exits 2 without uv; `uv tool install`, the other way to install. | The uv installer, or Homebrew. | `uv self update`; a Homebrew copy upgrades with Homebrew. |
| Python 3.11 to 3.14 | `sessionphone` and the bot run on it. | uv uses one already on the Mac, and fetches one when the Mac has none. | It stays with the install: `uv tool upgrade sessionphone` keeps it. |
| Apple's Command Line Tools | `python3`, which every `dc-*` tool runs for its JSON (`dc-say`, the agent's reply, among them). `git`, for `claude plugin marketplace add` from a URL, which clones the marketplace. On a Mac without them, `/usr/bin/python3` only offers to install them. | `xcode-select --install`, or Homebrew's installer, which brings them. | Software Update. |
| `message`, from [sessionmail][pkg-mail] | `bot install` and `bot run`: the bot hands each chat message and call turn to the agent with `message send`, and checks a guest's mail with `message verify`. Without it they refuse, exit 2, "no mail can reach the agent". `bot install` pins it into the job as `DC_MESSAGE`. | `uv tool install sessionmail`, or [Session Kit][pkg-kit], which brings it. | `uv tool upgrade sessionmail`, or `uv tool upgrade sessionkit`. |
| [Delta Chat][delta-chat] | `link`, `invite`, chat and calls: the owner and every caller use the app on their phone. `sessionphone setup` makes the agent's own account on a public chatmail relay. | The app comes from your phone's app store. | The app's own update. |
| The Python packages | Every command. The bot, calls and the web page need the media pack too (below). | The plugin's `bin/sessionphone` fetches them with uv; or `uv tool install 'sessionphone[media]'`. | `uv tool upgrade sessionphone`, or `claude plugin update` for the plugin's copy. |

The Python packages, and what each one is for:

- click 8.1 or later: the `sessionphone` command line.
- deltachat-rpc-client and deltachat-rpc-server, 2.61.0 or later: the
  agent's Delta Chat account. The server binary ships in its wheel, so
  nothing else installs it.
- segno 1.6.6 or later: the QR codes `qr` and `invite` print.
- The `media` extra, for `bot install`, `bot run`, `web` and `web enrol`:
  aiortc 1.15.0, aioice 0.10.2 and av 17.1.0, pinned, the WebRTC stack
  calls run on; numpy 2 or later; websockets 16, for `sessionphone web`;
  cryptography 42 or later, which checks the web page's passkeys. Without
  it, a media verb refuses, exit 2, and says how to fetch it. A bot with
  `DC_CALLS=off` is chat only and needs none of it.

The plugin's `bin/sessionphone` runs at the versions in `uv.lock`, and
fetches the media pack only at the first media verb. `uv tool install`
resolves again from the ranges in `pyproject.toml`. [Installation][install]
gives the sizes, and what a failed fetch leaves.

## Optional

| Dependency | What needs it | Without it | How you get it | How you upgrade it |
|---|---|---|---|---|
| The speech server | Hearing a caller, and the voice notes' text: the bot posts the caller's speech to `DC_STT_URL`, by default `127.0.0.1:8890`. The bot's replies in the voice `DC_VOICE` names, from the same server. The first call waits while the server downloads its speech models. | A call connects, and the agent does not hear the caller; `bot install` says so on its `hearing:` line. A voice note reaches the agent as "[voice note - transcription failed]". The bot speaks with macOS `say`, and logs that it did. | sessionphone does not install it. With Session Kit it is the kit's own, from `ears services install` ([sessionvoice][pkg-voice]); standalone, VoiceMode's. | Its installer's own upgrade: under Session Kit, `ears services install` again after `uv tool upgrade sessionkit`. |
| `ears`, from [sessionvoice][pkg-voice] | Turn previews (`DC_PREVIEWS=1`) and hearing a call through ears in the bot's own process (`DC_EARS=1`), both off by default. The first call's wait, and `bot install`'s `hearing:` line, which ask `ears services weights` whether the speech models are on the Mac yet. | Calls are heard and answered as they are. The first call has no wait line while the models download, and the `hearing:` line does not say whether they are downloading. | `uv tool install sessionvoice`, or Session Kit. | `uv tool upgrade sessionvoice`, or `uv tool upgrade sessionkit`. |
| `session`, from [sessiontools][pkg-tools] | `bot install`, which asks `session start --list-agents` whether an agent file names the bot's agent. | The install says it found no agent file, and goes on. | `uv tool install sessiontools`, or Session Kit. | `uv tool upgrade sessiontools`, or `uv tool upgrade sessionkit`. |
| [Tailscale][tailscale] | `sessionphone web`: the address a phone can open, and `tailscale serve` in front of the page. `web enrol`'s QR. | No QR: "no address a phone can open"; type the enrol code, or pass `--origin`. | The Tailscale app. | The app's own update. |
| `swiftc`, from Apple's Command Line Tools | The text the bot reads off a picture, through macOS Vision. The bot builds its reader with it once. | The bot logs "IMGTEXT off: no swiftc", and a picture reaches the agent without its text. | `xcode-select --install`. | Software Update. |

macOS itself brings the rest: `curl`, which the bot posts to the speech
server with; `say`, its voice when no speech server answers; and
`launchctl`, for the bot's launchd job.

## Pending a ruling

Whether these two are required is not decided yet, so this page gives them
no class and no install step. Each entry says what uses it today, what
happens without it, and where it comes from today.

### [Homebrew][brew]

Status: pending a ruling. It hangs on the ruling for ffmpeg below.

- **What uses it:** no sessionphone command runs `brew`. It is where
  ffmpeg comes from today, and its installer brings Apple's Command Line
  Tools. The bot's launchd job puts Homebrew's two `bin` folders first on
  its `PATH`, and the bot looks for ffmpeg in them.
- **Without it:** the Command Line Tools come from `xcode-select
  --install` instead, and the bot finds an ffmpeg only on the job's `PATH`
  or where `DC_FFMPEG` names it.
- **Where it comes from today:** its installer, from its own site.

### [ffmpeg][ffmpeg]

Status: pending a ruling. Whether sessionphone requires ffmpeg is not
decided yet.

- **What uses it:** the bot, to turn each reply the speech server renders
  into call audio; the first call's wait music, which it decodes;
  `dc-call-mix`, which makes a recorded call's stereo file.
- **Without it:** ffmpeg is the one dependency whose absence fails
  silently. With the speech server up, every spoken reply fails: the call
  stays up, the caller hears nothing, the bot's log says `tts failed`, and
  the reply goes to the chat as text. `bot install` and `bot status` say so
  beforehand, on their `speaking:` line. With no speech server answering,
  or with `DC_TTS=apple`, the bot speaks with macOS `say` and needs no
  ffmpeg. The wait plays its line alone, without the music, and
  `dc-call-mix` stops with "ffmpeg not on PATH".
- **Where it comes from today:** Homebrew. The bot looks in `DC_FFMPEG`,
  then on its job's `PATH`, then in Homebrew's two `bin` folders, so an
  ffmpeg anywhere else needs `DC_FFMPEG` ([settings][settings]).

## Upgrade

```bash
uv tool upgrade sessionphone
claude plugin marketplace update sessionphone
claude plugin update sessionphone@sessionphone
sessionphone bot install
```

- **The first line** upgrades `sessionphone` and its Python packages, and
  keeps the Python the install chose. Installed through Session Kit, `uv
  tool upgrade sessionkit` does it instead. Re-install with the `media`
  extra, never without it: a tool installed without it takes the pack away
  from a bot that runs from it.
- **The plugin lines** fetch the marketplace again, then the plugin. Its
  `bin/sessionphone` then builds its environment again at the new
  `uv.lock`, the next time it runs.
- **The last line** restarts the bot on the new version. A running bot
  keeps the code it started with until its job is installed again, and
  `bot install` keeps the settings it was given before.

[readme-requirements]: ../README.md#requirements
[uv-lock]: ../uv.lock
[upgrade]: #upgrade
[pending]: #pending-a-ruling
[install]: ../README.md#installation
[settings]: settings.md
[brew]: https://brew.sh
[claude-code]: https://claude.ai/code
[uv]: https://docs.astral.sh/uv/
[ffmpeg]: https://ffmpeg.org
[delta-chat]: https://delta.chat
[tailscale]: https://tailscale.com
[pkg-kit]: https://github.com/sessionkit/sessionkit
[pkg-mail]: https://github.com/sessionkit/sessionmail
[pkg-tools]: https://github.com/sessionkit/sessiontools
[pkg-voice]: https://github.com/sessionkit/sessionvoice

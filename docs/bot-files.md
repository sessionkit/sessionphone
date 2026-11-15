# Bot files: where the bot keeps its state, logs and job

The bot keeps its account and lists in one root, its logs under the kit's
phone folder (or, under a root that is not the default, in that root), and
its launchd job in the user's LaunchAgents. Never move or delete an old log. The [sessionphone skill][skill] links here.

- **State:** `$SESSIONPHONE_ROOT`, else the root `bot install` keeps, else
  `~/.sessionkit/phone/state/`: the account (`accounts/`), `invite.txt`,
  `callers.txt`, `channels.txt` (where the bot may send: see
  [Channels][channels]), its `outbox/` (what the `dc-*` verbs queue), one
  dir per call under `calls/`, and `undelivered/` (mail to you that has not
  gone yet; `dead/` under it never goes).
- **Saved attachments:** `save_dir`, else `attachments/` in the bot's home
  (its root, or the profile's folder under it): one `chat-<chat id>/`
  folder per chat, and a stranger's files apart, under `untrusted/`. Folders
  0700, files 0600. Nothing prunes it. A program may watch it, leaving
  `untrusted/` out. See [Host-only keys][host-only].
- **`not-downloaded.json`**, in the bot's home (0600): every message the bot
  mailed as not downloaded, by message id, with its chat, its reason and
  whether it is a stranger's. It is state, not a setting: `dc-fetch` reads
  it, so it fetches only what this bot left undownloaded, and drops an entry
  once that message is mailed whole. Do not edit or delete it: `dc-fetch`
  refuses a message it does not name.
- **The account's own download limit:** on start, the bot also writes its
  `download_limit` into the account, Delta Chat's own setting, so the core
  leaves a big file on the server. `bot uninstall` does not open the
  account and leaves that value in it. The bot writes it again at its next
  start, and anything else run on the account meanwhile keeps the bot's
  last limit. Change the limit with `bot install --set download_limit=...`,
  never in the account itself.
- **Logs:** `~/.sessionkit/phone/log/<agent>/` (`<agent>/<profile>/` for a
  named profile): the bot's `bot.log` (`DC_LOG` overrides it) and
  `bot.launchd.log`, launchd's capture of its output. That is for the
  default root, `~/.sessionkit/phone/state/` (or the old `~/.sessionphone`),
  however it is named. A bot under any other root (`DC_ROOT`,
  `$SESSIONPHONE_ROOT` or the root `bot install` keeps), such as a test bot
  or a second bot, keeps both logs in that root instead:
  `<root>/log/<agent>/` (`<agent>/<profile>/` for a named profile). So it
  never writes into the default bot's logs. `sessionphone bot
  status` names the one it read. A bot that has not started on this version
  yet may still write `bot.log` in its root, and a job installed before the
  logs moved keeps `bot.launchd.log` there until `bot install` runs again.
  Never move or delete an old log.
- **Call sockets' index:** `~/.sessionkit/phone/run/<agent>/`
  (`<agent>.<profile>/` for a named profile): a symlink to each call socket
  the bot is serving, so the mouth and the ears find them whatever the root.
  See [Calls][calls-index].
- **The bot's job:**
  `~/Library/LaunchAgents/dev.sessionkit.phone.bot.AGENT.plist`. DC_*
  variables and sessionmail's mail-route settings set when you run `bot
  install` are carried into it, and nothing else from that shell.
- **Keys:** a plain SQLite file under `accounts/`. The only protection is
  the 0700 directory and the login; see [the profile reference][profile]
  for what else lives in the account.
- **The CLI:** `src/sessionphone/cli.py`.

[skill]: ../skills/sessionphone/SKILL.md
[channels]: channels.md
[profile]: ../skills/sessionphone/references/profile.md
[calls-index]: calls.md#where-a-program-finds-the-call-sockets
[host-only]: settings.md#host-only-keys

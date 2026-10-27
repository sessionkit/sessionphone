# Bot files: where the bot keeps its state, logs and job

The bot keeps its account and lists in one root, its logs under the kit's
phone folder, and its launchd job in the user's LaunchAgents. Never move or
delete an old log. The [sessionphone skill][skill] links here.

- **State:** `$SESSIONPHONE_ROOT`, else the root `bot install` keeps, else
  `~/.sessionkit/phone/state/`: the account (`accounts/`), `invite.txt`,
  `callers.txt`, `channels.txt` (where the bot may send: see
  [Channels][channels]), its `outbox/` (what the `dc-*` verbs queue), one
  dir per call under `calls/`, and `undelivered/` (mail to you that has not
  gone yet; `dead/` under it never goes).
- **Logs:** `~/.sessionkit/phone/log/<agent>/` (`<agent>/<profile>/` for a
  named profile): the bot's `bot.log` (`DC_LOG` overrides it) and
  `bot.launchd.log`, launchd's capture of its output. `sessionphone bot
  status` names the one it read. A bot that has not started on this version
  yet may still write `bot.log` in its root, and a job installed before the
  logs moved keeps `bot.launchd.log` there until `bot install` runs again.
  Never move or delete an old log.
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

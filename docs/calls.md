# Calls: the verbs on a call, and when they ring you

On a call the caller's words reach you as mail, one finished turn at a
time, and your reply is spoken into the call. Each turn's mail says how to
answer: the mouth first, `dc-say --call` if the mouth fails. The
[sessionphone skill][skill] keeps the short form and the turn-taking
rules; this page has the rest.

- Is a call live: `dc-live`.
- Answer: the line the call's mail ends with.
- End it: `dc-hangup CHAT_ID`.

## On a call

- `dc-live` answers whether a call is live, and which one. `dc-live --id`
  gives just the id. Exit 2 means no bot is running for this root, and its
  second stderr line says why: no `bot.pid` in the root, or a stale one. A
  call that began before the running bot started is not live: a bot
  stopped mid-call left it. `dc-live` says so on stderr and exits 1.
- **The mail says how to answer.** Each call turn's mail ends with
  `Answer with: mouth say --device call:N "text"`, where N is the call's
  id, and then `If the mouth fails:` with the `dc-say --call` line for that
  call and chat.
- `dc-say --call "$(dc-live --id)" CHAT_ID "text"` speaks into the call:
  the fallback, for when the mouth fails. If the call has ended, it refuses
  (exit 3) and sends nothing.
- A plain `dc-say CHAT "text"` is also read aloud into a call that is live
  in that chat. For a report, a log, or anything addressed to the chat and
  not to the person on the call, use `dc-say --text-only CHAT "text"`: it
  posts to the chat and never speaks. The ack ends `NOT spoken (chat
  only)`. `--text-only` with `--call` is a usage error (exit 1, nothing
  sent).
- `dc-hangup CHAT_ID` ends it.
- **A call that drops says so in its chat.** When a connected call ends and
  the bot did not end it (the connection failed, or the phone ended it), the
  bot posts one line to the call's chat, never spoken:
  `call dropped 14:05:30 after 600 s; last audio heard 20 s before the close`.
  A call that heard nothing from the caller ends the line
  `no audio was heard from the caller`. A call ended by `dc-hangup`, a
  decline, or the bot stopping posts nothing. The line goes through
  `channels.txt` like every in-call post; a refusal or a failed send is
  logged in `bot.log`, and the transcript still goes.
- **A call whose connection dies is ended on the phone too.** When the
  connection fails or closes and the phone never said it hung up, the bot
  ends the call in the core once, so the caller's app shows it over instead
  of leaving them talking into a dead call. The drop line still posts.
  `bot.log` says `ENDED msg <id> in the core: connection failed` (or
  `closed`); a failure to end it is logged there and never stops the
  transcript.
- Hearing them is the bot's job. It sends what they say to the
  speech-to-text server at `DC_STT_URL` ([Voice notes][voice-notes]) and
  mails you each finished turn. It also hands the call's audio to ears on
  `$SESSIONPHONE_ROOT/call-ears.sock`, where the ears keeper can wake you.
  See the ears skill. With `DC_EARS=1` in the bot's environment, the bot
  itself hears through ears instead of the speech-to-text server.
- **They can cut you off.** At the defaults a line stops when they talk
  over it, whichever path spoke it. The mouth cuts its own line when the
  ears on that socket hear them. The bot's barge-in (`DC_BARGE_IN`, on by
  default) cuts any line once their words are heard: a mouth line, a
  `dc-say` or a mail into the call. A short murmur such as "yeah" does
  not cut, though "stop" or "wait" does. Words that sound like your own
  voice coming back do not cut. With `DC_EARS=1` the bot's barge-in is
  off, and only the mouth cuts.

## When they ring you

- By default (`DC_ANSWER=auto`) the bot answers at once and greets in your
  voice. It's cheap: what they say lands in your box as mail, and no turn
  of yours is spent on the pick-up.
- **One greeting on a call: the bot's.** The `CALL CONNECTED` mail says
  they have already been greeted and quotes the line said: your
  `DC_GREETING`, the voicemail greeting, the hello on a call the bot made,
  or a line chosen for the call. Do not greet them again; reply to what
  they say. A greeting of yours would play straight after the bot's.
- With `DC_ANSWER=ring` in the bot's environment it rings you first. You
  get a `RINGING` mail naming the chat.
  - `dc-answer CHAT_ID` picks up, and says whether it did (exit 3: it had
    already gone to voicemail or ended). The phone rings on for the ~5 s
    the bot takes to connect. Then talk as on any call.
  - `dc-hangup CHAT_ID` declines. You get a `MISSED` mail, as you do when
    they hang up first.
  - No pick-up within `DC_RING_WAIT` seconds (default 25) and it answers as
    voicemail (`DC_GREETING_VOICEMAIL`). What they say still reaches you as
    mail.

## Where a program finds the call sockets

The mouth and the ears reach a call through the bot's two sockets,
`call-mouth.sock` and `call-ears.sock`. They are in the bot's home: its root,
or `<root>/<profile>/` for a named profile, unless `DC_MOUTH_SOCK` or
`DC_EARS_SOCK` names another path. A bot's root can be set in its job, in
`phone.json` or by default, so a program on the same Mac cannot always work
it out. The bot therefore also says where its sockets are, in one fixed
folder. This is a stable contract: a reader may rely on it.

```text
~/.sessionkit/phone/run/<agent>/call-mouth.sock            -> the bot's mouth socket
~/.sessionkit/phone/run/<agent>/call-ears.sock             -> the bot's ears socket
~/.sessionkit/phone/run/<agent>.<profile>/call-mouth.sock  (a named profile)
~/.sessionkit/phone/run/<agent>.<profile>/call-ears.sock
```

- Each entry is a symlink to the socket the bot actually bound, made once
  the socket is listening. An override (`DC_MOUTH_SOCK`, `DC_EARS_SOCK`) is
  linked as it was bound.
- `<agent>` is the bot's agent name. The `.<profile>` suffix is added only
  for a named profile; the default profile's entry is the bare name.
- `run/` and each entry folder are mode 0700, as the sockets are 0600: only
  the user the bot runs as can read them.
- To find every bot's mouth, glob `~/.sessionkit/phone/run/*/call-mouth.sock`
  and connect to each. The probe (`{"probe": true}`) answers with the bot's
  agent, profile and live calls.
- A link left by a bot that crashed points at a socket nobody serves:
  `connect()` fails, as it does on a stale socket. Skip it. A restarted
  bot replaces its own link, and at a normal exit removes it if it still
  points at its socket.
- Only the bot publishes here. The web demo's sockets (`sessionphone web
  --legs`) are not listed.
- If the folder cannot be written, the bot logs it and serves its sockets
  anyway.

[skill]: ../skills/sessionphone/SKILL.md
[voice-notes]: call-hearing.md#voice-notes

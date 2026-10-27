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
- Hearing them is the bot's job. It sends what they say to the
  speech-to-text server at `DC_STT_URL` ([Voice notes][voice-notes]) and
  mails you each finished turn. Only with `DC_EARS=1` in the bot's
  environment does it listen through ears instead and hand the call's
  audio to ears on `$SESSIONPHONE_ROOT/call-ears.sock`, where the ears
  keeper can wake you. See the ears skill.

## When they ring you

- By default (`DC_ANSWER=auto`) the bot answers at once and greets in your
  voice. It's cheap: what they say lands in your box as mail, and no turn
  of yours is spent on the pick-up.
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

[skill]: ../skills/sessionphone/SKILL.md
[voice-notes]: call-hearing.md#voice-notes

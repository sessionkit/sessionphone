# Call hearing: will you hear them, and they you

Whether a call works both ways comes down to two lines: `hearing:` (the bot
can turn the caller's words into text) and `speaking:` (the bot can speak
your replies). `setup` prints `hearing:`; `bot install` and `bot status`
print both. `yes` on both means a two-way call. Anything else, tell the
person before you ring. The [sessionphone skill][skill] links here from
its setup steps.

- **Is this Session Kit?** It is when `command -v sessionkit` prints a
  path. The answer decides which speech server to offer: see "On `NO` or
  `not confirmed`" below.
- **The speech-to-text endpoint** is `DC_STT_URL`: see
  [Voice notes](#voice-notes).

## At setup: read `hearing:` before you link

**Read `setup`'s last lines, `hearing:`, before you link.** Setup sends a
half-second test clip to the speech-to-text server the bot will use
(`DC_STT_URL` in this shell, else the default) and says what answered.
`yes` is one line: go on. On `NO` or `not confirmed` the speech server is
missing or not ready, and installing it can take minutes and gigabytes, so
deal with it now, not after the link. Under Session Kit the line names the
kit's step, `ears services install --only speech` (read `ears services
status` first; below says how to read its `speech` row). Standalone it
names VoiceMode's (the cost rules below apply). Either is the user's to
run, on their yes. To check again, run the same `setup` line again.
Setup's exit code does not depend on it.

## At bot install: `hearing:` and `speaking:`

- **Will you hear them?** `bot install` ends with a `hearing:` line. It
  says `probing URL (read-only)`, sends a half-second test clip to the
  speech-to-text server at `DC_STT_URL`, and says what answered. `yes`
  means you will hear them. `NO` means the call connects but you will not
  hear what they say: tell them so before you ring. `not confirmed` means
  it could not tell: read the line's reason, fix what it names, then run
  `bot install` again (if the server was only slow to load its model, wait
  a few minutes first). Ears is not needed to hear them. When `ears
  services weights` can say where the speech models stand, the line says
  more: `downloading` with how far it has got means they are still coming
  to this Mac and the first call waits for them, so wait, then run `bot
  install` again; `not downloaded yet` means the server's first request
  fetches them, and the first call waits for that. On that first call the
  caller is not left in silence: the bot says the voice model is
  downloading and plays the music file `DC_WAIT_MUSIC` names, if one is
  there (any audio file the bot can decode; none ships), until the call
  can hear. It stops at once when the greeting or any line is spoken.
  `DC_FIRST_CALL_WAIT=off` turns it off.
- **Will they hear you?** After `hearing:`, `bot install` prints a
  `speaking:` line. `yes` means it found the ffmpeg the bot needs to speak
  through the speech server. `NO` means every spoken reply will go to the
  caller as chat text: tell them before you ring, and give them the fix the
  line shows (`brew install ffmpeg`, then `sessionphone bot install`
  again). Installing it is theirs to run. `sessionphone bot status` prints
  the same line.
- **Later, `sessionphone bot status`** asks the job's speech-to-text too,
  in one `hearing:` line, capped at a few seconds. `NO` names the URL that
  did not answer: run `sessionphone bot install` to see why and how to fix
  it. `not confirmed` there can be a server still loading its model.
- **On `NO` or `not confirmed`, first ask whether this is Session Kit.** It
  is when `command -v sessionkit` prints a path. The kit's own `ears
  services install` loads an mlx-audio server on `127.0.0.1:8890`; a
  second mlx-audio job beside it leaves one of the two crash-looping.
  - **Under the kit,** run `ears services status` (read-only) and read its
    `speech` row. Never offer `voicemode service install mlx-audio`. The
    `hearing:` line agrees: it finds the same `sessionkit` on PATH, points
    at `ears services status`, and does not name VoiceMode's command.
    `mlx-audio on :8890`: the server is up, and its first request downloads
    the model, so hearing reads `not confirmed` (or `NO`) for a minute or
    more. Wait, then run `sessionphone bot install` again; if it still
    fails, its reason says why. `none on :8890`: no server and no job.
    `ears services install` loads the kit's; it is theirs to run, in the
    form that fits their Mac, as the `hearing:` line shows: `ears services
    install --no-agent` on a Mac that only runs the bot, with no agent seat
    for ears to wake, or `ears services install --agent NAME` where ears
    should wake an agent seat (NAME) when they speak. A bare install that
    names no seat to wake is refused; ask which fits. `none answering`: a
    job the row names is loaded but down or still starting, and install
    will not add a second. `:8890 held`: something holds the port but does
    not answer as mlx-audio. For either, tell them who the row names, and
    install nothing over it. No `speech` row: this kit's ears loads no
    speech server, so offer VoiceMode's, as below.
  - **Standalone** (no `sessionkit`), on `NO`, offer the STT the line
    names, VoiceMode's mlx-audio on `127.0.0.1:8890`. sessionphone does not
    install it. Tell them the cost first: it needs VoiceMode, it runs as a
    background service at every login, and its first request downloads the
    model the bot asks for (`mlx-community/parakeet-tdt-0.6b-v3`), which is
    large. Then give them VoiceMode's own command, `voicemode service
    install mlx-audio`. Run it only on their explicit yes, given in this
    conversation; otherwise it is theirs to run. Then run `sessionphone bot
    install` again and read its `hearing:` line.

## Voice notes

A voice note arrives as its transcript. The bot posts it to an
OpenAI-compatible `/v1/audio/transcriptions` endpoint, and names a model on
every request. A call uses the same endpoint for what the caller says.

- `DC_STT_URL` is the endpoint. The default is an mlx-audio server at
  `mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions`: the kit's own
  speech server under Session Kit, or VoiceMode's standalone (the section
  above says which to offer). It is the server the bot's voice uses too.
  `ears serve` (Apple speech, macOS 27) is
  `http://127.0.0.1:2023/v1/audio/transcriptions`. Name its kind with a
  prefix, which is stripped before use, as the default does. Kinds:
  `mlx-audio+`, `whisper+` (whisper.cpp), `openai+`, `apple+` (ears). An
  untagged URL is guessed from its port: 8890 is mlx-audio, 2022 is
  whisper, 2023 is apple, and an openai.com host is openai.
- `DC_STT_MODEL` is the model to send. Unset, it suits the kind:
  `mlx-community/parakeet-tdt-0.6b-v3` (mlx-audio), `large-v2` (whisper,
  which ignores it), `whisper-1` (openai), `apple-speechtranscriber`
  (apple). The first three are VoiceMode's defaults. The apple row is
  sessionphone's, ears' own model id, until VoiceMode adopts it.
- No kind and no `DC_STT_MODEL`: the bot refuses to start, and `bot
  install` refuses too, with exit 2, before it writes anything. Both say to
  tag `DC_STT_URL` or set `DC_STT_MODEL`. An HTTP error logs its status and
  the server's reply (`TRANSCRIBE HTTP 422 ...`), and so does a reply with
  no text.

[skill]: ../skills/sessionphone/SKILL.md

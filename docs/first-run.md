# What the first run fetches

`sessionphone` needs `uv`, and it fetches what it needs in two steps: a
small setup first, then the media pack once. The agent runs the command and
lets it fetch; it never asks first. The [sessionphone skill][skill] links
here.

- **Setup comes first, and it is small.** `setup`, `qr` and `link` need four
  packages, about 10 MB. uv uses a Python already on the Mac when it is 3.11
  to 3.14; only a Mac with none also fetches Python (about 25 MB).
- **The media pack: under Session Kit it is already installed.** The bot,
  calls and the web page need it: aiortc, av, numpy and their kin. The kit
  installs sessionphone with the pack, so `bot install` fetches nothing. Never
  ask the user about it. ([Call hearing][hearing] says how to tell it is
  Session Kit.)
- **Standalone, the pack comes once, at the first `bot install`** (or
  `bot run`, or `web`), about 31 MB. Run the command and let it fetch; do not
  ask first. One line says so before the fetch starts. If that is the first
  command of all, the same fetch also brings setup's four packages and
  Python.
- **On a slow link that first `bot install` can take minutes**, longer than
  a Bash call's default two-minute timeout. Give that call a longer timeout.
- **If the fetch fails, it exits 2 and says so.** Nothing else changes, and
  setup, qr and link still work. Run the same command again. uv keeps the
  packages it finished, so the re-run goes on from there (inferred from uv's
  cache, not measured).

[skill]: ../skills/sessionphone/SKILL.md
[hearing]: call-hearing.md

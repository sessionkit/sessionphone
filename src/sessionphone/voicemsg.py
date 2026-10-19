"""voicemsg - may this file go out as a Delta voice message? Stdlib only.

The core never re-types a Voice message and never inspects its bytes, so
whatever we hand it as Voice goes out as Voice: a `notes.txt`, or an empty
`.ogg` left by a failed TTS run, would arrive as a voice message that cannot
play. This is the check that stops that.

Accepted are the seven suffixes the core itself types as Audio, in any case,
and the first bytes must agree with the suffix:

    .ogg .oga   OggS
    .m4a        ftyp at offset 4
    .mp3        ID3, or an MPEG layer III frame sync
    .aac        an ADTS frame sync, or ID3
    .wav        RIFF....WAVE
    .flac       fLaC

There is one checker with two callers, as with `channels`. The tool
(`dc-send-file --voice-message`) runs it before it queues a job, through
`main()`; the bot runs `problem()` again as the authority, because an outbox's
job never passes through the tool.
"""
from __future__ import annotations

import pathlib
import sys

from sessionphone import paths

SUFFIXES = (".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".wav")
HEAD = 12   # bytes read: the longest signature, RIFF....WAVE


def _id3(h: bytes) -> bool:
    return h[:3] == b"ID3"


def _mp3_frame(h: bytes) -> bool:
    # 11 sync bits, then layer III: byte 2 is 111V VLLP with LL = 01.
    # A UTF-16LE byte-order mark, FF FE, has LL = 11 and fails.
    return len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and (h[1] & 0x06) == 0x02


def _adts(h: bytes) -> bool:
    # 12 sync bits, then the ID bit, then a layer of 00: byte 2 is 1111 I00P.
    return len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xF6) == 0xF0


# suffix -> (what the bytes should be, as said in a refusal; the test)
SIGNATURES = {
    ".ogg": ("OggS", lambda h: h[:4] == b"OggS"),
    ".oga": ("OggS", lambda h: h[:4] == b"OggS"),
    ".m4a": ("ftyp at byte 4", lambda h: h[4:8] == b"ftyp"),
    ".mp3": ("ID3 or an MPEG layer III frame", lambda h: _id3(h) or _mp3_frame(h)),
    ".aac": ("an ADTS frame or ID3", lambda h: _adts(h) or _id3(h)),
    ".wav": ("RIFF....WAVE", lambda h: h[:4] == b"RIFF" and h[8:12] == b"WAVE"),
    ".flac": ("fLaC", lambda h: h[:4] == b"fLaC"),
}
assert set(SIGNATURES) == set(SUFFIXES)


def accepted() -> str:
    """The accepted suffixes, as a refusal names them."""
    return " ".join(SUFFIXES)


def problem(path: str | pathlib.Path) -> str | None:
    """None if PATH may go out as a voice message, else why not (one phrase,
    without the path: the caller names the file)."""
    p = pathlib.Path(path)
    suffix = p.suffix.lower()
    if suffix not in SIGNATURES:
        return (f"{suffix or 'no suffix'} is not a voice-message suffix "
                f"(accepted: {accepted()})")
    try:        # paths.*, not Path.*: on 3.14 a path below an unsearchable dir reads "absent"
        there, regular = paths.exists(p), paths.is_file(p)
    except OSError as e:
        return f"cannot be read: {e.strerror or e}"
    if not there:
        return "no such file"
    if not regular:
        return "not a regular file"
    try:
        with p.open("rb") as f:
            head = f.read(HEAD)
    except OSError as e:
        return f"cannot be read: {e.strerror or e}"
    if not head:
        return "the file is empty"
    want, ok = SIGNATURES[suffix]
    if not ok(head):
        return f"not {suffix[1:]} audio: its first bytes are not {want}"
    return None


def main(argv: list[str] | None = None) -> int:
    """check [--tool NAME] PATH

    For the dc-* tools. Exit 0, printing nothing, if PATH may go out as a
    voice message. Else exit 1, and stderr names the file, why, and the
    accepted suffixes. Usage: exit 2.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="python -m sessionphone.voicemsg")
    sub = ap.add_subparsers(dest="verb", required=True)
    ck = sub.add_parser("check")
    ck.add_argument("--tool", default="voicemsg")
    ck.add_argument("path")
    a = ap.parse_args(argv)
    why = problem(a.path)
    if why:
        print(f"{a.tool}: not sent as a voice message: {a.path}: {why}. "
              f"A voice message is one of {accepted()}, and its bytes must match.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

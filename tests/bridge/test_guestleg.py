#!/usr/bin/env python3
"""The 77 stranded guest mails, turned into a test.

    BOT=/path/to/delta-chat/bot .venv/bin/python test_guestleg.py

With GUEST_BOX set to a bot's REAL maildir, also runs `candidates()` against
it, which is where the fault was found: two mails sent into a live call on
2030-06-09 landed in cur/ as `:2,` and were never spoken.
"""
import os
import pathlib
import sys
import tempfile
import time

from sessionphone.bridge import calls

calls._hooks = calls.Hooks(lambda m: None, lambda p: "", lambda *a, **k: None)


def touch(p, age=0):
    p.write_text("From: x@y\n\nhi\n")
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def sweeper(box):
    calls.BOT_BOX = box
    s = object.__new__(calls.MailLeg)
    return s


def main():
    box = pathlib.Path(tempfile.mkdtemp())
    (box / "new").mkdir()
    (box / "cur").mkdir()

    fresh_new = touch(box / "new" / "1000.aaa.example")
    stranded = touch(box / "cur" / "1001.bbb.example:2,")       # notmuch got it
    spoken = touch(box / "cur" / "1002.ccc.example:2,S")        # seen() wrote S
    ancient = touch(box / "cur" / "1003.ddd.example:2,", age=86400)  # a past call

    names = {f.name for f in sweeper(box).candidates()}

    assert fresh_new.name in names, "stopped reading new/"
    assert stranded.name in names, "still blind to what notmuch moved"
    assert spoken.name not in names, "would speak the same mail twice"
    assert ancient.name not in names, "would resurrect mail from a past call"

    # flags, not directories: a cur/ name says whether we spoke it
    assert "S" in spoken.name.rsplit(":2,", 1)[-1]
    assert "S" not in stranded.name.rsplit(":2,", 1)[-1]

    print("unit: new/ + cur/`:2,` found, `:2,S` skipped, stale skipped")

    real = os.environ.get("GUEST_BOX")
    real_box = pathlib.Path(real).expanduser() if real else None
    if real_box and real_box.is_dir():
        cur = real_box / "cur"
        missed = [f for f in cur.iterdir()
                  if f.is_file() and "S" not in f.name.rsplit(":2,", 1)[-1]]
        print(f"real box: {len(missed)} guest mails delivered and never spoken "
              f"(oldest {time.strftime('%Y-%m-%d', time.localtime(min(f.stat().st_mtime for f in missed)))})"
              if missed else "real box: none missed")
        calls.BOT_BOX = real_box
        found = sweeper(real_box).candidates()
        recent = [f for f in found if "S" not in f.name.rsplit(":2,", 1)[-1]]
        print(f"          the fix picks up {len(recent)} of them inside its "
              f"{calls.MailLeg.CUR_WINDOW}s window")


main()

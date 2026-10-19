"""ack - the bot's yes or no to a job's writer, written where the job asked.

It answers EVERY job, a chat send as much as a call, so it cannot live in
calls.py: a chat-only bot never imports calls, nor the media pack calls
imports. Standard library only.
"""
import json, pathlib, sys


def write(path, ok, detail="", log=None):
    """{"ok": bool, "detail": str} at path, atomically; nothing when the job
    named no path. A failure is logged through log (stderr when None), never
    raised: the writer then sees no ack, which already means "no answer"."""
    if not path:
        return
    try:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_text(json.dumps({"ok": bool(ok), "detail": detail}))
        tmp.replace(p)          # atomic: a reader never sees half a file
    except Exception as e:
        msg = f"ack write failed for {path}: {e}"
        try:
            (log or (lambda m: print(m, file=sys.stderr)))(msg)
        except Exception:
            print(msg, file=sys.stderr)

"""The caller's name on a web call: ONE setting, ONE reader.

Every user-facing place that names the person on a web call reads it from
here, and none keeps a default of its own:

    the auth-ok frame          web/server.py   {"type": "auth-ok", "user": NAME}
    each finished turn's mail  web/turns.py    Subject "[web N] NAME: ...", body "from: NAME"
    a new passkey's label      web/passkeys.py WebAuthn user name / displayName

The sources, in order: `sessionphone web --caller-name NAME`, else the env var
DC_CALLER_NAME, else DEFAULT, a role word. A blank or whitespace-only value
falls through to the next source.

The name is a display label and grants nothing: who may sign in is the
passkey store's alone (matched by rp_id, web/passkeys.py), so an env source
here does not break "env never grants privilege". A rename only
changes the label on future enrols and orphans no passkey.

Control characters are dropped and whitespace runs collapse to one space,
because the name goes into a mail Subject and a JSON frame.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT = "owner"          # the passkey holder of a web call is the host's owner: a role, not a person
ENV = "DC_CALLER_NAME"     # the kit's DC_ env contract, beside DC_MAILTO and DC_AGENT
MAX_LEN = 64               # a label, not a paragraph: the Subject is cut to 69 anyway


def clean(raw: object) -> str:
    """`raw` as a one-line label: control characters out, whitespace collapsed, cut to MAX_LEN."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(raw or ""))
    return " ".join(text.split())[:MAX_LEN].strip()


def caller_name(flag: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    """--caller-name, else $DC_CALLER_NAME, else DEFAULT; blank falls through."""
    e = os.environ if environ is None else environ
    for raw in (flag, e.get(ENV)):
        name = clean(raw)
        if name:
            return name
    return DEFAULT

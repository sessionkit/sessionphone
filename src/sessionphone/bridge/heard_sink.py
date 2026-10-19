"""heard_sink - a Delta call's words into VoiceMode's heard log (M8).

VoiceMode keeps one append-only log of what it hears,
``~/.voicemode/logs/conversations/heard_YYYY-MM-DD.jsonl``, and every
agent's ride-along hook and wake waiter read it. A call is one more pair of
ears: its chunks go in as ``partial`` lines and its turns as ``turn`` lines,
``source: "call"``, the bridge's account and the chat as ``device``, so the
hook and the waiter serve a call exactly as they serve the mic.

The writing is VoiceMode's own ``heard.py``, loaded by path, not a copy of
its protocol here (one lock, seq across midnight, one write per line): two
implementations of a lock protocol drift. ``heard.py`` is stdlib-only, and
``voicemode claude hooks add heard`` installs it as
``~/.voicemode/bin/voicemode-heard-hook``, which is the default path.

OFF unless ``DC_HEARD=1``, and then ONLY for the chat ids in
``DC_HEARD_CHATS`` (comma-separated). What lands in this log lands in
every agent's context on its next tool call, so a stranger's call must
never reach it: list the owner's own verified 1:1 chat, where only their key can
speak. The public relay's contact-request refusal is not enough on its own.

Never raises into the call: a sink that cannot write says so once in the
bridge log and goes quiet, and the call carries on.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import threading
from typing import Callable, Optional

from sessionphone.bridge import dc_env

DEFAULT_HEARD_PY = pathlib.Path.home() / ".voicemode" / "bin" / "voicemode-heard-hook"

_lock = threading.Lock()
_module = None
_failed: Optional[str] = None
_said: set[str] = set()


def enabled() -> bool:
    return (dc_env.env("HEARD", "0") or "0").strip().lower() in ("1", "on", "true", "yes")


def allowed_chats() -> set[int]:
    raw = dc_env.env("HEARD_CHATS", "") or ""
    out = set()
    for part in raw.replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def heard_py() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(dc_env.env("HEARD_PY") or str(DEFAULT_HEARD_PY)))


def _once(log: Callable[[str], None], key: str, msg: str) -> None:
    if key not in _said:
        _said.add(key)
        log(msg)


def _load(log: Callable[[str], None]):
    """heard.py as a module, once; None (said once) if it cannot be had."""
    global _module, _failed
    with _lock:
        if _module is not None or _failed is not None:
            return _module
        path = heard_py()
        try:
            loader = importlib.machinery.SourceFileLoader("voicemode_heard", str(path))
            spec = importlib.util.spec_from_loader("voicemode_heard", loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            for name in ("partial", "turn", "event"):
                if not callable(getattr(mod, name, None)):
                    raise ImportError(f"{path} has no heard.{name}()")
            _module = mod
            log(f"heard: writing call lines through {path}")
        except Exception as e:
            _failed = f"{type(e).__name__}: {e}"
            log(f"heard: DC_HEARD=1 but {path} could not be loaded ({_failed}); "
                "no call lines will be written. `voicemode claude hooks add heard` "
                "installs it, or set DC_HEARD_PY.")
        return _module


def _reset_for_tests() -> None:
    global _module, _failed
    with _lock:
        _module, _failed = None, None
        _said.clear()


class CallSink:
    """One call's writer. Every method is safe to call when OFF or refused."""

    def __init__(self, chat_id: int, log: Callable[[str], None], *,
                 chat_name: Optional[str] = None, call: Optional[str] = None):
        self.chat_id = int(chat_id)
        self.log = log
        self.chat_name = chat_name
        self.call = call

    @property
    def device(self) -> str:
        return f"delta:{dc_env.AGENT}/{self.chat_id}"

    def _heard(self):
        if not enabled():
            return None
        if self.chat_id not in allowed_chats():
            _once(self.log, f"refused-{self.chat_id}",
                  f"heard: chat {self.chat_id} is not in DC_HEARD_CHATS - its words stay "
                  "out of the heard log (only the owner's own chat may feed every agent's context)")
            return None
        return _load(self.log)

    def _write(self, fn_name: str, *args, **kw) -> Optional[dict]:
        mod = self._heard()
        if mod is None:
            return None
        try:
            return getattr(mod, fn_name)(*args, source="call", device=self.device,
                                         agent=dc_env.AGENT, chat_id=self.chat_id,
                                         chat_name=self.chat_name, call=self.call, **kw)
        except Exception as e:
            _once(self.log, f"write-{type(e).__name__}",
                  f"heard: a call line was not written ({type(e).__name__}: {e})")
            return None

    def partial(self, text: str, **kw) -> Optional[dict]:
        text = (text or "").strip()
        return self._write("partial", text, **kw) if text else None

    def turn(self, text: str, detector: Optional[str] = None, **kw) -> Optional[dict]:
        text = (text or "").strip()
        return self._write("turn", text, detector=detector, **kw) if text else None

    def event(self, name: str, **kw) -> Optional[dict]:
        return self._write("event", name, **kw)

    # >>> said-rows: the bridge's own voice, as the mouth writes it.
    # A line spoken into the call (dc-say, or any speak()) goes in as the
    # mouth's pair: `saying` when it starts (with the text), `said` when it
    # ends. Then Talk shows both sides of a call, the waker trims its echo,
    # and "a reply to my last line" works on a call as it does in the room.
    # Optional in heard.py: an older one has no saying()/said(), which is
    # said once and skipped - the call itself never notices.

    def _has(self, name: str) -> bool:
        mod = self._heard()
        if mod is None:
            return False
        if callable(getattr(mod, name, None)):
            return True
        _once(self.log, f"no-{name}",
              f"heard: {heard_py()} has no heard.{name}() - lines spoken into the call "
              "stay out of the heard log; point DC_HEARD_PY at a heard.py that has it "
              "(the mouth's)")
        return False

    def saying(self, text: str, *, utt: str, **kw) -> Optional[dict]:
        text = (text or "").strip()
        if not text or not self._has("saying"):
            return None
        return self._write("saying", text, utt=utt, **kw)

    def said(self, *, utt: str, **kw) -> Optional[dict]:
        if not self._has("said"):
            return None
        return self._write("said", utt=utt, **kw)
    # <<< said-rows


class NullSink:
    """CallSession's class-level default, so a session built without
    __init__ (the script tests do) writes nothing instead of raising."""
    chat_name = None

    def partial(self, *a, **k):
        return None

    def turn(self, *a, **k):
        return None

    def event(self, *a, **k):
        return None

    def saying(self, *a, **k):
        return None

    def said(self, *a, **k):
        return None

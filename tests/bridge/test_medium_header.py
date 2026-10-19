"""PHONE-40: every spoken call line says so in its headers.

A bridged call line carries `Medium: voice`, the turn and the PREVIEW alike,
so one pager rule can wake on it.
The real CallSession methods run; mail_in is captured. No call, no network.
"""
import asyncio
import types

import pytest

from sessionphone.bridge import calls


class _NoSink:
    def turn(self, *a, **k):
        pass

    def partial(self, *a, **k):
        pass


def _session():
    s = object.__new__(calls.CallSession)
    s.chat_id, s.chat_name, s.caller = 12, "Bob", "Bob"
    s.lines, s.preview_id = [], None
    s.root_id = "call-test@example.org"
    s.guest_list = list
    s.heard = _NoSink()
    return s


@pytest.fixture
def mailed(monkeypatch):
    out = []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(
        lambda m: None, lambda p: "", lambda *a, **k: out.append((a, k))))
    return out


def test_a_turn_carries_medium_voice(mailed):
    asyncio.run(_session()._mail_turn("can you check the build"))
    (args, kw), = mailed
    assert args[3] == "(on the call) can you check the build"
    assert kw["headers"] == {"Medium": "voice"}


def test_a_preview_carries_medium_voice_beside_its_own(mailed):
    p = types.SimpleNamespace(text="can you check", turn=3, seq=2, at=10.0, turn_start=8.0)
    asyncio.run(_session()._preview_out(p))
    (args, kw), = mailed
    assert "PREVIEW" in args[3]
    assert kw["headers"] == {"X-Call-Preview": "3.2", "Medium": "voice"}

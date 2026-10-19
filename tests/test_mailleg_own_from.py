"""PHONE-106 do-004, contract §2.3: the mail leg skips the bridge's own mail by
its LOCAL PART, the host discarded.

The seat's From is `<agent>-chat`, qualified by the MTA, so it can arrive as
`ada-chat@host`, `ada-chat@example.net` or bare, while dc_env.ADDR is spelt with
getfqdn()'s host. Each must be skipped (marked read, never spoken); a mail from
another local part on the same host must still be spoken. Fakes only: a scratch
maildir, a fake call session, no loop, no TTS, no Delta Chat.
"""
import pytest

from sessionphone.bridge import calls, dc_env

ROOT = "call-12-1789@ms5.elsewhere.example"
ADDR = "ada-chat@ms5.elsewhere.example"         # another host than every From below


class Session:
    pc, chat_id, root_id, account = object(), 12, ROOT, None

    async def speak(self, text, voice, who):
        return None


class Manager:
    def __init__(self):
        self.sessions, self.loop = {1: Session()}, None


@pytest.fixture
def leg(tmp_path, monkeypatch):
    box = tmp_path / "box"
    (box / "new").mkdir(parents=True)
    monkeypatch.setattr(calls, "BOT_BOX", box)
    monkeypatch.setattr(calls, "VOICE_MAP_FILE", tmp_path / "voices.json")
    monkeypatch.setattr(dc_env, "ADDR", ADDR)
    logged, spoken = [], []
    monkeypatch.setattr(calls, "_hooks", calls.Hooks(logged.append, None, None))
    monkeypatch.setattr(calls, "reply_seen", lambda *a: None)
    monkeypatch.setattr(calls, "post_to_chat", lambda *a: True)

    def run(coro, loop):
        spoken.append(coro.cr_frame.f_locals.get("text"))
        coro.close()
    monkeypatch.setattr(calls.asyncio, "run_coroutine_threadsafe", run)

    class Rig:
        def drop(self, frm, name="m1"):
            f = box / "new" / name
            f.write_text(f"From: {frm}\nTo: ada@host\nSubject: re: call\nIn-Reply-To: <{ROOT}>\n"
                         "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\nHello the call.\n")
            calls.MailLeg(Manager()).one(f)
            return f
    r = Rig()
    r.box, r.logged, r.spoken = box, logged, spoken
    return r


@pytest.mark.parametrize("frm", [
    "Ada's Delta Chat bridge <ada-chat@host>",
    "Ada's Delta Chat bridge <ada-chat@example.net>",
    "ada-chat",
    "<ADA-Chat@Example.Net>",
    "ada-chat@ms5.elsewhere.example",                   # today's exact spelling still skips
], ids=["short host", "fqdn", "bare", "case", "exact ADDR"])
def test_the_bridges_own_mail_is_skipped_whatever_its_host(leg, frm):
    f = leg.drop(frm)
    assert leg.spoken == []
    assert not f.exists() and [p.name for p in (leg.box / "cur").iterdir()] == ["m1:2,S"]


@pytest.mark.parametrize("frm", [
    "Alice <alice@host>",
    "Ada <ada@ms5.elsewhere.example>",                  # the agent itself, not its bridge
    "ada-chatter@ms5.elsewhere.example",                # a prefix is not the local part
    "Ada chat <ada-chat.x@host>",
], ids=["another agent", "the agent", "prefix", "dotted"])
def test_another_local_part_is_still_spoken(leg, frm):
    leg.drop(frm)
    assert leg.spoken == ["Hello the call."]


@pytest.mark.parametrize("addr, want", [
    ("ada-chat@host", "ada-chat"), ("ADA-chat@example.net", "ada-chat"), ("ada-chat", "ada-chat"),
    (" ada-chat@host ", "ada-chat"), ("a@b@c", "a@b"), ("", ""), (None, ""), ("@host", ""),
])
def test_local_part(addr, want):
    assert calls.local_part(addr) == want


@pytest.mark.parametrize("addr", ["", None, "@ms5.elsewhere.example", "@host"])
def test_no_local_part_is_never_own(monkeypatch, addr):
    """An empty From (parseaddr's answer to a header it cannot read) is not ours."""
    monkeypatch.setattr(dc_env, "ADDR", ADDR)
    assert calls.own_mail(addr) is False

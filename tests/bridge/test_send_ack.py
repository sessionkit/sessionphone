"""The outbox loop answers every job: "taken" is not "sent". The bot
logged SEND FAILED, unlinked the job, and the tool had already said it went.

One pass of main()'s outbox loop over fakes: each job's ack says what was
done, or why not, and is on disk before the job is unlinked.
Fakes only: no account, no rpc-server.
"""
import json
import threading
import time

import pytest

from sessionphone.bridge import bot


class Done(Exception):
    """Ends main()'s loop at its first sleep: one pass of the outbox."""


class BotTime:
    """bot.py's `time`: the time module, but sleep raises Done. Only bot.py's
    own calls see it; the start probe's child wait and every thread keep the
    real one (patched process-wide, the pass could end inside the probe, PHONE-171)."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, s):
        raise Done


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


SENT = []      # every send_message call's kwargs, in order (PHONE-118: the view type)


class FakeChat:
    def __init__(self, cid):
        self.id = cid

    def send_text(self, text):
        return Obj(id=77)

    def send_message(self, **kw):
        SENT.append(dict(kw, chat=self.id))
        return Obj(id=78)


class FakeAccount:
    def is_configured(self):
        return True

    def start_io(self):
        pass

    def get_config(self, key):
        return "carol@test"

    def get_chat_by_id(self, cid):
        if cid == 99:
            raise ValueError("no chat 99")
        return FakeChat(cid)

    def get_message_by_id(self, mid):
        return Obj(id=mid, send_reaction=lambda r: Obj(id=79),
                   get_snapshot=lambda: Obj(chat_id=13))


class FakeRpc:
    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    out = tmp_path / "outbox"
    stop = threading.Event()
    lst = tmp_path / "channels.txt"         # PHONE-102: the chats these jobs go to are listed
    lst.write_text("thirteen  chat:13\nninetynine  chat:99\n")
    lst.chmod(0o600)
    monkeypatch.setenv("DC_CHANNELS_FILE", str(lst))
    monkeypatch.setattr(bot, "check_root", lambda: None)
    monkeypatch.setattr(bot, "OUTBOX", out)
    monkeypatch.setattr(bot, "Rpc", FakeRpc)
    monkeypatch.setattr(bot, "DeltaChat", lambda rpc: Obj(get_all_accounts=lambda: [FakeAccount()]))
    monkeypatch.setattr(bot, "server_choice", lambda: (None, "package", "test"))
    monkeypatch.setattr(bot, "sweep", lambda account: None)
    monkeypatch.setattr(bot, "handle_events", lambda account: stop.wait())
    monkeypatch.setattr(bot, "CALLS", False)
    monkeypatch.setattr(bot, "INVITE", None)
    logged = []                             # PHONE-118 R5: the log is captured, not muted
    monkeypatch.setattr(bot, "log", logged.append)
    SENT.clear()

    unlinked_with_ack = []
    real_unlink = type(out).unlink

    def unlink(self, missing_ok=False):     # was the ack there when the job went?
        unlinked_with_ack.append(json.loads(self.read_text()).get("ack") and
                                 (tmp_path / "acks" / self.name).exists())
        real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(type(out), "unlink", unlink)

    monkeypatch.setattr(bot, "time", BotTime())

    def run(*jobs):
        run.logged, run.sent = logged, SENT
        out.mkdir(parents=True, exist_ok=True)
        for i, job in enumerate(jobs):
            job["ack"] = str(tmp_path / "acks" / f"{i:02d}.json")
            (out / f"{i:02d}.json").write_text(json.dumps(job))
        with pytest.raises(Done):
            bot.main()
        stop.set()
        assert not list(out.glob("*.json"))                 # every job unlinked
        assert all(unlinked_with_ack)                       # ...after its ack
        return [json.loads((tmp_path / "acks" / f"{i:02d}.json").read_text())
                for i in range(len(jobs))]
    return run


def test_every_job_is_answered(outbox):
    sent, failed, refused, reacted = outbox(
        {"chat_id": 13, "text": "hello"},
        {"chat_id": 99, "text": "hello"},
        {"call": 13},                                       # calls are off here
        {"msg_id": 5, "reaction": "+1"},
    )
    assert sent == {"ok": True, "detail": "sent to chat 13, msg 77"}
    assert failed == {"ok": False, "detail": "ValueError: no chat 99"}
    assert refused == {"ok": False, "detail": "refused: calls are off on this profile"}
    assert reacted == {"ok": True, "detail": "reacted +1 to msg 5"}


# >>> voice-message (PHONE-118): "voice_message": true on a file job sends view
# type Voice; a plain file job's call is unchanged; the bot checks the audio.
OGG = b"OggS\x00\x02" + b"\x00" * 26
M4A = b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 20


def test_a_voice_job_passes_view_type_voice_and_a_plain_file_none(outbox, tmp_path):
    ogg = tmp_path / "note.ogg"
    ogg.write_bytes(OGG)
    voice, plain = outbox(
        {"chat_id": 13, "file": str(ogg), "voice_message": True},
        {"chat_id": 13, "file": str(ogg)},
    )
    assert voice == {"ok": True, "detail": "sent voice message note.ogg to chat 13, msg 78"}
    assert plain == {"ok": True, "detail": "sent note.ogg to chat 13, msg 78"}
    sent_voice, sent_plain = outbox.sent
    assert sent_voice["viewtype"] == "Voice"
    assert sent_voice == {"chat": 13, "text": None, "html": None, "file": str(ogg),
                          "filename": "note.ogg", "viewtype": "Voice"}
    # the plain call is exactly today's: no viewtype key at all
    assert sent_plain == {"chat": 13, "text": None, "html": None, "file": str(ogg),
                          "filename": "note.ogg"}


def test_voice_and_file_log_distinct_lines(outbox, tmp_path):
    m4a = tmp_path / "hello.m4a"
    m4a.write_bytes(M4A)
    outbox(
        {"chat_id": 13, "file": str(m4a), "voice_message": True, "text": "caption"},
        {"chat_id": 13, "file": str(m4a), "voice_message": False},
    )
    assert f"SENT VOICE to chat 13: hello.m4a ({len(M4A)} bytes) msg 78" in outbox.logged
    assert f"SENT FILE to chat 13: hello.m4a ({len(M4A)} bytes) msg 78" in outbox.logged
    assert outbox.sent[0]["text"] == "caption"
    assert "viewtype" not in outbox.sent[1]


def test_a_voice_job_to_an_unlisted_chat_sends_nothing(outbox, tmp_path):
    ogg = tmp_path / "note.ogg"
    ogg.write_bytes(OGG)
    ack, = outbox({"chat_id": 14, "file": str(ogg), "voice_message": True})
    assert ack["ok"] is False
    assert "chat 14" in ack["detail"]
    assert outbox.sent == []
    assert any(line.startswith("REFUSED send to chat 14") and "(send-voice)" in line
               for line in outbox.logged)


@pytest.mark.parametrize("name,data", [
    ("notes.txt", b"hello\n"),                      # not an audio suffix
    ("empty.ogg", b""),                             # a failed TTS run
    ("text.ogg", b"TTS failed: no voice\n"),        # text under an audio suffix
])
def test_a_voice_job_with_a_non_audio_file_sends_nothing(outbox, tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    ack, = outbox({"chat_id": 13, "file": str(p), "voice_message": True})
    assert ack["ok"] is False
    assert str(p) in ack["detail"]
    assert ack["detail"].endswith("nothing was sent.")
    assert outbox.sent == []
    assert any(line.startswith(f"REFUSED voice message {p}: ") for line in outbox.logged)


def test_the_same_non_audio_file_sent_plain_still_goes(outbox, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"hello\n")
    ack, = outbox({"chat_id": 13, "file": str(p)})
    assert ack == {"ok": True, "detail": "sent notes.txt to chat 13, msg 78"}


@pytest.mark.parametrize("value", [1, 0, "true", "false", [], {}])
def test_voice_message_must_be_a_json_bool(outbox, tmp_path, value):
    # R4: 1 == True and 0 == False in Python; neither is accepted as one
    ogg = tmp_path / "note.ogg"
    ogg.write_bytes(OGG)
    ack, = outbox({"chat_id": 13, "file": str(ogg), "voice_message": value})
    assert ack["ok"] is False
    assert '"voice_message" must be true or false' in ack["detail"]
    assert str(ogg) in ack["detail"]
    assert outbox.sent == []
    assert any(line.startswith(f"REFUSED voice message {ogg}: ") for line in outbox.logged)


def test_voice_message_null_is_a_plain_file(outbox, tmp_path):
    ogg = tmp_path / "note.ogg"
    ogg.write_bytes(OGG)
    ack, = outbox({"chat_id": 13, "file": str(ogg), "voice_message": None})
    assert ack == {"ok": True, "detail": "sent note.ogg to chat 13, msg 78"}
    assert "viewtype" not in outbox.sent[0]


def test_a_missing_voice_file_is_answered_not_sent(outbox, tmp_path):
    gone = tmp_path / "gone.ogg"
    ack, = outbox({"chat_id": 13, "file": str(gone), "voice_message": True})
    assert ack["ok"] is False
    assert str(gone) in ack["detail"]
    assert outbox.sent == []
# <<< voice-message

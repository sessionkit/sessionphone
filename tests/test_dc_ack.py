"""The dc tools read the bot's ack: "taken" is not "sent".

The bot logged SEND FAILED and unlinked the job, and the tool had already
reported it. Now the bot answers every job in the ack file the job names, ok
or not, BEFORE it unlinks the job; the tools report that answer, exit 3 on a
failure, and say NOT CONFIRMED - never "sent" - when a bot gives no answer.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sessionphone import jobs

BIN = Path(__file__).resolve().parent.parent / "bin"


def allow(root, text="twelve  chat:12\nthirteen  chat:13\nninety-nine  chat:99\n"):
    """The allow-list the tools' pre-check reads (PHONE-102): with none, every send is refused."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / "channels.txt"
    p.write_text(text)
    p.chmod(0o600)
    return p


def run(tool, root, *args, wait="5", listed=True):
    if listed and not (root / "channels.txt").exists():
        allow(root)
    env = dict(os.environ, DC_ROOT=str(root), DC_TAKEN_WAIT=wait)
    for k in ("DC_OUTBOX", "DC_OUTBOX_PUBLIC", "DC_ACKS", "DC_LIVE", "DC_LOG",
              "DC_CHANNELS_FILE", "PIP_CHAT_CHANNELS_FILE"):
        env.pop(k, None)
    return subprocess.run([str(BIN / tool), *args], env=env, capture_output=True,
                          text=True, timeout=30, check=False)


class Bot:
    """A bot that answers each job with answer(job) -> (ok, detail), or
    writes no ack at all when answer is None (a bot older than acks). It
    stamps the job keys it reads, as a running bot does since PHONE-129
    (sessionphone.jobs), unless stamp=False: then the tools judge it a bot
    older than PHONE-129 and refuse the keys it would misread."""

    def __init__(self, root, answer, stamp=True):
        self.out = root / "outbox"
        self.out.mkdir(parents=True, exist_ok=True)
        if stamp:
            jobs.write_stamp(self.out)      # this test's pid: alive
        self.answer, self.jobs, self.stop = answer, [], threading.Event()
        self.t = threading.Thread(target=self.loop)

    def loop(self):
        while not self.stop.is_set():
            for f in sorted(self.out.glob("*.json")):
                job = json.loads(f.read_text())
                self.jobs.append(job)
                if self.answer:
                    ok, detail = self.answer(job)
                    Path(job["ack"]).write_text(json.dumps({"ok": ok, "detail": detail}))
                f.unlink()          # after the ack, as the real bot does
            time.sleep(0.1)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()


def test_a_sent_line_reports_what_the_bot_did(tmp_path):
    with Bot(tmp_path, lambda job: (True, f"sent to chat {job['chat_id']}, msg 77")) as bot:
        r = run("dc-say", tmp_path, "13", "hello")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-say: sent to chat 13, msg 77"
    ack = Path(bot.jobs[0]["ack"])
    assert ack.parent == tmp_path / "acks" and ack.exists()     # kept: the audit trail


def test_a_failed_send_exits_3_and_says_why(tmp_path):
    with Bot(tmp_path, lambda job: (False, "ValueError: no chat 99")):
        r = run("dc-say", tmp_path, "99", "hello")
        d = run("dc-dm", tmp_path, "Sam", "hello")              # dc_job's path
    for res, tool in ((r, "dc-say"), (d, "dc-dm")):
        assert res.returncode == 3
        assert f"{tool}: FAILED - ValueError: no chat 99" in res.stderr
        assert res.stdout == ""


# ---- no ack: exit 4 = NOT CONFIRMED for a gated job (PHONE-102 do-007) ----
# Super ruling: "a distinct code lets a script tell refused (3) from
# unknown (4)". This test was test_no_ack_is_not_confirmed_never_sent, which
# pinned exit 0 for dc-say and dc-react: both are gated, so they now exit 4.


def test_a_gated_job_with_no_ack_exits_4_not_confirmed(tmp_path):
    with Bot(tmp_path, None):
        r = run("dc-say", tmp_path, "13", "hello")
        j = run("dc-react", tmp_path, "5", "+1")
    for res, tool in ((r, "dc-say"), (j, "dc-react")):
        assert res.returncode == 4, (tool, res.stderr)
        assert res.stdout == ""                                 # nothing claims it went
        assert (f"{tool}: NOT CONFIRMED (no ack): the bot took it but never confirmed; "
                f"nothing may have been sent. Its log: {tmp_path / 'bot.log'}") in res.stderr


def test_refused_stays_3_and_ok_stays_0_beside_4(tmp_path):
    """The three answers side by side, one tool: ok 0, refused 3, no ack 4."""
    with Bot(tmp_path, lambda job: (True, "sent")):
        ok = run("dc-say", tmp_path, "13", "hello")
    with Bot(tmp_path, lambda job: (False, "REFUSED: chat 13 is not on the allow-list")):
        no = run("dc-say", tmp_path, "13", "hello")
    with Bot(tmp_path, None):
        mute = run("dc-say", tmp_path, "13", "hello")
    assert (ok.returncode, no.returncode, mute.returncode) == (0, 3, 4)
    assert "FAILED - REFUSED" in no.stderr and "NOT CONFIRMED" not in no.stderr
    assert "NOT CONFIRMED (no ack)" in mute.stderr and "FAILED" not in mute.stderr


def test_every_gated_tool_exits_4_with_no_ack(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    gated = (("dc-dm", "Sam", "hi"), ("dc-react", "5", "+1"), ("dc-send-file", "13", str(f)),
             ("dc-xdc-update", "5", "{}"), ("dc-call", "13"), ("dc-ring", "Sam"),
             ("dc-invite", "13", "alice@example.org"), ("dc-say", "13", "hi"),
             ("dc-hangup", "thirteen"), ("dc-answer", "twelve"))    # by NAME: gated
    with Bot(tmp_path, None) as bot:
        for tool, *args in gated:
            r = run(tool, tmp_path, *args)
            assert r.returncode == 4, (tool, r.returncode, r.stderr)
            assert f"{tool}: NOT CONFIRMED (no ack)" in r.stderr, (tool, r.stderr)
            assert "sent to" not in r.stdout and "done" not in r.stdout, (tool, r.stdout)
    assert len(bot.jobs) == len(gated)                          # each one was taken


def test_a_gated_job_with_no_ack_exits_4_under_wait_zero_too(tmp_path):
    """DC_TAKEN_WAIT=0 never applies to a gated job (do-003), so neither does exit 0."""
    with Bot(tmp_path, None):
        r = run("dc-say", tmp_path, "13", "hello", wait="0")
    assert r.returncode == 4 and "NOT CONFIRMED (no ack)" in r.stderr


def test_hangup_and_answer_by_number_keep_todays_no_ack_answer(tmp_path):
    """Ungated (design row 9): ending a call by its number. No ack keeps the old
    answer: dc-hangup exits 0 with NOT CONFIRMED on stdout and the old warning;
    dc-answer keeps its own 4 'NO ANSWER' (test_dc_answer_reports_the_bots_yes_or_no)."""
    with Bot(tmp_path, None):
        h = run("dc-hangup", tmp_path, "13")
        a = run("dc-answer", tmp_path, "12")
    assert h.returncode == 0, h.stderr
    assert h.stdout.strip() == "dc-hangup: taken by the bot, NOT CONFIRMED (no ack)"
    assert "wrote no ack, so whether it went is unknown" in h.stderr
    assert "NOT CONFIRMED (no ack): the bot took it" not in h.stderr      # not the gated wording
    assert a.returncode == 4 and "dc-answer: NO ANSWER from the bot" in a.stderr
    assert "NOT CONFIRMED (no ack): the bot took it" not in a.stderr


def test_hangup_by_number_with_wait_zero_is_still_not_waited_for(tmp_path):
    with Bot(tmp_path, None):
        r = run("dc-hangup", tmp_path, "13", wait="0")
    assert r.returncode == 0 and r.stdout.strip() == "dc-hangup: queued (not waited for)"


def test_a_dash_name_is_refused_not_a_usage_error(tmp_path):
    """dc_precheck ends the pre-check's options with '--' (do-003 review advisory 1):
    a CHAT like '-x' reaches the list and is refused (3), never argparse's exit 2."""
    for tool, *args in (("dc-say", "-x", "hi"), ("dc-hangup", "-x"), ("dc-call", "-x")):
        r = run(tool, tmp_path, *args)
        assert r.returncode == 3, (tool, r.returncode, r.stderr)
        assert f"{tool}: REFUSED: no channel '-x' in {tmp_path / 'channels.txt'}" in r.stderr
        assert not list((tmp_path / "outbox").glob("*.json"))


def test_every_dc_job_tool_names_an_ack(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    with Bot(tmp_path, lambda job: (True, "done")) as bot:
        for tool, *args in (("dc-dm", "Sam", "hi"), ("dc-react", "5", "+1"),
                            ("dc-send-file", "13", str(f)), ("dc-xdc-update", "5", "{}"),
                            ("dc-call", "13"), ("dc-hangup", "13"), ("dc-ring", "Sam"),
                            ("dc-invite", "13", "alice@example.org"), ("dc-say", "13", "hi")):
            r = run(tool, tmp_path, *args)
            assert r.returncode == 0 and r.stdout.startswith(f"{tool}: done"), (tool, r.stderr)
    assert len({j["ack"] for j in bot.jobs}) == 9
    assert not list((tmp_path / "outbox").glob("*.part"))


def test_a_call_line_the_bot_refuses_is_not_sent(tmp_path):
    (tmp_path / "call-live.json").write_text(json.dumps({"call_id": 5531}))
    with Bot(tmp_path, lambda job: (False, "call 5531 is over; the live call is msg 5600")):
        r = run("dc-say", tmp_path, "--call", "5531", "12", "hello")
    assert r.returncode == 3
    assert "dc-say: NOT SENT - call 5531 is over" in r.stderr


def test_dc_answer_reports_the_bots_yes_or_no(tmp_path):
    """dc-answer (ring-first) rides dc_job like every tool: it must not wait on
    an ack of its own, because dc_job names the ack the bot writes (PHONE-26)."""
    with Bot(tmp_path, lambda job: (True, f"picked up msg 9 in chat {job['answer']}")) as bot:
        ok = run("dc-answer", tmp_path, "12")
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == "dc-answer: picked up msg 9 in chat 12"
    assert bot.jobs[0]["answer"] == 12 and bot.jobs[0]["ack"]
    with Bot(tmp_path, lambda job: (False, "nothing ringing in chat 12")):
        no = run("dc-answer", tmp_path, "12")
    assert no.returncode == 3 and "nothing ringing in chat 12" in no.stderr
    assert no.stdout == ""
    with Bot(tmp_path, None):
        mute = run("dc-answer", tmp_path, "12")
    assert mute.returncode == 4 and "NO ANSWER from the bot" in mute.stderr
    assert "picked up" not in mute.stdout


# >>> voice-message (PHONE-118, design.md decisions 1-4, review R6):
# dc-send-file --voice-message checks the audio before it queues anything, adds
# "voice_message": true to the file job, keeps the gate's exits (0, 3, 4), and
# warns when a bot older than voice messages sent a plain file instead.
OGG = b"OggS\x00\x02" + b"\x00" * 26


def voice_file(tmp_path, name="note.ogg", data=OGG):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def new_bot(job):
    """The PHONE-118 bot's answer: a voice job acks 'sent voice message ...'."""
    name = Path(job["file"]).name
    if job.get("voice_message") is True:
        return True, f"sent voice message {name} to chat {job['chat_id']}, msg 78"
    return True, f"sent {name} to chat {job['chat_id']}, msg 78"


def old_bot(job):
    """A bot from before PHONE-118: it ignores the key and sends a file. Such a
    bot writes no stamp, and since PHONE-129 the tool refuses `voice_message`
    to an unstamped bot before queueing (test_an_unstamped_bot_...). The tests
    below run it stamped, so the ack check, the second line, stays pinned."""
    return True, f"sent {Path(job['file']).name} to chat {job['chat_id']}, msg 78"


def test_voice_message_queues_the_key_and_reports_the_voice_ack(tmp_path):
    ogg = voice_file(tmp_path)
    with Bot(tmp_path, new_bot) as bot:
        r = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg), "hello", "there")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-send-file: sent voice message note.ogg to chat 13, msg 78"
    assert r.stderr == ""
    job, = bot.jobs
    assert job["voice_message"] is True
    assert (job["chat_id"], job["file"], job["text"]) == (13, str(ogg), "hello there")


def test_voice_message_by_channel_name_and_after_other_flags(tmp_path):
    ogg = voice_file(tmp_path)
    html = tmp_path / "full.html"
    html.write_text("<b>hi</b>")
    with Bot(tmp_path, lambda job: (True, "sent voice message note.ogg to chat 13, msg 78")) as bot:
        r = run("dc-send-file", tmp_path, "--html", str(html), "--voice-message", "thirteen", str(ogg))
    assert r.returncode == 0, r.stderr
    job, = bot.jobs
    assert job["chat"] == "thirteen" and job["voice_message"] is True and job["html"] == "<b>hi</b>"


def test_without_the_flag_the_job_is_todays(tmp_path):
    """No flag: no voice_message key at all, and the job's keys are exactly today's."""
    ogg = voice_file(tmp_path)
    with Bot(tmp_path, new_bot) as bot:
        r = run("dc-send-file", tmp_path, "13", str(ogg), "hi")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-send-file: sent note.ogg to chat 13, msg 78"
    job, = bot.jobs
    assert set(job) == {"chat_id", "file", "text", "ack"}


def test_voice_message_with_a_missing_file_exits_1_and_queues_nothing(tmp_path):
    with Bot(tmp_path, new_bot) as bot:
        r = run("dc-send-file", tmp_path, "--voice-message", "13", str(tmp_path / "gone.ogg"))
    assert r.returncode == 1
    assert r.stderr.startswith(f"dc-send-file: not sent as a voice message: {tmp_path / 'gone.ogg'}: no such file")
    assert ".aac .flac .m4a .mp3 .oga .ogg .wav" in r.stderr     # the slice: names the suffixes too
    assert bot.jobs == [] and not list((tmp_path / "outbox").glob("*.json"))


def test_without_the_flag_a_missing_file_keeps_todays_line(tmp_path):
    with Bot(tmp_path, new_bot) as bot:
        r = run("dc-send-file", tmp_path, "13", str(tmp_path / "gone.ogg"))
    assert r.returncode == 1
    assert r.stderr == f"dc-send-file: no such file: {tmp_path / 'gone.ogg'}\n"
    assert bot.jobs == []


@pytest.mark.parametrize("name,data", [
    ("notes.txt", b"hello\n"),                     # not an audio suffix
    ("empty.ogg", b""),                            # a failed TTS run
    ("text.ogg", b"TTS failed: no voice\n"),       # text under an audio suffix
])
def test_voice_message_with_a_non_audio_file_exits_1_naming_it(tmp_path, name, data):
    p = voice_file(tmp_path, name, data)
    with Bot(tmp_path, new_bot) as bot:
        r = run("dc-send-file", tmp_path, "--voice-message", "13", str(p))
    assert r.returncode == 1, r.stderr
    assert r.stderr.startswith(f"dc-send-file: not sent as a voice message: {p}: ")
    assert ".aac .flac .m4a .mp3 .oga .ogg .wav" in r.stderr     # the accepted suffixes
    assert r.stdout == ""
    assert bot.jobs == [] and not list((tmp_path / "outbox").glob("*.json"))


def test_the_same_non_audio_file_without_the_flag_still_goes(tmp_path):
    p = voice_file(tmp_path, "notes.txt", b"hello\n")
    with Bot(tmp_path, new_bot):
        r = run("dc-send-file", tmp_path, "13", str(p))
    assert r.returncode == 0, r.stderr


def test_voice_message_exits_0_3_4(tmp_path):
    """The gate's three answers: sent 0, refused 3, no ack 4 (even with a wait of 0)."""
    ogg = voice_file(tmp_path)
    with Bot(tmp_path, new_bot):
        ok = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg))
    with Bot(tmp_path, lambda job: (False, f"REFUSED: voice message {job['file']}: no; nothing was sent.")):
        no = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg))
    with Bot(tmp_path, None):
        mute = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg))
        mute0 = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg), wait="0")
    assert (ok.returncode, no.returncode, mute.returncode, mute0.returncode) == (0, 3, 4, 4)
    assert "FAILED - REFUSED: voice message" in no.stderr
    for m in (mute, mute0):
        assert "dc-send-file: NOT CONFIRMED (no ack)" in m.stderr
        assert "plain FILE" not in m.stderr


def test_voice_message_to_an_unlisted_chat_is_refused_3_before_any_job(tmp_path):
    ogg = voice_file(tmp_path)
    with Bot(tmp_path, new_bot) as bot:
        r = run("dc-send-file", tmp_path, "--voice-message", "42", str(ogg))
    assert r.returncode == 3, r.stderr
    assert bot.jobs == []


def test_an_old_bot_sending_a_plain_file_warns_but_exits_0(tmp_path):
    ogg = voice_file(tmp_path)
    with Bot(tmp_path, old_bot):
        r = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg))
    assert r.returncode == 0, r.stderr                 # the file went: a resend sends it twice
    assert r.stdout.strip() == "dc-send-file: sent note.ogg to chat 13, msg 78"
    assert "dc-send-file: the bot sent note.ogg as a plain FILE, not a voice message" in r.stderr
    assert "Do not resend" in r.stderr


def test_an_old_bot_and_a_file_named_voice_message_still_warns(tmp_path):
    """R6: the old bot's ack 'sent voice message x.ogg to chat 13' for a file named
    'voice message x.ogg' does not pass for the new ack: the opening is matched exactly."""
    ogg = voice_file(tmp_path, "voice message x.ogg")
    with Bot(tmp_path, old_bot):
        r = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-send-file: sent voice message x.ogg to chat 13, msg 78"
    assert "as a plain FILE, not a voice message" in r.stderr
    with Bot(tmp_path, new_bot):
        n = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg))
    assert n.returncode == 0 and n.stderr == ""


def test_an_unstamped_bot_is_refused_a_voice_message_before_any_job(tmp_path):
    """PHONE-129 (c): no stamp means a bot older than PHONE-129, judged on the
    pre-PHONE-102 keys, which have no voice_message. It would send a plain
    file and ack ok, so the tool refuses first: exit 3, restart the bot."""
    ogg = voice_file(tmp_path)
    with Bot(tmp_path, old_bot, stamp=False) as bot:
        r = run("dc-send-file", tmp_path, "--voice-message", "13", str(ogg))
        plain = run("dc-send-file", tmp_path, "13", str(ogg))
    assert r.returncode == 3, r.stderr
    assert "does not read job key(s) 'voice_message'" in r.stderr and "restart the bot" in r.stderr
    assert plain.returncode == 0, plain.stderr                    # today's job still goes
    assert [set(j) for j in bot.jobs] == [{"chat_id", "file", "text", "ack"}]


def test_the_help_names_the_flag(tmp_path):
    r = run("dc-send-file", tmp_path, "--help")
    assert r.returncode == 0 and "--voice-message" in r.stdout
    u = run("dc-send-file", tmp_path, "--voice-message", "13")
    assert u.returncode == 1 and "[--voice-message]" in u.stderr
# <<< voice-message

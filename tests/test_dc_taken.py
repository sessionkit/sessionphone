"""dc-say / dc_job: "queued" means a bot took the job.

Two ETA messages to a guest sat in an outbox no bot read while the tool said
"queued". Every bot unlinks a job once handled, so the tools now wait for that
(DC_TAKEN_WAIT seconds) and exit 3, loudly, when nothing takes it.
"""
import os
import subprocess
import threading
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def run(tool, root, *args, wait="1"):
    lst = root / "channels.txt"      # PHONE-102: with no allow-list the pre-check refuses
    if not lst.exists():
        lst.write_text("thirteen  chat:13\n")
        lst.chmod(0o600)
    env = dict(os.environ, DC_ROOT=str(root), DC_TAKEN_WAIT=wait)
    for k in ("DC_OUTBOX", "DC_OUTBOX_PUBLIC", "DC_CHANNELS_FILE", "PIP_CHAT_CHANNELS_FILE"):
        env.pop(k, None)
    return subprocess.run([str(BIN / tool), *args], env=env, capture_output=True, text=True, timeout=30, check=False)


def test_no_bot_is_refused_loudly_and_the_job_is_kept(tmp_path):
    r = run("dc-say", tmp_path, "13", "hello")
    assert r.returncode == 3
    assert "NOT TAKEN" in r.stderr and "queued" not in r.stdout
    assert len(list((tmp_path / "outbox").glob("*.json"))) == 1


def test_a_bot_that_takes_the_job_is_reported_as_taken(tmp_path):
    out = tmp_path / "outbox"; out.mkdir()
    stop = threading.Event()

    def bot():                      # a bot: poll, handle, unlink
        while not stop.is_set():
            for f in out.glob("*.json"):
                f.unlink()
            time.sleep(0.2)
    t = threading.Thread(target=bot); t.start()
    try:
        r = run("dc-say", tmp_path, "13", "hello", wait="5")
        j = run("dc-react", tmp_path, "123", "+1", wait="5")   # dc_job's path
    finally:
        stop.set(); t.join()
    # This bot writes no ack. A send is gated, so taken-with-no-ack is exit 4,
    # NOT CONFIRMED (PHONE-102 do-007, super ruling); it was exit 0 before. The
    # job WAS taken: the wording says so, and it is not NOT TAKEN (3).
    for res, tool in ((r, "dc-say"), (j, "dc-react")):
        assert res.returncode == 4, (tool, res.stderr)
        assert "the bot took it but never confirmed" in res.stderr and "NOT TAKEN" not in res.stderr


def test_wait_zero_does_not_wait_and_does_not_claim_taken(tmp_path):
    """Only a job the bot does not gate: ending a call by its number (design row 9)."""
    r = run("dc-hangup", tmp_path, "13", wait="0")
    assert r.returncode == 0 and "not waited for" in r.stdout
    assert len(list((tmp_path / "outbox").glob("*.json"))) == 1


def test_wait_zero_never_applies_to_a_send(tmp_path):
    """PHONE-102 rule 3: the bot may refuse a send, so a send is always waited
    for. With no bot it is NOT TAKEN (exit 3), never "queued", whatever DC_TAKEN_WAIT says."""
    t0 = time.monotonic()
    r = run("dc-say", tmp_path, "13", "hello", wait="0")
    assert r.returncode == 3 and "NOT TAKEN" in r.stderr and "not waited for" not in r.stdout
    assert time.monotonic() - t0 >= 9      # the default 10 s wait, not 0

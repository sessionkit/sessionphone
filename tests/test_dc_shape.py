"""A dc-* tool never queues a job the running bot would misread (PHONE-129 (c)).

The field case, Sun 2030-06-09: the public bot process predated
PHONE-102, the tools on disk did not, and `dc-say --profile public sessionkit
...` queued `{"chat": "sessionkit"}`; the bot crashed with KeyError 'chat_id'.
Now bin/dc-env's dc_shape checks the job against <OUTBOX>/.bot (the keys the
bot stamped at start; none = the pre-PHONE-102 keys) before it is written:
exit 3, "restart the bot", and no job file.

Fakes only: the real tools, a fake bot thread, scratch DC_ROOT. Never a Delta send.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_dc_ack import OGG
from test_dc_channels import Bot, allow, run

from sessionphone import jobs


def queued(root):
    """Every job file, whole or partial, in either outbox (the stamp is not one)."""
    return [p for d in (root / "outbox", root / "public" / "outbox") if d.exists()
            for p in d.iterdir() if p.suffix in (".json", ".part")]


def stamp(out: Path, keys, pid=None):
    out.mkdir(parents=True, exist_ok=True)
    jobs.stamp_path(out).write_text(json.dumps(
        {"pid": os.getpid() if pid is None else pid, "started": "2030-12-08T23:34:56+1100",
         "keys": sorted(keys)}))


class OldBot(Bot):
    """A bot older than PHONE-129: answers like Bot, but writes no stamp."""

    def __init__(self, out, answer):
        super().__init__(out, answer)
        jobs.stamp_path(out).unlink()


def test_dc_say_name_to_an_unstamped_bot_exits_3_and_queues_nothing(tmp_path):
    """The field case: a NAME job, no stamp. Refused before anything is written."""
    allow(tmp_path)
    r = run("dc-say", tmp_path, "owner", "hello", wait="5")
    assert r.returncode == 3, r.stderr
    assert "dc-say: REFUSED: no bot has stamped" in r.stderr
    assert "does not read job key(s) 'chat'" in r.stderr and "restart the bot" in r.stderr
    assert queued(tmp_path) == []                            # no job file, no .part


def test_dc_say_public_name_to_a_pre_102_public_bot(tmp_path):
    """The field case's command itself: --profile public reads the PUBLIC outbox's stamp."""
    allow(tmp_path)
    allow(tmp_path / "public", "sessionkit  chat:20\n")
    stamp(tmp_path / "outbox", jobs.KEYS)                    # the default bot is current
    r = run("dc-say", tmp_path, "--profile", "public", "sessionkit", "hi", wait="5")
    assert r.returncode == 3 and "restart the bot" in r.stderr, r.stderr
    assert str(tmp_path / "public" / "outbox" / ".bot") in r.stderr
    assert queued(tmp_path) == []


def test_dc_say_number_to_an_unstamped_bot_still_goes(tmp_path):
    """LEGACY_KEYS: a job the old bot reads as meant is not refused."""
    allow(tmp_path)
    with OldBot(tmp_path / "outbox", lambda job: (True, f"sent to chat {job['chat_id']}, msg 7")) as bot:
        r = run("dc-say", tmp_path, "42", "hello", wait="5")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dc-say: sent to chat 42, msg 7"
    assert bot.jobs[0]["chat_id"] == 42 and "chat" not in bot.jobs[0]


def test_a_live_current_stamp_lets_a_name_through(tmp_path):
    allow(tmp_path)
    with Bot(tmp_path / "outbox", lambda job: (True, "sent to chat 42, msg 7")) as bot:   # stamps KEYS
        r = run("dc-say", tmp_path, "family", "hello", wait="5")
    assert r.returncode == 0, r.stderr
    assert bot.jobs[0]["chat"] == "family"


def test_a_dead_stamp_is_judged_as_no_stamp(tmp_path):
    allow(tmp_path)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    stamp(tmp_path / "outbox", jobs.KEYS, pid=p.pid)
    r = run("dc-say", tmp_path, "family", "hello", wait="5")
    assert r.returncode == 3, r.stderr
    assert f"(pid {p.pid}) is not running" in r.stderr and "restart the bot" in r.stderr
    assert queued(tmp_path) == []


def test_voice_message_against_a_stamp_without_it_exits_3(tmp_path):
    """The silent skew: a pre-PHONE-118 bot would send a plain file and ack ok."""
    allow(tmp_path)
    ogg = tmp_path / "note.ogg"
    ogg.write_bytes(OGG)
    stamp(tmp_path / "outbox", jobs.KEYS - {"voice_message"})
    r = run("dc-send-file", tmp_path, "--voice-message", "42", str(ogg), wait="5")
    assert r.returncode == 3, r.stderr
    assert "dc-send-file: REFUSED: the running bot" in r.stderr
    assert "does not read job key(s) 'voice_message'" in r.stderr and "restart the bot" in r.stderr
    assert queued(tmp_path) == []


@pytest.mark.parametrize("tool,args", [
    ("dc-hangup", ("family",)), ("dc-answer", ("family",)), ("dc-call", ("family",)),
    ("dc-invite", ("family", "alice@example.org")), ("dc-xdc-update", ("5", "{}")),
])
def test_every_dc_job_tool_is_checked(tmp_path, tool, args):
    """dc_job runs dc_shape before it writes: a NAME job to an unstamped bot is
    refused; a job of legacy keys (dc-xdc-update by message id) goes."""
    allow(tmp_path)
    with OldBot(tmp_path / "outbox", lambda job: (True, "done")) as bot:
        r = run(tool, tmp_path, *args, wait="5")
    if tool == "dc-xdc-update":
        assert r.returncode == 0, r.stderr
        assert len(bot.jobs) == 1
        return
    assert r.returncode == 3, (tool, r.stderr)
    assert f"{tool}: REFUSED: no bot has stamped" in r.stderr and "restart the bot" in r.stderr
    assert bot.jobs == [] and queued(tmp_path) == []


def test_a_check_that_cannot_run_exits_2_and_queues_nothing(tmp_path):
    """dc_shape exit 2: the check itself failed (here: python cannot import the module)."""
    allow(tmp_path)
    real = Path(__file__).resolve().parent.parent / "bin"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "dc-say").write_text((real / "dc-say").read_text())     # a copy: it sources ITS dc-env
    (fake / "dc-say").chmod(0o755)
    env_src = (real / "dc-env").read_text()
    home = '_DC_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"'
    assert home in env_src and "from sessionphone.jobs import main" in env_src
    env_src = env_src.replace(home, f'_DC_HOME="{real.parent}"')   # the pre-check still runs
    (fake / "dc-env").write_text(env_src.replace("from sessionphone.jobs import main",
                                                 "from sessionphone.nosuchjobs import main"))
    with Bot(tmp_path / "outbox", lambda job: (True, "sent")) as bot:
        r = run("dc-say", tmp_path, "42", "hello", wait="5", bin_dir=fake)
    assert r.returncode == 2, r.stderr
    assert "dc-say: the job-shape check could not run" in r.stderr and "nothing was queued" in r.stderr
    assert bot.jobs == []


def test_readme_and_changelog_name_the_skew_refusal(tmp_path):
    """README quotes the refusal the code writes (its two ends), and both say restart the bot."""
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    notice = (root / "CHANGELOG.md").read_text().split("## [0.1.0]")[0]
    quoted = next(line for line in readme.splitlines()
                  if line.startswith("dc-say: REFUSED: the running bot"))
    head, _, tail = quoted.partition(" ... ")
    stamp(tmp_path, jobs.KEYS - {"chat"})
    why = "dc-say: " + jobs.check(tmp_path, {"chat": "x"})
    assert why.startswith(head.replace("pid N, started T", f"pid {os.getpid()}, started 2030-12-08T23:34:56+1100"))
    assert why.endswith(tail)
    for text in (" ".join(readme.split()), " ".join(notice.split())):
        assert "restart the bot" in text and "<outbox>/.bot" in text
    assert "PHONE-129" in notice

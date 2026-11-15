"""PHONE-307: the footer's dc-say is THIS install's, whatever the reader's PATH holds.

A bridged mail's footer told the agent `dc-say NAME 'your reply'`, the word
bare, so the READER's shell ran the first dc-say on the READER's PATH. On mba
that was another tool's (~/.local/bin/dc-say -> kin/bin), which takes only a
numeric chat id and refused the channel name with ValueError: the reply was
never sent. The bot cannot see the reader's PATH (under launchd its own is
service.BASE_PATH), so the footer now names this install's dc-say by its
absolute, shell-quoted path, always (dc_env.own_say, found by PHONE-302's
dctools.homes()); DC_SAY, set, still wins verbatim.

The end-to-end check runs every footer form through bash with a foreign
dc-say FIRST on PATH: the foreign one must never run. This install's dc-say
answers instead and refuses (no channels file in a tmp root, exit 3), so
nothing is sent anywhere.
"""
import os
import shlex
import subprocess
import sys

import pytest

from sessionphone import dctools
from sessionphone.bridge import dc_env

FOREIGN = """#!/bin/sh
echo "$0 $*" > "$FOREIGN_MARK"
echo "ValueError: invalid literal for int() with base 10: '$1'" >&2
exit 1
"""


def _tool(d, body="#!/bin/sh\nexit 0\n"):
    d.mkdir(parents=True, exist_ok=True)
    f = d / "dc-say"
    f.write_text(body)
    f.chmod(0o755)
    return f


# ---- own_say: which dc-say, as one shell word ----------------------------------------

def test_own_say_is_the_first_home_that_has_one(tmp_path):
    first, second = tmp_path / "scripts", tmp_path / "dcbin"
    _tool(second)
    assert dc_env.own_say([first, second]) == str(second / "dc-say")     # first has none
    _tool(first)
    assert dc_env.own_say([first, second]) == str(first / "dc-say")      # the console script first


def test_own_say_quotes_a_path_a_shell_would_split(tmp_path):
    d = tmp_path / "Application Support" / "it's here"
    _tool(d)
    word = dc_env.own_say([d])
    assert shlex.split(word) == [str(d / "dc-say")]


def test_own_say_with_no_copy_of_its_own_is_the_bare_word(tmp_path):
    assert dc_env.own_say([tmp_path / "nothing", tmp_path / "here"]) == "dc-say"


# ---- the hint, as the bot builds it at import ----------------------------------------

PROBE = r"""
from sessionphone.bridge import dc_env
print(dc_env.say_cmd(""))
print(dc_env.reply_hint(12, profile="", channel="sessionkit"))
"""


def _probe(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DC_", "PIP_CHAT_")) and k != "SESSIONPHONE_ROOT"}
    env.update(DC_ROOT=str(tmp_path / "root"), DC_AGENT="ada", DC_HOST="host.example", **extra)
    return subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True,
                          text=True, check=True).stdout.splitlines()


def test_the_default_hint_names_this_install_dc_say_by_absolute_path(tmp_path):
    say, hint = _probe(tmp_path)
    word = shlex.split(say)
    assert len(word) == 1 and os.path.isabs(word[0]), say
    _, mine = dctools._ours("dc-say", dctools.homes())
    assert os.path.realpath(word[0]) in mine, (word[0], mine)
    assert hint == f"{say} sessionkit 'your reply'"


def test_dc_say_set_still_wins_verbatim(tmp_path):
    say, hint = _probe(tmp_path, DC_SAY="my-say --loud")
    assert say == "my-say --loud"
    assert hint == "my-say --loud sessionkit 'your reply'"


# ---- end to end: a foreign dc-say first on PATH never answers ------------------------

def _forms(outbox):
    """Every footer a bridged mail carries, as the line the agent pastes."""
    fallback = dc_env.reply_hint(12, call=5531, profile="").splitlines()[1]
    pub = dc_env.reply_hint(12, call=5531, profile="public").splitlines()[1]
    head = "If the mouth fails: "
    assert fallback.startswith(head) and pub.startswith(head)
    return {
        "channel name": dc_env.reply_hint(12, profile="", channel="sessionkit"),
        "chat id": dc_env.reply_hint(12, profile=""),
        "quote": dc_env.reply_hint(12, profile="", channel="sessionkit", quote=5531),
        "call fallback": fallback[len(head):],
        "profile outbox": dc_env.reply_hint(12, profile="public", channel="sessionkit"),
        "profile call fallback": pub[len(head):],
    }


FORMS = ["channel name", "chat id", "quote", "call fallback", "profile outbox",
         "profile call fallback"]


@pytest.mark.parametrize("form", FORMS)
def test_the_footer_runs_this_install_dc_say_not_the_first_on_path(tmp_path, monkeypatch, form):
    root, outbox = tmp_path / "root", tmp_path / "root" / "public" / "outbox"
    root.mkdir()
    monkeypatch.setenv("DC_OUTBOX", str(outbox))       # the profile forms name it
    line = _forms(outbox)[form]
    assert "DC_OUTBOX=" in line if form.startswith("profile") else "DC_OUTBOX=" not in line
    foreign, mark = _tool(tmp_path / "kin" / "bin", FOREIGN), tmp_path / "foreign-ran"
    env = {"HOME": str(tmp_path / "home"), "FOREIGN_MARK": str(mark),
           "PATH": f"{foreign.parent}{os.pathsep}/usr/bin{os.pathsep}/bin",
           "DC_ROOT": str(root), "DC_AGENT": "ada", "DC_HOST": "host.example"}
    r = subprocess.run(["bash", "-c", line], env=env, capture_output=True, text=True, timeout=60)
    assert not mark.exists(), f"the foreign dc-say ran: {mark.read_text()}"
    assert "ValueError" not in r.stderr, r.stderr
    # this install's dc-say answered: nothing is set up, so it refuses, exit 3
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert r.stderr.startswith("dc-say: "), r.stderr
    assert not (outbox.exists() and any(outbox.iterdir())), "nothing may be queued"

"""No test reaches the real sendmail: conftest's sendmail_tripwire (PHONE-129 fix-004).

On the merge of PHONE-129 onto PHONE-94, the group-job rig ran the bot with no
channels list, the list alert went out by bot.send_mail, and the real
/usr/sbin/sendmail took it. These pin the tripwire that now stands in its place.

Since PHONE-72 part 2b no src path runs sendmail (bot.send_mail is gone), but
`message send` is itself a sendmail client, so the tripwire stays: these launch
sendmail directly, by each name a rig could use.
"""
import os
import subprocess

from sessionphone.bridge import bot, dc_env


def test_dc_sendmail_is_the_tripwire_not_the_real_one(sendmail_tripwire):
    assert dc_env.env("SENDMAIL") not in (None, "", "/usr/sbin/sendmail")
    assert dc_env.env("SENDMAIL").endswith("/sendmail")


def test_a_send_that_reaches_it_is_refused_and_recorded(sendmail_tripwire):
    r = subprocess.run([os.environ["DC_SENDMAIL"], "-f", "a@example.invalid", "-t"],
                       input="To: nobody@example.invalid\nSubject: t\n\nbody\n", text=True, check=False)
    assert r.returncode == 75
    calls = sendmail_tripwire.read_text()
    assert "test_a_send_that_reaches_it_is_refused_and_recorded" in calls and " -t" in calls
    sendmail_tripwire.write_text("")       # this test meant to reach it: clear, so teardown passes


def test_a_child_process_inherits_it(sendmail_tripwire):
    """A tool run as a subprocess sees the same DC_SENDMAIL, so it trips too."""
    r = subprocess.run(["sh", "-c", 'echo x | "$DC_SENDMAIL" -t'], check=False)
    assert r.returncode == 75 and sendmail_tripwire.read_text().count("\n") == 1
    sendmail_tripwire.write_text("")


def _strip_dc(monkeypatch):
    """What six rigs do after the autouse fixture has run (test_dc_host, test_callers_on_install, ...)."""
    for k in [k for k in os.environ if k.startswith("DC_")]:
        monkeypatch.delenv(k)


def test_a_rig_that_strips_dc_still_cannot_reach_the_real_one(sendmail_tripwire, monkeypatch):
    """With DC_SENDMAIL unset, the real /usr/sbin/sendmail by path: the Popen guard refuses it."""
    _strip_dc(monkeypatch)
    assert dc_env.env("SENDMAIL") is None
    r = subprocess.run(["/usr/sbin/sendmail", "-f", "a@example.invalid", "-t"],
                       input="To: nobody@example.invalid\nSubject: t\n\nbody\n", text=True, check=False)
    assert r.returncode == 75
    calls = sendmail_tripwire.read_text()
    assert "test_a_rig_that_strips_dc_still_cannot_reach_the_real_one" in calls
    assert "| REAL /usr/sbin/sendmail -f " in calls and calls.count("\n") == 1
    sendmail_tripwire.write_text("")


def test_any_name_for_the_real_one_is_refused(sendmail_tripwire, tmp_path):
    """A bare `sendmail`, a shell line naming it, `executable=`, or a symlink to it: all refused."""
    link = tmp_path / "mta"
    link.symlink_to("/usr/sbin/sendmail")
    runs = [
        (["sendmail", "-t"], {}),
        ("echo x | /usr/sbin/sendmail -t", {"shell": True}),
        (["mta", "-t"], {"executable": "/usr/sbin/sendmail"}),
        ([str(link), "-t"], {}),
    ]
    for args, kw in runs:
        r = subprocess.run(args, input="x\n", text=True, capture_output=True, check=False, **kw)
        assert r.returncode == 75, (args, r)
    assert sendmail_tripwire.read_text().count("| REAL ") == len(runs)
    sendmail_tripwire.write_text("")


def test_a_stub_of_subprocess_run_still_wins(sendmail_tripwire, monkeypatch):
    """A rig that stubs bot.subprocess.run never launches anything, so nothing is recorded."""
    _strip_dc(monkeypatch)
    seen = []

    class Done:
        returncode, stderr = 0, ""

    monkeypatch.setattr(bot.subprocess, "run", lambda argv, **k: seen.append(argv) or Done())
    assert bot.subprocess.run(["/usr/sbin/sendmail", "-t"], input="x").returncode == 0
    assert seen[0][0] == "/usr/sbin/sendmail" and not sendmail_tripwire.exists()


def test_other_commands_run_as_before(sendmail_tripwire):
    r = subprocess.run(["sh", "-c", "echo hi"], capture_output=True, text=True, check=True)
    assert r.stdout == "hi\n" and not sendmail_tripwire.exists()

"""PHONE-248: the bot's box is a bare name, never name@host.

DC_MAILTO defaulted to <agent>@<host>, so the bot's box carried this host's
name: when the host was renamed, every mail to the old name was refused
(rc=3, "is not on this host"). Measured on both of sessionmail's carriers: a
bare To (`claude`) is delivered on this host, and sessionmail adds the host
itself, and it is still delivered after a rename; `claude@<old host>` is
refused after one.

So:

1. the default MAILTO is the bare agent name, on every path that builds it
   (dc_env.bind_agent, dc_env.take_mail_host, dc_env.mailto);
2. a DC_MAILTO that names a host is kept exactly as given;
3. a hostname change does not change the default;
4. a bare DC_MAILTO is a box on THIS host: seatmail.on_host says so, so the
   start's box check makes it (seat.have_box / ensure_box) instead of
   refusing it as off this host on the drop (PHONE-207), and a bare Cc is
   judged as a box here (seatmail.guard_cc).

Fakes only: tests/test_drop_box.py's fake `message`, and settings.mail_file
stubbed as tests/test_readers_say_their_source.py stubs it.
"""
import os

import pytest
import test_drop_box as dropbox  # its fake `message` (Drop) and HOST; nothing in it is changed

from sessionphone import seat, settings
from sessionphone.bridge import dc_env, seatmail

HOST = dropbox.HOST
FAR = "claude@elsewhere.example"
MAIL_ENV = ("SESSIONMAIL_HOST", "SESSION_MAIL_HOST", "DC_HOST", "DC_ADDR", "DC_MAILTO",
            "PIP_CHAT_HOST", "PIP_CHAT_MAILTO", "PIP_CHAT_AGENT")


@pytest.fixture
def plain(monkeypatch):
    """No host or mail setting in the environment; this machine is `laptop`."""
    for k in MAIL_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "laptop.local")
    return monkeypatch


@pytest.fixture
def bound(plain):
    """dc_env's bound values, put back after the test; the agent is ada."""
    for k in ("HOST", "ADDR", "MAILTO", "HOST_PROBLEM", "AGENT", "AGENT_FROM", "AGENT_NAME"):
        plain.setattr(dc_env, k, getattr(dc_env, k))
    plain.setattr(dc_env, "AGENT", "ada")       # take_mail_host uses the bound agent
    plain.setenv("DC_AGENT", "ada")             # bind_agent picks it afresh
    return plain


@pytest.fixture
def mail_json_host(plain):
    """sessionmail's mail.json answers `host` = HOST (settings.mail_file stubbed)."""
    def answer(host):
        out = {"sendmail": (None, "default"), "root": (None, "default"),
               "host": (host, "host"), "postfix.relay_domain": (None, "default")}
        plain.setattr(settings, "mail_file", lambda exe: out)
    return answer


@pytest.fixture
def drop(tmp_path, monkeypatch):
    d = dropbox.Drop(tmp_path, monkeypatch)
    yield d
    seatmail.forget_boxes()


# ---- 1. the default MAILTO is the bare agent name ------------------------------

def test_mailto_default_is_the_bare_agent_name(plain):
    assert dc_env.mailto({}) == "claude"
    assert dc_env.mailto({"DC_AGENT": "Ada"}) == "ada"


def test_mailto_default_is_bare_whatever_host_sessionmail_names(plain):
    assert dc_env.mailto({"SESSIONMAIL_HOST": "mac"}) == "claude"
    assert dc_env.mailto({"SESSION_MAIL_HOST": "mac2", "DC_AGENT": "ada"}) == "ada"


def test_bind_agent_binds_a_bare_mailto(bound):
    dc_env.bind_agent()
    assert dc_env.MAILTO == "ada"


def test_the_start_rebind_on_the_mail_json_host_keeps_mailto_bare(bound, mail_json_host):
    mail_json_host("mailhost")
    assert dc_env.take_mail_host() == dc_env.FROM_MAIL_JSON
    assert dc_env.HOST == "mailhost"           # HOST still mints Message-IDs
    assert dc_env.MAILTO == "ada"


# ---- 2. a DC_MAILTO that names a host is kept as given --------------------------

def test_an_explicit_name_at_host_is_kept(plain):
    assert dc_env.mailto({"DC_MAILTO": FAR}) == FAR
    assert dc_env.mailto({"DC_MAILTO": f"box@{HOST}", "SESSIONMAIL_HOST": "other"}) == f"box@{HOST}"
    assert dc_env.mailto({"PIP_CHAT_MAILTO": FAR}) == FAR       # the deprecated alias too


def test_an_explicit_name_at_host_is_kept_by_bind_and_rebind(bound, mail_json_host):
    bound.setenv("DC_MAILTO", FAR)
    dc_env.bind_agent()
    assert dc_env.MAILTO == FAR
    mail_json_host("mailhost")
    dc_env.take_mail_host()
    assert dc_env.MAILTO == FAR


def test_an_explicit_bare_name_is_kept(plain):
    assert dc_env.mailto({"DC_MAILTO": "frontdesk"}) == "frontdesk"


# ---- 3. a hostname change does not change the default -------------------------

def test_a_renamed_machine_keeps_the_same_default(plain):
    before = dc_env.mailto({})
    plain.setattr(dc_env.socket, "gethostname", lambda: "laptop-2.local")
    assert dc_env.mail_host({}) == "laptop-2"        # the host did change ...
    assert dc_env.mailto({}) == before == "claude"   # ... the box did not


def test_a_renamed_sessionmail_host_keeps_the_same_default(plain):
    assert dc_env.mailto({"SESSIONMAIL_HOST": "laptop"}) == dc_env.mailto({"SESSIONMAIL_HOST": "laptop-2"})


def test_a_renamed_machine_keeps_the_bound_mailto(bound):
    dc_env.bind_agent()
    before = dc_env.MAILTO
    bound.setattr(dc_env.socket, "gethostname", lambda: "laptop-2.local")
    dc_env.bind_agent()
    assert dc_env.MAILTO == before == "ada"


# ---- 4. a bare DC_MAILTO is a box on this host ---------------------------------

def test_a_bare_address_is_on_this_host(plain):
    env = {"SESSIONMAIL_HOST": HOST}
    assert seatmail.on_host("claude", env)
    assert seatmail.on_host("ada+web", env)            # a bare plus address too
    assert seat.on_host("claude", env)
    assert not seatmail.on_host(FAR, env)               # the control: a far host stays off


def test_drop_a_bare_mailto_makes_its_box_at_start(drop):
    """PHONE-207 refuses an off-host DC_MAILTO on the drop; a bare one is here."""
    env = {**os.environ, "DC_MAILTO": "claude"}
    box = seat.have_box(env, env)
    assert box.name == "claude" and box.mailto == "claude"
    assert box.path == str(drop.root / "claude")


def test_drop_install_makes_a_bare_mailto_box(drop):
    env = {**os.environ, "DC_MAILTO": "claude"}
    box = seat.ensure_box(env, env)
    assert box.path == str(drop.root / "claude")
    assert ["setup", "--box", "claude"] in drop.argvs()


def test_drop_the_default_box_line_is_bare(drop, monkeypatch):
    """install / setup / status print Box.line(): the bare name, no host."""
    monkeypatch.setenv("DC_AGENT", "claude")
    env = dict(os.environ)
    box = seat.have_box(env, env)
    assert box.mailto == "claude"
    assert box.line() == f"Delta text goes to claude, box {drop.root / 'claude'}"
    assert "@" not in box.line().partition(", box ")[0]


def test_drop_a_bare_cc_with_a_box_is_kept(drop):
    drop.box("bob")
    kept, dropped = seatmail.guard_cc(["bob"], dict(os.environ))
    assert kept == ["bob"] and dropped == []

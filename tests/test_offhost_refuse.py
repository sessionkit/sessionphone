"""Under the mail drop, an off-host recipient is refused at start, not per turn.

The drop delivers on this host only: a send to an address off it is refused
whole (rc=3, undeliverable). So the bot whose DC_MAILTO is off this host, and
`sessionphone web --legs` whose To is off it, would lose every mail of the
run. They start anyway today (seat.have_box answers a path-less Box off this
host, on any carrier), and then each send is refused.

On an MTA an address off this host is the MTA's to route: there they start,
as today.

And there is ONE "this host" predicate, dc_env.is_this_host: seatmail.on_host
(which seat.on_host and the start's box check ask) must agree with it, the
reverse-DNS guard included.

Fakes only: tests/test_drop_box.py's fake `message`.
"""
import os
import pathlib
import shutil
import tempfile

import pytest
import test_drop_box as dropbox  # its fake `message` (Drop) and HOST; nothing in it is changed

from sessionphone import seat
from sessionphone.bridge import dc_env, seatmail

HOST = dropbox.HOST

FAR = "claude@elsewhere.example"
MTA = "/usr/sbin/sendmail"


@pytest.fixture
def drop(tmp_path, monkeypatch):
    d = dropbox.Drop(tmp_path, monkeypatch)
    yield d
    seatmail.forget_boxes()


@pytest.fixture
def web_root():
    """A scratch root with a SHORT path (a Unix socket path is capped at 104 bytes)."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="spb-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---- (a) the bot's start: DC_MAILTO off this host ---------------------------------

def test_drop_an_off_host_mailto_is_refused_at_start(drop):
    env = {**os.environ, "DC_MAILTO": FAR}
    with pytest.raises(seat.NoBox) as e:
        seat.have_box(env, env)
    why = str(e.value)
    assert "\n" not in why                                      # one plain line
    assert FAR in why and f"not on this host ({HOST})" in why
    assert "set DC_MAILTO to a box on this host" in why          # what to do: a local To ...
    assert "an MTA in SESSIONMAIL_SENDMAIL" in why               # ... or a carrier that reaches it
    assert drop.argvs() == []                                   # nothing asked, nothing made


def test_drop_install_refuses_an_off_host_mailto_too(drop):
    """`bot install` and `account setup` (ensure_box) refuse what the start
    would refuse, so install loads no job that refuses at its first start."""
    env = {**os.environ, "DC_MAILTO": FAR}
    with pytest.raises(seat.NoBox) as e:
        seat.ensure_box(env, env)
    assert FAR in str(e.value) and "set DC_MAILTO" in str(e.value)
    assert drop.argvs() == []


def test_mta_install_an_off_host_mailto_makes_no_box(drop, monkeypatch):
    """The control: on an MTA install makes no box off this host, as before."""
    monkeypatch.setenv("SESSIONMAIL_SENDMAIL", MTA)
    env = {**os.environ, "DC_MAILTO": FAR}
    box = seat.ensure_box(env, env)
    assert box.mailto == FAR and box.path is None and drop.argvs() == []


def test_drop_a_reverse_dns_name_under_the_host_is_refused(drop):
    """A reverse-DNS name is never this host, even one under the short host."""
    addr = f"claude@{HOST}.x.arpa"
    env = {**os.environ, "DC_MAILTO": addr}
    with pytest.raises(seat.NoBox) as e:
        seat.have_box(env, env)
    assert addr in str(e.value)


def test_drop_then_every_send_to_it_is_refused(drop):
    """The per-send refusal the start lets through: the whole mail, rc=3."""
    sent = seatmail.send(FAR, "s", "b")
    assert not sent.ok
    assert "not on this host" in (sent.error or "")
    assert not [p for p in drop.root.glob("*/new/*")]


def test_mta_an_off_host_mailto_starts(drop, monkeypatch):
    """The control: on an MTA the address is the MTA's to route."""
    monkeypatch.setenv("SESSIONMAIL_SENDMAIL", MTA)
    env = {**os.environ, "DC_MAILTO": FAR}
    box = seat.have_box(env, env)
    assert box.mailto == FAR and box.path is None and drop.argvs() == []


def test_drop_an_on_host_mailto_starts(drop):
    """The control: on this host the drop's box is made, as before."""
    env = {**os.environ, "DC_MAILTO": f"claude@{HOST}"}
    assert seat.have_box(env, env).path == str(drop.root / "claude")


def test_drop_a_tailnet_name_under_the_host_is_on_host(drop):
    """The control: a name under the short host (a tailnet FQDN) is this host."""
    drop.box("claude")
    env = {**os.environ, "DC_MAILTO": f"claude@{HOST}.tail.example"}
    assert seat.have_box(env, env).path == str(drop.root / "claude")


def test_drop_the_bot_start_refuses_an_off_host_mailto(drop, monkeypatch, tmp_path, capsys):
    """bot.main(): one line on stderr, exit 2, before the outbox or any log line."""
    from sessionphone.bridge import bot
    monkeypatch.setenv("DC_MAILTO", FAR)
    lines = []
    dropbox._start(monkeypatch, tmp_path, lines)
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 2
    err = capsys.readouterr().err.strip()
    assert "\n" not in err and err.startswith(f"sessionphone bot: {FAR} is not on this host")
    assert lines == [] and not (tmp_path / "outbox").exists()   # nothing half-started
    assert not [a for a in drop.argvs() if a[:1] == ["send"]]


def test_mta_the_bot_start_takes_an_off_host_mailto(drop, monkeypatch, tmp_path):
    """The control: on an MTA the bot starts and names the box off this host."""
    from sessionphone.bridge import bot
    monkeypatch.setenv("SESSIONMAIL_SENDMAIL", MTA)
    monkeypatch.setenv("DC_MAILTO", FAR)
    lines = []
    dropbox._start(monkeypatch, tmp_path, lines)
    with pytest.raises(dropbox.Started):
        bot.main()
    assert lines[-1].startswith(f"agent box: Delta text goes to {FAR}")


# ---- (a) `sessionphone web --legs`: the To off this host --------------------------

def _web_legs_to(to, drop, web_root, monkeypatch, tmp_path):
    """_web_legs with the seat's To TO."""
    import asyncio

    from sessionphone.web import server as srvmod
    for k in ("DC_AGENT", "PIP_CHAT_AGENT", "PIP_CHAT_MAILTO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DC_HOST", HOST)
    monkeypatch.setattr(seatmail, "check", lambda *a, **k: None)
    monkeypatch.setattr(srvmod.WebServer, "_own_origins", lambda self: {f"http://localhost:{self.port}"})
    out = []
    rc = srvmod.run(web_root / "web", port=0, host="127.0.0.1", tls=None, origins=(), legs=True,
                    explicit_root=True, mail_to=to, heard_dir=tmp_path / "conversations",
                    echo=out.append, until=lambda: asyncio.sleep(0))
    return rc, out


def test_drop_web_legs_to_an_off_host_to_is_refused_at_start(drop, web_root, monkeypatch, tmp_path):
    rc, out = _web_legs_to("alice@elsewhere.example", drop, web_root, monkeypatch, tmp_path)
    assert rc != 0
    (why,) = out                                                # one plain line, before anything is served
    assert why.startswith("sessionphone web: ") and "alice@elsewhere.example" in why
    assert "\n" not in why
    assert "set --mail-to to a box on this host" in why          # the web seat's own setting
    assert not list(web_root.rglob("*.sock"))
    assert not [a for a in drop.argvs() if a[:1] == ["send"]]


def test_drop_web_legs_to_an_on_host_to_starts(drop, web_root, monkeypatch, tmp_path):
    """The control: under the drop a To on this host still starts the call."""
    drop.box("alice")
    rc, out = _web_legs_to(f"alice@{HOST}", drop, web_root, monkeypatch, tmp_path)
    assert rc == 0, out


def test_mta_web_legs_to_an_off_host_to_starts(drop, web_root, monkeypatch, tmp_path):
    """The control: on an MTA the web call starts."""
    monkeypatch.setenv("SESSIONMAIL_SENDMAIL", MTA)
    rc, out = _web_legs_to("alice@elsewhere.example", drop, web_root, monkeypatch, tmp_path)
    assert rc == 0, out


# ---- (b) ONE "this host" predicate ------------------------------------------------
#
# (address, mail host) -> on this host? dc_env.is_this_host's answer is the
# judgement; seatmail.on_host must give the same, row for row.

ON_HOST = [
    ("a@stranger", "stranger", True),
    ("a@STRANGER", "stranger", True),
    ("a@stranger.ts.net", "stranger", True),            # a tailnet name under the short host
    ("a@stranger.", "stranger", True),
    ("a@stranger.ts.net.", "stranger", True),           # a trailing dot is the same name
    ("a@strangerx", "stranger", False),
    ("a@elsewhere.example", "stranger", False),
    ("a@stranger.x.arpa", "stranger", False),           # reverse DNS: never a mail host
    ("a@stranger.x.arpa.", "stranger", False),          # ... a trailing dot does not hide it
    ("a@stranger.in-addr.arpa", "stranger", False),
    ("a@x.ip6.arpa", "x.ip6.arpa", False),              # a reverse-DNS mail host names no host
]


@pytest.mark.parametrize("address,host,want", ON_HOST)
def test_is_this_host_is_the_judgement(address, host, want):
    """dc_env.is_this_host takes a domain as written; on_host strips its
    trailing dot before asking it."""
    assert dc_env.is_this_host(address.rpartition("@")[2].rstrip("."), host) is want


@pytest.mark.parametrize("address,host,want", ON_HOST)
def test_seatmail_on_host_agrees_with_is_this_host(address, host, want):
    env = {"SESSIONMAIL_HOST": host}
    assert seatmail.on_host(address, env) is want
    assert seat.on_host(address, env) is want                   # seat asks the same judgement

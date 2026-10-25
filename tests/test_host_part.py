"""PHONE-217: dc_env's copy of the mail drop's host-part rule, held equal to sessiontools'.

The rule's home is sessiontools `src/sessiontools/operator.py` (`short_host`,
`is_this_host`, `host_part`), from ST-217, measured at its READY tag
ready/fix/ST-217-operator-host -> 41b3d96 (not yet merged; sessiontools
sends the merged sha when it lands). sessionphone does not depend on
sessiontools, so dc_env carries a copy, and the rows below are that
sha's own answers, verbatim (the task's findings/rca.md, produced by running
operator.py at 41b3d96 with the same seams and its mail setting stubbed).
A change upstream changes these rows; this copy must follow.

The one step that differs in its means is the mail setting: upstream reads
the `mail` tool's settings layers, dc_env asks the sessionmail the bot sends
with for the same mail.json `host`. Both are stripped, both count empty as
unset. Here that ask is stubbed at settings.mail_file, so dc_env's own
settings.mail_text (the strip) still runs.

It also records why dc_env.HOST, mailto() and seatmail.id_host stay on
mail_host: on some inputs short_host gives another value (the strip, and an
empty-after-strip variable falling through), and id_host must stay
byte-equal to sessionmail's host_of.
"""
import pytest

from sessionphone import settings
from sessionphone.bridge import dc_env, seatmail

UPSTREAM_SHA = "41b3d96"   # sessiontools operator.py, ST-217's READY tag
ARPA = "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa"
A = "…ip6.arpa"             # how the upstream table writes ARPA

# (env, mail.json host, hostname, getfqdn, short_host, host_part): upstream @ 41b3d96, verbatim
UPSTREAM = [
    ({}, "", "stranger.local", A, "stranger", "stranger"),
    ({}, "", "stranger", "stranger.example-tail.ts.net", "stranger", "stranger.example-tail.ts.net"),
    ({}, "", "stranger", "mac.example.net", "stranger", "stranger"),
    ({}, "", "Stranger", "Stranger.Example-Tail.ts.net", "stranger", "stranger.example-tail.ts.net"),
    ({}, "", "stranger", "stranger", "stranger", "stranger"),
    ({}, "", "stranger", "", "stranger", "stranger"),
    ({}, "", "stranger", "strangerfoo.example.net", "stranger", "stranger"),
    ({}, "", "stranger", "stranger.ip6.arpa", "stranger", "stranger"),
    ({}, "", "", "x.example.net", "", None),
    ({}, "", "localhost", "localhost", "localhost", "localhost"),
    ({"SESSIONMAIL_HOST": "box"}, "", "stranger", "box.example-tail.ts.net", "box", "box.example-tail.ts.net"),
    ({"SESSIONMAIL_HOST": "Box"}, "", "stranger", A, "box", "box"),
    ({"SESSIONMAIL_HOST": " "}, "", "stranger", A, "stranger", "stranger"),
    ({"SESSIONMAIL_HOST": " box "}, "", "stranger", A, "box", "box"),
    ({"SESSIONMAIL_HOST": "", "SESSION_MAIL_HOST": "old"}, "", "stranger", "old.lan", "old", "old.lan"),
    ({}, "filed", "stranger", "filed.example.net", "filed", "filed.example.net"),
    ({}, " Filed ", "stranger", A, "filed", "filed"),
    ({"SESSIONMAIL_HOST": "box"}, "filed", "stranger", "filed.example.net", "box", "box"),
    ({"SESSIONMAIL_HOST": "x.ip6.arpa"}, "", "stranger", A, "x.ip6.arpa", "x.ip6.arpa"),
]

# is_this_host(domain, host) -> answer: upstream @ 41b3d96, verbatim
UPSTREAM_IS_THIS_HOST = [
    ("stranger.ts.net", "stranger", True),
    ("STRANGER", "stranger", True),
    ("strangerx", "stranger", False),
    ("stranger.x.arpa", "stranger", False),
    ("x", "", False),
    ("stranger.", "stranger", True),
]


def full(s):
    return ARPA if s == A else s


@pytest.fixture
def filed(monkeypatch):
    """The mail.json `host` sessionmail would give, as settings.mail_file's probe."""
    def given(value):
        probe = {dc_env.MAIL_HOST_KEY: (value, settings.HOST_SOURCE)} if value else {}
        monkeypatch.setattr(seatmail, "resolve_message", lambda e=None: "/fake/message")
        monkeypatch.setattr(settings, "mail_file", lambda exe: probe)
    return given


def row_id(row):
    env, mail, hn, fq, _sh, _hp = row
    return f"env={env}|mail={mail!r}|hostname={hn!r}|fqdn={fq!r}"


@pytest.mark.parametrize("row", UPSTREAM, ids=[row_id(r) for r in UPSTREAM])
def test_short_host_and_host_part_match_upstream_row_for_row(row, filed):
    env, mail, hostname, fqdn, want_short, want_part = row
    filed(mail)
    assert dc_env.short_host(env, lambda: hostname) == want_short
    got = dc_env.host_part(env, resolve_fqdn=lambda: full(fqdn), resolve_hostname=lambda: hostname)
    assert got == full(want_part)


@pytest.mark.parametrize("domain,host,want", UPSTREAM_IS_THIS_HOST)
def test_is_this_host_matches_upstream(domain, host, want):
    assert dc_env.is_this_host(domain, host) is want


def test_the_dns_name_is_asked_only_once_a_short_host_is_found(filed):
    filed("")
    asked = []
    assert dc_env.host_part({}, resolve_fqdn=lambda: asked.append(1) or "x.example.net",
                            resolve_hostname=lambda: "") is None
    assert asked == []


def test_ask_false_runs_nothing_and_takes_the_hostname(monkeypatch):
    monkeypatch.setattr(settings, "mail_file", lambda exe: pytest.fail("asked sessionmail with ask=False"))
    assert dc_env.short_host({}, lambda: "Stranger.local", ask=False) == "stranger"
    assert dc_env.host_part({}, resolve_fqdn=lambda: ARPA, resolve_hostname=lambda: "stranger",
                            ask=False) == "stranger"


def test_the_defaults_are_the_socket_calls(monkeypatch, filed):
    filed("")
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger.local")
    monkeypatch.setattr(dc_env.socket, "getfqdn", lambda: "stranger.example-tail.ts.net")
    assert dc_env.host_part({}) == "stranger.example-tail.ts.net"


# --- the PHONE-98 last resort, on host_part's answer ---------------------------

@pytest.mark.parametrize("host", [None, "", "  ", "localhost", "LOCALHOST.", "localhost.localdomain",
                                  "x.ip6.arpa", ARPA, "4.3.2.1.in-addr.arpa", "arpa"])
def test_no_host_localhost_or_reverse_dns_is_a_problem(host):
    assert dc_env.host_part_problem(host)


@pytest.mark.parametrize("host", ["stranger", "macbook", "stranger.example-tail.ts.net", "mac.example.net"])
def test_a_dotless_short_host_or_a_dotted_name_is_no_problem(host):
    assert dc_env.host_part_problem(host) is None


# --- why HOST and id_host stay on mail_host (mailto() is bare: PHONE-248) --------

@pytest.mark.parametrize("env", [{"SESSIONMAIL_HOST": " box "}, {"SESSIONMAIL_HOST": " "}])
def test_short_host_would_change_mail_host_beyond_case_so_host_stays(env, monkeypatch):
    """mail_host is sessionmail's host_of: no strip, and ' ' counts as set. short_host
    strips, and falls through on ' '. Moving HOST onto it would change these values.
    The default To carries neither: it is the bare agent (PHONE-248)."""
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger")
    today, rule = dc_env.mail_host(env, ask=False), dc_env.short_host(env, ask=False)
    assert today.lower() != rule
    assert dc_env.mailto({**env, "DC_AGENT": "ada"}) == "ada"


@pytest.mark.parametrize("env", [{}, {"SESSIONMAIL_HOST": " box "}, {"SESSIONMAIL_HOST": "Box"},
                                 {"SESSION_MAIL_HOST": "m2"}, {"SESSIONMAIL_HOST": " "}])
def test_id_host_is_still_mail_host_never_short_host(env, monkeypatch):
    monkeypatch.setattr(dc_env.socket, "gethostname", lambda: "stranger.local")
    monkeypatch.setattr(settings, "mail_file", lambda exe: None)
    assert seatmail.id_host(env) == dc_env.mail_host(env)

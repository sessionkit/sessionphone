"""dc_env - who this bridge is and where it keeps its state, from the environment.

Each setting is DC_<NAME>. PIP_CHAT_<NAME> is a deprecated alias: it is
still read, with a warning on stderr, so an older launchd job keeps working.
Rename it. An empty value counts as unset. The defaults suit a bare install: one bot per user, with all of
its state in one private root.

The root is DC_ROOT, else sessionphone.paths.root() (SESSIONPHONE_ROOT,
else the root `bot install` keeps in phone.json, else paths.default_root():
~/.sessionkit/phone/state, or the old ~/.sessionphone while only that one is
there). It is mode 0700, and it holds the account, the
outbox, the call dirs, the approved callers and the ear and mouth sockets.
`sessionphone setup` makes it. A named profile (DC_PROFILE) keeps all of
those in its own home, <root>/<profile>/ (home()); the default profile's
home is the root itself. bin/dc-env mirrors this file: change both
or neither.

The agent is picked as `bot install` picks it (agent_from): DC_AGENT, else
this session's durable seat as sessionmail names it, else the one bot
phone.json records, else the harness vendor's word (settings.default_agent:
`session agent default`, else "claude"). AGENT and the values built from it
(AGENT_NAME, ADDR, MAILTO, the greetings) are bound on first use, not at
import, so importing this module never asks sessionmail; the bot's job
always sets DC_AGENT, so the bot never asks either.

MAILTO, the agent's box, is a bare name by default: the agent's, never
<agent>@<host>. sessionmail adds this host to a bare To on both of its
carriers, so a bare box still delivers after the host is renamed, where a
baked `name@<old host>` is refused. A DC_MAILTO is kept exactly as given:
name a host in it only for a box on another host.
"""
import os
import pathlib
import socket
import sys

from sessionphone import paths  # the one root, shared with the setup verbs
from sessionphone import settings  # sessionmail's own answer for its mail.json

HOME = pathlib.Path.home()


ALIAS = "PIP_CHAT_"   # the deprecated prefix: read, with one warning per name
_warned = set()       # names already warned about in this process
BAD = []   # settings that did not parse, said once in the bot's log (never a crash on import)


def _alias(name, value):
    """-> value, read from ALIAS+name: say so on stderr, once per name per process."""
    if name not in _warned:
        _warned.add(name)
        print(f"sessionphone: {ALIAS}{name} is deprecated; rename it DC_{name} "
              "(the old name is read until a later release)", file=sys.stderr)
    return value


def env(name, default=None):
    """DC_<name>, else the deprecated PIP_CHAT_<name> (warned once), else
    default. Empty counts as unset."""
    return _get(os.environ, name) or default


def _get(e, name):
    """DC_<name>, else the deprecated PIP_CHAT_<name> (warned once), from the
    mapping e. Empty counts as unset."""
    if e.get(f"DC_{name}"):
        return e[f"DC_{name}"]
    if e.get(f"{ALIAS}{name}"):
        return _alias(name, e[f"{ALIAS}{name}"])
    return None


def agent_from(environ=None):
    """-> (the bot a process run with ENVIRON speaks for, where the name came
    from in words), picked as `bot install` picks it (settings.bot_name,
    asking sessionmail): DC_AGENT (or its deprecated alias), else this
    session's durable seat, else the one bot phone.json records, else the
    harness vendor's word (settings.default_agent, else "claude"). A
    phone.json that cannot be used, or two bots in it and no name, gives
    that same default, and BAD says why. bin/dc-env asks this same
    function, so the dc-* tools and the bot pick one name."""
    e = os.environ if environ is None else environ
    raw = (_get(e, "AGENT") or "").strip().lower()
    if raw:
        return raw, "DC_AGENT in this shell"
    try:
        return settings.bot_name(e, verb=None, ask=True)
    except settings.Bad as exc:
        agent = settings.default_agent(e)
        BAD.append(f"{exc} Using {agent} for now.")
        return agent, f"the default ({settings.shown(settings.file_path())} cannot be used)"


# Bound on first use (__getattr__), from os.environ then: AGENT and every value built from it.
_BY_AGENT = ("AGENT", "AGENT_FROM", "AGENT_NAME", "ADDR", "MAILTO",
             "GREETING_NAME", "GREETING", "GREETING_OUT", "GREETING_VOICEMAIL")
for _n in _BY_AGENT:    # a reload binds them afresh
    globals().pop(_n, None)


def bind_agent():
    """Bind AGENT (agent_from, on os.environ now) and every value built from
    it. Done on first use; `bot run` does it again once it has set DC_AGENT."""
    g = globals()
    agent, came = agent_from()
    name = env("AGENT_NAME") or agent.capitalize()
    g.update(AGENT=agent, AGENT_FROM=came, AGENT_NAME=name,
             ADDR=env("ADDR") or f"{agent}-chat@{HOST}",       # the bridge's own mail address
             MAILTO=env("MAILTO") or default_mailto(agent))    # the agent's box: where text in lands
    bind_greetings(greeting_name(agent))


def greeting_name(agent, display=None):
    """-> the name the bot says in its greetings, first match wins:
    DC_AGENT_NAME, DC_NAME, DISPLAY (the account's display name, once the
    bot's start has settled it), else the agent's name, capitalised. It is
    the name people see in their chat list, so a bot set up with
    `setup --name` greets by that name. AGENT_NAME (the mail's From, the
    transcript labels) stays the agent's."""
    return (env("AGENT_NAME") or (env("NAME") or "").strip()
            or (display or "").strip() or agent.capitalize())


def bind_greetings(name):
    """Bind GREETING_NAME and the three greetings built from NAME. A
    DC_GREETING, DC_GREETING_OUT or DC_GREETING_VOICEMAIL is a whole line and
    is used as it is."""
    globals().update(
        GREETING_NAME=name,
        GREETING=env("GREETING") or f"{name} here. Go ahead.",
        GREETING_OUT=env("GREETING_OUT") or f"Hi, {name} here, ringing you back.",
        GREETING_VOICEMAIL=env("GREETING_VOICEMAIL") or (
            f"{name} can't pick up right now. Leave a message and it reaches "
            f"{name} as mail."))


def settle_greetings(display):
    """Bind the greetings again, now the bot's start has settled the
    account's DISPLAY name (bot.main, after displayname.at_start). An empty
    DISPLAY keeps the greetings bind_agent made. Opens nothing."""
    if "AGENT" not in globals():
        bind_agent()
    bind_greetings(greeting_name(globals()["AGENT"], display))


def __getattr__(name):
    if name in _BY_AGENT:
        bind_agent()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# sessionmail's own host settings (its host_of): both spellings, empty = unset.
MAIL_HOST_VARS = ("SESSIONMAIL_HOST", "SESSION_MAIL_HOST")


MAIL_HOST_KEY = settings.MAIL_KEYS["SESSIONMAIL_HOST"]
FROM_MACHINE = "this machine's name"
FROM_MAIL_JSON = f"sessionmail's {settings.shown(settings.MAIL_FILE)} `{MAIL_HOST_KEY}`"


def mail_host_from(environ=None, ask=True):
    """-> (the host sessionmail delivers this machine's mail on, where it came
    from in words), in sessionmail's own order (its host_of): this
    environment's $SESSIONMAIL_HOST, else $SESSION_MAIL_HOST; else the `host`
    sessionmail keeps in its own mail.json; else the first label of this
    machine's name (socket.gethostname()). Empty counts as unset, as in
    sessionmail.

    The file is asked of the sessionmail the bot sends with (its `message`:
    DC_MESSAGE, else `message` on this environment's PATH, through
    settings.mail_file, asked once per process), never read here. ASK=False
    skips that step: the import-time defaults below take the environment
    only, so importing this module runs nothing.

    Both of sessionmail's carriers deliver <box>@<this host>: the mail drop keeps
    a To whose domain is this host or under it, and postfix's boxes are keyed by
    the short name. A DNS answer (getfqdn) is neither, and on a Mac it can be a
    reverse-DNS name. seatmail.id_host is this same answer, so the two cannot
    drift."""
    e = os.environ if environ is None else environ
    for var in MAIL_HOST_VARS:
        if e.get(var):
            return e[var], f"{var} in this environment"
    if ask:
        from sessionphone.bridge import seatmail  # here, not at import: seatmail imports this module
        filed = settings.mail_text(settings.mail_file(seatmail.resolve_message(e)), MAIL_HOST_KEY)
        if filed:
            return filed, FROM_MAIL_JSON
    return socket.gethostname().split(".")[0], FROM_MACHINE


def mail_host(environ=None, ask=True):
    """The host sessionmail delivers this machine's mail on (mail_host_from)."""
    return mail_host_from(environ, ask)[0]


# ---- the host part of a derived address: the mail drop's rule for a bare From ----
#
# short_host, is_this_host and host_part mirror sessiontools'
# src/sessiontools/operator.py functions of the same names, which follow the
# rule the mail drop uses to qualify a bare From. sessionphone does not depend
# on sessiontools, so the rule is copied here, and tests/test_host_part.py
# holds this copy equal to that one, row by row. Only the mail-setting step
# differs in its means: it asks the sessionmail the bot sends with (as
# mail_host_from does) for the same mail.json `host`.
#
# This is NOT mail_host. mail_host neither strips nor lowercases, as
# sessionmail's own host_of does not, and seatmail.id_host must stay
# byte-equal to that, or `message send --message-id` refuses the ids. HOST,
# mailto() and the ids stay on mail_host; host_part is the web seat's default
# host (web/turns.named_seat).

REVERSE_DNS_SUFFIX = ".arpa"   # a reverse-DNS name is never a mail host, whatever it sits under


def short_host(environ=None, resolve_hostname=None, ask=True):
    """This host's short name, lowercased, in the mail drop's order: the
    variables (MAIL_HOST_VARS), sessionmail's mail.json `host`, the first label
    of this machine's name. Each value is stripped, and an empty one counts as
    unset. "" if there is none. ASK=False skips the mail.json step (nothing is
    run); resolve_hostname stands in for socket.gethostname in tests."""
    e = os.environ if environ is None else environ
    resolve_hostname = socket.gethostname if resolve_hostname is None else resolve_hostname
    for var in MAIL_HOST_VARS:
        value = (e.get(var) or "").strip()
        if value:
            return value.lower()
    found = ""
    if ask:
        from sessionphone.bridge import seatmail  # here, not at import: seatmail imports this module
        found = settings.mail_text(settings.mail_file(seatmail.resolve_message(e)), MAIL_HOST_KEY) or ""
    if not found:
        found = (resolve_hostname() or "").strip().split(".")[0]
    return found.lower()


def is_this_host(domain, host):
    """DOMAIN names this host: it is HOST, or a name under it (case-insensitive),
    and it is not a reverse-DNS name."""
    domain, host = domain.strip().lower(), host.strip().lower()
    if not host or domain.endswith(REVERSE_DNS_SUFFIX):
        return False
    return domain == host or domain.startswith(host + ".")


def host_part(environ=None, *, resolve_fqdn=None, resolve_hostname=None, ask=True):
    """The host part of a derived local address: this machine's DNS name when
    it names this host (a name under the short host, such as a tailnet name),
    else the short host. A reverse-DNS name or an unrelated DNS name is never
    used. None when there is no short host. The DNS name is looked up only once
    a short host is found; resolve_fqdn stands in for socket.getfqdn in tests."""
    resolve_fqdn = socket.getfqdn if resolve_fqdn is None else resolve_fqdn
    host = short_host(environ, resolve_hostname, ask)
    if not host:
        return None
    fqdn = (resolve_fqdn() or "").strip().lower()
    return fqdn if is_this_host(fqdn, host) else host


def host_part_problem(host):
    """-> None, or what is wrong with HOST, host_part()'s answer, as the host of
    a seat's mail: none, localhost, or a reverse-DNS name (SESSIONMAIL_HOST can
    still name one). A short name has no dot by design and is accepted, as
    host_problem accepts it."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return "is empty"
    if host == "arpa" or host.endswith(REVERSE_DNS_SUFFIX):
        return f"is {host!r}, a reverse-DNS name, not a mail domain"
    if host == "localhost" or host.startswith("localhost."):
        return f"is {host!r}, which names no machine"
    return None


# This machine's mail name. It builds ADDR's default below (MAILTO's default is
# bare: default_mailto); Message-IDs use seatmail.id_host. It's not the Delta
# Chat relay: that lives in the account.
# At import, from the environment only (no sessionmail is asked): the bot
# takes sessionmail's own file into account once, at start (take_mail_host).
HOST = env("HOST") or mail_host(ask=False) or "localhost"
# ADDR (the bridge's own mail address) is built from AGENT and HOST, and MAILTO
# (the agent's box: where text in lands) from AGENT alone, on first use (bind_agent).


def default_mailto(agent):
    """The agent's box when no DC_MAILTO is set: the bare agent name. No host,
    not even DC_HOST: sessionmail adds this host to a bare To, so the box
    survives a rename of the host."""
    return agent


def mailto(environ=None):
    """Where a bot run with `environ` mails Delta text: DC_MAILTO as given,
    else the bare agent name (default_mailto). MAILTO's answer, read from
    `environ` now (the plist's, at install), not frozen at import."""
    e = os.environ if environ is None else environ
    if _get(e, "MAILTO"):
        return _get(e, "MAILTO")
    return default_mailto(agent_from(e)[0])


def _junk(host):
    """-> None, or what is wrong with HOST as a mail domain built from a DNS name:
    empty, a .arpa name (ip6.arpa, in-addr.arpa), localhost, or no dot."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return "is empty"
    if host == "arpa" or host.endswith(".arpa"):
        return f"is {host!r}, a reverse-DNS name, not a mail domain"
    if host == "localhost" or host.startswith("localhost.") or "." not in host:
        return f"is {host!r}, which has no mail domain"
    return None


def host_problem(environ=None, host=None):
    """-> None, or why the bot must refuse to start: with no DC_HOST, the
    bot's own address (ADDR) is built on mail_host(), and it names no machine.
    MAILTO is not: its default is the bare agent name (default_mailto), which
    sessionmail delivers on this host whatever the host is called.

    The default host is sessionmail's own (mail_host): the host both of its
    carriers deliver this machine's boxes on. A short name has no dot by
    design and is accepted; it is refused only when it is empty or localhost.
    It is never a DNS answer: on macOS getfqdn() can be a reverse-DNS name
    (1.0.0...ip6.arpa), and every incoming chat was once mailed to
    <agent>@<that> while the bot looked healthy (measured live on a user's Mac).
    A DC_HOST the operator named is trusted (DC_HOST=localhost included), and
    with DC_ADDR named no address is built from HOST, so nothing is refused.

    environ is the environment the bot will run with (os.environ, or the
    plist's at install); host stands in for mail_host(environ) in tests.
    """
    e = os.environ if environ is None else environ

    def get(name):
        return _get(e, name)

    if get("HOST") or get("ADDR"):
        return None
    host = (mail_host(e) if host is None else host).strip().lower().rstrip(".")
    if not host:
        what = "is empty"
    elif host == "localhost" or host.startswith("localhost."):
        what = f"is {host!r}, which names no machine"
    else:
        return None
    agent = agent_from(e)[0]
    return (f"no DC_HOST is set and this machine's mail host {what}, so the bot's own "
            f"address would be {agent}-chat@{host or 'localhost'}. Set "
            f"SESSIONMAIL_HOST (sessionmail's host for this machine) or DC_HOST to this "
            f"machine's mail domain (DC_HOST=mac.example.net) and run "
            f"`sessionphone bot install` again, or name DC_ADDR.")


def fqdn_problem(environ, fqdn):
    """-> None, or why a host built from the DNS name FQDN is no mail domain,
    when `environ` names no DC_HOST: the rule for a caller that builds its host
    from getfqdn() itself (_junk). A DC_HOST is trusted. No default here is
    built that way now: a derived default goes through host_part."""
    if _get(environ, "HOST"):
        return None
    what = _junk(fqdn)
    return None if what is None else f"no DC_HOST is set and this machine's name {what}"


HOST_PROBLEM = host_problem(host=mail_host(ask=False))   # refused in bot main(), never a crash on import


def take_mail_host():
    """At the bot's start: HOST, ADDR, MAILTO and HOST_PROBLEM in the full
    order, sessionmail's mail.json included (mail_host_from). They are
    rebound only when that file gives the host, since without it the
    import-time answer already is the full one. -> where the host came from."""
    global HOST, HOST_PROBLEM
    host, where = mail_host_from()
    if where != FROM_MAIL_JSON:
        return where
    HOST = env("HOST") or host
    agent = globals().get("AGENT") or __getattr__("AGENT")     # the bound agent, else bind it now
    globals().update(ADDR=env("ADDR") or f"{agent}-chat@{HOST}", MAILTO=env("MAILTO") or default_mailto(agent))
    HOST_PROBLEM = host_problem(host=host)
    return where


# ---- a profile's home: where one Delta Chat account's state lives ----
#
# One root can hold two bots, each its own Delta Chat account: the default
# profile (DC_PROFILE unset) and a named one (DC_PROFILE=public). Chat ids are
# per account (chat 12 on one is another chat on the other), so everything a
# bot keeps about its account lives in its HOME: the root itself for the
# default profile, exactly as before, and <root>/<profile>/ for a named one.
# That is the allow-list, the callers, the outbox, the seen ids, the acks, the
# undelivered spool, the calls, the live call, the sockets and the accounts
# folder. An explicit DC_<NAME> still wins. ROOT_WIDE names the few settings
# that are not about an account and stay at the root for every profile: the
# wait music and the voice map. The logs are not in the root at all: they are
# under ~/.sessionkit/phone/log/<bot>[/<profile>]/ (log_file, paths.log_dir).
ROOT_WIDE = ("WAIT_MUSIC", "VOICES")


def profile(environ=None):
    """DC_PROFILE (or its deprecated alias) from `environ`, stripped; "" for the
    default profile."""
    e = os.environ if environ is None else environ
    return (_get(e, "PROFILE") or "").strip()


def root_of(environ=None):
    """The root a bot run with `environ` keeps: DC_ROOT, else SESSIONPHONE_ROOT,
    else paths.root() (which may refuse: settings.Bad)."""
    e = os.environ if environ is None else environ
    r = _get(e, "ROOT") or e.get("SESSIONPHONE_ROOT")
    return pathlib.Path(r).expanduser() if r else paths.root()


def home(environ=None, root=None):
    """Where a bot run with `environ` keeps its account's state: <root>/<profile>
    when DC_PROFILE is set, else the root itself, unchanged. ROOT, when given,
    is the root already worked out (install's); else root_of(environ)."""
    r = root_of(environ) if root is None else pathlib.Path(root)
    p = profile(environ)
    return r / p if p else r


def log_file(environ=None, agent=None, profile_name=None):
    """The log a bot run with ENVIRON writes: DC_LOG (or its deprecated
    alias), else paths.bot_log, ~/.sessionkit/phone/log/<bot>[/<profile>]/bot.log.
    AGENT and PROFILE_NAME, when given, are the ones already worked out (the
    bot's, bound at import); else agent_from(environ) and profile(environ)."""
    e = os.environ if environ is None else environ
    given = _get(e, "LOG")
    if given:
        return pathlib.Path(given).expanduser()
    a = agent if agent is not None else agent_from(e)[0]
    return paths.bot_log(a, profile(e) if profile_name is None else profile_name)


def log_for_reader(environ=None, agent=None, root=None):
    """The bot's log as a reader finds it (`bot status`, bin/dc-env, bin/dc-log):
    log_file, unless DC_LOG is unset and only the bot's old log is there,
    <home>/bot.log (home, ROOT as given), which a bot that has not started on
    this version yet still writes (paths.legacy_or)."""
    e = os.environ if environ is None else environ
    new = log_file(e, agent)
    if _get(e, "LOG"):
        return new
    try:
        old = home(e, root) / paths.BOT_LOG
    except settings.Bad:
        return new          # no root to look in: the new log is the answer
    return paths.legacy_or(new, old)


def callers_file(environ=None):
    """The approved-callers file a bot run with `environ` reads: DC_CALLERS_FILE,
    else <home>/callers.txt (home: <DC_ROOT, else SESSIONPHONE_ROOT, else
    paths.root()>, plus /<DC_PROFILE> when one is set). The same answer as
    path("CALLERS_FILE", "callers.txt"), for any environment (the plist's, at
    install)."""
    e = os.environ if environ is None else environ
    v = _get(e, "CALLERS_FILE")
    if v:
        return pathlib.Path(v).expanduser()
    return home(e) / "callers.txt"


def accounts_dir(environ=None, root=None):
    """The Delta Chat accounts folder a bot run with `environ` opens:
    DC_ACCOUNTS, else <home>/accounts (home as callers_file; ROOT as home()).
    The one answer: the bot opens it, and so does every CLI verb that opens
    the account (`setup`, `link`, `callers add|pin` without --key)."""
    e = os.environ if environ is None else environ
    v = _get(e, "ACCOUNTS")
    if v:
        return pathlib.Path(v).expanduser()
    return home(e, root) / "accounts"


def invite_file(environ=None, root=None):
    """The invite link saved for the account accounts_dir opens: <home>/invite.txt
    (home as callers_file). `setup` writes it, `invite` reads it, and the bot
    rewrites it at its start (displayname.at_start, on its home)."""
    return home(environ, root) / "invite.txt"


def channels_file(environ=None):
    """The allow-list a bot run with `environ` reads: DC_CHANNELS_FILE, else
    <home>/channels.txt (home as callers_file). path("CHANNELS_FILE",
    "channels.txt"), but read from `environ` now (the plist's, at install), not
    from the home frozen at import."""
    e = os.environ if environ is None else environ
    v = _get(e, "CHANNELS_FILE")
    if v:
        return pathlib.Path(v).expanduser()
    return home(e) / "channels.txt"


# The per-account files a named profile may not share with the default profile,
# each with why: (what, setting, file, reason). A channels.txt entry is a chat
# id, one account's; a callers.txt line is a key that may ring (keygate.judge:
# the key decides), so one file approves the same callers on both bots.
PROFILE_FILES = (
    ("allow-list", "CHANNELS_FILE", "channels.txt",
     "Chat ids are per account, so an entry for one account would open a chat on the other."),
    ("callers file", "CALLERS_FILE", "callers.txt",
     "It says who may ring, so a caller approved on one profile could ring the other."),
)


def profile_files_problem(environ=None, root=None, profile_name=None):
    """A NAMED profile whose allow-list or callers file is the default
    profile's (<root>/channels.txt, <root>/callers.txt), by a DC_CHANNELS_FILE
    or DC_CALLERS_FILE that points there or by a link (the files are compared
    by realpath), would gate one account on the other's. -> None, or the
    reason a bot run with `environ` refuses to start. The one rule: the bot's
    start check (bot.check_profile_files) and `bot install` both ask it.
    ROOT and PROFILE_NAME, when given, are the ones already worked out (the
    bot's, bound at import); else root_of(environ) and profile(environ)."""
    e = os.environ if environ is None else environ
    p = profile(e) if profile_name is None else profile_name
    if not p:
        return None
    r = root_of(e) if root is None else pathlib.Path(root)
    for what, name, rel, reason in PROFILE_FILES:
        given, own, theirs = _get(e, name), r / p / rel, r / rel
        mine = pathlib.Path(given).expanduser() if given else own
        if os.path.realpath(mine) != os.path.realpath(theirs):
            continue
        if given:
            return (f"profile {p}'s {what} is {mine}, the default profile's own. {reason} "
                    f"Unset DC_{name} (this profile then reads {own}) or point it at "
                    f"a file of this profile's own.")
        # its own path is a link (the file, or the profile's folder) to the default's
        return (f"profile {p}'s {what} is {mine}, which resolves to {os.path.realpath(theirs)}, "
                f"the default profile's own. {reason} Make {mine} a file of this profile's own, "
                f"not a link to the default profile's.")
    return None


def stt_url(environ=None):
    """The STT a bot run with `environ` posts to: DC_STT_URL, else STT_DEFAULT.
    STT_URL's answer, read from `environ` now (the plist's, at install: the
    hearing probe), not from os.environ frozen at import."""
    e = os.environ if environ is None else environ
    return _get(e, "STT_URL") or STT_DEFAULT


def stt_model(environ=None):
    """DC_STT_MODEL from `environ`, else None (the model then suits the URL's kind)."""
    e = os.environ if environ is None else environ
    return _get(e, "STT_MODEL")


# What --owner wants: the owner is a Delta Chat contact.
# The kit's operator setting is a MAIL address, so nothing seeds the owner from
# it, and every no-owner refusal says which address to give.
DELTA_ADDR = ("ADDR is the owner's Delta Chat address, as their Delta Chat profile "
              "shows it, not their mail address")


def callers_problem(environ=None):
    """-> None, or why NOBODY may ring a bot run with `environ`: calls are on and
    the callers file holds no address (or, once keyed, none pinned to a key).
    Every call would be refused, and with
    no line here nothing would say so. DC_CALLERS counts for nothing: an env
    var never grants the right to ring (callers_ignored says so).

    `bot install` refuses on it; the bot logs it loudly at start (it still
    runs: chat works without callers). DC_CALLS=off is never rung, by design.
    The refusal names the two ways forward: link the phone, or --owner with
    the owner's Delta Chat address."""
    e = os.environ if environ is None else environ
    if (_get(e, "CALLS") or "on") == "off":
        return None
    # one parser, shared with `sessionphone callers` and the call gate
    from sessionphone import keygate
    f = callers_file(e)
    try:
        entries = keygate.read_caller_entries(f)
        # A file from an older version (no keys marker) is pinned at the bot's
        # first start (keygate.migrate), so its address lines still count here.
        if any(x.key for x in entries) or (entries and not keygate.is_marked(f)):
            return None
        if entries:
            # `callers pin` reaches f only with this bot's settings in front of it
            pin = f"{with_env(e)}sessionphone callers pin ADDR"
            return (f"NOBODY may ring this agent: {f} lists {len(entries)} address(es), none "
                    f"pinned to a key, so every call would be refused. Pin each one: "
                    f"`{pin} --key FPR` (the phone shows the key under "
                    f"the contact's profile, Encryption), or with the bot stopped "
                    f"`{pin}`.")
        what = "has no address in it" if f.exists() else "does not exist"
    except OSError as exc:
        what = f"cannot be read ({exc.strerror or exc})"
    # `link` writes callers_file() for the environment it runs in: with this
    # bot's settings in front of it, it writes f and opens this bot's account
    first = (f"run `{with_env(e)}sessionphone link` and scan the invite from the owner's "
             f"phone (the first to join becomes the owner)")
    return (f"NOBODY may ring this agent: {f} {what}, so every call would be refused. "
            f"Name the owner, one of two ways: {first}; or run "
            f"`sessionphone bot install --owner ADDR` (writes ADDR into {f}), where {DELTA_ADDR}.")


def callers_ignored(environ=None):
    """-> None, or the one line that says DC_CALLERS (or its deprecated alias) is
    set and IGNORED. Approved callers come from the callers file only: any
    process can set an env var, so one never grants the right to ring. The bot
    logs this at start; `bot install` refuses on it."""
    e = os.environ if environ is None else environ
    name = next((k for k in ("DC_CALLERS", f"{ALIAS}CALLERS")
                 if any(a.strip() for a in (e.get(k) or "").split(","))), None)
    if name is None:
        return None
    f = callers_file(e)
    return (f"{name} is set and IGNORED: approved callers come from {f} only; add each "
            f"with `{with_env(e)}sessionphone callers add ADDR`.")


# The settings that move a bot's callers file or account away from the CLI
# root's: a CLI verb run with them in front of it touches that bot's own.
WHERE = ("ROOT", "PROFILE", "ACCOUNTS", "CALLERS_FILE")


def with_env(environ=None):
    """The `DC_<NAME>=value ` prefix (shell-quoted, one per WHERE name set in
    `environ`) that makes a CLI verb resolve this bot's callers file and
    account; "" when none is set, the CLI's own answer then being the bot's."""
    import shlex
    e = os.environ if environ is None else environ
    out = []
    for n in WHERE:
        v = _get(e, n)
        if v:
            out.append(f"DC_{n}={shlex.quote(v)} ")
    return "".join(out)


def _root():
    """DC_ROOT, else paths.root(). At import this never raises: a settings
    file that cannot be used gives the default root, and its refusal goes
    in BAD (the verbs that need the root ask paths.root() and refuse)."""
    if env("ROOT"):
        return pathlib.Path(env("ROOT")).expanduser()
    try:
        return paths.root()
    except settings.Bad as exc:
        r = paths.default_root()
        BAD.append(f"{exc} Using {r} for now.")
        return r


ROOT = _root()
PROFILE = profile()     # "" = the default profile: its home is ROOT itself


def home_dir():
    """This bot's home, from ROOT and PROFILE as bound at import (home(), frozen
    as ROOT is): ROOT for the default profile, ROOT/<PROFILE> for a named one."""
    return ROOT / PROFILE if PROFILE else ROOT
# The voice the bridge speaks in when nobody names one: a kokoro id (af_sky),
# or a clone dir under ~/.voicemode/voices.
VOICE = env("VOICE") or "af_sky"
# GREETING and GREETING_OUT: built from GREETING_NAME on first use (bind_agent),
# and again once the bot's start has settled its display name (settle_greetings).
SAY = env("SAY") or "dc-say"                      # the reply hint in bridged mail
# How an incoming call from an approved caller is answered:
#   auto - the bot answers at once and greets in the agent's voice. Cheap:
#          he says what he wants and it lands as mail, no agent turn needed.
#   ring - ring first, answer second. The bot mails the agent RINGING and
#          waits RING_WAIT seconds for `dc-answer CHAT_ID`. No answer (the
#          agent is busy, or not running) = it answers as voicemail.
ANSWER = (env("ANSWER") or "auto").strip().lower()


def number(name, default):
    """DC_<name> as a float; a value that is not one falls back to default, and says so."""
    v = env(name)
    try:
        return float(v) if v else default
    except ValueError:
        BAD.append(f"DC_{name}={v!r} is not a number; using {default:g}")
        return default


# Under the core's 120 s ring (RINGING_SECONDS), with room for ICE (~5 s).
RING_WAIT = number("RING_WAIT", 25.0)
# The no-reply watchdog: a call turn with no reply this many seconds
# after it was mailed hears HOLDING_LINE, once. Off by default (0); set
# DC_NO_REPLY_S above 0 to turn it on.
NO_REPLY_S = number("NO_REPLY_S", 0.0)
HOLDING_LINE = env("HOLDING_LINE") or "I heard you, give me a moment."
# Floor control (bridge/floor.py): a reply line does not start while the caller
# is speaking. It waits for FLOOR_GAP seconds of his quiet, FLOOR_CAP at most,
# then plays. FLOOR_GAP=0 turns it off: a reply starts the moment it arrives.
FLOOR_GAP = number("FLOOR_GAP", 0.8)
FLOOR_CAP = number("FLOOR_CAP", 6.0)
# GREETING_VOICEMAIL: built from GREETING_NAME, as GREETING is.
# STT for voice notes, and for what a caller says: any OpenAI-compatible
# /v1/audio/transcriptions. The default is VoiceMode's mlx-audio on :8890, the
# server the bot's voice already uses. The bot requests Parakeet from it
# (stt.MODEL_DEFAULTS); that the server holds Parakeet is INFERRED, not
# measured. `ears serve` (Apple speech, macOS 27) is
# DC_STT_URL=http://127.0.0.1:2023/v1/audio/transcriptions.
STT_DEFAULT = "mlx-audio+http://127.0.0.1:8890/v1/audio/transcriptions"
STT_URL = env("STT_URL") or STT_DEFAULT
STT_MODEL = env("STT_MODEL")   # unset: the model suits the URL's kind (bridge/stt.py)
# aioice's STUN retry. Its own 0.5 s never fits a public relay's round trip,
# and calls dropped at 33 s. 2.0 s is proven end to end.
STUN_RTO = float(env("STUN_RTO") or "2.0")
# The heard log streamed live into a webxdc app (bridge/xdc_feed.py).
# XDC_FEED_CHATS is a consent of its own, apart from DC_HEARD_CHATS: that one
# lets a call in a chat WRITE the heard log; this one lets the heard log be READ
# OUT into a chat, to every member's open app. Comma-separated chat ids, unset
# by default, which means no feed can bind. List only your own 1:1 chat.
XDC_FEED_CHATS = env("XDC_FEED_CHATS") or ""
XDC_FEEDS_MAX = max(0, int(number("XDC_FEEDS_MAX", 2)))     # feeds at once


def path(name, *rel):
    """DC_<name> (or its deprecated alias) as a path, else home_dir()/<rel...>:
    this bot's home, which is ROOT for the default profile. A ROOT_WIDE name
    is ROOT/<rel...> for every profile. Never LOG: the log is out of the
    home (log_file)."""
    v = env(name)
    if v:
        return pathlib.Path(v).expanduser()
    return (ROOT if name in ROOT_WIDE else home_dir()).joinpath(*rel)


# The bot's pid file (bridge/pidfile.py): the bot writes it once it holds the
# account; dc-live reads it to know the bot runs. bin/dc-env's DC_PID_FILE is
# this same path: change both or neither.
PID_FILE = path("PID_FILE", "bot.pid")
# The live call (calls.py writes it on connect, removes it at hang-up; the bot
# removes an earlier bot's at start). bin/dc-env's DC_LIVE is this same path.
LIVE_FILE = path("LIVE", "call-live.json")


# The bot's Delta Chat display name (sessionphone/displayname.py). Unset, it is
# the agent's name, title-cased, unless the account carries a name set by hand.
# The bot reads it from ITS OWN environment, at its start.
NAME = (env("NAME") or "").strip()


# The first call's wait (bridge/firstcall.py): while the speech models are
# still downloading, a call hears a line saying so, then the music file, if
# there is one, until the call can hear. on/off, default on; off is no wait.
FIRST_CALL_WAIT = (env("FIRST_CALL_WAIT") or "on").strip().lower() != "off"
# Any audio file ffmpeg reads. Missing or unreadable: the line alone.
WAIT_MUSIC = path("WAIT_MUSIC", "wait-music.opus")


def say_cmd(profile=None):
    """dc-say as a reader can run it. A second profile's bot is named by its
    outbox, DC_OUTBOX=..., never `--profile`: an older dc-say has no --profile
    and crashes on it with ValueError. dc-say finds that bot's list and live
    call beside the outbox (bin/dc-env's dc_channels and dc_live)."""
    profile = PROFILE if profile is None else profile
    return f"DC_OUTBOX={path('OUTBOX', 'outbox')} {SAY}" if profile else SAY


def reply_hint(chat_id, call=None, profile=None, channel=None):
    """How to answer a bridged mail. On a call the mouth leads and dc-say is
    only the fallback, for when the mouth has a problem. call is the call's
    id, the invite message's id.
    channel: the channel NAME the chat is listed as, so the hint names it, as
    the agent would; the chat id otherwise."""
    say = say_cmd(profile)
    if call:
        return (f'Answer with: mouth say --device call:{call} "text"\n'
                f"If the mouth fails: {say} --call {call} {chat_id} 'text'")
    return f"{say} {channel or chat_id} 'your reply'"


if __name__ == "__main__":
    bind_agent()
    for k in ("AGENT", "AGENT_FROM", "AGENT_NAME", "HOST", "ADDR", "MAILTO", "ROOT", "PROFILE",
              "VOICE", "GREETING",
              "ANSWER", "RING_WAIT", "NO_REPLY_S", "FLOOR_GAP", "FLOOR_CAP", "GREETING_VOICEMAIL", "STT_URL", "STUN_RTO",
              "XDC_FEED_CHATS", "XDC_FEEDS_MAX", "FIRST_CALL_WAIT", "WAIT_MUSIC", "NAME"):
        print(f"{k:12} {globals()[k]}")
    print(f"{'HOME':12} {home_dir()}")
    if HOST_PROBLEM:
        print(f"{'REFUSED':12} {HOST_PROBLEM}")
    for k, rel in (("ACCOUNTS", ("accounts",)), ("OUTBOX", ("outbox",)), ("CALLS_DIR", ("calls",)),
                   ("LIVE", ("call-live.json",)), ("ACKS", ("acks",)),
                   ("CALLERS_FILE", ("callers.txt",)), ("CHANNELS_FILE", ("channels.txt",)),
                   ("SEEN", ("bridged-ids.txt",)), ("UNDELIVERED", ("undelivered",)),
                   ("PID_FILE", ("bot.pid",))):
        print(f"{k:12} {path(k, *rel)}")
    print(f"{'LOG':12} {log_file(agent=AGENT, profile_name=PROFILE)}")

"""Shared test rigs. No test here ever runs the real launchctl."""
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlparse

import pytest

# ---- the seat's environment never reaches the suite (PHONE-131) -------------
# bridge/dc_env.py reads the agent and the root AT IMPORT: DC_AGENT, else
# CLAUDE_CODE_AGENT, gives dc_env.AGENT and so calls.DEFAULT_VOICE_MAP; DC_ROOT
# (else SESSIONPHONE_ROOT) gives dc_env.ROOT and every dc_env.path() default
# (voices.json, guests, callers, calls, spool). Run from a seat's shell, a test
# then read that seat's name and its live root. So this runs at the top of the
# first conftest pytest loads, before any sessionphone import, and drops every
# such variable for the whole session. A test that needs one sets it itself
# (monkeypatch.setenv); the autouse tripwires below set DC_SENDMAIL, DC_MESSAGE
# and DC_SIGNER_* per test, after this.
_SEAT_ENV_NAMES = ("CLAUDE_CODE_AGENT", "SESSIONPHONE_ROOT")
_SEAT_ENV_PREFIXES = ("DC_", "PIP_CHAT_")
_early = sorted(m for m in sys.modules if m == "sessionphone" or m.startswith("sessionphone."))
if _early:   # the isolation below would be too late to matter: say so, loudly
    raise RuntimeError(f"tests/conftest.py: sessionphone was imported before the env was isolated: {_early}")
for _k in [k for k in os.environ if k in _SEAT_ENV_NAMES or k.startswith(_SEAT_ENV_PREFIXES)]:
    del os.environ[_k]

from sessionphone import hearing, service


class _R:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeLaunchd:
    """launchd as a small state machine, standing in for service._launchctl.

    bootout is asynchronous, as the real one is: it returns at once, but the
    old job lingers for `linger` more `print` polls, and a bootstrap in that
    window fails "5: Input/output error" (PHONE-98 gap b, seen on a dev Mac and on
    a user's Mac). `bootstrap_rc` scripts extra failures for the first
    bootstraps; `never_loads` makes a bootstrap that answers 0 leave nothing
    loaded. Every call is kept in `calls`.

    A legacy job (a label under service.LEGACY_LABEL_PREFIX, PHONE-193) is
    separate state: `legacy` holds the legacy labels loaded, and print and
    bootout of one answer from it, never from the job above.
    """

    def __init__(self, loaded: bool = True, linger: int = 0, bootstrap_rc=(), never_loads: bool = False):
        self.loaded = loaded
        self.linger = linger
        self.lingering = 0
        self.bootstrap_rc = list(bootstrap_rc)
        self.never_loads = never_loads
        self.legacy: set[str] = set()
        self.calls: list[tuple] = []

    @staticmethod
    def _legacy_label(args) -> str | None:
        lab = args[-1].rsplit("/", 1)[-1] if len(args) > 1 else ""
        return lab if lab.startswith(service.LEGACY_LABEL_PREFIX + ".") else None

    def __call__(self, *args: str) -> _R:
        self.calls.append(args)
        verb = args[0]
        old = self._legacy_label(args)
        if old and verb in ("print", "bootout"):
            if old not in self.legacy:
                return _R(113 if verb == "print" else 3, "", f"Could not find service \"{old}\"")
            if verb == "bootout":
                self.legacy.discard(old)
                return _R(0)
            return _R(0, "state = running\npid = 1717\n")
        if verb == "bootout":
            if not self.loaded:
                return _R(3, "", "Boot-out failed: 3: No such process")
            self.loaded, self.lingering = False, self.linger
            return _R(0)
        if verb == "print":
            if self.lingering:
                self.lingering -= 1
                return _R(0, "state = exiting\n")
            if self.loaded:
                return _R(0, "state = running\npid = 4242\n")
            return _R(113, "", f"Could not find service \"{args[1]}\" in domain")
        if verb == "bootstrap":
            rc = self.bootstrap_rc.pop(0) if self.bootstrap_rc else 0
            if self.lingering or rc:
                return _R(rc or 5, "", "Bootstrap failed: 5: Input/output error")
            self.loaded = not self.never_loads
            return _R(0)
        raise AssertionError(f"unexpected launchctl verb: {args}")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeClock:
    """service._clock / service._sleep: time passes only when the code sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    # raising=False: the repro ran these tests against service.py before it had a clock
    monkeypatch.setattr(service, "_clock", c.clock, raising=False)
    monkeypatch.setattr(service, "_sleep", c.sleep, raising=False)
    return c


def no_stt(argv, **kw):
    """hearing.RUN in a world with no STT: curl's answer when nothing listens (rc 7)."""
    return subprocess.CompletedProcess(argv, 7, "", "")


@pytest.fixture
def launchd(monkeypatch, clock):
    """Install a FakeLaunchd (loaded, no linger) as service._launchctl; tests
    reconfigure its attributes before acting.

    Every `bot install` that succeeds ends with the hearing probe (PHONE-18), so
    this rig's world has no STT either: hearing.RUN answers as curl does when
    nothing listens. A test of the hearing line sets its own RUN."""
    fake = FakeLaunchd()
    monkeypatch.setattr(service, "_launchctl", fake)
    monkeypatch.setattr(hearing, "RUN", no_stt)
    return fake


@pytest.fixture(scope="session")
def _sendmail_tripwire_script(tmp_path_factory):
    """A stand-in sendmail that records its call to $SENDMAIL_TRIP_LOG and fails (75)."""
    script = tmp_path_factory.mktemp("sendmail") / "sendmail"
    script.write_text('#!/bin/sh\nprintf \'%s | %s\\n\' "$PYTEST_CURRENT_TEST" "$*" >> "$SENDMAIL_TRIP_LOG"\n'
                      "cat > /dev/null\nexit 75\n")
    script.chmod(0o755)
    return script


# The real sendmail, however a test or a rig names it: the usual path (and
# sessionmail's own default), the other usual path, a bare PATH lookup, or
# anything that resolves to the same file.
_REAL_SENDMAILS = ("/usr/sbin/sendmail", "/usr/lib/sendmail", "sendmail")


def _is_real_sendmail(argv0) -> bool:
    argv0 = os.fspath(argv0)
    if argv0 in _REAL_SENDMAILS:
        return True
    return any(os.path.exists(r) and os.path.realpath(argv0) == os.path.realpath(r)
               for r in _REAL_SENDMAILS[:2])


def _reaches_real_sendmail(args, shell) -> bool:
    if isinstance(args, (str, bytes, os.PathLike)):
        cmd = os.fsdecode(args)
        words = shlex.split(cmd) if shell else [cmd]
    else:
        words = [os.fsdecode(a) for a in args]
    return any(_is_real_sendmail(w) for w in (words if shell else words[:1]))


@pytest.fixture(autouse=True)
def sendmail_tripwire(_sendmail_tripwire_script, tmp_path_factory, monkeypatch):
    """No test reaches the real /usr/sbin/sendmail (PHONE-129 fix-004).

    Since PHONE-72 part 2b nothing in src runs sendmail: every mail goes by
    sessionmail's `message send` (seatmail, and the message tripwire below).
    This stays because `message send` is itself a sendmail client, and a test
    that let a real one run would reach the real MTA. Two guards, and a test
    that trips either FAILS at teardown:

    1. DC_SENDMAIL is pointed at a tripwire script that records the call and
       exits 75. A child process inherits it (a rig that names it, as the old
       bot did, still trips here).
    2. subprocess.Popen is wrapped: a launch of the real sendmail (by
       absolute path, a bare `sendmail`, or a shell line naming it) is
       recorded here and swapped for a refuser that reads stdin and exits 75.
       subprocess.run, call and check_output all go through Popen.

    The in-process guard cannot see a child process that has DC_* stripped
    and then execs sendmail itself; no test does that today.

    Yields the call log; a test of the tripwire empties it. Applies to
    tests/bridge too: bin/bridge-check runs those files under pytest from the
    repo root, so this conftest loads."""
    log = tmp_path_factory.mktemp("sendmail-calls") / "calls"
    monkeypatch.setenv("DC_SENDMAIL", str(_sendmail_tripwire_script))
    monkeypatch.setenv("SENDMAIL_TRIP_LOG", str(log))

    real_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **k):
        exe = k.get("executable")
        if _reaches_real_sendmail(args, k.get("shell", False)) or (exe is not None and _is_real_sendmail(exe)):
            shown = args if isinstance(args, (str, bytes)) else " ".join(map(os.fsdecode, args))
            with open(log, "a") as f:
                f.write(f"{os.environ.get('PYTEST_CURRENT_TEST', '?')} | REAL {shown}\n")
            args, k["shell"] = ["/bin/sh", "-c", "cat > /dev/null; exit 75"], False
            k.pop("executable", None)
        real_init(self, args, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    yield log
    if log.exists() and log.read_text():
        pytest.fail(f"a test reached sendmail (use fake_message, or stub bot.subprocess.run):\n{log.read_text()}")


# ---- the message tripwire (PHONE-106 part 2a) --------------------------------
# Agent-bound mail now goes by sessionmail's `message send` (bridge/seatmail.py),
# so no test may reach the REAL `message` either: a real one, with a seat in the
# environment, would sign and deliver. This mirrors sendmail_tripwire above.

@pytest.fixture(scope="session")
def _message_tripwire_script(tmp_path_factory):
    """A stand-in `message` that records its argv to $MESSAGE_TRIP_LOG and refuses
    as `message send --json` does when the MTA will not take it (exit 3).

    The ONE exception is the start probe (seatmail.PROBE_ARGS, PHONE-72 part
    2b): it sends nothing by construction, so it is answered as a sessionmail
    with SM-716 answers it (exit 2, kind message-id) and NOT recorded. A test
    of the probe itself uses fake_message, which can answer it otherwise."""
    from sessionphone.bridge import seatmail
    probe = " ".join(seatmail.PROBE_ARGS)
    script = tmp_path_factory.mktemp("message") / "message"
    script.write_text('#!/bin/sh\n'
                      f'if [ "$*" = "{probe}" ]; then\n'
                      '  echo \'{"sent": "no", "kind": "message-id", "message": "probe", "notices": []}\'\n'
                      "  exit 2\nfi\n"
                      'printf \'%s | %s\\n\' "$PYTEST_CURRENT_TEST" "$*" >> "$MESSAGE_TRIP_LOG"\n'
                      "cat > /dev/null\n"
                      'echo \'{"sent": "no", "kind": "mta", "message": "message tripwire", "notices": []}\'\n'
                      "exit 3\n")
    script.chmod(0o755)
    return script


def _real_messages() -> set[str]:
    """Every path the real `message` is reachable by, on PATH or in uv's tool dir."""
    found = set()
    for p in (shutil.which("message"),
              os.path.expanduser("~/.local/share/uv/tools/sessionmail/bin/message"),
              os.path.expanduser("~/.local/bin/message")):
        if p and os.path.exists(p):
            found.add(os.path.realpath(p))
    return found


_REAL_MESSAGES = _real_messages()   # measured once, before any test edits PATH or HOME


def _is_real_message(argv0) -> bool:
    argv0 = os.fsdecode(argv0)
    return argv0 == "message" or os.path.realpath(argv0) in _REAL_MESSAGES


def _reaches_real_message(args, shell) -> bool:
    if isinstance(args, (str, bytes, os.PathLike)):
        cmd = os.fsdecode(args)
        words = shlex.split(cmd) if shell else [cmd]
    else:
        words = [os.fsdecode(a) for a in args]
    return any(_is_real_message(w) for w in (words if shell else words[:1]))


@pytest.fixture(autouse=True)
def message_tripwire(_message_tripwire_script, tmp_path_factory, monkeypatch):
    """No test reaches the real `message` (PHONE-106 part 2a).

    1. DC_MESSAGE is the tripwire script; it records the call and refuses
       (exit 3, kind mta), but answers the start probe as SM-716 does. A
       child process inherits it. DC_SIGNER_SOCK and DC_SIGNER_PUB name a
       fake signer in tmp whose comment names the suite's default seat,
       `claude-chat seat` (DEFAULT_SEAT), so bot.main() passes its start-up
       check; nothing there can sign anything. A test on another seat names
       its key with seat_key.
    2. subprocess.Popen is wrapped, as for sendmail: a launch of the real
       `message` (a bare `message`, its path on PATH or in uv's tool dir, or a
       shell line naming it) is recorded and swapped for a refuser that exits 3.
    3. seatmail.MESSAGE, the binary resolved at a bot start, is reset to None.

    A test that drives mail_in or the alert stubs bot.subprocess.run (or
    seatmail.send) itself. Yields the call log; a test of the tripwire empties it."""
    from sessionphone.bridge import seatmail
    log = tmp_path_factory.mktemp("message-calls") / "calls"
    signer = tmp_path_factory.mktemp("signer")
    (signer / "seat.pub").write_text(f"ssh-ed25519 AAAAtripwire {DEFAULT_SEAT} seat\n")
    monkeypatch.setenv("DC_MESSAGE", str(_message_tripwire_script))
    monkeypatch.setenv("DC_SIGNER_SOCK", str(signer / "agent.sock"))
    monkeypatch.setenv("DC_SIGNER_PUB", str(signer / "seat.pub"))
    monkeypatch.setenv("MESSAGE_TRIP_LOG", str(log))
    monkeypatch.setattr(seatmail, "MESSAGE", None)

    real_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **k):
        exe = k.get("executable")
        if _reaches_real_message(args, k.get("shell", False)) or (exe is not None and _is_real_message(exe)):
            shown = args if isinstance(args, (str, bytes)) else " ".join(map(os.fsdecode, args))
            with open(log, "a") as f:
                f.write(f"{os.environ.get('PYTEST_CURRENT_TEST', '?')} | REAL {shown}\n")
            args, k["shell"] = ["/bin/sh", "-c", "cat > /dev/null; exit 3"], False
            k.pop("executable", None)
        real_init(self, args, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    yield log
    if log.exists() and log.read_text():
        pytest.fail(f"a test reached message (stub bot.subprocess.run or seatmail.send instead):\n"
                    f"{log.read_text()}")


VALUE_FLAGS = ("-s", "-f", "-c", "--header", "--message-id", "-r", "--supersedes")   # message send's, as seatmail.args uses them
DEFAULT_SEAT = "claude-chat"     # dc_env.ADDR's local part with no DC_* set: the tripwire key's seat


@pytest.fixture
def seat_key(tmp_path, monkeypatch):
    """seat_key(comment) -> a fake .pub whose comment is COMMENT (`ada-chat seat`,
    `ada-chat@host seat`, or any junk), set as DC_SIGNER_PUB. The key check reads
    only the comment (seatmail.key_seat)."""
    def make(comment: str):
        pub = tmp_path / "keys" / "seat.pub"
        pub.parent.mkdir(exist_ok=True)
        pub.write_text(f"ssh-ed25519 AAAAfake {comment}\n".replace("  ", " "))
        monkeypatch.setenv("DC_SIGNER_PUB", str(pub))
        return pub
    return make


@pytest.fixture
def fake_message(tmp_path, monkeypatch):
    """A fake `message` as DC_MESSAGE (PHONE-72 part 2b): every call but the
    start probe is recorded (argv, env, stdin) and answered from a script.

    ns.answer(*replies): reply i is (rc, json-object) for send i; past the end,
    (0, {"sent": "yes", "id": "<the --message-id, else 1.2.3@host.session-mail>"}).
    ns.probe(rc, stdout, stderr=""): how the probe is answered; the default is
    sessionmail with SM-716's (2, kind message-id). ns.probes() counts them.
    ns.calls() -> [{"argv", "env", "body"}]; ns.argv() / env() / body(): the last.
    ns.mails() -> each send as an email.message, its headers as the argv ASKS
    for them (To, Subject, Cc, each --header, Message-ID, In-Reply-To and
    References from -r, Supersedes): what we hand sessionmail, not its bytes."""
    import email.message
    import json as _json
    import types as _types

    from sessionphone.bridge import seatmail
    rec = tmp_path / "message-rec"
    rec.mkdir()
    exe = tmp_path / "message-bin" / "message"
    exe.parent.mkdir()
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"d = pathlib.Path({str(rec)!r})\n"
        f"PROBE = {seatmail.PROBE_ARGS!r}\n"
        "a = sys.argv[1:]\n"
        "if a == PROBE:\n"
        "    n = len(list(d.glob('probe-*')))\n"
        "    (d / f'probe-{n:03d}').write_text('')\n"
        "    rc, out, err = json.loads((d / 'probe.json').read_text()) if (d / 'probe.json').exists() else (\n"
        "        2, json.dumps({'sent': 'no', 'kind': 'message-id', 'message': 'invalid --message-id', 'notices': []}), '')\n"
        "    sys.stdout.write(out + ('\\n' if out else '')); sys.stderr.write(err); sys.exit(rc)\n"
        "n = len(list(d.glob('call-*.json')))\n"
        "(d / f'call-{n:03d}.json').write_text(json.dumps({'argv': a, 'env': dict(os.environ), 'body': sys.stdin.read()}))\n"
        "replies = json.loads((d / 'answers.json').read_text()) if (d / 'answers.json').exists() else []\n"
        "mid = a[a.index('--message-id') + 1] if '--message-id' in a else '1.2.3@host.session-mail'\n"
        "rc, obj = replies[n] if n < len(replies) else (0, {'sent': 'yes', 'id': f'<{mid}>', 'notices': []})\n"
        "print(json.dumps(obj)); sys.exit(rc)\n")
    exe.chmod(0o755)
    monkeypatch.setenv("DC_MESSAGE", str(exe))
    ns = _types.SimpleNamespace(exe=exe, rec=rec)
    ns.answer = lambda *replies: (rec / "answers.json").write_text(_json.dumps([list(r) for r in replies]))
    ns.probe = lambda rc, out, err="": (rec / "probe.json").write_text(_json.dumps([rc, out, err]))
    ns.probes = lambda: len(list(rec.glob("probe-*")))
    ns.calls = lambda: [_json.loads(p.read_text()) for p in sorted(rec.glob("call-*.json"))]
    ns.argv = lambda: ns.calls()[-1]["argv"]
    ns.env = lambda: ns.calls()[-1]["env"]
    ns.body = lambda: ns.calls()[-1]["body"]

    def render(call):
        a, m = call["argv"], email.message.EmailMessage()
        flags, i = [], 2                       # argv: send TO, then flags
        while i < len(a):
            if a[i] in VALUE_FLAGS:
                flags.append((a[i], a[i + 1]))
                i += 2
            else:
                i += 1
        m["To"] = a[1]
        m["Subject"] = dict(flags)["-s"]
        ccs = [v for k, v in flags if k == "-c"]
        if ccs:
            m["Cc"] = ", ".join(ccs)
        for k, v in flags:
            if k == "--header":
                name, _, value = v.partition(": ")
                m[name] = value
        for k, v in flags:
            if k == "--message-id":
                m["Message-ID"] = f"<{v.strip('<>')}>"
            elif k == "-r":
                m["In-Reply-To"] = m["References"] = v
            elif k == "--supersedes":
                m["Supersedes"] = v
        m.set_content(call["body"])
        return m
    ns.mails = lambda: [render(c) for c in ns.calls()]
    return ns


@pytest.fixture(autouse=True)
def recording_switch_isolated(tmp_path_factory, monkeypatch):
    """The recording switch (bridge/recording.py) reads EARS_RECORD,
    EARS_RECORD_DAYS and ears.json ($EARS_CONF, else recording.EARS_CONF_DEFAULT).
    No test reads the seat's: each starts with both unset and EARS_CONF at a
    file that does not exist, so the switch is OFF (default). calls._switch,
    which a test that runs the real calls.setup() leaves set, is reset before
    and after. A test that wants recording on sets EARS_RECORD itself."""
    for k in ("EARS_RECORD", "EARS_RECORD_DAYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EARS_CONF", str(tmp_path_factory.mktemp("ears") / "ears.json"))
    for m in ("sessionphone.bridge.calls",):
        if m in sys.modules:
            monkeypatch.setattr(sys.modules[m], "_switch", None)
    yield
    if "sessionphone.bridge.calls" in sys.modules:
        sys.modules["sessionphone.bridge.calls"]._switch = None


@pytest.fixture(autouse=True)
def undelivered_in_tmp(tmp_path_factory, monkeypatch):
    """A failed seat send is spooled (bridge/spool.py, PHONE-106 do-003). No test
    may spool into the real <DC_ROOT>/undelivered: spool.DIR is a fresh tmp dir.
    Yields it, so a test can look at what was kept."""
    from sessionphone.bridge import spool
    d = tmp_path_factory.mktemp("undelivered")
    monkeypatch.setattr(spool, "DIR", d)
    yield d


@pytest.fixture(autouse=True)
def xdc_feed_in_tmp(tmp_path_factory, monkeypatch):
    """The bot's heard feed (bridge/xdc_feed.py, PHONE-165) is made at import over
    the real <root>/xdc-feeds.json and the real heard log, and bot.main() resumes
    and starts it. No test may read or rewrite those: when the bot is loaded,
    bot.XDC_FEED is a fresh feed over a tmp dir, and any thread it started is
    stopped after the test. Yields it (None when no test module loaded the bot)."""
    bot = sys.modules.get("sessionphone.bridge.bot")
    if bot is None:
        yield None
        return
    d = tmp_path_factory.mktemp("xdc-feed")
    feed = bot.xdc_feed_make(d / "xdc-feeds.json", d / "heard")
    monkeypatch.setattr(bot, "XDC_FEED", feed)
    yield feed
    feed.stop()


@pytest.fixture(autouse=True)
def imgtext_starts(monkeypatch):
    """bot.main() starts the OCR helper (imgtext.start, PHONE-190): on a Mac with
    swiftc that COMPILES it, or warms the built one, on a daemon thread, into
    dc_env.ROOT / "bin". dc_env.ROOT is bound at import, after the seat env above
    is dropped, so it is the user's REAL default root, and a rig's later
    setenv("SESSIONPHONE_ROOT") cannot move it. So no test starts the real
    engine: imgtext._ENGINE is a fresh engine per test whose start() only
    records its bin dir, builds nothing and runs nothing (it stays unstarted,
    so imgtext.read answers None, the no-OCR body). Yields the recorded bin
    dirs. A test of the engine itself makes its own ImageText (tests/test_imgtext.py)."""
    from sessionphone import imgtext
    starts = []
    engine = imgtext.ImageText()
    monkeypatch.setattr(engine, "start", lambda bin_dir, log, **kw: starts.append(pathlib.Path(bin_dir)))
    monkeypatch.setattr(imgtext, "_ENGINE", engine)
    yield starts


# ---- the STT tripwire (PHONE-18) ----------------------------------------------
# `bot install` now ends by asking the STT at DC_STT_URL to transcribe a test
# clip (sessionphone/hearing.py). No test may reach a live STT: the fleet's run
# on 127.0.0.1:2022 (whisper.cpp), :2023 (ears serve) and :8890 (mlx-audio), and
# a hosted one is someone else's. EARS-222's rule, kept here as a tripwire in
# the shape of sendmail_tripwire's.

LIVE_STT_PORTS = {2022, 2023, 8890}
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


# Port 1 on loopback (tcpmux): nothing on a Mac serves it, and only root may bind
# below 1024, so no test server can take it. A connection there is refused at once
# (curl rc 7). A port bound and never listened on is NOT: macOS drops the SYN and
# curl waits out its -m (measured, rc 28).
STT_CLOSED_URL = "mlx-audio+http://127.0.0.1:1/v1/audio/transcriptions"


def _live_stt(args, shell) -> str | None:
    """-> the URL, if this launch is curl posting a file (the STT request's shape,
    bridge/stt.py curl_argv) to a live STT: a fleet port, or a host off this Mac.
    A stub server on an ephemeral loopback port (test_stt's end to end) is no STT."""
    if isinstance(args, (str, bytes, os.PathLike)):
        cmd = os.fsdecode(args)
        words = shlex.split(cmd) if shell else [cmd]
    else:
        words = [os.fsdecode(a) for a in args]
    if not any(os.path.basename(w) == "curl" for w in (words if shell else words[:1])):
        return None
    if not any(w.startswith("file=@") for w in words):
        return None
    for w in words:
        if "://" not in w:
            continue
        try:
            u = urlparse(w)
            port = u.port
        except ValueError:
            return w
        if (u.hostname or "") not in _LOOPBACK or port in LIVE_STT_PORTS:
            return w
    return None


@pytest.fixture(autouse=True)
def stt_tripwire(tmp_path_factory, monkeypatch):
    """No test reaches a live STT (PHONE-18). Two guards, as sendmail_tripwire's:

    1. DC_STT_URL is STT_CLOSED_URL, a loopback port nothing serves, tagged
       mlx-audio so it names a model: a probe there is refused at once. A child
       process inherits it (test_media_guard passes it to its fresh process).
    2. Rigs strip every DC_* var, and then the default URL (:8890) is live. So
       subprocess.Popen is wrapped: curl posting a file to a fleet STT port or
       to a host off this Mac is recorded and swapped for one that exits 7
       (connection refused), and the test FAILS at teardown.

    The `launchd` rig sets hearing.RUN to a fake with no STT, so the install
    tests never launch curl at all. Yields the call log; a test of the tripwire
    empties it."""
    log = tmp_path_factory.mktemp("stt-calls") / "calls"
    monkeypatch.setenv("DC_STT_URL", STT_CLOSED_URL)

    real_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **k):
        url = _live_stt(args, k.get("shell", False))
        if url:
            with open(log, "a") as f:
                f.write(f"{os.environ.get('PYTEST_CURRENT_TEST', '?')} | LIVE STT {url}\n")
            args, k["shell"] = ["/bin/sh", "-c", "exit 7"], False
            k.pop("executable", None)
        real_init(self, args, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    yield log
    if log.exists() and log.read_text():
        pytest.fail(f"a test reached a live STT (stub hearing.RUN, or pass probe(run=...)):\n{log.read_text()}")


# ---- the test-time ANCHOR: synthetic 2030 dates --------------------------------
# A lone typed date or time in a test, one nothing else compares with, is the
# ANCHOR or a variation RELATIVE to it (ANCHOR - timedelta(days=1)), never a
# fresh literal. Both are Sundays in synthetic 2030: ANCHOR is Melbourne
# +10:00 (no DST), ANCHOR_SUMMER Melbourne +11:00 (DST). Defined here, once.
# Grouped date families (dates compared with each other) keep their own block
# shift instead. A test takes them as the `anchor` / `anchor_summer` fixtures:
# `from conftest import ANCHOR` is NOT safe, because tests/web/ has its own
# conftest.py and, once pytest has loaded it, the name `conftest` means that one.
import datetime as _dt

ANCHOR = _dt.datetime(2030, 6, 9, 12, 34, 56, tzinfo=_dt.UTC)
ANCHOR_SUMMER = _dt.datetime(2030, 12, 8, 12, 34, 56, tzinfo=_dt.UTC)


@pytest.fixture
def anchor() -> _dt.datetime:
    return ANCHOR


@pytest.fixture
def anchor_summer() -> _dt.datetime:
    return ANCHOR_SUMMER


# ---- the private-word guards' loud skip ---------------------------------------
# A guard that reads the private scan list (tests/_scanlist.py) and finds it unset
# skips, and this repeats that skip in the terminal summary, counted and naming the
# variable to set: a guard that cannot run says so, never passes quietly.
import _scanlist


def pytest_terminal_summary(terminalreporter):
    _scanlist.summary(terminalreporter)

"""echo.py - drop the words we just spoke, when they come back as his.

THE FAULT.  When the far end is a speakerphone in a noisy room, our own
text-to-speech comes out of their speakers and back into their microphone.
Transcription hears it, and the bridge labels every inbound line with the
caller's name, so OUR words enter the record as HIS. Two harms: the memory
is poisoned with misattributed speech, and the agent answers itself.

WHY IT IS NOT DONE IN THE AUDIO.  Acoustic echo cancellation has to predict
what is coming. We do not: we have the text, in advance, with timings. It
is a text-matching problem wearing an audio problem's coat. Gating the
audio was also tried and rejected - both bridges carry the note in
`_listen()`: "dropping frames here broke the VAD's continuity... Barge-in
works as a side effect." So the drop happens on the TEXT, after
transcription, and barge-in keeps working.

THE TWO PARTS.

  1. A playout clock. `spoken()` is told each clip's DURATION, and keeps a
     queue the way the outgoing track does, so it knows when each clip
     actually leaves the speaker. This matters more than it sounds: on the
     recorded call the bridge logged four utterances within two seconds that
     carried 17.7 SECONDS of audio, and their echoes arrived up to sixteen
     seconds after the log line. A window anchored on "when we called
     speak()" would have to be ~20s wide and would eat real speech.
     Anchored on projected end-of-playout, it is a second or two.

  2. An asymmetric fuzzy match. See `echolib.score`.

WHAT IT IS MEASURED AT.  Against 823 genuinely-human lines from 22 recorded
calls, and 9 known echoes from one recorded call:

     threshold   echoes caught   real lines wrongly dropped
       0.75          8 / 9            1 / 823   (0.12%)
       0.80          7 / 9            0 / 823   (0.00%)

Default is 0.80, because the two errors are not equal. A false positive
DELETES something the caller said, silently. A false negative leaves a line
misattributed - the bug we already have - and `check()` logs every drop, so
misses are visible and recoverable while deletions would not be.

The echo it does not catch at 0.80 is "Note written and committed." coming
back as "No commitment committed." - twenty-four characters, mangled past
recognition. No text matcher gets that one; say so rather than lowering the
bar until it does.

NEVER DELETE.  `check()` returns a verdict. It is the caller's job to
withhold the line from the record and LOG it. A gate nobody has seen fail
is a decoration.
"""
import datetime
import json
import os
import re
import time
import unicodedata
from collections import deque
from difflib import SequenceMatcher

# ----------------------------------------------------------- the matcher
# A heard line shorter than this is never judged an echo. Short lines carry
# too little information to tell "he agreed" from "we said that": "Bye.",
# "Email.", "No." all matched something we had just said, at ratio 1.00.
MIN_CHARS = 18

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").lower()
    return _WS.sub(" ", _PUNCT.sub(" ", s)).strip()


def _best_window(h, s):
    """The span of `s` that best explains `h`, as (ratio, window)."""
    if len(s) <= len(h):
        return SequenceMatcher(None, h, s, autojunk=False).ratio(), s
    best, win = 0.0, ""
    # anchor on the matching blocks rather than sliding every offset
    for a, b, size in SequenceMatcher(None, h, s, autojunk=False).get_matching_blocks():
        if not size:
            continue
        start = max(0, min(b - a, len(s) - len(h)))
        w = s[start:start + len(h)]
        r = SequenceMatcher(None, h, w, autojunk=False).ratio()
        if r > best:
            best, win = r, w
    return best, win


def score(heard, spoken):
    """0..1 - how much of `heard` is explained by a span of `spoken`.

    Asymmetric on purpose: a short thing we said can never explain a long
    thing he said, however well it matches inside it."""
    h, s = norm(heard), norm(spoken)
    if len(h) < MIN_CHARS or not s:
        return 0.0
    return _best_window(h, s)[0]


def best(heard, spoken_texts):
    b, which = 0.0, None
    for s in spoken_texts:
        r = score(heard, s)
        if r > b:
            b, which = r, s
    return b, which


# ------------------------------------------------------------- the window

# Seconds after a clip finishes playing during which its echo may still
# arrive: network jitter, the VAD's own hold, and transcription queueing.
# Measured echoes landed 1-2s past end-of-playout on the recorded call.
TAIL = 3.0

# How similar a heard line must be to a span of something we played.
# Measured, not chosen - see the table above.
THRESHOLD = 0.80

# Time is evidence. A line arriving a fraction
# of a second after our own audio stopped is in a position almost nothing
# else can occupy: for it to be innocent, the caller would have to be quoting us
# back, verbatim, within a second of us finishing. So inside that window a
# lower bar is safe, and outside it the full bar still applies.
#
# Measured over the bridge log and the transcript archive:
#
#   rule                      echoes caught   false positives
#   0.80 flat                       39         0/495 · 0/823
#   <=1.0s and >=0.70 too           50         0/495 · 0/823
#   <=1.0s and >=0.65 too           59         0/495 · 1/823
#
# 0.70 is chosen: it catches 28% more while keeping the zero-false-positive
# property across BOTH negative sets, and a false positive silently deletes
# something he said. The window is deliberately short - widening it to 3s
# adds almost nothing (71 vs 69 at the loosest bar), which is itself
# evidence that the effect really is concentrated right after playout.
NEAR_WINDOW = 1.0
NEAR_THRESHOLD = 0.70

# Say so when a new line has to wait this long behind audio already queued.
# At four seconds he has usually started answering the previous one.
BACKLOG_WARN = 4.0

# A hard cap on remembered utterances, for memory only - the real eviction
# is by TIME (`_expire`). It used to be a small count, and that was a bug:
# once the guard also reads voicemode's log, a burst from another agent
# could push the BRIDGE's own speech out of a window that was still open,
# silently disabling suppression for the one source it exists to cover.
KEEP = 256

# Any process on this machine may append one JSON object per line here to
# declare that it is about to play audio: {"at": epoch, "dur": seconds,
# "text": "..."}. The echo loop catches ANY agent speaking on this box, not
# only the bridge's own TTS - proved when a second agent's
# voicemode greeting came back through the call labelled as the caller.
SPOOL = os.environ.get("ECHO_SPOOL", os.path.expanduser("~/.voicemode/playout.jsonl"))

# ...and voicemode ALREADY WRITES ONE, so nothing has to be added to it.
# Its event log carries, live, one JSON object per line:
#   TTS_START         data.message  - the full text, at synthesis
#   TTS_FIRST_AUDIO                 - when sound starts leaving the speaker
#   TTS_PLAYBACK_END  data.metrics  - bytes/format/sample_rate -> duration
# Why the end of playback matters, by example: a greeting that plays for
# about 14 s can be heard back by the bridge about one second after
# playback ends.
VOICEMODE_EVENTS = os.environ.get(
    "ECHO_VOICEMODE_EVENTS",
    os.path.expanduser("~/.voicemode/logs/events"))


def _split_at(text, fraction):
    """Roughly which words were out before the cut. Proportional, snapped to
    a word boundary - speech is not uniform, so this is an estimate and is
    reported as one."""
    words = text.split()
    if not words:
        return "", ""
    n = max(0, min(len(words), round(len(words) * max(0.0, min(1.0, fraction)))))
    return " ".join(words[:n]), " ".join(words[n:])


class Played:
    """One thing we put out the speaker, and when it was actually audible."""

    __slots__ = ("text", "start", "end", "source", "key")  # end/text move on a cut

    def __init__(self, text, start, end, source, key=None):
        self.text, self.start, self.end, self.source = text, start, end, source
        # identifies the SAME utterance across two reports of it: voicemode
        # tells us once when sound starts (length estimated from the text)
        # and again when it stops (length from the byte count). The second
        # supersedes the first; without this they would both sit in the
        # window as if we had said it twice.
        self.key = key

    def __repr__(self):
        return f"<Played {self.source} {self.end - self.start:.1f}s {self.text[:40]!r}>"


class CutPoint:
    """Where barge-in stopped us: the agent is told at what point."""

    __slots__ = ("line", "seconds_in", "said", "unsaid", "dropped", "seconds_cut")

    def __init__(self, line, seconds_in, said, unsaid, dropped, seconds_cut):
        self.line, self.seconds_in = line, seconds_in
        self.said, self.unsaid = said, unsaid
        self.dropped, self.seconds_cut = dropped, seconds_cut

    def __bool__(self):
        return self.line is not None or bool(self.dropped)

    def __str__(self):
        if not self:
            return "nothing was playing"
        bits = []
        if self.line is not None:
            bits.append(f"cut {self.seconds_in:.1f}s into {self.said!r}"
                        f" - he did not hear {self.unsaid!r}")
        if self.dropped:
            bits.append(f"{len(self.dropped)} queued line(s) never played "
                        f"({self.seconds_cut:.1f}s)")
        return "; ".join(bits)


class Verdict:
    """Why a line was dropped. Truthy only when it is an echo."""

    __slots__ = ("is_echo", "score", "of", "lag", "source")

    def __init__(self, is_echo, score=0.0, of=None, lag=None, source=None):
        self.is_echo, self.score, self.of = is_echo, score, of
        self.lag, self.source = lag, source

    def __bool__(self):
        return self.is_echo

    def __str__(self):
        if not self.is_echo:
            return "not an echo"
        return (f"echo of {self.source} at {self.score:.2f}, "
                f"{self.lag:.1f}s after it finished playing: {self.of[:60]!r}")


def _epoch(stamp):
    """voicemode stamps UTC ISO-8601 with an offset."""
    try:
        return datetime.datetime.fromisoformat(stamp).timestamp()
    except (ValueError, TypeError):
        return None


class VoicemodeEvents:
    """Tail voicemode's own event log as a playout spool.

    Nothing is added to voicemode: it already records the text at synthesis
    and the timing at playback. Each utterance is registered TWICE - once at
    TTS_FIRST_AUDIO with the length ESTIMATED from the text, so the echo of
    a long clip's opening words is covered while it is still playing, and
    again at TTS_PLAYBACK_END with the true duration from the byte count."""

    CPS = 13.2          # kokoro, measured over 254 untruncated log lines

    def __init__(self, directory=VOICEMODE_EVENTS, clock=time.time,
                 from_start=False):
        self.dir, self.clock = directory, clock
        self.at, self.path = 0, None
        # Mark the end of today's file NOW - at construction, which is call
        # start - not at the first drain: audio that finished before we
        # opened cannot still be echoing, and replaying a whole day of it
        # would flood the window. Positioning at first drain instead would
        # skip everything said between the two, which is the whole call.
        self.from_start = from_start
        if not from_start:
            self.path = self._today()
            try:
                self.at = os.path.getsize(self.path)
            except OSError:
                self.at = 0
        self.pending = {}          # session_id -> (text, voice)
        self.started = {}          # session_id -> epoch of first audio

    def _today(self):
        d = datetime.datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
        return os.path.join(self.dir, f"voicemode_events_{d}.jsonl")

    def drain(self):
        """New Played records since the last call. Never blocks."""
        path = self._today()
        if path != self.path:          # midnight rollover: read the new one whole
            self.path, self.at = path, 0
        out = []
        try:
            if os.path.getsize(path) <= self.at:
                return out
            with open(path, "r", errors="replace") as fh:
                fh.seek(self.at)
                lines = fh.readlines()
                self.at = fh.tell()
        except OSError:
            return out
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            kind, sid = e.get("event_type"), e.get("session_id")
            data = e.get("data") or {}
            when = _epoch(e.get("timestamp"))
            if kind == "TTS_START":
                msg = data.get("message")
                if msg:
                    self.pending[sid] = (msg, data.get("voice") or "voicemode")
            elif kind == "TTS_FIRST_AUDIO" and sid in self.pending:
                text, voice = self.pending[sid]
                self.started[sid] = when
                out.append(Played(text, when, when + len(text) / self.CPS,
                                  f"voicemode/{voice}", key=sid))
            elif kind == "TTS_PLAYBACK_END" and sid in self.pending:
                text, voice = self.pending.pop(sid)
                start = self.started.pop(sid, when)
                m = data.get("metrics") or {}
                rate, n = m.get("sample_rate_hz"), m.get("bytes_received")
                dur = (n / (rate * 2.0)) if (rate and n) else (when - start)
                out.append(Played(text, when - max(0.0, dur), when,
                                  f"voicemode/{voice}", key=sid))
        return out


# A backchannel is not an interruption. Measured over the whole bridge log:
# of 184 lines that would trigger barge-in, 61 (a third) are under this many
# characters, and the list is what you would guess - "Mm.", "Ha!", "Oh!",
# "Um...", "Yeah.", "Okay.", "Bye." Each one would cut the agent off and
# throw away the ~14s of speech still queued behind it. Same number and the
# same reasoning as MIN_CHARS above, for the opposite job: a short utterance
# carries too little to judge as echo OR to act on as interruption.
BARGE_IN_MIN_CHARS = 18

# ...except the shortest utterances of all are the ones that mean it most.
# A length floor alone would ignore exactly the interruption barge-in exists for.
STOP_WORDS = (
    "stop", "wait", "hang on", "hold on", "pause", "enough", "shut up",
    "no no", "hey hey", "shush", "quiet", "cut it",
)


def interrupts(heard):
    """Is this a real interruption, or someone murmuring along?

    Length floor, with an explicit exemption for the words that are short
    BECAUSE they are urgent."""
    h = norm(heard)
    if not h:
        return False
    if any(h == w or h.startswith(w + " ") or h.endswith(" " + w) for w in STOP_WORDS):
        return True
    return len(h) >= BARGE_IN_MIN_CHARS


class EchoGuard:
    """Ask it before you believe a transcribed line came from a human.

        guard = EchoGuard()
        ...
        pcm = tts(text)
        track.say(pcm)
        guard.spoken(text, len(pcm) / OUT_RATE)      # duration, not clock
        ...
        v = guard.check(heard_text)
        if v:
            log(f"ECHO DROPPED: {v}")                # never silently
        else:
            record(heard_text)
    """

    def __init__(self, threshold=THRESHOLD, tail=TAIL, keep=KEEP, spool=SPOOL,
                 clock=time.time, log=None, backlog_warn=BACKLOG_WARN,
                 voicemode=VOICEMODE_EVENTS, near_window=NEAR_WINDOW,
                 near_threshold=NEAR_THRESHOLD):
        self.threshold, self.tail, self.clock = threshold, tail, clock
        self.near_window, self.near_threshold = near_window, near_threshold
        self.played = deque(maxlen=keep)
        self.spool, self._spool_at = spool, 0
        self.dropped = 0
        self.checked = 0
        self.log, self.backlog_warn = log, backlog_warn
        self.worst_backlog = 0.0
        self.voicemode = (VoicemodeEvents(voicemode, clock=self.clock)
                          if voicemode else None)

    # ---- what we played

    def spoken(self, text, duration, at=None, source="bridge"):
        """Record one clip we have just handed to the outgoing track.

        `duration` is the clip's length in SECONDS - for PCM that is
        len(pcm) / rate. Clips queue behind each other exactly as the track
        plays them, so a clip handed over while three are still playing
        becomes audible when those three have finished, not now."""
        now = self.clock() if at is None else at
        self._expire(now)
        ours = [p for p in self.played if p.source == source]
        start = max(now, ours[-1].end if ours else 0.0)
        p = Played(text, start, start + max(0.0, duration), source)
        self.played.append(p)
        queued = start - now                    # audio still to play ahead of it
        # from handing a line over to having finished saying it
        self.worst_backlog = max(self.worst_backlog, queued + max(0.0, duration))
        if self.log and queued >= self.backlog_warn:
            # Not a fix - a mirror. Four utterances handed over inside two
            # seconds carried 17.7s of audio on the recorded call, and by the
            # time the last one played he had asked two more questions.
            # Merging them in the bridge would HIDE that; the habit is to
            # think the whole thought before speaking. So: say the number
            # out loud and let whoever is talking see it.
            self.log(f"BACKLOG {queued:.1f}s of audio still to play before "
                     f"this line; it lands {queued:.1f}s late: {text[:50]}")
        return p

    def _remember(self, p):
        """Add an utterance, superseding an earlier report of the same one."""
        if p.key is not None:
            for i, old in enumerate(self.played):
                if old.key == p.key:
                    self.played[i] = p
                    return
        self.played.append(p)

    # Keep a little history past an utterance's own window: a merged chunk
    # arrives on the LAST line's timing, and to recognise it we still need
    # the one or two played just before, whose windows have closed.
    RUN_LOOKBACK = 20.0

    def _expire(self, now):
        """Drop anything that can no longer be echoing at us."""
        cut = now - self.tail - self.RUN_LOOKBACK
        while self.played and self.played[0].end < cut:
            self.played.popleft()

    def _drain_spool(self):
        """Pick up anything another process on this box declared it played."""
        if not self.spool:
            return
        try:
            size = os.path.getsize(self.spool)
            if size <= self._spool_at:
                if size < self._spool_at:       # truncated or rotated
                    self._spool_at = 0
                else:
                    return
            with open(self.spool, "r", errors="replace") as fh:
                fh.seek(self._spool_at)
                lines = fh.readlines()
                self._spool_at = fh.tell()
        except OSError:
            return
        for ln in lines:
            try:
                d = json.loads(ln)
                at, dur = float(d["at"]), float(d.get("dur", 0.0))
                text = d.get("text") or ""
            except (ValueError, KeyError, TypeError):
                continue
            if text:
                # another process's audio is not in OUR queue: it played
                # when it said it did
                self._remember(Played(text, at, at + dur,
                                      d.get("source", "other"), d.get("key")))

    # ---- what we heard

    def check(self, heard, at=None):
        """Is this transcribed line something we played? Never mutates."""
        self.checked += 1
        now = self.clock() if at is None else at
        self._drain_spool()
        if self.voicemode:
            for p in self.voicemode.drain():
                self._remember(p)
        # keep the ring in playout order, so expiry from the left is correct
        if len(self.played) > 1:
            self.played = deque(sorted(self.played, key=lambda p: p.end),
                                maxlen=self.played.maxlen)
        self._expire(now)
        h = norm(heard)
        if not h:
            return Verdict(False)
        best = Verdict(False)
        # Candidates: each utterance still in its window, AND runs of
        # CONSECUTIVE ones. Lines played back to back come back as ONE
        # transcribed chunk, and against either half alone the other half is
        # unexplained, so both score low and the echo survives. Measured live:
        # two of the agent's lines returned merged; 0.64 and
        # 0.56 apart, 0.92 together, and the 0.56 reached the caller's record as the caller's.
        # The playout clock already knows which clips ran back to back, so
        # the spans cost a second loop, not new machinery.
        played = list(self.played)
        live = [p for p in played
                if p.start - 1.0 <= now <= p.end + self.tail]
        cands = [(p.text, p) for p in live]
        # A run is anchored on an utterance that is still live and extends
        # BACKWARDS through the ones played immediately before it, whose own
        # windows may already have closed - the merged chunk arrives on the
        # last one's timing, not the first one's.
        for p in live:
            i = played.index(p)
            for n in (2, 3):
                if i - n + 1 < 0:
                    continue
                run = played[i - n + 1:i + 1]
                cands.append((" ".join(x.text for x in run), p))
        for text, p in cands:
            s = score(heard, text)
            if s > best.score:
                lag = max(0.0, now - p.end)
                bar = (self.near_threshold if lag <= self.near_window
                       else self.threshold)
                best = Verdict(s >= bar, s, text, lag, p.source)
        if best.is_echo:
            self.dropped += 1
        return best

    def cut(self, at=None):
        """Barge-in: he started talking, so we stopped. Say where.

        Also TRIMS the playout queue, which is not cosmetic: audio that
        never left the speaker can never echo, and leaving it here would
        hold the echo window open over words nobody heard - and the next
        thing arriving is, by definition, him talking."""
        now = self.clock() if at is None else at
        line, seconds_in, said, unsaid = None, 0.0, "", ""
        dropped, seconds_cut = [], 0.0
        for p in list(self.played):
            if p.end <= now:
                continue                       # already out of the speaker
            if p.start <= now:
                line, seconds_in = p, now - p.start
                said, unsaid = _split_at(p.text, seconds_in / max(p.end - p.start, 1e-9))
                seconds_cut += p.end - now
                p.end = now                    # only the played part can echo
                p.text = said                  # and only those words
            else:
                dropped.append(p.text)
                seconds_cut += p.end - p.start
                self.played.remove(p)
        return CutPoint(line, seconds_in, said, unsaid, dropped, seconds_cut)

    def backlog(self, at=None):
        """Seconds of audio still to come out of the speaker."""
        now = self.clock() if at is None else at
        return max(0.0, (self.played[-1].end - now) if self.played else 0.0)

    def stats(self):
        return {"checked": self.checked, "dropped": self.dropped,
                "held": len(self.played),
                "worst_backlog": round(self.worst_backlog, 1)}


def announce(text, duration, at=None, spool=SPOOL, source=None):
    """Tell every EchoGuard on this machine that you are about to speak.

    For anything that is NOT the call bridge - voicemode, a notifier, any
    agent with a voice. One append, no locking needed for single lines."""
    rec = {"at": float(at if at is not None else time.time()),
           "dur": float(duration), "text": text,
           "source": source or os.environ.get("CLAUDE_CODE_AGENT", "other")}
    try:
        os.makedirs(os.path.dirname(spool), exist_ok=True)
        with open(spool, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass

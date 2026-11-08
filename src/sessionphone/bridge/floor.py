"""Floor control: a reply does not start while the caller is speaking.

Before this, the bot voiced a reply the moment it reached the track, even in
the middle of the caller's sentence. With barge-in off, nothing stopped it.
Each time he talked over it, that made a new caller turn, which drew the next
reply, and that reply queued behind the one still playing. One measured call
heard "another one, another one" (a reply started while the caller
was saying "oh, stop").

A reply LINE (a dc-say, a mail into the call, a mouth line) now waits until
the caller has been quiet for GAP seconds, then plays. CAP bounds the wait,
so VAD noise (a fan, our own echo) can never silence the agent: after CAP
seconds held, the line plays anyway. The bridge's own lines (the greeting,
the holding line, the notices) are not gated.

The cap waits while his WORDS are still arriving. The energy VAD alone cannot
tell a caller who talks on from a fan, so the cap used to release a line over
a caller still mid-sentence. The signal that tells them apart is the call's
transcript: CallSession calls heard() each time real words of his reach it,
at the two places the transcript is fed: a transcribed chunk that is words
(not an echo, not "[COUGH]" or empty), and an ears final that is words. Past
the cap, a line is held while words arrived in the last WORDS seconds, and
released by the cap once none have: VAD speech with no words is noise.

The window and the chunk cadence. With the bridge's own chunk transcriber
(DC_EARS=0), words arrive only when a chunk closes: 0.35 s of quiet closes
one, transcribing it takes about 0.65 s, and speech with no such pause is cut
only at 12 s. So in ordinary speech words land every few seconds, well inside
WORDS (5 s), and the line waits. A caller who talks for more than about 4 s
without a 0.35 s pause sends no words for that long, which this feed cannot
tell from noise: the cap may then release the line before his chunk is
transcribed. That is the trade: a longer window would hold a line longer on
a noisy line after his last words. The very first words of a call arrive
only after its first chunk closes, so a hold that starts on noise before he
has said anything releases at the cap, as before. A track whose floor is
never fed words (no transcript) keeps the old rule: the cap alone.

This module only keeps the clock. The track (calls.PipVoiceTrack) asks it, on
every 20 ms frame, whether the line at the head of its queue may start. The
VAD state is read, never written: CallSession.speaking, which both the energy
VAD (_feed) and the ears leg keep.
"""
import time

GAP_DEFAULT = 0.8   # s of caller quiet before a reply starts: inside a
                    # thought he pauses <= 0.98 s (TURN_JOIN's measure), and a
                    # reply that waits for his whole turn would wait 1.6 s more
CAP_DEFAULT = 6.0   # s a reply is held by the VAD alone; past it, only his words hold it
WORDS_DEFAULT = 5.0  # s: words this recent hold a line past the cap. A chunk's
                     # words land ~1 s after it closes (0.35 s quiet + ~0.65 s
                     # transcribing), so ordinary speech lands words well inside it


class Floor:
    """Who has the floor, for the track's next line.

    speaking: a callable, -> whether the caller is speaking now (the VAD).
    gap, cap: seconds; gap <= 0 turns floor control off.
    clock: monotonic seconds; a test passes its own.
    words: seconds; his words this recent hold a line past the cap (heard())."""

    def __init__(self, speaking, gap=GAP_DEFAULT, cap=CAP_DEFAULT, clock=time.monotonic,
                 words=WORDS_DEFAULT):
        self._speaking = speaking
        self.gap, self.cap, self.clock = float(gap), float(cap), clock
        self.words = float(words)
        self.last_speech = None     # when the caller was last heard speaking
        self.last_words = None      # when his words last reached the transcript

    def heard(self):
        """Real words of his just reached the call's transcript (see the module
        docstring for the two places that call this)."""
        self.last_words = self.clock()

    def _talking(self, now):
        """His words arrived within the words window: he is talking, not noise."""
        return self.last_words is not None and now - self.last_words < self.words

    @property
    def on(self):
        return self.gap > 0

    def tick(self):
        """Read the VAD once. -> now. Called on every frame, so the quiet is
        measured from his last frame of speech, to 20 ms."""
        now = self.clock()
        if self._speaking():
            self.last_speech = now
        return now

    def free(self, now):
        """The caller is quiet and has been for gap seconds. Never spoken
        on this call counts as quiet."""
        if not self.on:
            return True
        if self._speaking():
            return False
        return self.last_speech is None or now - self.last_speech >= self.gap

    def verdict(self, held_since, now):
        """May a line held since HELD_SINCE (None: not held yet) start NOW?
        -> "quiet", "cap", or None (hold it). Past the cap, the line is still
        held while his words keep arriving: "cap" means the VAD reads speech
        and no words have come for the words window."""
        if self.free(now):
            return "quiet"
        if held_since is not None and now - held_since >= self.cap and not self._talking(now):
            return "cap"
        return None

    def reason(self, why, held_since, now):
        """WHY (a verdict) as the FLOOR release line says it."""
        if why == "cap":
            if self.last_words is None:
                return "cap (no words heard)"
            return f"cap (no words for {now - self.last_words:.1f}s)"
        if why == "quiet" and held_since is not None and now - held_since >= self.cap:
            return f"quiet (his words held it past the {self.cap:g}s cap)"
        return why

    def line(self):
        """The start-up line for the log."""
        if not self.on:
            return "floor control: off (a reply may start while the caller speaks)"
        return (f"floor control: a reply waits for {self.gap:g}s of caller quiet; after {self.cap:g}s "
                f"it plays anyway, unless his words reached the transcript in the last {self.words:g}s")

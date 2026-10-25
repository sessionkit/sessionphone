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

This module only keeps the clock. The track (calls.PipVoiceTrack) asks it, on
every 20 ms frame, whether the line at the head of its queue may start. The
VAD state is read, never written: CallSession.speaking, which both the energy
VAD (_feed) and the ears leg keep.
"""
import time

GAP_DEFAULT = 0.8   # s of caller quiet before a reply starts: inside a
                    # thought he pauses <= 0.98 s (TURN_JOIN's measure), and a
                    # reply that waits for his whole turn would wait 1.6 s more
CAP_DEFAULT = 6.0   # s a reply may be held, at most


class Floor:
    """Who has the floor, for the track's next line.

    speaking: a callable, -> whether the caller is speaking now (the VAD).
    gap, cap: seconds; gap <= 0 turns floor control off.
    clock: monotonic seconds; a test passes its own."""

    def __init__(self, speaking, gap=GAP_DEFAULT, cap=CAP_DEFAULT, clock=time.monotonic):
        self._speaking = speaking
        self.gap, self.cap, self.clock = float(gap), float(cap), clock
        self.last_speech = None     # when the caller was last heard speaking

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
        -> "quiet", "cap", or None (hold it)."""
        if self.free(now):
            return "quiet"
        if held_since is not None and now - held_since >= self.cap:
            return "cap"
        return None

    def line(self):
        """The start-up line for the log."""
        if not self.on:
            return "floor control: off (a reply may start while the caller speaks)"
        return f"floor control: a reply waits for {self.gap:g}s of caller quiet, {self.cap:g}s at most"

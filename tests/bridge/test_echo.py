#!/usr/bin/env python3
"""The echo guard's cases, as a test.

    python3 test_echo.py            # stdlib only, no pytest

Every case below is an invented line in the shape the bridge meets on a
call: its own words coming back mangled by transcription, or a person
talking over them. If a future change makes one of these fail, the change
is wrong, or the matcher's contract has moved - read echo.py before you
edit the expectation.
"""
import datetime
import json
import os
import sys
import tempfile
import unittest

from sessionphone.bridge.echo import EchoGuard, announce, score, interrupts

CPS = 13.2          # kokoro's measured rate, characters per second

# --- spoken by the bridge -> what transcription handed back, as the caller
ECHOES = [
    ("And git over Delta is already solved here, neither of us remember.",
     "and get over Delta is already solved here. Neither of us remember."),
    ("We have done it four times, in the care packs for Ben and Ada.",
     "We have done it four times."),
    ("We have done it four times, in the care packs for Ben and Ada.",
     "in the care plaques for Ben and--"),
    ("One file, no accounts, no access to set up, and the far end just opened it.",
     "One file, no accounts, no access to setup, and the bar game just closed it."),
    ("Carol is started and has the echo fix, with the warning that our two "
     "bridges have already diverged.",
     "with the warning that our two bridges have already diverged ."),
    ("And Sam cleared something up: the bot is the robot.",
     "Engine cleared something up."),
    ("I went to add the new reviewer to the list, and she is "
     "already in the Kith regulars.",
     "and she is already in a kid's regards."),
]

# --- the caller, really talking, in the middle of an echo run
REAL_DURING_ECHO = [
    ("One file, no accounts, no access to set up, and the far end just opened it.",
     "Git bundle create and send the file into the chat."),
    ("Note written and committed.",
     "Okay, so could you please create a note called Delta in the notebook?"),
    ("And Sam cleared something up: the bot is the robot.",
     "I need to pick up some milk later."),
]

# --- short human answers landing right after we used the same word. The
# first, symmetric version of the matcher dropped all of these: 27 false
# positives in 823 human lines, every one this shape.
SHORT_ANSWERS = [
    ("Bye Sam.", "Bye."),
    ("On a scale of one to delighted, how is your morning going?", "- Delighted."),
    ("The variable is EMAIL, in capitals.", "Email."),
    ("Last quick one: tea in the flask, yes or no?", "No."),
    ("Yes?", "Yes, please. Yep, that'd be great."),
    ("Noted, the plant needs a bigger pot.", "No."),
]


def guard(**kw):
    kw.setdefault("spool", None)        # no machine-wide spool in unit tests
    kw.setdefault("voicemode", None)    # nor the live voicemode event log
    return EchoGuard(**kw)


class Ticking:
    """A clock you set by hand."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestEchoIsCaught(unittest.TestCase):
    def test_each(self):
        for said, heard in ECHOES:
            with self.subTest(heard=heard[:40]):
                clock = Ticking()
                g = guard(clock=clock)
                g.spoken(said, duration=len(said) / CPS)
                clock.t += len(said) / CPS + 1.0          # just after playout
                v = g.check(heard)
                self.assertTrue(v, f"missed at {score(heard, said):.2f}")
                self.assertGreaterEqual(v.score, 0.80)

    def test_the_one_we_knowingly_miss(self):
        """"Note written and committed." came back as "No commitment
        committed." - 24 characters, mangled past recognition. Kept as a
        test so the limit stays visible instead of being rediscovered."""
        clock = Ticking()
        g = guard(clock=clock)
        g.spoken("Note written and committed.", duration=2.0)
        clock.t += 2.5
        self.assertFalse(g.check("No commitment committed."))


class TestRealSpeechSurvives(unittest.TestCase):
    def test_mid_echo_run(self):
        for said, heard in REAL_DURING_ECHO:
            with self.subTest(heard=heard[:40]):
                clock = Ticking()
                g = guard(clock=clock)
                g.spoken(said, duration=len(said) / CPS)
                clock.t += 0.5
                self.assertFalse(g.check(heard), "dropped something the caller said")

    def test_short_answers(self):
        for said, heard in SHORT_ANSWERS:
            with self.subTest(heard=heard):
                clock = Ticking()
                g = guard(clock=clock)
                g.spoken(said, duration=len(said) / CPS)
                clock.t += 0.5
                self.assertFalse(g.check(heard), "a short answer is not an echo")


class TestPlayoutClock(unittest.TestCase):
    def test_queue_not_log_time(self):
        """Four clips handed over inside two seconds carried 17.7s of audio
        on the recorded call, and the last one's echo arrived sixteen seconds
        after its log line. The window has to follow the speaker, not the
        log."""
        clock = Ticking()
        g = guard(clock=clock)
        for text, dur in [("one", 2.0), ("two", 7.2), ("three", 3.8),
                          ("the fourth thing we said", 4.7)]:
            g.spoken(text, dur)
            clock.t += 0.5                               # enqueued in a burst
        last = g.played[-1]
        self.assertAlmostEqual(last.start, 1000.0 + 2.0 + 7.2 + 3.8, places=3)
        self.assertAlmostEqual(last.end, 1000.0 + 17.7, places=3)

    def test_window_closes(self):
        said = ("One file, no accounts, no access to set up, and the far end "
                "just opened it.")
        heard = ("One file, no accounts, no access to setup, and the bar game "
                 "just closed it.")
        clock = Ticking()
        g = guard(clock=clock)
        g.spoken(said, duration=6.0)
        clock.t = 1007.0                                 # inside 6s + 3s tail
        self.assertTrue(g.check(heard))
        clock.t = 1060.0                                 # a minute on: he means it
        self.assertFalse(g.check(heard))


class TestMachineWide(unittest.TestCase):
    def test_another_agents_voice(self):
        """The loop catches ANY voice on this box, not just the bridge's.
        Say a second agent's voicemode greeting comes back through the
        call labelled as the caller."""
        path = os.path.join(tempfile.mkdtemp(), "playout.jsonl")
        said = ("I'm here at the desk with Dan and Eve and Alice's got the "
                "call. I'm CC'd on the build thread, so throw me any ask and "
                "I'll take it.")
        heard = ("I'm here at the desk with Dan and Eve and Alice's got the "
                 "call. I'm CC'd on the build-thru, so throw me any ask and "
                 "I'll take it.")
        announce(said, duration=9.5, at=1000.0, spool=path, source="carol/voicemode")
        g = EchoGuard(spool=path, clock=Ticking(1010.0))
        v = g.check(heard)
        self.assertTrue(v)
        self.assertEqual(v.source, "carol/voicemode")


class TestBacklogMirror(unittest.TestCase):
    """The stacking is not the bridge queueing, it is an agent sending,
    thinking of more, and sending again. So do not merge the lines - that
    hides it. Report the number, so the habit shows."""

    def test_warns_once_the_queue_is_deep(self):
        said = []
        clock = Ticking()
        g = guard(clock=clock, log=said.append)
        for text, dur in [("one", 2.0), ("two", 7.2), ("three", 3.8),
                          ("the fourth thing", 4.7)]:
            g.spoken(text, dur)
            clock.t += 0.5
        warns = [m for m in said if m.startswith("BACKLOG")]
        self.assertEqual(len(warns), 2, said)          # clips three and four
        # the fourth line was handed over 1.5s in, behind 13.0s of audio
        self.assertIn("11.5s", warns[-1])
        # handed over 1.5s in, behind 13.0s, itself 4.7s long: 16.2s
        # from opening her mouth to having finished saying it
        self.assertEqual(g.stats()["worst_backlog"], 16.2)

    def test_silent_when_lines_are_spaced(self):
        said = []
        clock = Ticking()
        g = guard(clock=clock, log=said.append)
        for _ in range(4):
            g.spoken("a sentence at a time", 2.0)
            clock.t += 3.0
        self.assertEqual([m for m in said if m.startswith("BACKLOG")], [])

    def test_backlog_is_readable_for_barge_in(self):
        """Barge-in (ASKS 110) needs 'how far in was I cut'. Same clock."""
        clock = Ticking()
        g = guard(clock=clock)
        g.spoken("a long one", 10.0)
        clock.t += 4.0
        self.assertAlmostEqual(g.backlog(), 6.0, places=3)


class TestBargeIn(unittest.TestCase):
    """ASKS 110, two asks in one breath: stop the audio, AND say where."""

    def setUp(self):
        self.clock = Ticking()
        self.g = guard(clock=self.clock)
        self.g.spoken("The bodies exist from the turntable, but H3 renders "
                      "take minutes each.", 6.0)
        self.g.spoken("And the weekly digest for the Kith group chat is not "
                      "written yet.", 5.0)

    def test_says_where_it_was_cut(self):
        self.clock.t += 2.0
        cut = self.g.cut()
        self.assertTrue(cut)
        self.assertAlmostEqual(cut.seconds_in, 2.0, places=3)
        self.assertTrue(cut.said)
        self.assertTrue(cut.unsaid)
        self.assertNotIn(cut.unsaid.split()[0], cut.said.split())
        self.assertEqual(len(cut.dropped), 1)       # the queued line never played
        self.assertAlmostEqual(cut.seconds_cut, 4.0 + 5.0, places=3)

    def test_the_echo_window_shrinks_to_what_he_actually_heard(self):
        """Not cosmetic: audio that never left the speaker can never echo,
        and the very next thing arriving is him talking. Leaving the cut
        words in the window risks dropping HIS speech as our echo."""
        self.clock.t += 2.0
        cut = self.g.cut()
        self.assertEqual(self.g.played[-1].text, cut.said)
        self.assertEqual(self.g.backlog(), 0.0)
        self.clock.t += 0.5
        # the words he never heard must no longer be able to suppress him
        self.assertFalse(self.g.check("the turntable, but H3 renders take "
                                      "minutes each"))

    def test_cutting_when_silent_says_so(self):
        self.clock.t += 60.0
        cut = self.g.cut()
        self.assertFalse(cut)
        self.assertIn("nothing was playing", str(cut))


class TestVoicemodeEvents(unittest.TestCase):
    # NOTE the shape of these: the guard opens the log EMPTY and voicemode
    # speaks afterwards, which is what happens on a live call. The guard
    # deliberately seeks to end of file when it opens - audio that finished
    # before the call started cannot still be echoing, and replaying a whole
    # day of it would flood the window.
    """Another agent's voice, replayed from an invented event log.

    Say a second agent on the same machine greets the caller by voice. It
    goes out the speakers, through the call, and comes back into the
    bridge as a line labelled as the caller. Voicemode already logs the
    text at synthesis and the timing at playback, so nothing had to be
    added to it."""

    GREETING = ("Carol's up. I can see you're at the desk with Dan and Eve, "
                "and Alice's got the call. I'm Cc'd on the build tree, so "
                "throw me any ask and I'll take it.")
    # as a transcriber might hear it off the call's microphone
    AS_HEARD = ("I'm here at the desk with Dan and Eve and Alice's got the "
                "call. I'm CC'd on the build-thru, so throw me any ask and "
                "I'll take it.")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "voicemode_events_2030-06-09.jsonl")
        open(self.path, "w").close()          # the guard opens it EMPTY, as in
        self.heard_at = datetime.datetime.fromisoformat(  # a live call
            "2030-06-09T10:00:14+00:00").timestamp()

    def _speak(self):
        """voicemode plays something WHILE the guard is already watching."""
        sid = "conv_1893664800700"
        rows = [
            {"timestamp": "2030-06-09T10:00:00.700000+00:00",
             "event_type": "TTS_START", "session_id": sid,
             "data": {"message": self.GREETING, "voice": "carol"}},
            {"timestamp": "2030-06-09T10:00:04.200000+00:00",
             "event_type": "TTS_FIRST_AUDIO", "session_id": sid, "data": {}},
            {"timestamp": "2030-06-09T10:00:13.700000+00:00",
             "event_type": "TTS_PLAYBACK_END", "session_id": sid,
             "data": {"metrics": {"bytes_received": 449280, "format": "pcm",
                                  "sample_rate_hz": 24000}}},
        ]
        with open(self.path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def _guard(self, at):
        return EchoGuard(spool=None, voicemode=self.dir, clock=lambda: at)

    def test_another_agents_greeting_is_caught(self):
        g = self._guard(self.heard_at)
        self._speak()
        v = g.check(self.AS_HEARD)
        self.assertTrue(v, "the greeting would still poison the record")
        self.assertEqual(v.source, "voicemode/carol")
        self.assertLess(v.lag, 2.0)

    def test_duration_comes_from_the_byte_count(self):
        g = self._guard(self.heard_at)
        self._speak()
        g.check("anything")                       # drains the log
        played = [p for p in g.played if p.source == "voicemode/carol"]
        self.assertTrue(played)
        # 449280 bytes / (24000 Hz * 2 bytes) = 9.36s
        self.assertAlmostEqual(played[-1].end - played[-1].start, 9.36, places=2)

    def test_the_caller_answering_it_is_not_suppressed(self):
        g = self._guard(self.heard_at)
        self._speak()
        self.assertFalse(g.check(
            "Okay, so you're using voice mode but we're actually on a "
            "Delta call in the kitchen with Dan and Eve cooking dinner."))


class TestInterrupts(unittest.TestCase):
    """A backchannel is not an interruption - but "Stop." is.

    Lines of the shape the bridge log shows. Of 184 that would trigger
    barge-in, 61 are under the floor and every one of them is someone
    murmuring along."""

    BACKCHANNEL = ["Mm.", "Ha!", "Oh!", "Um...", "Yeah.", "Okay.", "Yes.",
                   "Bye.", "Amen.", "Thank you.", "But I can--"]
    REAL = ["Stop.", "Wait.", "hang on", "Hold on!", "Stop stop stop",
            "No no no", "Sorry, can I jump in there for a second?",
            "Actually that is not what I meant at all"]

    def test_murmurs_do_not_cut(self):
        for t in self.BACKCHANNEL:
            with self.subTest(t=t):
                self.assertFalse(interrupts(t))

    def test_short_and_urgent_still_cuts(self):
        for t in self.REAL:
            with self.subTest(t=t):
                self.assertTrue(interrupts(t))

    def test_the_one_that_cost_us_13_seconds(self):
        """Live: "Thank you." cut the agent off 1.2s into a sentence
        and threw away 13.6s of queued speech. Correct by the old rule."""
        self.assertFalse(interrupts("Thank you."))


class TestTimeIsEvidence(unittest.TestCase):
    """A line arriving a moment after our audio stopped is judged on a lower
    bar than the same line arriving later. Added after the guard let two of
    the agent's own lines through at 0.79 and 0.69."""

    SAID = ("You're right that WebRTC has echo cancellation, and it was "
            "working - that's why four days of calls had no echo at all.")
    HEARD = "I thought what opportunity has echo cancellation and it was working."

    def test_the_0_79_that_got_through(self):
        clock = Ticking()
        g = guard(clock=clock)
        g.spoken(self.SAID, 5.8)
        clock.t += 5.8 + 0.4                      # just after playout ended
        v = g.check(self.HEARD)
        self.assertTrue(v, "the live miss still gets through")
        self.assertLess(v.score, 0.80, "this is below the normal bar, on purpose")

    def test_the_same_line_later_is_left_alone(self):
        clock = Ticking()
        g = guard(clock=clock)
        g.spoken(self.SAID, 5.8)
        clock.t += 5.8 + 40                        # he may simply be agreeing
        self.assertFalse(g.check(self.HEARD))

    def test_the_window_does_not_lower_the_bar_for_everything(self):
        """0.55 is still 0.55 - the window relaxes the bar, it does not
        remove it, or a short answer right after we speak would vanish."""
        clock = Ticking()
        g = guard(clock=clock)
        g.spoken("Last quick one: tea in the flask, yes or no?", 3.0)
        clock.t += 3.2
        self.assertFalse(g.check("No, I already have one."))


class TestVerdict(unittest.TestCase):
    def test_advisory_and_counted(self):
        clock = Ticking()
        g = guard(clock=clock)
        g.spoken("We have done it four times, in the care packs for Ben "
                 "and Ada.", duration=5.0)
        clock.t = 1003.0
        self.assertTrue(g.check("We have done it four times."))
        self.assertFalse(g.check("Can you send me the link?"))
        self.assertEqual(g.stats(), {"checked": 2, "dropped": 1, "held": 1,
                                     "worst_backlog": 5.0})

    def test_reads_as_a_log_line(self):
        clock = Ticking()
        g = guard(clock=clock)
        g.spoken("We have done it four times, in the care packs for Ben "
                 "and Ada.", duration=5.0)
        clock.t = 1006.0
        self.assertIn("echo of bridge at", str(g.check("We have done it four times.")))


if __name__ == "__main__":
    unittest.main(verbosity=2)

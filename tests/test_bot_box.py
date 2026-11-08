"""The bot's own box, the one the mail leg reads (calls.bot_box): sessionmail
says where it is. The bridge never spells the mail root (sessionmail 0.3.3
moved it, and the box folder with it).

1. $DC_BOT_BOX, when set, is the box, and sessionmail is not asked;
2. else `message path <agent>-chat` (seatmail.box_path, read-only) names it;
   a box found is kept for the run, a no is asked again after
   BOT_BOX_RECHECK_S and said once;
3. no box: the mail leg reads nothing, and makes nothing;
4. seen() moves a mail to cur/ in the box it was read from, making cur/ when
   it is missing, never the box above it.

Fakes only: seatmail.box_path is replaced, the clock is set by hand.
"""
import pytest

from sessionphone.bridge import calls, dc_env, seatmail


@pytest.fixture
def fresh(monkeypatch):
    """No test override, no DC_BOT_BOX, nothing asked yet; log lines kept."""
    monkeypatch.setattr(calls, "BOT_BOX", None)
    monkeypatch.setattr(calls, "_bot_box_asked", None)
    for k in ("DC_BOT_BOX", "PIP_CHAT_BOT_BOX"):
        monkeypatch.delenv(k, raising=False)
    said = []
    monkeypatch.setattr(calls, "log", said.append)
    return said


def _ask(monkeypatch, answer):
    asked = []

    def box_path(name, environ=None):
        asked.append(name)
        return answer(name)
    monkeypatch.setattr(seatmail, "box_path", box_path)
    return asked


def _clock(monkeypatch, start=1000.0):
    now = [start]
    monkeypatch.setattr(calls.time, "monotonic", lambda: now[0])
    return now


def test_dc_bot_box_is_the_box_and_sessionmail_is_not_asked(fresh, monkeypatch, tmp_path):
    asked = _ask(monkeypatch, lambda name: tmp_path / "asked")
    monkeypatch.setenv("DC_BOT_BOX", str(tmp_path / "given"))
    assert calls.bot_box() == tmp_path / "given"
    assert asked == []


def test_a_test_override_wins_over_everything(fresh, monkeypatch, tmp_path):
    asked = _ask(monkeypatch, lambda name: tmp_path / "asked")
    monkeypatch.setenv("DC_BOT_BOX", str(tmp_path / "given"))
    monkeypatch.setattr(calls, "BOT_BOX", tmp_path / "override")
    assert calls.bot_box() == tmp_path / "override"
    assert asked == []


def test_else_sessionmail_names_it_and_a_box_found_is_kept(fresh, monkeypatch, tmp_path):
    asked = _ask(monkeypatch, lambda name: tmp_path / "stores" / name)
    now = _clock(monkeypatch)
    name = f"{dc_env.AGENT}-chat"
    assert calls.bot_box() == tmp_path / "stores" / name
    now[0] += 10 * calls.BOT_BOX_RECHECK_S
    assert calls.bot_box() == tmp_path / "stores" / name
    assert asked == [name]                       # asked once, kept for the run
    assert fresh == []


def test_no_box_reads_nothing_says_once_and_asks_again_later(fresh, monkeypatch, tmp_path):
    there = []
    asked = _ask(monkeypatch, lambda name: there[0] if there else None)
    now = _clock(monkeypatch)
    leg = object.__new__(calls.MailLeg)
    assert leg.candidates() == []
    assert leg.candidates() == []                # inside the window: not asked again
    assert len(asked) == 1
    assert len(fresh) == 1 and f"no box `{dc_env.AGENT}-chat`" in fresh[0]
    now[0] += calls.BOT_BOX_RECHECK_S + 1
    assert leg.candidates() == []                # asked again, still none: not said again
    assert len(asked) == 2 and len(fresh) == 1
    box = tmp_path / "box"
    (box / "new").mkdir(parents=True)
    (box / "new" / "1.mail").write_text("From: x@y\n\nhi\n")
    there.append(box)
    now[0] += calls.BOT_BOX_RECHECK_S + 1
    assert [f.name for f in leg.candidates()] == ["1.mail"]
    assert not list(tmp_path.glob("**/cur"))    # reading made nothing


def test_seen_moves_to_cur_in_the_box_it_was_read_from(fresh, tmp_path):
    box = tmp_path / "box"
    (box / "new").mkdir(parents=True)
    f = box / "new" / "1.mail"
    f.write_text("From: x@y\n\nhi\n")
    object.__new__(calls.MailLeg).seen(f)
    assert [p.name for p in (box / "cur").iterdir()] == ["1.mail:2,S"]


def test_seen_never_makes_the_box(fresh, tmp_path):
    """The box went away under the leg: no tree is made in its place."""
    f = tmp_path / "gone" / "new" / "1.mail"
    object.__new__(calls.MailLeg).seen(f)
    assert not (tmp_path / "gone").exists()
    assert len(fresh) == 1 and "could not mark 1.mail read" in fresh[0]

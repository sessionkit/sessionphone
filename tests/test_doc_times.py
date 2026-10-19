"""The shipped docs' example times give away no time zone.

README.md and the skill ship to every user. An example line copied from a real
record carries the writer's UTC offset and working hours. So every clock time
in them is UTC (`+0000`, `+00:00` or `Z`) or carries no offset at all. The
README's two JSON line examples still parse with the format the bot writes
their `ts` in, `%Y-%m-%dT%H:%M:%S%z`.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", *sorted((ROOT / "skills").rglob("*.md"))]

# A clock time after a date, with whatever offset follows it.
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
UTC = {None, "Z", "+0000", "+00:00", "-0000", "-00:00"}


def test_no_doc_example_time_carries_a_non_utc_offset():
    bad = [f"{p.relative_to(ROOT)}:{n}: {m.group(0)}"
           for p in DOCS
           for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
           for m in STAMP.finditer(line) if m.group(1) not in UTC]
    assert not bad, "\n".join(bad)


def test_the_stamp_check_catches_an_offset():
    for text in ("2030-01-03T12:00:00+0530", "2030-01-03T12:00+09:00", "2030-01-03 12:00:00-05:00"):
        assert STAMP.search(text).group(1) not in UTC, text
    for text in ("2030-01-03T12:00:00+0000", "2030-01-03T12:00:00Z", "2030-01-03 12:00"):
        assert STAMP.search(text).group(1) in UTC, text


def test_the_readme_json_line_examples_keep_the_bot_ts_format():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in readme.splitlines() if line.startswith('{"ts": ')]
    assert len(lines) >= 2
    for rec in lines:
        ts = datetime.datetime.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S%z")
        assert ts.utcoffset() == datetime.timedelta(0), rec["ts"]
        assert isinstance(rec["msg_id"], int)


def test_the_anchor_is_a_synthetic_sunday_either_side_of_melbourne_dst(anchor, anchor_summer):
    """tests/conftest.py's ANCHOR (PHONE-187): the one time a lone typed date or clock becomes."""
    from zoneinfo import ZoneInfo
    mel = ZoneInfo("Australia/Melbourne")
    assert anchor == datetime.datetime(2030, 6, 9, 12, 34, 56, tzinfo=datetime.UTC)
    assert anchor_summer == datetime.datetime(2030, 12, 8, 12, 34, 56, tzinfo=datetime.UTC)
    assert anchor.weekday() == anchor_summer.weekday() == 6                        # Sundays
    assert anchor.astimezone(mel).utcoffset() == datetime.timedelta(hours=10)      # no DST
    assert anchor_summer.astimezone(mel).utcoffset() == datetime.timedelta(hours=11)  # DST

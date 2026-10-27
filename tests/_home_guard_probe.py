"""Run only by tests/test_home_guard.py, in a child pytest whose HOME is a FAKE home.

Not a test_ file, so the suite never collects it; the child names it on its command
line. With HOME_GUARD_PROBE_WRITE set, its one test writes that path, as a test that
bound a path under the home before conftest moved HOME would. The child's conftest
took the fake home for the real one when it loaded, so its end-of-run check must
fail the run: the guard firing, proved without ever touching the real home.
"""
import os
import pathlib


def test_writes_where_it_is_told():
    target = os.environ.get("HOME_GUARD_PROBE_WRITE")
    if target:
        p = pathlib.Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("52484\n")

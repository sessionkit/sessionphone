"""Setup's install is the light four; the media pack is an extra (PHONE-10).

A stranger's first `sessionphone setup` used to fetch every dependency, PyAV
and numpy included, before the profile and the QR (it died on a patchy link,
9 Jun). The heavy pins now sit in a `media` extra. These tests read
pyproject.toml and uv.lock with tomllib: nothing is installed or resolved.

The base closure is an ALLOWLIST, walked through every marker (so it holds on
every platform): a new base dependency, direct or transitive, is a conscious
change to this file, not a quiet growth of setup's first download.
"""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What setup, qr and link need, and nothing else (design.md section 1).
LIGHT = {"click", "deltachat-rpc-client", "deltachat-rpc-server", "segno"}
# Markered transitive deps the light closure may carry, each named with its
# marker. None today: click 8.5 brings no colorama.
LIGHT_MARKERED: dict[str, str] = {}
# The six pins that moved out of [project].dependencies.
MEDIA = {"aiortc", "aioice", "av", "numpy", "websockets", "cryptography"}


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _lock() -> dict:
    return tomllib.loads((ROOT / "uv.lock").read_text())


def _name(requirement: str) -> str:
    m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert m, requirement
    return m.group(0).lower().replace("_", "-")


def _packages(lock: dict) -> dict[str, list[dict]]:
    by_name: dict[str, list[dict]] = {}
    for p in lock["package"]:
        by_name.setdefault(p["name"], []).append(p)
    return by_name


def _closure(lock: dict, edges: list[dict]) -> tuple[set[str], dict[str, str]]:
    """Every package reachable from `edges`, through every marker and extra.

    Returns the names, and {name: marker} for each name reached only by
    markered edges (a platform- or Python-conditional dep).
    """
    by_name = _packages(lock)
    # (name, version, extras followed) -> reached unconditionally? A node
    # reached first by a markered edge is walked again if an unmarkered one
    # reaches it later, so its children are not left labelled conditional.
    seen: dict[tuple[str, str, tuple[str, ...]], bool] = {}
    unconditional: set[str] = set()
    markers: dict[str, str] = {}
    todo = [(e, e.get("marker") is None) for e in edges]
    while todo:
        edge, always = todo.pop()
        name = edge["name"]
        always = always and edge.get("marker") is None
        if always:
            unconditional.add(name)
        else:
            markers.setdefault(name, edge.get("marker", "(via a markered parent)"))
        candidates = by_name[name]
        if "version" in edge:
            candidates = [p for p in candidates if p["version"] == edge["version"]]
        assert candidates, f"uv.lock has no package for edge {edge}"
        for pkg in candidates:
            extras = tuple(sorted(edge.get("extra", [])))
            key = (pkg["name"], pkg["version"], extras)
            if key in seen and (seen[key] or not always):
                continue
            seen[key] = always
            nexts = list(pkg.get("dependencies", []))
            for extra in extras:
                nexts += pkg.get("optional-dependencies", {}).get(extra, [])
            todo += [(n, always) for n in nexts]
    names = {n for n, _, _ in seen}
    return names, {n: m for n, m in markers.items() if n not in unconditional}


def _sessionphone(lock: dict) -> dict:
    [pkg] = _packages(lock)["sessionphone"]
    return pkg


# ---- pyproject.toml ---------------------------------------------------------

def test_pyproject_base_is_the_light_four():
    deps = _pyproject()["project"]["dependencies"]
    assert {_name(d) for d in deps} == LIGHT
    assert len(deps) == len(LIGHT)


def test_pyproject_media_extra_is_the_six():
    extras = _pyproject()["project"]["optional-dependencies"]
    assert set(extras) == {"media"}
    assert {_name(d) for d in extras["media"]} == MEDIA
    assert len(extras["media"]) == len(MEDIA)


def test_pyproject_dev_group_carries_the_media_pack():
    # make test, uv sync and bin/bridge-check keep the whole stack
    dev = _pyproject()["dependency-groups"]["dev"]
    assert "sessionphone[media]" in dev


# ---- uv.lock ------------------------------------------------------------------

def test_lock_base_closure_is_an_allowlist():
    names, markered = _closure(_lock(), _sessionphone(_lock())["dependencies"])
    assert names == LIGHT | set(LIGHT_MARKERED), (
        f"setup's install grew: {sorted(names - LIGHT - set(LIGHT_MARKERED))}; "
        f"lost: {sorted(LIGHT - names)}"
    )
    assert markered == LIGHT_MARKERED


def test_lock_base_closure_has_no_heavy_package():
    names, _ = _closure(_lock(), _sessionphone(_lock())["dependencies"])
    assert not names & {"av", "numpy", "aiortc", "aioice", "cryptography", "websockets", "cffi"}


def test_lock_media_closure_carries_the_stack():
    sp = _sessionphone(_lock())
    names, _ = _closure(_lock(), sp["optional-dependencies"]["media"])
    assert {"av", "numpy", "aiortc"} <= names
    assert MEDIA <= names


def test_lock_dev_group_reaches_the_media_pack():
    lock = _lock()
    dev = _sessionphone(lock)["dev-dependencies"]["dev"]
    assert {"name": "sessionphone", "extra": ["media"]} in dev
    names, _ = _closure(lock, dev)
    assert MEDIA <= names


def test_lock_matches_pyproject():
    # a stale lock (pyproject edited, `uv lock` not re-run) fails here
    meta = _sessionphone(_lock())["metadata"]
    assert meta["provides-extras"] == ["media"]
    base = {r["name"] for r in meta["requires-dist"] if "marker" not in r}
    media = {r["name"] for r in meta["requires-dist"] if r.get("marker") == "extra == 'media'"}
    assert base == LIGHT
    assert media == MEDIA
    assert len(meta["requires-dist"]) == len(LIGHT) + len(MEDIA)


def test_closure_walk_follows_markers_and_extras():
    # the walker itself: a markered edge is named, an extra is followed
    lock = {"package": [
        {"name": "a", "version": "1", "dependencies": [{"name": "b", "marker": "sys_platform == 'win32'"}],
         "optional-dependencies": {"x": [{"name": "c"}]}},
        {"name": "b", "version": "1", "dependencies": [{"name": "d"}]},
        {"name": "c", "version": "1"},
        {"name": "d", "version": "1"},
    ]}
    names, markered = _closure(lock, [{"name": "a"}])
    assert names == {"a", "b", "d"}
    assert markered == {"b": "sys_platform == 'win32'", "d": "(via a markered parent)"}
    names, _ = _closure(lock, [{"name": "a", "extra": ["x"]}])
    assert names == {"a", "b", "c", "d"}
    # reached conditionally first, then unconditionally: d is unconditional
    names, markered = _closure(lock, [{"name": "b"}, {"name": "a"}])
    assert markered == {}
    # a second version of a name is walked only when the edge names it
    lock["package"].append({"name": "d", "version": "2", "dependencies": [{"name": "c"}]})
    names, _ = _closure(lock, [{"name": "d", "version": "1"}])
    assert names == {"d"}


# ---- Makefile -----------------------------------------------------------------

def test_make_install_asks_for_the_media_pack():
    # `uv tool install` replaces the tool env: a bare `-e .` would strip the
    # pack from under a bot that runs from it
    recipe = re.search(r"^install:.*\n((?:\t.*\n)+)", (ROOT / "Makefile").read_text(), re.MULTILINE)
    assert recipe, "Makefile has no install target"
    assert "uv tool install -e '.[media]'" in recipe.group(1)

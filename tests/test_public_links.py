"""A shipped file links to this repo relatively, and names no branch in a URL.

The docs must open where the repo is: in an editor, offline, from a clone or
an unpacked archive. So a shipped file never links to this repo by a full
GitHub blob, tree or raw URL. It uses a relative link, and every relative
link in a shipped Markdown file resolves, from that file's own directory, to
a file in the export. A link with an anchor also names a heading in that
file, by GitHub's anchor rule.

A link to another sessionkit repo stays a full URL, and its ref is `HEAD` or
a `v*` release tag, never a branch name. GitHub resolves `HEAD` to the repo's
default branch, whatever it is named, so the link does not break when a branch
is renamed; a link on a branch the repo lacks is a 404. No shipped file tells
a clone to check out main either.

The gate reads the exported tree: `git archive` of HEAD with the
working-tree `.gitattributes`, so export-ignored paths (CLAUDE.md, design/)
are out, as in test_export_ignore.py. It also reads the working-tree copy of
each exported file, so an edit fails before it is committed. Outside a git
checkout (an sdist, or an unpacked archive) the tree on disk is the export.

The URLs these tests plant are built from parts, so this file passes its own
gate.
"""
import io
import posixpath
import re
import subprocess
import tarfile

import pytest
import test_config_docs as config_docs  # its _slug: GitHub's anchor for a heading
import test_export_ignore as export
import test_pypi_readme as pypi_readme  # its link_targets: the prose's links, code left out

ROOT = export.ROOT
REPO = "sessionphone"
GH = "https://github.com/sessionkit"
RAW = "https://raw.githubusercontent.com/sessionkit"
MAIN = "ma" + "in"                     # the branch name, split so this file stays clean
END = r"(?![A-Za-z0-9_.-])"            # the branch word ends here: not mainline, not main-x
# The refs a cross-repo URL may name: HEAD (the default branch), or a release tag.
REF_OK = re.compile(r"HEAD|v[0-9][A-Za-z0-9._-]*")

# A sessionkit blob, tree or raw URL: the repo and the branch it names.
SESSIONKIT_URL = re.compile(
    rb"github\.com/sessionkit/([A-Za-z0-9._-]+)/(?:blob|tree|raw)/([A-Za-z0-9._-]+)"
    rb"|raw\.githubusercontent\.com/sessionkit/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)",
    re.IGNORECASE)
BRANCH_FLAG = re.compile(rb"(?<![A-Za-z0-9_-])(?:-b|--branch)(?:[ \t]+|=)" + MAIN.encode()
                         + END.encode())

# Not part of the export when the tree on disk is scanned (outside a checkout):
# the environment and caches a test run leaves beside it.
NOT_SHIPPED = {".git", ".venv", "venv", ".pytest_cache", ".ruff_cache", "__pycache__",
               "node_modules", "dist", "build"}

# The relative links this repo's docs carry: (linking file, target).
RELATIVE_LINKS = [
    ("README.md", "docs/configuration.md#upgrading-from-010"),
    ("README.md", "docs/configuration.md"),
    ("README.md", "docs/settings.md"),
    ("docs/configuration.md", "settings.md"),
    ("docs/configuration.md", "../README.md#call-recording-off-unless-it-is-turned-on"),
    ("docs/settings.md", "configuration.md"),
    ("README.md", "docs/dependencies.md"),
    ("docs/dependencies.md", "../README.md#requirements"),
    ("docs/dependencies.md", "../README.md#installation"),
    ("docs/dependencies.md", "../uv.lock"),
    ("docs/dependencies.md", "settings.md"),
]


def url_faults(line):
    """Why `line` breaks the rule, if it does: a full URL to this repo, a
    sessionkit URL whose ref is not HEAD or a v* tag, or a branch flag naming main."""
    faults = []
    for m in SESSIONKIT_URL.finditer(line):
        repo, ref = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if repo.decode().lower() == REPO:
            faults.append("a full URL to this repo: link it relatively")
        elif not REF_OK.fullmatch(ref.decode()):
            faults.append(f"{repo.decode()} on {ref.decode()}: name HEAD or a v* tag, not a branch")
    if BRANCH_FLAG.search(line):
        faults.append(f"a branch flag naming {MAIN}: the public branch is master")
    return faults


def hits(files):
    """`label:path:line: fault: text` for every line of `files`, an iterable of
    (label, path, bytes), that breaks the rule."""
    found = []
    for label, path, data in files:
        for n, line in enumerate(data.splitlines(), 1):
            for fault in url_faults(line):
                text = line.decode("utf-8", "replace").strip()
                found.append(f"{label}:{path}:{n}: {fault}: {text}")
    return found


def archived():
    """(path, bytes) for every regular file in the release archive of HEAD."""
    out = subprocess.run(["git", "-C", str(ROOT), "archive", "--worktree-attributes", "HEAD"],
                         capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(out)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                yield member.name, tar.extractfile(member).read()


def on_disk():
    """(path, bytes) for every file under ROOT bar a run's environment and caches."""
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if any(part in NOT_SHIPPED or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.is_file():
            yield rel.as_posix(), path.read_bytes()


def exported():
    """(label, path, bytes) for the exported tree: HEAD's archive and the
    working-tree copy of each file in it, or, with no checkout, the tree."""
    if not export.in_checkout():
        yield from (("tree", p, d) for p, d in on_disk())
        return
    for path, data in archived():
        yield "HEAD", path, data
        here = ROOT / path
        if here.is_file():
            yield "tree", path, here.read_bytes()


def current(files):
    """{path: text} for the Markdown files, the working-tree copy where both are read."""
    docs = {}
    for label, path, data in files:
        if path.endswith(".md") and (label == "tree" or path not in docs):
            docs[path] = data.decode("utf-8", "replace")
    return docs


# An ATX heading: up to three spaces, one to six #, then its text, less any closing #s.
HEADING = re.compile(r" {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")


def headings(text):
    """GitHub's anchors for the headings in `text`, fenced code left out.

    GitHub slugs a heading's text with its code spans' text kept: only the
    backticks go, with the rest of the punctuation. A heading that repeats an
    anchor already used takes -1, then -2, the way GitHub numbers it."""
    anchors, seen = set(), {}
    for line in pypi_readme.unfenced(text):
        m = HEADING.fullmatch(line)
        if not m:
            continue
        anchor = base = config_docs._slug(m.group(1))
        while anchor in seen:
            seen[base] += 1
            anchor = f"{base}-{seen[base]}"
        seen[anchor] = 0
        anchors.add(anchor)
    return anchors


def is_relative(target):
    return not re.match(r"(#|[a-z][a-z0-9+.-]*:)", target, re.IGNORECASE)


def link_fault(source, target, paths, docs):
    """Why the relative link `target` in `source` does not resolve, or None."""
    rel, _, anchor = target.partition("#")
    if not rel:                                         # an in-page anchor: this file
        path = source
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(source), rel))
    if path.startswith("../") or path == "..":
        return f"{target}: leaves the repo"
    if path not in paths and not any(p.startswith(path + "/") for p in paths):
        return f"{target}: {path} is not in the export"
    if anchor:
        if path not in docs:
            return f"{target}: {path} is not a Markdown file with headings"
        if anchor not in headings(docs[path]):
            return f"{target}: {path} has no heading #{anchor}"
    return None


def broken_links(files):
    """`path: fault` for every relative link or in-page anchor in the Markdown
    of `files` that does not resolve."""
    files = list(files)
    paths = {path for _, path, _ in files}
    docs = current(files)
    found = []
    for source, text in sorted(docs.items()):
        for target in pypi_readme.link_targets(text):
            if is_relative(target) or target.startswith("#"):
                fault = link_fault(source, target, paths, docs)
                if fault:
                    found.append(f"{source}: {fault}")
    return found


@pytest.fixture(scope="module")
def shipped():
    return list(exported())


# --- the gate over the export -------------------------------------------------

def test_the_gate_reads_an_export(shipped):
    paths = {path for _, path, _ in shipped}
    assert {"README.md", "docs/settings.md", "docs/configuration.md"} <= paths, \
        "the gate read no export"


@export.needs_checkout
@export.needs_present("CLAUDE.md")
def test_the_gate_reads_the_export_not_the_tree(shipped):
    """The archive leaves CLAUDE.md and design/ out, so the gate does too."""
    paths = {path for _, path, _ in shipped}
    assert "CLAUDE.md" not in paths
    assert not [p for p in paths if p == "design" or p.startswith("design/")]


def test_no_shipped_file_links_this_repo_by_url_or_names_main(shipped):
    assert hits(shipped) == []


def test_every_relative_link_in_a_shipped_doc_resolves(shipped):
    assert broken_links(shipped) == []


@pytest.mark.parametrize("source, target", RELATIVE_LINKS)
def test_the_docs_link_each_other_relatively(shipped, source, target):
    """Each of the docs' own links is there, relative, and it resolves."""
    docs = current(shipped)
    assert target in pypi_readme.link_targets(docs[source]), f"{source} no longer links {target}"
    paths = {path for _, path, _ in shipped}
    assert link_fault(source, target, paths, docs) is None


# --- the controls: planted files the gate must catch, and must leave ------------

def _plant(text, path="zz-planted.md"):
    text = text.replace("{gh}", GH).replace("{raw}", RAW).replace("BRANCH", MAIN)
    return [("planted", path, text.encode())]


@pytest.mark.parametrize("line, fault", [
    # (a) a sessionkit repo on main, or a branch flag naming it
    ("[kit]: {gh}/sessionkit/blob/BRANCH/docs/settings.md", "sessionkit on BRANCH"),
    ("see {gh}/sessionmail/tree/BRANCH/skills", "sessionmail on BRANCH"),
    ("curl -fsSL {gh}/sessionmail/raw/BRANCH/install.sh", "sessionmail on BRANCH"),
    ("curl {raw}/sessionmail/BRANCH/x.sh",
     "sessionmail on BRANCH"),
    ("[x]({gh}/sessionvoice/blob/BRANCH)", "sessionvoice on BRANCH"),
    ("{gh}/sessionmail/blob/BRANCHline/x.md", "sessionmail on BRANCHline"),
    # (a) master too: a branch name, where HEAD survives a rename
    ("[kit-settings]: {gh}/sessionkit/blob/master/docs/settings.md", "sessionkit on master"),
    ("{gh}/sessionmail/tree/master/skills", "sessionmail on master"),
    ("{raw}/sessionmail/master/install.sh", "sessionmail on master"),
    ("{gh}/sessionmail/blob/head/x.md", "sessionmail on head"),         # HEAD is upper case
    ("{gh}/sessionmail/blob/vnext/x.md", "sessionmail on vnext"),       # a tag is v + digit
    ("git clone -b BRANCH {gh}/sessionmail", "branch flag naming BRANCH"),
    ("git clone --branch BRANCH {gh}/sessionmail", "branch flag naming BRANCH"),
    ("git clone --branch=BRANCH {gh}/sessionmail", "branch flag naming BRANCH"),
    ("    git checkout -b BRANCH", "branch flag naming BRANCH"),
    # (b) a full URL to this repo, on any branch
    ("[docs]({gh}/sessionphone/blob/master/docs/settings.md)", "a full URL to this repo"),
    ("[docs]({gh}/sessionphone/blob/HEAD/docs/settings.md)", "a full URL to this repo"),
    ("[docs]({gh}/sessionphone/blob/v0.2.0/docs/settings.md)", "a full URL to this repo"),
    ("[settings]: {gh}/sessionphone/blob/BRANCH/docs/settings.md", "a full URL to this repo"),
    ("see {gh}/sessionphone/tree/master/skills", "a full URL to this repo"),
    ("{gh}/SessionPhone/raw/master/README.md", "a full URL to this repo"),
    ("{raw}/sessionphone/master/README.md",
     "a full URL to this repo"),
])
def test_the_gate_fires_on_a_planted_line(line, fault):
    found = hits(_plant(f"before\n{line}\nafter\n"))
    assert len(found) == 1, found
    assert found[0].startswith("planted:zz-planted.md:2: ")
    assert fault.replace("BRANCH", MAIN) in found[0]


@pytest.mark.parametrize("line", [
    "[kit-settings]: {gh}/sessionkit/blob/HEAD/docs/settings.md",       # cross-repo, HEAD
    "{gh}/sessionmail/tree/HEAD/skills",
    "{raw}/sessionmail/HEAD/install.sh",
    "{gh}/sessionkit/blob/v0.2.0/docs/settings.md",                     # a release tag
    "{gh}/sessionkit/tree/v1.10.3-rc1/docs",
    "{gh}/sessionphone",                       # branchless, as pyproject.toml has it
    '  "repository": "{gh}/sessionphone"',     # and .claude-plugin/plugin.json
    "[configuration]: configuration.md",       # relative
    "https://github.com/chatmail/core/blob/BRANCH/deltachat-rpc-server/README.md",
    "    branch = BRANCH[:3]",                  # a Python variable in a test
    "# the reply reaches bot.BRANCH, not the bridge",
    "def BRANCH() -> None:",
    "git clone -b master {gh}/sessionmail",
    "python -m BRANCH --branch master",
])
def test_the_gate_leaves_a_planted_line_that_keeps_the_rule(line):
    assert hits(_plant(f"{line}\n")) == []


def _tree(**docs):
    """A planted export: README.md, docs/configuration.md and docs/settings.md
    with their headings, a LICENSE, and the files given, by path."""
    files = {"README.md": "# sessionphone\n\n## Call recording: off unless it is turned on\n",
             "docs/configuration.md": "# Configuration\n\n### Upgrading from 0.1.0\n",
             "docs/settings.md": "# Settings\n",
             "LICENSE": "MIT\n"}
    files.update(docs)
    return [("planted", path, text.encode()) for path, text in files.items()]


@pytest.mark.parametrize("link", [
    "[c](docs/configuration.md)",
    "[c](docs/configuration.md#upgrading-from-010)",
    "[c](./docs/settings.md)",
    "[l](LICENSE)",
    "[d](docs)",
    "[h](README.md#call-recording-off-unless-it-is-turned-on)",
    "[h](#planted-zz)",                                  # an in-page anchor, this file's own
    "[w](https://example.com/a.md)",
    "`[not](a/link)`",
])
def test_a_planted_link_that_resolves_passes(link):
    assert broken_links(_tree(**{"zz.md": f"# Planted: `zz`\n\nsee {link}\n"})) == []


@pytest.mark.parametrize("source, link, fault", [
    ("zz.md", "[c](docs/configuration.md#no-such-heading)", "has no heading #no-such-heading"),
    ("zz.md", "[c](docs/missing.md)", "docs/missing.md is not in the export"),
    ("zz.md", "[c](CLAUDE.md)", "CLAUDE.md is not in the export"),
    ("zz.md", "[c](../README.md)", "leaves the repo"),
    ("zz.md", "[c](LICENSE#x)", "is not a Markdown file"),
    ("docs/zz.md", "[c](configuration.md#upgrading-from-01)", "has no heading"),
    ("docs/zz.md", "[c](README.md#call-recording-off-unless-it-is-turned-on)",
     "docs/README.md is not in the export"),
    ("docs/zz.md", "[recording]: ../README.md#call-recording",
     "README.md has no heading #call-recording"),
    # an in-page anchor names a heading in the linking file, not in another
    ("zz.md", "[h](#call-recording-off-unless-it-is-turned-on)",
     "zz.md has no heading #call-recording-off-unless-it-is-turned-on"),
    ("docs/zz.md", "[h](#no-such-heading)", "docs/zz.md has no heading #no-such-heading"),
])
def test_a_planted_broken_link_fails(source, link, fault):
    """The control: a link that misses its file or its heading is caught,
    resolved from the linking file's own directory."""
    found = broken_links(_tree(**{source: f"see\n{link}\n"}))
    assert len(found) == 1, found
    assert found[0].startswith(f"{source}: ") and fault in found[0], found


# --- the anchors: GitHub's slug for a heading -----------------------------------

@pytest.mark.parametrize("heading, anchor", [
    # a code span's text stays in the anchor; only its backticks go
    ("### The Guide Trail app: `sessionphone app`", "the-guide-trail-app-sessionphone-app"),
    ("## App taps: `DC_XDC_IN` is the integration point",
     "app-taps-dc_xdc_in-is-the-integration-point"),
    ("## ``a`b`` and `c`", "ab-and-c"),                  # a double-backtick span
    ("## The dc-* tools", "the-dc--tools"),               # punctuation goes, each space stays
    ("## The bot's seat", "the-bots-seat"),
    ("## Upgrading from 0.1.0", "upgrading-from-010"),
    ("# One", "one"),
    ("###### Six", "six"),
    ("   ## Indented three", "indented-three"),
    ("## Closed ##", "closed"),                           # a closing sequence is not text
    ("## C# notes", "c-notes"),                           # a # inside the text is
])
def test_a_heading_takes_githubs_anchor(heading, anchor):
    assert headings(f"intro\n\n{heading}\n\nbody\n") == {anchor}


def test_a_code_span_heading_no_longer_takes_the_anchor_without_its_code():
    found = headings("## The Guide Trail app: `sessionphone app`\n")
    assert "the-guide-trail-app" not in found
    assert "app-taps--is-the-integration-point" not in headings(
        "## App taps: `DC_XDC_IN` is the integration point\n")


@pytest.mark.parametrize("text", [
    "```\n# a comment, not a heading\n```\n",
    "```bash\n# a comment, not a heading\n```\n",
    "~~~\n# a comment, not a heading\n~~~\n",
    "````\n```\n# a comment, not a heading\n```\n````\n",   # a fence inside a longer one
    "#not a heading: no space\n",
    "####### seven is not a heading\n",
    "    # four spaces is code\n",
])
def test_a_line_that_is_not_a_heading_is_no_anchor(text):
    assert headings(text) == set()


def test_text_after_a_closed_fence_is_read_again():
    assert headings("```\n# in\n```\n## Out `here`\n") == {"out-here"}


def test_a_repeated_heading_is_numbered_the_way_github_does():
    text = "## Upgrade\n\n## Upgrade\n\n### Upgrade\n"
    assert headings(text) == {"upgrade", "upgrade-1", "upgrade-2"}


def test_a_numbered_repeat_steps_past_an_anchor_a_heading_already_holds():
    # "A", then a heading whose own anchor is a-1: the next "A" is a-2
    assert headings("## A\n## A-1\n## A\n") == {"a", "a-1", "a-2"}


def test_a_link_to_a_code_span_heading_resolves_by_githubs_anchor():
    page = "# Page\n\n### The Guide Trail app: `sessionphone app`\n"
    good = "[g](docs/page.md#the-guide-trail-app-sessionphone-app)"
    assert broken_links(_tree(**{"docs/page.md": page, "zz.md": f"see {good}\n"})) == []
    found = broken_links(_tree(**{"docs/page.md": page,
                                  "zz.md": "see [g](docs/page.md#the-guide-trail-app)\n"}))
    assert len(found) == 1 and "has no heading #the-guide-trail-app" in found[0], found


def test_prose_still_drops_code_for_the_link_scan():
    """headings() keeps code spans; link_targets() still reads prose without them."""
    assert pypi_readme.prose("a `[x](y)` b\n```\n[z](w)\n```\n") == "a  b"
    assert pypi_readme.link_targets("a `[x](y)` b [k](v)\n") == ["v"]
    assert pypi_readme.unfenced("a `b`\n```\nc\n```\nd\n") == ["a `b`", "d"]

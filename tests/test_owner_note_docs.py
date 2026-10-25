"""The pre-upgrade check in the docs matches what the key migration does (PHONE-243).

README's "Before you upgrade, check each list" tells an operator three things
before the key upgrade: add `# owner` to the owner's line, `chmod 600` the
lists, and take care with one callers.txt shared by two bots. These tests feed
the README's own example lines to the code's migration, so they fail if the
code's owner rule (a note that starts `owner`) or its mode rule changes, or if
the README or the changelog fragment stops saying it.
"""
import pathlib

import _changelog
import _keyfakes as kf

from sessionphone import channels, keygate

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
FLAT = " ".join(README.split())
# the entry: its fragment while pending, its CHANGELOG.md section once released
FRAGMENT = " ".join(_changelog.entry(
    "PHONE-243-owner-note-docs.md", "Check callers.txt and channels.txt before the key upgrade").split())
# the callers pre-upgrade block, from its lead to the upgrade steps
BEFORE = README.split("**Before you upgrade, check each list.**", 1)[1].split(
    "**Upgrading a list from an older version.**", 1)[0]
# the channels one, from its lead to the channels upgrade paragraph
CHANNELS_BEFORE = README.split("**Before you upgrade, check the owner line.**", 1)[1].split(
    "**Upgrading a list that has address lines.**", 1)[0]


def fenced(block: str, lang: str) -> list[str]:
    """The lines of every ```LANG fence in BLOCK, unindented."""
    out, inside = [], False
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("```"):
            inside = s == f"```{lang}" if not inside else False
        elif inside:
            out.append(s)
    return out


def owner_example() -> str:
    """The README's example owner line with no key: the one an operator copies."""
    lines = [s for s in fenced(BEFORE, "text") if "# owner" in s and "key:" not in s]
    assert len(lines) == 1, lines
    return lines[0]


def keyed_example() -> str:
    lines = [s for s in fenced(BEFORE, "text") if "# owner" in s and "key:" in s]
    assert len(lines) == 1, lines
    return lines[0]


def callers(tmp_path, text, mode=0o600, name="callers.txt"):
    f = tmp_path / name
    f.write_text(text)
    f.chmod(mode)
    return f


def one_key_contact(addr):
    return kf.Account([kf.Contact(10, addr, kf.K1)])


def test_readme_owner_example_is_pinned_and_the_same_line_without_the_note_is_not(tmp_path):
    line = owner_example()
    addr = line.split()[0]
    noted = callers(tmp_path, line + "\n")
    got = keygate.migrate(one_key_contact(addr), noted, lambda _s: None)
    assert [d.kind for d in got] == ["pinned"]
    assert keygate.read_caller_entries(noted)[0].key == kf.FPR_K1

    bare = callers(tmp_path, addr + "\n", name="bare.txt")      # a hand-written line, no note
    said = []
    got = keygate.migrate(one_key_contact(addr), bare, said.append)
    assert [d.kind for d in got] == ["unpinned"]
    assert any("nobody can ring" in s for s in said)


def test_readme_names_the_codes_owner_prefix():
    assert keygate.parse_caller_line(owner_example()).note.lower().startswith("owner")
    assert "note starts `owner`" in " ".join(BEFORE.split())
    assert "Add `# owner` to the owner's line before you upgrade" in " ".join(BEFORE.split())


def test_channels_owner_line_is_the_name_owner_or_a_note_that_starts_owner():
    said = " ".join(CHANNELS_BEFORE.split())
    assert "the one named `owner`, or one whose note starts `owner`" in said
    assert "add `# owner` to it before you upgrade" in said
    p = pathlib.Path("channels.txt")
    named = channels.parse("owner  owner@relay.example\n", p)["owner"]
    noted = channels.parse("boss  owner@relay.example  # owner\n", p)["boss"]
    plain = channels.parse("boss  owner@relay.example\n", p)["boss"]
    assert channels.is_owner(named) and channels.is_owner(noted)
    assert not channels.is_owner(plain)


def test_a_loose_list_is_not_migrated_with_the_log_line_the_readme_quotes(tmp_path):
    line = owner_example()
    f = callers(tmp_path, line + "\n", mode=0o664)
    said = []
    assert keygate.migrate(one_key_contact(line.split()[0]), f, said.append) is None
    quoted = "NOT migrated: mode 0664 lets others write it"
    assert quoted in " ".join(BEFORE.split())
    assert any(quoted in s for s in said), said
    assert "chmod 600 callers.txt channels.txt" in fenced(BEFORE, "bash")
    f.chmod(0o600)                                       # the fix the README gives
    assert [d.kind for d in keygate.migrate(one_key_contact(line.split()[0]), f, lambda _s: None)] == ["pinned"]


def test_a_shared_file_is_migrated_once_by_the_first_bot_in_its_own_account(tmp_path):
    """Two owner lines, two bots: the first start pins only what its own
    account knows, the second skips the marked file, as the README says."""
    f = callers(tmp_path, "one@relay.example  # owner\ntwo@relay.example  # owner\n")
    first = kf.Account([kf.Contact(10, "one@relay.example", kf.K1)])
    second = kf.Account([kf.Contact(11, "two@relay.example", kf.K2)])
    assert [d.kind for d in keygate.migrate(first, f, lambda _s: None)] == ["pinned", "unpinned"]
    said = []
    assert keygate.migrate(second, f, said.append) is None and said == []    # no log line
    assert [e.key for e in keygate.read_caller_entries(f)] == [kf.FPR_K1, None]
    text = " ".join(BEFORE.split())
    assert "The second sees the marker and skips the file, with no log line." in text
    assert "DC_CALLERS_FILE" in text and "do not rely on which starts first" in text


def test_the_readme_keyed_owner_line_reads_as_pinned_by_the_new_parser(tmp_path):
    said = []
    f = callers(tmp_path, keyed_example() + "\n")
    entries = keygate.read_caller_entries(f, said.append)
    assert said == [] and len(entries) == 1
    assert entries[0].key and entries[0].note.startswith("owner")


def test_the_changelog_fragment_says_the_same_three_things():
    assert "# owner" in FRAGMENT and "note starts `owner`" in FRAGMENT
    assert "chmod 600" in FRAGMENT
    assert "DC_CALLERS_FILE" in FRAGMENT and "whichever bot starts first" in FRAGMENT


def test_the_pre_upgrade_check_never_says_pending():
    """The shared-file behaviour was measured; the README states it plainly."""
    assert "pending" not in BEFORE.lower()
    assert "pending" not in FRAGMENT.lower()
    assert "Before you upgrade, check each list" in FLAT

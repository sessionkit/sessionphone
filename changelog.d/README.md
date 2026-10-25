# changelog.d: one fragment per change

**Write your change's changelog entry here, never in `CHANGELOG.md`.** Two
branches that both edit `CHANGELOG.md` clash on it, and a line added just
before a release can merge cleanly under the wrong version. Two branches
that each add their own file here never clash. The release moves the
entries into `CHANGELOG.md`.

## The file

- **One file per task:** `changelog.d/<TASK-ID>-<slug>.md`, for example
  `changelog.d/PHONE-239-changelog-fragments.md`. The slug is the task's
  short name, the tail of its branch name.
- **A task that changes its entry edits its own fragment.** It never adds a
  second one, and never renames the first.
- **A change no user would notice writes no fragment** (tests only, internal
  tooling, CI). Its description says so, in these words:
  `Changelog: none, no user-visible change`.
- **Every `*.md` here but this README is a fragment.** Nothing else belongs
  in `changelog.d/`: no other file name, no sub-directory. The release
  refuses a file it would not read.

## What a fragment holds

One or more `### <headline>` sections, each with its bullets, exactly as the
entry should read in `CHANGELOG.md`. This is the shape every entry under
`## [0.3.0]` has:

```markdown
### A long call turn reaches the agent whole

- A turn of more than 4000 characters was cut at 4000, and the rest was
  dropped.
- Heard words are now never cut on any path that feeds an agent.
```

The release refuses, before it bumps the version and naming the file:

- text before the first `###`;
- any other heading level (`#`, `##`, `####` and deeper), so a fragment can
  never add a version heading;
- a fragment with no entries.

A heading inside a code fence is text, not a heading.

A fragment never names a version or `[Unreleased]`: the release puts it
under the right one.

**No relative links.** A link written to work from here breaks once the
entry is moved into `CHANGELOG.md`, and one written for `CHANGELOG.md` is
broken here. Name the file in a code span instead: `README.md`, not a link
to it. A full web address to a page outside this repo is fine.

The text rules are the same as for `CHANGELOG.md`: neutral public prose, no
private names, paths or host names, and example times only.

## Reading what is pending

`make changelog` prints `## [Unreleased]` with every fragment here collated
into it, as the next release will write it. It uses the release's own
merge, writes nothing, and needs no clean tree. Each file here is also its
own entry, verbatim.

## At release

`make release` (`scripts/release.py`) collects every fragment into
`CHANGELOG.md` under the new `## [X.Y.Z] - date` heading, then `git rm`s
them in the release commit. A release commit holds no fragment.

- **Order is by file name, with numbers compared as numbers:** `PHONE-9-…`,
  then `PHONE-20-…`, then `PHONE-999-…`, then `PHONE-1000-…`. An author has
  no say in where an entry lands, and the same files always give the same
  changelog.
- **Each fragment's sections are kept whole**, in the order written. Nothing
  is merged, within a fragment or across them.
- **Anything still under `## [Unreleased]` in `CHANGELOG.md`** (entries
  written before this directory existed) follows the fragments under the new
  version, unchanged.
- `python3 scripts/release.py patch --dry-run` lists the fragments it
  would collect and touches nothing.

## Fixing an entry (a scrub)

- **Not released yet:** edit the fragment, on your branch.
- **Released:** edit the entry in place in `CHANGELOG.md`, under its version.
- **Both at once:** a branch edits a fragment that a release has since
  collated and removed. Git stops the merge with a modify/delete conflict on
  the fragment. Resolve it in three steps:
  1. apply the fragment's new text to the collated entry in `CHANGELOG.md`;
  2. `git rm` the fragment;
  3. run `make scan`.

  **Never resolve it by keeping the deletion alone.** That drops the fix
  silently, and the old text stays in `CHANGELOG.md`.

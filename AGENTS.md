# AGENTS.md

Instructions for any coding agent working on this repo.

## What sessionphone is

A Claude Code plugin and Python package that lets a person text and ring
their coding-agent session from Delta Chat on their phone. The resident bot
(`src/sessionphone/bridge/`) holds the agent's Delta Chat account, hands each
chat message and call turn to the agent as signed mail, and speaks the
agent's replies into calls. `sessionphone` is the CLI
(`src/sessionphone/cli.py`); `skills/sessionphone/SKILL.md` is the map the
agent follows to set it up.

## The spec rule

Read the spec before you build. The spec is:

- `README.md`, above all "The agent contract": what reaches an agent, and how;
- `skills/sessionphone/SKILL.md`: what the agent is told to do.

A change in behaviour updates the doc that describes it in the same change,
and writes its changelog entry as one fragment,
`changelog.d/<TASK-ID>-<slug>.md`, never in `CHANGELOG.md`
(`changelog.d/README.md` says how). `make changelog` prints the pending
entries as the next release will read.

## Build and test

```bash
uv sync          # the dev environment, media pack included
make test        # the pytest suite (uv run pytest -q)
make check       # ruff, shellcheck, pytest, then bin/bridge-check (the bridge tests)
make browser     # the web page in headless Chrome; not part of make test
make scan        # the private-word scan; set SESSIONKIT_SCAN to the release-scan tool
```

The tests that guard against private words read the same private list, from
the file `SESSIONKIT_SCRUB_RULES` names. Without it they skip, and the
`make test` summary counts them and names the variable.

## Rules for this repo

- Tests use fakes: no real Delta Chat account, relay, launchd job, mail
  delivery or speech server.
- `bin/dc-env` mirrors `src/sessionphone/bridge/dc_env.py`: change both or
  neither.
- Every setting is `DC_<NAME>`. `PIP_CHAT_<NAME>` is a deprecated alias that
  is still read.
- Keep private paths, host names and personal addresses out of the tree.

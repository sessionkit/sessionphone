# sessionphone - Makefile. `make` lists the targets; each line with ## is one.

.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash
BUMP ?= patch
# every tracked *.sh, and every tracked file whose FIRST line is a sh/bash shebang
SHELLSCRIPTS := $(shell { git ls-files -z | xargs -0 awk 'FNR==1 && /^\#!\/(usr\/bin\/env )?(ba)?sh/ {print FILENAME}'; git ls-files '*.sh'; } | sort -u)
.PHONY: help install test test-vision lint format check browser build release push publish

help:  ## this list
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

# `uv tool install` replaces an existing tool env, so this always asks for the
# media pack: a bare `-e .` would strip it from under a bot that runs from it.
install:  ## put sessionphone on the PATH, editable, with the media pack (uv tool install -e '.[media]')
	@uv tool install -e '.[media]'

test:  ## the pytest suite
	@uv run pytest -q

test-vision:  ## OPT-IN, not in `test`: imgtext on real macOS Vision (builds the Swift helper; skips off macOS or with no swiftc)
	@SESSIONPHONE_VISION=1 uv run pytest -q -rs tests/test_imgtext_vision.py

lint:  ## ruff check; shellcheck on every tracked shell script (see SHELLSCRIPTS)
	@uv run ruff check .
	@if [ -n "$(SHELLSCRIPTS)" ]; then command -v shellcheck >/dev/null || { echo "shellcheck not installed (brew install shellcheck)"; exit 1; }; shellcheck $(SHELLSCRIPTS) && echo "shellcheck: $(words $(SHELLSCRIPTS)) scripts clean"; else echo "shellcheck: no shell scripts"; fi

format:  ## ruff format
	@uv run ruff format .

check: lint test  ## lint, then test

browser:  ## the web page in headless Chrome: scratch HTTPS localhost, virtual passkey, fake mic (tests/browser/run.sh)
	@tests/browser/run.sh

build:  ## wheel + sdist into dist/
	@uv build

release:  ## bump (BUMP=patch|minor|major) or set (VERSION=X.Y.Z), then mirror, changelog, commit, tag
	@uv run python scripts/release.py $(or $(VERSION),$(BUMP))

push:  ## push the branch and the tags to origin (release never pushes; this is the line it printed)
	@git push origin HEAD && git push origin --tags

# THE TOKEN NEVER TOUCHES A COMMAND LINE. argv is readable by every process
# on the machine (`ps`); a child's environment only by its own user. So the
# token reaches uv as UV_PUBLISH_TOKEN in uv's environment, from one
# `pass show` (one touch), and what `ps` shows is `uv publish dist/...`.
# PYPI_TOKEN_PASS_PATH names the pass entry, e.g.
# pypi/upload-token; set it in your shell profile.
publish: build  ## upload THIS version's wheel + sdist to PyPI; token via $$PYPI_TOKEN_PASS_PATH (pass) or $$UV_PUBLISH_TOKEN, never on a command line
	@dist="dist/$$(echo 'sessionphone' | tr '-' '_')-$$(uv version --short)"; \
	if [ -n "$$UV_PUBLISH_TOKEN" ]; then uv publish "$$dist"*; \
	elif [ -n "$$PYPI_TOKEN_PASS_PATH" ]; then UV_PUBLISH_TOKEN="$$(pass show "$$PYPI_TOKEN_PASS_PATH" | head -n1)" uv publish "$$dist"*; \
	else echo "publish: set PYPI_TOKEN_PASS_PATH to the pass entry holding your PyPI token (or UV_PUBLISH_TOKEN); the token is never put on a command line" >&2; exit 1; fi

# The pre-publish scrub check. The scanner and its word list are PRIVATE and never
# live in this repo: SESSIONKIT_SCAN names the release-scan tool (it finds its list,
# or takes SESSIONKIT_SCRUB_RULES). Exits non-zero on any P0 term in HEAD's tree.
.PHONY: scan scrub
scan:  ## scrub check: HEAD's tree against the private word list; fails on any P0 term
	@[ -n "$$SESSIONKIT_SCAN" ] || { echo "scan: set SESSIONKIT_SCAN to the release-scan tool" >&2; exit 2; }
	@"$$SESSIONKIT_SCAN" --here --fail-on P0

scrub: scan  ## the same check, by its other name

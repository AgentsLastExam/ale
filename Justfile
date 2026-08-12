# ALE developer surface.
#
# Every recipe is plain shell, so anything here can also be run by hand.
# Install just itself with:  uv tool install rust-just
#
# Golden rules (see CLAUDE.md):
#   * always `uv run <cmd>` — never activate a venv, never `pip install`
#   * worktrees live in ../ale-worktrees/, created with `just wt <name>`
#   * run `just doctor` before debugging any environment problem

set shell := ["bash", "-uc"]

# Deterministic installs everywhere: the lockfile is the single source of truth.
export UV_FROZEN := "1"

_default:
    @just --list

# Install the workspace into a per-tree .venv (fast: the global uv cache hardlinks).
bootstrap:
    @scripts/bootstrap.sh

# Verify this tree behaves exactly like main; prints a fix for every failure.
# --no-project skips the lockfile entirely, so UV_FROZEN would be a no-op warning.
doctor:
    @env -u UV_FROZEN uv run --no-project python scripts/doctor.py

# Create a worktree under ../ale-worktrees/<name> and bootstrap it in one step.
wt name:
    #!/usr/bin/env bash
    set -euo pipefail
    main="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
    dest="$(dirname "$main")/ale-worktrees/{{ name }}"
    if [ -d "$dest" ]; then
        echo ">> worktree already exists: $dest"
    else
        git -C "$main" worktree add "$dest" -b "{{ name }}"
    fi
    "$main/scripts/bootstrap.sh" "$dest"
    echo ">> ready: cd $dest"

# Remove a worktree and its branch (branch removal is best effort).
wt-rm name:
    #!/usr/bin/env bash
    set -euo pipefail
    main="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
    dest="$(dirname "$main")/ale-worktrees/{{ name }}"
    git -C "$main" worktree remove "$dest" --force
    git -C "$main" branch -d "{{ name }}" || true

# Fast in-process suite: unit and lightweight conformance.
test:
    @uv run pytest -m unit

# Everything that needs a real sandbox; add -m 'integration and not needs_kvm' to skip VMs.
test-int:
    @uv run pytest -m "conformance or integration"

lint:
    @uv run ruff check .
    @uv run ruff format --check .
    @uv run lint-imports

fmt:
    @uv run ruff format .
    @uv run ruff check --fix .

# Build the base sandbox images locally (publishing happens in CI).
images:
    @images/base/build.sh

# The canonical smoke run: one command, one verdict.
demo:
    @uv run ale run demo/readfile_secret --agent claude-code

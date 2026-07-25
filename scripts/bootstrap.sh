#!/usr/bin/env bash
# Bring a checkout (main clone or worktree) to a working state.
#
# Design notes — this is the "no venv rebuild" contract:
#   * Each tree gets its OWN .venv. Sharing one venv across worktrees is unsafe:
#     workspace members are installed as editables pointing at absolute paths, so a
#     shared venv would silently import another tree's source.
#   * Speed comes from uv's global cache (~/.cache/uv) instead: wheels are hardlinked,
#     so a fresh worktree syncs in seconds without rebuilding anything.
#   * The committed uv.lock is the single source of truth; --frozen forbids silent
#     re-resolution, so every tree and CI install exactly the same versions.
set -euo pipefail

target="${1:-.}"
cd "$target"

command -v uv >/dev/null 2>&1 || {
    echo "ERROR: uv not found — install from https://docs.astral.sh/uv/" >&2
    exit 1
}

echo ">> syncing $(pwd) (frozen)"
uv sync --frozen --all-groups

# Hooks live in the shared git dir, so this is idempotent across worktrees.
uv run pre-commit install --install-hooks >/dev/null 2>&1 || {
    echo ">> warning: pre-commit hook install failed (non-fatal)" >&2
}

echo ">> done. next: uv run ale --help   |   just doctor"

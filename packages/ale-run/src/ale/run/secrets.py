"""Credentials, loaded from the repository rather than from a home directory.

A checkout carries its own configuration: `.env` beside `.env.example`, gitignored, the
way every reference implementation does it. Two worktrees can then point at different
providers without fighting over a shared file, and "what credentials did this run use?"
is answered by looking at the tree the run came from.

Nothing here ever reaches a sandbox. Values loaded into the host process are what the
gateway holds; an agent only ever sees a per-episode token.
"""

from __future__ import annotations

import os
from pathlib import Path

from ale.core.errors import ConfigError

__all__ = ["ENV_FILE", "find_env_file", "load_env", "provider_credentials"]

ENV_FILE = ".env"
EXAMPLE_FILE = ".env.example"


def find_env_file(start: Path | None = None) -> Path | None:
    """Locate the environment file for a checkout.

    ``ALE_ENV_FILE`` wins when set, so a run can point at a different provider without
    editing anything; otherwise walk up from the working directory to the repository
    root, which makes the file work from anywhere inside a tree.
    """
    if override := os.environ.get("ALE_ENV_FILE"):
        path = Path(override).expanduser()
        return path if path.is_file() else None

    here = (start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / ENV_FILE
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():  # stop at the repository root
            break
    return None


def load_env(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Read ``KEY=value`` lines into the process environment.

    Existing variables win by default: an explicit export on the command line should
    beat a file, not the other way round.
    """
    file = path or find_env_file()
    if file is None:
        return {}

    loaded: dict[str, str] = {}
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


DEFAULT_KEY_VAR = "ANTHROPIC_API_KEY"
DEFAULT_BASE_URL = "https://api.anthropic.com"


def provider_credentials(key_var: str = DEFAULT_KEY_VAR, base_url: str = "") -> tuple[str, str]:
    """The API key and upstream endpoint the gateway should use.

    A run names which variable holds its key and states the endpoint that key belongs to.
    Both are per-run, because one checkout is used against several providers and the
    alternative — one pair of well-known variable names — means editing the environment
    between runs and, sooner or later, sending one provider's credential to another.

    The key is named rather than passed. A value on the command line is in the shell's
    history and in every process listing on the machine; a variable name is not.
    """
    load_env()
    key = os.environ.get(key_var, "")
    if not key:
        raise ConfigError(
            f"{key_var} is not set. Put it in the checkout's .env (copy .env.example) "
            f"or name a different variable with --api-key-env."
        )
    return key, (base_url or DEFAULT_BASE_URL).rstrip("/")

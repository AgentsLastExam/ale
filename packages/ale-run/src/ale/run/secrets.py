"""Credentials, loaded from the repository rather than from a home directory.

A checkout carries its own configuration: `.env` beside `.env.example`, gitignored, the
way every reference implementation does it. Two worktrees can then point at different
providers without fighting over a shared file, and "what credentials did this run use?"
is answered by looking at the tree the run came from.

Every name ALE reads carries an ``ALE_`` prefix, so a checkout's credentials never collide
with the variables the operator's own tools read. API keys loaded here remain Host-side
behind the Gateway. Checkout-local subscription credentials are resolved separately and
also remain Host-side.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from ale.core.errors import ConfigError

__all__ = [
    "ENV_FILE",
    "PROVIDER_URLS",
    "find_env_file",
    "hf_token",
    "load_env",
    "provider_credentials",
    "provider_endpoint",
]

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


#: Each model provider's own API, used when the checkout names no other endpoint for it.
PROVIDER_URLS = {
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "meta": "https://api.meta.ai",
    "moonshot": "https://api.moonshot.ai",
    "openai": "https://api.openai.com",
    "qwen": "https://maas.qwencloudapi.com/compatible-mode",
    "xai": "https://api.x.ai",
    "zai": "https://api.z.ai/api/anthropic",
}


def provider_endpoint(provider: str) -> tuple[str, str]:
    """The variable holding a provider's key, and the endpoint that key belongs to.

    Both come from the provider's name, ``ALE_<PROVIDER>_API_KEY`` and
    ``ALE_<PROVIDER>_BASE_URL``, so a key is only ever paired with its own provider's URL;
    an unset URL selects the provider's own API.
    """
    prefix = f"ALE_{provider.upper()}"
    url = os.environ.get(f"{prefix}_BASE_URL", "").strip() or PROVIDER_URLS[provider]
    return f"{prefix}_API_KEY", url.rstrip("/")


def provider_credentials(
    provider: str,
    *,
    base_url: str = "",
    key_var: str = "",
) -> tuple[str, str]:
    """The API key and upstream endpoint the Gateway uses for one provider.

    ``base_url`` and ``key_var`` are one-off overrides (``--base-url``, ``--api-key-env``).
    An endpoint other than the provider's own must name the variable holding its key, so
    the provider's key is never sent anywhere else.

    The key is named rather than passed. A value on the command line is in the shell's
    history and in every process listing on the machine; a variable name is not.
    """
    default_key_var, default_base_url = provider_endpoint(provider)
    if not key_var and base_url and base_url.rstrip("/") != default_base_url:
        raise ConfigError(
            f"gateway.base_url {base_url!r} is not the {provider} endpoint; name the "
            "variable holding its key with --api-key-env (gateway.api_key_env) so "
            f"{default_key_var} is never sent to another endpoint"
        )
    key_var = key_var or default_key_var
    key = os.environ.get(key_var, "")
    if not key:
        raise ConfigError(
            f"{key_var} is not set. Put it in the checkout's .env (copy .env.example) "
            f"or name a different variable with --api-key-env."
        )
    return key, (base_url or default_base_url).rstrip("/")


def hf_token() -> str | Literal[False]:
    """The Hugging Face token ALE passes to every Hub call, from ``ALE_HF_TOKEN``.

    ``False`` makes ``huggingface_hub`` look no further, so neither the operator's own
    ``HF_TOKEN`` nor a cached login decides which account ALE acts as.
    """
    return os.environ.get("ALE_HF_TOKEN", "").strip() or False

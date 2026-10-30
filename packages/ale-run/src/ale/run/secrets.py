"""Credentials, loaded from the repository rather than from a home directory.

A checkout carries its own configuration: `.env` beside `.env.example`, gitignored, the
way every reference implementation does it. Two worktrees can then point at different
providers without fighting over a shared file, and "what credentials did this run use?"
is answered by looking at the tree the run came from.

Every name ALE reads carries an ``ALE_`` prefix, so a checkout's credentials never collide
with the variables the operator's own tools read. API keys loaded here remain Host-side
behind the Gateway. Checkout-local subscription credentials are resolved separately and
also remain Host-side.

The same file supplies the variables Tasks declare (``[environment].env`` and the other
Harbor ``env`` tables), and only the file does: a variable merely exported in the
operator's shell, such as their own ``GITHUB_TOKEN``, never reaches a Sandbox unasked.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from ale.core.errors import ConfigError
from ale.core.lock import TaskVariableProvenance
from ale.core.task import ResolvedVariables, Task
from ale.core.taskspec import TaskVariables, VariableReference

__all__ = [
    "ENV_FILE",
    "PROVIDER_URLS",
    "find_env_file",
    "hf_token",
    "host_credentials",
    "is_credential_name",
    "load_env",
    "provider_credentials",
    "provider_endpoint",
    "read_env_file",
    "resolve_task_variables",
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


def read_env_file(path: Path) -> dict[str, str]:
    """The ``KEY=value`` lines of an environment file."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Read ``KEY=value`` lines into the process environment.

    Existing variables win by default: an explicit export on the command line should
    beat a file, not the other way round.
    """
    file = path or find_env_file()
    if file is None:
        return {}

    loaded = read_env_file(file)
    for key, value in loaded.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


#: A name that marks a credential, as Harbor recognizes one; ``MAX_TOKENS`` is a count.
_CREDENTIAL_NAME = re.compile(r"KEY|SECRET|TOKEN(?!S)|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)
#: Values too common to redact: replacing every ``1`` or ``true`` would corrupt the record.
_COMMON_VALUE = re.compile(r"\d*|true|false|yes|no", re.IGNORECASE)


def is_credential_name(name: str) -> bool:
    return bool(_CREDENTIAL_NAME.search(name))


def host_credentials(names: Iterable[str] = ()) -> dict[str, str]:
    """The Host's own credentials: every credential-named ``ALE_`` variable and ``names``.

    ``names`` are the variables a run configuration points its keys at instead, such as
    ``gateway.api_key_env``.
    """
    named = set(names)
    return {
        name: value
        for name, value in os.environ.items()
        if value and (name in named or (name.startswith("ALE_") and is_credential_name(name)))
    }


def resolve_task_variables(
    tasks: Iterable[Task], *, oracle: bool, protected: Mapping[str, str]
) -> None:
    """Resolve every Task's variables from the checkout's environment file, or fail.

    Runs before any image is built, Sandbox started or model called, and names every
    missing variable of every Task at once. ``[solution].env`` is resolved only for the
    oracle. A value equal to one of the ``protected`` Host credentials fails too: model
    and Judge keys reach a model through the Gateway only, never through a Task.
    """
    file = find_env_file()
    values = read_env_file(file) if file is not None else {}
    held = {value: name for name, value in protected.items()}
    problems: list[str] = []
    for task in tasks:
        task.variables, missing, leaked = _resolve(task.spec.variables, values, held, oracle)
        if missing:
            problems.append(f"{task.spec.name}: {', '.join(missing)} not set")
        problems.extend(f"{task.spec.name}: {item}" for item in leaked)
    if problems:
        source = str(file) if file is not None else "the checkout's .env (none found)"
        raise ConfigError(
            f"Task variables cannot be resolved from {source}:\n  " + "\n  ".join(problems)
        )


def _resolve(
    declared: TaskVariables, values: Mapping[str, str], held: Mapping[str, str], oracle: bool
) -> tuple[ResolvedVariables, list[str], list[str]]:
    tables: dict[str, dict[str, str]] = {}
    provenance: list[TaskVariableProvenance] = []
    secrets: set[str] = set()
    missing: list[str] = []
    leaked: list[str] = []
    for table, env in declared.tables:
        if table == "solution" and not oracle:
            continue
        resolved = tables.setdefault(table, {})
        for key, value in env.items():
            reference = VariableReference.parse(value)
            source: Literal["literal", "env_file", "default"] = "literal"
            if reference is not None:
                # Empty counts as unset, as a shell's ``${NAME:-default}`` treats it: an
                # empty line copied from an example file is not a credential.
                if found := values.get(reference.name, ""):
                    value, source = found, "env_file"
                    if (
                        is_credential_name(key) or is_credential_name(reference.name)
                    ) and not _COMMON_VALUE.fullmatch(found):
                        secrets.add(found)
                elif reference.default is not None:
                    value, source = reference.default, "default"
                else:
                    missing.append(f"{reference.name} ([{table}].env)")
                    continue
            if value in held:
                leaked.append(
                    f"[{table}].env {key} holds the Host credential {held[value]}, which "
                    "reaches a model through the Gateway only"
                )
            resolved[key] = value
            provenance.append(TaskVariableProvenance(table=table, name=key, source=source))  # type: ignore[arg-type]
    variables = TaskVariables(
        environment=tables["environment"],
        verifier=tables["verifier"],
        verifier_environment=tables.get("verifier.environment"),
        solution=tables.get("solution", {}),
    )
    return (
        ResolvedVariables(
            values=variables, secrets=tuple(sorted(secrets)), provenance=tuple(provenance)
        ),
        missing,
        leaked,
    )


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

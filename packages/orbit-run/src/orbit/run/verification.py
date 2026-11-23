"""Locate the framework-owned ``ale_verify`` package and gate runs on its Judges."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.metadata
import importlib.resources
import os
from collections.abc import Iterable
from pathlib import Path

from ale.core.config import VerificationConfig
from ale.core.episode import EventSink
from ale.core.errors import ConfigError, TaskDefinitionError
from ale.core.ids import content_hash
from ale.run.gateway.server import REQUEST_PATHS, Gateway
from ale.run.gateway.session import GatewaySession, Upstream
from ale.run.secrets import provider_endpoint
from ale_verify import ConfigurationError, endpoint_identity, judge_route, probe_judge

__all__ = [
    "installed_ale_verify",
    "judge_endpoints",
    "judge_kinds",
    "open_judge_sessions",
    "preflight_judges",
    "resolved_judges",
    "verifier_judge_config",
]

_JUDGE_KINDS = ("llm", "agent")

_IGNORED = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _hash_directory(directory: Path) -> str:
    entries: list[tuple[str, str]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or any(part in _IGNORED for part in path.parts):
            continue
        entries.append(
            (
                str(path.relative_to(directory)),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return content_hash(entries)


def installed_ale_verify() -> tuple[Path, str, str]:
    source = Path(str(importlib.resources.files("ale_verify")))
    if not source.is_dir():
        raise TaskDefinitionError("installed ale_verify package directory is unavailable")
    import fastjsonschema

    return (
        source,
        importlib.metadata.version("ale-verify"),
        content_hash(
            {
                "ale_verify": _hash_directory(source),
                "fastjsonschema": _hash_directory(Path(fastjsonschema.__file__).parent),
            }
        ),
    )


def judge_kinds(verify_dirs: Iterable[Path]) -> frozenset[str]:
    """The Judge kinds these verifiers call, from a syntax scan of their Python files.

    An imported Judge call whose kind is not a string literal could be either kind and
    counts as both. A file that does not parse is left for the verifier to report.
    """
    kinds: set[str] = set()
    for directory in verify_dirs:
        for path in sorted(directory.rglob("*.py")):
            try:
                tree = ast.parse(path.read_bytes(), filename=str(path))
            except (OSError, SyntaxError, ValueError):
                continue
            judge_names = set()
            module_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    module_names.update(
                        item.asname or item.name for item in node.names if item.name == "ale_verify"
                    )
                if isinstance(node, ast.ImportFrom) and node.module == "ale_verify":
                    judge_names.update(
                        item.asname or item.name for item in node.names if item.name == "judge"
                    )
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and (
                        (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == "judge"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id in module_names
                        )
                        or (isinstance(node.func, ast.Name) and node.func.id in judge_names)
                    )
                ):
                    continue
                kind = node.args[0] if node.args else None
                for keyword in node.keywords:
                    if keyword.arg == "kind":
                        kind = keyword.value
                if isinstance(kind, ast.Constant) and kind.value in _JUDGE_KINDS:
                    kinds.add(kind.value)
                elif not isinstance(kind, ast.Constant):
                    kinds.update(_JUDGE_KINDS)
    return frozenset(kinds)


def resolved_judges(config: VerificationConfig) -> dict[str, dict[str, str]]:
    """Each configured Judge as ``ale_verify`` reads it: its provider's endpoint and key name.

    A Judge names only its provider, so its key and URL resolve exactly as the Gateway's do,
    from ``ALE_<PROVIDER>_API_KEY`` and ``ALE_<PROVIDER>_BASE_URL``. An LLM Judge's endpoint
    carries its dialect's path, which is how ``ale_verify`` learns the protocol; an Agent
    Judge's protocol follows from its CLI.
    """
    resolved: dict[str, dict[str, str]] = {}
    for kind in _JUDGE_KINDS:
        section = getattr(config, kind)
        if section is None:
            continue
        key_var, base_url = provider_endpoint(section.provider)
        if kind == "llm":
            base_url += REQUEST_PATHS[section.dialect]
        resolved[kind] = {
            **section.model_dump(mode="json", exclude={"provider", "dialect"}),
            "base_url": base_url,
            "api_key_env": key_var,
        }
    return resolved


async def preflight_judges(verify_dirs: Iterable[Path], config: VerificationConfig) -> None:
    """Stop before any agent runs when a verifier's Judge cannot answer.

    A Judge reached only after an agent has spent its budget turns a configuration
    mistake into a batch of failed, paid-for episodes. One tiny request per Judge kind
    proves the endpoint, key and model first.
    """
    judges = resolved_judges(config)
    for kind in sorted(judge_kinds(verify_dirs)):
        if kind not in judges:
            raise ConfigError(
                f"a selected verifier calls the {kind} Judge, but verification.{kind} "
                "is not configured"
            )
        try:
            await asyncio.to_thread(probe_judge, kind, judges[kind])
        except ConfigurationError as error:
            raise ConfigError(f"{kind} Judge preflight failed: {error}") from error


#: The variable a verifier finds each Judge's Gateway token in.
_TOKEN_ENV = {"llm": "ALE_LLM_JUDGE_TOKEN", "agent": "ALE_AGENT_JUDGE_TOKEN"}


def judge_endpoints(config: VerificationConfig) -> dict[str, str]:
    """Each configured Judge's own endpoint, which is what its provenance records.

    A verifier reaches its Judges through the Gateway, so the address it observes is the
    Gateway's, on a port that changes every run.
    """
    return {
        kind: endpoint_identity(section["base_url"])
        for kind, section in resolved_judges(config).items()
    }


def open_judge_sessions(
    gateway: Gateway, config: VerificationConfig, *, episode_id: str, trace: EventSink
) -> dict[str, GatewaySession]:
    """Open one Gateway session per configured Judge, holding that Judge's own key.

    The key stays on the Host: the verifier receives only the session's token, so code the
    solver wrote cannot read a credential it could spend elsewhere. A Judge whose key is
    not set gets no session, and fails in the verifier if a Task calls it.
    """
    sessions: dict[str, GatewaySession] = {}
    for kind, section in resolved_judges(config).items():
        if not (key := os.environ.get(section["api_key_env"], "")):
            continue
        protocol, endpoint = judge_route(kind, section)
        sessions[kind] = gateway.open_session(
            GatewaySession(
                episode_id=episode_id,
                model=section["model"],
                purpose="judge",
                upstream=Upstream(dialect=protocol, endpoint=endpoint, api_key=key),
            ),
            trace=trace,
        )
    return sessions


def verifier_judge_config(
    config: VerificationConfig, tokens: dict[str, str], gateway_url: str | None
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """The Judge configuration and environment a verifier sees: the Gateway, not the key."""
    payload = resolved_judges(config)
    env: dict[str, str] = {}
    for kind, section in payload.items():
        token = tokens.get(kind)
        if token is None:
            continue
        if not gateway_url:
            raise ConfigError(
                f"the {kind} Judge needs the Gateway, which this sandbox cannot reach"
            )
        protocol, _ = judge_route(kind, section)
        base = gateway_url.rstrip("/")
        if kind == "llm":
            section["base_url"] = base + REQUEST_PATHS[protocol]
        elif section["adapter"] == "codex-cli":
            section["base_url"] = base + "/v1"
        else:
            section["base_url"] = base
        section["api_key_env"] = _TOKEN_ENV[kind]
        env[_TOKEN_ENV[kind]] = token
    return payload, env

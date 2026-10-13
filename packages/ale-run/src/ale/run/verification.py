"""Locate the framework-owned ``ale_verify`` package and gate runs on its Judges."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.metadata
import importlib.resources
from collections.abc import Iterable
from pathlib import Path

from ale.core.config import VerificationConfig
from ale.core.errors import ConfigError, TaskDefinitionError
from ale.core.ids import content_hash
from ale_verify import ConfigurationError, probe_judge

__all__ = ["installed_ale_verify", "judge_kinds", "preflight_judges"]

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
    return source, importlib.metadata.version("ale-verify"), _hash_directory(source)


def judge_kinds(verify_dirs: Iterable[Path]) -> frozenset[str]:
    """The Judge kinds these verifiers call, from a syntax scan of their Python files.

    A ``.judge(...)`` call whose kind is not a string literal could be either kind and
    counts as both. A file that does not parse is left for the verifier to report.
    """
    kinds: set[str] = set()
    for directory in verify_dirs:
        for path in sorted(directory.rglob("*.py")):
            try:
                tree = ast.parse(path.read_bytes(), filename=str(path))
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "judge"
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


async def preflight_judges(verify_dirs: Iterable[Path], config: VerificationConfig) -> None:
    """Stop before any agent runs when a verifier's Judge cannot answer.

    A Judge reached only after an agent has spent its budget turns a configuration
    mistake into a batch of failed, paid-for episodes. One tiny request per Judge kind
    proves the endpoint, key and model first.
    """
    for kind in sorted(judge_kinds(verify_dirs)):
        section = getattr(config, kind)
        if section is None:
            raise ConfigError(
                f"a selected verifier calls the {kind} Judge, but verification.{kind} "
                "is not configured"
            )
        try:
            await asyncio.to_thread(probe_judge, kind, section.model_dump(mode="json"))
        except ConfigurationError as error:
            raise ConfigError(f"{kind} Judge preflight failed: {error}") from error

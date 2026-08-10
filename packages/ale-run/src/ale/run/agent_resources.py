"""Resolve declared Skills and MCP servers into one deterministic episode resource set."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from pydantic import TypeAdapter, ValidationError

from ale.core.errors import AgentResourceConflictError, AgentResourceError
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, ResolvedSkill
from ale.core.ids import content_hash
from ale.core.lock import TaskSource
from ale.core.taskspec import (
    McpServer,
    McpSource,
    NetworkMode,
    NetworkPolicy,
    SkillSource,
    StdioMcpServer,
    StreamableHttpMcpServer,
    ToolProvision,
)

__all__ = ["continuation_fingerprint", "resolve_agent_resources"]


def continuation_fingerprint(
    *,
    harness: str,
    model: str,
    settings: object,
    resources_digest: str,
) -> str:
    """Bind native state to every definition value that can change its behavior."""
    return content_hash(
        {
            "harness": harness,
            "model": model,
            "settings": settings,
            "resources_digest": resources_digest,
        }
    )


def resolve_agent_resources(
    *,
    task: ToolProvision,
    agent_skills: tuple[SkillSource, ...] = (),
    agent_mcp_servers: tuple[McpSource, ...] = (),
    task_root: Path,
    task_source: TaskSource,
    network: NetworkPolicy | None = None,
) -> EffectiveAgentResources:
    """Resolve all declarations before a provider or sandbox is created."""
    skill_sources = [
        *agent_skills,
        *(
            source.model_copy(
                update={"origin": "task", "declared": source.path},
            )
            for source in task.skills
        ),
    ]
    skills = _merge_skills(
        [
            resolved
            for source in skill_sources
            for resolved in _resolve_skill_source(source, task_root, task_source)
        ]
    )
    mcp_sources = [
        *agent_mcp_servers,
        *(
            McpSource(
                path=source.path,
                origin="task",
                declared=source.path,
            )
            for source in task.mcp_servers
        ),
    ]
    mcp_servers = _merge_mcp(
        [
            _resolve_mcp_source(
                source,
                task_root=task_root,
                task_source=task_source,
                network=network or NetworkPolicy(),
            )
            for source in mcp_sources
        ]
    )
    digest = content_hash(
        [{"kind": "skill", "name": skill.name, "digest": skill.digest} for skill in skills]
        + [{"kind": "mcp", "name": server.name, "digest": server.digest} for server in mcp_servers]
    )
    return EffectiveAgentResources(skills=skills, mcp_servers=mcp_servers, digest=digest)


def _resolve_skill_source(
    source: SkillSource, task_root: Path, task_source: TaskSource
) -> tuple[ResolvedSkill, ...]:
    origin = source.origin or "run"
    declared = source.declared or source.path
    path = Path(source.path)
    if origin == "task":
        if path.is_absolute():
            raise AgentResourceError(f"Task Skill path must be relative: {declared}")
        root = task_root.resolve()
        path = (root / path).resolve()
        _inside(path, root, f"Task Skill {declared!r}")
        relative = path.relative_to(root)
        if relative.parts[:2] != ("tools", "skills"):
            raise AgentResourceError(f"Task Skill {declared!r} must be below tools/skills/")
    else:
        path = path.resolve()

    if not path.is_dir():
        raise AgentResourceError(f"Skill source is not a directory: {declared}")

    if (path / "SKILL.md").is_file():
        directories = (path,)
    else:
        children = tuple(
            child
            for child in sorted(path.iterdir())
            if not child.name.startswith(".") and child.is_dir()
        )
        if not children:
            raise AgentResourceError(f"Skill collection is empty: {declared}")
        missing = [child.name for child in children if not (child / "SKILL.md").is_file()]
        if missing:
            raise AgentResourceError(
                f"Skill collection {declared!r} has children without SKILL.md: {', '.join(missing)}"
            )
        directories = children

    reportable = origin == "task" and task_source.kind == "registry"
    version = task_source.commit if reportable else None
    reason = None if reportable else _reportability_reason(origin, task_source)
    return tuple(
        _resolved_skill(
            directory,
            origin=origin,
            declared=declared,
            reportable=reportable,
            source_version=version,
            reason=reason,
        )
        for directory in directories
    )


def _resolved_skill(
    path: Path,
    *,
    origin: str,
    declared: str,
    reportable: bool,
    source_version: str | None,
    reason: str | None,
) -> ResolvedSkill:
    digest, executables = _directory_digest(path, path)
    return ResolvedSkill(
        name=path.name,
        path=path,
        source_layers=(origin,),  # type: ignore[arg-type]
        declared_sources=(declared,),
        digest=digest,
        source_version=source_version,
        reportable=reportable,
        reportability_reason=reason,
        executable_files=executables,
    )


def _directory_digest(path: Path, source_root: Path) -> tuple[str, tuple[str, ...]]:
    hasher = hashlib.sha256()
    executables: list[str] = []

    def visit(directory: Path, prefix: PurePosixPath, stack: frozenset[Path]) -> None:
        real = directory.resolve()
        _inside(real, source_root.resolve(), f"Skill path {directory}")
        if real in stack:
            raise AgentResourceError(f"Skill contains a symlink cycle: {directory}")
        for entry in sorted(real.iterdir(), key=lambda item: item.name):
            target = entry.resolve()
            _inside(target, source_root.resolve(), f"Skill entry {entry}")
            relative = prefix / entry.name
            if target.is_dir():
                visit(target, relative, stack | {real})
                continue
            if not target.is_file():
                raise AgentResourceError(f"Skill entry is not a regular file: {entry}")
            executable = bool(target.stat().st_mode & 0o111)
            rel = relative.as_posix()
            hasher.update(rel.encode())
            hasher.update(b"\0x\0" if executable else b"\0-\0")
            hasher.update(target.read_bytes())
            hasher.update(b"\0")
            if executable:
                executables.append(rel)

    visit(path, PurePosixPath(), frozenset())
    return f"sha256:{hasher.hexdigest()}", tuple(executables)


def _merge_skills(skills: list[ResolvedSkill]) -> tuple[ResolvedSkill, ...]:
    merged: dict[str, ResolvedSkill] = {}
    for skill in skills:
        current = merged.get(skill.name)
        if current is None:
            merged[skill.name] = skill
            continue
        if current.digest != skill.digest:
            raise AgentResourceConflictError(
                f"Skill {skill.name!r} resolves to both {current.digest} and {skill.digest}"
            )
        layers = tuple(dict.fromkeys((*current.source_layers, *skill.source_layers)))
        sources = tuple(dict.fromkeys((*current.declared_sources, *skill.declared_sources)))
        merged[skill.name] = current.model_copy(
            update={
                "source_layers": layers,
                "declared_sources": sources,
                "reportable": current.reportable and skill.reportable,
                "reportability_reason": current.reportability_reason or skill.reportability_reason,
            }
        )
    return tuple(merged[name] for name in sorted(merged))


def _resolve_mcp_source(
    source: McpSource,
    *,
    task_root: Path,
    task_source: TaskSource,
    network: NetworkPolicy,
) -> ResolvedMcpServer:
    origin = source.origin or "run"
    declared = source.declared or source.path or source.builtin or ""
    if source.builtin:
        if source.builtin != "cua-desktop":
            raise AgentResourceError(f"unknown built-in MCP server: {source.builtin}")
        from ale.run.tools import resolved_cua_desktop

        resolved = resolved_cua_desktop()
        return resolved.model_copy(
            update={
                "source_layers": (origin,),
                "declared_sources": (declared,),
            }
        )

    assert source.path is not None
    path = Path(source.path)
    if origin == "task":
        if path.is_absolute():
            raise AgentResourceError(f"Task MCP path must be relative: {declared}")
        root = task_root.resolve()
        path = (root / path).resolve()
        _inside(path, root, f"Task MCP {declared!r}")
        relative = path.relative_to(root)
        if relative.parts[:2] != ("tools", "mcp"):
            raise AgentResourceError(f"Task MCP {declared!r} must be below tools/mcp/")
    else:
        path = path.resolve()
    if not path.is_file():
        raise AgentResourceError(f"MCP descriptor is not a file: {declared}")

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        server = TypeAdapter(McpServer).validate_python(raw)
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise AgentResourceError(f"invalid MCP descriptor {declared!r}: {exc}") from exc
    _check_network(server, network)

    staged_files = None
    if isinstance(server, StdioMcpServer):
        staged_files = path.parent

    external = isinstance(server, StreamableHttpMcpServer)
    reportable = origin == "task" and task_source.kind == "registry" and not external
    version = task_source.commit if reportable else None
    reason = (
        "external Streamable HTTP service has no immutable identity"
        if external
        else None
        if reportable
        else _reportability_reason(origin, task_source)
    )
    files_digest = _directory_digest(staged_files, staged_files)[0] if staged_files else None
    return ResolvedMcpServer(
        name=server.name,
        server=server,
        source_layers=(origin,),  # type: ignore[arg-type]
        declared_sources=(declared,),
        digest=content_hash({"server": server.model_dump(mode="json"), "files": files_digest}),
        source_version=version,
        reportable=reportable,
        reportability_reason=reason,
        staged_files=staged_files,
    )


def _check_network(server: McpServer, network: NetworkPolicy) -> None:
    if not isinstance(server, StreamableHttpMcpServer):
        return
    host = (urlparse(server.url).hostname or "").lower()
    if network.mode is NetworkMode.BLOCK:
        raise AgentResourceError(f"remote MCP {server.name!r} is blocked by task network policy")
    if network.mode is NetworkMode.ALLOWLIST and not any(
        host == allowed.lower() or host.endswith(f".{allowed.lower()}")
        for allowed in network.allowed_hosts
    ):
        raise AgentResourceError(
            f"remote MCP {server.name!r} host {host!r} is not in task allowed_hosts"
        )


def _merge_mcp(servers: list[ResolvedMcpServer]) -> tuple[ResolvedMcpServer, ...]:
    merged: dict[str, ResolvedMcpServer] = {}
    for server in servers:
        current = merged.get(server.name)
        if current is None:
            merged[server.name] = server
            continue
        if current.digest != server.digest:
            raise AgentResourceConflictError(
                f"MCP server {server.name!r} resolves to both {current.digest} and {server.digest}"
            )
        merged[server.name] = current.model_copy(
            update={
                "source_layers": tuple(
                    dict.fromkeys((*current.source_layers, *server.source_layers))
                ),
                "declared_sources": tuple(
                    dict.fromkeys((*current.declared_sources, *server.declared_sources))
                ),
                "reportable": current.reportable and server.reportable,
                "reportability_reason": current.reportability_reason or server.reportability_reason,
            }
        )
    return tuple(merged[name] for name in sorted(merged))


def _inside(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AgentResourceError(f"{label} escapes {root}") from exc


def _reportability_reason(origin: str, task_source: TaskSource) -> str:
    if origin == "task" and task_source.kind == "local":
        return "task resource came from a local task path"
    return f"{origin} local resource cannot be re-fetched"

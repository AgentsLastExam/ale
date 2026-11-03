"""Local Skill and MCP resource resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ale.core.errors import AgentResourceConflictError, AgentResourceError
from ale.core.lock import TaskSource
from ale.core.taskspec import (
    McpSource,
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    SkillSource,
    StdioMcpServer,
    StreamableHttpMcpServer,
    ToolProvision,
)
from ale.run.agent_resources import resolve_agent_resources

pytestmark = pytest.mark.unit


def source(kind: str = "git") -> TaskSource:
    if kind == "git":
        return TaskSource(
            kind="git",
            repo="https://example.com/tasks.git",
            commit="a" * 40,
            path="tasks/demo/hello",
        )
    return TaskSource(kind="local", path="/tmp/task")


def skill(path: Path, text: str = "name") -> Path:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(text)
    return path


def resolve(*paths: Path):
    return resolve_agent_resources(
        task=ToolProvision(),
        agent_skills=tuple(SkillSource(path=str(path), origin="run") for path in paths),
        task_source=source(),
    )


def stdio(name: str = "search", command: str = "/opt/search") -> StdioMcpServer:
    return StdioMcpServer(name=name, transport="stdio", command=command)


def remote(name: str = "remote") -> StreamableHttpMcpServer:
    return StreamableHttpMcpServer(
        name=name, transport="streamable-http", url="https://mcp.example.com/api"
    )


def test_single_skill_and_immediate_collection_resolve(tmp_path: Path) -> None:
    one = skill(tmp_path / "one")
    collection = tmp_path / "collection"
    skill(collection / "two")
    skill(collection / "three")
    skill(collection / ".hidden")

    resources = resolve(one, collection)

    assert [item.name for item in resources.skills] == ["one", "three", "two"]
    assert all(item.source_layers == ("run",) for item in resources.skills)
    assert all(not item.reportable for item in resources.skills)
    assert resources.digest.startswith("sha256:")


@pytest.mark.parametrize("layout", ["missing", "file", "empty", "partial", "recursive"])
def test_invalid_skill_layouts_fail(tmp_path: Path, layout: str) -> None:
    path = tmp_path / layout
    if layout == "file":
        path.write_text("no")
    elif layout == "empty":
        path.mkdir()
    elif layout == "partial":
        skill(path / "good")
        (path / "bad").mkdir()
    elif layout == "recursive":
        skill(path / "outer" / "inner")

    with pytest.raises(AgentResourceError):
        resolve(path)


def test_a_symlink_cannot_leave_the_skill(tmp_path: Path) -> None:
    directory = skill(tmp_path / "tool")
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (directory / "link").symlink_to(outside)

    with pytest.raises(AgentResourceError, match="escapes"):
        resolve(directory)


def test_digest_and_executable_metadata_track_file_mode(tmp_path: Path) -> None:
    directory = skill(tmp_path / "tool")
    script = directory / "run.sh"
    script.write_text("#!/bin/sh\n")
    before = resolve(directory).skills[0]

    script.chmod(script.stat().st_mode | 0o111)
    after = resolve(directory).skills[0]

    assert before.digest != after.digest
    assert after.executable_files == ("run.sh",)


def test_operator_layers_deduplicate_and_different_content_conflicts(tmp_path: Path) -> None:
    preset = skill(tmp_path / "preset" / "same", "same")
    run = skill(tmp_path / "run" / "same", "same")
    os.chmod(run / "SKILL.md", (preset / "SKILL.md").stat().st_mode)

    resources = resolve_agent_resources(
        task=ToolProvision(),
        agent_skills=(
            SkillSource(path=str(preset), origin="preset"),
            SkillSource(path=str(run), origin="run"),
        ),
        task_source=source(),
    )
    assert len(resources.skills) == 1
    assert resources.skills[0].source_layers == ("preset", "run")

    (run / "SKILL.md").write_text("different")
    with pytest.raises(AgentResourceConflictError):
        resolve(preset, run)


def test_a_task_skills_directory_is_passed_to_the_harness_and_counts_in_the_digest() -> None:
    plain = resolve_agent_resources(task=ToolProvision(), task_source=source())
    declared = resolve_agent_resources(
        task=ToolProvision(skills_dir="/skills"), task_source=source()
    )
    assert declared.skills_dir == "/skills"
    assert not declared.skills
    assert declared.digest != plain.digest


def test_task_servers_resolve_from_their_entries_without_any_upload() -> None:
    resources = resolve_agent_resources(
        task=ToolProvision(mcp_servers=(stdio(), remote())),
        task_source=source(),
        network=NetworkPolicy(mode=NetworkMode.OPEN),
    )

    assert [server.name for server in resources.mcp_servers] == ["remote", "search"]
    external, local = resources.mcp_servers
    assert local.source_layers == ("task",)
    assert local.staged_files is None
    assert local.reportable and local.source_version == "a" * 40
    assert not external.reportable
    assert "no immutable identity" in (external.reportability_reason or "")


def test_a_local_task_server_is_visible_but_not_reportable() -> None:
    resources = resolve_agent_resources(
        task=ToolProvision(mcp_servers=(stdio(),)), task_source=source("local")
    )
    assert not resources.mcp_servers[0].reportable
    assert "not committed in a git repository" in (
        resources.mcp_servers[0].reportability_reason or ""
    )


def mcp(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def resolve_descriptors(*paths: Path, network: NetworkPolicy | None = None):
    return resolve_agent_resources(
        task=ToolProvision(),
        agent_mcp_servers=tuple(McpSource(path=str(path), origin="run") for path in paths),
        task_source=source(),
        network=network,
    )


def test_strict_stdio_and_streamable_http_descriptors(tmp_path: Path) -> None:
    stdio_descriptor = mcp(
        tmp_path / "stdio/stdio.toml",
        """
schema_version = 1
name = "files"
transport = "stdio"
command = "python3"
args = ["{mcp}/server.py"]
cwd = "{mcp}"
[environment]
MODE = "read-only"
""",
    )
    http_descriptor = mcp(
        tmp_path / "http/http.toml",
        """
schema_version = 1
name = "remote"
transport = "streamable-http"
url = "https://mcp.example.com/api"
""",
    )

    resources = resolve_descriptors(
        stdio_descriptor, http_descriptor, network=NetworkPolicy(mode=NetworkMode.OPEN)
    )

    assert [server.name for server in resources.mcp_servers] == ["files", "remote"]
    assert resources.mcp_servers[0].server.transport == "stdio"
    assert resources.mcp_servers[0].server.args == ("{mcp}/server.py",)
    assert resources.mcp_servers[0].server.cwd == "{mcp}"
    assert resources.mcp_servers[0].staged_files == stdio_descriptor.parent
    assert resources.mcp_servers[1].server.transport == "streamable-http"


@pytest.mark.parametrize(
    "body",
    [
        """
schema_version = 1
name = "bad"
transport = "sse"
url = "https://mcp.example.com"
""",
        """
schema_version = 1
name = "bad"
transport = "stdio"
command = "python3"
url = "https://mcp.example.com"
""",
        """
schema_version = 1
name = "bad"
transport = "streamable-http"
url = "https://mcp.example.com"
headers = { Authorization = "Bearer secret" }
""",
        """
schema_version = 2
name = "bad"
transport = "stdio"
command = "python3"
""",
    ],
)
def test_invalid_transport_auth_and_unknown_fields_fail(tmp_path: Path, body: str) -> None:
    with pytest.raises(AgentResourceError, match="invalid MCP descriptor"):
        resolve_descriptors(
            mcp(tmp_path / "bad.toml", body), network=NetworkPolicy(mode=NetworkMode.OPEN)
        )


def test_remote_mcp_obeys_network_policy() -> None:
    def resolved(network: NetworkPolicy | None = None):
        return resolve_agent_resources(
            task=ToolProvision(mcp_servers=(remote(),)),
            task_source=source(),
            network=network,
        )

    def allowlist(*hosts: str) -> NetworkPolicy:
        return NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=hosts)

    with pytest.raises(AgentResourceError, match="blocked"):
        resolved()
    with pytest.raises(AgentResourceError, match="allowed_hosts"):
        resolved(allowlist("other.example.com"))
    assert resolved(allowlist("example.com")).mcp_servers


@pytest.mark.parametrize("operating_system", list(OperatingSystem))
def test_builtin_cua_desktop_is_opt_in_and_deduplicates(operating_system: OperatingSystem) -> None:
    assert not resolve_descriptors().mcp_servers

    resources = resolve_agent_resources(
        task=ToolProvision(),
        agent_mcp_servers=(
            McpSource(
                builtin="cua-desktop",
                origin="run",
                declared="cua-desktop",
            ),
            McpSource(
                builtin="cua-desktop",
                origin="cli",
                declared="cua-desktop",
            ),
        ),
        task_source=source(),
        operating_system=operating_system,
    )
    assert [server.name for server in resources.mcp_servers] == ["cua-desktop"]
    assert resources.mcp_servers[0].source_layers == ("run", "cli")
    server = resources.mcp_servers[0].server
    assert isinstance(server, StdioMcpServer)
    assert server.command == (
        "python.exe" if operating_system is OperatingSystem.WINDOWS else "python3"
    )

    with pytest.raises(AgentResourceError, match="unknown built-in"):
        resolve_agent_resources(
            task=ToolProvision(),
            agent_mcp_servers=(McpSource(builtin="missing"),),
            task_source=source(),
        )


def test_an_operator_server_replaces_a_task_server_of_the_same_name(tmp_path: Path) -> None:
    descriptor = mcp(
        tmp_path / "search.toml",
        """
schema_version = 1
name = "search"
transport = "stdio"
command = "two"
""",
    )

    resources = resolve_agent_resources(
        task=ToolProvision(mcp_servers=(stdio(command="one"),)),
        agent_mcp_servers=(McpSource(path=str(descriptor), origin="run"),),
        task_source=source(),
    )

    (server,) = resources.mcp_servers
    assert server.source_layers == ("run",)
    assert isinstance(server.server, StdioMcpServer) and server.server.command == "two"


def test_operators_cannot_disagree_about_one_server(tmp_path: Path) -> None:
    first, second = (
        mcp(
            tmp_path / f"{command}.toml",
            f"""
schema_version = 1
name = "duplicate"
transport = "stdio"
command = "{command}"
""",
        )
        for command in ("one", "two")
    )

    with pytest.raises(AgentResourceConflictError, match="duplicate"):
        resolve_descriptors(first, second)

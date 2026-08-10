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
    SkillSource,
    TaskMcpSource,
    ToolProvision,
)
from ale.run.agent_resources import resolve_agent_resources

pytestmark = pytest.mark.unit


def source(kind: str = "registry") -> TaskSource:
    if kind == "registry":
        return TaskSource(
            kind="registry",
            repo="https://example.com/tasks.git",
            commit="a" * 40,
            path="tasks/demo/hello",
        )
    return TaskSource(kind="local", path="/tmp/task")


def skill(path: Path, text: str = "name") -> Path:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(text)
    return path


def resolve(task_root: Path, *paths: str, kind: str = "registry"):
    return resolve_agent_resources(
        task=ToolProvision(
            skills=tuple(SkillSource(path=f"tools/skills/{path}") for path in paths)
        ),
        task_root=task_root,
        task_source=source(kind),
    )


def test_single_skill_and_immediate_collection_resolve(tmp_path: Path) -> None:
    task = tmp_path / "task"
    skill(task / "tools/skills/one")
    collection = task / "tools/skills/collection"
    skill(collection / "two")
    skill(collection / "three")
    skill(collection / ".hidden")

    resources = resolve(task, "one", "collection")

    assert [item.name for item in resources.skills] == ["one", "three", "two"]
    assert all(item.source_layers == ("task",) for item in resources.skills)
    assert all(item.source_version == "a" * 40 for item in resources.skills)
    assert resources.digest.startswith("sha256:")


@pytest.mark.parametrize("layout", ["missing", "file", "empty", "partial", "recursive"])
def test_invalid_skill_layouts_fail(tmp_path: Path, layout: str) -> None:
    task = tmp_path / "task"
    task.mkdir()
    path = task / "tools/skills" / layout
    path.parent.mkdir(parents=True)
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
        resolve(task, layout)


def test_task_path_and_symlink_cannot_escape_or_enter_invisible_content(
    tmp_path: Path,
) -> None:
    task = tmp_path / "task"
    task.mkdir()
    outside = skill(tmp_path / "outside")
    (task / "tools/skills").mkdir(parents=True)
    (task / "tools/skills/escape").symlink_to(outside, target_is_directory=True)
    skill(task / "verify" / "secret")

    with pytest.raises(AgentResourceError, match="escapes"):
        resolve(task, "escape")
    with pytest.raises(AgentResourceError, match="tools/skills"):
        resolve_agent_resources(
            task=ToolProvision(skills=(SkillSource(path="verify/secret"),)),
            task_root=task,
            task_source=source(),
        )
    with pytest.raises(AgentResourceError, match="relative"):
        resolve_agent_resources(
            task=ToolProvision(skills=(SkillSource(path=str(outside)),)),
            task_root=task,
            task_source=source(),
        )


def test_digest_and_executable_metadata_track_file_mode(tmp_path: Path) -> None:
    task = tmp_path / "task"
    directory = skill(task / "tools/skills/tool")
    script = directory / "run.sh"
    script.write_text("#!/bin/sh\n")
    before = resolve(task, "tool").skills[0]

    script.chmod(script.stat().st_mode | 0o111)
    after = resolve(task, "tool").skills[0]

    assert before.digest != after.digest
    assert after.executable_files == ("run.sh",)


def test_identical_names_deduplicate_and_different_content_conflicts(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task_skill = skill(task / "tools/skills/same", "same")
    run = tmp_path / "run" / "same"
    skill(run, "same")
    os.chmod(run / "SKILL.md", (task_skill / "SKILL.md").stat().st_mode)

    resources = resolve_agent_resources(
        task=ToolProvision(skills=(SkillSource(path="tools/skills/same"),)),
        agent_skills=(
            SkillSource(
                path=str(run),
                origin="run",
                declared=str(run),
            ),
        ),
        task_root=task,
        task_source=source(),
    )
    assert len(resources.skills) == 1
    assert resources.skills[0].source_layers == ("run", "task")
    assert not resources.skills[0].reportable

    (run / "SKILL.md").write_text("different")
    with pytest.raises(AgentResourceConflictError):
        resolve_agent_resources(
            task=ToolProvision(skills=(SkillSource(path="tools/skills/same"),)),
            agent_skills=(SkillSource(path=str(run), origin="run"),),
            task_root=task,
            task_source=source(),
        )


def test_local_task_skill_is_visible_but_not_reportable(tmp_path: Path) -> None:
    task = tmp_path / "task"
    skill(task / "tools/skills/local")
    resolved = resolve(task, "local", kind="local").skills[0]
    assert not resolved.reportable
    assert "local task path" in (resolved.reportability_reason or "")


def mcp(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def resolve_mcp(
    task_root: Path,
    *sources: TaskMcpSource,
    network: NetworkPolicy | None = None,
):
    return resolve_agent_resources(
        task=ToolProvision(mcp_servers=sources),
        task_root=task_root,
        task_source=source(),
        network=network,
    )


def test_strict_stdio_and_streamable_http_descriptors(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    mcp(
        task / "tools/mcp/stdio.toml",
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
    mcp(
        task / "tools/mcp/http.toml",
        """
schema_version = 1
name = "remote"
transport = "streamable-http"
url = "https://mcp.example.com/api"
""",
    )

    resources = resolve_mcp(
        task,
        TaskMcpSource(path="tools/mcp/stdio.toml"),
        TaskMcpSource(path="tools/mcp/http.toml"),
        network=NetworkPolicy(mode=NetworkMode.OPEN),
    )

    assert [server.name for server in resources.mcp_servers] == ["files", "remote"]
    assert resources.mcp_servers[0].server.transport == "stdio"
    assert resources.mcp_servers[0].server.args == ("{mcp}/server.py",)
    assert resources.mcp_servers[0].server.cwd == "{mcp}"
    assert resources.mcp_servers[0].staged_files == task / "tools/mcp"
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
    task = tmp_path / "task"
    task.mkdir()
    (task / "tools/mcp").mkdir(parents=True)
    mcp(task / "tools/mcp/bad.toml", body)

    with pytest.raises(AgentResourceError, match="invalid MCP descriptor"):
        resolve_mcp(
            task,
            TaskMcpSource(path="tools/mcp/bad.toml"),
            network=NetworkPolicy(mode=NetworkMode.OPEN),
        )


def test_remote_mcp_obeys_network_policy(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    mcp(
        task / "tools/mcp/remote.toml",
        """
schema_version = 1
name = "remote"
transport = "streamable-http"
url = "https://mcp.example.com/api"
""",
    )
    declaration = TaskMcpSource(path="tools/mcp/remote.toml")

    with pytest.raises(AgentResourceError, match="blocked"):
        resolve_mcp(task, declaration)
    with pytest.raises(AgentResourceError, match="allowed_hosts"):
        resolve_mcp(
            task,
            declaration,
            network=NetworkPolicy(
                mode=NetworkMode.ALLOWLIST,
                allowed_hosts=("other.example.com",),
            ),
        )
    assert resolve_mcp(
        task,
        declaration,
        network=NetworkPolicy(
            mode=NetworkMode.ALLOWLIST,
            allowed_hosts=("example.com",),
        ),
    ).mcp_servers


def test_builtin_cua_desktop_is_opt_in_and_deduplicates(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    assert not resolve_mcp(task).mcp_servers

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
        task_root=task,
        task_source=source(),
    )
    assert [server.name for server in resources.mcp_servers] == ["cua-desktop"]
    assert resources.mcp_servers[0].source_layers == ("run", "cli")

    with pytest.raises(AgentResourceError, match="unknown built-in"):
        resolve_agent_resources(
            task=ToolProvision(),
            agent_mcp_servers=(McpSource(builtin="missing"),),
            task_root=task,
            task_source=source(),
        )


def test_same_name_different_mcp_definitions_conflict(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    mcp(
        task / "tools/mcp/first.toml",
        """
schema_version = 1
name = "duplicate"
transport = "stdio"
command = "one"
""",
    )
    second = mcp(
        tmp_path / "second.toml",
        """
schema_version = 1
name = "duplicate"
transport = "stdio"
command = "two"
""",
    )

    with pytest.raises(AgentResourceConflictError, match="duplicate"):
        resolve_agent_resources(
            task=ToolProvision(mcp_servers=(TaskMcpSource(path="tools/mcp/first.toml"),)),
            agent_mcp_servers=(McpSource(path=str(second), origin="run"),),
            task_root=task,
            task_source=source(),
        )

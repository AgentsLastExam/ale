"""A sandbox-side autonomous agent must consume both a Task Skill and Task MCP."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from ale.core.harness import AgentRun, HarnessSession, TrajectoryParseContext
from ale.core.lock import TaskSource
from ale.core.sandbox import Identity, Sandbox
from ale.core.trajectory import (
    AtifAgent,
    AtifObservation,
    AtifObservationResult,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.core.verdict import Status
from ale.run.agent_resources import resolve_agent_resources
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

from .conftest import IMAGE

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


class ResourceProbeHarness(ClaudeCodeHarness):
    """A deterministic autonomous agent exercising the same installed resources."""

    name = "resource-probe"
    logs = ("resource-probe.json",)

    async def install(self, sandbox: Sandbox) -> str:
        return "1"

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        script = r"""
import json
import os
import re
import subprocess
from pathlib import Path

home = Path(os.environ["ALE_HOME"])
skill_path = home / ".claude-config/skills/resource-proof/SKILL.md"
skill = skill_path.read_text(encoding="utf-8")
template = re.search(r"Write exactly `([^`]+)`", skill).group(1)
nonce = (home / "input/nonce.txt").read_text(encoding="utf-8").strip()

config = json.loads((home / ".claude-config/mcp.json").read_text(encoding="utf-8"))
server = config["mcpServers"]["task-proof"]
requests = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "resource-probe", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "derive_fragment", "arguments": {"nonce": nonce}},
    },
]
process = subprocess.run(
    [server["command"], *server.get("args", [])],
    cwd=server.get("cwd") or str(home),
    env=os.environ | server.get("env", {}),
    input="".join(json.dumps(item) + "\n" for item in requests),
    text=True,
    capture_output=True,
    check=True,
)
replies = [json.loads(line) for line in process.stdout.splitlines()]
assert replies[1]["result"]["tools"][0]["name"] == "derive_fragment"
fragment = replies[2]["result"]["structuredContent"]["fragment"]
answer = template.replace("<nonce>", nonce).replace("<fragment>", fragment)
(home / "output/result.txt").write_text(answer, encoding="utf-8")
(home / "resource-probe.json").write_text(
    json.dumps(
        {
            "skill_path": str(skill_path),
            "server": "task-proof",
            "tool": "derive_fragment",
            "fragment": fragment,
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
"""
        result = await sandbox.exec(
            ["python3", "-c", script],
            env={"ALE_HOME": session.home},
            identity=Identity.AGENT,
            timeout_sec=timeout_sec,
        )
        return AgentRun(
            exit_code=result.exit_code,
            final_message=result.stderr.strip() or result.stdout.strip() or None,
        )

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        payload = json.loads((context.logs_dir / "resource-probe.json").read_text())
        builder = TrajectoryBuilder(
            trajectory_id=context.trajectory_id,
            agent=AtifAgent(
                name=self.name,
                version=context.agent_version,
                model_name=context.model or None,
            ),
        )
        builder.add(source="user", message=context.instruction)
        builder.add(
            source="agent",
            message=context.final_message or "",
            tool_calls=[
                AtifToolCall(
                    tool_call_id="resource-probe-1",
                    function_name="mcp__task-proof__derive_fragment",
                    arguments={"nonce": "A1B2C3D4"},
                    extra={
                        "ale": {
                            "mcp": {
                                "server": payload["server"],
                                "tool": payload["tool"],
                            }
                        }
                    },
                )
            ],
            observation=AtifObservation(
                results=[
                    AtifObservationResult(
                        source_call_id="resource-probe-1",
                        content=payload["fragment"],
                    )
                ]
            ),
        )
        return builder.build()


def _write_task(root: Path) -> Path:
    root.mkdir()
    task = root / "tasks" / "resource_injection"
    for directory in (
        task / "image",
        task / "tools" / "mcp",
        task / "tools" / "skills" / "resource-proof",
        task / "setup",
        task / "verify",
        task / "oracle",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (task / "task.yaml").write_text(
        textwrap.dedent("""
        spec_type: core/v1
        name: resource-injection
        image: {{ kind: container }}
        resources: {{ cpus: 1, memory_mb: 512 }}
        network: {{ mode: block }}
        timeouts: {{ setup: 60, agent: 60, verify: 60 }}
        tools:
          skills: [{{ path: tools/skills/resource-proof }}]
          mcp_servers: [{{ path: tools/mcp/task-proof.toml }}]
        artifacts: [/home/user/output]
        """)
        .strip()
        .replace("{{", "{")
        .replace("}}", "}")
    )
    (task / "instruction.md").write_text("Use the injected Skill and MCP to complete the proof.")
    (task / "tools" / "skills" / "resource-proof" / "SKILL.md").write_text(
        textwrap.dedent("""
        ---
        name: resource-proof
        description: Resource injection proof.
        ---
        Read `/home/user/input/nonce.txt`, call `task-proof.derive_fragment`, then
        Write exactly `SKILL-R7::<nonce>::<fragment>` to the requested output.
        """).strip()
    )
    (task / "tools" / "mcp" / "task-proof.toml").write_text(
        textwrap.dedent("""
        schema_version = 1
        name = "task-proof"
        transport = "stdio"
        command = "python3"
        args = ["{mcp}/task_proof_mcp.py"]
        """).strip()
    )
    (task / "tools" / "mcp" / "task_proof_mcp.py").write_text(
        textwrap.dedent("""
        import json, secrets, sys
        from pathlib import Path

        nonce_path = Path("/home/user/input/nonce.txt")
        receipt = Path("/home/user/output/mcp-call.json")
        for line in sys.stdin:
            message = json.loads(line)
            request_id = message.get("id")
            if request_id is None:
                continue
            method = message.get("method")
            if method == "initialize":
                payload = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "task-proof", "version": "1"},
                }
            elif method == "tools/list":
                payload = {"tools": [{"name": "derive_fragment", "inputSchema": {
                    "type": "object",
                    "properties": {"nonce": {"type": "string"}},
                    "required": ["nonce"],
                }}]}
            else:
                nonce = message["params"]["arguments"]["nonce"]
                assert nonce == nonce_path.read_text().strip()
                fragment = secrets.token_hex(12).upper()
                receipt.write_text(json.dumps({"nonce": nonce, "fragment": fragment}))
                payload = {
                    "content": [{"type": "text", "text": fragment}],
                    "structuredContent": {"fragment": fragment},
                }
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": payload}),
                  flush=True)
        """).strip()
    )
    (task / "image" / "Dockerfile").write_text(
        f"FROM {IMAGE}\n"
        "RUN mkdir -p /home/user/input /home/user/output "
        "&& chown -R user:user /home/user/input /home/user/output "
        "\n"
    )
    (task / "setup" / "run.sh").write_text(
        "#!/bin/bash\nset -e\nmkdir -p /home/user/input /home/user/output\n"
        "printf 'A1B2C3D4' > /home/user/input/nonce.txt\n"
    )
    (task / "verify" / "run.sh").write_text(
        textwrap.dedent("""
        #!/bin/bash
        set -e
        python3 - <<'PY'
        import json
        from pathlib import Path

        from ale_verify import CheckResult, Verification

        nonce = Path("/home/user/input/nonce.txt").read_text()
        answer = Path("/home/user/output/result.txt").read_text()
        receipt = json.loads(Path("/home/user/output/mcp-call.json").read_text())
        fragment = receipt.get("fragment", "")
        ok = answer == f"SKILL-R7::{nonce}::{fragment}"
        ok = ok and receipt.get("nonce") == nonce and len(fragment) == 24
        verification = Verification()
        verification.check("reward", CheckResult(float(ok)))
        verification.write()
        PY
        """).strip()
    )
    (task / "oracle" / "run.sh").write_text("#!/bin/bash\nexit 0\n")
    entries = (
        task / "setup" / "run.sh",
        task / "verify" / "run.sh",
        task / "oracle" / "run.sh",
    )
    for entry in entries:
        entry.chmod(0o755)
    return task


@pytest.mark.asyncio
async def test_autonomous_agent_uses_task_skill_and_mcp_to_pass(tmp_path: Path) -> None:
    task_root = _write_task(tmp_path / "repo")
    task = load_tasks(task_root)[0]
    resources = resolve_agent_resources(
        task=task.spec.tools,
        task_root=task_root,
        task_source=TaskSource(kind="local", path=str(task_root)),
        network=task.spec.network,
    )

    result = await run_episode(
        task,
        StandardEnvironment(ResourceProbeHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        agent_resources=resources,
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    call = trajectory.steps[1].tool_calls[0]
    assert call.extra["ale"]["mcp"] == {
        "server": "task-proof",
        "tool": "derive_fragment",
    }

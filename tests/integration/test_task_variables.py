"""Task variables reach exactly the commands their tables name, on every backend, unrecorded.

Every stage of one Task prints which variables it received. The Task's credential comes from
the checkout's ``.env``; it must reach setup, the oracle and the verifier, and no file the
episode leaves on the Host may hold it. A Run's ``[agent.env]`` reaches every turn of the
agent, and no verifier, under the same rules.
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orbit.core.config import RunConfig
from orbit.core.lock import RunLock, TaskSource
from orbit.core.trace import read_jsonl
from orbit.core.verdict import Status
from orbit.run.episode import EpisodeResult, run_episode
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from orbit.run.providers import ProviderRegistry
from orbit.run.providers.qemu import QemuProvider
from orbit.run.secrets import resolve_task_variables
from orbit.run.tasksets.manifest import ManifestTask, load_task_folder
from tests.support import (
    CONTAINER_BASE,
    VM_BASE,
    VM_RESOURCES,
    ScriptedCliHarness,
    provider_registry,
    write_task,
)

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

NAMES = ("PLAIN", "REGION", "SERVICE_TOKEN", "SHARED", "VERIFY_DATA", "SOLUTION_ONLY", "OWN")
OBSERVE = (
    "import json, os, sys\n"
    f"seen = {{name: os.environ.get(name) for name in {NAMES!r}}}\n"
    "print('ORBIT-ENV ' + json.dumps({'stage': sys.argv[1], 'env': seen}, sort_keys=True))\n"
)
VERIFY = (
    "import os\n"
    "from orbit_verify import write\n"
    "write({'reward': 1.0, 'seen': os.environ.get('SERVICE_TOKEN', '')})\n"
)
LINUX = {
    "environment/setup/setup.sh": (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 observe.py setup\n"
        'echo "setup stderr: ${SERVICE_TOKEN}" >&2\n'
    ),
    "solution/solve.sh": ("#!/usr/bin/env bash\nset -euo pipefail\npython3 observe.py oracle\n"),
    "tests/test.sh": (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 observe.py verify\n"
        'exec "$ORBIT_VERIFY_PYTHON" verify.py\n'
    ),
}
WINDOWS = {
    "environment/setup/setup.ps1": (
        "python.exe .\\observe.py setup\nif ($LASTEXITCODE) { exit $LASTEXITCODE }\n"
        '[Console]::Error.WriteLine("setup stderr: $env:SERVICE_TOKEN")\n'
    ),
    "solution/solve.ps1": "python.exe .\\observe.py oracle\nexit $LASTEXITCODE\n",
    "tests/test.ps1": (
        "python.exe .\\observe.py verify\nif ($LASTEXITCODE) { exit $LASTEXITCODE }\n"
        "& $env:ORBIT_VERIFY_PYTHON .\\verify.py\nexit $LASTEXITCODE\n"
    ),
}
ENVIRONMENT = {
    "PLAIN": "plain",
    "REGION": "${REGION:-eu}",
    "SERVICE_TOKEN": "${SERVICE_TOKEN}",
    "SHARED": "environment",
}
SETUP = {"PLAIN": "plain", "REGION": "eu", "SERVICE_TOKEN": "[REDACTED]", "SHARED": "environment"}


def _task(root: Path, kind: str, verifier: dict[str, Any]) -> ManifestTask:
    windows = kind == "windows"
    config: dict[str, Any] = {
        "environment": {"env": ENVIRONMENT},
        "verifier": {"env": {"SHARED": "verifier", "VERIFY_DATA": "${VERIFY_DATA}"}, **verifier},
        "solution": {"env": {"SOLUTION_ONLY": "solution"}},
    }
    if kind == "vm":
        config |= {"environment": {"env": ENVIRONMENT, **VM_RESOURCES, "image": VM_BASE}}
    if windows:
        config |= {
            "environment": {
                "env": ENVIRONMENT,
                "os": "windows",
                "image": "local/windows-variables",
                "cpus": 2,
                "memory_mb": 8192,
            },
        }
    write_task(
        root,
        name="agentslastexam/variables",
        config=config,
        dockerfile=None if kind in ("vm", "windows") else f"FROM {CONTAINER_BASE}\n",
        test=None,
        solve=None,
    )
    for relative, body in (WINDOWS if windows else LINUX).items():
        entry = root / relative
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(body)
        entry.chmod(0o755)
        (entry.parent / "observe.py").write_text(OBSERVE)
    (root / "tests/verify.py").write_text(VERIFY)
    return load_task_folder(root)


def _observed(result: EpisodeResult) -> dict[str, dict[str, str]]:
    """What each stage printed it received, as the execution trace recorded it."""
    seen: dict[str, dict[str, str]] = {}
    for record in read_jsonl(result.run_dir / "trace.execution.jsonl").records:
        if record.get("kind") != "command_finished":
            continue
        for line in (record["stdout"].get("inline") or "").splitlines():
            if line.startswith("ORBIT-ENV "):
                event = json.loads(line.removeprefix("ORBIT-ENV "))
                seen[event["stage"]] = {k: v for k, v in event["env"].items() if v is not None}
    return seen


def _holding(root: Path, secret: str) -> list[str]:
    return [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and secret.encode() in path.read_bytes()
    ]


def _registry(kind: str, tmp_path: Path) -> ProviderRegistry:
    if kind != "windows":
        return ProviderRegistry(RunConfig())
    image = Path(os.environ.get("ORBIT_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ORBIT_TEST_WINDOWS_IMAGE to an Orbit Windows base disk")
    return provider_registry(QemuProvider(image=image, overlay_dir=tmp_path / "qemu"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "verifier", "verified"),
    [
        ("container", {}, {**SETUP, "SHARED": "verifier", "VERIFY_DATA": "verify-data"}),
        (
            "container",
            {"environment_mode": "separate"},
            {**SETUP, "SHARED": "verifier", "VERIFY_DATA": "verify-data"},
        ),
        (
            "container",
            {"environment": {"cpus": 1, "memory_mb": 1024, "env": {"OWN": "own"}}},
            {"OWN": "own", "SHARED": "verifier", "VERIFY_DATA": "verify-data"},
        ),
        pytest.param(
            "vm",
            {},
            {**SETUP, "SHARED": "verifier", "VERIFY_DATA": "verify-data"},
            marks=pytest.mark.needs_kvm,
        ),
        pytest.param(
            "windows",
            {},
            {**SETUP, "SHARED": "verifier", "VERIFY_DATA": "verify-data"},
            marks=pytest.mark.needs_kvm,
        ),
    ],
    ids=["container-shared", "container-separate", "container-own", "vm", "windows"],
)
async def test_each_command_receives_its_tables_and_no_record_keeps_the_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    kind: str,
    verifier: dict[str, Any],
    verified: dict[str, str],
) -> None:
    if kind == "vm":
        request.getfixturevalue("docker_buildx")
        request.getfixturevalue("kvm_host")
    registry = _registry(kind, tmp_path)
    secret = f"tok-{uuid.uuid4().hex}"
    env_file = tmp_path / "checkout.env"
    env_file.write_text(f"SERVICE_TOKEN={secret}\nVERIFY_DATA=verify-data\nREGION=\n")
    monkeypatch.setenv("ORBIT_ENV_FILE", str(env_file))
    task = _task(tmp_path / "task", kind, verifier)
    resolve_task_variables([task], oracle=True, protected={})
    harness = OracleHarness()
    settings = RunConfig()
    result = await run_episode(
        task,
        EpisodeProtocol(harness),
        registry,
        run_dir=tmp_path / "runs",
        provenance=ProvenanceInputs(
            source=TaskSource(kind="local", path=str(task.folder.root)),
            agent=agent_provenance(harness, ""),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert _observed(result) == {
        "setup": SETUP,
        "oracle": {**SETUP, "SOLUTION_ONLY": "solution"},
        "verify": verified,
    }
    assert _holding(result.run_dir, secret) == []
    trace = (result.run_dir / "trace.execution.jsonl").read_text()
    assert "setup stderr: [REDACTED]" in trace
    recorded = json.loads((result.run_dir / "verification.json").read_text())
    assert recorded["seen"] == ("" if "OWN" in verified else "[REDACTED]")
    lock = RunLock.model_validate_json((result.run_dir / "lock.json").read_text())
    sources = {(item.table, item.name): item.source for item in lock.task.variables}
    assert sources[("environment", "SERVICE_TOKEN")] == "env_file"
    assert sources[("environment", "REGION")] == "default"
    assert sources[("solution", "SOLUTION_ONLY")] == "literal"


@pytest.mark.asyncio
async def test_a_failure_message_does_not_carry_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = f"tok-{uuid.uuid4().hex}"
    env_file = tmp_path / "checkout.env"
    env_file.write_text(f"SERVICE_TOKEN={secret}\nVERIFY_DATA=verify-data\n")
    monkeypatch.setenv("ORBIT_ENV_FILE", str(env_file))
    task = _task(tmp_path / "task", "container", {})
    (task.folder.root / "environment/setup/setup.sh").write_text(
        '#!/usr/bin/env bash\necho "cannot reach the service with ${SERVICE_TOKEN}" >&2\nexit 3\n'
    )
    resolve_task_variables([task], oracle=True, protected={})
    result = await run_episode(
        task, EpisodeProtocol(OracleHarness()), ProviderRegistry(RunConfig()),
        run_dir=tmp_path / "runs",
    )  # fmt: skip
    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.failure is not None
    assert "cannot reach the service with [REDACTED]" in result.verdict.failure.message
    assert _holding(result.run_dir, secret) == []


AGENT_NAMES = ("REGION", "WANDB_API_KEY", "WANDB_MODE", "SHARED")


def _observe(stage: str) -> str:
    """A command that prints which of ``AGENT_NAMES`` it received, as ``stage``."""
    return "python3 -c " + shlex.quote(
        "import json, os\n"
        f"seen = {{name: os.environ.get(name) for name in {AGENT_NAMES!r}}}\n"
        f"print('ORBIT-ENV ' + json.dumps({{'stage': {stage!r}, 'env': seen}}))\n"
    )


class _ObservingCli(ScriptedCliHarness):
    """The scripted CLI, each of whose turns first prints which variables it received, as
    its answer in the trajectory."""

    async def prepare_command(  # type: ignore[no-untyped-def]
        self, prompt_path, sandbox, session, *, native_session_id
    ):
        command = await super().prepare_command(
            prompt_path, sandbox, session, native_session_id=native_session_id
        )
        return replace(command, script=_observe("agent") + "\n" + command.script)

    def parse_trajectory(self, context):  # type: ignore[no-untyped-def]
        from orbit.core.trajectory import AtifAgent, TrajectoryBuilder
        from orbit.run.harnesses.base import invocations

        builder = TrajectoryBuilder(
            trajectory_id=context.trajectory_id,
            session_id=context.session_id,
            agent=AtifAgent(name=self.name, version=context.agent_version),
        )
        runs = invocations(context)
        for invocation in runs:
            builder.add(source="user", message=invocation.prompt)
        # Each turn's line from the whole log: its turns' offsets count the bytes before the
        # log was redacted.
        for line in "".join(invocation.stdout for invocation in runs).splitlines():
            if line.startswith("ORBIT-ENV "):
                builder.add(source="agent", message=line)
        return builder.build()


@pytest.mark.asyncio
async def test_agent_env_reaches_each_turn_and_no_verifier_unrecorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = f"wandb-{uuid.uuid4().hex}"
    env_file = tmp_path / "checkout.env"
    env_file.write_text(f"WANDB_API_KEY={secret}\n")
    monkeypatch.setenv("ORBIT_ENV_FILE", str(env_file))
    root = tmp_path / "task"
    write_task(
        root,
        name="agentslastexam/agent-env",
        config={
            "environment": {"env": {"REGION": "eu"}},
            "agent": {"validation_rounds": 1, "timeout_sec": 600},
            "verifier": {"env": {"SHARED": "verifier"}},
        },
        dockerfile=f"FROM {CONTAINER_BASE}\n",
        test=None,
    )
    for stage in ("tests", "validation"):
        (root / stage).mkdir(exist_ok=True)
        (root / stage / "test.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            f"{_observe(stage)}\n"
            '"$ORBIT_VERIFY_PYTHON" -c "from orbit_verify import write; write({\'reward\': 1.0})"\n'
        )
    task = load_task_folder(root)
    agent = {"WANDB_API_KEY": "${WANDB_API_KEY}", "WANDB_MODE": "offline"}
    resolve_task_variables([task], oracle=False, protected={}, agent=agent)
    result = await run_episode(
        task,
        EpisodeProtocol(_ObservingCli(), validation_rounds=1),
        ProviderRegistry(RunConfig()),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure

    # What each turn printed is in the trajectory, the verifiers' in the execution trace.
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    lines = [step["message"] for step in trajectory["steps"] if step["source"] == "agent"]
    for record in read_jsonl(result.run_dir / "trace.execution.jsonl").records:
        if record.get("kind") == "command_finished" and record.get("component") == "task-stage":
            lines.extend((record["stdout"].get("inline") or "").splitlines())
    seen = [
        (event["stage"], {key: value for key, value in event["env"].items() if value is not None})
        for event in (
            json.loads(line.removeprefix("ORBIT-ENV "))
            for line in lines
            if line.startswith("ORBIT-ENV ")
        )
    ]
    agent_env = {"REGION": "eu", "WANDB_API_KEY": "[REDACTED]", "WANDB_MODE": "offline"}
    verifier_env = {"REGION": "eu", "SHARED": "verifier"}
    assert seen == [
        ("agent", agent_env),
        ("agent", agent_env),
        ("validation", verifier_env),
        ("tests", verifier_env),
    ]
    assert _holding(result.run_dir, secret) == []

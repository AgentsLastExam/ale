"""Task variables reach exactly the commands their tables name, on every backend, unrecorded.

Every stage of one Task prints which variables it received. The Task's credential comes from
the checkout's ``.env``; it must reach setup, the oracle and the verifier, and no file the
episode leaves on the Host may hold it.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from ale.core.config import RunConfig
from ale.core.lock import RunLock, TaskSource
from ale.core.taskspec import ImageKind
from ale.core.trace import read_jsonl
from ale.core.verdict import Status
from ale.run.episode import EpisodeResult, run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers import ProviderRegistry
from ale.run.providers.qemu import QemuProvider
from ale.run.secrets import resolve_task_variables
from ale.run.tasksets.manifest import ManifestTask, load_task_folder
from tests.support import CONTAINER_BASE, VM_BASE, provider_registry, write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

NAMES = ("PLAIN", "REGION", "SERVICE_TOKEN", "SHARED", "VERIFY_DATA", "SOLUTION_ONLY", "OWN")
OBSERVE = (
    "import json, os, sys\n"
    f"seen = {{name: os.environ.get(name) for name in {NAMES!r}}}\n"
    "print('ALE-ENV ' + json.dumps({'stage': sys.argv[1], 'env': seen}, sort_keys=True))\n"
)
VERIFY = (
    "import os\n"
    "from ale_verify import write\n"
    "write({'reward': 1.0, 'seen': os.environ.get('SERVICE_TOKEN', '')})\n"
)
LINUX = {
    "environment/setup/setup.sh": (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 observe.py setup\n"
        'echo "setup stderr: ${SERVICE_TOKEN}" >&2\n'
    ),
    "solution/solve.sh": "#!/usr/bin/env bash\nset -euo pipefail\npython3 observe.py oracle\n",
    "tests/test.sh": (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 observe.py verify\n"
        'exec "$ALE_VERIFY_PYTHON" verify.py\n'
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
        "& $env:ALE_VERIFY_PYTHON .\\verify.py\nexit $LASTEXITCODE\n"
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
        config |= {"environment": {"env": ENVIRONMENT, "cpus": 2, "vm": True}}
    if windows:
        config |= {
            "environment": {
                "env": ENVIRONMENT,
                "os": "windows",
                "vm_image": "local/windows-variables",
                "cpus": 2,
                "memory_mb": 8192,
            },
        }
    write_task(
        root,
        name="agentslastexam/variables",
        config=config,
        dockerfile=None if windows else f"FROM {VM_BASE if kind == 'vm' else CONTAINER_BASE}\n",
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
            if line.startswith("ALE-ENV "):
                event = json.loads(line.removeprefix("ALE-ENV "))
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
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to an ALE Windows base disk")
    return provider_registry(
        QemuProvider(image=image, overlay_dir=tmp_path / "qemu"), kind=ImageKind.VM
    )


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
    monkeypatch.setenv("ALE_ENV_FILE", str(env_file))
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
    monkeypatch.setenv("ALE_ENV_FILE", str(env_file))
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

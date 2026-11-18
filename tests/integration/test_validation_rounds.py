"""Validation rounds end to end: real snapshots, a CLI that resumes its session, real verifiers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ale.core.sandbox import Provider
from ale.core.trajectory import AtifTrajectory
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.providers.firecracker import FirecrackerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from tests.support import (
    VM_BASE,
    VM_RESOURCES,
    ScriptedCliHarness,
    edit_task_config,
    provider_registry,
    write_task,
)

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

#: One verifier for validation/ and tests/ alike, as a Task may have: a third of the reward
#: per line of work, the lines themselves, how many of the agent's processes outlived a turn
#: and what of validation/ a turn saw; validation/'s feedback tells the agent how far it got,
#: and tests/'s is never read.
VERIFY = """\
import os
from pathlib import Path
from ale_verify import write

home = Path(os.environ["ALE_HOME"])
work = home / "work.txt"
lines = work.read_text().splitlines() if work.is_file() else []
survivors = home / "survivors"
outlived = len(survivors.read_text().splitlines()) if survivors.is_file() else 0
leaks = home / "leaks"
seen = leaks.read_text().splitlines() if leaks.is_file() else []
reward = round(min(1.0, len(lines) / 3), 2)
result = {"reward": reward, "lines": lines, "survivors": outlived, "leaks": seen}
write(result, feedback=f"{len(lines)} of 3 lines; the last: {lines[-1] if lines else ''}")
"""
TEST = '#!/usr/bin/env bash\nset -euo pipefail\nexec "$ALE_VERIFY_PYTHON" verify.py\n'
TEST_PS1 = "& $env:ALE_VERIFY_PYTHON .\\verify.py\nexit $LASTEXITCODE\n"
INSTRUCTION = "Complete the task described here.\n"


def _task(
    root: Path, mode: str, rounds: int = 2, environment: dict[str, Any] | None = None
) -> Path:
    scaffold_task(root)
    verifier: dict[str, Any] = {"environment_mode": mode, "timeout_sec": 600}
    if mode != "shared":
        verifier["environment"] = {"cpus": 2, "memory_mb": 2048}
    edit_task_config(
        root,
        {
            "artifacts": ["work.txt"],
            "environment": environment or {"cpus": 1, "memory_mb": 1024},
            "agent": {"validation_rounds": rounds, "timeout_sec": 600},
            "verifier": verifier,
        },
    )
    _stages(root, TEST, "test.sh")
    (root / "instruction.md").write_text(INSTRUCTION)
    return root


def _stages(root: Path, entry: str, name: str) -> None:
    for stage in ("tests", "validation"):
        (root / stage).mkdir(exist_ok=True)
        (root / stage / name).write_text(entry)
        (root / stage / "verify.py").write_text(VERIFY)


async def _episode(tmp_path: Path, root: Path, provider: Provider) -> Any:
    task = load_tasks(root)[0]
    return await run_episode(
        task,
        EpisodeProtocol(ScriptedCliHarness(), validation_rounds=task.spec.validation_rounds),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
    )


def _check(result: Any, rounds: int = 2) -> None:
    """``rounds + 1`` turns, a scored validation round between each two, tests/ once on the
    final state."""
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": round(min(1.0, (rounds + 1) / 3), 2)}
    scores = [round(min(1.0, index / 3), 2) for index in range(1, rounds + 1)]
    assert [(item.index, item.status, item.reward) for item in result.record.validation_rounds] == [
        (index, Status.COMPLETED, score) for index, score in enumerate(scores, start=1)
    ]
    feedback = (result.run_dir / "validation/1/feedback.md").read_text()
    assert feedback.startswith("1 of 3 lines; the last: ")
    # tests/ wrote feedback too, which is never read.
    assert not (result.run_dir / "feedback.md").exists()
    verification = json.loads((result.run_dir / "verification.json").read_text())
    # Each turn read its prompt back from its native session: the instruction first, then
    # each round's result.
    assert verification["lines"] == [
        INSTRUCTION.strip(),
        *(
            f"Validation round {index} of {rounds}: reward {score:.2f}."
            for index, score in enumerate(scores, start=1)
        ),
    ]
    # No process the agent started outlived the turn it started in, and no later turn saw
    # validation/ or what it wrote.
    assert verification["survivors"] == 0
    assert verification["leaks"] == []
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    users = [step for step in trajectory.steps if step.source == "user"]
    assert [(step.extra or {}).get("ale", {}).get("validation_round") for step in users] == [
        None,
        *range(1, rounds + 1),
    ]
    outcomes = [(item.roles, item.outcome) for item in result.record.sandboxes]
    assert all(outcome == "destroyed" for _, outcome in outcomes), outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate", "snapshot"])
async def test_an_agent_improves_over_validation_rounds_in_docker(
    tmp_path: Path, mode: str
) -> None:
    root = _task(tmp_path / f"rounds-{mode}", mode)
    result = await _episode(tmp_path, root, DockerProvider())
    _check(result)
    # Every round's snapshot is gone, and so is every Sandbox.
    episode = result.episode_id
    images = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", "ale-snapshot"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert not [image for image in images if episode in image]
    containers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=ale.episode={episode}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert containers == []


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_an_agent_improves_over_validation_rounds_in_a_microvm(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    request.getfixturevalue("kvm_host")
    root = _task(tmp_path / "rounds-firecracker", "shared")
    provider = FirecrackerProvider(
        overlay_dir=tmp_path / "overlays",
        snapshot_dir=tmp_path / "snapshots",
        disk_dir=tmp_path / "disks",
    )
    result = await _episode(tmp_path, root, provider)
    _check(result)
    assert await provider.list_retained() == []


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_an_agent_improves_over_validation_rounds_in_a_vm(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    request.getfixturevalue("docker_buildx")
    request.getfixturevalue("kvm_host")
    root = _task(tmp_path / "rounds-vm", "shared", environment={"image": VM_BASE, **VM_RESOURCES})
    (root / "environment/Dockerfile").unlink()
    (root / "environment/build.sh").write_text("true\n")
    provider = QemuProvider(overlay_dir=tmp_path / "qemu", snapshot_dir=tmp_path / "snapshots")
    result = await _episode(tmp_path, root, provider)
    _check(result)
    assert await provider.list_retained() == []


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_an_agent_improves_over_a_validation_round_in_a_windows_vm(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    request.getfixturevalue("kvm_host")
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the Windows base qcow2")
    monkeypatch.setattr("ale.run.task_images.cache_root", lambda: tmp_path / "cache")
    root = tmp_path / "rounds-windows"
    write_task(
        root,
        name="agentslastexam/rounds-windows",
        config={
            "artifacts": [],
            "environment": {
                "os": "windows",
                "image": "local/windows-base",
                "cpus": 2,
                "memory_mb": 4096,
            },
            "agent": {"timeout_sec": 600, "validation_rounds": 1},
            "verifier": {"timeout_sec": 600},
        },
        dockerfile=None,
        instruction=INSTRUCTION,
        test=None,
        solve=None,
    )
    (root / "environment").mkdir(exist_ok=True)
    # Built on the base, so the image gets the Windows framework.
    (root / "environment/build.ps1").write_text("exit 0\n")
    (root / "calibration/solution").mkdir(parents=True)
    (root / "calibration/solution/solve.ps1").write_text("exit 0\n")
    _stages(root, TEST_PS1, "test.ps1")
    provider = QemuProvider(
        image=image, overlay_dir=tmp_path / "qemu", snapshot_dir=tmp_path / "snapshots"
    )
    result = await _episode(tmp_path, root, provider)
    _check(result, rounds=1)
    assert await provider.list_retained() == []


@pytest.mark.asyncio
async def test_calibration_checks_validation_on_each_cases_final_state_in_docker(
    tmp_path: Path,
) -> None:
    """Each case's final state is saved, scored by validation/, and then by tests/ in a
    solver booted from it, against each expectation."""
    from ale.core.config import RunConfig
    from ale.run.cli.tasks import _calibrate

    root = _task(tmp_path / "rounds-calibration", "shared", rounds=0)
    edit_task_config(
        root,
        {
            "calibration": [
                {"name": "solution", "expect": {"reward": 1}},
                {
                    "name": "partial",
                    "expect": {"reward": 0.33},
                    "expect_validation": {"reward": 0.33},
                },
            ]
        },
    )
    for case, lines in (("solution", "a\\nb\\nc\\n"), ("partial", "a\\n")):
        (root / "calibration" / case).mkdir(parents=True, exist_ok=True)
        (root / "calibration" / case / "solve.sh").write_text(
            f'#!/usr/bin/env bash\nprintf "{lines}" > "${{ALE_HOME:-$HOME}}/work.txt"\n'
        )
    settings = RunConfig.model_validate({"sandbox": {"providers": ["docker:runc"]}})
    code = await _calibrate(str(root), settings, tmp_path / "runs")
    (path,) = (tmp_path / "runs").glob("calibrate-*/calibration.json")
    observation = json.loads(path.read_text())
    assert code == 0, observation
    cases = {case["name"]: case for case in observation["tasks"][0]["cases"]}
    assert cases["solution"]["validation"]["observed"] == {"reward": 1.0}
    assert cases["partial"]["validation"]["observed"] == {"reward": 0.33}

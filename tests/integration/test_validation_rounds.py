"""Validation rounds end to end: real snapshots, a CLI that resumes its session, real verifiers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orbit.core.sandbox import Provider
from orbit.core.trajectory import AtifTrajectory
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.providers.docker import DockerProvider
from orbit.run.providers.firecracker import FirecrackerProvider
from orbit.run.providers.qemu import QemuProvider
from orbit.run.scaffold import scaffold_task
from orbit.run.secrets import resolve_task_variables
from orbit.run.tasksets.manifest import load_tasks
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
#: per line of work, the lines themselves, how many of the agent's processes outlived a turn,
#: what of validation/ or tests/ a turn saw and how many prompts the trajectory it received
#: holds; validation/'s feedback tells the agent how far it got, and tests/'s is never read.
VERIFY = """\
import os
from pathlib import Path
from orbit_verify import load_trajectory, write

home = Path(os.environ["ORBIT_HOME"])
work = home / "work.txt"
lines = work.read_text().splitlines() if work.is_file() else []
survivors = home / "survivors"
outlived = len(survivors.read_text().splitlines()) if survivors.is_file() else 0
leaks = home / "leaks"
seen = leaks.read_text().splitlines() if leaks.is_file() else []
turns = {
    name: (home / name).read_text().splitlines() if (home / name).is_file() else []
    for name in ("shown", "written", "mounted")
}
trajectory = load_trajectory()
prompts = sum(step["source"] == "user" for step in trajectory.document["steps"])
reward = round(min(1.0, len(lines) / 3), 2)
result = {
    "reward": reward,
    "lines": lines,
    "survivors": outlived,
    "leaks": seen,
    "prompts": prompts,
    "split": os.environ.get("SPLIT"),
    **turns,
}
write(result, feedback=f"{len(lines)} of 3 lines; the last: {lines[-1] if lines else ''}")
"""
TEST = '#!/usr/bin/env bash\nset -euo pipefail\nexec "$ORBIT_VERIFY_PYTHON" verify.py\n'
TEST_PS1 = "& $env:ORBIT_VERIFY_PYTHON .\\verify.py\nexit $LASTEXITCODE\n"
INSTRUCTION = "Complete the task described here.\n"


def _task(
    root: Path,
    mode: str,
    rounds: int = 2,
    environment: dict[str, Any] | None = None,
    *,
    validation: bool = True,
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
    _stages(root, TEST, "test.sh", validation=validation)
    (root / "instruction.md").write_text(INSTRUCTION)
    return root


def _stages(root: Path, entry: str, name: str, *, validation: bool = True) -> None:
    for stage in ("tests", "validation") if validation else ("tests",):
        (root / stage).mkdir(exist_ok=True)
        (root / stage / name).write_text(entry)
        (root / stage / "verify.py").write_text(VERIFY)


async def _episode(tmp_path: Path, root: Path, provider: Provider) -> Any:
    task = load_tasks(root)[0]
    resolve_task_variables([task], oracle=False, protected={})
    return await run_episode(
        task,
        EpisodeProtocol(ScriptedCliHarness(), validation_rounds=task.spec.validation_rounds),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
    )


def _check(result: Any, rounds: int = 2, *, stage: str = "validation", shown: bool = False) -> None:
    """``rounds + 1`` turns, a round ``stage`` scored between each two, tests/ once on the
    final state; every turn read the copy of ``stage`` where it was ``shown``, and none
    changed it."""
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": round(min(1.0, (rounds + 1) / 3), 2)}
    scores = [round(min(1.0, index / 3), 2) for index in range(1, rounds + 1)]
    assert [(item.index, item.status, item.reward) for item in result.record.validation_rounds] == [
        (index, Status.COMPLETED, score) for index, score in enumerate(scores, start=1)
    ]
    # Each round's feedback, of tests/ too where it validated; the final run's is not read.
    feedback = (result.run_dir / "validation/1/feedback.md").read_text()
    assert feedback.startswith("1 of 3 lines; the last: ")
    assert sorted(path.parent.name for path in result.run_dir.rglob("feedback.md")) == [
        str(index) for index in range(1, rounds + 1)
    ]
    verification = json.loads((result.run_dir / "verification.json").read_text())
    # Each turn read its prompt back from its native session: the instruction first, then
    # each round's result.
    name = "Validation round" if stage == "validation" else "Round"
    assert verification["lines"] == [
        INSTRUCTION.strip(),
        *(
            f"{name} {index} of {rounds}: reward {score:.2f}."
            for index, score in enumerate(scores, start=1)
        ),
    ]
    # No process the agent started outlived the turn it started in, and no turn saw
    # validation/, tests/ or what they wrote.
    assert verification["survivors"] == 0
    assert verification["leaks"] == []
    assert verification["shown"] == ([f"turn {i}" for i in range(1, rounds + 2)] if shown else [])
    assert verification["written"] == []
    # Each round's verifier received the trajectory of the turns so far, which the round
    # keeps; the final one received every turn's.
    for index in range(1, rounds + 1):
        scored = json.loads((result.run_dir / f"validation/{index}/verification.json").read_text())
        assert scored["prompts"] == index
        kept = AtifTrajectory.model_validate_json(
            (result.run_dir / f"validation/{index}/trajectory.json").read_text()
        )
        assert sum(step.source == "user" for step in kept.steps) == index
    assert verification["prompts"] == rounds + 1
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    users = [step for step in trajectory.steps if step.source == "user"]
    assert [(step.extra or {}).get("orbit", {}).get("validation_round") for step in users] == [
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
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", "orbit-snapshot"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert not [image for image in images if episode in image]
    containers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=orbit.episode={episode}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert containers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate", "snapshot"])
async def test_an_agent_improves_over_rounds_tests_scores_without_validation_in_docker(
    tmp_path: Path, mode: str
) -> None:
    """A Task without validation/ loops on tests/ as on a copy of it: each round runs it in a
    verifier of its own, and the agent receives its result."""
    root = _task(tmp_path / f"rounds-tests-{mode}", mode, validation=False)
    result = await _episode(tmp_path, root, DockerProvider())
    _check(result, stage="tests")
    assert sorted(path.name for path in (result.run_dir / "validation/1").iterdir()) == [
        "feedback.md",
        "trajectory.json",
        "verification.json",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("validation", [True, False])
async def test_the_agent_reads_the_stage_that_validates_where_the_task_shows_it_in_docker(
    tmp_path: Path, validation: bool
) -> None:
    """Every turn reads its read-only copy, of validation/ or else of tests/, and cannot
    change it; the rounds and the final score run the Host's."""
    shown = "validation" if validation else "tests"
    root = _task(tmp_path / f"rounds-shown-{shown}", "shared", validation=validation)
    edit_task_config(root, {"verifier": {"validation": {"visibility": True}}})
    result = await _episode(tmp_path, root, DockerProvider())
    _check(result, stage=shown, shown=True)
    # Docker mounts the Task folder's stage read-only in every turn's solver, copying none.
    verification = json.loads((result.run_dir / "verification.json").read_text())
    assert verification["mounted"] == ["turn 1", "turn 2", "turn 3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["separate", "snapshot"])
async def test_verifier_validation_places_the_rounds_with_variables_of_their_own_in_docker(
    tmp_path: Path, mode: str
) -> None:
    """[verifier.validation] gives the rounds a verifier of their own, here apart from a
    shared tests/, with a limit and a variable over [verifier]'s."""
    root = _task(tmp_path / f"rounds-own-{mode}", "shared")
    validation = {"environment_mode": mode, "timeout_sec": 300, "env": {"SPLIT": "validation"}}
    if mode == "separate":
        validation["environment"] = {"cpus": 2, "memory_mb": 2048}
    edit_task_config(root, {"verifier": {"env": {"SPLIT": "tests"}, "validation": validation}})
    result = await _episode(tmp_path, root, DockerProvider())
    _check(result)
    scored = json.loads((result.run_dir / "validation/1/verification.json").read_text())
    final = json.loads((result.run_dir / "verification.json").read_text())
    assert (scored["split"], final["split"]) == ("validation", "tests")
    # The rounds' verifiers ran apart from the solver, tests/ in it.
    roles = [item.roles for item in result.record.sandboxes]
    assert roles.count(("verifier",)) == 2, roles


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
    image = Path(os.environ.get("ORBIT_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ORBIT_TEST_WINDOWS_IMAGE to the Windows base qcow2")
    monkeypatch.setattr("orbit.run.task_images.cache_root", lambda: tmp_path / "cache")
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
    (root / "solution").mkdir(parents=True)
    (root / "solution/solve.ps1").write_text("exit 0\n")
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
    from orbit.core.config import RunConfig
    from orbit.run.cli.tasks import _calibrate

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
    for case, lines in (("solution", "a\\nb\\nc\\n"), ("calibration/partial", "a\\n")):
        (root / case).mkdir(parents=True, exist_ok=True)
        (root / case / "solve.sh").write_text(
            f'#!/usr/bin/env bash\nprintf "{lines}" > "${{ORBIT_HOME:-$HOME}}/work.txt"\n'
        )
    settings = RunConfig.model_validate({"sandbox": {"providers": ["docker:runc"]}})
    code = await _calibrate(str(root), settings, tmp_path / "runs")
    (path,) = (tmp_path / "runs").glob("calibrate-*/calibration.json")
    observation = json.loads(path.read_text())
    assert code == 0, observation
    cases = {case["name"]: case for case in observation["tasks"][0]["cases"]}
    assert cases["solution"]["validation"]["observed"] == {"reward": 1.0}
    assert cases["partial"]["validation"]["observed"] == {"reward": 0.33}

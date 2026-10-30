"""A real CLI agent continues, episode after episode, in the solver an earlier episode kept
or in Sandboxes booted from its snapshot.

The chains run in a Docker container, a QEMU Linux VM and a QEMU Windows VM. pi runs
through a real Gateway of each episode's own, as separate runs would, against a scripted
model, so each turn calls the shell once and then finishes. Each turn appends a line to a
file in the agent's home, and the Task's verification scores a quarter per line: the score
proves the Sandbox carried over, and each trajectory shows only its own turn of the one
native session.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from ale.core.config import RunConfig, SandboxRetentionConfig
from ale.core.harness import Harness
from ale.core.lock import RunLock, TaskSource
from ale.core.sandbox import SandboxRequest, SandboxRole
from ale.core.taskspec import ImageKind, OperatingSystem
from ale.core.trajectory import AtifTrajectory
from ale.core.verdict import Status
from ale.run.cli.tasks import _attach_retained, _continuation
from ale.run.episode import EpisodeResult, run_episode
from ale.run.gateway.server import Gateway
from ale.run.harnesses.pi_cli import PiCliHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol, Resume
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers import ProviderRegistry
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import ManifestTask, load_tasks
from tests.integration.scripted_model import ScriptedModel
from tests.support import VM_BASE, edit_task_config, provider_registry, write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

DIALECT = "openai-chat-completions"
MODEL = "scripted-model"
#: Where each turn is recorded; pi's shell is Git Bash on Windows.
OUTPUT = {
    "container": "/home/user/output",
    "vm": "/home/user/output",
    "windows": "C:/Users/user/output",
}


def _inputs(task: ManifestTask, harness: Harness, model: str) -> ProvenanceInputs:
    settings = RunConfig.model_validate({})
    return ProvenanceInputs(
        source=TaskSource(kind="local", path=str(task.folder.root)),
        agent=agent_provenance(harness, model),
        gateway=gateway_provenance(settings),
        config_hash=settings.config_hash,
    )


def _task(tmp_path: Path, kind: str) -> ManifestTask:
    root = tmp_path / "continued"
    if kind == "windows":
        write_task(
            root,
            name="agentslastexam/continued",
            config={
                "environment": {"os": "windows", "cpus": 2, "memory_mb": 8192},
                "agent": {"timeout_sec": 1200},
                "verifier": {"timeout_sec": 300},
                "ale": {"sandbox": "vm", "vm_base": "local/windows-base"},
            },
            dockerfile=None,
            instruction="Record one more turn.\n",
            test=None,
            solve=None,
        )
        for directory in ("environment/setup", "tests", "solution"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        # Built on the base, so the image gets the Windows framework.
        (root / "environment/build.ps1").write_text("exit 0\n")
        (root / "environment/setup/setup.ps1").write_text("exit 0\n")
        (root / "solution/solve.ps1").write_text("exit 0\n")
        (root / "tests/test.ps1").write_text("python.exe .\\verify.py\nexit $LASTEXITCODE\n")
    else:
        scaffold_task(root)
    if kind == "vm":
        edit_task_config(root, {"environment": {"cpus": 2}, "ale": {"sandbox": "vm"}})
        dockerfile = root / "environment/Dockerfile"
        _, rest = dockerfile.read_text().split("\n", 1)
        dockerfile.write_text(f"FROM {VM_BASE}\n{rest}")
    (root / "tests/verify.py").write_text(
        "from pathlib import Path\n"
        "from ale_verify import write\n"
        f"path = Path({OUTPUT[kind] + '/turns.txt'!r})\n"
        "turns = len(path.read_text().splitlines()) if path.is_file() else 0\n"
        "write({'reward': turns / 4})\n"
    )
    return load_tasks(root)[0]


async def _kept_stages(handle: str, task: ManifestTask, run_dir: Path) -> list[str]:
    """What a kept solver still holds of the Task's stages, seen through a new attachment."""
    lock = RunLock.model_validate_json((run_dir / "lock.json").read_text())
    assert task.prepared_image is not None
    request = SandboxRequest(
        episode_id="inspection",
        os=task.spec.os,
        role=SandboxRole.SHARED,
        prepared_image=task.prepared_image,
        resources=task.spec.resources,
        network=task.spec.network,
        user=task.spec.image.user,
    )
    sandbox = await _attach_retained(handle, request, lock)
    try:
        if task.spec.os is OperatingSystem.WINDOWS:
            # The base's stage directories stay, empty.
            argv = [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "@('setup', 'tests') | ForEach-Object { $stage = \"C:\\ProgramData\\ALE\\$_\"; "
                "if (Test-Path -LiteralPath $stage) { "
                "Get-ChildItem -LiteralPath $stage -Force | ForEach-Object FullName "
                '} else { "missing $stage" } }',
            ]
        else:
            argv = ["sh", "-c", "find /opt/ale/setup /opt/ale/tests 2>/dev/null; true"]
        listing = await sandbox.exec(argv)
    finally:
        await sandbox.retain(roles=("solver",), reason="inspected")
    assert listing.ok, listing.stderr
    return listing.stdout.split()


@dataclass
class _Chain:
    """One Task, Provider and scripted model; every episode serves its own Gateway."""

    kind: str
    tmp_path: Path
    provider: DockerProvider | QemuProvider
    registry: ProviderRegistry
    task: ManifestTask
    model: ScriptedModel
    upstream: str
    harness: Harness = field(default_factory=lambda: PiCliHarness(gateway_dialect=DIALECT))
    retained: list[str] = field(default_factory=list)

    async def episode(
        self,
        name: str,
        *,
        resume: Resume | None,
        retention: Literal["destroy", "keep", "snapshot"],
    ) -> EpisodeResult:
        gateway = Gateway(
            api_key="host-only", upstream=self.upstream, dialect=DIALECT, host="0.0.0.0"
        )
        gateway_url = await gateway.start()
        try:
            result = await run_episode(
                self.task,
                EpisodeProtocol(self.harness, resume=resume),
                self.registry,
                run_dir=self.tmp_path / name,
                gateway=gateway,
                gateway_url=gateway_url,
                model=MODEL,
                provenance=_inputs(self.task, self.harness, MODEL),
                sandbox_retention=SandboxRetentionConfig(solver=retention),
            )
        finally:
            await gateway.stop()
        self.retained.extend(item.handle for item in result.record.sandboxes if item.handle)
        return result

    def continuing(self, result: EpisodeResult) -> Resume:
        return _continuation(result.run_dir, [self.task], RunConfig(), self.harness)


@asynccontextmanager
async def _chain(
    kind: str, tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Chain]:
    if kind == "vm":
        request.getfixturevalue("docker_buildx")
        request.getfixturevalue("kvm_host")
    registry = ProviderRegistry(RunConfig())
    provider: DockerProvider | QemuProvider = DockerProvider()
    if kind == "vm":
        provider = QemuProvider(overlay_dir=tmp_path / "qemu", snapshot_dir=tmp_path / "snapshots")
        registry = provider_registry(provider, kind=ImageKind.VM)
    if kind == "windows":
        image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
        if not image.is_file():
            pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the private Windows base qcow2")
        monkeypatch.setattr("ale.run.task_images.cache_root", lambda: tmp_path / "cache")
        provider = QemuProvider(
            image=image, overlay_dir=tmp_path / "qemu", snapshot_dir=tmp_path / "snapshots"
        )
        registry = provider_registry(provider, kind=ImageKind.VM)
    turns = f"{OUTPUT[kind]}/turns.txt"
    model = ScriptedModel(
        command=f"mkdir -p {OUTPUT[kind]} && echo turn >> {turns} && wc -l < {turns}"
    )
    chain = _Chain(kind, tmp_path, provider, registry, _task(tmp_path, kind), model, "")
    chain.upstream = await model.start()
    try:
        yield chain
    finally:
        listed = {item["handle"] for item in await provider.list_retained()}
        for handle in dict.fromkeys(chain.retained):
            if handle in listed:
                await provider.destroy_retained(handle)
        await model.stop()


def _trajectory(result: EpisodeResult) -> AtifTrajectory:
    return AtifTrajectory.model_validate_json((result.run_dir / "trajectory.json").read_text())


KINDS = [
    "container",
    pytest.param("vm", marks=pytest.mark.needs_kvm),
    pytest.param("windows", marks=pytest.mark.needs_kvm),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_kept_episodes_continue_and_a_superseded_one_is_refused(
    tmp_path: Path, kind: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _chain(kind, tmp_path, request, monkeypatch) as chain:
        first = await chain.episode("first", resume=None, retention="keep")
        assert first.verdict.status is Status.COMPLETED, first.verdict.failure
        assert first.verdict.rewards == {"reward": 0.25}
        assert first.record.continuation is not None
        (kept_first,) = [item.handle for item in first.record.sandboxes if item.handle]
        assert await _kept_stages(kept_first, chain.task, first.run_dir) == []
        second = await chain.episode("second", resume=chain.continuing(first), retention="keep")
        assert second.verdict.status is Status.COMPLETED, second.verdict.failure
        assert second.verdict.rewards == {"reward": 0.5}

        # The earlier record still names the Sandbox, which now holds the second agent's
        # state; continuing from it is refused without touching that Sandbox.
        stale = await chain.episode("stale", resume=chain.continuing(first), retention="destroy")
        assert stale.verdict.status is not Status.COMPLETED
        assert stale.verdict.failure is not None
        assert "holds episode" in stale.verdict.failure.message

        third = await chain.episode("third", resume=chain.continuing(second), retention="destroy")
        assert third.verdict.status is Status.COMPLETED, third.verdict.failure
        assert third.verdict.rewards == {"reward": 0.75}

    assert first.record.continuation == second.record.continuation == third.record.continuation
    lock = RunLock.model_validate_json((third.run_dir / "lock.json").read_text())
    assert lock.resumed_from is not None
    assert lock.resumed_from.episode_id == second.record.episode_id
    assert lock.resumed_from.retention == "keep"
    assert [item.outcome for item in third.record.sandboxes] == ["destroyed"]

    # One native session, one trajectory segment per episode, each beginning at its prompt.
    segments = [_trajectory(result) for result in (first, second, third)]
    assert len({segment.session_id for segment in segments}) == 1
    for segment in segments:
        assert segment.steps[0].source == "user"
        assert sum(step.source == "user" for step in segment.steps) == 1
    links = [((segment.extra or {}).get("ale") or {}).get("resumed_from") for segment in segments]
    assert links[0] is None
    assert links[1] == {
        "episode_id": first.record.episode_id,
        "trajectory_id": segments[0].trajectory_id,
        "native_steps": len(segments[0].steps),
    }
    assert links[2]["native_steps"] == len(segments[0].steps) + len(segments[1].steps)
    assert "parse_issues" not in json.dumps([segment.extra for segment in segments[1:]])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_snapshot_is_continued_by_concurrent_forks(
    tmp_path: Path, kind: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _chain(kind, tmp_path, request, monkeypatch) as chain:
        first = await chain.episode("first", resume=None, retention="snapshot")
        assert first.verdict.status is Status.COMPLETED, first.verdict.failure
        assert first.verdict.rewards == {"reward": 0.25}
        (saved,) = first.record.sandboxes
        assert (saved.requested, saved.outcome) == ("snapshot", "snapshotted")
        assert saved.handle is not None and saved.handle.startswith(f"{chain.provider.name}:")
        assert saved.handle in {item["handle"] for item in await chain.provider.list_retained()}

        # Each fork starts from the snapshot, not from the other's work.
        left, right = await asyncio.gather(
            chain.episode("left", resume=chain.continuing(first), retention="destroy"),
            chain.episode("right", resume=chain.continuing(first), retention="snapshot"),
        )
        for fork in (left, right):
            assert fork.verdict.status is Status.COMPLETED, fork.verdict.failure
            assert fork.verdict.rewards == {"reward": 0.5}
        assert [item.outcome for item in left.record.sandboxes] == ["destroyed"]

        third = await chain.episode("third", resume=chain.continuing(right), retention="destroy")
        assert third.verdict.status is Status.COMPLETED, third.verdict.failure
        assert third.verdict.rewards == {"reward": 0.75}

    for fork, source in ((left, first), (right, first), (third, right)):
        lock = RunLock.model_validate_json((fork.run_dir / "lock.json").read_text())
        assert lock.resumed_from is not None
        assert (lock.resumed_from.episode_id, lock.resumed_from.retention) == (
            source.record.episode_id,
            "snapshot",
        )
        # The Task's image is recorded as prepared, the snapshot as what actually booted.
        assert lock.image.prepared_identity == chain.task.prepared_image.prepared_identity
    segments = [_trajectory(result) for result in (first, left, right, third)]
    assert len({segment.session_id for segment in segments}) == 1
    assert "parse_issues" not in json.dumps([segment.extra for segment in segments[1:]])

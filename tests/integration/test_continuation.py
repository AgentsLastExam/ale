"""A real CLI agent continues, episode after episode, in the solver an earlier episode kept.

The chain runs in a Docker container, a QEMU Linux VM and a QEMU Windows VM. pi runs
through the real Gateway against a scripted model, so each turn calls the shell once and
then finishes. Each turn appends a line to a file in the agent's home, and the Task's
verification scores a quarter per line: the score proves the Sandbox carried over, and
each trajectory shows only its own turn of the one native session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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
from tests.support import VM_BASE, provider_registry

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
        for stage in ("image", "setup", "oracle", "verify"):
            (root / stage).mkdir(parents=True)
        # Built on the base, so the image gets the Windows framework.
        (root / "image/run.ps1").write_text("exit 0\n")
        (root / "setup/run.ps1").write_text("exit 0\n")
        (root / "oracle/run.ps1").write_text("exit 0\n")
        (root / "verify/run.ps1").write_text("python.exe .\\verify.py\nexit $LASTEXITCODE\n")
        (root / "instruction.md").write_text("Record one more turn.\n")
        (root / "task.yaml").write_text(
            "spec_type: core/v1\nname: continued\nos: windows\n"
            "image: {kind: vm, base_ref: local/windows-base}\n"
            "resources: {cpus: 2, memory_mb: 8192}\n"
            "timeouts: {setup: 300, agent: 1200, verify: 300}\n"
        )
    else:
        scaffold_task(root)
    if kind == "vm":
        manifest = root / "task.yaml"
        manifest.write_text(
            manifest.read_text()
            .replace("  kind: container", "  kind: vm")
            .replace("cpus: 1,", "cpus: 2,")
        )
        dockerfile = root / "image/Dockerfile"
        _, rest = dockerfile.read_text().split("\n", 1)
        dockerfile.write_text(f"FROM {VM_BASE}\n{rest}")
    (root / "verify/verify.py").write_text(
        "from pathlib import Path\n"
        "from ale_verify import write\n"
        f"path = Path({OUTPUT[kind] + '/turns.txt'!r})\n"
        "turns = len(path.read_text().splitlines()) if path.is_file() else 0\n"
        "write({'overall': turns / 4})\n"
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
                "@('setup', 'verify') | ForEach-Object { $stage = \"C:\\ProgramData\\ALE\\$_\"; "
                "if (Test-Path -LiteralPath $stage) { "
                "Get-ChildItem -LiteralPath $stage -Force | ForEach-Object FullName "
                '} else { "missing $stage" } }',
            ]
        else:
            argv = ["sh", "-c", "find /opt/ale/setup /opt/ale/verify 2>/dev/null; true"]
        listing = await sandbox.exec(argv)
    finally:
        await sandbox.retain(roles=("solver",), reason="inspected")
    assert listing.ok, listing.stderr
    return listing.stdout.split()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "container",
        pytest.param("vm", marks=pytest.mark.needs_kvm),
        pytest.param("windows", marks=pytest.mark.needs_kvm),
    ],
)
async def test_kept_episodes_continue_and_a_superseded_one_is_refused(
    tmp_path: Path, kind: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind == "vm":
        request.getfixturevalue("docker_buildx")
        request.getfixturevalue("kvm_host")
    registry = ProviderRegistry(RunConfig())
    provider: DockerProvider | QemuProvider = (
        DockerProvider() if kind == "container" else QemuProvider()
    )
    if kind == "windows":
        image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
        if not image.is_file():
            pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the private Windows base qcow2")
        monkeypatch.setattr("ale.run.task_images.cache_root", lambda: tmp_path / "cache")
        provider = QemuProvider(image=image, overlay_dir=tmp_path / "qemu")
        registry = provider_registry(provider, kind=ImageKind.VM)
    task = _task(tmp_path, kind)
    harness = PiCliHarness(gateway_dialect=DIALECT)
    settings = RunConfig.model_validate({})
    turns = f"{OUTPUT[kind]}/turns.txt"
    model = ScriptedModel(
        command=f"mkdir -p {OUTPUT[kind]} && echo turn >> {turns} && wc -l < {turns}"
    )
    gateway = Gateway(
        api_key="host-only", upstream=await model.start(), dialect=DIALECT, host="0.0.0.0"
    )
    gateway_url = await gateway.start()
    kept: list[str] = []

    async def episode(name: str, *, resume: Resume | None, keep: bool) -> EpisodeResult:
        result = await run_episode(
            task,
            EpisodeProtocol(harness, resume=resume),
            registry,
            run_dir=tmp_path / name,
            gateway=gateway,
            gateway_url=gateway_url,
            model=MODEL,
            provenance=_inputs(task, harness, MODEL),
            sandbox_retention=SandboxRetentionConfig(solver="keep" if keep else "destroy"),
        )
        kept.extend(item.handle for item in result.record.sandboxes if item.handle is not None)
        return result

    def continuing(result: EpisodeResult) -> Resume:
        return _continuation(result.run_dir, [task], settings, harness)

    def trajectory(result: EpisodeResult) -> AtifTrajectory:
        return AtifTrajectory.model_validate_json((result.run_dir / "trajectory.json").read_text())

    try:
        first = await episode("first", resume=None, keep=True)
        assert first.verdict.status is Status.COMPLETED, first.verdict.failure
        assert first.verdict.rewards == {"overall": 0.25}
        assert first.record.continuation is not None
        (kept_first,) = [item.handle for item in first.record.sandboxes if item.handle]
        assert await _kept_stages(kept_first, task, first.run_dir) == []
        second = await episode("second", resume=continuing(first), keep=True)
        assert second.verdict.status is Status.COMPLETED, second.verdict.failure
        assert second.verdict.rewards == {"overall": 0.5}

        # The earlier record still names the Sandbox, which now holds the second agent's
        # state; continuing from it is refused without touching that Sandbox.
        stale = await episode("stale", resume=continuing(first), keep=False)
        assert stale.verdict.status is not Status.COMPLETED
        assert stale.verdict.failure is not None
        assert "holds episode" in stale.verdict.failure.message

        third = await episode("third", resume=continuing(second), keep=False)
        assert third.verdict.status is Status.COMPLETED, third.verdict.failure
        assert third.verdict.rewards == {"overall": 0.75}
    finally:
        for handle in dict.fromkeys(kept):
            if handle in {item["handle"] for item in await provider.list_retained()}:
                await provider.destroy_retained(handle)
        await gateway.stop()
        await model.stop()

    assert first.record.continuation == second.record.continuation == third.record.continuation
    lock = RunLock.model_validate_json((third.run_dir / "lock.json").read_text())
    assert lock.resumed_from is not None
    assert lock.resumed_from.episode_id == second.record.episode_id
    assert [item.outcome for item in third.record.sandboxes] == ["destroyed"]

    # One native session, one trajectory segment per episode, each beginning at its prompt.
    segments = [trajectory(result) for result in (first, second, third)]
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

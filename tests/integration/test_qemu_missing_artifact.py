"""Exercise missing artifact roots against a real Linux QEMU guest."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ale.core.sandbox import ImageRef, SandboxRequest
from ale.core.taskspec import ImageKind, NetworkPolicy, Resources
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import _Artifacts, run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.qemu import QemuProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]


def linux_image() -> Path:
    value = os.environ.get("ALE_TEST_LINUX_VM_IMAGE")
    if not value or not Path(value).is_file():
        pytest.skip("set ALE_TEST_LINUX_VM_IMAGE to a prepared Linux QEMU disk")
    return Path(value)


@pytest.mark.asyncio
async def test_missing_linux_vm_artifact_reaches_shared_verifier(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "missing-vm-output")
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text()
        .replace("kind: container", "kind: vm\n  ref: ghcr.io/agentslastexam/test-vm:local")
        .replace("cpus: 1, memory_mb: 1024", "cpus: 2, memory_mb: 4096")
        + "artifact_allow_missing: true\n"
    )
    (task / "oracle/run.sh").write_text("#!/bin/sh\nset -eu\nrm -rf /home/user/output\n")
    provider = QemuProvider(image=linux_image(), overlay_dir=tmp_path / "qemu")

    result = await run_episode(
        load_tasks(task)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(provider, kind=ImageKind.VM),
        run_dir=tmp_path / "runs",
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"content": 0.0, "overall": 0.0}
    entry = json.loads((result.run_dir / "artifacts/snapshot.json").read_text())["entries"][0]
    assert entry["kind"] == "missing" and entry["allow_missing"] is True


@pytest.mark.asyncio
async def test_missing_linux_vm_artifact_removes_stale_restored_root(tmp_path: Path) -> None:
    provider = QemuProvider(image=linux_image(), overlay_dir=tmp_path / "qemu")
    prepared = await provider.prepare_image(
        ImageRef(kind=ImageKind.VM, reference="ghcr.io/agentslastexam/test-vm:local")
    )
    sandbox = await provider.create(
        SandboxRequest(
            episode_id="missing-vm-artifact",
            prepared_image=prepared,
            resources=Resources(cpus=2, memory_mb=4096),
            network=NetworkPolicy(),
        )
    )
    root = "/tmp/ale-test-missing-artifact"
    try:
        assert (await sandbox.exec(["rm", "-rf", "--", root])).ok
        sink = _Artifacts(tmp_path / "run")
        await sink.collect(sandbox, root, "output", allow_missing=True)
        assert (await sandbox.exec(["mkdir", "-p", "--", root])).ok
        await sink.restore(sandbox)
        assert not (await sandbox.exec(["test", "-e", root])).ok
    finally:
        await sandbox.destroy()

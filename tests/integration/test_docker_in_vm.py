from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit.core.config import RunConfig
from orbit.core.episode import Phase
from orbit.core.lock import TaskSource
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.provenance import (
    ProvenanceInputs,
    agent_provenance,
    gateway_provenance,
)
from orbit.run.providers import ProviderRegistry
from orbit.run.tasksets.manifest import load_tasks
from tests.support import VM_BASE, VM_RESOURCES, write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]

OUTPUT = "/home/user/output/nested-container.txt"
#: The agent's network is closed, so the image it runs is kept as a root filesystem to import.
BUILD = """\
set -euo pipefail
apt-get update
apt-get install -y docker.io
rm -rf /var/lib/apt/lists/*
systemctl enable docker.service containerd.service
usermod -aG docker user
busybox=busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
systemctl start docker
docker pull --quiet "$busybox" >/dev/null
container=$(docker create "$busybox")
install -d /usr/local/share/docker-in-vm
docker export "$container" -o /usr/local/share/docker-in-vm/busybox-rootfs.tar
chmod 0644 /usr/local/share/docker-in-vm/busybox-rootfs.tar
docker rm "$container" >/dev/null
docker image prune --all --force >/dev/null
"""
SETUP = f"""\
#!/usr/bin/env bash
set -euo pipefail
mkdir -p /home/user/output
: > {OUTPUT}
chown -R user:user /home/user/output
for _ in $(seq 1 60); do
    docker info >/dev/null 2>&1 && exit 0
    sleep 1
done
echo "Docker did not become ready" >&2
exit 1
"""
SOLVE = f"""\
#!/usr/bin/env bash
set -euo pipefail
docker import /usr/local/share/docker-in-vm/busybox-rootfs.tar busybox:offline >/dev/null
docker run --rm --network none busybox:offline sh -c 'printf "nested-container-ok\\n"' \\
    > {OUTPUT}
"""
VERIFY = f"""\
from orbit_verify import checks, write

nested_container = checks.text_equals({OUTPUT!r}, "nested-container-ok\\n")
write({{"reward": nested_container["score"], "nested_container": nested_container}})
"""


def _task(root: Path) -> Path:
    """A VM Task whose image runs Docker, for an agent with no network."""
    write_task(
        root,
        name="agentslastexam/docker-in-vm",
        config={
            "artifacts": [OUTPUT],
            "environment": {"image": VM_BASE, **VM_RESOURCES, "storage_mb": 8192},
            "agent": {"timeout_sec": 300},
        },
        dockerfile=None,
        instruction="Run the nested container workload and leave its evidence in the declared "
        "output file.\n",
        test='#!/usr/bin/env bash\nset -euo pipefail\nexec "$ORBIT_VERIFY_PYTHON" verify.py\n',
        solve=SOLVE,
    )
    setup = root / "environment/setup/setup.sh"
    setup.parent.mkdir(parents=True)
    (root / "environment/build.sh").write_text(BUILD)
    setup.write_text(SETUP)
    setup.chmod(0o755)
    (root / "tests/verify.py").write_text(VERIFY)
    return root


@pytest.mark.asyncio
async def test_task_owned_docker_runs_a_nested_offline_container(tmp_path: Path) -> None:
    task = _task(tmp_path / "docker-in-vm")
    settings = RunConfig()
    phases: list[Phase] = []
    result = await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness()),
        ProviderRegistry(settings),
        run_dir=tmp_path / "runs",
        phase_callback=phases.append,
        provenance=ProvenanceInputs(
            source=TaskSource(kind="local", path=str(task)),
            agent=agent_provenance(OracleHarness(), ""),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
        collect_artifacts=True,
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    (entry,) = json.loads((result.run_dir / "artifacts/snapshot.json").read_text())["entries"]
    assert entry["source"] == OUTPUT
    stored = result.run_dir / "artifacts" / entry["stored_path"]
    assert stored.read_text() == "nested-container-ok\n"
    assert phases == [
        Phase.PROVISION,
        Phase.SETUP,
        Phase.AGENT,
        Phase.VERIFY,
        Phase.TEARDOWN,
    ]
    assert result.lock is not None
    assert result.lock.framework.schema_version == 3
    assert result.lock.resources is not None
    assert result.lock.resources.effective.storage_mb is not None
    assert result.lock.resources.effective.storage_mb >= 8192
    image = result.lock.image
    assert image.declaration.kind == "vm"
    assert image.source == "local"
    assert image.provider == "qemu"
    assert image.observed_identity == image.prepared_identity
    assert image.builder_identity and image.base_materials
    assert "content_digest" not in image.model_dump()

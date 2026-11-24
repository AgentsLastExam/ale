from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from orbit.core.config import SandboxRetentionConfig
from orbit.core.sandbox import ImageRef, PreparedTaskImage, SandboxRequest
from orbit.core.taskspec import ImageKind, ImageSpec, NetworkMode, NetworkPolicy, VerifySpec
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.providers.docker import DockerProvider
from orbit.run.providers.firecracker import FirecrackerProvider
from orbit.run.providers.qemu import QemuProvider
from orbit.run.scaffold import scaffold_task
from orbit.run.task_images import (
    _OciBuild,
    prepare_verifier_image_result,
)
from orbit.run.tasksets.manifest import load_tasks
from tests.support import (
    CONTAINER_BASE,
    VM_BASE,
    VM_RESOURCES,
    edit_task_config,
    provider_registry,
    skip_without_gvisor,
    skip_without_kata,
    write_task,
)

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


@pytest.fixture
def framework_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake image identities are not Docker images; the layer is tested on real ones."""

    async def layer(image: str, *, user: str, keep_alive: bool = False) -> str:
        return image if image.startswith("sha256:") else DIGEST

    monkeypatch.setattr("orbit.run.task_images.with_framework", layer)

    async def disk(image, **kwargs):
        return image, True

    monkeypatch.setattr("orbit.run.task_images.framed_disk", disk)


DIGEST = "sha256:" + "d" * 64


def _prepared(kind: ImageKind, source: str = "solver-local") -> PreparedTaskImage:
    values: dict[str, object] = {
        "kind": kind,
        "source": source,
        "input_identity": DIGEST,
        "runtime_ref": "orbit:local" if kind is ImageKind.CONTAINER else "/tmp/disk.qcow2",
        "prepared_identity": DIGEST,
    }
    if source == "external-ref":
        values["resolved_reference"] = "ghcr.io/acme/image@" + DIGEST
    else:
        values["image_source_identity"] = DIGEST
    if kind is ImageKind.VM and source != "external-ref":
        values["builder_identity"] = DIGEST
        values["base_materials"] = (DIGEST,)
    return PreparedTaskImage.model_validate(values)


class _Provider:
    def __init__(self, kind: ImageKind) -> None:
        self.kind = kind
        self.inputs: list[ImageRef | PreparedTaskImage] = []

    async def prepare_image(self, image):  # type: ignore[no-untyped-def]
        self.inputs.append(image)
        return (
            image if isinstance(image, PreparedTaskImage) else _prepared(self.kind, "external-ref")
        )


def task_repository(tmp_path: Path, name: str) -> tuple[Path, Path]:
    repository = tmp_path / "ale-tasks-demo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    task = scaffold_task(repository / "tasks" / name)
    return repository, task


async def execute(task_path: Path, run_dir: Path, *, collect: bool = True):  # type: ignore[no-untyped-def]
    return await run_episode(
        load_tasks(task_path)[0],
        EpisodeProtocol(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=run_dir,
        collect_artifacts=collect,
    )


@pytest.mark.asyncio
async def test_shared_verification_has_no_second_prepared_image(tmp_path: Path) -> None:
    task = load_tasks(scaffold_task(tmp_path / "shared-image"))[0]
    task.prepared_image = _prepared(ImageKind.CONTAINER)
    assert await prepare_verifier_image_result(task, _Provider(ImageKind.CONTAINER)) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", tuple(ImageKind))
async def test_separate_verifier_reuses_solver_prepared_kind(
    tmp_path: Path, kind: ImageKind
) -> None:
    task = load_tasks(scaffold_task(tmp_path / f"reuse-{kind}"))[0]
    # Only a container is built from the Task folder; a VM solver names its image.
    named = None if kind is ImageKind.CONTAINER else "ghcr.io/acme/solver:v1"
    task.spec = task.spec.model_copy(
        update={
            "image": ImageSpec(kind=kind, ref=named),
            "verify": VerifySpec.model_validate(
                {
                    "environment_mode": "separate",
                    "resources": {
                        "cpus": 1,
                        "memory_mb": 512,
                        "storage_mb": None,
                        "gpus": 0,
                    },
                }
            ),
        }
    )
    task.prepared_image = _prepared(kind)
    result = await prepare_verifier_image_result(task, _Provider(kind))  # type: ignore[arg-type]
    assert result is not None
    assert result.image is task.prepared_image
    assert result.image.kind is kind
    assert result.steps[-1].outcome == "reused"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", tuple(ImageKind))
async def test_external_separate_verifier_uses_matching_provider(
    tmp_path: Path, kind: ImageKind, framework_layer: None
) -> None:
    task = load_tasks(scaffold_task(tmp_path / f"external-{kind}"))[0]
    task.spec = task.spec.model_copy(
        update={
            "verify": VerifySpec.model_validate(
                {
                    "environment_mode": "separate",
                    "image": {"kind": kind, "ref": "ghcr.io/acme/verifier:v1"},
                    "resources": {
                        "cpus": 1,
                        "memory_mb": 512,
                        "storage_mb": None,
                        "gpus": 0,
                    },
                }
            )
        }
    )
    task.prepared_image = _prepared(ImageKind.CONTAINER)
    provider = _Provider(kind)
    result = await prepare_verifier_image_result(task, provider)  # type: ignore[arg-type]
    assert result is not None and result.image.kind is kind
    assert provider.inputs == [ImageRef(kind=kind, reference="ghcr.io/acme/verifier:v1")]


@pytest.mark.asyncio
async def test_local_separate_verifier_builds_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework_layer: None
) -> None:
    task = load_tasks(scaffold_task(tmp_path / "local-verifier"))[0]
    (task.folder.root / "tests/Dockerfile").write_text("FROM fixture\n")
    task.spec = task.spec.model_copy(
        update={
            "verify": VerifySpec.model_validate(
                {
                    "environment_mode": "separate",
                    "image": {"kind": "container"},
                    "resources": {
                        "cpus": 1,
                        "memory_mb": 512,
                        "storage_mb": None,
                        "gpus": 0,
                    },
                }
            )
        }
    )
    task.verifier_image_source_digest = DIGEST
    build = _OciBuild(DIGEST, DIGEST, "orbit:verifier", DIGEST, ())

    async def build_oci(**_kwargs: object) -> _OciBuild:
        return build

    monkeypatch.setattr("orbit.run.task_images._build_oci", build_oci)
    result = await prepare_verifier_image_result(task, _Provider(ImageKind.CONTAINER))  # type: ignore[arg-type]
    assert result is not None
    assert result.image.kind is ImageKind.CONTAINER
    assert result.image.source == "verifier-local"


@pytest.mark.asyncio
async def test_task_local_helper_and_framework_package_are_importable(tmp_path: Path) -> None:
    task_path = scaffold_task(tmp_path / "local-helper")
    (task_path / "tests" / "helper.py").write_text(
        "from orbit_verify import checks\n"
        "\n"
        "def result():\n"
        "    return checks.text_equals('/home/user/output/result.txt', 'hello\\n')\n"
    )
    (task_path / "tests" / "verify.py").write_text(
        "from orbit_verify import write\n"
        "from helper import result\n"
        "v = {}\n"
        "v['local'] = result()['score']\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await execute(task_path, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_solution_assets_are_staged_with_the_solution(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "solution-assets")
    asset = task / "solution/assets/result.txt"
    asset.parent.mkdir(parents=True)
    asset.write_text("hello\n")
    (task / "solution/solve.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cp assets/result.txt /home/user/output/result.txt\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_tests_assets_are_withheld_then_available_by_relative_path(
    tmp_path: Path,
) -> None:
    _, task = task_repository(tmp_path, "external-reference")
    setup = task / "environment/setup"
    setup.mkdir()
    (setup / "setup.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ntest ! -e /opt/orbit/tests/assets/reference.txt\n"
    )
    (setup / "setup.sh").chmod(0o755)
    reference = task / "tests/assets/reference.txt"
    reference.parent.mkdir(parents=True)
    reference.write_text("withheld")
    (task / "tests" / "verify.py").write_text(
        "from orbit_verify import checks, write\n"
        "v = {}\n"
        "v['reference'] = checks.text_equals('assets/reference.txt', 'withheld')['score']\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_no_assets_verifier_does_not_receive_a_synthetic_asset_root(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "no-assets")
    (task / "tests" / "verify.py").write_text(
        "from pathlib import Path\n"
        "from orbit_verify import write\n"
        "v = {}\n"
        "v['absent'] = float(not Path('assets').exists())\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_missing_requested_verify_asset_is_a_task_error_not_zero(
    tmp_path: Path,
) -> None:
    task = scaffold_task(tmp_path / "missing-asset")
    (task / "tests" / "verify.py").write_text(
        "from pathlib import Path\nPath('assets/missing.json').read_text()\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None
    assert "tests/ failed with exit code" in result.verdict.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("collect", [True, False], ids=["host", "none"])
async def test_separate_verifier_restores_file_and_directory_to_exact_paths(
    tmp_path: Path, collect: bool
) -> None:
    task = scaffold_task(tmp_path / "separate")
    edit_task_config(
        task,
        {
            "artifacts": ["/home/user/output/result.json", "/var/lib/orbit-report"],
            "environment": {"memory_mb": 512},
            "verifier": {"environment": {"cpus": 1, "memory_mb": 512}},
        },
    )
    (task / "instruction.md").write_text("Produce both declared outputs.\n")
    (task / "environment/Dockerfile").write_text(
        f"FROM {CONTAINER_BASE}\n"
        "RUN mkdir -p /home/user/output /var/lib/orbit-report "
        "&& printf '{}\\n' > /home/user/output/result.json "
        "&& chown -R user:user /home/user/output /var/lib/orbit-report\n"
    )
    (task / "tests/Dockerfile").write_text(f"FROM {CONTAINER_BASE}\n")
    (task / "solution/solve.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        'printf \'{"status":"ok"}\\n\' > /home/user/output/result.json\n'
        "printf ready > /var/lib/orbit-report/state.txt\n"
    )
    (task / "tests/verify.py").write_text(
        "from orbit_verify import checks, write\n"
        "v = {}\n"
        "v['file'] = checks.json_value('/home/user/output/result.json', 'status', 'ok')['score']\n"
        "v['directory'] = checks.text_equals('/var/lib/orbit-report/state.txt', 'ready')['score']\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )

    result = await execute(task, tmp_path / "runs", collect=collect)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    # The directory is a volume the verifier views from the solver's snapshot, which is
    # released after it.
    assert [outcome.roles for outcome in result.record.sandboxes] == [
        ("verifier",),
        ("solver",),
    ]
    snapshot = result.run_dir / "artifacts/snapshot.json"
    if collect:
        assert '"source": "/var/lib/orbit-report"' in snapshot.read_text()
    else:
        assert not snapshot.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True], ids=["solver-image", "external-ref"])
async def test_separate_verifier_image_sources_restore_identically_without_setup(
    tmp_path: Path,
    named: bool,
) -> None:
    task = scaffold_task(tmp_path / ("separate-ref" if named else "separate-reuse"))
    verifier = {"cpus": 1, "memory_mb": 512} | ({"image": CONTAINER_BASE} if named else {})
    edit_task_config(
        task,
        {
            "artifacts": ["/home/user/output/result.txt"],
            "environment": {"memory_mb": 512},
            "verifier": {"environment": verifier},
        },
    )
    (task / "instruction.md").write_text("Write the requested output.\n")
    setup = task / "environment/setup/setup.sh"
    setup.parent.mkdir()
    setup.write_text("#!/bin/sh\nset -eu\ntouch /tmp/solver-setup-ran\n")
    setup.chmod(0o755)
    (task / "solution/solve.sh").write_text(
        "#!/bin/sh\nset -eu\nprintf 'hello\\n' > /home/user/output/result.txt\n"
    )
    (task / "tests/verify.py").write_text(
        "from pathlib import Path\n"
        "from orbit_verify import checks, write\n"
        "v = {}\n"
        "v['content'] = checks.text_equals('/home/user/output/result.txt', 'hello\\n')['score']\n"
        "v['fresh'] = float(not Path('/tmp/solver-setup-ran').exists())\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )

    result = await execute(task, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate"])
async def test_privileged_artifacts_glob_and_never_follow_links(tmp_path, mode):
    task = scaffold_task(tmp_path / f"copy-{mode}")
    (task / "instruction.md").write_text("Leave the prepared outputs for verification.\n")
    edit_task_config(
        task,
        {
            "artifacts": ["/root/answers/*.sgf", "/root/missing", "/root/answers/result.json"],
            "environment": {"memory_mb": 512},
            "verifier": {"environment_mode": mode},
        },
    )
    setup = task / "environment/setup/setup.sh"
    setup.parent.mkdir()
    setup.write_text(
        "#!/bin/sh\nset -eu\n"
        "mkdir -p /root/answers/result.json /root/private\n"
        "printf protected > /root/private/value\n"
        "chmod 000 /root/private/value\n"
        "ln -s /root/private/value /root/answers/game.sgf\n"
        "ln -s /root/missing /root/answers/broken.sgf\n"
        "ln -s /root/answers/result.json /root/answers/result.json/loop\n"
    )
    setup.chmod(0o755)
    (task / "solution/solve.sh").write_text("#!/bin/sh\nset -eu\n[ $(id -u) -ne 0 ]\n")
    (task / "tests/verify.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from orbit_verify import write\n"
        "assert os.geteuid() == 0\n"
        "v = {}\n"
        # A link the agent left is never followed, so the protected file it points at
        # cannot reach a separate verifier; a shared verifier sees the sandbox as left.
        + (
            "v['link'] = float(not os.path.lexists('/root/answers/game.sgf'))\n"
            if mode == "separate"
            else "v['link'] = float(os.path.islink('/root/answers/game.sgf'))\n"
        )
        + "v['directory'] = float(Path('/root/answers/result.json').is_dir())\n"
        "v['missing'] = float(not Path('/root/missing').exists())\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    entries = json.loads((result.run_dir / "artifacts/snapshot.json").read_text())["entries"]
    assert not any(
        entry["source"].endswith(("game.sgf", "broken.sgf", "/loop")) for entry in entries
    )


#: What a snapshot verifier checks of the Sandbox it boots in: the agent's file outside any
#: declared artifact, the solver's account and no stage of the solver's; under runc, from
#: its cgroups, and in a microVM, from its kernel, its own CPUs and memory (MiB, the second
#: argument) rather than its solver's. It leaves a file of its own, which must not reach
#: the solver.
_SNAPSHOT_VERIFY = """\
import os
import pwd
import sys
from pathlib import Path
from orbit_verify import write

def cgroup(name):
    return Path('/sys/fs/cgroup', name).read_text().split()

runtime, memory = sys.argv[1], int(sys.argv[2])
v = {}
v['checkpoint'] = float(Path('/home/user/checkpoints/step-1000.bin').read_text() == 'weights')
v['setup'] = float(not any(Path('/opt/orbit/setup').glob('*')))
v['account'] = float(pwd.getpwnam('user').pw_dir == '/home/user')
if runtime == 'runc':
    v['cpus'] = float(cgroup('cpu.max') == ['200000', '100000'])
    v['memory'] = float(int(cgroup('memory.max')[0]) == memory << 20)
if runtime == 'firecracker':
    v['cpus'] = float(os.cpu_count() == 2)
    total = next(line for line in open('/proc/meminfo') if line.startswith('MemTotal:'))
    v['memory'] = float(memory - 512 < int(total.split()[1]) >> 10 <= memory)
Path('/home/user/verifier-was-here').write_text('verifier')
v['reward'] = sum(v.values()) / len(v.values())
write(v)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime", "retention"),
    [
        ("runc", "destroy"),
        ("runc", "snapshot"),
        ("kata", "destroy"),
        ("gvisor", "destroy"),
        pytest.param("firecracker", "destroy", marks=pytest.mark.needs_kvm),
    ],
)
async def test_a_snapshot_verifier_sees_the_solvers_disk_with_its_own_resources(
    tmp_path: Path, runtime: str, retention: str
) -> None:
    if runtime == "kata":
        skip_without_kata()
    elif runtime == "gvisor":
        skip_without_gvisor()
    provider: DockerProvider | FirecrackerProvider = (
        FirecrackerProvider() if runtime == "firecracker" else DockerProvider(runtime=runtime)
    )
    # A microVM's memory also holds its kernel, and the base starts a GNOME session.
    memory = 2048 if runtime == "firecracker" else 512
    task = scaffold_task(tmp_path / f"snapshot-{runtime}-{retention}")
    edit_task_config(
        task,
        {
            "artifacts": [],
            "environment": {"cpus": 1, "memory_mb": memory},
            "verifier": {
                "environment_mode": "snapshot",
                "environment": {"cpus": 2, "memory_mb": 2 * memory},
            },
        },
    )
    (task / "instruction.md").write_text("Train, and leave your checkpoint.\n")
    setup = task / "environment/setup/setup.sh"
    setup.parent.mkdir()
    setup.write_text("#!/bin/sh\nset -eu\ntrue\n")
    (task / "solution/solve.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /home/user/checkpoints\n"
        "printf weights > /home/user/checkpoints/step-1000.bin\n"
    )
    (task / "tests/test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'exec "$ORBIT_VERIFY_PYTHON" verify.py {runtime} {2 * memory}\n'
    )
    (task / "tests/verify.py").write_text(_SNAPSHOT_VERIFY)

    result = await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness()),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(solver=retention),  # type: ignore[arg-type]
    )

    kept = [outcome.handle for outcome in result.record.sandboxes if outcome.handle]
    try:
        assert result.verdict.status is Status.COMPLETED, result.verdict.failure
        verification = json.loads((result.run_dir / "verification.json").read_text())
        assert verification["reward"] == 1.0, verification
        trace = [
            json.loads(line)
            for line in (result.run_dir / "trace.execution.jsonl").read_text().splitlines()
        ]
        # Saved for the solver, then destroyed or kept: the trace names it by its handle.
        (handle,) = {
            record["data"]["handle"]
            for record in trace
            if "handle" in (record.get("data") or {}) and record["phase"] == "agent"
        }
        image = handle.replace("docker:snapshot:", "orbit-snapshot:")
        by_role = {outcome.roles: outcome for outcome in result.record.sandboxes}
        assert by_role[("verifier",)].outcome == "destroyed"
        listed = {str(item["handle"]) for item in await provider.list_retained()}
        if retention == "destroy":
            assert by_role[("solver",)].outcome == "destroyed"
            assert handle not in listed
            if runtime != "firecracker":
                gone = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
                assert gone.returncode != 0
        else:
            assert kept == [handle] and handle in listed
            assert by_role[("solver",)].outcome == "snapshotted"
            # The kept snapshot is the solver's disk as it was before the verifier ran.
            probe = subprocess.run(
                ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
                 "test -s /home/user/checkpoints/step-1000.bin "
                 "&& test ! -e /home/user/verifier-was-here && test ! -e /logs/verifier"],
                capture_output=True,
            )  # fmt: skip
            assert probe.returncode == 0, probe.stderr
    finally:
        for handle in kept:
            await provider.destroy_retained(handle)


#: A VM verifier's own CPUs and memory are its guest's, so it reads them directly.
_VM_SNAPSHOT_VERIFY = """\
import os
from pathlib import Path
from orbit_verify import write

def memory_mb():
    if os.name == 'nt':
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in ('total', 'free', 'page', 'page_free', 'virtual', 'virtual_free', 'x')
            ]

        status = Status()
        status.length = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.total >> 20
    line = next(line for line in open('/proc/meminfo') if line.startswith('MemTotal:'))
    return int(line.split()[1]) >> 10

v = {}
v['checkpoint'] = float(Path(HOME, 'checkpoints', 'step-1000.bin').read_text() == 'weights')
v['cpus'] = float(os.cpu_count() == 4)
# The guest's kernel and firmware keep some of what the VM is given.
v['memory'] = float(5120 < memory_mb() <= 6144)
Path(HOME, 'verifier-was-here').write_text('verifier')
v['reward'] = sum(v.values()) / len(v.values())
write(v)
"""


@pytest.mark.needs_kvm
@pytest.mark.asyncio
@pytest.mark.parametrize("guest", ["linux", "windows"])
async def test_a_snapshot_verifier_boots_a_vm_of_its_own_size_on_the_solvers_disk(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    guest: str,
) -> None:
    request.getfixturevalue("docker_buildx")
    request.getfixturevalue("kvm_host")
    root = tmp_path / "snapshot-vm"
    verifier = {"environment_mode": "snapshot", "environment": {"cpus": 4, "memory_mb": 6144}}
    if guest == "windows":
        image = Path(os.environ.get("ORBIT_TEST_WINDOWS_IMAGE", ""))
        if not image.is_file():
            pytest.skip("set ORBIT_TEST_WINDOWS_IMAGE to the Windows base qcow2")
        monkeypatch.setattr("orbit.run.task_images.cache_root", lambda: tmp_path / "cache")
        provider = QemuProvider(
            image=image, overlay_dir=tmp_path / "qemu", snapshot_dir=tmp_path / "snapshots"
        )
        home = "C:/Users/user"
        write_task(
            root,
            name="agentslastexam/snapshot-vm",
            config={
                "artifacts": [],
                "environment": {
                    "os": "windows",
                    "image": "local/windows-base",
                    "cpus": 2,
                    "memory_mb": 4096,
                },
                "agent": {"timeout_sec": 600},
                "verifier": {**verifier, "timeout_sec": 600},
            },
            dockerfile=None,
            test=None,
            solve=None,
        )
        for directory in ("environment", "tests", "solution"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        # Built on the base, so the image gets the Windows framework.
        (root / "environment/build.ps1").write_text("exit 0\n")
        (root / "solution/solve.ps1").write_text(
            "New-Item -ItemType Directory -Force C:\\Users\\user\\checkpoints | Out-Null\n"
            "Set-Content -NoNewline -Value weights C:\\Users\\user\\checkpoints\\step-1000.bin\n"
        )
        (root / "tests/test.ps1").write_text(
            "& $env:ORBIT_VERIFY_PYTHON .\\verify.py\nexit $LASTEXITCODE\n"
        )
    else:
        provider = QemuProvider(overlay_dir=tmp_path / "qemu", snapshot_dir=tmp_path / "snapshots")
        home = "/home/user"
        scaffold_task(root)
        edit_task_config(
            root,
            {
                "artifacts": [],
                "environment": {"image": VM_BASE, **VM_RESOURCES},
                "verifier": verifier,
            },
        )
        (root / "environment/Dockerfile").unlink()
        (root / "environment/build.sh").write_text("true\n")
        (root / "solution/solve.sh").write_text(
            "#!/bin/sh\nset -eu\nmkdir -p /home/user/checkpoints\n"
            "printf weights > /home/user/checkpoints/step-1000.bin\n"
        )
    (root / "tests/verify.py").write_text(f"HOME = {home!r}\n" + _VM_SNAPSHOT_VERIFY)
    task = load_tasks(root)[0]

    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(solver="snapshot"),
    )

    kept = [outcome.handle for outcome in result.record.sandboxes if outcome.handle]
    try:
        assert result.verdict.status is Status.COMPLETED, result.verdict.failure
        verification = json.loads((result.run_dir / "verification.json").read_text())
        assert verification["reward"] == 1.0, verification
        by_role = {outcome.roles: outcome for outcome in result.record.sandboxes}
        assert by_role[("verifier",)].outcome == "destroyed"
        assert by_role[("solver",)].outcome == "snapshotted"
        assert kept == [by_role[("solver",)].handle]
        assert {str(item["handle"]) for item in await provider.list_retained()} == set(kept)
        # The kept snapshot is the solver's disk as it was: the verifier wrote nothing to it.
        assert task.prepared_image is not None
        probe = await provider.create(
            SandboxRequest(
                episode_id="probe",
                os=task.spec.os,
                prepared_image=task.prepared_image,
                snapshot=kept[0],
                resources=task.spec.resources,
                network=NetworkPolicy(mode=NetworkMode.BLOCK),
                user=task.spec.image.user,
            )
        )
        try:
            assert await probe.read_file(f"{home}/checkpoints/step-1000.bin") == b"weights"
            assert not await probe.list_files(f"{home}/verifier-was-here")
        finally:
            await probe.destroy()
    finally:
        for handle in kept:
            await provider.destroy_retained(handle)
    assert not list((tmp_path / "snapshots").iterdir())

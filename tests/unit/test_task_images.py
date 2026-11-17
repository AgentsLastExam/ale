from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.support import CONTAINER_BASE, edit_task_config

from ale.core.capability import Arch, CapabilityProfile
from ale.core.config import RunConfig, SandboxRetentionConfig
from ale.core.errors import ProviderCapabilityError, ProviderStartError, TaskDefinitionError
from ale.core.ids import content_hash
from ale.core.sandbox import ExecResult, ImageRef, PreparedTaskImage, Provider
from ale.core.task import Task
from ale.core.taskspec import (
    ContainerFeature,
    ImageKind,
    ImageSpec,
    NetworkMode,
    OperatingSystem,
    VerifySpec,
)
from ale.run.cli.tasks import _prepare_images
from ale.run.providers import ProviderRegistry
from ale.run.providers.fleet import FleetProvider
from ale.run.task_images import (
    prepare_task_image,
    prepare_task_image_result,
    prepare_verifier_image,
    prepare_verifier_image_result,
    resolve_image_kinds,
)
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit

#: What the framework was asked to frame, by the tests that look: an image or disk, the
#: account, and whether it is a VM disk.
LAYERED: list[tuple[str, str, bool]] = []


@pytest.fixture(autouse=True)
def framework_layer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """These tests exercise the pipeline; the layer itself is tested in test_framework."""

    async def layer(image: str, *, user: str) -> str:
        LAYERED.append((image, user, False))
        return image

    async def frame(
        image: PreparedTaskImage, *, operating_system: OperatingSystem, user: str
    ) -> tuple:
        LAYERED.append((image.runtime_ref, user, True))
        return image, False

    LAYERED.clear()
    monkeypatch.setattr("ale.run.task_images.with_framework", layer)
    monkeypatch.setattr("ale.run.task_images.framed_disk", frame)
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path / "ale-cache"))


def _task(write_task_repo, **kwargs: object) -> object:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("demo",), **kwargs)
    return load_tasks(repository / "tasks/demo")[0]


def _classified(task: Task, kind: ImageKind) -> Task:
    """``task`` as it reaches preparation: each image it names alone found to be ``kind``.

    Loading leaves a Linux registry reference's kind to the registry, which is read before
    anything prepares the image.
    """

    def decided(image: ImageSpec | None) -> ImageSpec | None:
        return image.model_copy(update={"kind": kind}) if image and image.kind is None else image

    spec = task.spec
    verify = spec.verify.model_copy(update={"image": decided(spec.verify.image)})
    task.spec = spec.model_copy(update={"image": decided(spec.image), "verify": verify})
    return task


def _prepared(kind: ImageKind, source: str = "solver-local") -> PreparedTaskImage:
    digest = "sha256:" + "d" * 64
    values: dict[str, object] = {
        "kind": kind,
        "source": source,
        "input_identity": digest,
        "runtime_ref": "ale-local:fixture" if kind is ImageKind.CONTAINER else "/tmp/disk.qcow2",
        "prepared_identity": digest,
    }
    if source == "external-ref":
        values["resolved_reference"] = "ghcr.io/acme/image@" + digest
    else:
        values["image_source_identity"] = digest
    if kind is ImageKind.VM and source != "external-ref":
        values["builder_identity"] = digest
        values["base_materials"] = (digest,)
    return PreparedTaskImage.model_validate(values)


class _Provider:
    def __init__(
        self, kind: ImageKind, features: frozenset[ContainerFeature] = frozenset()
    ) -> None:
        self.kind = kind
        self.features = features
        self.inputs: list[ImageRef | PreparedTaskImage] = []

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return tuple(
            CapabilityProfile(
                provider="recording",
                kind=self.kind,
                os=system,
                arch=Arch.of(system),
                gui=True,
                network_modes=frozenset(NetworkMode),
                storage_limit=True,
                container_features=self.features,
            )
            for system in OperatingSystem
        )

    refusals = Provider.refusals
    unserved = Provider.unserved

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        assert image.kind is self.kind
        self.inputs.append(image)
        if isinstance(image, PreparedTaskImage):
            return image
        digest = "sha256:" + ("a" if self.kind is ImageKind.CONTAINER else "b") * 64
        return PreparedTaskImage(
            kind=self.kind,
            source="external-ref",
            input_identity=digest,
            runtime_ref=(
                "ghcr.io/acme/image@" + digest
                if self.kind is ImageKind.CONTAINER
                else "/cache/disk.qcow2"
            ),
            prepared_identity=digest,
            resolved_reference="ghcr.io/acme/image@" + digest,
        )


class _Registry(ProviderRegistry):
    """A Run's registry whose container and VM leaves, ``providers`` by the kind they
    prepare, record what they are asked to prepare."""

    def __init__(self) -> None:
        super().__init__(RunConfig())
        self.providers = {kind: _Provider(kind) for kind in ImageKind}
        vm = self.providers[ImageKind.VM]
        self._instances.update(
            {"docker:runc": self.providers[ImageKind.CONTAINER], "qemu": vm, "orchard": vm}  # type: ignore[dict-item]
        )


@pytest.fixture
def windows_task(write_task_repo) -> Task:  # type: ignore[no-untyped-def]
    repository = write_task_repo(
        config={
            "environment": {"os": "windows", "image": "/images/windows.qcow2"},
        },
        dockerfile=None,
    )
    folder = repository / "tasks/demo"
    (folder / "environment").mkdir()
    (folder / "environment/build.ps1").write_text("Write-Output 'installed'\n")
    (folder / "tests/test.sh").rename(folder / "tests/test.ps1")
    (folder / "calibration/solution/solve.sh").rename(folder / "calibration/solution/solve.ps1")
    return load_tasks(folder)[0]


@pytest.fixture
def linux_vm_task(write_task_repo) -> Task:  # type: ignore[no-untyped-def]
    repository = write_task_repo(
        config={"environment": {"image": "ghcr.io/acme/vm-base:1"}}, dockerfile=None
    )
    folder = repository / "tasks/demo"
    (folder / "environment").mkdir()
    (folder / "environment/build.sh").write_text("apt-get install -y jq\n")
    return load_tasks(folder)[0]


def _qemu_builder(  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None = None
):
    """The real Linux or Windows build, in a builder VM whose sandbox only records what it
    is told.

    Its ``save_image`` keeps the layer it is given; ``finished_layer`` reads it back."""
    import ale.run.task_images as images
    from ale.run.providers.qemu import QemuSandbox

    built: dict[Path, str] = {}
    seen: SimpleNamespace = SimpleNamespace(
        requests=[], sandboxes=[], runner="sha256:" + "e" * 64, builder="sha256:" + "f" * 64
    )

    class Builder:
        def __init__(self, *, overlay_dir: Path, runner_image: str) -> None:
            assert runner_image == seen.runner

        async def create_builder(self, request):  # type: ignore[no-untyped-def]
            seen.requests.append(request)
            sandbox = AsyncMock(spec=QemuSandbox)
            windows = request.os is OperatingSystem.WINDOWS
            sandbox.agent_home = "C:\\Users\\user" if windows else "/home/user"
            seen.sandboxes.append(sandbox)

            async def run(argv, **kwargs):  # type: ignore[no-untyped-def]
                if "build.ps1" in argv[-1] or argv == ["bash", "build.sh"]:
                    assert (Path(kwargs["cwd"].replace("\\", "/")).name) == "environment"
                    if failure == "input-change":
                        environment = seen.task.folder.environment_dir
                        (environment / "added-during-build").mkdir()
                    if failure == "script":
                        return ExecResult(exit_code=1, stderr="installer failed")
                return ExecResult(exit_code=0)

            async def save(layer: Path) -> str:
                if failure == "save":
                    raise ProviderStartError("no space left")
                layer.write_bytes(b"layer")
                built[layer] = "sha256:" + content_hash({"build": len(built)})[7:]
                return built[layer]

            sandbox.exec.side_effect = run
            sandbox.save_image.side_effect = save
            return sandbox

    async def identity(reference: str) -> str:
        return seen.builder if reference == images.MATERIALIZER_IMAGE else seen.runner

    async def finished(layer: Path) -> str | None:
        return built.get(layer) if layer.is_file() else None

    async def remove(disk: Path) -> None:
        disk.unlink(missing_ok=True)

    monkeypatch.setattr(images, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(images, "_docker_image_identity", identity)
    monkeypatch.setattr(images, "QemuProvider", Builder)
    monkeypatch.setattr(images, "finished_layer", finished)
    monkeypatch.setattr(images, "remove_disk", remove)
    return seen, built


@pytest.mark.asyncio
async def test_windows_image_cache_tracks_all_inputs_including_cli_batches(
    windows_task: Task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ale.run.task_images as images

    seen, built = _qemu_builder(monkeypatch, tmp_path)
    registry = _Registry()
    first = await prepare_task_image_result(windows_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert registry.providers[ImageKind.VM].inputs[0] == ImageRef(
        kind=ImageKind.VM, reference="/images/windows.qcow2"
    )
    (request,) = seen.requests
    assert request.os is OperatingSystem.WINDOWS and request.user == "user"
    assert (request.resources.cpus, request.resources.memory_mb) == (2, 4096)
    assert request.network.mode is NetworkMode.OPEN
    # The build VM's disk is kept as the Task's layer, known by its own identity.
    (layer,) = built
    seen.sandboxes[0].save_image.assert_awaited_once_with(layer)
    assert layer.parent == tmp_path / "cache/vm-builds"
    assert first.image.runtime_ref == str(layer) and first.image.prepared_identity == built[layer]
    assert first.image.input_identity == "sha256:" + layer.stem
    second = await prepare_task_image_result(windows_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert len(seen.requests) == 1
    assert [step.name for step in first.steps] == [
        "lint",
        "base-resolution",
        "windows-build",
        "artifact-check",
    ]
    assert second.steps[2].outcome == "reused"
    assert first.image == second.image
    assert first.image.base_materials == ("sha256:" + "b" * 64,)
    assert first.image.builder_identity

    assert windows_task.folder is not None
    setup = windows_task.folder.environment_dir / "setup/setup.ps1"
    setup.parent.mkdir()
    setup.write_text("Write-Output 'per episode'\n")
    assert await prepare_task_image(windows_task, registry.providers[ImageKind.VM]) == first.image  # type: ignore[arg-type]
    assert len(seen.requests) == 1
    asset = windows_task.folder.environment_dir / "assets/input.txt"
    asset.parent.mkdir()
    for value in ("first", "changed"):
        asset.write_text(value)
        changed = await prepare_task_image(windows_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
        assert changed.prepared_identity != first.image.prepared_identity
    assert len(seen.requests) == 3
    (windows_task.folder.environment_dir / "empty").mkdir()
    await prepare_task_image(windows_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert len(seen.requests) == 4
    # Another runner, or another VM builder to frame the disk and discard, is another build.
    seen.runner = "sha256:" + "1" * 64
    await prepare_task_image(windows_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert len(seen.requests) == 5
    seen.builder = "sha256:" + "2" * 64
    await prepare_task_image(windows_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert len(seen.requests) == 6
    base = _prepared(ImageKind.VM, "external-ref")
    await images._prepare_qemu_image(windows_task, base)
    assert len(seen.requests) == 7

    other = load_tasks(windows_task.folder.root)[0]
    other.spec = other.spec.model_copy(
        update={
            "resources": other.spec.resources.model_copy(
                update={"storage_mb": (other.spec.resources.storage_mb or 0) + 4096}
            )
        }
    )
    monkeypatch.setattr("ale.run.cli.tasks.observe_task_assets", lambda task: None)
    results = await _prepare_images([windows_task, other], registry)  # type: ignore[arg-type]
    assert results[0].image.prepared_identity != results[1].image.prepared_identity
    assert len(seen.requests) == 8  # The unchanged first Task reuses its layer; the larger builds.


@pytest.mark.asyncio
async def test_a_linux_image_is_built_on_its_base_disk_as_on_windows(
    linux_vm_task: Task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ale.run.task_images as images

    seen, built = _qemu_builder(monkeypatch, tmp_path)
    registry = _Registry()
    first = await prepare_task_image_result(linux_vm_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert registry.providers[ImageKind.VM].inputs[0] == ImageRef(
        kind=ImageKind.VM, reference="ghcr.io/acme/vm-base:1"
    )
    (request,) = seen.requests
    assert request.os is OperatingSystem.LINUX and request.user == "user"
    assert request.network.mode is NetworkMode.OPEN
    # As root, in a copy of environment/ under root-only /opt/ale, which it leaves behind.
    (sandbox,) = seen.sandboxes
    assert sandbox.upload_dir.await_args.args[1] == "/opt/ale/environment"
    build, cleanup = sandbox.exec.await_args_list
    assert build.args[0] == ["bash", "build.sh"]
    assert build.kwargs["cwd"] == "/opt/ale/environment"
    assert build.kwargs["env"] == {"ALE_HOME": "/home/user"}
    assert "identity" not in build.kwargs
    assert cleanup.args[0] == ["rm", "-rf", "/opt/ale/environment"]
    (layer,) = built
    sandbox.save_image.assert_awaited_once_with(layer)
    assert layer.parent == tmp_path / "cache/vm-builds"
    assert first.image.runtime_ref == str(layer) and first.image.prepared_identity == built[layer]
    assert [step.name for step in first.steps] == [
        "lint",
        "base-resolution",
        "linux-build",
        "artifact-check",
    ]
    assert first.image.base_materials == ("sha256:" + "b" * 64,)
    assert first.image.builder_identity
    second = await prepare_task_image_result(linux_vm_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert second.steps[2].outcome == "reused" and second.image == first.image
    assert len(seen.requests) == 1
    # Another runner, VM builder, build contract, discard method or Linux framework is
    # another build.
    changes = (
        lambda: setattr(seen, "runner", "sha256:" + "1" * 64),
        lambda: setattr(seen, "builder", "sha256:" + "2" * 64),
        lambda: monkeypatch.setattr(images, "LINUX_BUILD_CONTRACT", "ale-linux-bash/next"),
        lambda: monkeypatch.setattr(images, "LINUX_DISCARD", (*images.LINUX_DISCARD, "-v")),
        lambda: monkeypatch.setattr(images, "framework_identity", lambda **_: "sha256:" + "3" * 64),
    )
    identities = {first.image.builder_identity}
    for change in changes:
        change()
        image = await prepare_task_image(linux_vm_task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
        identities.add(image.builder_identity)
    assert len(seen.requests) == len(identities) == len(changes) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["script", "input-change", "save"])
@pytest.mark.parametrize(
    ("fixture", "step"), [("windows_task", "windows"), ("linux_vm_task", "linux")]
)
async def test_failed_vm_build_does_not_publish_or_fall_back(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    fixture: str,
    step: str,
) -> None:
    task = request.getfixturevalue(fixture)
    seen, built = _qemu_builder(monkeypatch, tmp_path, failure)
    seen.task = task
    registry = _Registry()
    with pytest.raises((TaskDefinitionError, ProviderStartError), match=f"{step}-build:"):
        await prepare_task_image(task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert len(registry.providers[ImageKind.VM].inputs) == 1
    assert isinstance(registry.providers[ImageKind.VM].inputs[0], ImageRef)
    assert built == {} and list((tmp_path / "cache").rglob("*.qcow2")) == []
    (sandbox,) = seen.sandboxes
    sandbox.destroy.assert_awaited_once()


@pytest.fixture
def macos_task(write_task_repo) -> Task:  # type: ignore[no-untyped-def]
    repository = write_task_repo(
        config={
            "environment": {"os": "macos", "image": "ghcr.io/acme/macos:latest"},
            "agent": {"user": "admin", "network_mode": "public", "sudo": True},
        },
        dockerfile=None,
    )
    folder = repository / "tasks/demo"
    (folder / "environment").mkdir()
    (folder / "environment/build.sh").write_text("echo installed\n")
    return load_tasks(folder)[0]


def _macos_builder(  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    build_exit: int = 0,
    homes: tuple[str | None, str | None] = ("/Users/admin", "/Users/admin"),
):
    from ale.run.providers.orchard import OrchardProvider, OrchardSandbox

    provider = OrchardProvider(SimpleNamespace())
    base = _prepared(ImageKind.VM, "external-ref")
    kept: set[str] = set()

    async def prepare(image):  # type: ignore[no-untyped-def]
        if isinstance(image, ImageRef):
            return base
        if image.runtime_ref not in kept:
            raise ProviderCapabilityError("missing")
        return image

    sandbox = AsyncMock(spec=OrchardSandbox)
    sandbox.agent_home = "/Users/admin"
    sandbox.account_home.side_effect = list(homes)
    sandbox.exec.side_effect = [
        ExecResult(exit_code=build_exit, stderr="build failed" if build_exit else ""),
        ExecResult(exit_code=0),
    ]
    sandbox.save_image.side_effect = lambda image: kept.add(image.runtime_ref)
    requests = []

    async def create(request):  # type: ignore[no-untyped-def]
        requests.append(request)
        return sandbox

    monkeypatch.setattr(provider, "prepare_image", prepare)
    monkeypatch.setattr(provider, "create_builder", create)
    return provider, sandbox, requests, base


@pytest.mark.asyncio
async def test_macos_image_is_built_once_on_its_base(
    macos_task: Task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ale.run.task_images as images

    monkeypatch.setattr(images, "cache_root", lambda: tmp_path / "cache")
    registry, sandbox, requests, base = _macos_builder(monkeypatch)
    first = await prepare_task_image_result(macos_task, registry)  # type: ignore[arg-type]
    second = await prepare_task_image_result(macos_task, registry)  # type: ignore[arg-type]
    assert [step.name for step in first.steps] == [
        "lint",
        "base-resolution",
        "macos-build",
        "artifact-check",
    ]
    assert second.steps[2].outcome == "reused" and first.image == second.image
    assert first.image.runtime_ref.startswith("ale-built-")
    assert first.image.base_materials == (base.prepared_identity,)
    (request,) = requests
    assert request.os is OperatingSystem.MACOS and request.prepared_image == base
    assert (request.user, request.sudo) == ("admin", True)
    build, cleanup = sandbox.exec.await_args_list
    assert build.args[0] == ["bash", "build.sh"]
    assert build.kwargs["cwd"] == "/opt/ale/environment"
    assert build.kwargs["env"] == {"ALE_HOME": "/Users/admin"}
    assert cleanup.args[0] == ["rm", "-rf", "/opt/ale/environment"]
    sandbox.save_image.assert_awaited_once_with(first.image)
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_macos_build_gets_the_home_of_the_account_it_creates(
    write_task_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    import ale.run.task_images as images

    repository = write_task_repo(
        config={
            "environment": {"os": "macos", "image": "ghcr.io/acme/macos:latest"},
            "agent": {"user": "tester", "network_mode": "public"},
        },
        dockerfile=None,
    )
    folder = repository / "tasks/demo"
    (folder / "environment").mkdir()
    (folder / "environment/build.sh").write_text("sysadminctl -addUser tester\n")
    task = load_tasks(folder)[0]
    monkeypatch.setattr(images, "cache_root", lambda: tmp_path / "cache")
    registry, sandbox, requests, _ = _macos_builder(monkeypatch, homes=(None, "/Users/tester"))
    await prepare_task_image(task, registry)  # type: ignore[arg-type]
    (request,) = requests
    assert (request.user, request.sudo) == ("admin", True)
    sandbox.account_home.assert_awaited_with("tester")
    build, _ = sandbox.exec.await_args_list
    assert build.kwargs["env"] == {"ALE_HOME": "/Users/tester"}


@pytest.mark.parametrize(
    ("built", "message"),
    [(None, r"has no account 'admin'"), ("/Users/elsewhere", r"not ALE_HOME \(/Users/admin\)")],
)
@pytest.mark.asyncio
async def test_macos_build_must_leave_the_account_at_ale_home(
    macos_task: Task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, built, message
) -> None:  # type: ignore[no-untyped-def]
    import ale.run.task_images as images

    monkeypatch.setattr(images, "cache_root", lambda: tmp_path / "cache")
    registry, sandbox, _, _ = _macos_builder(monkeypatch, homes=("/Users/admin", built))
    with pytest.raises(TaskDefinitionError, match=message):
        await prepare_task_image(macos_task, registry)  # type: ignore[arg-type]
    sandbox.save_image.assert_not_awaited()
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_macos_build_keeps_no_image(
    macos_task: Task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ale.run.task_images as images

    monkeypatch.setattr(images, "cache_root", lambda: tmp_path / "cache")
    registry, sandbox, _, _ = _macos_builder(monkeypatch, build_exit=1)
    with pytest.raises(TaskDefinitionError, match=r"macos-build:.*build failed"):
        await prepare_task_image(macos_task, registry)  # type: ignore[arg-type]
    sandbox.save_image.assert_not_awaited()
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_buildx_uses_fixed_context_and_observes_container_identity(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)
    (task.folder.environment_dir / ".dockerignore").write_text("!setup\n*.log\n")  # type: ignore[attr-defined]
    commands: list[tuple[str, ...]] = []
    ignored: list[str] = []

    async def fake_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[:3] == ("docker", "buildx", "version"):
            return 0, "buildx", ""
        if argv[:3] == ("docker", "buildx", "build"):
            assert timeout == task.spec.image.build_timeout_sec
            dockerfile = Path(argv[argv.index("--file") + 1])
            assert dockerfile.read_text() == f"FROM {CONTAINER_BASE}\n"
            ignored.extend(dockerfile.with_name("Dockerfile.dockerignore").read_text().split())
            Path(argv[argv.index("--metadata-file") + 1]).write_text(
                json.dumps(
                    {
                        "materials": [{"uri": "pkg:docker/base"}],
                        "containerimage.config.digest": "sha256:" + "c" * 64,
                    }
                )
            )
            return 0, "", ""
        return 0, "sha256:" + "c" * 64 + "\n", ""

    monkeypatch.setattr("ale.run.task_images._run", fake_run)
    registry = _Registry()
    prepared = await prepare_task_image(task, registry.providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    build = next(command for command in commands if command[:3] == ("docker", "buildx", "build"))
    assert build[-1] == str(task.folder.environment_dir)  # type: ignore[attr-defined]
    # The Task's own rules come first, so it cannot bring setup/ back into the context.
    assert ignored == ["!setup", "*.log", "setup", "setup/**"]
    assert prepared.kind is ImageKind.CONTAINER
    assert prepared.source == "solver-local"
    assert prepared.runtime_ref == prepared.prepared_identity
    inspect = next(command for command in commands if command[:3] == ("docker", "image", "inspect"))
    assert inspect[-1] == prepared.prepared_identity
    assert prepared.prepared_identity == "sha256:" + "c" * 64
    assert registry.providers[ImageKind.CONTAINER].inputs == [prepared]
    assert [(prepared.runtime_ref, "user", False)] == LAYERED


@pytest.mark.asyncio
async def test_a_containerd_image_store_is_found_by_its_manifest_digest(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)
    manifest = "sha256:" + "d" * 64

    async def fake_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "buildx", "version"):
            return 0, "buildx", ""
        if argv[:3] == ("docker", "buildx", "build"):
            Path(argv[argv.index("--metadata-file") + 1]).write_text(
                json.dumps(
                    {
                        "containerimage.config.digest": "sha256:" + "c" * 64,
                        "containerimage.digest": manifest,
                    }
                )
            )
            return 0, "", ""
        if argv[:3] == ("docker", "image", "inspect") and argv[-1] == manifest:
            return 0, manifest + "\n", ""
        return 1, "", "No such image"

    monkeypatch.setattr("ale.run.task_images._run", fake_run)
    prepared = await prepare_task_image(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert prepared.prepared_identity == manifest


@pytest.mark.asyncio
async def test_unchanged_task_uses_its_prepared_image_without_building(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)
    asset = task.folder.environment_dir / "assets/data.txt"  # type: ignore[attr-defined]
    asset.parent.mkdir()
    asset.write_text("one")
    builds: list[tuple[str, ...]] = []
    images = {"sha256:" + "c" * 64}

    async def fake_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "buildx", "version"):
            return 0, "", ""
        if argv[:3] == ("docker", "buildx", "build"):
            builds.append(argv)
            images.add("sha256:" + "c" * 64)
            Path(argv[argv.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.config.digest": "sha256:" + "c" * 64})
            )
            return 0, "", ""
        if argv[:3] == ("docker", "image", "inspect") and argv[-1] in images:
            return 0, argv[-1], ""
        return 1, "", "No such image"

    monkeypatch.setattr("ale.run.task_images._run", fake_run)
    first = await prepare_task_image_result(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    second = await prepare_task_image_result(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert len(builds) == 1
    assert second.image == first.image
    assert [(step.name, step.outcome) for step in second.steps[1:3]] == [
        ("oci-build", "reused"),
        ("framework-layer", "reused"),
    ]

    setup = task.folder.environment_dir / "setup/setup.sh"  # type: ignore[attr-defined]
    setup.parent.mkdir()
    setup.write_text("echo per-episode\n")
    await prepare_task_image(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert len(builds) == 1  # setup/ is never part of the image

    asset.write_text("three")
    await prepare_task_image(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert len(builds) == 2

    # Elevation is set at every sandbox start, so the image serves either declaration.
    task.spec = task.spec.model_copy(
        update={"resources": task.spec.resources.model_copy(update={"sudo": True})}
    )
    await prepare_task_image(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert len(builds) == 2

    images.clear()
    await prepare_task_image(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert len(builds) == 3


@pytest.mark.asyncio
async def test_a_named_container_is_prepared_by_reference(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _task(
        write_task_repo,
        config={"environment": {"image": "ghcr.io/acme/t:v1"}},
        dockerfile=None,
    )
    _classified(task, ImageKind.CONTAINER)  # type: ignore[arg-type]
    registry = _Registry()
    await prepare_task_image(task, registry.providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert registry.providers[ImageKind.CONTAINER].inputs == [
        ImageRef(kind="container", reference="ghcr.io/acme/t:v1")
    ]


@pytest.mark.asyncio
async def test_build_failure_never_falls_back(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)

    async def fake_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "buildx", "version"):
            return 0, "", ""
        return 1, "", "Dockerfile line 3 failed"

    monkeypatch.setattr("ale.run.task_images._run", fake_run)
    registry = _Registry()
    with pytest.raises(TaskDefinitionError, match="Dockerfile line 3"):
        await prepare_task_image(task, registry.providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert registry.providers[ImageKind.CONTAINER].inputs == []


@pytest.mark.asyncio
async def test_ref_only_solver_is_prepared_by_matching_provider(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _classified(
        _task(
            write_task_repo,
            config={"environment": {"image": "ghcr.io/acme/task-vm:v1"}},
            dockerfile=None,
        ),
        ImageKind.VM,
    )
    registry = _Registry()
    prepared = await prepare_task_image(task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert prepared.kind is ImageKind.VM
    assert registry.providers[ImageKind.VM].inputs == [
        ImageRef(kind="vm", reference="ghcr.io/acme/task-vm:v1")
    ]
    assert registry.providers[ImageKind.CONTAINER].inputs == []


@pytest.mark.asyncio
async def test_preparation_result_orders_ref_and_artifact_stages(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _classified(
        _task(
            write_task_repo,
            config={"environment": {"image": "ghcr.io/acme/task-vm:v1"}},
            dockerfile=None,
        ),
        ImageKind.VM,
    )
    result = await prepare_task_image_result(task, _Registry().providers[ImageKind.VM])  # type: ignore[arg-type]
    assert result.task == "agentslastexam/demo"
    assert result.role == "solver"
    assert [step.name for step in result.steps] == [
        "lint",
        "reference-resolution",
        "framework-layer",
        "artifact-check",
    ]
    assert LAYERED == [("/cache/disk.qcow2", "user", True)]
    assert all(step.outcome == "executed" for step in result.steps)


@pytest.mark.asyncio
async def test_preparation_failure_names_and_bounds_its_stage(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _classified(
        _task(
            write_task_repo,
            config={"environment": {"image": "example.invalid/vm:latest"}},
            dockerfile=None,
        ),
        ImageKind.VM,
    )

    class _FailingProvider:
        async def prepare_image(self, image):  # type: ignore[no-untyped-def]
            raise ProviderStartError("x" * 3000 + "tail-marker")

    with pytest.raises(ProviderStartError, match=r"^reference-resolution: ") as caught:
        await prepare_task_image_result(task, _FailingProvider())  # type: ignore[arg-type]
    assert len(str(caught.value)) <= 2022
    assert str(caught.value).endswith("tail-marker")


@pytest.mark.asyncio
async def test_image_assets_do_not_change_the_local_source_identity(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)
    asset = task.folder.environment_dir / "assets/data.txt"  # type: ignore[attr-defined]
    asset.parent.mkdir()
    asset.write_text("one")

    async def fake_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "buildx", "version"):
            return 0, "", ""
        if argv[:3] == ("docker", "buildx", "build"):
            Path(argv[argv.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.config.digest": "sha256:" + "c" * 64})
            )
            return 0, "", ""
        return 0, "sha256:" + "c" * 64, ""

    monkeypatch.setattr("ale.run.task_images._run", fake_run)
    first = await prepare_task_image(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    asset.write_text("two")
    second = await prepare_task_image(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert first.input_identity == second.input_identity


@pytest.mark.asyncio
async def test_separate_verifier_reuses_complete_solver_image(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)
    task.spec = task.spec.model_copy(
        update={
            "verify": VerifySpec.model_validate(
                {
                    "environment_mode": "separate",
                    "resources": {"cpus": 1, "memory_mb": 512, "storage_mb": None, "gpus": 0},
                }
            )
        }
    )
    task.prepared_image = _prepared(ImageKind.VM)
    assert (
        await prepare_verifier_image(task, _Registry().providers[ImageKind.VM])
        is task.prepared_image
    )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_verifier_on_another_provider_runs_the_image_its_solver_ran(
    write_task_repo,
) -> None:  # type: ignore[no-untyped-def]
    """Its own Provider checks the solver's image rather than preparing the Task's
    declaration again, whose tag may name another image by now; the Fleet, which prepares
    images on its Nodes, prepares it there from that declaration, its tag pinned to the
    digest the solver's resolved to. A solver on the Fleet resolved its tag on its Node, so
    a verifier here resolves it again."""
    separate = {"verifier": {"environment_mode": "separate", "environment": {"cpus": 1}}}
    repository = write_task_repo(
        "ale-tasks-verified",
        config={"environment": {"image": "ghcr.io/acme/tool:1"}, **separate},
        dockerfile=None,
    )
    task = _classified(load_tasks(repository / "tasks/demo")[0], ImageKind.CONTAINER)
    task.provider, task.verifier_provider = "docker:kata", "docker:runc"
    task.prepared_image = _prepared(ImageKind.CONTAINER, "external-ref")
    runc = _Provider(ImageKind.CONTAINER)
    checked = await prepare_verifier_image_result(task, runc)  # type: ignore[arg-type]
    assert checked is not None
    assert (checked.role, checked.image) == ("verifier", task.prepared_image)
    assert runc.inputs == [task.prepared_image]
    fleet = FleetProvider(SimpleNamespace(recipes={}))  # type: ignore[arg-type]
    task.verifier_provider = "fleet"
    on_fleet = await prepare_verifier_image_result(task, fleet)
    assert on_fleet is not None and on_fleet.role == "verifier"
    recipe = fleet.client.recipes[on_fleet.image.prepared_identity]
    assert recipe.task.image.ref == task.prepared_image.resolved_reference
    assert task.spec.image.ref == "ghcr.io/acme/tool:1"
    task.provider, task.verifier_provider = "fleet", "docker:runc"
    task.prepared_image = on_fleet.image
    runc = _Provider(ImageKind.CONTAINER)
    await prepare_verifier_image_result(task, runc)  # type: ignore[arg-type]
    assert [image.reference for image in runc.inputs] == ["ghcr.io/acme/tool:1"]


@pytest.mark.asyncio
async def test_verifier_builds_tests_dockerfile_unless_an_image_is_named(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    separate = {"verifier": {"environment_mode": "separate"}}
    task = _task(write_task_repo, config=separate, verifier_dockerfile=True)
    built: list[dict[str, object]] = []

    async def fake_local(**kwargs: object) -> tuple[PreparedTaskImage, list[object]]:
        built.append(kwargs)
        return _prepared(ImageKind.CONTAINER, "verifier-local"), []

    monkeypatch.setattr("ale.run.task_images._prepare_local", fake_local)
    await prepare_verifier_image(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert built[0]["dockerfile"] == task.folder.root / "tests/Dockerfile"  # type: ignore[attr-defined]
    assert built[0]["exclude"] == ()
    assert built[0]["source_digest"] == task.verifier_image_source_digest  # type: ignore[attr-defined]

    named = {"image": "ghcr.io/acme/v:1", "cpus": 1, "memory_mb": 512}
    repository = write_task_repo("ale-tasks-named", config={"verifier": {"environment": named}})
    task = _classified(load_tasks(repository / "tasks/demo")[0], ImageKind.CONTAINER)
    registry = _Registry()
    await prepare_verifier_image(task, registry.providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    assert len(built) == 1
    assert registry.providers[ImageKind.CONTAINER].inputs == [
        ImageRef(kind="container", reference="ghcr.io/acme/v:1")
    ]


@pytest.mark.asyncio
async def test_macos_external_images_skip_linux_framework_preparation(write_task_repo) -> None:
    reference = "ghcr.io/example/macos@sha256:" + "a" * 64
    repository = write_task_repo(
        config={
            "environment": {"os": "macos", "image": reference},
            "agent": {"user": "admin", "network_mode": "public"},
            "verifier": {"environment": {"image": reference}},
        },
        dockerfile=None,
    )
    task = load_tasks(repository / "tasks/demo")[0]
    registry = _Registry()
    solver = await prepare_task_image_result(task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    verifier = await prepare_verifier_image(task, registry.providers[ImageKind.VM])  # type: ignore[arg-type]
    assert solver.image.source == "external-ref"
    assert verifier is not None and verifier.source == "external-ref"
    assert [step.name for step in solver.steps] == [
        "lint",
        "reference-resolution",
        "artifact-check",
    ]
    assert LAYERED == []


@pytest.mark.asyncio
async def test_registry_images_are_classified_once_before_preparation(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    read: list[str] = []

    async def classify(reference: str) -> ImageKind:
        read.append(reference)
        return ImageKind.VM if "/vm" in reference else ImageKind.CONTAINER

    monkeypatch.setattr("ale.run.task_images.classify_image", classify)
    monkeypatch.setattr("ale.run.cli.tasks.observe_task_assets", lambda task: None)
    named = write_task_repo(
        "ale-tasks-named",
        tasks=("one", "two"),
        config={
            "environment": {"image": "ghcr.io/acme/vm:1"},
            "verifier": {"environment": {"image": "ghcr.io/acme/checker:1"}},
        },
        dockerfile=None,
    )
    tasks = load_tasks(named / "tasks")
    built = _task(write_task_repo)
    declared = built.spec  # type: ignore[attr-defined]
    assert {task.spec.image.kind for task in tasks} == {None}
    registry = _Registry()
    results = await _prepare_images([*tasks, built], registry)  # type: ignore[arg-type, list-item]
    assert sorted(read) == ["ghcr.io/acme/checker:1", "ghcr.io/acme/vm:1"]
    for task in tasks:
        assert task.spec.image.kind is ImageKind.VM
        assert task.spec.verify.image is not None
        assert task.spec.verify.image.kind is ImageKind.CONTAINER
        assert task.spec.spec_hash  # decided, so it can be hashed and recorded
    assert built.spec is declared  # type: ignore[attr-defined]
    assert [result.image.kind for result in results[:2]] == [ImageKind.VM, ImageKind.CONTAINER]
    await resolve_image_kinds(tasks)
    assert len(read) == 2  # a decided kind is never read again


@pytest.mark.asyncio
async def test_an_image_is_prepared_once_for_each_provider_that_runs_it(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Prepared images are Provider-specific, so one is shared only within a Provider."""
    monkeypatch.setattr("ale.run.cli.tasks.observe_task_assets", lambda task: None)
    repository = write_task_repo(
        "ale-tasks-shared",
        tasks=("plain", "perf", "checked"),
        config={"environment": {"image": "ghcr.io/acme/tool:1"}},
        dockerfile=None,
    )
    edit_task_config(repository / "tasks/perf", {"environment": {"container_features": ["perf"]}})
    # A verifier without an image of its own runs the solver's, on what its needs select.
    edit_task_config(
        repository / "tasks/checked",
        {
            "environment": {"container_features": ["perf"]},
            "verifier": {"environment_mode": "separate", "environment": {"cpus": 1}},
        },
    )
    tasks = {
        str(task.spec.name).rpartition("/")[2]: _classified(task, ImageKind.CONTAINER)
        for task in load_tasks(repository / "tasks")
    }
    registry = _Registry()
    registry.settings = RunConfig.model_validate(
        {"sandbox": {"providers": ["docker:runc", "docker:kata"]}}
    )
    kata = _Provider(ImageKind.CONTAINER, frozenset({ContainerFeature.PERF}))
    registry._instances["docker:kata"] = kata  # type: ignore[assignment]
    await _prepare_images(list(tasks.values()), registry)  # type: ignore[arg-type]
    plain, perf, checked = tasks["plain"], tasks["perf"], tasks["checked"]
    assert (checked.provider, checked.verifier_provider) == ("docker:kata", "docker:runc")
    reference = ImageRef(kind="container", reference="ghcr.io/acme/tool:1")
    assert kata.inputs == [reference]
    assert checked.prepared_image is perf.prepared_image
    assert plain.prepared_image is not perf.prepared_image
    # The verifier runs the very image its solver runs, which its own Provider checks.
    assert checked.prepared_verifier_image == checked.prepared_image
    assert registry.providers[ImageKind.CONTAINER].inputs == [checked.prepared_image, reference]


@pytest.mark.asyncio
async def test_a_registry_image_decided_a_vm_takes_no_container_features(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    async def classify(reference: str) -> ImageKind:
        return ImageKind.VM if "/vm" in reference else ImageKind.CONTAINER

    monkeypatch.setattr("ale.run.task_images.classify_image", classify)
    repository = write_task_repo(
        "ale-tasks-features",
        tasks=("tool", "vm"),
        config={"environment": {"image": "ghcr.io/acme/tool:1", "container_features": ["perf"]}},
        dockerfile=None,
    )
    edit_task_config(repository / "tasks/vm", {"environment": {"image": "ghcr.io/acme/vm:1"}})
    (container,) = load_tasks(repository / "tasks/tool")
    await resolve_image_kinds([container])
    assert container.spec.image.kind is ImageKind.CONTAINER
    (vm,) = load_tasks(repository / "tasks/vm")
    with pytest.raises(
        TaskDefinitionError,
        match=r"invalid Task agentslastexam/vm: the solver image is a VM, which takes no "
        "container_features",
    ):
        await resolve_image_kinds([vm])


@pytest.mark.asyncio
async def test_an_episode_decides_its_image_kinds_before_preparing_them(
    write_task_repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """An episode reached without ``_prepare_images`` still prepares only decided kinds."""
    from ale.run.episode import run_episode

    task = _task(
        write_task_repo, config={"environment": {"image": "ghcr.io/acme/vm:1"}}, dockerfile=None
    )
    read: list[str] = []
    prepared: list[ImageKind | None] = []

    async def classify(reference: str) -> ImageKind:
        read.append(reference)
        return ImageKind.VM

    async def prepare(task: Task, provider: object) -> PreparedTaskImage:
        prepared.append((task.spec.image.kind, provider))
        raise ProviderStartError("stop before a Sandbox starts")

    monkeypatch.setattr("ale.run.task_images.classify_image", classify)
    monkeypatch.setattr("ale.run.episode.prepare_task_image", prepare)
    monkeypatch.setattr("ale.run.episode.observe_task_assets", lambda task: None)
    registry = _Registry()
    with pytest.raises(ProviderStartError, match="stop before"):
        await run_episode(task, None, registry, run_dir=tmp_path / "runs")  # type: ignore[arg-type]
    # Selected by its decided kind, then prepared for the Provider selected.
    assert read == ["ghcr.io/acme/vm:1"] and task.provider == "qemu"
    assert prepared == [(ImageKind.VM, registry.providers[ImageKind.VM])]


@pytest.mark.asyncio
async def test_an_episode_selects_by_the_retention_it_is_given(
    write_task_repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A solver whose episode snapshots it goes to a Provider that saves snapshots."""
    from ale.run.episode import run_episode

    task = _task(
        write_task_repo,
        config={"environment": {"image": "ghcr.io/acme/tool:1", "memory_mb": 2048}},
        dockerfile=None,
    )
    task = _classified(task, ImageKind.CONTAINER)

    async def prepare(task: Task, _provider: object) -> PreparedTaskImage:
        raise ProviderStartError(f"stop on {task.provider}")

    monkeypatch.setattr("ale.run.episode.prepare_task_image", prepare)
    monkeypatch.setattr("ale.run.episode.observe_task_assets", lambda task: None)
    registry = ProviderRegistry(
        RunConfig.model_validate({"sandbox": {"providers": ["daytona", "docker:runc"]}})
    )
    with pytest.raises(ProviderStartError, match="stop on docker:runc"):
        await run_episode(
            task,
            None,  # type: ignore[arg-type]
            registry,
            run_dir=tmp_path / "runs",
            sandbox_retention=SandboxRetentionConfig(solver="snapshot"),
        )


@pytest.mark.asyncio
async def test_an_episode_checks_on_this_host_only_the_leaves_it_selected_itself(
    write_task_repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A caller that selected its Tasks' Providers checked them before its first episode."""
    from ale.run.episode import run_episode

    task = _task(
        write_task_repo, config={"environment": {"image": "ghcr.io/acme/tool:1"}}, dockerfile=None
    )
    task = _classified(task, ImageKind.CONTAINER)
    checked: list[list[Task]] = []

    async def preflight(_registry: ProviderRegistry, tasks: list[Task]) -> None:
        checked.append(tasks)
        raise ProviderStartError("checked on this Host")

    monkeypatch.setattr(ProviderRegistry, "preflight", preflight)
    monkeypatch.setattr("ale.run.episode.observe_task_assets", lambda task: None)
    registry = _Registry()
    with pytest.raises(ProviderStartError, match="checked on this Host"):
        await run_episode(task, None, registry, run_dir=tmp_path / "runs")  # type: ignore[arg-type]
    assert checked == [[task]] and task.provider == "docker:runc"
    # Selected now, as by a Run: the episode runs (and here fails) without a check of its
    # own.
    await run_episode(task, None, registry, run_dir=tmp_path / "runs")  # type: ignore[arg-type]
    assert checked == [[task]]


@pytest.mark.asyncio
async def test_an_undecided_kind_is_never_prepared(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _task(
        write_task_repo,
        config={
            "environment": {"image": "ghcr.io/acme/t:v1"},
            "verifier": {"environment": {"image": "ghcr.io/acme/v:1"}},
        },
        dockerfile=None,
    )
    with pytest.raises(ValueError, match="kinds of its images are decided"):
        _ = task.spec.spec_hash  # type: ignore[attr-defined]
    with pytest.raises(TaskDefinitionError, match="was not decided before its preparation"):
        await prepare_task_image_result(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]
    with pytest.raises(TaskDefinitionError, match="was not decided before its preparation"):
        await prepare_verifier_image_result(task, _Registry().providers[ImageKind.CONTAINER])  # type: ignore[arg-type]

"""Load self-contained Task folders from filesystem paths."""

from __future__ import annotations

import subprocess
import tomllib
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ale.core.episode import EpisodeContext
from ale.core.errors import TaskDefinitionError
from ale.core.task import Stage, Task, TaskSourceContext
from ale.core.taskspec import (
    ImageKind,
    ImageSpec,
    OperatingSystem,
    SkillSource,
    TaskConfig,
    TaskMcpSource,
    TaskSpec,
    ToolProvision,
    VerificationMode,
    VerifierResources,
    VerifySpec,
)
from ale.core.verdict import Rewards
from ale.run.assets import ASSET_ROOTS
from ale.run.content import tree_digest

__all__ = [
    "INSTRUCTION",
    "TASK_CONFIG",
    "ManifestTask",
    "TaskFolder",
    "discover_task_folders",
    "load_task_folder",
    "load_tasks",
]

TASK_CONFIG = "task.toml"
INSTRUCTION = "instruction.md"
ENVIRONMENT_DIR = "environment"
#: Below ``environment/``: never part of an image build context.
SETUP_DIR = "setup"
WINDOWS_BUILD = "build.ps1"
_SOURCE_EXCLUDES = (*ASSET_ROOTS, ".ale-cache")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise TaskDefinitionError(f"missing {path.name}: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise TaskDefinitionError(f"invalid TOML in {path}: {exc}") from exc


def _describe(error: ValidationError) -> str:
    """One readable line per problem, each naming the ``task.toml`` key it is about."""
    problems = []
    for item in error.errors():
        key = ".".join(str(part) for part in item["loc"])
        message = str(item["msg"]).removeprefix("Value error, ")
        if item["type"] == "extra_forbidden":
            problems.append(f"{key} is not a task.toml key")
        elif item["type"] == "missing":
            problems.append(f"{key} is required")
        else:
            problems.append(f"{key}: {message}" if key else message)
    return "; ".join(problems)


class TaskFolder:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @property
    def environment_dir(self) -> Path:
        return self.root / ENVIRONMENT_DIR

    @property
    def dockerfile(self) -> Path | None:
        return _file(self.environment_dir / "Dockerfile")

    @property
    def build_script(self) -> Path | None:
        return _file(self.environment_dir / WINDOWS_BUILD)

    @property
    def verifier_dockerfile(self) -> Path | None:
        return _file(self.root / Stage.TESTS / "Dockerfile")

    def stage_dir(self, stage: Stage) -> Path | None:
        path = self.environment_dir / SETUP_DIR if stage is Stage.SETUP else self.root / stage
        return path if path.is_dir() else None

    def stage_entry(
        self,
        stage: Stage,
        operating_system: OperatingSystem = OperatingSystem.LINUX,
    ) -> Path | None:
        directory = self.stage_dir(stage)
        return _file(directory / stage.entry(operating_system)) if directory else None

    def image_source_digest(self, image: ImageSpec) -> str | None:
        """What a solver image is built from: its build context, without assets."""
        if image.ref is not None:
            return None
        return tree_digest(self.environment_dir, exclude=("assets", SETUP_DIR))

    def verifier_image_source_digest(self, image: ImageSpec | None) -> str | None:
        if image is None or image.ref is not None:
            return None
        return tree_digest(self.root / Stage.TESTS, exclude=("assets",))

    @cached_property
    def content_digest(self) -> str:
        return tree_digest(self.root, exclude=_SOURCE_EXCLUDES)

    @cached_property
    def non_verifier_digest(self) -> str:
        """Identity of everything a solver depends on, read once per folder.

        Asset bytes are left to their own commit identity rather than hashed on every load;
        reverification compares that commit separately.
        """
        return tree_digest(self.root, exclude=(Stage.TESTS, *_SOURCE_EXCLUDES))

    @cached_property
    def source(self) -> TaskSourceContext:
        location = self.root
        while not location.exists():
            location = location.parent
        try:
            result = subprocess.run(
                ["git", "-C", str(location), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return TaskSourceContext()
        if result.returncode != 0:
            return TaskSourceContext()
        repository_root = Path(result.stdout.strip()).resolve()
        try:
            relative = self.root.relative_to(repository_root).as_posix()
        except ValueError:
            return TaskSourceContext()
        return TaskSourceContext(
            repository_root=repository_root,
            repository_name=_repository_name(repository_root),
            task_relative_path=relative,
        )


def _file(path: Path) -> Path | None:
    return path if path.is_file() else None


def _repository_name(root: Path) -> str:
    origin = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if origin.returncode == 0 and origin.stdout.strip():
        return (
            origin.stdout.strip()
            .rstrip("/")
            .rsplit("/", 1)[-1]
            .rsplit(":", 1)[-1]
            .removesuffix(".git")
        )
    common = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    )
    directory = Path(common.stdout.strip())
    return (
        directory.parent.name if directory.name == ".git" else directory.name.removesuffix(".git")
    )


class ManifestTask(Task):
    folder: TaskFolder

    def __init__(self, spec: TaskSpec, folder: TaskFolder) -> None:
        super().__init__(
            spec,
            folder=folder,
            source=folder.source,
            task_digest=folder.content_digest,
            non_verifier_digest=folder.non_verifier_digest,
            image_source_digest=folder.image_source_digest(spec.image),
            verifier_image_source_digest=folder.verifier_image_source_digest(spec.verify.image),
        )

    async def score(self, ctx: EpisodeContext) -> Rewards:
        if ctx.verified_rewards is None:
            raise TaskDefinitionError(
                "verification produced no rewards; tests must write /logs/verifier/reward.json"
            )
        return {str(key): float(value) for key, value in ctx.verified_rewards.items()}


def discover_task_folders(path: Path) -> tuple[TaskFolder, ...]:
    source = path.expanduser().resolve()
    if not source.exists():
        raise TaskDefinitionError(f"Task path does not exist: {source}")
    if (source / TASK_CONFIG).is_file():
        return (TaskFolder(source),)
    configs = sorted(
        source.rglob(TASK_CONFIG),
        key=lambda item: item.relative_to(source).as_posix(),
    )
    if not configs:
        raise TaskDefinitionError(f"no {TASK_CONFIG} found below {source}")
    return tuple(TaskFolder(config.parent) for config in configs)


def load_tasks(path: Path) -> list[ManifestTask]:
    tasks: list[ManifestTask] = []
    seen: dict[str, Path] = {}
    repositories: dict[str, Path] = {}
    for folder in discover_task_folders(path):
        source = folder.source
        if source.repository_name and source.repository_root:
            previous_root = repositories.get(source.repository_name)
            if previous_root is not None and previous_root != source.repository_root:
                raise TaskDefinitionError(
                    f"ambiguous Task repository name {source.repository_name!r}: "
                    f"{previous_root} and {source.repository_root}"
                )
            repositories[source.repository_name] = source.repository_root
        task = _load_folder(folder)
        previous = seen.get(str(task.spec.name))
        if previous is not None:
            raise TaskDefinitionError(
                f"duplicate Task name {task.spec.name!r}: {previous} and {folder.root}"
            )
        seen[str(task.spec.name)] = folder.root
        tasks.append(task)
    return tasks


def load_task_folder(path: Path) -> ManifestTask:
    tasks = load_tasks(path)
    if len(tasks) > 1:
        raise TaskDefinitionError(f"{path} selects more than one Task")
    return tasks[0]


def _load_folder(folder: TaskFolder) -> ManifestTask:
    path = folder.root / TASK_CONFIG
    try:
        config = TaskConfig.model_validate(_read_toml(path))
    except ValidationError as exc:
        raise TaskDefinitionError(f"invalid {path}: {_describe(exc)}") from exc
    _require_files(folder, config.environment.os)
    try:
        spec = _spec(config, folder)
    except ValidationError as exc:
        raise TaskDefinitionError(f"invalid Task {folder.root}: {_describe(exc)}") from exc
    except TaskDefinitionError as exc:
        raise TaskDefinitionError(f"invalid Task {folder.root}: {exc}") from exc
    return ManifestTask(spec, folder)


def _spec(config: TaskConfig, folder: TaskFolder) -> TaskSpec:
    """The runtime specification: ``task.toml`` with the folder's build definitions."""
    return TaskSpec(
        name=config.task.name,
        os=config.environment.os,
        image=_solver_image(config, folder),
        instruction=(folder.root / INSTRUCTION).read_text(encoding="utf-8"),
        resources={
            "cpus": config.environment.cpus,
            "memory_mb": config.environment.memory_mb,
            "storage_mb": config.environment.storage_mb,
            "gpus": config.environment.gpus,
            "sudo": config.ale.sudo,
        },
        network=config.agent.network,
        timeouts={"agent": config.agent.timeout_sec, "verify": config.verifier.timeout_sec},
        artifacts=config.artifacts,
        tools=ToolProvision(
            skills=tuple(SkillSource(path=path) for path in config.ale.skills),
            mcp_servers=tuple(TaskMcpSource(path=path) for path in config.ale.mcp_servers),
        ),
        verify=_verification(config, folder),
        metadata=config.metadata,
    )


def _solver_image(config: TaskConfig, folder: TaskFolder) -> ImageSpec:
    """The solver image: one named directly wins over a build definition, as in Harbor.

    ``[ale].sandbox`` and the OS decide what ``environment/`` builds: a Dockerfile for a
    container or a Linux VM, ``build.ps1`` for Windows.
    """
    ale, user = config.ale, config.agent.user
    windows = config.environment.os is OperatingSystem.WINDOWS
    if not windows and folder.build_script is not None:
        raise TaskDefinitionError("environment/build.ps1 requires os 'windows'")
    if ale.sandbox is ImageKind.CONTAINER:
        ref = config.environment.docker_image
        if ref is None and folder.dockerfile is None:
            raise TaskDefinitionError(
                "a container Task requires environment/Dockerfile or [environment].docker_image"
            )
        return ImageSpec(kind=ImageKind.CONTAINER, ref=ref, user=user)

    if windows:
        if folder.dockerfile is not None:
            raise TaskDefinitionError(
                "a Windows Task builds environment/build.ps1, not a Dockerfile"
            )
        if folder.build_script is not None and ale.vm_base is None:
            raise TaskDefinitionError("environment/build.ps1 requires [ale].vm_base")
        if ale.vm_base is not None and folder.build_script is None:
            raise TaskDefinitionError("[ale].vm_base requires environment/build.ps1")
        if ale.vm_base is None and ale.vm_image is None:
            raise TaskDefinitionError(
                "a Windows Task requires environment/build.ps1 or [ale].vm_image"
            )
        return ImageSpec(kind=ImageKind.VM, ref=ale.vm_image, base_ref=ale.vm_base, user=user)

    if ale.vm_image is None and folder.dockerfile is None:
        raise TaskDefinitionError(
            "a Linux VM Task requires environment/Dockerfile or [ale].vm_image"
        )
    return ImageSpec(kind=ImageKind.VM, ref=ale.vm_image, user=user)


def _verification(config: TaskConfig, folder: TaskFolder) -> VerifySpec:
    """Harbor's verifier semantics; ``[ale.verifier]`` adds a VM verifier image."""
    verifier, ale_verifier = config.verifier, config.ale.verifier
    dockerfile = folder.verifier_dockerfile
    if verifier.mode is VerificationMode.SHARED:
        if dockerfile is not None:
            raise TaskDefinitionError("tests/Dockerfile requires separate verification")
        return VerifySpec()
    if dockerfile is not None and config.environment.os is OperatingSystem.WINDOWS:
        raise TaskDefinitionError("a Windows verifier reuses the solver image or names a vm_image")

    environment = verifier.environment
    user = config.agent.user
    image: ImageSpec | None = None
    if environment is not None and environment.docker_image is not None:
        image = ImageSpec(kind=ImageKind.CONTAINER, ref=environment.docker_image, user=user)
    elif ale_verifier is not None and ale_verifier.vm_image is not None:
        image = ImageSpec(kind=ImageKind.VM, ref=ale_verifier.vm_image, user=user)
    elif dockerfile is not None:
        kind = ale_verifier.sandbox if ale_verifier is not None else ImageKind.CONTAINER
        image = ImageSpec(kind=kind, user=user)
    elif ale_verifier is not None:
        raise TaskDefinitionError("[ale.verifier] requires vm_image or tests/Dockerfile")

    # Without a verifier environment, a separate verifier gets the solver's resources.
    sized = environment or config.environment
    resources = VerifierResources(
        cpus=sized.cpus, memory_mb=sized.memory_mb, storage_mb=sized.storage_mb, gpus=sized.gpus
    )
    return VerifySpec(environment_mode=VerificationMode.SEPARATE, image=image, resources=resources)


def _require_files(folder: TaskFolder, operating_system: OperatingSystem) -> None:
    required = (
        folder.root / TASK_CONFIG,
        folder.root / INSTRUCTION,
        folder.root / Stage.TESTS / Stage.TESTS.entry(operating_system),
        folder.root / Stage.SOLUTION / Stage.SOLUTION.entry(operating_system),
    )
    missing = [path.relative_to(folder.root).as_posix() for path in required if not path.is_file()]
    if missing:
        raise TaskDefinitionError(
            f"{folder.root} is missing required Task file(s): {', '.join(missing)}"
        )

"""Load self-contained Task folders from filesystem paths."""

from __future__ import annotations

import subprocess
import tomllib
from functools import cached_property
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from ale.core.episode import EpisodeContext
from ale.core.errors import TaskDefinitionError
from ale.core.ids import content_hash
from ale.core.task import (
    CALIBRATION_DIR,
    SOLUTION_DIR,
    Stage,
    Task,
    TaskSourceContext,
    case_entry,
    case_path,
)
from ale.core.taskspec import (
    SOLUTION_CASE,
    ImageKind,
    ImageSpec,
    OperatingSystem,
    TaskConfig,
    TaskSpec,
    TaskVariables,
    ToolProvision,
    VerificationMode,
    VerifierResources,
    VerifySpec,
)
from ale.core.verdict import Rewards
from ale.run.assets import asset_roots
from ale.run.content import tree_digest

__all__ = [
    "INSTRUCTION",
    "TASK_CONFIG",
    "ManifestTask",
    "TaskFolder",
    "describe",
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
SHELL_BUILD = "build.sh"
#: The script that builds a VM image on ``[environment].image``, by the Task's OS.
BUILD_SCRIPTS = {
    OperatingSystem.LINUX: SHELL_BUILD,
    OperatingSystem.WINDOWS: WINDOWS_BUILD,
    OperatingSystem.MACOS: SHELL_BUILD,
}
_OS_LABELS = {
    OperatingSystem.LINUX: "Linux",
    OperatingSystem.WINDOWS: "Windows",
    OperatingSystem.MACOS: "macOS",
}
#: Generated state, never Task source.
_CACHE_DIR = ".ale-cache"


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise TaskDefinitionError(f"missing {path.name}: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise TaskDefinitionError(f"invalid TOML in {path}: {exc}") from exc


def describe(error: ValidationError) -> str:
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

    def build_script(self, operating_system: OperatingSystem) -> Path | None:
        return _file(self.environment_dir / BUILD_SCRIPTS[operating_system])

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

    def case_dir(self, case: str) -> Path | None:
        """The directory of the solution a calibration case runs: ``solution/`` for the
        reference solution, ``calibration/<case>/`` for every further case."""
        path = self.root / case_path(case)
        return path if path.is_dir() else None

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
        return tree_digest(self.root, exclude=(*asset_roots(self.root), _CACHE_DIR))

    def non_verifier_digest(self, config: TaskConfig) -> str:
        """Identity of everything an evaluated agent's episode depends on.

        Verification and the calibration cases, the reference solution among them, never reach
        an evaluated agent, so ``tests/``, ``solution/``, ``calibration/`` and ``task.toml``'s
        ``[solution]`` and ``[[calibration]]`` tables are left out: changing them keeps a
        retained solver reverifiable. ``validation/`` stays in, since the agent receives its
        results. Asset bytes are left to their own commit identity rather than hashed on every
        load; reverification compares that commit separately.
        """
        files = tree_digest(
            self.root,
            exclude=(
                TASK_CONFIG,
                Stage.TESTS,
                SOLUTION_DIR,
                CALIBRATION_DIR,
                *asset_roots(self.root),
                _CACHE_DIR,
            ),
        )
        configuration = config.model_dump(mode="json", exclude={"solution", "calibration"})
        return content_hash({"files": files, TASK_CONFIG: configuration})

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

    def __init__(self, spec: TaskSpec, folder: TaskFolder, config: TaskConfig) -> None:
        super().__init__(
            spec,
            folder=folder,
            source=folder.source,
            task_digest=folder.content_digest,
            non_verifier_digest=folder.non_verifier_digest(config),
            image_source_digest=folder.image_source_digest(spec.image),
            verifier_image_source_digest=folder.verifier_image_source_digest(spec.verify.image),
        )

    async def score(self, ctx: EpisodeContext) -> Rewards:
        if ctx.verified_rewards is None:
            raise TaskDefinitionError(
                "verification produced no rewards; tests must write reward.json "
                "in the platform verifier directory"
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
        raise TaskDefinitionError(f"invalid {path}: {describe(exc)}") from exc
    _require_files(folder, config)
    try:
        spec = _spec(config, folder)
    except ValidationError as exc:
        raise TaskDefinitionError(f"invalid Task {folder.root}: {describe(exc)}") from exc
    except TaskDefinitionError as exc:
        raise TaskDefinitionError(f"invalid Task {folder.root}: {exc}") from exc
    return ManifestTask(spec, folder, config)


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
            "gpu_types": config.environment.gpu_types or (),
            "gpu_memory_mb": config.environment.gpu_memory_mb,
            "sudo": config.agent.sudo,
            "container_features": config.environment.container_features,
        },
        network=config.agent_network,
        workdir=config.agent.workdir or config.environment.workdir,
        healthcheck=config.environment.healthcheck,
        timeouts={"agent": config.agent.timeout_sec, "verify": config.verifier.timeout_sec},
        validation_rounds=config.agent.validation_rounds,
        artifacts=config.artifacts,
        tools=ToolProvision(
            skills_dir=config.environment.skills_dir,
            mcp_servers=tuple(entry.server for entry in config.environment.mcp_servers),
        ),
        verify=(verify := _verification(config, folder)),
        validation=_validation(config, verify),
        variables=TaskVariables(
            environment=config.environment.env,
            verifier=config.verifier.env,
            verifier_environment=(
                config.verifier.environment.env if config.verifier.environment else None
            ),
            solution=config.solution.env,
            mcp_headers={
                server.name: server.headers
                for server in config.environment.mcp_servers
                if server.headers
            },
        ),
        calibration=config.calibration_cases,
        metadata=config.metadata,
    )


def _solver_image(config: TaskConfig, folder: TaskFolder) -> ImageSpec:
    """The solver image, from ``[environment].image`` and what ``environment/`` holds.

    A Dockerfile builds a Linux container and names its base in ``FROM``. A build script
    (``build.ps1`` on Windows, ``build.sh`` elsewhere) builds a VM on ``image``. ``image``
    alone is used directly.
    """
    environment, user = config.environment, config.agent.user
    operating_system, image = environment.os, environment.image
    linux = operating_system is OperatingSystem.LINUX
    label, script = _OS_LABELS[operating_system], BUILD_SCRIPTS[operating_system]
    timeout = environment.build_timeout_sec
    if operating_system is not OperatingSystem.WINDOWS and _file(
        folder.environment_dir / WINDOWS_BUILD
    ):
        raise TaskDefinitionError("environment/build.ps1 requires os 'windows'")
    if folder.dockerfile is not None:
        if not linux:
            raise TaskDefinitionError(
                f"a {label} Task builds environment/{script}, not a Dockerfile"
            )
        if image is not None:
            raise TaskDefinitionError(
                "environment/Dockerfile names its base in FROM; remove [environment].image"
            )
        return ImageSpec(kind=ImageKind.CONTAINER, user=user, build_timeout_sec=timeout)
    if folder.build_script(operating_system) is not None:
        if image is None:
            raise TaskDefinitionError(
                f"environment/{script} requires [environment].image, the base it builds on"
            )
        return ImageSpec(kind=ImageKind.VM, base_ref=image, user=user, build_timeout_sec=timeout)
    if image is None:
        needed = "environment/Dockerfile or [environment].image" if linux else "[environment].image"
        raise TaskDefinitionError(f"a {label} Task requires {needed}")
    kind = _named_kind(image, operating_system)
    return ImageSpec(kind=kind, ref=image, user=user, build_timeout_sec=timeout)


def _named_kind(image: str, operating_system: OperatingSystem) -> ImageKind | None:
    """What an image named alone is, as far as the Task decides it.

    Windows and macOS run only VMs, and an absolute path is a local VM disk. A Linux
    registry reference is either kind, so the registry decides it before preparation.
    """
    if operating_system is not OperatingSystem.LINUX or PurePosixPath(image).is_absolute():
        return ImageKind.VM
    return None


def _verification(config: TaskConfig, folder: TaskFolder) -> VerifySpec:
    """Harbor's verifier semantics; a Windows or macOS verifier image is a VM. A snapshot
    verifier boots from the solver's snapshot, so it has resources and no image."""
    verifier = config.verifier
    mode = verifier.mode
    dockerfile = folder.verifier_dockerfile
    # Harbor's working directory: a verifier's own environment's, else the agent's.
    own = verifier.environment.workdir if verifier.environment is not None else None
    workdir = own or config.environment.workdir
    if mode is not VerificationMode.SEPARATE and dockerfile is not None:
        raise TaskDefinitionError("tests/Dockerfile requires separate verification")
    account = None if verifier.user is None else str(verifier.user)
    if mode is VerificationMode.SHARED:
        return VerifySpec(workdir=workdir, user=account)
    operating_system = config.environment.os
    if dockerfile is not None and operating_system is not OperatingSystem.LINUX:
        raise TaskDefinitionError(
            "only a Linux verifier builds tests/Dockerfile; reuse the solver image or name "
            "[verifier.environment].image"
        )

    environment = verifier.environment
    # Without a verifier environment, the verifier gets the solver's resources.
    sized = environment or config.environment
    named = environment.image if environment is not None else None
    user = config.agent.user
    image: ImageSpec | None = None
    if dockerfile is not None:
        if named is not None:
            raise TaskDefinitionError(
                "tests/Dockerfile names its base in FROM; remove [verifier.environment].image"
            )
        image = ImageSpec(
            kind=ImageKind.CONTAINER, user=user, build_timeout_sec=sized.build_timeout_sec
        )
    elif named is not None:
        image = ImageSpec(kind=_named_kind(named, operating_system), ref=named, user=user)

    resources = VerifierResources(
        cpus=sized.cpus or config.environment.cpus,
        memory_mb=sized.memory_mb or config.environment.memory_mb,
        storage_mb=sized.storage_mb,
        gpus=sized.gpus,
        gpu_types=sized.gpu_types or (),
        gpu_memory_mb=sized.gpu_memory_mb,
        container_features=sized.container_features,
    )
    return VerifySpec(
        environment_mode=mode, image=image, resources=resources, workdir=workdir, user=account
    )


def _validation(config: TaskConfig, verify: VerifySpec) -> VerifySpec | None:
    """Where ``[validation]`` puts ``validation/``, or None to run it as ``tests/``. A
    separate one runs a separate verifier's image of ``tests/``, or else the solver's."""
    table = config.validation
    if table is None:
        return None
    mode = table.mode
    # [verifier]'s account, and Harbor's working directory: its own environment's, else
    # where tests/ runs.
    own = table.environment.workdir if table.environment is not None else None
    placed = {"workdir": own or verify.workdir, "user": verify.user}
    if mode is VerificationMode.SHARED:
        return VerifySpec(**placed)
    sized = table.environment or config.environment
    resources = VerifierResources(
        cpus=sized.cpus or config.environment.cpus,
        memory_mb=sized.memory_mb or config.environment.memory_mb,
        storage_mb=sized.storage_mb,
        gpus=sized.gpus,
        gpu_types=sized.gpu_types or (),
        gpu_memory_mb=sized.gpu_memory_mb,
        container_features=sized.container_features,
    )
    image = verify.image if mode is VerificationMode.SEPARATE else None
    return VerifySpec(environment_mode=mode, image=image, resources=resources, **placed)


def _require_files(folder: TaskFolder, config: TaskConfig) -> None:
    """The stage entries a Task runs, and one directory per calibration case.

    A calibration case runs as the oracle, so its directory holds ``solve.sh``, or
    ``solve.ps1`` on Windows. ``solution/`` is the reference solution every Task has; every
    further case is a directory of ``calibration/`` with a ``[[calibration]]`` entry.
    ``validation/`` is optional, and has the entry ``tests/`` has when it exists; validation
    rounds and a case's ``expect_validation`` need it.
    """
    # The reference solution is the Task root's solution/, never a calibration/ directory.
    if (folder.root / CALIBRATION_DIR / SOLUTION_CASE).is_dir():
        raise TaskDefinitionError(
            f"{folder.root}: {CALIBRATION_DIR}/{SOLUTION_CASE}/ is not a calibration case; the "
            f"reference solution lives in {SOLUTION_DIR}/"
        )
    operating_system = config.environment.os
    solve = case_entry(operating_system)
    cases = [case.name for case in config.calibration_cases]
    validation = folder.stage_dir(Stage.VALIDATION) is not None
    required = (
        folder.root / TASK_CONFIG,
        folder.root / INSTRUCTION,
        folder.root / Stage.TESTS / Stage.TESTS.entry(operating_system),
        *(
            (folder.root / Stage.VALIDATION / Stage.VALIDATION.entry(operating_system),)
            if validation
            else ()
        ),
        *(folder.root / case_path(name) / solve for name in cases),
    )
    missing = [path.relative_to(folder.root).as_posix() for path in required if not path.is_file()]
    if missing:
        raise TaskDefinitionError(
            f"{folder.root} is missing required Task file(s): {', '.join(missing)}"
        )
    if not validation:
        if config.agent.validation_rounds:
            raise TaskDefinitionError(
                f"{folder.root}: [agent].validation_rounds needs validation/, the verifier "
                "whose results the agent receives"
            )
        if unchecked := [case.name for case in config.calibration if case.expect_validation]:
            raise TaskDefinitionError(
                f"{folder.root}: expect_validation of [[calibration]] {', '.join(unchecked)} "
                "needs validation/"
            )
    for name in sorted(path.name for path in (folder.root / CALIBRATION_DIR).glob("*/")):
        if name not in cases:
            raise TaskDefinitionError(
                f"{folder.root}: {CALIBRATION_DIR}/{name}/ needs a [[calibration]] entry "
                f'named "{name}"'
            )

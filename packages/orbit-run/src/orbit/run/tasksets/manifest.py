"""Load self-contained Task folders from filesystem paths."""

from __future__ import annotations

import re
import subprocess
import tomllib
from functools import cached_property
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from orbit.core.episode import EpisodeContext
from orbit.core.errors import TaskDefinitionError
from orbit.core.ids import TaskId, content_hash, in_orbit_form
from orbit.core.task import (
    CALIBRATION_DIR,
    SOLUTION_DIR,
    Stage,
    Task,
    TaskSourceContext,
    case_entry,
    case_path,
)
from orbit.core.taskspec import (
    HARBOR_ACCOUNT,
    SCHEMA_VERSION,
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
    VerifierTable,
    VerifySpec,
)
from orbit.core.verdict import Rewards
from orbit.run.assets import asset_roots
from orbit.run.content import tree_digest

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
_CACHE_DIR = ".orbit-cache"
#: A Dockerfile stage's base and its alias.
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.I | re.M)
_USER = re.compile(r"^\s*USER\s+(\S+)", re.I | re.M)


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

    @property
    def validation_dockerfile(self) -> Path | None:
        return _file(self.root / Stage.VALIDATION / "Dockerfile")

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

    def validation_image_source_digest(self, image: ImageSpec | None) -> str | None:
        """What ``validation/Dockerfile`` builds the validation verifier's image from."""
        if image is None or image.ref is not None or self.validation_dockerfile is None:
            return None
        return tree_digest(self.root / Stage.VALIDATION, exclude=("assets",))

    @cached_property
    def content_digest(self) -> str:
        return tree_digest(self.root, exclude=(*asset_roots(self.root), _CACHE_DIR))

    def non_verifier_digest(self, config: TaskConfig) -> str:
        """Identity of everything an evaluated agent's episode depends on.

        Verification and the calibration cases, the reference solution among them, never reach
        an evaluated agent, so ``tests/``, ``solution/``, ``calibration/`` and ``task.toml``'s
        ``[solution]`` and ``[[calibration]]`` tables are left out: changing them keeps a
        retained solver reverifiable, unless ``tests/`` validated its agent's rounds, which
        reverification checks itself. ``validation/`` stays in, since the agent receives its
        results, and so does ``tests/`` where the agent has a copy of it in place of a
        ``validation/``. Asset bytes are left to their own commit identity rather than hashed
        on every load; reverification compares that commit separately.
        """
        own = config.verifier.validation
        shown = own is not None and own.visibility and self.stage_dir(Stage.VALIDATION) is None
        files = tree_digest(
            self.root,
            exclude=(
                TASK_CONFIG,
                *(() if shown else (Stage.TESTS,)),
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
            harbor=config.harbor,
            warnings=_warnings(config, folder, spec.name),
            task_digest=folder.content_digest,
            non_verifier_digest=folder.non_verifier_digest(config),
            image_source_digest=folder.image_source_digest(spec.image),
            verifier_image_source_digest=folder.verifier_image_source_digest(spec.verify.image),
            validation_image_source_digest=folder.validation_image_source_digest(
                spec.validation.image if spec.validation else None
            ),
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
                f"duplicate Task name {task.spec.name!r}: {previous} and {folder.root}; "
                "give each its own [task].name"
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
        spec = _spec(config, folder, config.task.name or _folder_name(folder))
    except ValidationError as exc:
        raise TaskDefinitionError(f"invalid Task {folder.root}: {describe(exc)}") from exc
    except TaskDefinitionError as exc:
        raise TaskDefinitionError(f"invalid Task {folder.root}: {exc}") from exc
    return ManifestTask(spec, folder, config)


def _folder_name(folder: TaskFolder) -> TaskId:
    """The id of a Task that names none, as Harbor names one by its folder: its repository,
    or ``local`` outside one, then its folder, each in the characters a Task id allows."""
    parts = (folder.source.repository_name or "local", folder.root.name)
    cleaned = (
        re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "-", part)).strip("._-")
        for part in parts
    )
    try:
        return TaskId("/".join(cleaned))
    except ValueError:
        raise TaskDefinitionError(
            "no [task].name, and its folder gives no Task id; name the Task in task.toml"
        ) from None


def _warnings(config: TaskConfig, folder: TaskFolder, name: TaskId) -> tuple[str, ...]:
    """Where the Task departs from Orbit's form, each naming the file it is about: what it
    leaves to a default Orbit then chooses, and what can change under it."""
    found: list[str] = []
    version = config.schema_version
    if config.harbor:
        written = (
            "no schema_version" if version is None else f'Harbor\'s schema_version "{version}"'
        )
        found.append(
            f"{TASK_CONFIG}: {written}, so it is read as a Harbor Task in Orbit's format "
            f'"{SCHEMA_VERSION}"{_harbor_defaults(config)}'
        )
    if config.task.name is None:
        found.append(f"{TASK_CONFIG}: no [task].name, so this Task is {name}, after where it lives")
    elif not in_orbit_form(name):
        found.append(
            f"{TASK_CONFIG}: [task].name {name} is Harbor's form; Orbit's is a lowercase org/name "
            "of letters, digits, '-' and '_'"
        )
    found.extend(
        f"{path}: {image} can change under the Task; Orbit uses the version it names when the "
        "image is prepared and records its digest; name an exact version"
        for path, image in _moving_images(config, folder)
    )
    account = _image_user(folder.dockerfile) if folder.dockerfile else None
    defaulted = "user" not in config.agent.model_fields_set
    if account is not None and defaulted and config.agent_user == HARBOR_ACCOUNT:
        found.append(
            f"{ENVIRONMENT_DIR}/Dockerfile: its image runs as USER {account}, which Harbor's "
            f"agent runs as; Orbit's runs as {HARBOR_ACCOUNT} unless [agent].user names {account}"
        )
    return tuple(found)


def _harbor_defaults(config: TaskConfig) -> str:
    """What Harbor's defaults gave a Harbor Task, as the end of its warning."""
    if "user" not in config.agent.model_fields_set and config.agent_user == HARBOR_ACCOUNT:
        return f"; as in Harbor, its agent runs as {HARBOR_ACCOUNT}"
    return ""


def _moving_images(config: TaskConfig, folder: TaskFolder) -> list[tuple[str, str]]:
    """Every image the Task names, built on or used directly, that names no exact version,
    with the file naming it."""
    named: list[tuple[str, str]] = []
    for dockerfile in (folder.dockerfile, folder.verifier_dockerfile, folder.validation_dockerfile):
        if dockerfile is None:
            continue
        stages = _FROM.findall(dockerfile.read_text(encoding="utf-8", errors="replace"))
        aliases = {alias.lower() for _, alias in stages} | {"scratch"}
        path = dockerfile.relative_to(folder.root).as_posix()
        named += [(path, base) for base, _ in stages if base.lower() not in aliases]
    validation = config.verifier.validation
    tables = (config.verifier.environment, validation.environment if validation else None)
    for image in (config.environment.image, *(table.image if table else None for table in tables)):
        if image is not None:
            named.append((TASK_CONFIG, image))
    return [(path, image) for path, image in named if _moves(image)]


def _moves(image: str) -> bool:
    """Whether ``image`` can change under a Task: no digest, and no tag or ``latest``."""
    if "@" in image or "$" in image:  # a build argument names it elsewhere
        return False
    if PurePosixPath(image).is_absolute():  # a local disk, whose content is its identity
        return False
    return image.rpartition("/")[2].partition(":")[2] in ("", "latest")


def _image_user(dockerfile: Path) -> str | None:
    """The account other than root a Dockerfile's last stage runs as, if it names one."""
    text = dockerfile.read_text(encoding="utf-8", errors="replace")
    stages = list(_FROM.finditer(text))
    final = text[stages[-1].end() :] if stages else text
    users = _USER.findall(final)
    account = users[-1].partition(":")[0] if users else None
    return None if account in (None, HARBOR_ACCOUNT, "0") else account


def _spec(config: TaskConfig, folder: TaskFolder, name: TaskId) -> TaskSpec:
    """The runtime specification: ``task.toml`` with the folder's build definitions."""
    return TaskSpec(
        name=name,
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
            "sudo": config.agent_sudo,
            "container_features": config.environment.container_features,
            "nodes": config.environment.nodes,
        },
        network=config.agent_network,
        workdir=config.agent.workdir or config.environment.workdir,
        healthcheck=config.environment.healthcheck,
        timeouts={
            "agent": config.agent.timeout_sec,
            "verify": config.verifier.timeout_sec,
            "validation": (
                config.verifier.for_validation().timeout_sec
                if config.verifier.validation is not None
                else None
            ),
        },
        validation_rounds=config.agent.validation_rounds,
        validation_visible=bool(
            config.verifier.validation and config.verifier.validation.visibility
        ),
        artifacts=config.artifacts,
        tools=ToolProvision(
            skills_dir=config.environment.skills_dir,
            mcp_servers=tuple(entry.server for entry in config.environment.mcp_servers),
        ),
        verify=(verify := _verification(config, config.verifier, folder.verifier_dockerfile)),
        validation=_validation(config, folder, verify),
        variables=TaskVariables(
            environment=config.environment.env,
            verifier=config.verifier.env,
            verifier_environment=(
                config.verifier.environment.env if config.verifier.environment else None
            ),
            validation=validation.env if (validation := config.verifier.validation) else None,
            validation_environment=(
                validation.environment.env if validation and validation.environment else None
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
    environment, user = config.environment, config.agent_user
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
        return ImageSpec(
            kind=ImageKind.CONTAINER,
            user=user,
            keep_alive=config.harbor,
            build_timeout_sec=timeout,
        )
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
    return ImageSpec(
        kind=kind,
        ref=image,
        user=user,
        keep_alive=config.harbor and kind is not ImageKind.VM,
        build_timeout_sec=timeout,
    )


def _named_kind(image: str, operating_system: OperatingSystem) -> ImageKind | None:
    """What an image named alone is, as far as the Task decides it.

    Windows and macOS run only VMs, and an absolute path is a local VM disk. A Linux
    registry reference is either kind, so the registry decides it before preparation.
    """
    if operating_system is not OperatingSystem.LINUX or PurePosixPath(image).is_absolute():
        return ImageKind.VM
    return None


def _verification(
    config: TaskConfig,
    verifier: VerifierTable,
    dockerfile: Path | None,
    *,
    stage: Stage = Stage.TESTS,
    table: str = "verifier",
) -> VerifySpec:
    """Harbor's verifier semantics, for the verifier ``verifier`` describes, whose own image
    ``<stage>/Dockerfile`` builds; a Windows or macOS verifier image is a VM. A snapshot
    verifier boots from the solver's snapshot, so it has resources and no image."""
    mode = verifier.mode
    # Harbor's working directory: a verifier's own environment's, else the agent's.
    own = verifier.environment.workdir if verifier.environment is not None else None
    workdir = own or config.environment.workdir
    if mode is not VerificationMode.SEPARATE and dockerfile is not None:
        raise TaskDefinitionError(f"{stage}/Dockerfile requires separate verification in [{table}]")
    account = None if verifier.user is None else str(verifier.user)
    network = config.network_of(verifier)
    if mode is VerificationMode.SHARED:
        return VerifySpec(workdir=workdir, user=account, network=network)
    operating_system = config.environment.os
    if dockerfile is not None and operating_system is not OperatingSystem.LINUX:
        raise TaskDefinitionError(
            f"only a Linux verifier builds {stage}/Dockerfile; reuse the solver image or name "
            f"[{table}.environment].image"
        )

    environment = verifier.environment
    # Without a verifier environment, the verifier gets the solver's resources.
    sized = environment or config.environment
    named = environment.image if environment is not None else None
    user = config.agent_user
    image: ImageSpec | None = None
    if dockerfile is not None:
        if named is not None:
            raise TaskDefinitionError(
                f"{stage}/Dockerfile names its base in FROM; remove [{table}.environment].image"
            )
        image = ImageSpec(
            kind=ImageKind.CONTAINER,
            user=user,
            keep_alive=config.harbor,
            build_timeout_sec=sized.build_timeout_sec,
        )
    elif named is not None:
        kind = _named_kind(named, operating_system)
        image = ImageSpec(
            kind=kind,
            ref=named,
            user=user,
            keep_alive=config.harbor and kind is not ImageKind.VM,
        )

    resources = VerifierResources(
        cpus=sized.cpus or config.environment.cpus,
        memory_mb=sized.memory_mb or config.environment.memory_mb,
        storage_mb=sized.storage_mb,
        gpus=sized.gpus,
        gpu_types=sized.gpu_types or (),
        gpu_memory_mb=sized.gpu_memory_mb,
        container_features=sized.container_features,
        nodes=sized.nodes,
    )
    return VerifySpec(
        environment_mode=mode,
        image=image,
        resources=resources,
        workdir=workdir,
        user=account,
        network=network,
    )


def _validation(config: TaskConfig, folder: TaskFolder, verify: VerifySpec) -> VerifySpec | None:
    """The verifier of validation rounds, where ``[verifier.validation]`` or
    ``validation/Dockerfile`` sets it apart from ``tests/``'s, by the rules of ``tests/``'s
    over what ``[verifier]`` leaves it. A separate one runs the image ``validation/Dockerfile``
    builds or it names, or else the one it inherits, a separate verifier's of ``tests/``,
    else the solver's."""
    dockerfile = folder.validation_dockerfile
    own = config.verifier.validation
    if own is None and dockerfile is None:
        return None
    table = config.verifier.for_validation()
    named = own is not None and own.environment is not None and own.environment.image
    if dockerfile is not None and table.environment is not None and not named:
        # Its own Dockerfile builds its image, whatever image [verifier] names.
        table = table.model_copy(
            update={"environment": table.environment.model_copy(update={"image": None})}
        )
    placed = _verification(
        config, table, dockerfile, stage=Stage.VALIDATION, table="verifier.validation"
    )
    inherits = (
        placed.environment_mode is VerificationMode.SEPARATE
        and placed.image is None
        and verify.image is not None
        and verify.image.ref is None
    )
    # The image tests/Dockerfile builds, which it inherits as a key of [verifier] would be.
    return placed.model_copy(update={"image": verify.image}) if inherits else placed


def _require_files(folder: TaskFolder, config: TaskConfig) -> None:
    """The stage entries a Task runs, and one directory per calibration case.

    A calibration case runs as the oracle, so its directory holds ``solve.sh``, or
    ``solve.ps1`` on Windows. ``solution/`` is the reference solution every Task has; every
    further case is a directory of ``calibration/`` with a ``[[calibration]]`` entry.
    ``validation/`` is optional, and has the entry ``tests/`` has when it exists; a case's
    ``expect_validation`` needs it, since without it only ``tests/`` can validate, and
    ``expect`` already says what ``tests/`` must give.
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
    if not validation and (
        unchecked := [case.name for case in config.calibration if case.expect_validation]
    ):
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

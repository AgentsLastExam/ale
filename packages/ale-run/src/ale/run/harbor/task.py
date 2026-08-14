"""Load supported Harbor Task folders without depending on Harbor itself."""

from __future__ import annotations

import re
import subprocess
import tomllib
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from ale.core.environment import EpisodeContext
from ale.core.errors import TaskDefinitionError
from ale.core.ids import TaskId
from ale.core.task import Task, TaskSourceContext
from ale.core.taskspec import (
    BaseTaskSpec,
    ImageKind,
    ImageSpec,
    NetworkPolicy,
    PhaseTimeouts,
    ToolProvision,
    VerificationMode,
    VerifierResources,
    VerifySpec,
)
from ale.core.verdict import Rewards
from ale.run.content import tree_digest
from ale.run.harbor.config import (
    HarborArtifactConfig,
    HarborHealthcheckConfig,
    HarborTaskConfig,
)

__all__ = [
    "HarborTask",
    "HarborTaskFolder",
    "HarborTaskSpec",
    "discover_harbor_task_folders",
    "load_harbor_task_folder",
    "load_harbor_tasks",
]

TASK_MANIFEST = "task.toml"
INSTRUCTION = "instruction.md"
ENVIRONMENT_DIR = "environment"
SOLUTION_DIR = "solution"
TESTS_DIR = "tests"


class HarborTaskSpec(BaseTaskSpec):
    """Normalized runtime fields for one Harbor single-step Task."""

    spec_type: Literal["harbor/v1"] = "harbor/v1"
    environment: Literal["harbor"] = "harbor"
    environment_env: dict[str, str] = Field(default_factory=dict)
    verifier_environment_env: dict[str, str] = Field(default_factory=dict)
    verifier_env: dict[str, str] = Field(default_factory=dict)
    solution_env: dict[str, str] = Field(default_factory=dict)
    workdir: str | None = None
    healthcheck: HarborHealthcheckConfig | None = None
    verifier_network: NetworkPolicy
    verifier_healthcheck: HarborHealthcheckConfig | None = None
    verifier_workdir: str | None = None
    artifacts: tuple[HarborArtifactConfig, ...] = ()


class HarborTaskFolder:
    def __init__(self, root: Path, config: HarborTaskConfig) -> None:
        self.root = root.resolve()
        self.config = config

    @property
    def image_dir(self) -> Path:
        return self.root / ENVIRONMENT_DIR

    @property
    def image_dockerfile(self) -> Path | None:
        if self.config.environment.docker_image is not None:
            return None
        path = self.image_dir / "Dockerfile"
        return path if path.is_file() else None

    @property
    def verifier_dockerfile(self) -> Path | None:
        verifier = self.config.verifier
        if _verifier_mode(self.config) is VerificationMode.SHARED:
            return None
        if verifier.environment is not None and verifier.environment.docker_image is not None:
            return None
        path = self.root / TESTS_DIR / "Dockerfile"
        return path if path.is_file() else None

    def stage_dir(self, name: str) -> Path | None:
        mapped = {"oracle": SOLUTION_DIR, "verify": TESTS_DIR}.get(name)
        path = self.root / mapped if mapped else None
        return path if path is not None and path.is_dir() else None

    def stage_entry(self, name: str) -> Path | None:
        directory = self.stage_dir(name)
        entry_name = {"oracle": "solve.sh", "verify": "test.sh"}.get(name)
        entry = directory / entry_name if directory is not None and entry_name else None
        return entry if entry is not None and entry.is_file() else None

    @cached_property
    def content_digest(self) -> str:
        return tree_digest(self.root, exclude=(".ale-cache",))

    @cached_property
    def image_source_digest(self) -> str | None:
        if self.image_dockerfile is None:
            return None
        return tree_digest(self.image_dir)

    @cached_property
    def verifier_image_source_digest(self) -> str | None:
        if self.verifier_dockerfile is None:
            return None
        return tree_digest(self.root / TESTS_DIR)

    @cached_property
    def source(self) -> TaskSourceContext:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--show-toplevel"],
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
            repository_name=repository_root.name,
            task_relative_path=relative,
        )


class HarborTask(Task):
    folder: HarborTaskFolder

    def __init__(self, spec: HarborTaskSpec, folder: HarborTaskFolder) -> None:
        super().__init__(
            spec,
            folder=folder,
            source=folder.source,
            task_digest=folder.content_digest,
            image_source_digest=folder.image_source_digest,
            verifier_image_source_digest=folder.verifier_image_source_digest,
        )

    async def score(self, ctx: EpisodeContext) -> Rewards:
        if ctx.verified_rewards is None:
            raise TaskDefinitionError("Harbor verification produced no rewards")
        return {str(key): float(value) for key, value in ctx.verified_rewards.items()}


def discover_harbor_task_folders(path: Path) -> tuple[Path, ...]:
    source = path.expanduser().resolve()
    if not source.exists():
        raise TaskDefinitionError(f"Task path does not exist: {source}")
    if (source / TASK_MANIFEST).is_file():
        return (source,)
    manifests = sorted(
        source.rglob(TASK_MANIFEST),
        key=lambda item: item.relative_to(source).as_posix(),
    )
    if not manifests:
        raise TaskDefinitionError(f"no {TASK_MANIFEST} found below {source}")
    return tuple(manifest.parent for manifest in manifests)


def load_harbor_tasks(path: Path) -> list[HarborTask]:
    tasks: list[HarborTask] = []
    seen: dict[str, Path] = {}
    for root in discover_harbor_task_folders(path):
        task = _load_folder(root)
        previous = seen.get(task.id)
        if previous is not None:
            raise TaskDefinitionError(
                f"duplicate normalized Harbor Task name {task.id!r}: {previous} and {root}"
            )
        seen[task.id] = root
        tasks.append(task)
    return tasks


def load_harbor_task_folder(path: Path, *, variant: str = "base") -> HarborTask:
    if variant != "base":
        raise TaskDefinitionError("Harbor single-step Tasks have only the base variant")
    tasks = load_harbor_tasks(path)
    if len(tasks) != 1:
        raise TaskDefinitionError(f"{path} selects more than one Task")
    return tasks[0]


def _load_folder(root: Path) -> HarborTask:
    config_path = root / TASK_MANIFEST
    try:
        config = HarborTaskConfig.model_validate(tomllib.loads(config_path.read_text()))
    except FileNotFoundError as exc:
        raise TaskDefinitionError(f"missing {TASK_MANIFEST}: {config_path}") from exc
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise TaskDefinitionError(f"invalid Harbor task manifest {config_path}: {exc}") from exc
    folder = HarborTaskFolder(root, config)
    _require_folder(folder)
    spec = _effective_spec(folder)
    return HarborTask(spec, folder)


def _require_folder(folder: HarborTaskFolder) -> None:
    required = (folder.root / INSTRUCTION, folder.image_dir)
    missing = [path.name for path in required if not path.exists()]
    if missing:
        raise TaskDefinitionError(
            f"{folder.root} is missing required Harbor entry(s): {', '.join(missing)}"
        )
    compose = sorted(
        path.name
        for pattern in ("*compose*.yaml", "*compose*.yml")
        for path in folder.image_dir.glob(pattern)
    )
    if compose:
        raise TaskDefinitionError(
            f"Docker Compose is not supported by HarborEnvironment: {', '.join(compose)}"
        )
    if folder.image_dockerfile is None and folder.config.environment.docker_image is None:
        raise TaskDefinitionError(
            "Harbor solver requires environment/Dockerfile or environment.docker_image"
        )
    tests = folder.root / TESTS_DIR
    test_script = tests / "test.sh"
    verifier_mode = _verifier_mode(folder.config)
    if verifier_mode is VerificationMode.SHARED:
        baseline = folder.config.environment.network_policy()
        agent_network = folder.config.agent.network_policy(baseline)
        verifier_network = folder.config.verifier.network_policy(baseline)
        if verifier_network != agent_network:
            raise TaskDefinitionError(
                "shared Harbor verification cannot change the agent network policy"
            )
    if verifier_mode is VerificationMode.SHARED and not test_script.is_file():
        raise TaskDefinitionError("shared Harbor verification requires tests/test.sh")
    if verifier_mode is VerificationMode.SHARED and (tests / "Dockerfile").is_file():
        raise TaskDefinitionError("tests/Dockerfile requires separate Harbor verification")
    if verifier_mode is VerificationMode.SEPARATE:
        verifier = folder.config.verifier
        dedicated_ref = (
            verifier.environment.docker_image if verifier.environment is not None else None
        )
        if (
            not test_script.is_file()
            and dedicated_ref is None
            and folder.verifier_dockerfile is None
        ):
            raise TaskDefinitionError(
                "separate Harbor verification requires tests/test.sh or a dedicated verifier image"
            )


def _effective_spec(folder: HarborTaskFolder) -> HarborTaskSpec:
    config = folder.config
    environment = config.environment
    resources = environment.resources()
    network = config.agent.network_policy(environment.network_policy())
    verifier_environment = config.verifier.environment or environment
    verifier_network = config.verifier.network_policy(verifier_environment.network_policy())
    mode = _verifier_mode(config)
    verify_image: ImageSpec | None = None
    verify_resources: VerifierResources | None = None
    if mode is VerificationMode.SEPARATE:
        verifier_resources = verifier_environment.resources(resources)
        verify_resources = VerifierResources(
            cpus=verifier_resources.cpus,
            memory_mb=verifier_resources.memory_mb,
            storage_mb=verifier_resources.storage_mb,
            gpus=verifier_resources.gpus,
        )
        verifier_ref = (
            config.verifier.environment.docker_image
            if config.verifier.environment is not None
            else None
        )
        if verifier_ref is not None or folder.verifier_dockerfile is not None:
            verify_image = ImageSpec(kind=ImageKind.CONTAINER, ref=verifier_ref)

    raw_name = config.task.name.rsplit("/", 1)[-1] if config.task is not None else folder.root.name
    name = _task_id(raw_name)
    instruction = _strip_canary((folder.root / INSTRUCTION).read_text(encoding="utf-8"))
    return HarborTaskSpec(
        name=name,
        image=ImageSpec(kind=ImageKind.CONTAINER, ref=environment.docker_image),
        instruction=instruction,
        resources=resources,
        network=network,
        timeouts=PhaseTimeouts(
            setup=environment.build_timeout_sec,
            agent=config.agent.timeout_sec or 900,
            verify=config.verifier.timeout_sec,
        ),
        tools=ToolProvision(),
        params={},
        verify=VerifySpec(
            environment_mode=mode,
            image=verify_image,
            resources=verify_resources,
        ),
        metadata={
            **config.metadata,
            "harbor_task_name": config.task.name if config.task is not None else folder.root.name,
            "harbor_schema_version": config.version,
        },
        extras={},
        environment_env=environment.env,
        verifier_env=config.verifier.env,
        verifier_environment_env=verifier_environment.env,
        solution_env=config.solution.env,
        workdir=environment.workdir,
        healthcheck=environment.healthcheck,
        verifier_network=verifier_network,
        verifier_healthcheck=verifier_environment.healthcheck,
        verifier_workdir=verifier_environment.workdir,
        artifacts=config.normalized_artifacts,
    )


def _verifier_mode(config: HarborTaskConfig) -> VerificationMode:
    value = config.verifier.environment_mode
    if value is None:
        value = "separate" if config.verifier.environment is not None else "shared"
    return VerificationMode(value)


def _task_id(value: str) -> TaskId:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    try:
        return TaskId(normalized)
    except ValueError as exc:
        raise TaskDefinitionError(f"Harbor Task name {value!r} cannot form an ALE Task ID") from exc


def _strip_canary(value: str) -> str:
    lines = value.splitlines()
    while lines and re.match(r"^(?:<!--.*canary.*-->|#.*canary.*)$", lines[0].strip(), re.I):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)

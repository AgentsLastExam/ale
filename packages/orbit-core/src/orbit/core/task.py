"""Runtime binding for one rendered Task instance."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from orbit.core.lock import TaskVariableProvenance
from orbit.core.sandbox import PreparedTaskImage
from orbit.core.taskspec import SOLUTION_CASE, OperatingSystem, TaskSpec, TaskVariables
from orbit.core.verdict import Rewards

if TYPE_CHECKING:
    from orbit.core.episode import EpisodeContext

__all__ = [
    "CALIBRATION_DIR",
    "SOLUTION_DIR",
    "ResolvedVariables",
    "Stage",
    "Task",
    "TaskAssetObservation",
    "TaskFolder",
    "TaskSourceContext",
    "case_entry",
    "case_path",
]


@dataclass(frozen=True)
class TaskSourceContext:
    repository_root: Path | None = None
    repository_name: str | None = None
    task_relative_path: str | None = None

    def __post_init__(self) -> None:
        values = self.repository_root, self.repository_name, self.task_relative_path
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("Task source context fields must be present or absent together")


@dataclass(frozen=True)
class ResolvedVariables:
    """A Task's variables with their values, resolved on the Host before anything runs."""

    values: TaskVariables = field(default_factory=TaskVariables)
    secrets: tuple[str, ...] = ()
    """Values every record leaves out: the credentials among them."""
    provenance: tuple[TaskVariableProvenance, ...] = ()


@dataclass(frozen=True)
class TaskAssetObservation:
    repository: str
    task_path: str
    commit: str | None
    dirty: bool


#: The directory of a Task folder holding one directory per further calibration case, each
#: declared in ``[[calibration]]``.
CALIBRATION_DIR = "calibration"
#: The directory of the reference solution every Task has, the calibration case ``solution``:
#: the Task root's ``solution/``, where Harbor keeps it too.
SOLUTION_DIR = "solution"


def case_path(case: str) -> str:
    """A calibration case's directory, relative to the Task folder: ``solution/`` for the
    reference solution, ``calibration/<case>/`` for every further case."""
    return SOLUTION_DIR if case == SOLUTION_CASE else f"{CALIBRATION_DIR}/{case}"


def case_entry(operating_system: OperatingSystem) -> str:
    """The script a calibration case's directory holds and the oracle runs: ``solve.sh``, or
    ``solve.ps1`` on Windows."""
    return "solve.ps1" if operating_system is OperatingSystem.WINDOWS else "solve.sh"


class Stage(StrEnum):
    """A Task stage, named as its directory is in the sandbox."""

    SETUP = "setup"
    TESTS = "tests"
    VALIDATION = "validation"
    """The verifier whose results the agent receives between its turns; ``tests/``'s
    contract."""

    def entry(self, operating_system: OperatingSystem) -> str:
        """The script a stage runs: ``test.sh``, or ``test.ps1`` on Windows."""
        name = {Stage.SETUP: "setup", Stage.TESTS: "test", Stage.VALIDATION: "test"}[self]
        return f"{name}.ps1" if operating_system is OperatingSystem.WINDOWS else f"{name}.sh"


class TaskFolder(Protocol):
    """Filesystem surface used by the standard runtime."""

    root: Path

    @property
    def environment_dir(self) -> Path: ...

    @property
    def dockerfile(self) -> Path | None: ...

    def build_script(self, operating_system: OperatingSystem) -> Path | None: ...

    @property
    def verifier_dockerfile(self) -> Path | None: ...

    @property
    def validation_dockerfile(self) -> Path | None: ...

    def stage_dir(self, stage: Stage) -> Path | None: ...

    def stage_entry(
        self, stage: Stage, operating_system: OperatingSystem = OperatingSystem.LINUX
    ) -> Path | None: ...

    def case_dir(self, case: str) -> Path | None: ...


class Task(ABC):
    def __init__(
        self,
        spec: TaskSpec,
        *,
        folder: TaskFolder | None,
        source: TaskSourceContext | None = None,
        task_digest: str,
        image_source_digest: str | None,
        verifier_image_source_digest: str | None = None,
        validation_image_source_digest: str | None = None,
        non_verifier_digest: str | None = None,
        harbor: bool = False,
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.spec = spec
        self.folder = folder
        self.source = source or TaskSourceContext()
        self.harbor = harbor
        """Whether it was read as a Harbor Task, so Harbor's defaults apply where it sets none."""
        self.warnings = warnings
        """Where it departs from Orbit's form, each naming the file it is about."""
        self.task_digest = task_digest
        self.non_verifier_digest = non_verifier_digest
        self.image_source_digest = image_source_digest
        self.verifier_image_source_digest = verifier_image_source_digest
        self.validation_image_source_digest = validation_image_source_digest
        """What the verifier of validation rounds' own image is built from,
        ``validation/Dockerfile``'s context; None where it builds none."""
        self.provider: str | None = None
        """The provider id the Run selected to start the solver's Sandbox."""
        self.verifier_provider: str | None = None
        """The provider id selected for a verifier's own Sandbox: a separate verifier's, or a
        snapshot verifier's, which is its solver's."""
        self.prepared_image: PreparedTaskImage | None = None
        self.prepared_verifier_image: PreparedTaskImage | None = None
        self.prepared_validation_image: PreparedTaskImage | None = None
        self.asset_observation: TaskAssetObservation | None = None
        self.variables: ResolvedVariables | None = None
        self.validations: int = 0
        """How many times the Run's episodes of it validate before ``tests/`` scores the final
        state: once for each validation round its agent receives, or once for calibration's
        validation check of each case's final state. Each saves the solver as a snapshot, so
        selection gives it a Provider that saves that many in a row."""

    @property
    def id(self) -> str:
        return str(self.spec.name)

    @property
    def validates(self) -> bool:
        """Whether the Run's episodes of it validate."""
        return self.validations > 0

    @property
    def validation_stage(self) -> Stage:
        """The stage that validates: ``validation/``, or ``tests/`` for a Task without one."""
        folder = self.folder
        if folder is not None and folder.stage_dir(Stage.VALIDATION) is not None:
            return Stage.VALIDATION
        return Stage.TESTS

    async def setup(self, ctx: EpisodeContext) -> None:
        return None

    @abstractmethod
    async def score(self, ctx: EpisodeContext) -> Rewards:
        """Produce rewards for a finished episode."""

    async def cleanup(self, ctx: EpisodeContext) -> None:
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.spec.name})"

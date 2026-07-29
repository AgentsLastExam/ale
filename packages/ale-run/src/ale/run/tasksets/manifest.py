"""Loading tasks from a task repository checkout.

A task folder becomes one or more :class:`TaskSpec` values here — one per variant, each
with its own identity and its own rendered instruction. Nothing is duplicated on disk.

Two invariants are enforced during loading rather than left to review:

* an identifier is derived from the folder path and never read back for meaning;
* a component whose asset lock marks it ``verify`` cannot appear in a setup stage, so
  answer material cannot be staged before the agent by a task that asks in the wrong
  place.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ale.core.domain import DomainManifest
from ale.core.environment import EpisodeContext
from ale.core.errors import TaskDefinitionError
from ale.core.ids import slugify_path
from ale.core.kit import KitsLock
from ale.core.task import Task, Taskset
from ale.core.taskspec import ImageRef, SetupStage, TaskSpec, VerifyStage
from ale.core.template import render_instruction
from ale.core.verdict import Rewards

__all__ = ["ManifestTask", "ManifestTaskset", "TaskFolder", "load_task_folder"]

TASK_MANIFEST = "task.yaml"
INSTRUCTION = "instruction.md"
DOMAIN_MANIFEST = "domain.yaml"
KITS_LOCK = "kits.lock.yaml"
TASKS_DIR = "tasks"

STAGE_ENTRY = "run.sh"
VERIFY_DIR = "verify"
ORACLE_DIR = "oracle"
SETUP_DIR = "setup"
FILES_DIR = "files"

#: Never uploaded while the agent is running: manifests, scoring logic, solutions.
AGENT_INVISIBLE = frozenset({TASK_MANIFEST, VERIFY_DIR, ORACLE_DIR})


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise TaskDefinitionError(f"missing {path.name}: {path}") from exc
    except yaml.YAMLError as exc:
        raise TaskDefinitionError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TaskDefinitionError(f"{path} must contain a mapping")
    return loaded


class TaskFolder:
    """The on-disk form of a task, and the paths its stages use."""

    def __init__(self, root: Path, repo_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.repo_root = (repo_root or _find_repo_root(self.root)).resolve()

    @property
    def relative_path(self) -> str:
        """Path under ``tasks/``, which is what the identifier mirrors."""
        tasks_root = self.repo_root / TASKS_DIR
        try:
            return str(self.root.relative_to(tasks_root))
        except ValueError:
            return self.root.name

    def stage_dir(self, name: str) -> Path | None:
        path = self.root / name
        return path if path.is_dir() else None

    def stage_entry(self, name: str) -> Path | None:
        directory = self.stage_dir(name)
        if directory is None:
            return None
        entry = directory / STAGE_ENTRY
        return entry if entry.is_file() else None

    def visible_files(self) -> Path | None:
        """Small task files staged into the workspace before the agent runs."""
        return self.stage_dir(FILES_DIR)


def _find_repo_root(start: Path) -> Path:
    """Walk up to the directory holding ``domain.yaml``."""
    for candidate in [start, *start.parents]:
        if (candidate / DOMAIN_MANIFEST).is_file():
            return candidate
    raise TaskDefinitionError(
        f"no {DOMAIN_MANIFEST} above {start}: is this inside a task repository?"
    )


class ManifestTask(Task):
    """A task whose behaviour comes entirely from its folder.

    Scoring runs the task's own ``verify`` entry point inside the sandbox and reads the
    rewards it wrote; the environment, not this class, decides when that happens.
    """

    def __init__(self, spec: TaskSpec, folder: TaskFolder) -> None:
        super().__init__(spec)
        self.folder = folder

    async def score(self, ctx: EpisodeContext) -> Rewards:
        """Rewards are produced by the verify stage; the environment records them."""
        rewards = ctx.extras.get("rewards")
        if not isinstance(rewards, dict):
            raise TaskDefinitionError(
                "verification produced no rewards; the verify stage must write them"
            )
        return {str(key): float(value) for key, value in rewards.items()}


class ManifestTaskset(Taskset):
    """Loads tasks from a checkout of a task repository."""

    def __init__(self, path: Path, *, task_filter: str | None = None) -> None:
        self.path = path.resolve()
        self.task_filter = task_filter
        self.repo_root = _find_repo_root(self.path)
        self.domain = DomainManifest.model_validate(_read_yaml(self.repo_root / DOMAIN_MANIFEST))
        self.kits = self._load_kits()

    def load(self) -> Iterator[Task]:
        for folder in self.folders():
            yield from self.load_folder(folder)

    def metadata(self) -> dict[str, Any]:
        return {
            "domain": self.domain.name,
            "repo_root": str(self.repo_root),
            "requires_core": self.domain.requires_core,
        }

    # --- loading ---

    def _load_kits(self) -> KitsLock:
        path = self.repo_root / KITS_LOCK
        return KitsLock.model_validate(_read_yaml(path)) if path.is_file() else KitsLock()

    def folders(self) -> Iterator[TaskFolder]:
        """Yield the task folders selected by this taskset's path."""
        if (self.path / TASK_MANIFEST).is_file():
            yield TaskFolder(self.path, self.repo_root)
            return
        search_root = self.path if self.path != self.repo_root else self.repo_root / TASKS_DIR
        for manifest in sorted(search_root.rglob(TASK_MANIFEST)):
            folder = TaskFolder(manifest.parent, self.repo_root)
            if self.task_filter and self.task_filter not in folder.relative_path:
                continue
            yield folder

    def load_folder(self, folder: TaskFolder) -> Iterator[ManifestTask]:
        raw = _read_yaml(folder.root / TASK_MANIFEST)
        instruction_path = folder.root / INSTRUCTION
        if not instruction_path.is_file():
            raise TaskDefinitionError(f"{folder.root} has no {INSTRUCTION}")
        template = instruction_path.read_text(encoding="utf-8")

        task_id = slugify_path(folder.relative_path)
        base_params = dict(raw.pop("params", {}) or {})
        variants = raw.pop("variants", None)

        for variant_name, params, overrides in self._variants(base_params, variants):
            spec = self._build_spec(
                raw=raw | overrides,
                folder=folder,
                task_id=task_id,
                variant=variant_name,
                params=params,
                template=template,
            )
            yield ManifestTask(spec, folder)

    def _variants(
        self, base_params: dict[str, Any], variants: list[dict[str, Any]] | None
    ) -> Iterator[tuple[str | None, dict[str, Any], dict[str, Any]]]:
        """One entry per task instance: a single unnamed one, or one per variant.

        A variant may override more than parameters. Data bundles are per variant in
        practice — each has its own inputs and its own gold answers — so a variant can
        also name its own assets, and may declare an image when it genuinely needs a
        different one (a GUI variant beside a file-only one).
        """
        if not variants:
            yield None, base_params, {}
            return
        seen: set[str] = set()
        for entry in variants:
            name = entry.get("name")
            if not name:
                raise TaskDefinitionError("every variant needs a name")
            if name in seen:
                raise TaskDefinitionError(f"duplicate variant name: {name}")
            seen.add(name)
            overrides = {
                key: value
                for key, value in entry.items()
                if key
                in {
                    "image",
                    "setup",
                    "verify",
                    "harness_family",
                    "resources",
                    "timeouts",
                    "tools",
                }
            }
            yield name, base_params | dict(entry.get("params", {}) or {}), overrides

    def _build_spec(
        self,
        *,
        raw: dict[str, Any],
        folder: TaskFolder,
        task_id: str,
        variant: str | None,
        params: dict[str, Any],
        template: str,
    ) -> TaskSpec:
        payload = dict(raw)
        image = payload.pop("image", None)
        if not image:
            raise TaskDefinitionError(f"{folder.relative_path} declares no image")

        setup = SetupStage.model_validate(payload.pop("setup", None) or {})
        verify = VerifyStage.model_validate(payload.pop("verify", None) or {})
        self._check_kits(folder, setup, verify)

        where = f"{folder.relative_path}/{INSTRUCTION}"
        instruction = render_instruction(template, params, where=where)

        try:
            return TaskSpec.model_validate(
                {
                    **payload,
                    "id": task_id,
                    "domain": self.domain.name,
                    "variant": variant,
                    "instruction": instruction,
                    "image": _parse_image(image),
                    "setup": setup,
                    "verify": verify,
                    "params": params,
                }
            )
        except ValidationError as exc:
            raise TaskDefinitionError(f"invalid task manifest: {exc}") from exc

    # --- checks the loader owns ---

    def _check_kits(self, folder: TaskFolder, setup: SetupStage, verify: VerifyStage) -> None:
        for name in (*setup.kits, *verify.kits):
            try:
                self.kits.require(name)
            except KeyError as exc:
                raise TaskDefinitionError(f"{folder.relative_path}: {exc}") from exc


def _parse_image(value: str) -> ImageRef:
    name, _, tag = value.partition(":")
    return ImageRef(name=name, tag=tag or "latest")


def load_task_folder(path: Path) -> ManifestTask:
    """Load exactly one task from a folder, for the single-task paths of the CLI."""
    tasks = list(ManifestTaskset(path).load())
    if not tasks:
        raise TaskDefinitionError(f"no task found at {path}")
    if len(tasks) > 1:
        names = ", ".join(task.spec.label for task in tasks)
        raise TaskDefinitionError(f"{path} defines several variants ({names}); select one")
    return tasks[0]  # type: ignore[return-value]


def kit_source_dir(repo_root: Path, name: str) -> Path:
    """Where a kit's package lives in a checkout."""
    return repo_root / "kits" / name

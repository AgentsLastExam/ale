"""Synchronize Task-local stage assets with same-named Hugging Face datasets."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ale.core.config import resolve_asset_collection
from ale.core.errors import AssetError
from ale.core.task import Task, TaskAssetObservation
from ale.run.tasksets.manifest import StandardTask, load_standard_tasks

Stage = Literal["image", "setup", "verify", "oracle"]
STAGES: tuple[Stage, ...] = ("image", "setup", "verify", "oracle")

__all__ = [
    "AssetObservation",
    "AssetRepositorySelection",
    "AssetSyncResult",
    "asset_status",
    "observe_task_assets",
    "pull_assets",
    "push_assets",
    "select_asset_repositories",
]


AssetObservation = TaskAssetObservation


@dataclass(frozen=True)
class AssetRepositorySelection:
    repository_name: str
    repository_root: Path
    tasks: tuple[StandardTask, ...]


@dataclass(frozen=True)
class AssetSyncResult:
    repository: str
    remote_repo_id: str
    collection_slug: str
    commit: str
    tasks: tuple[AssetObservation, ...]


def _asset_roots(task: Task) -> tuple[Path, ...]:
    if task.folder is None:
        return ()
    return tuple(path for stage in STAGES if (path := task.folder.root / stage / "assets").is_dir())


def _task_path(task: StandardTask) -> str:
    if task.source.task_relative_path is None:
        raise AssetError("asset commands require Tasks inside a Git repository")
    return task.source.task_relative_path


def observe_task_assets(task: Task) -> AssetObservation | None:
    if task.folder is None:
        return None
    roots = _asset_roots(task)
    source = task.source
    if not source.repository_root or not source.repository_name or not source.task_relative_path:
        return AssetObservation("", str(task.folder.root), None, True) if roots else None
    with _repository_lock(source.repository_root, exclusive=True):
        state = _read_state(source.repository_root, source.repository_name)
        markers = state["tasks"]
        assert isinstance(markers, dict)
        marker = markers.get(source.task_relative_path)
        if not roots and not isinstance(marker, dict):
            return None
        inventory = _asset_inventory(task)
        dirty = not isinstance(marker, dict) or marker.get("inventory") != inventory
        commit = marker.get("commit") if isinstance(marker, dict) else None
        observation = AssetObservation(
            source.repository_name,
            source.task_relative_path,
            commit if isinstance(commit, str) else None,
            dirty,
        )
        if isinstance(marker, dict) and marker.get("dirty") != dirty:
            marker["dirty"] = dirty
            _write_state(source.repository_root, state)
        return observation


def asset_status(paths: Sequence[Path]) -> tuple[AssetObservation, ...]:
    return tuple(
        observation
        for selection in select_asset_repositories(paths)
        for task in selection.tasks
        if (observation := observe_task_assets(task)) is not None
    )


def select_asset_repositories(paths: Sequence[Path]) -> tuple[AssetRepositorySelection, ...]:
    tasks_by_root: dict[Path, StandardTask] = {}
    for path in paths:
        for task in load_standard_tasks(path.expanduser().resolve()):
            if task.spec.variant == "base":
                tasks_by_root[task.folder.root] = task

    repositories: dict[Path, list[StandardTask]] = {}
    names: dict[str, Path] = {}
    for task in tasks_by_root.values():
        source = task.source
        if not source.repository_root or not source.repository_name:
            raise AssetError("asset commands require Tasks inside a Git repository")
        previous = names.get(source.repository_name)
        if previous is not None and previous != source.repository_root:
            raise AssetError(f"ambiguous Task repository name {source.repository_name!r}")
        names[source.repository_name] = source.repository_root
        repositories.setdefault(source.repository_root, []).append(task)
    return tuple(
        AssetRepositorySelection(
            root.name,
            root,
            tuple(sorted(items, key=_task_path)),
        )
        for root, items in sorted(repositories.items(), key=lambda item: str(item[0]))
    )


def pull_assets(
    paths: Sequence[Path], *, collection: str | None = None, force: bool = False
) -> tuple[AssetSyncResult, ...]:
    from huggingface_hub import HfApi, snapshot_download

    config = resolve_asset_collection(collection)
    api = HfApi()
    remote_collection = api.get_collection(config.collection_slug)
    results: list[AssetSyncResult] = []
    for selection in select_asset_repositories(paths):
        remote_repo_id = _collection_dataset(remote_collection, selection.repository_name)
        commit = api.repo_info(remote_repo_id, repo_type="dataset").sha
        if not commit:
            raise AssetError(f"dataset {remote_repo_id} has no resolved commit")
        dirty = [
            item
            for item in asset_status([task.folder.root for task in selection.tasks])
            if item.dirty
        ]
        if dirty and not force:
            names = ", ".join(item.task_path for item in dirty)
            raise AssetError(f"local assets are dirty for {names}; pass --force to replace them")

        with tempfile.TemporaryDirectory(prefix="ale-assets-pull-") as temporary:
            downloaded = Path(temporary)
            try:
                snapshot_download(
                    repo_id=remote_repo_id,
                    repo_type="dataset",
                    revision=commit,
                    allow_patterns=[f"{_task_path(task)}/*/assets/**" for task in selection.tasks],
                    local_dir=downloaded,
                )
                with _repository_lock(selection.repository_root, exclusive=True):
                    _replace_selected_assets(selection.tasks, downloaded)
                    observations = _mark_clean(selection, commit)
            except AssetError:
                raise
            except Exception as exc:
                raise AssetError(
                    f"could not pull assets for {selection.repository_name}: {exc}"
                ) from exc
        results.append(
            AssetSyncResult(
                selection.repository_name,
                remote_repo_id,
                config.collection_slug,
                commit,
                observations,
            )
        )
    return tuple(results)


def push_assets(
    paths: Sequence[Path], *, collection: str | None = None
) -> tuple[AssetSyncResult, ...]:
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

    api = HfApi()
    try:
        username = str(api.whoami()["name"])
    except Exception as exc:
        raise AssetError(f"Hugging Face authentication is required for push: {exc}") from exc
    if collection:
        collection_slug = resolve_asset_collection(collection).collection_slug
        api.get_collection(collection_slug)
    else:
        environment = os.environ.get("ALE_ASSETS_COLLECTION", "").strip()
        collection_slug = (
            resolve_asset_collection().collection_slug
            if environment
            else api.create_collection("assets", namespace=username, exists_ok=True).slug
        )

    results: list[AssetSyncResult] = []
    for selection in select_asset_repositories(paths):
        remote_repo_id = f"{username}/{selection.repository_name}"
        api.create_repo(remote_repo_id, repo_type="dataset", private=False, exist_ok=True)
        api.add_collection_item(collection_slug, remote_repo_id, "dataset", exists_ok=True)
        parent = api.repo_info(remote_repo_id, repo_type="dataset").sha or None
        remote_files = set(api.list_repo_files(remote_repo_id, repo_type="dataset"))
        operations: list[CommitOperationAdd | CommitOperationDelete] = []
        for task in selection.tasks:
            task_path = _task_path(task)
            prefixes = [f"{task_path}/{stage}/assets/" for stage in STAGES]
            local_files: dict[str, Path] = {}
            for root in _asset_roots(task):
                for file in _regular_files(root):
                    remote = f"{task_path}/{file.relative_to(task.folder.root).as_posix()}"
                    local_files[remote] = file
                    operations.append(CommitOperationAdd(path_in_repo=remote, path_or_fileobj=file))
            for remote in sorted(remote_files):
                if (
                    any(remote.startswith(prefix) for prefix in prefixes)
                    and remote not in local_files
                ):
                    operations.append(CommitOperationDelete(path_in_repo=remote))
        if operations:
            commit = api.create_commit(
                remote_repo_id,
                operations,
                commit_message="Synchronize ALE Task assets",
                repo_type="dataset",
                parent_commit=parent,
            ).oid
        elif parent:
            commit = parent
        else:
            raise AssetError(f"{selection.repository_name} has no local assets to push")
        with _repository_lock(selection.repository_root, exclusive=True):
            observations = _mark_clean(selection, commit)
        results.append(
            AssetSyncResult(
                selection.repository_name,
                remote_repo_id,
                collection_slug,
                commit,
                observations,
            )
        )
    return tuple(results)


def _replace_selected_assets(tasks: Sequence[StandardTask], downloaded: Path) -> None:
    entries: list[tuple[Path, Path, Path]] = []
    for task in tasks:
        task_path = _task_path(task)
        for stage in STAGES:
            source = downloaded / task_path / stage / "assets"
            destination = task.folder.root / stage / "assets"
            backup = destination.with_name(f".{destination.name}.ale-backup")
            if source.is_dir():
                list(_regular_files(source))
            entries.append((source, destination, backup))

    for _, _, backup in entries:
        shutil.rmtree(backup, ignore_errors=True)
    try:
        for _, destination, backup in entries:
            if destination.exists():
                destination.rename(backup)
        for source, destination, _ in entries:
            if source.is_dir():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, destination)
    except Exception:
        for _, destination, backup in entries:
            shutil.rmtree(destination, ignore_errors=True)
            if backup.exists():
                backup.rename(destination)
        raise
    for _, _, backup in entries:
        shutil.rmtree(backup, ignore_errors=True)


def _mark_clean(selection: AssetRepositorySelection, commit: str) -> tuple[AssetObservation, ...]:
    state = _read_state(selection.repository_root, selection.repository_name)
    markers = state["tasks"]
    assert isinstance(markers, dict)
    observations: list[AssetObservation] = []
    for task in selection.tasks:
        task_path = _task_path(task)
        if _asset_roots(task):
            markers[task_path] = {
                "commit": commit,
                "dirty": False,
                "inventory": _asset_inventory(task),
            }
            observations.append(
                AssetObservation(selection.repository_name, task_path, commit, False)
            )
        else:
            markers.pop(task_path, None)
    _write_state(selection.repository_root, state)
    return tuple(observations)


def _asset_inventory(task: Task) -> list[dict[str, int | str]]:
    if task.folder is None:
        return []
    task_root = task.folder.root
    inventory: list[dict[str, int | str]] = []
    for root in _asset_roots(task):
        for path in (root, *sorted(root.rglob("*"))):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise AssetError(f"Task assets support only regular files and directories: {path}")
            stat = path.stat()
            inventory.append(
                {
                    "path": path.relative_to(task_root).as_posix(),
                    "type": "file" if path.is_file() else "directory",
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    return inventory


def _regular_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (path.exists() and not path.is_file() and not path.is_dir()):
            raise AssetError(f"Task assets support only regular files and directories: {path}")
        if path.is_file():
            yield path


def _state_path(repository_root: Path) -> Path:
    return repository_root / ".ale-cache" / "assets.json"


def _read_state(repository_root: Path, repository: str) -> dict[str, object]:
    path = _state_path(repository_root)
    if not path.is_file():
        return {"schema_version": 1, "repository": repository, "tasks": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError(f"invalid asset state {path}: {exc}") from exc
    if value.get("schema_version") != 1 or value.get("repository") != repository:
        raise AssetError(f"incompatible asset state {path}")
    if not isinstance(value.setdefault("tasks", {}), dict):
        raise AssetError(f"invalid asset state {path}: tasks must be an object")
    return value


def _write_state(repository_root: Path, state: dict[str, object]) -> None:
    path = _state_path(repository_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def _repository_lock(repository_root: Path, *, exclusive: bool) -> Iterator[None]:
    lock = repository_root / ".ale-cache" / "assets.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _collection_dataset(collection: object, repository_name: str) -> str:
    matches = [
        item_id
        for item in getattr(collection, "items", ())
        if getattr(item, "item_type", getattr(item, "type", None)) == "dataset"
        and isinstance((item_id := getattr(item, "item_id", getattr(item, "id", None))), str)
        and item_id.rsplit("/", 1)[-1] == repository_name
    ]
    if len(matches) != 1:
        raise AssetError(
            f"collection must contain exactly one dataset named {repository_name!r}; "
            f"found {len(matches)}"
        )
    return matches[0]

"""Local Git/LFS checkpoints and publication of the same commits to Hugging Face."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ale.core.errors import AssetError
from ale.run.assets import (
    STAGES,
    AssetLocation,
    AssetRepositorySelection,
    _asset_inventory,
    _mark_clean,
    _regular_files,
    _replace_selected_assets,
    _repository_lock,
    ensure_asset_repository,
    observe_task_assets,
    validate_asset_paths,
)
from ale.run.tasksets.manifest import TaskFolder


class AssetCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    repo_id: str = Field(pattern=r"^agents-last-exam/[A-Za-z0-9_.-]+$")
    collection_slug: str | None = None
    task_path: str
    workspace: Path
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    base_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    # --- validation ---
    @field_validator("task_path")
    @classmethod
    def _relative_task(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("task_path must be a non-empty relative path")
        return value


def read_checkpoint(path: Path) -> AssetCheckpoint:
    try:
        return AssetCheckpoint.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        raise AssetError(f"invalid asset checkpoint {path}: {exc}") from exc


def write_record(path: Path, value: BaseModel | dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        value.model_dump_json(indent=2)
        if isinstance(value, BaseModel)
        else json.dumps(value, indent=2)
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text + "\n")
    temporary.replace(path)


def _git(root: Path, *args: str) -> str:
    from huggingface_hub import get_token

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    token = get_token()
    if token:
        index = int(env.get("GIT_CONFIG_COUNT", "0"))
        env.update(
            {
                "GIT_CONFIG_COUNT": str(index + 1),
                f"GIT_CONFIG_KEY_{index}": "http.https://huggingface.co/.extraheader",
                f"GIT_CONFIG_VALUE_{index}": "Authorization: Basic "
                + base64.b64encode(f"hf_user:{token}".encode()).decode(),
            }
        )
    try:
        process = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise AssetError(f"could not run Git: {exc}") from exc
    if process.returncode:
        detail = process.stderr.strip()
        if token:
            detail = detail.replace(token, "[redacted]")
        raise AssetError(f"git {args[0]} failed: {detail}")
    return process.stdout.strip()


def _selection(task: Path) -> AssetRepositorySelection:
    validate_asset_paths(task)
    location = AssetLocation(TaskFolder(task))
    source = location.source
    if not source.repository_root or not source.repository_name:
        raise AssetError("asset checkpoint operations require a Task path inside a Git repository")
    return AssetRepositorySelection(source.repository_name, source.repository_root, (location,))


def _paths(checkpoint: AssetCheckpoint) -> list[str]:
    return [f"{checkpoint.task_path}/{stage}/assets" for stage in STAGES]


def _metadata(workspace: Path) -> Path:
    return Path(_git(workspace, "rev-parse", "--absolute-git-dir")) / "ale-assets.json"


def _initialize(
    selection: AssetRepositorySelection, workspace: Path, branch: str, collection: str | None
) -> AssetCheckpoint:
    root = selection.repository_root
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    repository = common / "ale-assets/repository"
    with _repository_lock(common, exclusive=True):
        if workspace.exists():
            checkpoint = read_checkpoint(_metadata(workspace))
            if (
                checkpoint.task_path != selection.tasks[0].source.task_relative_path
                or checkpoint.repo_id != f"agents-last-exam/{selection.repository_name}"
            ):
                raise AssetError("asset workspace belongs to a different Task repository or Task")
            if _git(workspace, "branch", "--show-current") != branch:
                raise AssetError("asset workspace is on a different Job branch")
            return checkpoint
        _git(root, "check-ref-format", "--branch", branch)
        repo_id, slug = ensure_asset_repository(selection.repository_name, collection)
        repository.parent.mkdir(parents=True, exist_ok=True)
        if not repository.exists():
            _git(
                root,
                "clone",
                "--no-checkout",
                f"https://huggingface.co/datasets/{repo_id}",
                str(repository),
            )
            _git(repository, "lfs", "install", "--local")
            excludes = repository / ".git/info/exclude"
            excludes.write_text(excludes.read_text() + "\n.ale-cache/\n")
        else:
            _git(repository, "fetch", "origin", "main")
        base = _git(repository, "rev-parse", "refs/remotes/origin/main")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        _git(repository, "worktree", "add", "-b", branch, str(workspace), base)
        checkpoint = AssetCheckpoint(
            repo_id=repo_id,
            collection_slug=slug,
            task_path=selection.tasks[0].source.task_relative_path or "",
            workspace=workspace,
            base_commit=base,
            commit=base,
        )
        write_record(_metadata(workspace), checkpoint)
        return checkpoint


def checkpoint_assets(
    task: Path, *, workspace: Path, branch: str, collection: str | None = None
) -> AssetCheckpoint:
    selection = _selection(task)
    task = selection.tasks[0].folder.root
    workspace = workspace.expanduser().resolve()
    if workspace.is_relative_to(task):
        raise AssetError("the asset worktree must be outside the Task folder")
    inventory = _asset_inventory(selection.tasks[0])
    observation = observe_task_assets(selection.tasks[0])
    if (
        not any(item["type"] == "file" for item in inventory)
        and not workspace.exists()
        and (observation is None or observation.commit is None)
    ):
        return AssetCheckpoint(
            repo_id=f"agents-last-exam/{selection.repository_name}",
            task_path=selection.tasks[0].source.task_relative_path or "",
            workspace=workspace,
        )
    checkpoint = _initialize(selection, workspace, branch, collection)
    with _repository_lock(workspace, exclusive=True):
        if _git(workspace, "status", "--porcelain", "--untracked-files=all"):
            raise AssetError("asset workspace contains uncommitted changes")
        observation = observe_task_assets(selection.tasks[0])
        head = _git(workspace, "rev-parse", "HEAD")
        if observation and not observation.dirty and observation.commit == head:
            return checkpoint.model_copy(update={"commit": head})
        destination = workspace / checkpoint.task_path
        validate_asset_paths(destination)
        for stage in STAGES:
            source = task / stage / "assets"
            target = destination / stage / "assets"
            shutil.rmtree(target, ignore_errors=True)
            if source.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target)
        attributes = workspace / ".gitattributes"
        existing = attributes.read_text() if attributes.exists() else ""
        rule = "**/assets/** filter=lfs diff=lfs merge=lfs -text"
        if rule not in existing.splitlines():
            attributes.write_text(existing.rstrip("\n") + "\n" + rule + "\n")
        _git(workspace, "add", "-A", "--", ".gitattributes", checkpoint.task_path)
        if _git(workspace, "diff", "--cached", "--name-only"):
            _git(
                workspace,
                "-c",
                "user.name=ALE",
                "-c",
                "user.email=ale@agents-last-exam.org",
                "commit",
                "-m",
                f"Update {checkpoint.task_path} assets",
            )
        commit = _git(workspace, "rev-parse", "HEAD")
        if checkpoint.base_commit and not _git(
            workspace,
            "diff",
            "--name-only",
            checkpoint.base_commit,
            commit,
            "--",
            *_paths(checkpoint),
        ):
            commit = checkpoint.base_commit
        if _asset_inventory(selection.tasks[0]) != inventory:
            raise AssetError(
                "Task assets changed while saving their checkpoint; retry after editing stops"
            )
        with _repository_lock(selection.repository_root, exclusive=True):
            _mark_clean(selection, commit)
        result = checkpoint.model_copy(update={"commit": commit})
        write_record(_metadata(workspace), result)
        return result


def check_assets(task: Path, checkpoint: AssetCheckpoint) -> None:
    selection = _selection(task)
    if (
        checkpoint.task_path != selection.tasks[0].source.task_relative_path
        or checkpoint.repo_id != f"agents-last-exam/{selection.repository_name}"
    ):
        raise AssetError("asset checkpoint belongs to a different Task")
    if checkpoint.commit is None and not any(
        item["type"] == "file" for item in _asset_inventory(selection.tasks[0])
    ):
        return
    observation = observe_task_assets(selection.tasks[0])
    if observation and (observation.dirty or observation.commit != checkpoint.commit):
        raise AssetError("Task assets differ from the accepted checkpoint")
    if (
        not observation
        and checkpoint.commit
        and _git(
            checkpoint.workspace,
            "ls-tree",
            "-r",
            "--name-only",
            checkpoint.commit,
            "--",
            *_paths(checkpoint),
        )
    ):
        raise AssetError("Task assets are missing from the working copy")


@contextmanager
def _materialized(checkpoint: AssetCheckpoint) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="ale-assets-") as directory:
        root = Path(directory) / "tree"
        if checkpoint.commit is None:
            root.mkdir()
            yield root
            return
        _git(checkpoint.workspace, "worktree", "add", "--detach", str(root), checkpoint.commit)
        try:
            paths = _paths(checkpoint)
            _git(root, "lfs", "checkout", *paths)
            include = ",".join(path + "/**" for path in paths)
            if any(
                line.split(maxsplit=2)[1] == "-"
                for line in _git(root, "lfs", "ls-files", f"--include={include}").splitlines()
            ):
                _git(root, "lfs", "pull", "origin", f"--include={include}")
            if any(
                line.split(maxsplit=2)[1] == "-"
                for line in _git(root, "lfs", "ls-files", f"--include={include}").splitlines()
            ):
                raise AssetError("asset checkpoint has missing LFS objects")
            yield root
        finally:
            _git(checkpoint.workspace, "worktree", "remove", "--force", str(root))


def materialize_assets(checkpoint: AssetCheckpoint, output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise AssetError("asset materialization output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    with _materialized(checkpoint) as root:
        validate_asset_paths(root / checkpoint.task_path)
        for stage in STAGES:
            source = root / checkpoint.task_path / stage / "assets"
            if source.exists():
                list(_regular_files(source))
                (output / stage).mkdir()
                shutil.copytree(source, output / stage / "assets")


def restore_assets(task: Path, checkpoint: AssetCheckpoint) -> None:
    selection = _selection(task)
    if (
        checkpoint.task_path != selection.tasks[0].source.task_relative_path
        or checkpoint.repo_id != f"agents-last-exam/{selection.repository_name}"
    ):
        raise AssetError("asset checkpoint belongs to a different Task")
    with (
        _materialized(checkpoint) as root,
        _repository_lock(selection.repository_root, exclusive=True),
    ):
        _replace_selected_assets(selection.tasks, root)
        _mark_clean(selection, checkpoint.commit or "")


def diff_assets(before: AssetCheckpoint, after: AssetCheckpoint) -> list[str]:
    if (before.repo_id, before.task_path) != (after.repo_id, after.task_path):
        raise AssetError("asset checkpoints refer to different Tasks")
    if before.commit == after.commit:
        return []
    checkpoint = after if after.commit else before
    if before.commit is None or after.commit is None:
        paths = _git(
            checkpoint.workspace,
            "ls-tree",
            "-rz",
            "--name-only",
            checkpoint.commit or "HEAD",
            "--",
            *_paths(checkpoint),
        )
    else:
        paths = _git(
            checkpoint.workspace,
            "diff",
            "--name-only",
            "-z",
            before.commit,
            after.commit,
            "--",
            *_paths(checkpoint),
        )
    return sorted(
        path.removeprefix(checkpoint.task_path + "/") for path in paths.split("\0") if path
    )


def publish_assets(
    checkpoint: AssetCheckpoint,
    *,
    title: str,
    description: str,
    output: Path,
    target_main: bool = False,
) -> dict[str, object]:
    from huggingface_hub import HfApi
    from huggingface_hub.community import DiscussionComment

    result: dict[str, object] = {
        "repo_id": checkpoint.repo_id,
        "commit": checkpoint.commit,
        "pr_number": None,
        "pr_url": None,
        "status": "no_assets",
    }
    if checkpoint.commit is None:
        write_record(output, result)
        return result
    base = checkpoint.base_commit
    if base and not _git(
        checkpoint.workspace,
        "diff",
        "--name-only",
        base,
        checkpoint.commit,
        "--",
        *_paths(checkpoint),
    ):
        result["status"] = "unchanged"
        write_record(output, result)
        return result
    api = HfApi()
    if not api.repo_info(checkpoint.repo_id, repo_type="dataset").private:
        raise AssetError(f"asset dataset must be private: {checkpoint.repo_id}")
    if target_main:
        reference = "refs/heads/main"
    else:
        marker = f"<!-- ale-assets:{checkpoint.task_path}:{checkpoint.commit} -->"
        if output.exists():
            saved = json.loads(output.read_text())
            if (
                saved.get("repo_id") != checkpoint.repo_id
                or saved.get("commit") != checkpoint.commit
            ):
                raise AssetError("publication record belongs to a different asset checkpoint")
            result.update(saved)
        if not result.get("pr_number"):
            existing = next(
                (
                    item
                    for item in api.get_repo_discussions(
                        checkpoint.repo_id, repo_type="dataset", discussion_type="pull_request"
                    )
                    if item.status == "draft"
                    and any(
                        isinstance(event, DiscussionComment) and marker in event.content
                        for event in api.get_discussion_details(
                            checkpoint.repo_id, item.num, repo_type="dataset"
                        ).events
                    )
                ),
                None,
            )
            pr = existing or api.create_pull_request(
                checkpoint.repo_id,
                title,
                repo_type="dataset",
                description=description + "\n\n" + marker,
            )
            result.update(
                pr_number=pr.num,
                pr_url=f"https://huggingface.co/datasets/{checkpoint.repo_id}/discussions/{pr.num}",
                status="pending",
            )
            remote = _git(checkpoint.workspace, "ls-remote", "origin", f"refs/pr/{pr.num}").split()
            result["expected_head"] = remote[0] if remote else ""
            write_record(output, result)
        reference = f"refs/pr/{result['pr_number']}"
    current = _git(checkpoint.workspace, "ls-remote", "origin", reference).split()
    if not current or current[0] != checkpoint.commit:
        arguments = (
            [] if target_main else [f"--force-with-lease={reference}:{result['expected_head']}"]
        )
        _git(checkpoint.workspace, "push", *arguments, "origin", f"{checkpoint.commit}:{reference}")
    published = _git(checkpoint.workspace, "ls-remote", "origin", reference).split()
    if not published or published[0] != checkpoint.commit:
        raise AssetError("HF did not retain the accepted asset commit")
    result["status"] = "published"
    write_record(output, result)
    return result


def comment_asset_pr(checkpoint: AssetCheckpoint, publication: Path, message: str) -> None:
    from huggingface_hub import HfApi
    from huggingface_hub.community import DiscussionComment

    result = json.loads(publication.read_text())
    if result.get("repo_id") != checkpoint.repo_id or result.get("commit") != checkpoint.commit:
        raise AssetError("publication record belongs to a different asset checkpoint")
    if not result.get("pr_number"):
        return
    api = HfApi()
    details = api.get_discussion_details(
        checkpoint.repo_id, result["pr_number"], repo_type="dataset"
    )
    comment = next(event for event in details.events if isinstance(event, DiscussionComment))
    if message.strip() not in comment.content:
        api.edit_discussion_comment(
            checkpoint.repo_id,
            result["pr_number"],
            comment.id,
            comment.content.rstrip() + "\n\n" + message.strip(),
            repo_type="dataset",
        )

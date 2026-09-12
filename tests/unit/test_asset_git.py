from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ale.core.errors import AssetError
from ale.run.asset_git import (
    AssetCheckpoint,
    check_assets,
    checkpoint_assets,
    comment_asset_pr,
    diff_assets,
    materialize_assets,
    publish_assets,
    restore_assets,
    write_record,
)
from ale.run.assets import asset_status, ensure_asset_repository, select_asset_repositories
from ale.run.cli.main import app

pytestmark = pytest.mark.unit


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def asset_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from ale.run import asset_git

    if shutil.which("git-lfs") is None:
        pytest.skip("git-lfs is required for the local asset Git contract test")
    seed = tmp_path / "hf-seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / ".gitattributes").write_text("# HF initial commit\n")
    git(seed, "add", ".gitattributes")
    git(
        seed, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Initial"
    )
    remote = tmp_path / "hf-assets.git"
    git(tmp_path, "clone", "--bare", str(seed), str(remote))
    original = asset_git._git

    def redirected(root: Path, *args: str) -> str:
        if args[0] == "clone":
            args = tuple(
                str(remote) if value.startswith("https://huggingface.co/") else value
                for value in args
            )
        return original(root, *args)

    monkeypatch.setattr(asset_git, "_git", redirected)
    monkeypatch.setattr(
        asset_git,
        "ensure_asset_repository",
        lambda name, collection: (f"agents-last-exam/{name}", "agents-last-exam/assets-123"),
    )
    return remote


def test_checkpoint_restore_and_isolated_task_worktrees(
    write_task_repo, asset_remote: Path, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo(
        "tasks-example", tasks=("one", "two"), stage_assets=("image", "verify")
    )
    one, two = (repository / f"tasks/{name}" for name in ("one", "two"))
    first = checkpoint_assets(one, workspace=tmp_path / "assets-one", branch="brew/one")
    check_assets(one, first)
    assert asset_status([one])[0].commit == first.commit
    assert git(asset_remote, "rev-parse", "main") == first.base_commit
    assert (
        checkpoint_assets(one, workspace=first.workspace, branch="brew/one").commit == first.commit
    )

    second = checkpoint_assets(two, workspace=tmp_path / "assets-two", branch="brew/two")
    assert first.workspace != second.workspace
    assert git(first.workspace, "rev-parse", "--git-common-dir") == git(
        second.workspace, "rev-parse", "--git-common-dir"
    )
    (one / "verify/assets/fixture.txt").write_text("changed verifier")
    with pytest.raises(AssetError, match="differ"):
        check_assets(one, first)
    changed = checkpoint_assets(one, workspace=first.workspace, branch="brew/one")
    assert diff_assets(first, changed) == ["verify/assets/fixture.txt"]
    assert (second.workspace / "tasks/two/verify/assets/fixture.txt").read_text() == "verify"
    assert "tasks/two" not in git(
        first.workspace, "ls-tree", "-r", "--name-only", changed.commit or "HEAD"
    )

    restore_assets(one, first)
    assert (one / "verify/assets/fixture.txt").read_text() == "verify"
    check_assets(one, first)
    destination = tmp_path / "materialized"
    materialize_assets(changed, destination)
    assert (destination / "verify/assets/fixture.txt").read_text() == "changed verifier"
    assert (destination / "image/assets/fixture.txt").read_text() == "image"
    pointer = git(first.workspace, "show", f"{first.commit}:tasks/one/image/assets/fixture.txt")
    assert pointer.startswith("version https://git-lfs.github.com/spec/v1")


@pytest.mark.parametrize("advance_main", [False, True])
def test_publish_keeps_exact_commit_and_resumes_same_draft(
    write_task_repo,
    asset_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    advance_main: bool,
) -> None:  # type: ignore[no-untyped-def]
    task = write_task_repo("tasks-example", stage_assets=("verify",)) / "tasks/demo"
    checkpoint = checkpoint_assets(task, workspace=tmp_path / "assets", branch="brew/demo")
    created: list[str] = []
    if advance_main:
        seed = tmp_path / "hf-seed"
        (seed / "other-task.txt").write_text("Another Job published while this one ran")
        git(seed, "add", ".")
        git(
            seed,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "Other Task",
        )
        git(seed, "push", str(asset_remote), "HEAD:main")
    main = git(asset_remote, "rev-parse", "main")

    class Api:
        def repo_info(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(private=True)

        def get_repo_discussions(self, *args: object, **kwargs: object) -> list[object]:
            return []

        def create_pull_request(self, repo: str, title: str, **kwargs: object) -> object:
            created.append(title)
            git(asset_remote, "update-ref", "refs/pr/7", main)
            return SimpleNamespace(num=7)

    monkeypatch.setattr("huggingface_hub.HfApi", Api)
    output = tmp_path / "publication.json"
    result = publish_assets(
        checkpoint, title="Build demo", description="Ready for review", output=output
    )
    assert result["status"] == "published"
    assert git(asset_remote, "rev-parse", "refs/pr/7") == checkpoint.commit
    assert git(asset_remote, "rev-parse", "main") == main
    assert publish_assets(checkpoint, title="Build demo", description="", output=output) == result
    assert created == ["Build demo"]
    assert json.loads(output.read_text())["pr_number"] == 7
    assert list((asset_remote / "lfs/objects").rglob("*"))


def test_empty_assets_cli_stays_offline_and_writes_null_commit(
    write_task_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = write_task_repo() / "tasks/demo"
    (task / "verify/assets").mkdir()
    monkeypatch.setattr(
        "ale.run.asset_git.ensure_asset_repository",
        lambda *_: pytest.fail("empty task contacted HF"),
    )
    record = tmp_path / "checkpoint.json"
    result = CliRunner().invoke(
        app,
        [
            "assets",
            "checkpoint",
            str(task),
            "--workspace",
            str(tmp_path / "assets"),
            "--branch",
            "brew/demo",
            "--output",
            str(record),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(record.read_text())["commit"] is None
    assert (
        CliRunner()
        .invoke(app, ["assets", "check", str(task), "--checkpoint", str(record)])
        .exit_code
        == 0
    )


def test_repository_name_uses_origin_and_main_worktree(write_task_repo, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("actual-tasks")
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Tasks",
    )
    linked = tmp_path / "job-uuid"
    git(repository, "worktree", "add", "-b", "brew/job", str(linked))
    assert select_asset_repositories([linked])[0].repository_name == "actual-tasks"
    git(repository, "remote", "add", "origin", "git@github.com:AgentsLastExam/canonical-tasks.git")
    assert select_asset_repositories([linked])[0].repository_name == "canonical-tasks"


def test_private_org_repository_and_collection_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class Api:
        def create_repo(self, repo: str, **kwargs: object) -> None:
            calls.append((repo, kwargs))

        def repo_info(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(private=True)

        def list_collections(self, **kwargs: object) -> list[object]:
            assert kwargs == {"owner": "agents-last-exam"}
            return [SimpleNamespace(title="assets", slug="agents-last-exam/assets-123")]

        def add_collection_item(self, *args: object, **kwargs: object) -> None:
            calls.append(("collection", args))

    monkeypatch.delenv("ALE_ASSETS_COLLECTION", raising=False)
    monkeypatch.setattr("huggingface_hub.HfApi", Api)
    assert ensure_asset_repository("tasks-example") == (
        "agents-last-exam/tasks-example",
        "agents-last-exam/assets-123",
    )
    assert calls[0] == (
        "agents-last-exam/tasks-example",
        {"repo_type": "dataset", "private": True, "exist_ok": True},
    )


def test_removed_assets_have_a_deletion_commit(
    write_task_repo, asset_remote: Path, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    task = write_task_repo(stage_assets=("verify",)) / "tasks/demo"
    first = checkpoint_assets(task, workspace=tmp_path / "assets", branch="brew/demo")
    shutil.rmtree(task / "verify/assets")
    removed = checkpoint_assets(task, workspace=first.workspace, branch="brew/demo")
    assert removed.commit != first.commit
    assert diff_assets(first, removed) == ["verify/assets/fixture.txt"]
    check_assets(task, removed)
    restore_assets(task, first)
    assert (task / "verify/assets/fixture.txt").read_text() == "verify"


def test_checkpoint_rejects_unsafe_task_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        AssetCheckpoint(repo_id="agents-last-exam/demo", task_path="../escape", workspace=tmp_path)


def test_partial_task_checkpoint_does_not_require_a_valid_manifest(
    write_task_repo,
    asset_remote: Path,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo(stage_assets=("verify",))
    task = repository / "tasks/demo"
    (task / "task.yaml").write_text("invalid: [")
    checkpoint = checkpoint_assets(task, workspace=tmp_path / "assets", branch="brew/demo")
    check_assets(task, checkpoint)
    executable = task / "verify/assets/fixture.txt"
    executable.chmod(0o755)
    with pytest.raises(AssetError, match="differ"):
        check_assets(task, checkpoint)
    executable_checkpoint = checkpoint_assets(
        task, workspace=checkpoint.workspace, branch="brew/demo"
    )
    assert executable_checkpoint.commit != checkpoint.commit
    assert diff_assets(checkpoint, executable_checkpoint) == ["verify/assets/fixture.txt"]
    restore_assets(task, checkpoint)
    assert not executable.stat().st_mode & 0o111
    missing = checkpoint_assets(
        repository / "tasks/not-created",
        workspace=tmp_path / "missing-assets",
        branch="brew/missing",
    )
    assert missing.commit is None
    check_assets(repository / "tasks/not-created", missing)


def test_checkpoint_rejects_symlinked_stages_before_any_upload(
    write_task_repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    task = write_task_repo() / "tasks/demo"
    outside = tmp_path / "private-data"
    (outside / "assets").mkdir(parents=True)
    (outside / "assets/secret.txt").write_text("must remain outside")
    shutil.rmtree(task / "image")
    (task / "image").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        "ale.run.asset_git.ensure_asset_repository",
        lambda *_: pytest.fail("unsafe Task contacted HF"),
    )
    with pytest.raises(AssetError, match="real directory"):
        checkpoint_assets(task, workspace=tmp_path / "assets", branch="brew/demo")
    assert (outside / "assets/secret.txt").read_text() == "must remain outside"


def test_publication_retry_does_not_overwrite_an_updated_pr(
    write_task_repo,
    asset_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    from ale.run import asset_git

    task = write_task_repo(stage_assets=("verify",)) / "tasks/demo"
    checkpoint = checkpoint_assets(task, workspace=tmp_path / "assets", branch="brew/demo")
    original = asset_git._git

    class Api:
        def repo_info(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(private=True)

        def get_repo_discussions(self, *args: object, **kwargs: object) -> list[object]:
            return []

        def create_pull_request(self, *args: object, **kwargs: object) -> object:
            git(asset_remote, "update-ref", "refs/pr/8", checkpoint.base_commit or "main")
            return SimpleNamespace(num=8)

    def fail_push(root: Path, *args: str) -> str:
        if args[0] == "push":
            raise AssetError("network interrupted")
        return original(root, *args)

    monkeypatch.setattr("huggingface_hub.HfApi", Api)
    monkeypatch.setattr(asset_git, "_git", fail_push)
    output = tmp_path / "publication.json"
    with pytest.raises(AssetError, match="network interrupted"):
        publish_assets(checkpoint, title="Build demo", description="", output=output)
    assert json.loads(output.read_text())["status"] == "pending"
    seed = tmp_path / "hf-seed"
    (seed / "human-edit.txt").write_text("An administrator changed this PR")
    git(seed, "add", ".")
    git(
        seed,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Review edit",
    )
    git(seed, "push", str(asset_remote), "HEAD:refs/pr/8")
    human_commit = git(asset_remote, "rev-parse", "refs/pr/8")
    monkeypatch.setattr(asset_git, "_git", original)
    with pytest.raises(AssetError, match="stale info"):
        publish_assets(checkpoint, title="Build demo", description="", output=output)
    assert git(asset_remote, "rev-parse", "refs/pr/8") == human_commit


def test_pr_cross_link_preserves_body_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from huggingface_hub.community import DiscussionComment

    checkpoint = AssetCheckpoint(
        repo_id="agents-last-exam/tasks-example",
        task_path="tasks/demo",
        workspace=tmp_path,
        commit="a" * 40,
    )
    publication = tmp_path / "publication.json"
    write_record(
        publication, {"repo_id": checkpoint.repo_id, "commit": checkpoint.commit, "pr_number": 9}
    )
    comment = DiscussionComment(
        id="body",
        type="comment",
        created_at=datetime.now(UTC),
        author="builder",
        _event={},
        content="Task build evidence",
        edited=False,
        hidden=False,
    )
    updates: list[str] = []

    class Api:
        def get_discussion_details(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(events=[comment])

        def edit_discussion_comment(
            self, repo: str, number: int, comment_id: str, body: str, **kwargs: object
        ) -> None:
            updates.append(body)
            comment.content = body

    monkeypatch.setattr("huggingface_hub.HfApi", Api)
    message = "Source PR: https://github.com/AgentsLastExam/tasks-example/pull/1"
    comment_asset_pr(checkpoint, publication, message)
    comment_asset_pr(checkpoint, publication, message)
    assert updates == ["Task build evidence\n\n" + message]

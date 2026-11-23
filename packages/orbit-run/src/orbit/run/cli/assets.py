"""Asset checkpoint commands for build orchestration."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from ale.run.asset_git import (
    check_assets,
    checkpoint_assets,
    comment_asset_pr,
    diff_assets,
    materialize_assets,
    publish_assets,
    read_checkpoint,
    restore_assets,
    write_record,
)

assets_app = typer.Typer(help="Manage and synchronize Task assets.", no_args_is_help=True)
TaskPath = Annotated[Path, typer.Argument(help="One Task folder")]
CheckpointPath = Annotated[Path, typer.Option("--checkpoint", help="Saved asset checkpoint JSON")]
OutputPath = Annotated[Path, typer.Option("--output", help="Output record or directory")]


@contextmanager
def _report_errors() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@assets_app.command("checkpoint")
def checkpoint(
    task: TaskPath,
    workspace: Annotated[Path, typer.Option("--workspace", help="This Job's asset worktree")],
    branch: Annotated[str, typer.Option("--branch", help="This Job's local asset branch")],
    output: OutputPath,
) -> None:
    """Commit assets locally; create the private empty HF repository when needed."""
    with _report_errors():
        result = checkpoint_assets(task, workspace=workspace, branch=branch)
        write_record(output, result)
        typer.echo(result.model_dump_json())


@assets_app.command("check")
def check(task: TaskPath, checkpoint: CheckpointPath) -> None:
    """Require clean assets at the accepted commit, without hashing file contents."""
    with _report_errors():
        check_assets(task, read_checkpoint(checkpoint))


@assets_app.command("restore")
def restore(task: TaskPath, checkpoint: CheckpointPath) -> None:
    """Restore exactly the saved asset commit into the Task."""
    with _report_errors():
        restore_assets(task, read_checkpoint(checkpoint))


@assets_app.command("diff")
def diff(
    before: Annotated[Path, typer.Option("--before", help="Earlier checkpoint JSON")],
    after: Annotated[Path, typer.Option("--after", help="Later checkpoint JSON")],
) -> None:
    """Print changed Task-relative asset paths as JSON."""
    with _report_errors():
        typer.echo(json.dumps(diff_assets(read_checkpoint(before), read_checkpoint(after))))


@assets_app.command("materialize")
def materialize(checkpoint: CheckpointPath, output: OutputPath) -> None:
    """Copy assets from the saved commit to an empty Task-relative directory."""
    with _report_errors():
        materialize_assets(read_checkpoint(checkpoint), output)


@assets_app.command("publish")
def publish(
    checkpoint: CheckpointPath,
    output: OutputPath,
    title: Annotated[str, typer.Option("--title", help="HF draft PR title")],
    description: Annotated[Path, typer.Option("--description", help="PR body text file")],
) -> None:
    """Push the accepted Git/LFS commit to a draft HF PR targeting main."""
    with _report_errors():
        result = publish_assets(
            read_checkpoint(checkpoint),
            title=title,
            description=description.read_text(),
            output=output,
        )
        typer.echo(json.dumps(result))


@assets_app.command("comment-pr")
def comment_pr(
    checkpoint: CheckpointPath,
    publication: Annotated[Path, typer.Option("--publication", help="Saved HF publication JSON")],
    message_file: Annotated[
        Path, typer.Option("--message-file", help="Text to append to the PR body")
    ],
) -> None:
    """Append a source PR link to the asset PR body once."""
    with _report_errors():
        comment_asset_pr(read_checkpoint(checkpoint), publication, message_file.read_text())

"""A missing selected root must reach verification as missing."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.errors import ArtifactTransferError
from ale.core.sandbox import ExecResult, OperatingSystem
from ale.run.episode import _Artifacts


class LocalSandbox:
    request = SimpleNamespace(os=OperatingSystem.LINUX)

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        assert process.returncode is not None
        return ExecResult(
            exit_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode()
        )

    async def download_dir(self, source: str, destination: str) -> None:
        shutil.copytree(source, destination, dirs_exist_ok=True)

    async def upload_dir(self, source: str, destination: str) -> None:
        shutil.copytree(source, destination, dirs_exist_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection", [{"includes": ("output/result.txt",)}, {"include_directories": ("input",)}]
)
async def test_missing_selected_root_is_removed_from_verifier(
    tmp_path: Path, selection: dict[str, tuple[str, ...]]
) -> None:
    source = tmp_path / "solver/base"
    sink = _Artifacts(tmp_path / "run")
    sandbox = LocalSandbox()
    await sink.collect(sandbox, str(source), "base", **selection)  # type: ignore[arg-type]

    entry = json.loads((sink.root / "snapshot.json").read_text())["entries"][0]
    assert entry["kind"] == "missing"
    assert entry["content_identity"] is None
    assert not (sink.root / entry["stored_path"]).exists()

    # Simulate the verifier image containing stale files at the original path.
    source.mkdir(parents=True)
    (source / "stale").write_text("must not reach verification")
    await sink.restore(sandbox)  # type: ignore[arg-type]
    assert not source.exists()


@pytest.mark.asyncio
async def test_missing_root_remains_an_error_without_selection(tmp_path: Path) -> None:
    with pytest.raises(ArtifactTransferError, match="declared artifact is missing"):
        await _Artifacts(tmp_path).collect(
            LocalSandbox(),
            str(tmp_path / "missing"),
            "missing",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_missing_roots_with_the_same_name_have_distinct_entries(tmp_path: Path) -> None:
    sink = _Artifacts(tmp_path / "run")
    sandbox = LocalSandbox()
    for parent in ("one", "two"):
        await sink.collect(sandbox, str(tmp_path / parent / "base"), "base", includes=("result",))  # type: ignore[arg-type]
    entries = json.loads((sink.root / "snapshot.json").read_text())["entries"]
    assert [entry["stored_path"] for entry in entries] == ["base", "0001-base"]


@pytest.mark.asyncio
async def test_inspection_failure_is_not_treated_as_a_missing_root(tmp_path: Path) -> None:
    class FailedInspection(LocalSandbox):
        async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            return ExecResult(exit_code=1, stderr="permission denied")

    with pytest.raises(ArtifactTransferError):
        await _Artifacts(tmp_path).collect(
            FailedInspection(),
            "/unreadable",
            "output",
            includes=("result",),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_failed_removal_is_an_artifact_error(tmp_path: Path) -> None:
    class FailedRemoval(LocalSandbox):
        async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            if argv[0] == "rm":
                return ExecResult(exit_code=1, stderr="permission denied")
            return await super().exec(argv, **kwargs)

    sandbox = FailedRemoval()
    sink = _Artifacts(tmp_path)
    await sink.collect(
        sandbox,
        str(tmp_path / "missing"),
        "missing",
        includes=("result",),  # type: ignore[arg-type]
    )
    with pytest.raises(ArtifactTransferError, match="could not restore missing artifact"):
        await sink.restore(sandbox)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("symlink", [False, True])
async def test_selected_file_directory_is_restored_without_its_contents(
    tmp_path: Path, symlink: bool
) -> None:
    sandbox = LocalSandbox()
    root = tmp_path / "solver/base"
    selected = root / "input/expected.txt"
    selected.parent.mkdir(parents=True)
    directory = tmp_path / "elsewhere" if symlink else selected
    directory.mkdir()
    (directory / "unselected.txt").write_text("do not collect")
    (directory / "broken-link").symlink_to("missing")
    if symlink:
        selected.symlink_to(directory, target_is_directory=True)
    (root / "other.txt").write_text("unselected")
    (root / "result.txt").write_text("selected")
    sink = _Artifacts(tmp_path / "run")
    await sink.collect(
        sandbox,
        str(root),
        "base",
        includes=("input/expected.txt", "result.txt"),
        preserve_file_directories=True,
    )  # type: ignore[arg-type]
    stored = sink.root / "base"
    assert (stored / "input/expected.txt").is_dir()
    assert not list((stored / "input/expected.txt").iterdir())
    assert (stored / "result.txt").read_text() == "selected"
    assert not (stored / "other.txt").exists()
    shutil.rmtree(root)
    await sink.restore(sandbox)  # type: ignore[arg-type]
    assert selected.is_dir() and not selected.is_symlink()
    assert not list(selected.iterdir())


@pytest.mark.asyncio
async def test_empty_selected_directory_changes_snapshot_identity(tmp_path: Path) -> None:
    sandbox = LocalSandbox()
    root = tmp_path / "base"
    root.mkdir()
    missing = _Artifacts(tmp_path / "missing")
    await missing.collect(sandbox, str(root), "base", includes=("result",))  # type: ignore[arg-type]
    (root / "result").mkdir()
    present = _Artifacts(tmp_path / "present")
    await present.collect(
        sandbox, str(root), "base", includes=("result",), preserve_file_directories=True
    )  # type: ignore[arg-type]
    assert missing.entries[0]["content_identity"] != present.entries[0]["content_identity"]


@pytest.mark.asyncio
async def test_selected_file_directory_is_omitted_by_default(tmp_path: Path) -> None:
    root = tmp_path / "base"
    (root / "result").mkdir(parents=True)
    sink = _Artifacts(tmp_path / "run")
    await sink.collect(LocalSandbox(), str(root), "base", includes=("result",))  # type: ignore[arg-type]
    assert not (sink.root / "base/result").exists()

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


@pytest.mark.asyncio
@pytest.mark.parametrize("link", [False, True])
async def test_unreadable_selected_file_can_be_omitted_for_separate_verification(
    tmp_path: Path, link: bool
) -> None:
    root = tmp_path / "base"
    root.mkdir()
    secret = tmp_path / "unreadable" if link else root / "private"
    secret.write_text("not captured")
    secret.chmod(0)
    selected = root / "private"
    if link:
        selected.symlink_to(secret)
    (root / "public").write_text("captured")
    try:
        try:
            secret.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("the test account can read a mode-000 file")

        sandbox = LocalSandbox()
        with pytest.raises(ArtifactTransferError, match="cannot stage artifact"):
            await _Artifacts(tmp_path / "strict").collect(
                sandbox, str(root), "base", includes=("private", "public")
            )  # type: ignore[arg-type]

        sink = _Artifacts(tmp_path / "omitted")
        await sink.collect(
            sandbox,
            str(root),
            "base",
            includes=("private", "public"),
            omit_unreadable_files=True,
        )  # type: ignore[arg-type]
        entry = json.loads((sink.root / "snapshot.json").read_text())["entries"][0]
        assert entry["omit_unreadable_files"] is True
        assert entry["unreadable_files"] == ["private"]
        assert not (sink.root / "base/private").exists()
        assert (sink.root / "base/public").read_text() == "captured"
        # A separate verifier must not retain a stale copy from its image.
        secret.chmod(0o600)
        shutil.rmtree(root)
        root.mkdir()
        (root / "private").write_text("stale")
        await sink.restore(sandbox)  # type: ignore[arg-type]
        assert not (root / "private").exists()
        assert (root / "public").read_text() == "captured"
    finally:
        if secret.exists():
            secret.chmod(0o600)


@pytest.mark.asyncio
async def test_omitting_an_unreadable_selected_file_does_not_hide_directory_failures(
    tmp_path: Path,
) -> None:
    root = tmp_path / "base"
    selected = root / "input/private"
    selected.parent.mkdir(parents=True)
    selected.write_text("unreadable")
    selected.chmod(0)
    try:
        try:
            selected.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("the test account can read a mode-000 file")
        with pytest.raises(ArtifactTransferError, match="cannot stage artifact"):
            await _Artifacts(tmp_path / "run").collect(
                LocalSandbox(),
                str(root),
                "base",
                includes=("other",),
                include_directories=("input",),
                omit_unreadable_files=True,
            )  # type: ignore[arg-type]
    finally:
        selected.chmod(0o600)


@pytest.mark.asyncio
async def test_selected_file_beneath_unsearchable_directory_is_omitted(tmp_path: Path) -> None:
    root = tmp_path / "base"
    directory = root / "locked"
    directory.mkdir(parents=True)
    selected = directory / "result"
    selected.write_text("hidden")
    directory.chmod(0)
    try:
        try:
            selected.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("the test account can search a mode-000 directory")
        sink = _Artifacts(tmp_path / "run")
        await sink.collect(
            LocalSandbox(),
            str(root),
            "base",
            includes=("locked/result",),
            omit_unreadable_files=True,
        )  # type: ignore[arg-type]
        assert sink.entries[0]["unreadable_files"] == ["locked/result"]
        assert not (sink.root / "base/locked/result").exists()
    finally:
        directory.chmod(0o700)


@pytest.mark.asyncio
async def test_omitting_unreadable_files_does_not_hide_staging_errors(tmp_path: Path) -> None:
    class FailedStage(LocalSandbox):
        async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            if argv[0] == "python3" and "ale-artifact-" in argv[2]:
                return ExecResult(exit_code=1, stderr="simulated I/O error")
            return await super().exec(argv, **kwargs)

    root = tmp_path / "base"
    root.mkdir()
    with pytest.raises(ArtifactTransferError, match="simulated I/O error"):
        await _Artifacts(tmp_path / "run").collect(
            FailedStage(),
            str(root),
            "base",
            includes=("result",),
            omit_unreadable_files=True,
        )  # type: ignore[arg-type]

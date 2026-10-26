"""The copy contract, using real temporary files and subprocesses without a provider."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.sandbox import ExecResult, Identity
from ale.core.taskspec import OperatingSystem, TaskManifestV1
from ale.run.artifact_files import inspect, read
from ale.run.episode import _Artifacts

pytestmark = pytest.mark.unit


class LocalSandbox:
    request = SimpleNamespace(os=OperatingSystem.LINUX)

    async def exec(self, argv, *, identity=Identity.FRAMEWORK, output_sink=None, **kwargs):
        assert identity is Identity.FRAMEWORK
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if output_sink is not None:
            await output_sink("stdout", stdout)
            await output_sink("stderr", stderr)
        return ExecResult(
            exit_code=process.returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(),
        )

    async def write_file(self, path, data, *, identity=Identity.FRAMEWORK):
        assert identity is Identity.FRAMEWORK
        Path(path).write_bytes(data)


def test_a_link_above_the_artifact_is_not_followed_either(tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "result.txt").write_text("secret")
    (tmp_path / "output").symlink_to(protected, target_is_directory=True)
    for pattern in ("output/result.txt", "output/*.txt"):
        result = inspect(str(tmp_path / pattern))
        assert result == {"entries": [], "skipped": [f"{tmp_path / 'output'}: link"]}
    with pytest.raises(ValueError, match="is a link"):
        read(str(tmp_path / "output" / "result.txt"))


def test_globs_directories_and_skips_links(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "file.sgf").write_text("game")
    (output / "directory.sgf").mkdir()
    (output / "directory.sgf" / "inside").write_text("inside")
    (output / "file-link.sgf").symlink_to("file.sgf")
    (output / "dir-link.sgf").symlink_to("directory.sgf")
    (output / "directory.sgf" / "cycle").symlink_to(output, target_is_directory=True)
    (output / "escape.sgf").symlink_to("/etc/hostname")
    (output / "broken.sgf").symlink_to("missing")
    os.mkfifo(output / "pipe.sgf")
    result = inspect(str(output / "*.sgf"))
    entries = {
        Path(entry["source"]).relative_to(output).as_posix(): entry["kind"]
        for entry in result["entries"]
    }
    assert entries == {
        "file.sgf": "file",
        "directory.sgf": "directory",
        "directory.sgf/inside": "file",
    }
    skipped = "\n".join(result["skipped"])
    for name in ("file-link.sgf", "dir-link.sgf", "cycle", "escape.sgf", "broken.sgf"):
        assert f"{name}: link" in skipped
    assert "pipe.sgf: special file" in skipped


@pytest.mark.asyncio
@pytest.mark.parametrize("operating_system", [OperatingSystem.LINUX, OperatingSystem.MACOS])
async def test_copy_restore_skips_missing_and_links_and_preserves_bytes_types_and_modes(
    tmp_path, operating_system
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "file.txt").write_bytes(b"data\x00\xff")
    (output / "file.txt").chmod(0o640)
    (output / "expected-file.json").mkdir()
    (output / "empty").mkdir()
    (output / "link.txt").symlink_to("file.txt")
    (output / "loop").symlink_to(output, target_is_directory=True)
    sandbox = LocalSandbox()
    sandbox.request = SimpleNamespace(os=operating_system)
    sink = _Artifacts(tmp_path / "run")
    await sink.collect(sandbox, str(tmp_path / "missing"), "missing")
    await sink.collect(sandbox, str(output), "output")
    shutil.rmtree(output)
    output.mkdir()
    (output / "stale.txt").write_text("stale")
    await sink.restore(sandbox)
    assert (output / "file.txt").read_bytes() == b"data\x00\xff"
    assert not (output / "link.txt").exists()
    assert (output / "file.txt").stat().st_mode & 0o777 == 0o640
    assert (output / "expected-file.json").is_dir()
    assert (output / "empty").is_dir()
    assert not (output / "loop").exists()
    assert not (output / "stale.txt").exists()
    assert json.loads((sink.root / "snapshot.json").read_text())["entries"]


@pytest.mark.asyncio
async def test_copy_failure_skips_one_file_and_still_restores_other_files(tmp_path, caplog):
    output = tmp_path / "output"
    output.mkdir()
    (output / "bad").write_text("bad")
    (output / "good").write_text("good")

    class FailingSandbox(LocalSandbox):
        async def exec(self, argv, **kwargs):
            if argv[-2:] == ["read", str(output / "bad")]:
                return ExecResult(exit_code=1, stderr="injected read failure")
            return await super().exec(argv, **kwargs)

    sink = _Artifacts(tmp_path / "run")
    await sink.collect(FailingSandbox(), str(output), "output")
    shutil.rmtree(output)
    await sink.restore(LocalSandbox())
    assert not (output / "bad").exists()
    assert (output / "good").read_text() == "good"
    assert "injected read failure" in caplog.text


@pytest.mark.asyncio
async def test_collection_none_never_inspects_guest(tmp_path):
    class NoCalls:
        async def exec(self, *args, **kwargs):
            raise AssertionError("disabled collection touched the guest")

    sink = _Artifacts(tmp_path, enabled=False)
    await sink.collect(NoCalls(), "/anything", "anything")
    await sink.restore(NoCalls())
    assert not sink.root.exists()


def test_artifacts_accept_file_directory_and_overlapping_glob_paths():
    manifest = TaskManifestV1(
        spec_type="core/v1",
        name="copy",
        image={"kind": "container"},
        artifacts=("/output", "/output/*.sgf", "/output/exact.sgf"),
    )
    assert len(manifest.artifacts) == 3


@pytest.mark.asyncio
async def test_overlapping_globs_are_restored_once_even_when_parent_is_declared_last(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "game.sgf").write_text("game")
    sandbox = LocalSandbox()
    sink = _Artifacts(tmp_path / "run")
    await sink.collect(sandbox, str(output / "*.sgf"), "*.sgf")
    await sink.collect(sandbox, str(output), "output")
    shutil.rmtree(output)
    await sink.restore(sandbox)
    assert (output / "game.sgf").read_text() == "game"
    assert len([entry for entry in sink.entries if entry["kind"] == "file"]) == 1


@pytest.mark.asyncio
async def test_restore_failure_does_not_prevent_other_outputs(tmp_path, caplog):
    output = tmp_path / "output"
    output.mkdir()
    for name in ("bad", "good"):
        (output / name).write_text(name)
    sink = _Artifacts(tmp_path / "run")
    await sink.collect(LocalSandbox(), str(output), "output")
    shutil.rmtree(output)

    class FailingWrite(LocalSandbox):
        async def write_file(self, path, data, **kwargs):
            if Path(path).name == "bad":
                raise OSError("injected write failure")
            await super().write_file(path, data, **kwargs)

    await sink.restore(FailingWrite())
    assert (output / "good").read_text() == "good"
    assert not (output / "bad").exists()
    assert "injected write failure" in caplog.text


def test_recursive_pattern_and_hidden_directory_contents(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/game.sgf").write_text("game")
    (tmp_path / "nested/.notes").write_text("notes")
    matches = inspect(str(tmp_path / "**/*.sgf"))
    assert [entry["source"] for entry in matches["entries"]] == [str(tmp_path / "nested/game.sgf")]
    directory = inspect(str(tmp_path / "nest*"))
    assert str(tmp_path / "nested/.notes") in [entry["source"] for entry in directory["entries"]]

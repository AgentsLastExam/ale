"""A Windows solver's work, verified by a separate Linux verifier through the Host."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support import MemoryProvider, MemorySandbox
from tests.unit.test_artifact_copy import LocalSandbox, WindowsGuest
from tests.unit.test_verifier_os import _windows_task

from ale.core.config import VerificationConfig
from ale.core.sandbox import PreparedTaskImage, Resources, SandboxRequest
from ale.core.task import ResolvedVariables
from ale.core.taskspec import Artifact, ImageKind, NetworkPolicy, OperatingSystem
from ale.run.episode import _Artifacts
from ale.run.harnesses.builtin import NopHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.recording import EpisodeRecording
from ale.run.tasksets.manifest import load_task_folder

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_windows_solvers_relative_artifact_reaches_a_linux_verifiers_start(
    tmp_path: Path,
) -> None:
    output = r"C:\Users\lane-a\Documents\output"
    solver = WindowsGuest(
        {
            output: [
                {"source": output, "kind": "directory", "mode": 0o777},
                {"source": output + r"\model.3dm", "kind": "file", "mode": 0o666, "bytes": b"3dm"},
            ]
        }
    )
    sink = _Artifacts(tmp_path / "run")
    await sink.collect(solver, Artifact(source="output"), r"C:\Users\lane-a\Documents")

    home = tmp_path / "verifier"
    await sink.restore(LocalSandbox(), str(home / "Documents"))

    assert (home / "Documents" / "output" / "model.3dm").read_bytes() == b"3dm"
    # Below its start directory as directories of its own system, not one name with
    # backslashes in it.
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == [
        "Documents",
        "Documents/output",
        "Documents/output/model.3dm",
    ]


class LinuxVerifier(MemorySandbox):
    """A Linux container holding a verifier's result, recording every command it runs."""

    def __init__(self) -> None:
        digest = "sha256:" + "0" * 64
        request = SandboxRequest(
            episode_id="episode",
            role="verifier",
            prepared_image=PreparedTaskImage(
                kind=ImageKind.CONTAINER,
                source="verifier-local",
                input_identity=digest,
                image_source_identity=digest,
                runtime_ref="ale-verifier:local",
                prepared_identity=digest,
            ),
            resources=Resources(),
            network=NetworkPolicy(),
        )
        super().__init__(MemoryProvider(), request)
        self.commands: list[tuple[list[str], str | None]] = []
        self.files["/logs/verifier/reward.json"] = b'{"reward": 1.0}'

    async def exec(self, argv, *, cwd=None, **kwargs):  # type: ignore[no-untyped-def]
        self.commands.append((list(argv), cwd))
        return await super().exec(argv, cwd=cwd, **kwargs)

    async def upload_dir(self, source, target, *, identity=None):  # type: ignore[no-untyped-def]
        self.uploads[target] = {path.name: "" for path in Path(source).iterdir()}


@pytest.mark.asyncio
async def test_a_linux_verifier_of_a_windows_task_runs_its_own_systems_entry(
    tmp_path: Path,
) -> None:
    task = load_task_folder(_windows_task(tmp_path / "task"))
    assert (task.spec.os, task.spec.verifier_os) == (
        OperatingSystem.WINDOWS,
        OperatingSystem.LINUX,
    )
    recording = EpisodeRecording(tmp_path / "run")
    ctx = SimpleNamespace(
        execution=recording.execution,
        blobs=recording.blobs,
        episode_id="episode",
        spec=task.spec,
        variables=ResolvedVariables(),
        secrets=(),
        verification_config=VerificationConfig(),
        judge_tokens={},
        verification_command_env={},
        verification_secrets=(),
        phase_timeout_sec=10.0,
        phase_deadline=None,
        run_dir=tmp_path / "run",
        phases=[],
    )
    verifier = LinuxVerifier()

    rewards = await EpisodeProtocol(NopHarness())._verify(task, ctx, verifier)  # type: ignore[arg-type]

    assert rewards == {"reward": 1.0}
    assert "test.sh" in verifier.uploads["/opt/ale/tests"]
    assert (["bash", "test.sh"], "/opt/ale/tests") in verifier.commands
    # Its verifier directory is Linux's, made ready before the entry runs.
    assert any(argv[-2:] == ["/logs", "/logs/verifier"] for argv, _ in verifier.commands)
    assert not [argv for argv, _ in verifier.commands if argv[0] == "powershell.exe"]

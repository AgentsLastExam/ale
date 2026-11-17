"""Every stage path below the Sandbox's own stage root, whatever root its Provider gives it."""

from __future__ import annotations

import re
import shutil
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support import MemoryProvider, MemorySandbox

from ale.core.episode import Phase
from ale.core.sandbox import PreparedTaskImage, Resources, SandboxRequest
from ale.core.task import ResolvedVariables, Stage
from ale.core.taskspec import ImageKind, NetworkPolicy, OperatingSystem
from ale.run.harnesses.builtin import NopHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol, _write_agent_episode
from ale.run.recording import EpisodeRecording
from ale.run.tasksets.manifest import load_task_folder

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/tasks/station-notepad"

#: A Lane's own root, as a Station gives it, below the machine's shared one.
LANE_ROOT = r"C:\ProgramData\ALE\lanes\laneB"

#: Any path below the machine's framework directory in what the protocol sent.
_FRAMEWORK_PATH = re.compile(r"C:\\ProgramData\\ALE[^'\"\s;]*", re.IGNORECASE)


class LaneSandbox(MemorySandbox):
    """A Windows Sandbox whose stage root is a Lane's, recording every command it runs."""

    stage_root = LANE_ROOT  # type: ignore[assignment]

    def __init__(self, provider: MemoryProvider, request: SandboxRequest) -> None:
        super().__init__(provider, request)
        self.commands: list[dict[str, Any]] = []

    async def exec(self, argv, *, cwd=None, env=None, **kwargs):  # type: ignore[no-untyped-def]
        self.commands.append({"argv": list(argv), "cwd": cwd, "env": dict(env or {})})
        return await super().exec(argv, cwd=cwd, env=env, **kwargs)


def _sandbox() -> LaneSandbox:
    digest = "sha256:" + "0" * 64
    request = SandboxRequest(
        episode_id="episode",
        os=OperatingSystem.WINDOWS,
        prepared_image=PreparedTaskImage(
            kind=ImageKind.STATION,
            source="external-ref",
            input_identity=digest,
            runtime_ref="notepad",
            prepared_identity=digest,
            resolved_reference="station:ws",
        ),
        resources=Resources(),
        network=NetworkPolicy(),
    )
    return LaneSandbox(MemoryProvider("station"), request)


def _context(tmp_path: Path, spec: Any) -> SimpleNamespace:
    recording = EpisodeRecording(tmp_path / "run")
    return SimpleNamespace(
        execution=recording.execution,
        blobs=recording.blobs,
        episode_id="episode",
        spec=spec,
        variables=ResolvedVariables(),
        secrets=(),
        verification_command_env={},
        phase_timeout_sec=10.0,
        phase_deadline=None,
        run_dir=tmp_path / "run",
        phases=[],
    )


def _framework_paths(sandbox: LaneSandbox) -> set[str]:
    sent = [*sandbox.uploads, *sandbox.files]
    for command in sandbox.commands:
        sent += [*command["argv"], command["cwd"] or "", *command["env"].values()]
    return {match for text in sent for match in _FRAMEWORK_PATH.findall(text)}


@pytest.mark.asyncio
async def test_every_stage_path_is_below_the_sandboxs_own_stage_root(tmp_path: Path) -> None:
    folder = shutil.copytree(FIXTURE, tmp_path / "task")
    (folder / "environment/setup").mkdir(parents=True)
    (folder / "environment/setup/setup.ps1").write_text("exit 0\n")
    task = load_task_folder(folder)
    ctx = _context(tmp_path, task.spec)
    sandbox = _sandbox()
    protocol = EpisodeProtocol(NopHarness())

    await protocol._setup(task, ctx, sandbox)  # type: ignore[arg-type]
    await protocol._run_stage(
        ctx,  # type: ignore[arg-type]
        sandbox,
        PureWindowsPath(LANE_ROOT, Stage.TESTS),
        Phase.VERIFY,
    )
    assert (await protocol._clear_stages(sandbox, (Stage.SETUP, Stage.TESTS))).ok
    await _write_agent_episode(sandbox, "episode")

    root = PureWindowsPath(LANE_ROOT)
    paths = {PureWindowsPath(path) for path in _framework_paths(sandbox)}
    assert {
        root / "setup",
        root / "tests",
        root / "tests" / "config.json",
        root / "agent-episode",
    } <= paths
    assert all(path == root or root in path.parents for path in paths), sorted(map(str, paths))

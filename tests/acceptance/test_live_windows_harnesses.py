from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ale.core.config import LoggingPolicy, load_run_config
from ale.core.harness import AgentRun, AutonomousHarness, HarnessSession
from ale.core.sandbox import Sandbox
from ale.core.taskspec import ImageKind, OperatingSystem
from ale.core.verdict import Status
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import Limits
from ale.run.providers.qemu import QemuProvider
from ale.run.secrets import provider_credentials
from ale.run.tasksets.manifest import load_task_folder
from tests.support import provider_registry

from .test_cross_os_standard_task import _write_task
from .trajectory import LIVE

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_kvm, pytest.mark.needs_llm, LIVE]


@pytest.mark.parametrize("name", ["claude-code", "codex-cli", "grok-build", "openclaw-cli"])
async def test_windows_native_harness_completes_a_file_task(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to a prepared Windows base qcow2")
    config = load_run_config(preset_path=PRESET_DIR / f"{name}.toml")
    harness = _harness(config)
    if name == "claude-code":
        launch = harness.launch

        async def launch_and_resume(
            self: AutonomousHarness,
            instruction: str,
            sandbox: Sandbox,
            session: HarnessSession,
            *,
            timeout_sec: float,
        ) -> AgentRun:
            first = await launch(instruction, sandbox, session, timeout_sec=timeout_sec)
            assert first.continuation is not None
            resumed = await self.resume(
                "Confirm the file task is complete. Do not modify any files.",
                first.continuation,
                sandbox,
                session,
                timeout_sec=timeout_sec,
            )
            assert resumed.continuation == first.continuation
            return resumed

        monkeypatch.setattr(type(harness), "launch", launch_and_resume)
    model = os.environ.get("ALE_LIVE_WINDOWS_MODEL", config.agent.model)
    api_key, upstream = provider_credentials(
        os.environ.get("ALE_LIVE_WINDOWS_API_KEY_ENV", config.gateway.api_key_env),
        os.environ.get("ALE_LIVE_WINDOWS_BASE_URL", config.gateway.base_url),
        config.gateway.dialect,
    )
    task_root = _write_task(tmp_path / "tasks", OperatingSystem.WINDOWS)
    (task_root / "instruction.md").write_text(
        "Read C:/Users/user/input/seed.txt, then write '${greeting} <word> verified' to "
        "C:/Users/user/output/result.txt, replacing <word> with the word you read. "
        "Do not include quotes or a trailing newline. Use your file or shell tools.\n"
    )
    gateway = Gateway(
        api_key=api_key, upstream=upstream, dialect=config.gateway.dialect, host="0.0.0.0"
    )
    await gateway.start()
    try:
        result = await run_episode(
            load_task_folder(task_root),
            StandardEnvironment(harness),
            provider_registry(
                QemuProvider(image=image, overlay_dir=tmp_path / "qemu"), kind=ImageKind.VM
            ),
            run_dir=tmp_path / "runs",
            gateway_url=gateway.base_url,
            gateway=gateway,
            model=model,
            limits=Limits(max_model_calls=12),
            logging_policy=LoggingPolicy(native_logs="debug"),
        )
    finally:
        await gateway.stop()

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"output": 1.0, "setup_asset": 1.0, "setup_ran": 1.0}
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    assert any(step.get("tool_calls") for step in trajectory["steps"])
    assert (result.run_dir / "logs" / name / "transcript.jsonl").stat().st_size > 0

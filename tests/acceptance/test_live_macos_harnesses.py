"""Opt-in macOS Harness acceptance on disposable Orchard VMs.

Run on a Mac whose local Orchard ``scripts/setup_macos.py`` keeps running. Set
ORBIT_RUN_LIVE_ACCEPTANCE=1, ORBIT_LIVE_MACOS_CONFIG to a private run TOML, and
ORBIT_ORCHARD_TEST_IMAGE / ORBIT_ORCHARD_TEST_USER to an image that logs that account in
automatically, such as ``tests.support.MACOS_BASE`` and ``admin``; the Task builds on it
as the CLI integration tests' image does. The TOML supplies the API-key Gateway
configuration; credentials stay in the environment. Select one Harness with pytest -k and
use its Gateway dialect: Responses for the CLIs, Anthropic for computer-use. Each test
creates and destroys its own VM.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

from orbit.core.config import LoggingPolicy, load_run_config
from orbit.core.harness import EffectiveAgentResources
from orbit.core.taskspec import OperatingSystem
from orbit.core.verdict import Status
from orbit.run.cli.tasks import PRESET_DIR, _harness
from orbit.run.episode import run_episode
from orbit.run.gateway.server import Gateway
from orbit.run.gateway.session import Limits
from orbit.run.harnesses.base import STDOUT_PATH
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.secrets import provider_credentials
from orbit.run.tasksets.manifest import load_task_folder
from orbit.run.tools import resolved_cua_desktop
from tests.support import MACOS_RUNTIME_BUILD, provider_registry, write_task

from .trajectory import LIVE, native_session_file, trajectory_mcp_calls

pytestmark = [pytest.mark.needs_orchard, pytest.mark.needs_gui, pytest.mark.needs_llm, LIVE]


def _write_task(root: Path, image: str, user: str, *, policy: bool) -> Path:
    home = f"/Users/{user}"
    output = shlex.quote(f"{home}/output")
    setup = f"#!/bin/sh\nset -eu\nmkdir -p {output}\n: > {output}/result.txt\n"
    setup += f"chown -R {shlex.quote(user)} {output}\n"
    if policy:
        setup += f"sudo -u {shlex.quote(user)} open -a TextEdit {output}/result.txt\n"
        instruction = (
            "Use the visible TextEdit window on macOS. Take a screenshot, type exactly "
            "Orbit macOS verified into the empty result.txt document, save with Command+S, "
            "and take a screenshot to confirm. Do not use a terminal."
        )
    else:
        instruction = (
            "Use the cua-desktop MCP screenshot tool to inspect the macOS desktop. "
            f"Then use your file or shell tools to write exactly Orbit macOS verified to "
            f"{home}/output/result.txt. Confirm the saved content and finish."
        )
    task = write_task(
        root,
        name="agentslastexam/macos-harness-acceptance",
        config={
            "artifacts": [f"{home}/output"],
            "environment": {"os": "macos", "image": image, "cpus": 2, "memory_mb": 4096},
            "agent": {"user": user, "timeout_sec": 600, "network_mode": "public", "sudo": True},
        },
        dockerfile=None,
        instruction=instruction + "\n",
        test='#!/bin/sh\nset -eu\nexec "$ORBIT_VERIFY_PYTHON" verify.py\n',
        solve=f"#!/bin/sh\nset -eu\nprintf 'Orbit macOS verified\\n' > {output}/result.txt\n",
    )
    (task / "environment" / "setup").mkdir(parents=True)
    (task / "environment" / "build.sh").write_text(MACOS_RUNTIME_BUILD)
    (task / "environment" / "setup" / "setup.sh").write_text(setup)
    (task / "tests" / "verify.py").write_text(
        "from pathlib import Path\nfrom orbit_verify import write\n"
        f"p = Path({f'{home}/output/result.txt'!r})\n"
        "text = p.read_text().strip() if p.is_file() else ''\n"
        "write({'reward': float(text == 'Orbit macOS verified')})\n"
    )
    return task


@pytest.mark.parametrize("name", ["grok-build", "openclaw-cli", "opencode", "computer-use"])
async def test_macos_harness_completes_task_and_records_tools(tmp_path: Path, name: str) -> None:
    config_path = os.environ.get("ORBIT_LIVE_MACOS_CONFIG")
    image = os.environ.get("ORBIT_ORCHARD_TEST_IMAGE")
    user = os.environ.get("ORBIT_ORCHARD_TEST_USER")
    if not config_path or not image or not user:
        pytest.skip("set the explicit macOS acceptance configuration, image and user")
    preset = PRESET_DIR / f"{name}.toml"
    config = load_run_config(
        preset_path=preset if preset.is_file() else None,
        run_path=Path(config_path),
        overrides=[f"agent.name={name}", "agent.authentication=api-key"],
    )
    harness = _harness(config)
    harness.validate_os(OperatingSystem.MACOS)
    policy = name == "computer-use"
    resources = EffectiveAgentResources(
        mcp_servers=() if policy else (resolved_cua_desktop(OperatingSystem.MACOS),)
    )
    task = load_task_folder(_write_task(tmp_path / "task", image, user, policy=policy))
    api_key, upstream = provider_credentials(
        config.gateway.provider,
        base_url=config.gateway.base_url,
        key_var=config.gateway.api_key_env,
    )
    gateway = Gateway(
        api_key=api_key, upstream=upstream, dialect=config.gateway.dialect, host="0.0.0.0"
    )
    await gateway.start()
    try:
        result = await run_episode(
            task,
            EpisodeProtocol(harness, max_steps=20, stall_limit=6),
            provider_registry(),
            run_dir=tmp_path / "runs",
            gateway_url=gateway.base_url,
            gateway=gateway,
            model=config.agent.model,
            agent_resources=resources,
            limits=Limits(max_model_calls=20),
            logging_policy=LoggingPolicy(native_logs="debug"),
        )
    finally:
        await gateway.stop()
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    steps = trajectory["steps"]
    if policy:
        calls = [call for step in steps for call in step.get("tool_calls") or ()]
        kinds = {call["extra"]["orbit"]["normalized_action"]["type"] for call in calls}
        assert {"screenshot", "type", "key"} <= kinds
    else:
        screenshots = [
            call
            for call in trajectory_mcp_calls(steps).values()
            if call["mcp"] == {"server": "cua-desktop", "tool": "screenshot"}
        ]
        assert any(
            shot["result"] is not None
            and not shot["result"].get("extra", {}).get("orbit", {}).get("is_error")
            for shot in screenshots
        ), "no successful desktop screenshot call"
        if name == "openclaw-cli":
            images = [
                item
                for step in steps
                for observation in (step.get("observation") or {}).get("results", [])
                if isinstance(observation["content"], list)
                for item in observation["content"]
                if item["type"] == "image"
            ]
            assert images, "no desktop image retained in native tool results"
            assert images[0]["source"]["media_type"] in {"image/png", "image/jpeg"}
            assert (result.run_dir / images[0]["source"]["path"]).is_file()
        logs = result.run_dir / "logs" / name
        assert (logs / STDOUT_PATH).stat().st_size > 0
        assert native_session_file(logs, name, trajectory["session_id"]).stat().st_size > 0

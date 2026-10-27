"""Opt-in macOS Harness acceptance on disposable Orchard VMs.

Set ALE_RUN_LIVE_ACCEPTANCE=1, ALE_LIVE_MACOS_CONFIG to a private run TOML,
and ALE_ORCHARD_TEST_IMAGE / ALE_ORCHARD_TEST_USER to a prepared desktop image
and its account. The TOML supplies Orchard and API-key Gateway configuration;
credentials stay in the environment. Select one Harness with pytest -k and use
its Gateway dialect: Responses for the CLIs, Anthropic for computer-use. Each test
creates and destroys its own VM.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest
import yaml

from ale.core.config import LoggingPolicy, load_run_config
from ale.core.harness import EffectiveAgentResources
from ale.core.taskspec import ImageKind, OperatingSystem
from ale.core.verdict import Status
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import Limits
from ale.run.harnesses.base import STDOUT_PATH
from ale.run.providers.orchard import OrchardProvider
from ale.run.secrets import provider_credentials
from ale.run.tasksets.manifest import load_task_folder
from ale.run.tools import resolved_cua_desktop
from tests.support import provider_registry

from .trajectory import LIVE, native_session_file, trajectory_mcp_calls

pytestmark = [pytest.mark.needs_orchard, pytest.mark.needs_gui, pytest.mark.needs_llm, LIVE]


def _write_task(root: Path, image: str, user: str, *, policy: bool) -> Path:
    home = f"/Users/{user}"
    for stage in ("setup", "verify", "oracle"):
        (root / stage).mkdir(parents=True)
    (root / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_type": "core/v1",
                "name": "macos-harness-acceptance",
                "os": "macos",
                "image": {"kind": "vm", "ref": image, "user": user},
                "resources": {"cpus": 2, "memory_mb": 4096, "sudo": True},
                "network": {"mode": "open"},
                "timeouts": {"setup": 120, "agent": 600, "verify": 120},
                "artifacts": [f"{home}/output"],
            }
        )
    )
    output = shlex.quote(f"{home}/output")
    setup = f"#!/bin/sh\nset -eu\nmkdir -p {output}\n: > {output}/result.txt\n"
    setup += f"chown -R {shlex.quote(user)} {output}\n"
    if policy:
        setup += f"sudo -u {shlex.quote(user)} open -a TextEdit {output}/result.txt\n"
        instruction = (
            "Use the visible TextEdit window on macOS. Take a screenshot, type exactly "
            "ALE macOS verified into the empty result.txt document, save with Command+S, "
            "and take a screenshot to confirm. Do not use a terminal."
        )
    else:
        instruction = (
            "Use the cua-desktop MCP screenshot tool to inspect the macOS desktop. "
            f"Then use your file or shell tools to write exactly ALE macOS verified to "
            f"{home}/output/result.txt. Confirm the saved content and finish."
        )
    (root / "instruction.md").write_text(instruction + "\n")
    (root / "setup/run.sh").write_text(setup)
    (root / "oracle/run.sh").write_text(
        f"#!/bin/sh\nset -eu\nprintf 'ALE macOS verified\\n' > {output}/result.txt\n"
    )
    (root / "verify/run.sh").write_text("#!/bin/sh\nset -eu\nexec python3 verify.py\n")
    (root / "verify/verify.py").write_text(
        "import os\nfrom pathlib import Path\nfrom ale_verify import write\n"
        "p = Path(os.environ['ALE_HOME']) / 'output/result.txt'\n"
        "write({'overall': float(p.is_file() and p.read_text().strip() == 'ALE macOS verified')})\n"
    )
    return root


@pytest.mark.parametrize("name", ["grok-build", "openclaw-cli", "computer-use"])
async def test_macos_harness_completes_task_and_records_tools(tmp_path: Path, name: str) -> None:
    config_path = os.environ.get("ALE_LIVE_MACOS_CONFIG")
    image = os.environ.get("ALE_ORCHARD_TEST_IMAGE")
    user = os.environ.get("ALE_ORCHARD_TEST_USER")
    if not config_path or not image or not user:
        pytest.skip("set the explicit macOS acceptance configuration, image and user")
    preset = PRESET_DIR / f"{name}.toml"
    config = load_run_config(
        preset_path=preset if preset.is_file() else None,
        run_path=Path(config_path),
        overrides=[f"agent.name={name}", "agent.authentication=api-key"],
    )
    assert config.vm.provider == "orchard"
    assert config.vm.orchard.driver_path and config.vm.orchard.socket_path
    harness = _harness(config)
    harness.validate_os(OperatingSystem.MACOS)
    policy = name == "computer-use"
    resources = EffectiveAgentResources(
        mcp_servers=() if policy else (resolved_cua_desktop(OperatingSystem.MACOS),)
    )
    task = load_task_folder(_write_task(tmp_path / "task", image, user, policy=policy))
    api_key, upstream = provider_credentials(
        config.gateway.api_key_env, config.gateway.base_url, config.gateway.dialect
    )
    gateway = Gateway(
        api_key=api_key, upstream=upstream, dialect=config.gateway.dialect, host="0.0.0.0"
    )
    await gateway.start()
    try:
        result = await run_episode(
            task,
            StandardEnvironment(harness, max_steps=20, stall_limit=6),
            provider_registry(OrchardProvider(config.vm.orchard), kind=ImageKind.VM),
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
    assert result.verdict.rewards == {"overall": 1.0}
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    steps = trajectory["steps"]
    if policy:
        calls = [call for step in steps for call in step.get("tool_calls") or ()]
        kinds = {call["extra"]["ale"]["normalized_action"]["type"] for call in calls}
        assert {"screenshot", "type", "key"} <= kinds
    else:
        screenshots = [
            call
            for call in trajectory_mcp_calls(steps).values()
            if call["mcp"] == {"server": "cua-desktop", "tool": "screenshot"}
        ]
        assert any(
            shot["result"] is not None
            and not shot["result"].get("extra", {}).get("ale", {}).get("is_error")
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

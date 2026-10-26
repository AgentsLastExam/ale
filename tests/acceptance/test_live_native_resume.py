from __future__ import annotations

import json
import secrets

import pytest

from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.core.trace import read_jsonl
from ale.run.environments.log_mirror import LogMirror
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.harnesses.base import STDOUT_PATH
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.providers.docker import DockerProvider
from ale.run.recording import EpisodeRecording
from ale.run.secrets import provider_credentials
from tests.support import CONTAINER_BASE, prepare_reference

from .trajectory import LIVE, MODEL, native_session_file

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]


@pytest.mark.asyncio
async def test_real_claude_launch_and_two_resumes_share_one_native_session(tmp_path) -> None:
    key, upstream = provider_credentials()
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(api_key=key, upstream=upstream, host="0.0.0.0")
    gateway_session = None
    sandbox = None
    await gateway.start()
    try:
        gateway_session = gateway.open_session(
            GatewaySession(episode_id="live-native-resume", model=MODEL),
            recording.transport,
        )
        provider = DockerProvider()
        prepared = await prepare_reference(provider, CONTAINER_BASE)
        sandbox = await provider.create(
            SandboxRequest(
                episode_id="live-native-resume",
                prepared_image=prepared,
                resources=Resources(cpus=1, memory_mb=1024),
                network=NetworkPolicy(),
                gateway_url=gateway.base_url,
            )
        )
        harness = ClaudeCodeHarness()
        code = f"RSM-{secrets.token_hex(4).upper()}"
        prompts = (
            f"Remember code {code}. Reply exactly SEGMENT-1.",
            f"Reply exactly SEGMENT-2::{code}.",
            f"Reply exactly SEGMENT-3::{code}.",
        )

        await sandbox.open_egress()
        assert await harness.install(sandbox) == "2.1.227"
        session = HarnessSession(
            episode_id="live-native-resume",
            gateway_url=sandbox.gateway_url or gateway.base_url,
            token=gateway_session.token,
            model=MODEL,
            sandbox_id=sandbox.sandbox_id,
            home="/home/user",
        )
        await harness.install_resources(sandbox, session, EffectiveAgentResources())
        await sandbox.close_egress()

        first = await harness.launch(prompts[0], sandbox, session, timeout_sec=180)
        assert first.continuation is not None
        second = await harness.resume(
            prompts[1], first.continuation, sandbox, session, timeout_sec=180
        )
        assert second.continuation is not None
        third = await harness.resume(
            prompts[2], second.continuation, sandbox, session, timeout_sec=180
        )
        assert third.continuation is not None

        session_ids = {
            first.continuation.native_session_id,
            second.continuation.native_session_id,
            third.continuation.native_session_id,
        }
        assert len(session_ids) == 1
        assert [first.final_message, second.final_message, third.final_message] == [
            "SEGMENT-1",
            f"SEGMENT-2::{code}",
            f"SEGMENT-3::{code}",
        ]
        transport = read_jsonl(tmp_path / "trace.transport.jsonl").records
        assert len(transport) == 3
        assert all(
            record["model"] == MODEL and record["disposition"] == "forwarded"
            for record in transport
        )

        native_id = first.continuation.native_session_id
        logs = tmp_path / "logs"
        mirror = LogMirror(
            sandbox, home=session.home, os=session.os, paths=harness.logs, target=logs
        )
        await mirror.sync(final=True)
        assert not mirror.incomplete
        native_state = native_session_file(logs, harness.name, native_id).read_text()
        user_messages = []
        for line in native_state.splitlines():
            event = json.loads(line)
            if event.get("type") != "user":
                continue
            content = (event.get("message") or {}).get("content")
            if isinstance(content, str):
                user_messages.append(content)
            elif isinstance(content, list):
                user_messages.append(
                    "".join(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                )
        assert [sum(prompt in message for message in user_messages) for prompt in prompts] == [
            1,
            1,
            1,
        ]

        transcript = (logs / STDOUT_PATH).read_text()
        trajectory = harness.parse_trajectory(
            TrajectoryParseContext(
                episode_id="live-native-resume",
                trajectory_id="live-native-resume-trajectory",
                instruction=prompts[0],
                logs_dir=logs,
                model=MODEL,
                agent_version=harness.version(),
                blobs=recording.blobs,
                session_id=first.continuation.native_session_id,
            )
        )
        assert trajectory.session_id in session_ids
        # Each turn appears once, in order: its prompt, then its answer.
        conversation = [
            step.message
            for step in trajectory.steps
            if step.source in {"user", "agent"} and isinstance(step.message, str) and step.message
        ]
        assert conversation == [
            prompts[0],
            "SEGMENT-1",
            prompts[1],
            f"SEGMENT-2::{code}",
            prompts[2],
            f"SEGMENT-3::{code}",
        ]
        results = [
            json.loads(line)
            for line in transcript.splitlines()
            if json.loads(line).get("type") == "result"
        ]
        assert len(results) == 3
        assert {result["session_id"] for result in results} == session_ids
    finally:
        if sandbox is not None:
            await sandbox.destroy()
        if gateway_session is not None:
            gateway.close_session(gateway_session)
        await gateway.stop()

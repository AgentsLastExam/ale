from __future__ import annotations

import contextlib
import json
import secrets

import pytest

from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import SandboxRequest
from ale.core.taskspec import ImageRef, NetworkPolicy, Resources
from ale.core.trace import read_jsonl
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness
from ale.run.images import resolve_ref
from ale.run.providers.docker import DockerProvider
from ale.run.recording import EpisodeRecording
from ale.run.secrets import provider_credentials

from .trajectory import LIVE, llm_audit_evidence

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]

CASES = (
    (
        GrokBuildHarness,
        "grok-4.5",
        "XAI_API_KEY",
        "https://api.x.ai",
    ),
    (
        CodexCliHarness,
        "gpt-5-mini",
        "OPENAI_API_KEY",
        "https://api.openai.com",
    ),
    (
        OpenClawCliHarness,
        "gpt-5-mini",
        "OPENAI_API_KEY",
        "https://api.openai.com",
    ),
)


@pytest.mark.parametrize(
    ("factory", "model", "key_var", "upstream_url"),
    CASES,
    ids=["grok-build", "codex-cli", "openclaw-cli"],
)
async def test_real_native_resume_keeps_exact_session_and_only_new_input(
    tmp_path, factory, model: str, key_var: str, upstream_url: str
) -> None:
    harness = factory()
    key, upstream = provider_credentials(key_var, upstream_url, "openai-responses")
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(
        api_key=key,
        upstream=upstream,
        dialect="openai-responses",
        host="0.0.0.0",
    )
    gateway_session = None
    sandbox = None
    await gateway.start()
    try:
        gateway_session = gateway.open_session(
            GatewaySession(episode_id=f"live-{harness.name}-resume", model=model),
            recording.transport,
        )
        sandbox = await DockerProvider().create(
            SandboxRequest(
                episode_id=f"live-{harness.name}-resume",
                image_ref=resolve_ref(ImageRef(name="sandbox-base-cli")),
                resources=Resources(cpus=1, memory_mb=2048),
                network=NetworkPolicy(),
                gateway_url=gateway.base_url,
            )
        )
        await sandbox.open_egress()
        observed = await harness.install(sandbox)
        session = HarnessSession(
            episode_id=f"live-{harness.name}-resume",
            gateway_url=sandbox.gateway_url or gateway.base_url,
            token=gateway_session.token,
            model=model,
            sandbox_id=sandbox.sandbox_id,
            home="/home/user",
        )
        await harness.install_resources(sandbox, session, EffectiveAgentResources())
        await sandbox.close_egress()

        code = f"RSM-{secrets.token_hex(4).upper()}"
        prompts = (
            f"Remember code {code}. Reply exactly SEGMENT-1.",
            f"Reply exactly SEGMENT-2::{code}.",
            f"Reply exactly SEGMENT-3::{code}.",
        )
        first = await harness.launch(prompts[0], sandbox, session, timeout_sec=240)
        assert first.continuation is not None
        second = await harness.resume(
            prompts[1], first.continuation, sandbox, session, timeout_sec=240
        )
        assert second.continuation is not None
        third = await harness.resume(
            prompts[2], second.continuation, sandbox, session, timeout_sec=240
        )
        assert third.continuation is not None

        continuations = (first.continuation, second.continuation, third.continuation)
        assert {item.native_session_id for item in continuations} == {
            first.continuation.native_session_id
        }
        assert [first.final_message, second.final_message, third.final_message] == [
            "SEGMENT-1",
            f"SEGMENT-2::{code}",
            f"SEGMENT-3::{code}",
        ]

        logs = tmp_path / "logs"
        logs.mkdir()
        for name in harness.logs:
            with contextlib.suppress(Exception):
                (logs / name).write_bytes(await sandbox.read_file(f"/home/user/{name}"))
        trajectory = harness.parse_trajectory(
            TrajectoryParseContext(
                episode_id=session.episode_id,
                trajectory_id=f"{session.episode_id}-trajectory",
                instruction=prompts[0],
                logs_dir=logs,
                model=model,
                agent_version=observed,
                blobs=recording.blobs,
                session_id=first.continuation.native_session_id,
            )
        )
        transport = read_jsonl(tmp_path / "trace.transport.jsonl").records
        assert len([record for record in transport if record["kind"] == "call"]) >= 3
        native_turns = [
            turn for turn in _native_turns(harness.name, logs) if turn["message"] in prompts
        ]
        if native_turns:
            assert [turn["message"] for turn in native_turns] == list(prompts)
            assert {turn["session_id"] for turn in native_turns} == {
                first.continuation.native_session_id
            }
        llm_audit_evidence(
            {
                "trajectory": trajectory.to_json_dict(),
                "transport": transport,
                "native_user_turns": native_turns,
                "expected_messages": [
                    "SEGMENT-1",
                    f"SEGMENT-2::{code}",
                    f"SEGMENT-3::{code}",
                ],
                "native_session_ids": [item.native_session_id for item in continuations],
            },
            requirement=(
                f"{harness.name} continued one exact native session across three turns. "
                "The native user-turn evidence must contain each supplied new instruction "
                "exactly once in order under that session. The transport includes every "
                "CLI-owned model request, including ancillary requests, so its call count "
                "need not equal the number of user turns."
            ),
        )
    finally:
        if sandbox is not None:
            await sandbox.destroy()
        if gateway_session is not None:
            gateway.close_session(gateway_session)
        await gateway.stop()


def _native_turns(harness: str, logs) -> list[dict[str, str]]:  # type: ignore[no-untyped-def]
    turns: list[dict[str, str]] = []
    if harness == "grok-build":
        for line in (logs / "session_updates.jsonl").read_text().splitlines():
            event = json.loads(line)
            params = event.get("params") or {}
            update = params.get("update") or {}
            if update.get("sessionUpdate") != "user_message_chunk":
                continue
            turns.append(
                {
                    "session_id": str(params.get("sessionId") or ""),
                    "message": str((update.get("content") or {}).get("text") or ""),
                }
            )
    elif harness == "openclaw-cli":
        events = [json.loads(line) for line in (logs / "transcript.jsonl").read_text().splitlines()]
        session_id = next(
            (str(event.get("id")) for event in events if event.get("type") == "session"),
            "",
        )
        for event in events:
            message = event.get("message") or {}
            if event.get("type") != "message" or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content
            else:
                text = "\n".join(
                    str(block.get("text") or "")
                    for block in content or ()
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            if text:
                turns.append({"session_id": session_id, "message": text})
    elif harness == "codex-cli":
        events = [json.loads(line) for line in (logs / "session.jsonl").read_text().splitlines()]
        session_id = next(
            (
                str((event.get("payload") or {}).get("id") or "")
                for event in events
                if event.get("type") == "session_meta"
            ),
            "",
        )
        for event in events:
            payload = event.get("payload") or {}
            if (
                event.get("type") != "response_item"
                or payload.get("type") != "message"
                or payload.get("role") != "user"
            ):
                continue
            text = "\n".join(
                str(block.get("text") or "")
                for block in payload.get("content") or ()
                if isinstance(block, dict) and block.get("type") == "input_text"
            )
            if text:
                turns.append({"session_id": session_id, "message": text})
    return turns

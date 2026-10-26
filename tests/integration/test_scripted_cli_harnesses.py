"""Each pinned native CLI, really installed, runs a scripted episode through the real Gateway.

The model is scripted, so the run is deterministic and unbilled while everything else is
the production path: the official program in a sealed Docker sandbox, its generated
native configuration, the Gateway as its only way out, a launch and two native resumes,
the Host log mirror, and the parser reading what was mirrored.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox, SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.core.trace import read_jsonl
from ale.core.trajectory import AtifTrajectory
from ale.run.environments.log_mirror import LogMirror
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.harnesses.base import CONTINUE_PROMPT, CliHarness
from ale.run.harnesses.droid import DroidHarness
from ale.run.harnesses.forgecode import ForgecodeHarness
from ale.run.harnesses.openhands_cli import OpenHandsCliHarness
from ale.run.harnesses.pi_cli import PiCliHarness
from ale.run.providers.docker import DockerProvider
from ale.run.recording import BlobStore, EpisodeRecording
from tests.integration.scripted_model import FINAL, ScriptedModel
from tests.support import CONTAINER_BASE, prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


@dataclass(frozen=True)
class Case:
    factory: Callable[[], CliHarness]
    dialect: str
    tool_pattern: str
    model: str = "scripted-model"


DIALECTS = ("anthropic", "openai-chat-completions", "openai-responses")
#: Each shipped native CLI, by test ID, with the dialect and native shell tool it uses.
CASES: dict[str, Case] = {
    **{
        f"droid-{dialect}": Case(
            partial(DroidHarness, gateway_dialect=dialect), dialect, r"^Execute$"
        )
        for dialect in DIALECTS
    },
    **{
        f"forgecode-{dialect}": Case(
            partial(ForgecodeHarness, gateway_dialect=dialect), dialect, r"^shell$"
        )
        for dialect in DIALECTS
    },
    **{
        # The pinned OpenHands SDK sends exactly the GPT-5 family over Responses.
        f"openhands-cli-{dialect}": Case(
            partial(OpenHandsCliHarness, gateway_dialect=dialect),
            dialect,
            r"^terminal$",
            "gpt-5-scripted" if dialect == "openai-responses" else "scripted-model",
        )
        for dialect in DIALECTS
    },
    **{
        f"pi-cli-{dialect}": Case(
            partial(PiCliHarness, gateway_dialect=dialect), dialect, r"^bash$"
        )
        for dialect in DIALECTS
    },
}


@dataclass
class Episode:
    harness: CliHarness
    sandbox: Sandbox
    session: HarnessSession
    version: str


@asynccontextmanager
async def scripted(
    tmp_path: Path, name: str, case: Case, model: ScriptedModel
) -> AsyncIterator[Episode]:
    """A prepared, sealed sandbox whose CLI reaches only the Gateway, scripted upstream."""
    harness = case.factory()
    upstream = await model.start()
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(api_key="host-only", upstream=upstream, dialect=case.dialect, host="0.0.0.0")
    await gateway.start()
    gateway_session = gateway.open_session(
        GatewaySession(episode_id=f"scripted-{name}", model=case.model), recording.transport
    )
    sandbox = None
    try:
        provider = DockerProvider()
        sandbox = await provider.create(
            SandboxRequest(
                episode_id=f"scripted-{name}",
                prepared_image=await prepare_reference(provider, CONTAINER_BASE),
                resources=Resources(cpus=2, memory_mb=4096),
                network=NetworkPolicy(),
                gateway_url=gateway.base_url,
            )
        )
        await sandbox.open_egress()
        observed = await harness.install(sandbox)
        assert observed == harness.version()
        session = HarnessSession(
            episode_id=f"scripted-{name}",
            gateway_url=sandbox.gateway_url or gateway.base_url,
            token=gateway_session.token,
            model=case.model,
            sandbox_id=sandbox.sandbox_id,
            home="/home/user",
        )
        await harness.install_resources(sandbox, session, EffectiveAgentResources())
        await sandbox.close_egress()
        yield Episode(harness, sandbox, session, observed)
    finally:
        if sandbox is not None:
            await sandbox.destroy()
        gateway.close_session(gateway_session)
        await gateway.stop()
        await model.stop()


async def mirrored_trajectory(
    tmp_path: Path, episode: Episode, instruction: str, native_id: str
) -> AtifTrajectory:
    logs = tmp_path / "logs"
    mirror = LogMirror(
        episode.sandbox,
        home=episode.session.home,
        os=episode.session.os,
        paths=episode.harness.logs,
        target=logs,
    )
    await mirror.sync(final=True)
    assert not mirror.incomplete
    return episode.harness.parse_trajectory(
        TrajectoryParseContext(
            episode_id=episode.session.episode_id,
            trajectory_id=f"{episode.session.episode_id}-trajectory",
            instruction=instruction,
            logs_dir=logs,
            model=episode.session.model,
            agent_version=episode.version,
            blobs=BlobStore(tmp_path),
            session_id=native_id,
        )
    )


@pytest.mark.parametrize("name", sorted(CASES))
async def test_cli_runs_a_scripted_episode_through_the_gateway(tmp_path: Path, name: str) -> None:
    case = CASES[name]
    model = ScriptedModel(tool_pattern=case.tool_pattern)
    prompts = ("Write the proof file.", "Write it once more.", CONTINUE_PROMPT)
    async with scripted(tmp_path, name, case, model) as episode:
        harness, sandbox, session = episode.harness, episode.sandbox, episode.session
        first = await harness.launch(prompts[0], sandbox, session, timeout_sec=300)
        assert first.continuation is not None
        second = await harness.resume(
            prompts[1], first.continuation, sandbox, session, timeout_sec=300
        )
        assert second.continuation is not None
        third = await harness.resume(None, second.continuation, sandbox, session, timeout_sec=300)
        assert third.continuation is not None
        native_id = first.continuation.native_session_id
        assert {
            run.continuation.native_session_id
            for run in (first, second, third)
            if run.continuation is not None
        } == {native_id}
        proof = await sandbox.exec(
            ["cat", "/home/user/scripted-proof.txt"], identity=Identity.AGENT
        )
        assert proof.stdout.strip() == "scripted"
        trajectory = await mirrored_trajectory(tmp_path, episode, prompts[0], native_id)

    assert not (trajectory.extra or {}).get("ale", {}).get("parse_issues")
    users = [step.message for step in trajectory.steps if step.source == "user"]
    assert users == list(prompts)
    calls = [call for step in trajectory.steps for call in step.tool_calls or ()]
    results = {
        result.source_call_id
        for step in trajectory.steps
        if step.observation
        for result in step.observation.results
    }
    assert len(calls) == 3 and {call.tool_call_id for call in calls} <= results
    assert [step.message for step in trajectory.steps if step.source == "agent"].count(FINAL) >= 3
    transport = read_jsonl(tmp_path / "trace.transport.jsonl").records
    calls_made = [record for record in transport if record["kind"] == "call"]
    assert len(calls_made) >= 6 and len(calls_made) == len(model.requests)


#: CLIs whose delegation tool spawns subagent sessions: the tool, and the arguments that
#: delegate ``task`` to a child.
DELEGATION: dict[str, tuple[str, Callable[[str], dict[str, object]]]] = {
    "forgecode-openai-chat-completions": (
        r"^task$",
        lambda task: {"agent_id": "forge", "tasks": [task]},
    ),
    "openhands-cli-openai-chat-completions": (
        r"^task$",
        lambda task: {
            "description": "Delegated work",
            "prompt": task,
            "subagent_type": "general-purpose",
            "security_risk": "LOW",
        },
    ),
}


@pytest.mark.parametrize("name", sorted(DELEGATION))
async def test_cli_subagents_link_to_the_calls_that_spawned_them(tmp_path: Path, name: str) -> None:
    """A delegation that itself delegates: each child embeds in its spawner's trajectory."""
    case = CASES[name]
    tool, arguments = DELEGATION[name]
    model = ScriptedModel(
        tool_pattern=case.tool_pattern,
        delegations=(
            ("ALE-DELEGATE-ROOT", tool, arguments("ALE-DELEGATE-CHILD: delegate the proof.")),
            ("ALE-DELEGATE-CHILD", tool, arguments("Write the proof file.")),
        ),
    )
    prompt = "ALE-DELEGATE-ROOT: delegate the proof."
    async with scripted(tmp_path, name, case, model) as episode:
        run = await episode.harness.launch(
            prompt, episode.sandbox, episode.session, timeout_sec=600
        )
        assert run.continuation is not None
        trajectory = await mirrored_trajectory(
            tmp_path, episode, prompt, run.continuation.native_session_id
        )

    assert not (trajectory.extra or {}).get("ale", {}).get("parse_issues")
    (child,) = trajectory.subagent_trajectories or ()
    assert child.trajectory_id.startswith(f"{trajectory.trajectory_id}-subagent-")
    refs = [
        ref
        for step in trajectory.steps
        if step.observation
        for result in step.observation.results
        for ref in result.subagent_trajectory_ref or ()
    ]
    assert [ref.trajectory_id for ref in refs] == [child.trajectory_id]
    for grandchild in child.subagent_trajectories or ():
        assert grandchild.trajectory_id.startswith(f"{child.trajectory_id}-subagent-")

"""Native continuations: isolated per session, and continued from a kept or snapshot sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
from pathlib import Path

import pytest

from ale.core.config import RunConfig, SandboxRetentionConfig
from ale.core.harness import AgentRun, HarnessSession, NativeContinuation, ResumeSupport
from ale.core.lock import RunLock, TaskSource
from ale.core.sandbox import ExecResult, Identity, Sandbox
from ale.core.testkit import AutonomousHarnessConformance
from ale.core.verdict import Status
from ale.run.cli.tasks import _continuation_source, _locked_image, _resume
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import NopHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import (
    DockerProvider,
    destroy_retained,
    list_retained,
    snapshot_image,
)
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = pytest.mark.integration


class FakeClaudeSandbox:
    def __init__(self, index: int) -> None:
        self.sandbox_id = f"sandbox-{index}"
        self.prompts: list[str] = []
        self.selectors: list[tuple[str, str]] = []
        self.transcript = b""

    async def exec(self, argv, *, env=None, **kwargs):  # type: ignore[no-untyped-def]
        command = " ".join(str(part) for part in argv)
        if "find " in command or "claude " not in command:
            return ExecResult(exit_code=0)
        prompt = next(value for key, value in (env or {}).items() if key.startswith("ALE_PROMPT_"))
        self.prompts.append(prompt)
        match = re.search(r"--(session-id|resume) ([^ ]+)", command)
        assert match
        selector, native_id = match.groups()
        native_id = native_id.strip("'")
        self.selectors.append((selector, native_id))
        line = (
            json.dumps({"type": "result", "session_id": native_id, "result": prompt}).encode()
            + b"\n"
        )
        self.transcript = self.transcript + line if " >> " in command else line
        return ExecResult(exit_code=0)

    async def read_file(self, path):  # type: ignore[no-untyped-def]
        return self.transcript


@pytest.mark.asyncio
async def test_twenty_three_segment_native_sessions_are_isolated() -> None:
    harness = ClaudeCodeHarness()

    async def run(index: int):  # type: ignore[no-untyped-def]
        sandbox = FakeClaudeSandbox(index)
        session = HarnessSession(
            episode_id=f"episode-{index}",
            gateway_url="http://gateway",
            token=f"token-{index}",
            model="claude-opus-4-8",
            sandbox_id=sandbox.sandbox_id,
            resources_digest="sha256:" + f"{index:064x}",
            home=f"/home/agent-{index}",
        )
        current = await harness.launch(
            f"{index}:one",
            sandbox,  # type: ignore[arg-type]
            session,
            timeout_sec=10,
        )
        for suffix in ("two", "three"):
            assert current.continuation is not None
            current = await harness.resume(
                f"{index}:{suffix}",
                current.continuation,
                sandbox,  # type: ignore[arg-type]
                session,
                timeout_sec=10,
            )
        assert current.continuation is not None
        AutonomousHarnessConformance.check_native_continuation(harness, current.continuation)
        return sandbox, current.continuation.native_session_id

    results = await asyncio.gather(*(run(index) for index in range(20)))

    assert len({native_id for _, native_id in results}) == 20
    for index, (sandbox, native_id) in enumerate(results):
        assert sandbox.prompts == [f"{index}:one", f"{index}:two", f"{index}:three"]
        assert sandbox.selectors == [
            ("session-id", native_id),
            ("resume", native_id),
            ("resume", native_id),
        ]


class SessionFileHarness(NopHarness):
    """Keeps its native session as a file in the agent's home, as agent CLIs do."""

    name = "session-file"
    resume_support = ResumeSupport.NATIVE

    def __init__(self) -> None:
        self.sessions: list[str] = []

    async def _turn(self, instruction: str, sandbox: Sandbox, session: HarnessSession) -> str:
        log = shlex.quote(f"{session.home}/session.log")
        result = await sandbox.exec(
            ["bash", "-c", f"echo {shlex.quote(instruction)} >> {log} && cat {log}"],
            identity=Identity.AGENT,
        )
        assert result.ok, result.stderr
        return result.stdout

    async def launch(  # type: ignore[override]
        self, instruction: str, sandbox: Sandbox, session: HarnessSession, *, timeout_sec: float
    ) -> AgentRun:
        await self._turn("first", sandbox, session)
        continuation = NativeContinuation(harness=self.name, native_session_id="session")
        return AgentRun(exit_code=0, continuation=continuation)

    async def resume(  # type: ignore[override]
        self,
        instruction: str,
        continuation: NativeContinuation,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        self.sessions.append(await self._turn("next", sandbox, session))
        return AgentRun(exit_code=0, continuation=continuation)


@pytest.mark.needs_docker
@pytest.mark.asyncio
@pytest.mark.parametrize("retention", ["snapshot", "keep"])
async def test_episode_continues_the_session_in_a_snapshot_or_kept_sandbox(
    tmp_path: Path, retention: str
) -> None:
    task_path = scaffold_task(tmp_path / "task")
    task = load_tasks(task_path)[0]
    registry = provider_registry(DockerProvider())
    harness = SessionFileHarness()
    settings = RunConfig()

    def inputs(resumed_from: str | None = None) -> ProvenanceInputs:
        return ProvenanceInputs(
            source=TaskSource(kind="local", path=str(task_path)),
            agent=agent_provenance(harness, ""),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
            resumed_from=resumed_from,
        )

    first = await run_episode(
        task,
        StandardEnvironment(harness),
        registry,
        run_dir=tmp_path / "first",
        provenance=inputs(),
        sandbox_retention=SandboxRetentionConfig(solver=retention),  # type: ignore[arg-type]
    )
    assert first.record.continuation == NativeContinuation(
        harness="session-file", native_session_id="session"
    )
    source, lock, handle = _continuation_source(first.run_dir)
    try:
        resume = _resume(source, lock, handle, harness, 1)
        task.prepared_image = (
            snapshot_image(handle) if retention == "snapshot" else _locked_image(lock)
        )
        second = await run_episode(
            task,
            StandardEnvironment(harness, resume=resume),
            registry,
            run_dir=tmp_path / "second",
            provenance=inputs(first.episode_id),
        )
        retained = {item["handle"] for item in await list_retained()}
    finally:
        with contextlib.suppress(Exception):
            await destroy_retained(handle)

    assert second.record.status is Status.COMPLETED, second.record.failure
    assert harness.sessions == ["first\nnext\n"]
    resumed = RunLock.model_validate_json((second.run_dir / "lock.json").read_text())
    assert resumed.resumed_from == first.episode_id
    # A snapshot stays for further branches; a kept sandbox now follows this episode's
    # retention, which destroys it.
    if retention == "snapshot":
        assert handle in retained
    assert [(item.requested, item.outcome) for item in second.record.sandboxes] == [
        ("destroy", "destroyed")
    ]

"""Shared execution rules for native command-line agents."""

from __future__ import annotations

import json
import shlex
from abc import abstractmethod
from dataclasses import dataclass
from typing import final

from pydantic import BaseModel

from ale.core.errors import (
    AgentError,
    AgentUnsupportedError,
    NativeContinuationError,
    PhaseTimeoutError,
)
from ale.core.harness import (
    AgentRun,
    AutonomousHarness,
    HarnessSession,
    NativeContinuation,
    ResumeSupport,
    TrajectoryParseContext,
)
from ale.core.sandbox import ExecResult, Identity, Sandbox
from ale.core.trajectory import AtifTrajectory
from ale.run.agent_resources import continuation_fingerprint
from ale.run.harnesses._diagnostics import failure_detail
from ale.run.harnesses._npm import agent_path, bash_command

LOG_ROOT = ".ale-harness"
STDOUT_NAME = "stdout.log"
STDERR_NAME = "stderr.log"
INVOCATIONS_NAME = "invocations.jsonl"
FIXED_PROMPT = "Read instruction.md in the current working directory and follow its instructions."


@dataclass(frozen=True)
class CliCommand:
    argv: tuple[str, ...]
    env: dict[str, str]
    expected_session_id: str | None = None


@dataclass(frozen=True)
class NativeResult:
    session_id: str | None
    final_message: str | None


@dataclass(frozen=True)
class NativeLogs:
    root: str
    append_globs: tuple[str, ...] = ()


class CliHarness(AutonomousHarness):
    """Keep invocation mechanics identical while leaving native behavior explicit."""

    settings: BaseModel

    @property
    @abstractmethod
    def native_logs(self) -> tuple[NativeLogs, ...]: ...

    @abstractmethod
    async def prepare_command(
        self,
        instruction_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand: ...

    @abstractmethod
    def read_result(self, result: ExecResult, session: HarnessSession) -> NativeResult: ...

    @abstractmethod
    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory: ...

    @final
    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        return await self._execute(instruction, sandbox, session, None, timeout_sec)

    @final
    async def resume(
        self,
        instruction: str,
        continuation: NativeContinuation,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        if self.resume_support is not ResumeSupport.NATIVE:
            raise AgentUnsupportedError(f"{self.name} does not support native resume")
        if continuation.harness != self.name or continuation.episode_id != session.episode_id:
            raise NativeContinuationError("continuation belongs to a different harness or episode")
        if not session.sandbox_id or continuation.sandbox_id != session.sandbox_id:
            raise NativeContinuationError("continuation requires the original live sandbox")
        if continuation.fingerprint != self._fingerprint(session):
            raise NativeContinuationError(
                "model, settings, or effective resources changed since launch"
            )
        return await self._execute(instruction, sandbox, session, continuation, timeout_sec)

    def _fingerprint(self, session: HarnessSession) -> str:
        return continuation_fingerprint(
            harness=self.name,
            model=session.model,
            settings=self.settings.model_dump(mode="json"),
            resources_digest=session.resources_digest,
            authentication=session.authentication,
            profile_slot_id=session.profile_slot_id,
        )

    async def _execute(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        continuation: NativeContinuation | None,
        timeout_sec: float,
    ) -> AgentRun:
        home = agent_path(session.home)
        instruction_path = str(home / "instruction.md")
        await sandbox.write_file(
            instruction_path, instruction.encode("utf-8"), identity=Identity.AGENT
        )
        command = await self.prepare_command(
            instruction_path,
            sandbox,
            session,
            native_session_id=continuation.native_session_id if continuation else None,
        )
        root = home / LOG_ROOT
        stdout_path, stderr_path = str(root / STDOUT_NAME), str(root / STDERR_NAME)
        invocation_path = str(root / INVOCATIONS_NAME)
        prepared = await sandbox.exec(
            bash_command(session.home, f"mkdir -p -- {shlex.quote(str(root))}", login=False),
            identity=Identity.AGENT,
        )
        if not prepared.ok:
            raise AgentError(f"could not prepare {self.name} log directory: {prepared.stderr}")
        if continuation is None:
            for path in (stdout_path, stderr_path, invocation_path):
                await sandbox.write_file(path, b"", identity=Identity.AGENT)
        sizes = {
            str(agent_path(item.path)): item.size for item in await sandbox.list_files(str(root))
        }
        stdout_offset = sizes.get(stdout_path, 0)
        stderr_offset = sizes.get(stderr_path, 0)
        previous = await sandbox.read_file(invocation_path)
        record = (
            json.dumps(
                {
                    "stdout_offset": stdout_offset,
                    "stderr_offset": stderr_offset,
                    "expected_session_id": command.expected_session_id,
                }
            ).encode("utf-8")
            + b"\n"
        )
        await sandbox.write_file(invocation_path, previous + record, identity=Identity.AGENT)
        script = (
            f"{shlex.join(command.argv)} >> {shlex.quote(stdout_path)} "
            f"2>> {shlex.quote(stderr_path)}"
        )
        result = await sandbox.exec(
            bash_command(session.home, script, login=False),
            cwd=str(home),
            env=command.env,
            identity=Identity.AGENT,
            timeout_sec=timeout_sec,
        )
        if result.timed_out:
            raise PhaseTimeoutError("agent", timeout_sec)
        stdout = (await sandbox.read_file(stdout_path, offset=stdout_offset)).decode(
            "utf-8", "replace"
        )
        stderr = (await sandbox.read_file(stderr_path, offset=stderr_offset)).decode(
            "utf-8", "replace"
        )
        current = result.model_copy(update={"stdout": stdout, "stderr": stderr or result.stderr})
        native = self.read_result(current, session)
        if not current.ok:
            raise AgentError(
                failure_detail(
                    self.name,
                    current.exit_code,
                    stdout=current.stdout,
                    stderr=current.stderr,
                    token=session.token,
                )
            )
        expected = continuation.native_session_id if continuation else command.expected_session_id
        if expected is not None and native.session_id != expected:
            raise NativeContinuationError(
                f"{self.name} did not confirm the requested native session ID"
            )
        if (
            command.expected_session_id is not None
            and native.session_id != command.expected_session_id
        ):
            raise NativeContinuationError(f"{self.name} reported a different native session ID")
        next_continuation = None
        if self.resume_support is ResumeSupport.NATIVE:
            if not native.session_id:
                raise NativeContinuationError(f"{self.name} did not report a native session ID")
            next_continuation = NativeContinuation(
                harness=self.name,
                native_session_id=native.session_id,
                episode_id=session.episode_id,
                sandbox_id=session.sandbox_id,
                fingerprint=self._fingerprint(session),
            )
        return AgentRun(
            exit_code=0, final_message=native.final_message, continuation=next_continuation
        )

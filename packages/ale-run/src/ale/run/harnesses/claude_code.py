"""Claude Code as an autonomous harness.

The agent runs inside the sandbox and drives itself; we hand it a prompt, point its SDK
at the gateway, and read what it left behind. Pointing it at the gateway needs no change
to the agent: it already honours ``ANTHROPIC_BASE_URL``, which is why credentials can
stay on the host and ceilings can bind an agent that knows nothing about them.

The flag and environment tables are declarative so the same descriptors serve three
purposes — building the command line, documenting what is configurable, and validating
what a run asked for. Harbor's integration is the reference for that shape.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from ale.core.errors import AgentError, AgentRefusalError
from ale.core.harness import AgentRun, AutonomousHarness, HarnessSession, ResumeSupport
from ale.core.sandbox import Identity, Sandbox

__all__ = ["ClaudeCodeHarness"]

#: File names inside whatever workspace the session supplies. The directory itself is
#: not ours to choose: the agent runs unprivileged and can only write what it owns.
TRANSCRIPT_NAME = "transcript.jsonl"
PROMPT_NAME = "prompt.txt"
STDERR_NAME = "agent.stderr"

#: Settings a run may pass through ``agent.kwargs``, and the flag each becomes.
CLI_FLAGS: dict[str, str] = {
    "max_turns": "--max-turns",
    "permission_mode": "--permission-mode",
    "allowed_tools": "--allowed-tools",
    "system_prompt": "--append-system-prompt",
}

#: Error signatures worth naming, so a failure is attributed rather than lumped into
#: "the agent exited non-zero".
ERROR_PATTERNS: tuple[tuple[str, type[Exception]], ...] = (
    ("ale_limit_reached", AgentError),
    ("rate_limit", AgentError),
    ("I can't help", AgentRefusalError),
    ("I cannot help", AgentRefusalError),
)


class ClaudeCodeHarness(AutonomousHarness):
    """Runs the Claude Code CLI inside the sandbox."""

    name = "claude-code"
    resume_support = ResumeSupport.NONE  # single-shot in Phase 0

    def __init__(self, *, cli_version: str | None = None, **kwargs: Any) -> None:
        self.cli_version = cli_version
        # The CLI asks a human before touching files. There is no human here, and the
        # isolation the prompt exists to provide is the sandbox's job — so an unset
        # permission mode means the agent stops on its first write and reports, quite
        # correctly, that it needs permission. Left overridable: a task studying how an
        # agent behaves under prompting can ask for that deliberately.
        kwargs.setdefault("permission_mode", "bypassPermissions")
        self.kwargs = kwargs
        self._resolved_version: str | None = None

    def version(self) -> str:
        return self._resolved_version or self.cli_version or "unknown"

    def integrity(self) -> str:
        """What pins the binary: the image, unless a run asked for a specific build."""
        return f"npm:{self.cli_version}" if self.cli_version else "image"

    async def install(self, sandbox: Sandbox) -> None:
        """Make sure the CLI is present, and record which build actually ran.

        Base images bake a default build so the common path needs no network at all;
        a run that pins a different version pays for the install and says so in
        provenance.
        """
        if self.cli_version:
            spec = f"@anthropic-ai/claude-code@{self.cli_version}"
            # Installing the pinned CLI is preparation, not the agent's work.
            result = await sandbox.exec(["npm", "install", "-g", spec], timeout_sec=600)
            if not result.ok:
                raise AgentError(f"could not install {spec}: {result.stderr[-500:]}")

        probe = await sandbox.exec(["claude", "--version"], timeout_sec=60)
        if not probe.ok:
            raise AgentError(
                "the claude CLI is not available in this image; bake it in or pin "
                "agent.version so it can be installed"
            )
        self._resolved_version = probe.stdout.strip().split()[0] if probe.stdout.strip() else None

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        work_dir = PurePosixPath(session.work_dir)
        prompt_file = work_dir / PROMPT_NAME
        transcript = work_dir / TRANSCRIPT_NAME
        stderr_file = work_dir / STDERR_NAME
        await sandbox.write_file(prompt_file, instruction.encode("utf-8"), identity=Identity.AGENT)

        argv = [
            "bash",
            "-lc",
            f"claude -p - --output-format stream-json --verbose "
            f"{self._flags()} < {prompt_file} > {transcript} 2>{stderr_file}",
        ]
        result = await sandbox.exec(
            argv,
            cwd=str(work_dir),
            env=self._env(session),
            timeout_sec=timeout_sec,
            # The thing being measured runs unprivileged, so it cannot change the
            # conditions of its own measurement.
            identity=Identity.AGENT,
        )

        stderr = await self._read_text(sandbox, stderr_file)
        if result.exit_code != 0:
            raise self._classify(stderr or result.stderr, result.exit_code)

        transcript = await self._read_text(sandbox, transcript)
        return AgentRun(exit_code=result.exit_code, final_message=_final_message(transcript))

    def parse_artifacts(self, artifacts_dir: Path) -> list[dict[str, Any]]:
        """Turn the transcript into semantic trace payloads.

        Pure and host-side: it reads collected files, so it can be re-run over a finished
        episode without touching a sandbox.
        """
        transcript = artifacts_dir / "transcript.jsonl"
        if not transcript.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in transcript.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                records.append({"kind": "agent_output", "text": _text_of(event)})
        return records

    # --- internals ---

    def _env(self, session: HarnessSession) -> dict[str, str]:
        """Point the agent's own SDK at the gateway.

        No patching, no wrapper: the agent believes it is talking to the provider, and
        every call is metered and recorded regardless of what it does.
        """
        return {
            "ANTHROPIC_BASE_URL": session.gateway_url,
            "ANTHROPIC_API_KEY": session.token,
            "ANTHROPIC_AUTH_TOKEN": session.token,
            "ANTHROPIC_MODEL": session.model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "IS_SANDBOX": "1",
        }

    def _flags(self) -> str:
        parts: list[str] = []
        for key, flag in CLI_FLAGS.items():
            value = self.kwargs.get(key)
            if value is None:
                continue
            parts.append(f"{flag} {value!r}" if " " in str(value) else f"{flag} {value}")
        return " ".join(parts)

    def _classify(self, stderr: str, exit_code: int) -> Exception:
        for needle, error_type in ERROR_PATTERNS:
            if needle.lower() in stderr.lower():
                return error_type(f"claude-code failed: {stderr[-500:]}")
        return AgentError(f"claude-code exited {exit_code}: {stderr[-500:]}")

    async def _read_text(self, sandbox: Sandbox, path: PurePosixPath) -> str:
        try:
            return (await sandbox.read_file(path)).decode("utf-8", "replace")
        except Exception:  # the file may not exist if the agent died early
            return ""


def _final_message(transcript: str) -> str | None:
    """The last thing the agent said, which is what a human reads first."""
    for line in reversed(transcript.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") in {"assistant", "result"}:
            return _text_of(event)
    return None


def _text_of(event: dict[str, Any]) -> str:
    message = event.get("message") or event
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(event.get("result", ""))

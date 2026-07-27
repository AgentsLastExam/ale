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
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ale.core.errors import AgentError, AgentRefusalError
from ale.core.harness import AgentRun, AutonomousHarness, HarnessSession, ResumeSupport
from ale.core.sandbox import Identity, Sandbox
from ale.run.tools import stage_desktop_bridge

__all__ = ["ClaudeCodeHarness"]

#: File names inside whatever workspace the session supplies. The directory itself is
#: not ours to choose: the agent runs unprivileged and can only write what it owns.
#: Both streams go here. Interleaved on purpose: when the CLI fails, the reason is often
#: the last thing it printed before the stream stopped, and two files lose that ordering.
TRANSCRIPT_NAME = "transcript.jsonl"


@dataclass(frozen=True)
class CliFlag:
    """One setting a run may pass through ``agent.kwargs``.

    Declarative because the same descriptor does three jobs: it builds the command line,
    documents what is configurable, and rejects a value the CLI would not accept. Borrowed
    from Harbor's integration, including the defaults — the important one being
    ``permission_mode``, whose absence is not a neutral choice.
    """

    name: str
    cli: str
    default: str | None = None
    choices: tuple[str, ...] = ()


CLI_FLAGS: tuple[CliFlag, ...] = (
    CliFlag("max_turns", "--max-turns"),
    CliFlag(
        "permission_mode",
        "--permission-mode",
        # The CLI checks permissions whether or not anything can answer. Headless, it
        # cannot prompt, so it refuses and says so — which is exactly what happened the
        # first time a real model ran: the model produced the right file and the right
        # contents, and the write was denied. An unset mode is a broken run, not a
        # cautious one.
        default="bypassPermissions",
        choices=("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"),
    ),
    CliFlag("allowed_tools", "--allowedTools"),
    CliFlag("disallowed_tools", "--disallowedTools"),
    CliFlag("system_prompt", "--append-system-prompt"),
    CliFlag("fallback_model", "--fallback-model"),
    CliFlag("max_budget_usd", "--max-budget-usd"),
)

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
        self.kwargs = kwargs
        self._resolved_version: str | None = None

        for flag in CLI_FLAGS:
            value = kwargs.get(flag.name)
            if value is not None and flag.choices and str(value) not in flag.choices:
                raise AgentError(f"{flag.name}={value!r} is not one of {', '.join(flag.choices)}")

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
        transcript_path = work_dir / TRANSCRIPT_NAME
        env = self._env(session)

        # A task that declared a desktop gets one it can actually drive. Without this an
        # autonomous agent can see a screen only by shelling out to whatever the image
        # happens to have, which is a different action space from the one the stepwise
        # family uses — and a task that scores differently depending on which family
        # attempted it is not measuring the thing it claims to.
        mcp_flags = ""
        if sandbox.request.needs_gui:
            config_path = await stage_desktop_bridge(sandbox, str(work_dir))
            # Only the config. The permission mode already decides what may be called,
            # and `--allowedTools` is a declared flag a run may set for itself — passing
            # it from here too would silently override what the run asked for.
            mcp_flags = f"--mcp-config {shlex.quote(config_path)}"

        # The CLI expects its configuration directory to exist, with these subdirectories
        # in place. It creates neither, and the failures are opaque when they are missing.
        await sandbox.exec(
            [
                "sh",
                "-c",
                'mkdir -p "$CLAUDE_CONFIG_DIR"/debug "$CLAUDE_CONFIG_DIR"/projects '
                '"$CLAUDE_CONFIG_DIR"/shell-snapshots "$CLAUDE_CONFIG_DIR"/statsig '
                '"$CLAUDE_CONFIG_DIR"/todos',
            ],
            env=env,
            identity=Identity.AGENT,
        )

        # The instruction travels in the environment, not on the command line and not
        # through a file. A command line is visible to every process in the sandbox, and a
        # file is one more thing to place somewhere the agent can read; an environment
        # variable read into a shell variable and then unset is visible to neither.
        prompt_var = f"ALE_PROMPT_{uuid.uuid4().hex}"
        command = (
            f'prompt="${prompt_var}"; unset {prompt_var}; '
            f'printf "%s" "$prompt" | '
            f"claude --verbose --output-format=stream-json {self._flags()} {mcp_flags} --print "
            f"> {transcript_path} 2>&1"
        )

        result = await sandbox.exec(
            ["bash", "-lc", command],
            cwd=str(work_dir),
            env={**env, prompt_var: instruction},
            timeout_sec=timeout_sec,
            # The thing being measured runs unprivileged, so it cannot change the
            # conditions of its own measurement.
            identity=Identity.AGENT,
        )

        transcript = await self._read_text(sandbox, transcript_path)
        if result.exit_code != 0:
            raise self._classify(transcript or result.stderr, result.exit_code)
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

        No patching, no wrapper: the agent believes it is talking to a provider, and every
        call is metered and recorded regardless of what it does.

        The alias variables are the part that is easy to miss. Claude Code does not use
        one model — it reaches for a small one to summarise, name a session or run a
        subagent, and asks for it by alias. Against Anthropic those aliases resolve. Against
        anything else they are model names the endpoint has never heard of, so the run dies
        partway through on a request nobody made deliberately. Pointing every alias at the
        one model a run declared is what makes a third-party endpoint usable at all.
        Borrowed from Harbor, which learned it the same way.
        """
        config_dir = PurePosixPath(session.work_dir) / "claude-config"
        env = {
            "ANTHROPIC_BASE_URL": session.gateway_url,
            "ANTHROPIC_API_KEY": session.token,
            "ANTHROPIC_AUTH_TOKEN": session.token,
            "ANTHROPIC_MODEL": session.model,
            # Telemetry has nowhere to go under a deny-all policy, and an agent retrying a
            # blocked request is an agent spending its budget on nothing.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # Without this the CLI refuses `bypassPermissions` outright in some
            # environments; it is the switch that says the isolation is the sandbox's job.
            "IS_SANDBOX": "1",
            # Somewhere writable that belongs to the agent. Left unset, the CLI writes to a
            # home directory it may not own.
            "CLAUDE_CONFIG_DIR": str(config_dir),
        }
        for alias in (
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env[alias] = session.model
        return env

    def _flags(self) -> str:
        parts: list[str] = []
        for flag in CLI_FLAGS:
            value = self.kwargs.get(flag.name, flag.default)
            if value is None:
                continue
            parts.append(f"{flag.cli}={shlex.quote(str(value))}")
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

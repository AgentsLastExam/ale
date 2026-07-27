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

#: The version a run gets unless it asks for another. A pin rather than "latest", because
#: an unpinned agent makes two runs incomparable for a reason that never appears in the
#: result. Bumping this is a deliberate, reviewable change; images need not be rebuilt for
#: it, since a mismatch installs the pinned build at the start of the episode.
DEFAULT_CLI_VERSION = "2.1.220"

#: Where a version this image did not bake gets installed, relative to the agent's home —
#: the account that runs it owns it, so no privilege is needed and none is granted.
CLI_PREFIX = ".local"


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
        self._prefix: str = ""
        self._path: str = ""

        for flag in CLI_FLAGS:
            value = kwargs.get(flag.name)
            if value is not None and flag.choices and str(value) not in flag.choices:
                raise AgentError(f"{flag.name}={value!r} is not one of {', '.join(flag.choices)}")

    def version(self) -> str:
        return self._resolved_version or self.cli_version or "unknown"

    def integrity(self) -> str:
        """What pins the binary. Always a version, never "whatever the image had"."""
        return f"npm:{self.cli_version or DEFAULT_CLI_VERSION}"

    async def install(self, sandbox: Sandbox) -> None:
        """Put the pinned CLI in place, whatever the image happened to ship.

        Three cases, and the middle one is the one that matters:

        * **Missing** — install it. An image is not required to bake an agent, and a run
          that fails because the image is not the one somebody assumed is a run that
          reports nothing about the model.
        * **Present but a different version** — install the pinned one anyway. This is
          what makes an experiment controlled: "claude-code" is not a version, and two
          runs a month apart against an image tagged ``latest`` are two different agents
          being compared as though they were one. It is also what lets the pin move
          without rebuilding every image.
        * **Present and already the pinned version** — do nothing, and cost no network.

        Installed under the agent's own ``~/.local`` and prepended to ``PATH`` so it wins
        over any baked copy. Prepending unconditionally rather than checking membership:
        the directory can already be on ``PATH`` but *behind* the one holding the stale
        copy, which looks identical until you read the version that actually ran.

        Follows ``agents-last-exam``'s deployer, which had already worked all of this out.
        """
        # Resolved rather than assumed: the home directory belongs to the image, which
        # declares the account but not where it lives.
        home = await sandbox.exec(["sh", "-c", "echo $HOME"], identity=Identity.AGENT)
        self._prefix = f"{home.stdout.strip() or '/home/user'}/{CLI_PREFIX}"
        self._path = f"{self._prefix}/bin:/usr/local/bin:/usr/bin:/bin"

        wanted = self.cli_version or DEFAULT_CLI_VERSION
        installed = await self._installed_version(sandbox)

        if installed != wanted:
            spec = f"@anthropic-ai/claude-code@{wanted}"
            # As the agent: it is the agent that runs this binary, and an install into
            # root's prefix is one the unprivileged account may not be able to execute.
            # `--force` so a same-version residue under the prefix is overwritten cleanly.
            result = await sandbox.exec(
                ["npm", "install", "-g", "--force", "--prefix", self._prefix, spec],
                env={"npm_config_cache": f"{self._prefix}/.npm-cache"},
                timeout_sec=900,  # a cold install on an image with no cache is minutes
                identity=Identity.AGENT,
            )
            if not result.ok:
                detail = (result.stderr or result.stdout)[-500:]
                if "EAI_AGAIN" in detail or "ENOTFOUND" in detail:
                    # The ordinary case, and worth naming: a sandbox has no egress but the
                    # gateway, so an image that does not carry the pinned build cannot
                    # obtain it. Both ways out are the operator's to choose.
                    raise AgentError(
                        f"this image has {installed or 'no claude'} and the run pinned "
                        f"{wanted}, but the sandbox has no network to install it. Either "
                        f"rebuild the image with CLAUDE_CODE_VERSION={wanted}, or run a "
                        f"task whose network policy reaches a registry."
                    )
                raise AgentError(f"could not install {spec}: {detail}")
            installed = await self._installed_version(sandbox)

        if installed != wanted:
            raise AgentError(
                f"the claude CLI reports {installed or 'nothing'} after installing "
                f"{wanted}; the run would not be measuring the agent it says it is"
            )
        self._resolved_version = installed

    async def _installed_version(self, sandbox: Sandbox) -> str | None:
        """What ``claude --version`` says, or ``None`` when there is no claude."""
        # Through a shell, so "there is no claude" is an exit code rather than an
        # exception: the guest service raises when it cannot find a binary at all, and
        # the missing case is the ordinary one here, not an error.
        probe = await sandbox.exec(
            ["sh", "-c", "command -v claude >/dev/null 2>&1 && claude --version"],
            env={"PATH": self._path},
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        if not probe.ok or not probe.stdout.strip():
            return None
        # "2.1.220 (Claude Code)" — the first field is the version.
        return probe.stdout.strip().split()[0]

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        home = PurePosixPath(session.home)
        transcript_path = home / TRANSCRIPT_NAME
        env = self._env(session)

        # Always, not only where a screen is expected. Which sandboxes have a desktop was
        # once a question the harness asked before staging these tools, and getting the
        # answer from the wrong place is what kept them from ever being staged. The tools
        # can answer it themselves: a screenshot in a sandbox with no desktop says so, and
        # an agent reads that as easily as it reads a missing tool.
        config_path = await stage_desktop_bridge(sandbox, str(home))
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
            cwd=str(home),
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
        config_dir = PurePosixPath(session.home) / ".claude-config"
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
            # The same search path `install` verified against. Without it the launch can
            # run a different binary from the one whose version was checked and recorded,
            # and provenance would name a build that never ran.
            "PATH": self._path or "/usr/local/bin:/usr/bin:/bin",
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

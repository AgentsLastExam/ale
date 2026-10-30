"""How every native command-line agent is run, and how its evidence is laid out.

Each invocation (a launch or a resume) appends to one stdout and one stderr log and
records, in ``invocations.jsonl``, the prompt it sent, the session it asked for and where
its output begins. Parsers read the same layout back from the Host mirror.
"""

from __future__ import annotations

import json
import shlex
from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, ClassVar, final

from pydantic import BaseModel, ValidationError

from ale.core.errors import (
    AgentError,
    AgentUnsupportedError,
    ConfigError,
    NativeContinuationError,
    PhaseTimeoutError,
    TaskError,
)
from ale.core.harness import (
    AgentRun,
    AutonomousHarness,
    EffectiveAgentResources,
    HarnessSession,
    NativeContinuation,
    ResumeSupport,
    TrajectoryParseContext,
)
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import OperatingSystem
from ale.run.harnesses._diagnostics import failure_detail
from ale.run.harnesses._npm import agent_path, bash_command, ensure_npm_cli
from ale.run.harnesses._release import ReleaseAssets, ensure_release_binary, release_identity
from ale.run.subscription import classify_subscription_error

LOG_ROOT = ".ale-harness"
PROMPT_PATH = f"{LOG_ROOT}/prompt.md"
STDOUT_PATH = f"{LOG_ROOT}/stdout.log"
STDERR_PATH = f"{LOG_ROOT}/stderr.log"
INVOCATIONS_PATH = f"{LOG_ROOT}/invocations.jsonl"
#: Sent by a resume without new task input. None of the headless CLIs can continue a
#: session without a message, so the request is explicit and recorded like any prompt.
CONTINUE_PROMPT = "Continue from where you left off."
#: Set on Windows for a CLI that takes its prompt as an argument: Git Bash would otherwise
#: rewrite a prompt that looks like a POSIX path before the native program sees it.
PROMPT_ARGUMENT_WINDOWS_ENV = {"MSYS2_ARG_CONV_EXCL": "*"}


def prompt_argument(prompt_path: str) -> tuple[str, str]:
    """Shell that reads the prompt file into ``$prompt``, and the word that passes it on.

    For a CLI that accepts a prompt only as an argument: the word is one argument expanded
    from the file's exact bytes and never re-quoted. The trailing ``.`` keeps the newlines
    command substitution would strip.
    """
    return (
        f'prompt="$(cat -- {shlex.quote(prompt_path)}; printf .)"; prompt="${{prompt%.}}"; ',
        '"$prompt"',
    )


@dataclass(frozen=True)
class CliCommand:
    """A POSIX shell command, its environment, and the session ID it asks for, if any."""

    script: str
    env: dict[str, str]
    session_id: str | None = None


@dataclass(frozen=True)
class NativeResult:
    """What one invocation's stdout reports about its own outcome."""

    session_id: str | None
    final_message: str | None
    error: str | None = None


@dataclass(frozen=True)
class Invocation:
    """One recorded launch or resume and the stdout it produced."""

    prompt: str
    session_id: str | None
    stdout: str


class CliHarness(AutonomousHarness):
    """Run a pinned CLI identically for every adapter; native behavior stays explicit.

    The program comes from its official npm package, or, when ``release_assets`` is set,
    from its official release binary for the Sandbox's OS and architecture, accepted only
    when its SHA-256 matches the pinned one.

    The prompt reaches the CLI as a file, through stdin or a native prompt-file option,
    so its bytes never pass through argv, an environment variable or shell quoting. The
    CLI starts from a non-login shell on Linux and Windows alike, so profile scripts do
    not change the measured program's environment.
    """

    package: ClassVar[str]
    """The official program: its npm package, or the upstream source of its releases."""
    binary: ClassVar[str]
    default_version: ClassVar[str]
    release_assets: ClassVar[ReleaseAssets] = {}
    """Pinned release executables of ``default_version`` by OS and architecture; empty
    installs from npm. Such an adapter supports exactly the systems this table covers."""
    settings_type: ClassVar[type[BaseModel]]
    settings: BaseModel

    supported_os = (OperatingSystem.LINUX, OperatingSystem.WINDOWS)
    resume_support = ResumeSupport.NATIVE

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if self.release_assets and cli_version not in (None, self.default_version):
            # A digest pins one build; another version would run unverified bytes.
            raise ConfigError(
                f"{self.name} pins release digests for {self.default_version} only, "
                f"not {cli_version}"
            )
        self.cli_version = cli_version
        try:
            self.settings = (
                settings
                if isinstance(settings, self.settings_type)
                else self.settings_type.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid {self.name} settings: {exc}") from exc

    def version(self) -> str:
        return self.cli_version or self.default_version

    def integrity(self) -> str:
        if self.release_assets:
            return release_identity(self.release_assets)
        return f"npm:{self.package}@{self.version()}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        """Every CLI here supports Skills and both canonical MCP transports."""
        return None

    async def install(self, sandbox: Sandbox) -> str:
        if self.release_assets:
            return await ensure_release_binary(
                sandbox, binary=self.binary, version=self.version(), assets=self.release_assets
            )
        return await ensure_npm_cli(
            sandbox, package=self.package, binary=self.binary, version=self.version()
        )

    @abstractmethod
    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        """Whether a home-relative path belongs to exactly this native session."""

    @abstractmethod
    async def prepare_command(
        self,
        prompt_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        """The command for one invocation; ``native_session_id`` is set on resume."""

    @abstractmethod
    def read_result(self, stdout: str) -> NativeResult:
        """Interpret one invocation's stdout."""

    def classify_failure(self, detail: str) -> Exception:
        """The typed error for a failed invocation; adapters add native limits."""
        return AgentError(detail)

    @final
    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        return await self._invoke(instruction, sandbox, session, None, timeout_sec)

    @final
    async def resume(
        self,
        instruction: str | None,
        continuation: NativeContinuation,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        if self.resume_support is not ResumeSupport.NATIVE:
            raise AgentUnsupportedError(f"{self.name} does not support native resume")
        if continuation.harness != self.name:
            raise NativeContinuationError(
                f"continuation belongs to {continuation.harness}, not {self.name}"
            )
        if not await self._session_exists(sandbox, session, continuation.native_session_id):
            raise NativeContinuationError(
                f"native {self.name} session {continuation.native_session_id!r} is absent"
            )
        return await self._invoke(
            CONTINUE_PROMPT if instruction is None else instruction,
            sandbox,
            session,
            continuation.native_session_id,
            timeout_sec,
        )

    async def _session_exists(
        self, sandbox: Sandbox, session: HarnessSession, session_id: str
    ) -> bool:
        home = agent_path(session.home)
        for declared in self.logs:
            for file in await sandbox.list_files(str(home / declared)):
                if self.is_session_file(agent_path(file.path).relative_to(home), session_id):
                    return True
        return False

    async def _invoke(
        self,
        prompt: str,
        sandbox: Sandbox,
        session: HarnessSession,
        native_session_id: str | None,
        timeout_sec: float,
    ) -> AgentRun:
        home = agent_path(session.home)
        prompt_path, stdout_path, stderr_path, invocations_path = (
            str(home / name) for name in (PROMPT_PATH, STDOUT_PATH, STDERR_PATH, INVOCATIONS_PATH)
        )
        # As the agent, which owns the log directory and appends to its streams.
        prepare = f"mkdir -p -- {shlex.quote(str(home / LOG_ROOT))}"
        if native_session_id is None:
            prepare += "".join(
                f" && : > {shlex.quote(path)}"
                for path in (stdout_path, stderr_path, invocations_path)
            )
        prepared = await sandbox.exec(
            bash_command(session.home, prepare, login=False), identity=Identity.AGENT
        )
        if not prepared.ok:
            raise AgentError(f"could not prepare {self.name} logs: {prepared.stderr.strip()}")
        await sandbox.write_file(prompt_path, prompt.encode("utf-8"), identity=Identity.AGENT)
        command = await self.prepare_command(
            prompt_path, sandbox, session, native_session_id=native_session_id
        )
        sizes = {
            str(agent_path(file.path)): file.size
            for file in await sandbox.list_files(str(home / LOG_ROOT))
        }
        stdout_offset, stderr_offset = sizes.get(stdout_path, 0), sizes.get(stderr_path, 0)
        record = {
            "prompt": prompt,
            "session_id": command.session_id,
            "stdout_offset": stdout_offset,
            "stderr_offset": stderr_offset,
        }
        await sandbox.write_file(
            invocations_path,
            await sandbox.read_file(invocations_path)
            + json.dumps(record, ensure_ascii=False).encode("utf-8")
            + b"\n",
            identity=Identity.AGENT,
        )
        result = await sandbox.exec(
            bash_command(
                session.home,
                f"{{ {command.script}; }} >> {shlex.quote(stdout_path)} "
                f"2>> {shlex.quote(stderr_path)}",
                login=False,
            ),
            cwd=str(agent_path(session.start_dir)),
            env=command.env,
            identity=Identity.AGENT,
            timeout_sec=timeout_sec,
        )
        if result.timed_out:
            raise PhaseTimeoutError("agent", timeout_sec)
        stdout = (await sandbox.read_file(stdout_path, offset=stdout_offset)).decode(
            "utf-8", "replace"
        )
        # A shell that could not start reports through the exec result, not the log.
        stderr = (await sandbox.read_file(stderr_path, offset=stderr_offset)).decode(
            "utf-8", "replace"
        ) or result.stderr
        native = self.read_result(stdout)
        if result.exit_code != 0 or native.error:
            detail = failure_detail(
                self.name,
                result.exit_code,
                native=native.error or "",
                stderr=stderr,
                stdout=stdout,
                token=session.token,
            )
            if session.authentication == "subscription":
                raise classify_subscription_error(self.name, detail)
            raise self.classify_failure(detail)
        if command.session_id is not None and native.session_id != command.session_id:
            raise NativeContinuationError(f"{self.name} did not confirm the requested session ID")
        if not native.session_id:
            raise NativeContinuationError(f"{self.name} did not report a native session ID")
        return AgentRun(
            exit_code=0,
            final_message=native.final_message,
            continuation=NativeContinuation(harness=self.name, native_session_id=native.session_id),
        )


async def install_skills(
    sandbox: Sandbox,
    session: HarnessSession,
    resources: EffectiveAgentResources,
    skills_root: PurePath,
) -> None:
    """Fill the agent's skills directory: the image's Skills, then the operator's.

    An operator's Skill replaces the image's Skill of its name, as in Harbor.
    """
    if resources.skills_dir is not None:
        root = shlex.quote(str(skills_root))
        copy = f"mkdir -p -- {root} && cp -R -- {shlex.quote(resources.skills_dir + '/.')} {root}"
        if resources.skills:
            replaced = (shlex.quote(str(skills_root / skill.name)) for skill in resources.skills)
            copy += f" && rm -rf -- {' '.join(replaced)}"
        copied = await sandbox.exec(
            bash_command(session.home, copy, login=False), identity=Identity.AGENT
        )
        if not copied.ok:
            raise TaskError(
                f"could not copy [environment].skills_dir {resources.skills_dir}: "
                + (copied.stderr.strip() or f"exit {copied.exit_code}")
            )
    for skill in resources.skills:
        target = skills_root / skill.name
        await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
        if skill.executable_files:
            await sandbox.exec(
                ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                identity=Identity.AGENT,
            )


# --- reading the mirrored evidence back ---


def invocations(context: TrajectoryParseContext) -> list[Invocation]:
    """Each launch and resume in order, with its prompt and its part of stdout.

    Without a readable record, all of stdout belongs to the launch, which always sends the
    Task instruction.
    """
    records: list[dict[str, Any]] = []
    path = context.logs_dir / INVOCATIONS_PATH
    for line in path.read_bytes().splitlines(keepends=True) if path.is_file() else ():
        if not line.endswith(b"\n"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(value, dict)
            and isinstance(value.get("prompt"), str)
            and isinstance(value.get("stdout_offset"), int)
        ):
            records.append(value)
    stdout_path = context.logs_dir / STDOUT_PATH
    data = stdout_path.read_bytes() if stdout_path.is_file() else b""
    if not records:
        records = [{"prompt": context.instruction, "stdout_offset": 0}]
    starts = [min(max(record["stdout_offset"], 0), len(data)) for record in records]
    ends = [*starts[1:], len(data)]
    return [
        Invocation(
            prompt=record["prompt"],
            session_id=(
                record["session_id"] if isinstance(record.get("session_id"), str) else None
            ),
            stdout=data[start : max(start, end)].decode("utf-8", "replace"),
        )
        for record, start, end in zip(records, starts, ends, strict=True)
    ]


def requested_session_id(context: TrajectoryParseContext, recorded: list[Invocation]) -> str | None:
    """The session a finished run confirmed, else the last one an invocation asked for."""
    if context.session_id:
        return context.session_id
    return next((item.session_id for item in reversed(recorded) if item.session_id), None)


def json_lines(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """JSON objects one per line, plus issues for lines that are not."""
    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("expected a JSON object")
            events.append(event)
        except ValueError as exc:
            issues.append({"line": number, "reason": "malformed_json", "detail": str(exc)[:500]})
    return events, issues


def stdout_events(
    recorded: list[Invocation],
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Each invocation's stdout events."""
    events: list[list[dict[str, Any]]] = []
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(recorded):
        parsed, problems = json_lines(item.stdout)
        events.append(parsed)
        issues.extend({"invocation": index + 1, **problem} for problem in problems)
    return events, issues


def native_events(
    context: TrajectoryParseContext, path: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A mirrored native JSONL file's events, with issues naming the file."""
    events, issues = json_lines(path.read_text(encoding="utf-8", errors="replace"))
    source = path.relative_to(context.logs_dir).as_posix()
    return events, [{"source": source, **issue} for issue in issues]


def mirrored_files(context: TrajectoryParseContext, root: str) -> list[Path]:
    """Every mirrored file below one declared log root."""
    base = context.logs_dir / root
    return sorted(path for path in base.rglob("*") if path.is_file()) if base.is_dir() else []


def unconverted(context: TrajectoryParseContext, paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Name mirrored session files a parser did not use; they stay as raw evidence."""
    paths = sorted(paths)
    if not paths:
        return []
    return [
        {
            "reason": "unconverted_native_files",
            "count": len(paths),
            "sources": [path.relative_to(context.logs_dir).as_posix() for path in paths[:10]],
        }
    ]

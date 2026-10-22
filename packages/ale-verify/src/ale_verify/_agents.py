"""Small local adapters for Agent-as-a-Judge."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import _llm
from ._io import JudgeError, digest, endpoint_identity, read_text_evidence, redact, sanitize
from ._journal import journal
from ._json import response_object
from ._records import EvidenceReference, JudgeAttempt, JudgeInvocation

#: Installing an exact Judge CLI is setup rather than judging, so it keeps a bound.
_INSTALL_TIMEOUT_SECONDS = 600


def run(
    *,
    invocation_id: str,
    name: str,
    prompt: str,
    output_schema: dict[str, Any] | None,
    evidence: Sequence[tuple[str | bytes, EvidenceReference]],
    config: Mapping[str, str],
    mcp_servers: Sequence[Mapping[str, object]] = (),
) -> tuple[dict[str, Any], JudgeInvocation]:
    adapter = config["adapter"]
    endpoint = endpoint_identity(config["base_url"])
    rendered = _prompt(name, prompt, output_schema, evidence)
    prompt_hash = digest(rendered)
    schema_hash = digest(json.dumps(output_schema, sort_keys=True))
    secret = os.environ.get(config["api_key_env"], "")

    if not _elevated():
        _preflight_failure(
            invocation_id,
            name,
            config,
            endpoint,
            prompt_hash,
            schema_hash,
            "Agent Judge requires framework privileges",
        )
    if not secret:
        _preflight_failure(
            invocation_id,
            name,
            config,
            endpoint,
            prompt_hash,
            schema_hash,
            f"{config['api_key_env']} is not set for the Agent Judge",
        )

    workspace = Path(os.environ.get("ALE_HOME", ""))
    if not workspace.is_absolute() or not workspace.is_dir():
        _preflight_failure(
            invocation_id,
            name,
            config,
            endpoint,
            prompt_hash,
            schema_hash,
            "ALE_HOME must identify the completed solver workspace",
        )
    stage = Path(os.environ.get("ALE_STAGE_DIR", "/opt/ale/verify"))
    home = stage / "agent" / invocation_id
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, mode=0o700)

    try:
        binary, version = _ensure_binary(adapter, config["version"], stage)
        mcp_config = _write_mcp_config(adapter, home, mcp_servers)
    except JudgeError as exc:
        _preflight_failure(
            invocation_id,
            name,
            config,
            endpoint,
            prompt_hash,
            schema_hash,
            sanitize(exc, (secret,)),
        )
    environment = _environment(adapter, home, config, secret)
    if not any(reference.kind == "solver_trajectory" for _, reference in evidence):
        environment.pop("ALE_TRAJECTORY_PATH", None)
    attempts: list[JudgeAttempt] = []
    session_id: str | None = None
    repair_error = ""
    repair = False
    repairs = 0
    failures = 0
    index = 0
    while True:
        index += 1
        mode: Literal["initial", "retry", "schema_repair"] = (
            "initial" if index == 1 else "schema_repair" if repair else "retry"
        )
        current_prompt = _repair_prompt(rendered, repair_error) if repair else rendered
        # A Judge run is bounded only by what is left of verification, never by a
        # per-attempt ceiling: a long, thorough inspection is exactly what it is for.
        left = _llm._time_left()
        if index > 1 and left is not None and left <= 0:
            break
        if adapter == "claude-code" and session_id is None:
            session_id = str(uuid.uuid4())
        argv = _command(
            adapter,
            binary,
            config,
            session_id=session_id,
            resume=repair,
            mcp_config=mcp_config,
        )
        started = _now()
        journal(
            "agent",
            {
                "event": "request",
                "id": invocation_id,
                "attempt": index,
                "prompt": current_prompt,
                "schema": output_schema,
                "model": config["model"],
                "reasoning_effort": config["reasoning_effort"],
                "endpoint_identity": endpoint,
            },
            (secret,),
        )
        try:
            completed = subprocess.run(
                argv,
                input=current_prompt,
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                # Never past the margin verification keeps for writing its verdict.
                timeout=None if left is None else max(left, 1.0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout or ""
            )
            stderr = (
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr or ""
            )
            _append_transcript(
                invocation_id=invocation_id,
                index=index,
                argv=argv,
                stdout=stdout,
                stderr=stderr,
                secrets=(secret,),
            )
            error = repair_error = "Agent Judge timed out: " + _failure_detail(
                adapter, None, stdout, stderr, secret
            )
            attempts.append(
                _attempt(
                    index,
                    mode,
                    "timed_out",
                    config,
                    endpoint,
                    prompt_hash,
                    schema_hash,
                    started,
                    session_id,
                    error,
                )
            )
            session_id = None
            repair = False
            failures += 1
            if not _llm._wait_to_retry(failures):
                break
            continue
        except OSError as exc:
            error = repair_error = sanitize(f"Agent Judge could not start: {exc}", (secret,))
            attempts.append(
                _attempt(
                    index,
                    mode,
                    "failed",
                    config,
                    endpoint,
                    prompt_hash,
                    schema_hash,
                    started,
                    session_id,
                    error,
                )
            )
            break

        _append_transcript(
            invocation_id=invocation_id,
            index=index,
            argv=argv,
            stdout=completed.stdout,
            stderr=completed.stderr,
            secrets=(secret,),
        )
        observed_session, final = _final_response(adapter, completed.stdout)
        if completed.returncode != 0 or _native_error(adapter, completed.stdout):
            session_id = observed_session or session_id
            repair_error = _failure_detail(
                adapter, completed.returncode, completed.stdout, completed.stderr, secret
            )
            attempts.append(
                _attempt(
                    index,
                    mode,
                    "failed",
                    config,
                    endpoint,
                    prompt_hash,
                    schema_hash,
                    started,
                    session_id,
                    repair_error,
                )
            )
            session_id = None
            repair = False
            failures += 1
            if not _llm._wait_to_retry(failures):
                break
            continue

        if adapter == "codex-cli":
            if session_id is None:
                session_id = observed_session
            elif observed_session != session_id:
                repair_error = "Codex resumed a different native session"
                attempts.append(
                    _attempt(
                        index,
                        mode,
                        "failed",
                        config,
                        endpoint,
                        prompt_hash,
                        schema_hash,
                        started,
                        session_id,
                        repair_error,
                    )
                )
                break
        elif observed_session != session_id:
            repair_error = "Claude did not confirm the requested native session"
            attempts.append(
                _attempt(
                    index,
                    mode,
                    "failed",
                    config,
                    endpoint,
                    prompt_hash,
                    schema_hash,
                    started,
                    session_id,
                    repair_error,
                )
            )
            break

        try:
            if not session_id:
                raise ValueError(f"{adapter} did not report a native session")
            if final is None:
                raise ValueError(f"{adapter} emitted no identifiable final response")
            result = response_object(final, output_schema)
            if secret in json.dumps(result, ensure_ascii=False):
                raise ValueError("Judge response contains a credential")
        except ValueError as exc:
            repair_error = sanitize(exc, (secret,))
            attempts.append(
                _attempt(
                    index,
                    mode,
                    "invalid",
                    config,
                    endpoint,
                    prompt_hash,
                    schema_hash,
                    started,
                    session_id,
                    repair_error,
                )
            )
            if not session_id or repairs >= _llm.REPAIRS:
                break
            repairs += 1
            repair = True
            continue

        attempts.append(
            _attempt(
                index,
                mode,
                "completed",
                config,
                endpoint,
                prompt_hash,
                schema_hash,
                started,
                session_id,
                None,
            )
        )
        return (
            result,
            JudgeInvocation(
                id=invocation_id,
                kind="agent",
                name=name,
                adapter=adapter,  # type: ignore[arg-type]
                adapter_version=version,
                status="completed",
                attempts=tuple(attempts),
            ),
        )

    failure = (
        f"Agent Judge produced no valid verdict after {len(attempts)} attempt(s): "
        f"{repair_error or attempts[-1].error or 'unknown failure'}"
    )
    invocation = JudgeInvocation(
        id=invocation_id,
        kind="agent",
        name=name,
        adapter=adapter,  # type: ignore[arg-type]
        adapter_version=version,
        status="failed",
        attempts=tuple(attempts),
        failure=failure,
    )
    raise JudgeError(failure, invocation)


def _command(
    adapter: str,
    binary: str,
    config: Mapping[str, str],
    *,
    session_id: str | None,
    resume: bool,
    mcp_config: Path | None,
) -> list[str]:
    if adapter == "codex-cli":
        argv = [
            binary,
            "exec",
            "--json",
            "--model",
            config["model"],
            "-c",
            f'model_reasoning_effort="{config["reasoning_effort"]}"',
            "-c",
            'model_provider="ale_verify"',
            "-c",
            'model_providers.ale_verify.name="ALE Verify"',
            "-c",
            f'model_providers.ale_verify.base_url="{_responses_base_url(config["base_url"])}"',
            "-c",
            'model_providers.ale_verify.env_key="OPENAI_API_KEY"',
            "-c",
            'model_providers.ale_verify.wire_api="responses"',
            "-c",
            "model_providers.ale_verify.supports_websockets=false",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if resume:
            if not session_id:
                raise ValueError("Codex repair requires a native session")
            argv.extend(["resume", session_id])
        argv.append("-")
        return argv
    selector = ["--resume", session_id] if resume else ["--session-id", session_id]
    argv = [
        binary,
        "--verbose",
        "--output-format=stream-json",
        "--model",
        config["model"],
        "--effort",
        config["reasoning_effort"],
        "--dangerously-skip-permissions",
        *[str(value) for value in selector],
    ]
    if mcp_config is not None:
        argv.extend(["--mcp-config", str(mcp_config), "--strict-mcp-config"])
    argv.append("--print")
    return argv


def _responses_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/responses"):
        base = base.removesuffix("/responses")
    return base if base.endswith("/v1") else base + "/v1"


def _environment(
    adapter: str,
    home: Path,
    config: Mapping[str, str],
    secret: str,
) -> dict[str, str]:
    environment = {
        **os.environ,
        "HOME": str(home),
        config["api_key_env"]: secret,
        "NO_COLOR": "1",
    }
    if adapter == "codex-cli":
        codex_home = home / ".codex"
        codex_home.mkdir(mode=0o700, exist_ok=True)
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "OPENAI_API_KEY": secret,
                "OPENAI_BASE_URL": config["base_url"],
            }
        )
    else:
        environment.update(
            {
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "ANTHROPIC_API_KEY": secret,
                "ANTHROPIC_AUTH_TOKEN": secret,
                "ANTHROPIC_BASE_URL": config["base_url"],
                "ANTHROPIC_MODEL": config["model"],
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "IS_SANDBOX": "1",
            }
        )
    return environment


def _prompt(
    name: str,
    prompt: str,
    output_schema: dict[str, Any] | None,
    evidence: Sequence[tuple[str | bytes, EvidenceReference]],
) -> str:
    instruction_path = os.environ.get("ALE_TASK_INSTRUCTION_PATH")
    instruction = ""
    if instruction_path:
        instruction, _ = read_text_evidence(instruction_path)
    resources = {
        key: os.environ.get(key)
        for key in (
            "ALE_HOME",
            "ALE_TASK_INSTRUCTION_PATH",
            "ALE_TASK_PARAMETERS_PATH",
            "ALE_TRAJECTORY_PATH",
        )
        if os.environ.get(key)
    }
    if not any(reference.kind == "solver_trajectory" for _, reference in evidence):
        resources.pop("ALE_TRAJECTORY_PATH", None)
    evidence_lines = "\n".join(
        f"- {reference.kind}: {reference.location or 'inline'} ({reference.sha256})"
        + (f"\n{content}" if reference.location is None else "")
        for content, reference in evidence
    )
    return (
        "You are a verification agent with elevated privileges and the completed workspace.\n"
        "Inspect files and execute commands needed to reach an empirical verdict.\n\n"
        f"Task instruction:\n{instruction}\n\n"
        f"{name}: {prompt}\n\n"
        f"Output JSON Schema: {json.dumps(output_schema or {'type': 'object'})}\n\n"
        f"Framework resources: {json.dumps(resources, sort_keys=True)}\n"
        f"Selected evidence:\n{evidence_lines}\n\n"
        "Return one JSON object, with no Markdown."
    )


def _repair_prompt(original: str, error: str) -> str:
    return _llm._repair_prompt(original, error) + (
        "\nDo not repeat workspace mutations unless needed to inspect their current result."
    )


def _elevated() -> bool:
    if os.name == "nt":
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def _final_response(adapter: str, transcript: str) -> tuple[str | None, str | None]:
    session_id = None
    final = None
    for event in _stream_events(transcript):
        if adapter == "codex-cli":
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                session_id = event["thread_id"]
            item = event.get("item")
            if (
                event.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
            ):
                final = str(item.get("text") or "")
        elif event.get("type") == "result":
            if isinstance(event.get("session_id"), str):
                session_id = event["session_id"]
            if isinstance(event.get("result"), str):
                final = event["result"]
    return session_id, final


def _stream_events(transcript: str) -> list[dict[str, object]]:
    events = []
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _native_error(adapter: str, transcript: str) -> str | None:
    failure = None
    for event in _stream_events(transcript):
        kind = event.get("type")
        if adapter == "codex-cli":
            if kind in {"thread.started", "turn.started", "turn.completed"}:
                failure = None
            elif kind in {"error", "turn.failed"}:
                failure = _error_text(event.get("error") or event.get("message")) or str(kind)
        elif kind == "result":
            subtype = str(event.get("subtype") or "")
            detail = _error_text(event.get("errors") or event.get("error") or event.get("result"))
            failure = (
                f"{subtype or 'native error'}: {detail}".rstrip(": ")
                if event.get("is_error") or subtype.startswith("error")
                else None
            )
        elif kind == "error" or (kind == "assistant" and event.get("error")):
            failure = _error_text(event.get("error") or event.get("message")) or "native error"
    return failure


def _error_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(filter(None, (_error_text(item) for item in value)))
    if isinstance(value, dict):
        return "; ".join(
            filter(
                None,
                (
                    _error_text(value.get(key))
                    for key in ("message", "detail", "error", "errors", "code", "type")
                ),
            )
        )
    return ""


def _failure_detail(
    adapter: str, exit_code: int | None, stdout: str, stderr: str, secret: str
) -> str:
    native = _native_error(adapter, stdout)
    parts = [f"{adapter} failed (exit code {exit_code})" if exit_code is not None else adapter]
    if native:
        parts.append("native: " + redact(native, (secret,))[:1400])
    if stderr.strip():
        parts.append("stderr: " + redact(stderr.strip(), (secret,))[-400:])
    if not native:
        lines = []
        for line in stdout.splitlines():
            try:
                json.loads(line)
            except json.JSONDecodeError:
                lines.append(line)
        if lines:
            parts.append("stdout: " + redact("\n".join(lines), (secret,))[-400:])
    return "; ".join(parts)


def _version(binary: str) -> str:
    result = subprocess.run(
        [binary, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise JudgeError(f"could not query {Path(binary).name} version")
    output = (result.stdout or result.stderr).strip()
    match = re.search(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])", output)
    if match is None:
        raise JudgeError(f"{Path(binary).name} reported no version")
    return match.group(1)


def _ensure_binary(adapter: str, expected: str, stage: Path) -> tuple[str, str]:
    binary_name = "codex" if adapter == "codex-cli" else "claude"
    binary = shutil.which(binary_name)
    if binary is not None:
        try:
            if _version(binary) == expected:
                return binary, expected
        except JudgeError:
            pass
    npm = shutil.which("npm")
    if npm is None:
        raise JudgeError(
            f"{binary_name} {expected} is unavailable and npm is not installed "
            "for exact installation"
        )
    root = stage / "agent-tools" / f"{adapter}-{expected}"
    package = "@openai/codex" if adapter == "codex-cli" else "@anthropic-ai/claude-code"
    completed = subprocess.run(
        [npm, "install", "--prefix", str(root), "--no-audit", "--no-fund", f"{package}@{expected}"],
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        check=False,
    )
    installed = root / "node_modules" / ".bin" / binary_name
    if completed.returncode != 0 or not installed.is_file():
        raise JudgeError(
            f"could not install exact Agent Judge {adapter} {expected}: "
            f"{(completed.stderr or completed.stdout)[-500:]}"
        )
    observed = _version(str(installed))
    if observed != expected:
        raise JudgeError(f"installed Agent Judge version {observed}, expected {expected}")
    return str(installed), observed


def _write_mcp_config(
    adapter: str,
    home: Path,
    servers: Sequence[Mapping[str, object]],
) -> Path | None:
    if not servers:
        return None
    normalized: dict[str, dict[str, object]] = {}
    for server in servers:
        name = server.get("name")
        transport = server.get("transport")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise JudgeError("Agent Judge MCP name is invalid")
        if name in normalized:
            raise JudgeError(f"duplicate Agent Judge MCP name: {name}")
        if transport == "stdio":
            command = server.get("command")
            args = server.get("args", ())
            cwd = server.get("cwd")
            environment = server.get("environment", {})
            if not isinstance(command, str) or not command:
                raise JudgeError(f"stdio MCP {name} requires command")
            if not isinstance(args, (list, tuple)) or not all(isinstance(v, str) for v in args):
                raise JudgeError(f"stdio MCP {name} args must be strings")
            if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
                raise JudgeError(f"stdio MCP {name} cwd must be absolute")
            if not isinstance(environment, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in environment.items()
            ):
                raise JudgeError(f"stdio MCP {name} environment must contain strings")
            normalized[name] = {
                "transport": "stdio",
                "command": command,
                "args": list(args),
                "cwd": cwd,
                "environment": environment,
            }
        elif transport == "streamable-http":
            url = server.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise JudgeError(f"HTTP MCP {name} requires an absolute URL")
            normalized[name] = {"transport": "streamable-http", "url": url}
        else:
            raise JudgeError(f"Agent Judge MCP {name!r} uses unsupported transport {transport!r}")

    if adapter == "claude-code":
        path = home / "mcp.json"
        payload = {"mcpServers": {}}
        for name, server in normalized.items():
            if server["transport"] == "stdio":
                # Claude Code reads a stdio server's variables from "env".
                native: dict[str, object] = {"command": server["command"], "args": server["args"]}
                if server["cwd"] is not None:
                    native["cwd"] = server["cwd"]
                if server["environment"]:
                    native["env"] = server["environment"]
                payload["mcpServers"][name] = native  # type: ignore[index]
            else:
                payload["mcpServers"][name] = {"type": "http", "url": server["url"]}  # type: ignore[index]
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        return path

    codex_home = home / ".codex"
    codex_home.mkdir(exist_ok=True)
    lines: list[str] = []
    for name, server in normalized.items():
        lines.append(f"[mcp_servers.{name}]")
        if server["transport"] == "stdio":
            lines.append(f"command = {json.dumps(server['command'])}")
            lines.append(f"args = {json.dumps(server['args'])}")
            if server["cwd"] is not None:
                lines.append(f"cwd = {json.dumps(server['cwd'])}")
            environment = server["environment"]
            if environment:
                pairs = ", ".join(
                    f"{json.dumps(key)} = {json.dumps(value)}"
                    for key, value in sorted(environment.items())  # type: ignore[union-attr]
                )
                lines.append(f"env = {{{pairs}}}")
        else:
            lines.append(f"url = {json.dumps(server['url'])}")
    (codex_home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return None


def _append_transcript(
    *,
    invocation_id: str,
    index: int,
    argv: Sequence[str],
    stdout: str,
    stderr: str,
    secrets: tuple[str, ...],
) -> None:
    record = {
        "invocation_id": invocation_id,
        "attempt": index,
        "argv": [Path(argv[0]).name, *argv[1:]],
        "stdout": redact(stdout, secrets),
        "stderr": redact(stderr, secrets),
    }
    journal("agent", {"event": "response", "id": invocation_id, **record}, secrets)


def _attempt(
    index: int,
    mode: Literal["initial", "retry", "schema_repair"],
    outcome: str,
    config: Mapping[str, str],
    endpoint: str,
    prompt_hash: str,
    schema_hash: str,
    started_at: str,
    session_id: str | None,
    error: str | None,
) -> JudgeAttempt:
    return JudgeAttempt(
        index=index,
        mode=mode,
        started_at=started_at,
        finished_at=_now(),
        outcome=outcome,  # type: ignore[arg-type]
        model=config["model"],
        reasoning_effort=config["reasoning_effort"],
        endpoint_identity=endpoint,
        prompt_hash=prompt_hash,
        schema_hash=schema_hash,
        request_id=session_id,
        error=error,
    )


def _preflight_failure(
    invocation_id: str,
    name: str,
    config: Mapping[str, str],
    endpoint: str,
    prompt_hash: str,
    schema_hash: str,
    failure: str,
) -> None:
    now = _now()
    invocation = JudgeInvocation(
        id=invocation_id,
        kind="agent",
        name=name,
        adapter=config["adapter"],  # type: ignore[arg-type]
        status="failed",
        attempts=(
            JudgeAttempt(
                index=1,
                mode="initial",
                started_at=now,
                finished_at=now,
                outcome="failed",
                model=config["model"],
                reasoning_effort=config["reasoning_effort"],
                endpoint_identity=endpoint,
                prompt_hash=prompt_hash,
                schema_hash=schema_hash,
                error=failure,
            ),
        ),
        failure=failure,
    )
    raise JudgeError(failure, invocation)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

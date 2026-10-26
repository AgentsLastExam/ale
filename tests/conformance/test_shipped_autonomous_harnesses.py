"""Every shipped native CLI adapter against the rules ``CliHarness`` cannot enforce alone.

Adapters are discovered, so a new CLI is held to the same checks as soon as it exists.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path, PurePosixPath

import pytest

import ale.run.cli.tasks  # noqa: F401  (imports every shipped adapter)
from ale.core.harness import HarnessSession, ResumeSupport, TrajectoryParseContext
from ale.core.sandbox import ExecResult
from ale.core.taskspec import OperatingSystem
from ale.core.testkit import AutonomousHarnessConformance
from ale.run.harnesses._release import release_identity
from ale.run.harnesses.base import LOG_ROOT, PROMPT_PATH, STDOUT_PATH, CliHarness, NativeResult
from ale.run.recording import BlobStore

pytestmark = pytest.mark.conformance


def _adapters(base: type[CliHarness]) -> list[type[CliHarness]]:
    found = []
    for child in base.__subclasses__():
        if child.__module__.startswith("ale.run.harnesses."):
            found.append(child)
        found.extend(_adapters(child))
    return found


HARNESSES = [adapter() for adapter in _adapters(CliHarness)]
PROMPT = "Unique prompt: 多行\n'quoted' $HOME `cmd`"


class PathSandbox:
    """Answers only what command preparation may ask: the agent's PATH."""

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        return ExecResult(exit_code=0, stdout="/usr/bin:/bin")


def session(**updates: object) -> HarnessSession:
    values: dict[str, object] = {
        "episode_id": "episode",
        "gateway_url": "http://gateway",
        "token": "token",
        "model": "model",
        "sandbox_id": "sandbox",
    }
    return HarnessSession(**(values | updates))


def context(tmp_path: Path, **updates: object) -> TrajectoryParseContext:
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "trajectory",
        "instruction": "Do the task.",
        "logs_dir": tmp_path,
        "model": "model",
        "agent_version": "1",
        "blobs": BlobStore(tmp_path),
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def test_every_shipped_cli_is_discovered() -> None:
    assert {harness.name for harness in HARNESSES} >= {
        "claude-code",
        "codex-cli",
        "grok-build",
        "openclaw-cli",
    }


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda harness: harness.name)
def test_definition_is_strict_pinned_and_declares_its_evidence(harness: CliHarness) -> None:
    AutonomousHarnessConformance.check_definition(harness)
    AutonomousHarnessConformance.check_empty_resources(harness)
    assert harness.settings_type.model_config.get("extra") == "forbid"
    assert harness.settings_type.model_config.get("frozen") is True
    if harness.release_assets:
        # A release adapter runs only digest-pinned builds, on exactly the systems they cover.
        assert harness.integrity() == release_identity(harness.release_assets)
        assert set(harness.supported_os) == {system for system, _ in harness.release_assets}
    else:
        # The npm package, or what an adapter installs from upstream's own installer: its
        # source at one commit, or the digest of the files it unpacks and runs.
        assert harness.integrity() == f"npm:{harness.package}@{harness.version()}" or (
            re.fullmatch(
                r"git:[\w.-]+/[\w.-]+@[0-9a-f]{40}|sha256:[0-9a-f]{64}", harness.integrity()
            )
        )
        assert set(harness.supported_os) == set(OperatingSystem)
    assert harness.resume_support is ResumeSupport.NATIVE
    assert harness.logs[0] == LOG_ROOT and len(harness.logs) > 1


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda harness: harness.name)
@pytest.mark.parametrize("native_session_id", [None, "0b7c5f1e-6f39-4f0a-9d47-6a0f0c8f3a11"])
async def test_prompt_arrives_as_a_file_and_resume_asks_for_its_exact_session(
    harness: CliHarness, native_session_id: str | None
) -> None:
    prompt_path = f"/home/user/{PROMPT_PATH}"
    command = await harness.prepare_command(
        prompt_path,
        PathSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=native_session_id,
    )
    assert prompt_path in command.script
    assert PROMPT not in command.script and PROMPT not in command.env.values()
    assert "token" not in command.script
    tokens = command.script.split()
    assert not {"--continue", "--last", "-c"} & set(tokens)
    if native_session_id is not None:
        assert command.session_id == native_session_id
    elif command.session_id is not None:
        uuid.UUID(command.session_id)
        again = await harness.prepare_command(
            prompt_path,
            PathSandbox(),  # type: ignore[arg-type]
            session(),
            native_session_id=None,
        )
        assert again.session_id != command.session_id


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda harness: harness.name)
@pytest.mark.parametrize("stdout", ["", "not json\n", '{"type":', '{"a": 1}\n[2]\n'])
def test_result_reading_is_total(harness: CliHarness, stdout: str) -> None:
    assert isinstance(harness.read_result(stdout), NativeResult)
    assert harness.read_result(stdout) == harness.read_result(stdout)


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda harness: harness.name)
def test_session_files_never_live_in_the_shared_log_root(harness: CliHarness) -> None:
    session_id = "0b7c5f1e-6f39-4f0a-9d47-6a0f0c8f3a11"
    for name in (f"{session_id}.jsonl", f"rollout-x-{session_id}.jsonl", "stdout.log"):
        assert not harness.is_session_file(PurePosixPath(LOG_ROOT, name), session_id)
        assert not harness.is_session_file(PurePosixPath(LOG_ROOT, session_id, name), session_id)


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda harness: harness.name)
@pytest.mark.parametrize("stdout", [None, "", "garbage\n{partial"])
@pytest.mark.parametrize("reason", [None, "agent_interrupted"])
def test_parser_is_pure_and_starts_with_the_prompt_it_was_sent(
    harness: CliHarness, tmp_path: Path, stdout: str | None, reason: str | None
) -> None:
    if stdout is not None:
        (tmp_path / STDOUT_PATH).parent.mkdir(parents=True)
        (tmp_path / STDOUT_PATH).write_text(stdout)
    parse = context(tmp_path, incomplete=reason is not None, incomplete_reason=reason)
    AutonomousHarnessConformance.check_parser_is_pure(harness, parse)
    trajectory = harness.parse_trajectory(parse)
    assert trajectory.steps[0].source == "user"
    assert trajectory.steps[0].message == "Do the task."
    if reason is not None:
        assert trajectory.extra["ale"]["incomplete_reason"] == reason

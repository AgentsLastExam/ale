from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.harness import HarnessSession, TrajectoryParseContext
from ale.core.testkit import AutonomousHarnessConformance
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.conformance

HARNESSES = (
    ClaudeCodeHarness(),
    GrokBuildHarness(),
    CodexCliHarness(),
    OpenClawCliHarness(),
)


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda harness: harness.name)
def test_shipped_harness_contract(harness, tmp_path: Path) -> None:
    session = HarnessSession(
        episode_id="episode",
        gateway_url="http://gateway",
        token="token",
        model="model",
        sandbox_id="sandbox",
    )
    context = TrajectoryParseContext(
        episode_id="episode",
        trajectory_id="trajectory",
        instruction="Do the task.",
        logs_dir=tmp_path,
        model="model",
        agent_version=harness.version(),
        blobs=BlobStore(tmp_path),
        session_id="session",
    )

    AutonomousHarnessConformance.check_definition(harness)
    AutonomousHarnessConformance.check_empty_resources(harness)
    AutonomousHarnessConformance.check_session(session)
    AutonomousHarnessConformance.check_parser_is_pure(harness, context)

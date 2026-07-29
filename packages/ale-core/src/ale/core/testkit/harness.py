"""Reusable assertions for autonomous harness adapters."""

from __future__ import annotations

from pathlib import PurePosixPath

from ale.core.harness import (
    AutonomousHarness,
    EffectiveAgentResources,
    HarnessFamily,
    HarnessSession,
    NativeContinuation,
    ResumeSupport,
    TrajectoryParseContext,
)
from ale.core.sandbox import Sandbox

__all__ = ["AutonomousHarnessConformance"]


class AutonomousHarnessConformance:
    """Small contract checks a new autonomous adapter can call from its tests."""

    @staticmethod
    def check_definition(harness: AutonomousHarness) -> None:
        assert harness.family is HarnessFamily.AUTONOMOUS
        assert harness.name
        assert harness.version()
        assert harness.integrity()
        assert len(harness.logs) == len(set(harness.logs))
        for name in harness.logs:
            path = PurePosixPath(name)
            assert name and not path.is_absolute() and ".." not in path.parts

    @staticmethod
    def check_empty_resources(harness: AutonomousHarness) -> None:
        harness.validate_resources(EffectiveAgentResources())

    @staticmethod
    def check_session(session: HarnessSession) -> None:
        assert session.episode_id
        assert session.gateway_url
        assert session.token
        assert session.model

    @staticmethod
    def check_parser_is_pure(harness: AutonomousHarness, context: TrajectoryParseContext) -> None:
        assert harness.parse_trajectory(context) == harness.parse_trajectory(context)

    @staticmethod
    async def check_cleanup_is_idempotent(
        harness: AutonomousHarness,
        sandbox: Sandbox,
        session: HarnessSession,
    ) -> None:
        await harness.cleanup(sandbox, session)
        await harness.cleanup(sandbox, session)

    @staticmethod
    def check_native_continuation(
        harness: AutonomousHarness,
        session: HarnessSession,
        continuation: NativeContinuation,
    ) -> None:
        assert harness.resume_support is ResumeSupport.NATIVE
        assert continuation.harness == harness.name
        assert continuation.episode_id == session.episode_id
        assert continuation.sandbox_id == session.sandbox_id
        assert continuation.native_session_id
        assert continuation.fingerprint.startswith("sha256:")

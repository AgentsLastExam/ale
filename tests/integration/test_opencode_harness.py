"""OpenCode searches a sealed Sandbox with the ripgrep ALE installed for it.

OpenCode downloads ripgrep on its first search unless it finds one where it looks; with the
network sealed that download fails and so would the search.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from ale.core.sandbox import Identity
from ale.core.taskspec import OperatingSystem
from ale.run.harnesses.opencode import OpenCodeHarness
from tests.integration.scripted_model import ScriptedModel
from tests.integration.test_scripted_cli_harnesses import Case, mirrored_trajectory, scripted
from tests.support import cli_os

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_cli_install,
    pytest.mark.needs_orchard if cli_os() is OperatingSystem.MACOS else pytest.mark.needs_docker,
]

PROOF = "ale-search-proof"


async def test_opencode_searches_files_with_the_network_sealed(tmp_path: Path) -> None:
    case = Case(partial(OpenCodeHarness), "openai-chat-completions", r"(?!)")
    model = ScriptedModel(tool_pattern=case.tool_pattern)
    async with scripted(tmp_path, "opencode-search", case, model) as episode:
        home = episode.session.home
        await episode.sandbox.write_file(
            f"{home}/search/notes.txt", f"{PROOF}\n".encode(), identity=Identity.AGENT
        )
        model.script = (
            ("grep", {"pattern": PROOF, "path": f"{home}/search"}),
            ("glob", {"pattern": "*.txt", "path": f"{home}/search"}),
        )
        run = await episode.harness.launch(
            "Search the notes.", episode.sandbox, episode.session, timeout_sec=300
        )
        assert run.continuation is not None
        trajectory = await mirrored_trajectory(
            tmp_path, episode, "Search the notes.", run.continuation.native_session_id
        )

    results = {
        call.function_name: result
        for step in trajectory.steps
        for call in step.tool_calls or ()
        for result in (step.observation.results if step.observation else ())
        if result.source_call_id == call.tool_call_id
    }
    assert set(results) == {"grep", "glob"}
    for result in results.values():
        assert not result.extra, result
        assert "notes.txt" in str(result.content)

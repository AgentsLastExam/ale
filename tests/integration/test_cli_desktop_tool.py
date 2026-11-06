"""Every pinned native CLI shows a scripted model a desktop screenshot taken through cua-desktop.

``tests.integration.cli_desktop`` holds the cases, one per CLI and Gateway dialect, runs
them one after another in a single shared desktop Sandbox, and checks each from what
reached the model to what the trajectory records.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from ale.core.taskspec import OperatingSystem
from tests.integration.cli_desktop import (
    CASES,
    Desk,
    desktop,
    expected_to_fail,
    screenshot_round_trip,
)
from tests.support import cli_os

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_cli_install,
    pytest.mark.needs_gui,
    pytest.mark.needs_orchard if cli_os() is OperatingSystem.MACOS else pytest.mark.needs_docker,
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def desk(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Desk]:
    """One desktop Sandbox with the CLIs of the selected cases installed."""
    selected = sorted(
        {
            item.callspec.params["name"]
            for item in request.session.items
            if item.module is request.module and hasattr(item, "callspec")
        }
    )
    async with desktop(
        tmp_path_factory.mktemp("desk"), [CASES[name].harness() for name in selected]
    ) as shared:
        yield shared


@pytest.mark.parametrize(
    "name", [pytest.param(name, marks=expected_to_fail(name)) for name in CASES]
)
async def test_cli_shows_the_model_a_desktop_screenshot(
    desk: Desk, tmp_path: Path, name: str
) -> None:
    await screenshot_round_trip(desk, CASES[name], tmp_path)

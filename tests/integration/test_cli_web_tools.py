"""Every pinned native CLI asks its provider to reach the web only when the Task's network is open.

``tests.integration.cli_web_tools`` holds the cases, one per CLI, runs them one after
another in a single shared Sandbox, and checks each from what reached the Gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from orbit.core.taskspec import OperatingSystem
from tests.integration.cli_web_tools import (
    CASES,
    DIRECT,
    Bench,
    attempt_search,
    bench,
    direct_request,
)
from tests.support import cli_os

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_cli_install,
    pytest.mark.needs_orchard if cli_os() is OperatingSystem.MACOS else pytest.mark.needs_docker,
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def shared(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Bench]:
    """One Sandbox with the CLIs of the selected cases installed."""
    selected = sorted(
        {
            name
            for item in request.session.items
            if item.module is request.module
            and (name := getattr(getattr(item, "callspec", None), "params", {}).get("name"))
        }
    )
    async with bench(
        tmp_path_factory.mktemp("bench"), [CASES[name].harness() for name in selected]
    ) as ready:
        yield ready


@pytest.mark.parametrize("name", sorted(CASES))
async def test_cli_asks_for_provider_web_tools_only_with_open_network(
    shared: Bench, tmp_path: Path, name: str
) -> None:
    case = CASES[name]

    # Open first: a closed launch may leave configuration that turns a tool off, such as
    # Antigravity's hook, which a real episode's fresh Sandbox would not have.
    opened = await attempt_search(shared, case, open_network=True, work=tmp_path / "open")
    closed = await attempt_search(shared, case, open_network=False, work=tmp_path / "closed")

    assert closed.exit_code == 0, closed
    assert not any(closed.asked), f"with the network closed, requests asked for {closed.asked}"
    assert closed.refused == 0, "with the network closed, the CLI still asked for a search"
    assert opened.exit_code == 0, opened
    assert opened.refused == 0
    if case.search.searches:
        assert any(opened.asked), "with the network open, no request asked the provider to search"
    else:
        assert not any(opened.asked), f"requests asked for {opened.asked}"


@pytest.mark.parametrize("dialect", sorted(DIRECT))
async def test_an_agents_own_request_for_web_tools_is_refused_unless_the_network_is_open(
    shared: Bench, dialect: str
) -> None:
    """The Gateway's refusal holds for a request no CLI wrote."""
    assert await direct_request(shared, dialect, open_network=False) == (400, 0)
    assert await direct_request(shared, dialect, open_network=True) == (200, 1)

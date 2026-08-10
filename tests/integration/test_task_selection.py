from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import AgentConfig, RunConfig
from ale.run.cli.main import _run_one

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


@pytest.mark.asyncio
async def test_run_uses_the_same_ordered_variant_selector_as_validation(
    tmp_path: Path,
    write_repo: Callable[..., Path],
) -> None:
    root = tmp_path / "repo"
    task = write_repo(root)
    with (task / "task.yaml").open("a") as handle:
        handle.write("\nvariants:\n  - name: hard\n    params: {greeting: hello}\n")
    settings = RunConfig(agent=AgentConfig(name="nop", model="none"))

    assert (
        await _run_one(
            f"{root}@{{hard,base}}",
            settings,
            tmp_path / "runs",
            run_id="selection",
        )
        == 0
    )

    with sqlite3.connect(tmp_path / "runs/selection/ledger.db") as database:
        variants = [
            row[0]
            for row in database.execute("SELECT variant FROM episodes ORDER BY rowid").fetchall()
        ]
    assert variants == ["hard", "base"]

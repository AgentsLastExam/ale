from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.run.cli.tasks import _validate

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


@pytest.mark.asyncio
async def test_validation_records_selected_variants_and_rejects_partial_oracles(
    tmp_path: Path,
    write_repo: Callable[..., Path],
) -> None:
    root = tmp_path / "repo"
    task = write_repo(root)
    with (task / "task.yaml").open("a") as handle:
        handle.write("\nvariants:\n  - name: hard\n    params: {greeting: hello}\n")
    (task / "verify" / "verify.py").write_text(
        "import os\n"
        "from ale_verify import CheckResult, Verification\n"
        "verification = Verification()\n"
        "verification.check(\n"
        "    'reward',\n"
        "    CheckResult(0.5 if os.path.isfile('/home/user/output/result.txt') else 0.0),\n"
        ")\n"
        "verification.aggregate('overall')\n"
        "verification.write()\n"
    )

    assert await _validate(f"{root}@{{base,hard}}", RunConfig(), tmp_path / "runs") == 2

    validation_path = next((tmp_path / "runs").glob("validate-*/validation.json"))
    observation = json.loads(validation_path.read_text())
    assert [task["variant"] for task in observation["tasks"]] == ["base", "hard"]
    for item in observation["tasks"]:
        assert item["passed"] is False
        assert item["untouched"]["rewards"] == {"reward": 0.0, "overall": 0.0}
        assert item["oracle"]["rewards"] == {"reward": 0.5, "overall": 0.5}
        assert item["warnings"] == []
        assert item["failures"][0]["code"] == "oracle_not_full"
        assert item["untouched"]["lock_digest"].startswith("sha256:")
        assert item["oracle"]["lock_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_validation_preserves_near_full_rewards_with_declared_tolerance(
    tmp_path: Path,
    write_repo: Callable[..., Path],
) -> None:
    root = tmp_path / "repo"
    task = write_repo(root)
    with (task / "task.yaml").open("a") as handle:
        handle.write("\nverify:\n  oracle_tolerance: 0.0002\n")
    (task / "verify" / "verify.py").write_text(
        "import os\n"
        "from ale_verify import CheckResult, Verification\n"
        "verification = Verification()\n"
        "verification.check(\n"
        "    'reward',\n"
        "    CheckResult(0.9998492667446177 if "
        "os.path.isfile('/home/user/output/result.txt') else 0.0),\n"
        ")\n"
        "verification.aggregate('overall')\n"
        "verification.write()\n"
    )

    assert await _validate(str(root), RunConfig(), tmp_path / "runs") == 0
    validation_path = next((tmp_path / "runs").glob("validate-*/validation.json"))
    item = json.loads(validation_path.read_text())["tasks"][0]
    assert item["passed"] is True
    assert item["untouched"]["rewards"] == {"reward": 0.0, "overall": 0.0}
    assert item["oracle"]["rewards"] == {
        "reward": 0.9998492667446177,
        "overall": 0.9998492667446177,
    }
    assert [warning["code"] for warning in item["warnings"]] == ["oracle_tolerance"]
    assert item["failures"] == []

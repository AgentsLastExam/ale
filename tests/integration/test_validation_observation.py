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
@pytest.mark.parametrize(("policy", "retained"), [("host", True), ("none", False)])
async def test_validation_respects_artifact_policy_for_shared_verification(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    policy: str,
    retained: bool,
) -> None:
    root = tmp_path / "repo"
    write_repo(root)
    settings = RunConfig.model_validate({"artifacts": {"collect": policy}})

    assert await _validate(str(root), settings, tmp_path / "runs") == 0

    validation_path = next((tmp_path / "runs").glob("validate-*/validation.json"))
    task = json.loads(validation_path.read_text())["tasks"][0]
    assert task["untouched"]["rewards"] == {"reward": 0.0, "overall": 0.0}
    assert task["oracle"]["rewards"] == {"reward": 1.0, "overall": 1.0}
    for name in ("untouched", "oracle"):
        episode = validation_path.parent / Path(task[name]["result_path"]).parent
        assert (episode / "artifacts" / "snapshot.json").is_file() is retained

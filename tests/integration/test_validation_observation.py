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
        "from ale_verify import write\n"
        "verification = {}\n"
        "verification['reward'] = 0.5 if os.path.isfile('/home/user/output/result.txt') else 0.0\n"
        "verification['overall'] = sum(verification.values()) / len(verification.values())\n"
        "write(verification)\n"
    )

    assert await _validate(f"{root}@{{base,hard}}", RunConfig(), tmp_path / "runs") == 2

    validation_path = next((tmp_path / "runs").glob("validate-*/validation.json"))
    observation = json.loads(validation_path.read_text())
    assert [task["variant"] for task in observation["tasks"]] == ["base", "hard"]
    for item in observation["tasks"]:
        assert item["passed"] is False
        assert item["untouched"]["rewards"] == {"overall": 0.0}
        assert item["oracle"]["rewards"] == {"overall": 0.5}
        assert item["warnings"] == []
        assert item["failures"][0]["code"] == "oracle_not_full"
        assert item["untouched"]["lock_digest"].startswith("sha256:")
        assert item["oracle"]["lock_digest"].startswith("sha256:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "retained", "writer"), [("host", True, "sdk"), ("none", False, "direct")]
)
async def test_validation_respects_artifact_policy_for_shared_verification(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    policy: str,
    retained: bool,
    writer: str,
) -> None:
    root = tmp_path / "repo"
    task_root = write_repo(root)
    script = (
        "import json, os\n"
        "from pathlib import Path\n"
        "score = 0.999 if Path('/home/user/output/result.txt').is_file() else 0.004\n"
        "result = {'overall':score,'details':{'raw':score}}\n"
    )
    if writer == "sdk":
        script += "from ale_verify import write\nwrite(result)\n"
    else:
        script += "Path(os.environ['ALE_VERDICT_PATH']).write_text(json.dumps(result))\n"
    (task_root / "verify" / "verify.py").write_text(script)
    settings = RunConfig.model_validate({"artifacts": {"collect": policy}})

    assert await _validate(str(root), settings, tmp_path / "runs") == 0

    validation_path = next((tmp_path / "runs").glob("validate-*/validation.json"))
    task = json.loads(validation_path.read_text())["tasks"][0]
    assert task["passed"] is True
    assert task["untouched"]["rewards"] == {"overall": 0.0}
    assert task["oracle"]["rewards"] == {"overall": 1.0}
    for name in ("untouched", "oracle"):
        episode = validation_path.parent / Path(task[name]["result_path"]).parent
        saved = json.loads((episode / "verification.json").read_text())
        assert saved["overall"] == task[name]["rewards"]["overall"]
        assert saved["details"]["raw"] == (0.004 if name == "untouched" else 0.999)
        assert (episode / "artifacts" / "snapshot.json").is_file() is retained

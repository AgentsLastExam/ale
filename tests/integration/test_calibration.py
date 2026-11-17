from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.run.cli.tasks import _calibrate

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


@pytest.mark.asyncio
async def test_calibration_checks_every_case_against_its_expectation(
    tmp_path: Path,
    write_repo: Callable[..., Path],
) -> None:
    root = tmp_path / "repo"
    task = write_repo(
        root,
        config={
            "calibration": [
                {"name": "solution", "expect": {"reward": 1, "solution_only": 1}},
                {
                    "name": "partial",
                    "expect": {"reward": [0.4, 0.6], "length": 5, "solution_only": 0},
                },
                {"name": "hardcoded-output", "expect": {"reward": 0}},
                {"name": "start-state", "expect": {"reward": 0, "length": 0}},
            ]
        },
    )
    # Each oracle episode stages only its own case, where its verifier can still see it.
    (task / "calibration/solution/only-in-solution.txt").write_text("only the solution holds this")
    (task / "tests" / "verify.py").write_text(
        "from pathlib import Path\n"
        "from ale_verify import write\n"
        "path = Path('/home/user/output/result.txt')\n"
        "text = path.read_text() if path.is_file() else ''\n"
        "reward = 1.0 if text == 'hello world' else 0.5 if text.startswith('hello') else 0.0\n"
        "staged = Path('/home/user/.ale-solution/only-in-solution.txt').is_file()\n"
        "write({'reward': reward, 'length': len(text), 'solution_only': int(staged)})\n"
    )
    # A partial answer from the case's own asset root, and one that skips reading the input
    # the instruction names, which this verifier cannot tell from the real solution.
    partial = task / "calibration" / "partial"
    (partial / "assets").mkdir(parents=True)
    (partial / "assets" / "answer.txt").write_text("hello")
    (partial / "solve.sh").write_text(
        'cp "$ALE_STAGE_DIR/assets/answer.txt" /home/user/output/result.txt\n'
    )
    hardcoded = task / "calibration" / "hardcoded-output"
    hardcoded.mkdir()
    (hardcoded / "solve.sh").write_text("printf 'hello world' > /home/user/output/result.txt\n")
    # The start state, which an author checks with a case that does nothing.
    (task / "calibration" / "start-state").mkdir()
    (task / "calibration" / "start-state" / "solve.sh").write_text("true\n")

    assert await _calibrate(str(root), RunConfig(), tmp_path / "runs") == 2

    path = next((tmp_path / "runs").glob("calibrate-*/calibration.json"))
    (observed,) = json.loads(path.read_text())["tasks"]
    assert observed["name"] == "agentslastexam/hello"
    assert observed["passed"] is False
    cases = {case["name"]: case for case in observed["cases"]}
    assert list(cases) == ["solution", "partial", "hardcoded-output", "start-state"]
    assert {name: case["observed"] for name, case in cases.items()} == {
        "solution": {"reward": 1.0, "solution_only": 1.0},
        "partial": {"reward": 0.5, "length": 5.0, "solution_only": 0.0},
        "hardcoded-output": {"reward": 1.0},
        "start-state": {"reward": 0.0, "length": 0.0},
    }
    assert [case["passed"] for case in cases.values()] == [True, True, False, True]
    assert [notice["code"] for notice in cases["hardcoded-output"]["failures"]] == [
        "unexpected_value"
    ]
    for case in cases.values():
        assert case["attempt"]["status"] == "completed"
        assert case["attempt"]["lock_digest"].startswith("sha256:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "retained", "writer"), [("host", True, "sdk"), ("none", False, "direct")]
)
async def test_calibration_respects_artifact_policy_for_shared_verification(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    policy: str,
    retained: bool,
    writer: str,
) -> None:
    root = tmp_path / "repo"
    task_root = write_repo(
        root, config={"calibration": [{"name": "start-state", "expect": {"reward": 0}}]}
    )
    (task_root / "calibration" / "start-state").mkdir()
    (task_root / "calibration" / "start-state" / "solve.sh").write_text("true\n")
    script = (
        "import json, os\n"
        "from pathlib import Path\n"
        "score = 0.999 if Path('/home/user/output/result.txt').is_file() else 0.004\n"
        "result = {'reward':score,'details':{'raw':score}}\n"
    )
    if writer == "sdk":
        script += "from ale_verify import write\nwrite(result)\n"
    else:
        script += (
            "Path('/logs/verifier/verification.json').write_text(json.dumps(result))\n"
            "Path('/logs/verifier/reward.json').write_text(json.dumps({'reward':score}))\n"
        )
    (task_root / "tests" / "verify.py").write_text(script)
    settings = RunConfig.model_validate({"artifacts": {"collect": policy}})

    assert await _calibrate(str(root), settings, tmp_path / "runs") == 0

    path = next((tmp_path / "runs").glob("calibrate-*/calibration.json"))
    task = json.loads(path.read_text())["tasks"][0]
    assert task["passed"] is True
    cases = {case["name"]: case["attempt"] for case in task["cases"]}
    assert cases["solution"]["rewards"] == {"reward": 1.0}
    assert cases["start-state"]["rewards"] == {"reward": 0.0}
    for name, attempt in cases.items():
        episode = path.parent / Path(attempt["result_path"]).parent
        saved = json.loads((episode / "verification.json").read_text())
        assert saved["reward"] == attempt["rewards"]["reward"]
        assert saved["details"]["raw"] == (0.004 if name == "start-state" else 0.999)
        assert (episode / "artifacts" / "snapshot.json").is_file() is retained

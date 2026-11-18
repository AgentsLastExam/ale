"""A real agent CLI receives two validation rounds and improves on what they tell it.

The Task hides a number from 1 to 7. ``validation/`` and ``tests/`` are the same checker: a
reward that falls with the distance from the number, and feedback that says whether the
guess is too low, too high or right. Three turns are enough to find it by halving, so the
rounds, which only the native session carries from one Sandbox to the next, are what lets
the agent's final answer score.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale.core.trajectory import AtifTrajectory
from ale.run.scaffold import scaffold_task
from tests.support import edit_task_config

from .trajectory import LIVE, run_task

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]

#: The model each CLI runs, as the continuation acceptance runs them.
AGENTS = {"claude-code": "claude-sonnet-5-5", "codex-cli": "gpt-6.1-terra"}
INSTRUCTION = (
    "Write one integer from 1 to 7 into /home/user/answer.txt, then end your turn. "
    "A hidden checker knows the right number.\n"
)
CHECK = """\
from pathlib import Path
from ale_verify import write

answer = Path("/home/user/answer.txt")
text = answer.read_text().strip() if answer.is_file() else ""
guess = int(text) if text.isdigit() else None
if guess is None:
    write({"reward": 0.0}, feedback="/home/user/answer.txt holds no integer.")
else:
    side = "right" if guess == 5 else "too low" if guess < 5 else "too high"
    reward = round(max(0.0, 1 - abs(guess - 5) / 4), 2)
    write({"reward": reward, "guess": guess}, feedback=f"{guess} is {side}.")
"""
TEST = '#!/usr/bin/env bash\nset -euo pipefail\nexec "$ALE_VERIFY_PYTHON" verify.py\n'


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_an_agent_uses_its_validation_rounds(tmp_path: Path, agent: str) -> None:
    root = scaffold_task(tmp_path / "hidden-number")
    (root / "instruction.md").write_text(INSTRUCTION)
    edit_task_config(root, {"agent": {"timeout_sec": 900, "validation_rounds": 2}})
    for stage in ("tests", "validation"):
        (root / stage).mkdir(exist_ok=True)
        (root / stage / "test.sh").write_text(TEST)
        (root / stage / "verify.py").write_text(CHECK)

    episode = run_task(root, tmp_path / "runs", run_id="rounds", agent=agent, model=AGENTS[agent])

    result = json.loads((episode / "result.json").read_text())
    assert result["status"] == "completed", result.get("failure")
    rounds = result["validation_rounds"]
    assert [item["index"] for item in rounds] == [1, 2]
    assert all(item["status"] == "completed" for item in rounds), rounds
    lock = json.loads((episode / "lock.json").read_text())
    assert lock["validation_rounds"] == 2
    trajectory = AtifTrajectory.model_validate_json((episode / "trajectory.json").read_text())
    marked = {
        (step.extra or {}).get("ale", {}).get("validation_round"): step
        for step in trajectory.steps
        if step.source == "user"
    }
    assert {1, 2} <= set(marked), trajectory.extra
    assert "Validation round 2 of 2" in str(marked[2].message)
    # Halving finds the number in three turns: the final state scores.
    assert result["rewards"] == {"reward": 1.0}, [item.get("reward") for item in rounds]

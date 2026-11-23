"""One episode longer than its Slurm allocation, completed across several, as Orbit runs on the
cluster's login node: each allocation's deadline cuts the agent's turn, the solver moves to
the next, and the agent goes on in the same session with the time it has left."""

from __future__ import annotations

import itertools
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orbit.core.trajectory import AtifTrajectory
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.protocol.episode_protocol import MOVED_PROMPT, EpisodeProtocol
from orbit.run.providers.slurm import SlurmProvider
from orbit.run.providers.slurm.job import Job, cache
from orbit.run.scaffold import scaffold_task
from orbit.run.secrets import resolve_task_variables
from orbit.run.tasksets.manifest import load_tasks
from tests.support import WorkingCliHarness, edit_task_config, provider_registry, slurm_config

pytestmark = [pytest.mark.integration, pytest.mark.needs_slurm]

#: Twelve minutes of the agent's work, a line every ten seconds, in allocations of ten
#: minutes, two of which drain: each run there ends 120 s before the drain and, as the only
#: turn is the last, keeps the verifier's minute too, so each holds about four of them.
LINES, EVERY = 72, 10
VERIFY = f"""\
import os
from pathlib import Path
from orbit_verify import write

home = Path(os.environ["ORBIT_HOME"])
def lines(name):
    path = home / name
    return path.read_text().splitlines() if path.is_file() else []
work = lines("work.txt")
write(
    {{
        "reward": round(min(1.0, len(work) / {LINES}), 2),
        "work": work,
        "prompts": lines("prompts.txt"),
        "survivors": lines("survivors"),
    }}
)
"""
INSTRUCTION = "Write the lines of work.\n"


def _task(root: Path) -> Path:
    scaffold_task(root)
    edit_task_config(
        root,
        {
            "environment": {"cpus": 1, "memory_mb": 1024},
            "agent": {"timeout_sec": 1800},
            "verifier": {"timeout_sec": 60},
        },
    )
    (root / "tests" / "test.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nexec "$ORBIT_VERIFY_PYTHON" verify.py\n'
    )
    (root / "tests" / "verify.py").write_text(VERIFY)
    (root / "instruction.md").write_text(INSTRUCTION)
    return root


@pytest.mark.asyncio
async def test_an_episode_longer_than_its_allocation_completes_across_allocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = slurm_config().model_copy(update={"walltime_minutes": 10, "drain_minutes": 2})
    provider = SlurmProvider(config)
    jobs: list[Job] = []
    submit = Job.submit

    async def counted(job: Job, **resources: Any) -> None:
        await submit(job, **resources)
        if resources.get("command", "supervise") == "supervise":
            jobs.append(job)

    monkeypatch.setattr(Job, "submit", counted)
    task = load_tasks(_task(tmp_path / "long"))[0]
    resolve_task_variables([task], oracle=False, protected={})
    result = await run_episode(
        task,
        EpisodeProtocol(WorkingCliHarness(LINES, every=EVERY)),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    verified = json.loads((result.run_dir / "verification.json").read_text())
    segments = result.record.segments
    moved = [item for item in segments if item.began == "moved"]
    assert segments[0].began == "provisioned" and len(moved) >= 2, segments
    # Each segment in an allocation of its own, each a job of its own.
    assert len({item.sandbox_id.partition(".")[0] for item in segments}) == len(segments)
    assert len(jobs) >= len(segments)
    # The agent's instruction, then the prompt that resumed it in each moved solver, in its
    # one session; no process outlived a move.
    resumed = MOVED_PROMPT.partition("{minutes}")[0]
    assert verified["prompts"][0] == INSTRUCTION.strip()
    assert len(verified["prompts"]) == len(segments)
    assert all(prompt.startswith(resumed) for prompt in verified["prompts"][1:])
    assert verified["survivors"] == []
    # Every line of work was written once, by the run of the segment it was written in.
    assert len(verified["work"]) == LINES
    runs = [int(line.split()[-1]) for line in verified["work"]]
    assert runs == sorted(runs) and set(runs) == set(range(1, len(segments) + 1))
    # The agent's time is what its runs spent: none of the saves, the queue and the boots
    # between them.
    spent = sum(item.agent_sec for item in segments)
    assert LINES * EVERY - EVERY <= spent <= LINES * EVERY + 4 * EVERY * len(segments)
    gaps = [
        (after.started_at - before.ended_at).total_seconds()
        for before, after in itertools.pairwise(segments)
        if before.ended_at is not None
    ]
    assert all(gap > 0 for gap in gaps)
    progress = json.loads((result.run_dir / "progress.json").read_text())
    assert progress["agent_used_sec"] == pytest.approx(spent, abs=1)
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    users = [step for step in trajectory.steps if step.source == "user"]
    marks = [((step.extra or {}).get("orbit") or {}).get("moved") for step in users]
    assert marks == [None, *(item.index for item in moved)]
    agent = [timing.outcome for timing in result.record.phases if timing.phase == "agent"]
    assert agent == ["moved"] * len(moved) + ["succeeded"]
    # Nothing is left: no job, no snapshot, no job directory.
    outcomes = [item.outcome for item in result.record.sandboxes]
    assert all(outcome == "destroyed" for outcome in outcomes), outcomes
    assert provider.snapshots == {}
    assert not [
        item for item in await provider.list_retained() if item["episode"] == result.episode_id
    ]
    listed = subprocess.run(
        ["squeue", "-h", "--me", "-o", "%j"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert not {job.name for job in jobs} & set(listed)
    assert not [job for job in jobs if (cache() / job.name).exists()]
    summary = {
        "segments": [
            {
                "began": item.began,
                "sandbox": item.sandbox_id,
                "node": item.allocation.node,
                "agent_sec": round(item.agent_sec, 1),
                "started": item.started_at.isoformat(),
                "ended": item.ended_at.isoformat() if item.ended_at else None,
            }
            for item in segments
        ],
        "gaps_sec": [round(gap, 1) for gap in gaps],
        "phases": [
            (timing.phase, timing.duration_ms, timing.outcome) for timing in result.record.phases
        ],
    }
    print("SEGMENTS " + json.dumps(summary))

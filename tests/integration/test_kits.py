"""Shared libraries reach a task's stages without an engine-specific search path.

The path we used to set was a rule every task author had to learn, and ours was quietly
wrong: it set a literal glob, which the variable does not expand, so one of its two
implementations never worked at all. Putting kits where the interpreter already looks
removes the rule and the class of bug with it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.kits import scan_kits, write_lock
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"


def repo_with_kit(root: Path) -> Path:
    """A task whose verify stage imports a kit the domain ships."""
    task = root / "tasks" / "usekit"
    for sub in ("setup", "verify", "oracle"):
        (task / sub).mkdir(parents=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")

    kit = root / "kits" / "grader-protocol" / "grader_protocol"
    kit.mkdir(parents=True)
    (root / "kits" / "grader-protocol" / "kit.toml").write_text(
        'name = "grader-protocol"\npackage = "grader_protocol"\n'
    )
    (kit / "__init__.py").write_text('def score(v):\n    return 1.0 if v == "hello" else 0.0\n')

    (task / "task.yaml").write_text(
        textwrap.dedent(f"""
        image: {IMAGE}
        resources: {{ cpus: 1, memory_mb: 512 }}
        timeouts: {{ setup: 60, agent: 60, verify: 60 }}
        artifacts: [/home/user/output]
        verify:
          kits: [grader-protocol]
        """).strip()
    )
    (task / "instruction.md").write_text("Write hello to /home/user/output/r.txt\n")
    (task / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /home/user/output\n"
    )
    (task / "oracle" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf hello > /home/user/output/r.txt\n"
    )
    (task / "verify" / "run.sh").write_text(
        textwrap.dedent("""
            #!/usr/bin/env bash
            set -euo pipefail
            python3 -c "
            import json, os, grader_protocol
            value = open('/home/user/output/r.txt').read().strip()
            json.dump({'rewards': {'reward': grader_protocol.score(value)}},
                      open(os.environ['ALE_VERDICT_PATH'], 'w'))
            "
        """).strip()
    )
    write_lock(root, scan_kits(root))
    return task


@pytest.mark.asyncio
async def test_a_kit_is_importable_with_no_search_path(tmp_path: Path) -> None:
    """The verify stage imports it directly — nothing sets PYTHONPATH."""
    task_root = repo_with_kit(tmp_path / "repo")
    task = next(iter(ManifestTaskset(task_root).load()))

    result = await run_episode(
        task, StandardEnvironment(OracleHarness()), DockerProvider(), run_dir=tmp_path / "runs"
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_a_setup_stage_kit_is_visible_to_the_agent(tmp_path: Path) -> None:
    """Stages run as root and the agent runs unprivileged.

    Installing into either one's private location would make a kit importable for that
    identity and missing for the other, which is why the destination is the system one.
    """
    task_root = repo_with_kit(tmp_path / "repo")
    manifest = task_root / "task.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "verify:\n  kits: [grader-protocol]", "setup:\n  kits: [grader-protocol]"
        )
    )
    # Imported by the oracle, which runs as the agent.
    (task_root / "oracle" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "python3 -c 'import grader_protocol' && printf hello > /home/user/output/r.txt\n"
    )
    (task_root / "verify" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'test "$(cat /home/user/output/r.txt)" = hello '
        '&& printf \'{"rewards": {"reward": 1.0}}\' > "$ALE_VERDICT_PATH" '
        '|| printf \'{"rewards": {"reward": 0.0}}\' > "$ALE_VERDICT_PATH"\n'
    )
    task = next(iter(ManifestTaskset(task_root).load()))

    result = await run_episode(
        task, StandardEnvironment(OracleHarness()), DockerProvider(), run_dir=tmp_path / "runs"
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}

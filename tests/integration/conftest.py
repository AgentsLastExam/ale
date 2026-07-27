"""Shared scaffolding for the container-backed tests.

The task repository these build is the smallest thing the loader accepts, which is also
what makes it a useful fixture: if a change breaks the minimum, every test here fails at
once rather than one obscure case failing later.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

#: Paths live under the agent's home, which is where a task's declared destinations
#: belong: the framework creates them as the agent and never changes anyone's ownership,
#: so a path the agent could not create is a task that fails at once rather than one that
#: is quietly chowned into working.
#:
#: Our own base image, not an upstream one. The contract requires an unprivileged user,
#: a command that keeps the sandbox alive and a guest interpreter — `python:3.12-slim`
#: has none of those, which is exactly why tasks build on curated images instead.
IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"

VERIFY_DEFAULT = textwrap.dedent("""
    #!/usr/bin/env bash
    set -euo pipefail
    expected="hello world"
    actual="$(cat /home/user/output/result.txt 2>/dev/null || true)"
    if [ "$actual" = "$expected" ]; then
        printf '{"rewards": {"reward": 1.0}}' > "$ALE_VERDICT_PATH"
    else
        printf '{"rewards": {"reward": 0.0}}' > "$ALE_VERDICT_PATH"
    fi
""").strip()

SETUP_DEFAULT = (
    "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /home/user/input /home/user/output\n"
    "printf 'world' > /home/user/input/word.txt\n"
)


def _write_repo(root: Path, *, with_oracle: bool = True, verify_body: str | None = None) -> Path:
    """Lay out a minimal task repository: manifest, instruction, setup, verify, oracle."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    task = root / "tasks" / "hello"
    (task / "setup").mkdir(parents=True)
    (task / "verify").mkdir()

    (task / "task.yaml").write_text(
        textwrap.dedent(f"""
        image: {IMAGE}
        resources: {{ cpus: 1, memory_mb: 512 }}
        timeouts: {{ setup: 120, agent: 120, verify: 120 }}
        artifacts: [/home/user/output]
        params: {{ greeting: hello }}
        validate: {{ min_reward: 1.0 }}
        """).strip()
    )
    (task / "instruction.md").write_text(
        "Write ${greeting} followed by the word in /home/user/input/word.txt "
        "into /home/user/output/result.txt\n"
    )
    (task / "setup" / "run.sh").write_text(SETUP_DEFAULT)
    (task / "verify" / "run.sh").write_text(verify_body or VERIFY_DEFAULT)
    if with_oracle:
        (task / "oracle").mkdir()
        (task / "oracle" / "run.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "word=$(cat /home/user/input/word.txt)\n"
            "printf 'hello %s' \"$word\" > /home/user/output/result.txt\n"
        )
    return task


@pytest.fixture
def write_repo() -> Callable[..., Path]:
    return _write_repo

"""Create a minimal self-contained Task that lints and validates."""

from __future__ import annotations

import shutil
from pathlib import Path

from ale.core.errors import TaskDefinitionError
from ale.core.ids import TaskId
from ale.run import ale_image

__all__ = ["DEFAULT_ORGANIZATION", "scaffold_task"]

#: The organization of a scaffolded Task's ``org/name``; edit ``[task].name`` for another.
DEFAULT_ORGANIZATION = "agentslastexam"

TASK_TOML = """\
schema_version = "1.4+ale.1"
artifacts = ["/home/user/output"]

[task]
name = "{name}"

[environment]
cpus = 1
memory_mb = 4096

[agent]
user = "user"
timeout_sec = 300
network_mode = "no-network"

[verifier]
timeout_sec = 120
"""

INSTRUCTION_MD = """\
Write the word hello into /home/user/output/result.txt
"""

DOCKERFILE = f"""\
FROM {ale_image("container-ubuntu22-base")}

RUN mkdir -p /home/user/output && chown -R user:user /home/user/output
"""

TEST_SH = """\
#!/usr/bin/env bash
set -euo pipefail
exec "$ALE_VERIFY_PYTHON" verify.py
"""

VERIFY_PY = """\
from ale_verify import checks, write

check = checks.text_equals("/home/user/output/result.txt", "hello\\n")
write({"reward": check["score"], "content": check})
"""


SOLVE_SH = """\
#!/usr/bin/env bash
set -euo pipefail
printf 'hello\\n' > /home/user/output/result.txt
"""


def scaffold_task(path: Path, *, force: bool = False) -> Path:
    target = path.resolve()
    try:
        name = TaskId(f"{DEFAULT_ORGANIZATION}/{target.name}")
    except ValueError as exc:
        raise TaskDefinitionError(
            "Task folder name must be a lowercase slug, or task.toml must be written by hand: "
            f"{exc}"
        ) from exc
    if target.exists():
        if not force:
            raise TaskDefinitionError(f"{target} already exists; pass --force to overwrite it")
        shutil.rmtree(target)

    files = {
        "task.toml": TASK_TOML.format(name=name),
        "instruction.md": INSTRUCTION_MD,
        "environment/Dockerfile": DOCKERFILE,
        "tests/test.sh": TEST_SH,
        "tests/verify.py": VERIFY_PY,
        "solution/solve.sh": SOLVE_SH,
    }
    for relative, content in files.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        if destination.suffix == ".sh":
            destination.chmod(0o755)
    return target

"""Create a minimal self-contained Task that lints and validates."""

from __future__ import annotations

import shutil
from pathlib import Path

from ale.core.errors import TaskDefinitionError
from ale.core.ids import TaskId
from ale.run import ale_image

__all__ = ["scaffold_task"]

TASK_YAML = """\
spec_type: core/v1
name: {name}
image:
  kind: container

resources: {{cpus: 1, memory_mb: 4096}}
network: {{mode: block}}
timeouts: {{agent: 300, verify: 120}}
artifacts: [/home/user/output]

metadata:
  tags: []
"""

INSTRUCTION_MD = """\
Write the word hello into /home/user/output/result.txt
"""

DOCKERFILE = f"""\
FROM {ale_image("container-ubuntu22-base")}

RUN mkdir -p /home/user/output && chown -R user:user /home/user/output
"""

VERIFY_SH = """\
#!/usr/bin/env bash
set -euo pipefail
exec "$ALE_VERIFY_PYTHON" verify.py
"""

VERIFY_PY = """\
from ale_verify import checks, write

check = checks.text_equals("/home/user/output/result.txt", "hello\\n")
write({"overall": check["score"], "content": check})
"""


ORACLE_SH = """\
#!/usr/bin/env bash
set -euo pipefail
printf 'hello\\n' > /home/user/output/result.txt
"""


def scaffold_task(path: Path, *, force: bool = False) -> Path:
    target = path.resolve()
    try:
        name = TaskId(target.name)
    except ValueError as exc:
        raise TaskDefinitionError(
            "Task folder name must be a lowercase slug or task.yaml must be authored "
            f"manually: {exc}"
        ) from exc
    if target.exists():
        if not force:
            raise TaskDefinitionError(f"{target} already exists; pass --force to overwrite it")
        shutil.rmtree(target)

    files = {
        "task.yaml": TASK_YAML.format(name=name),
        "instruction.md": INSTRUCTION_MD,
        "image/Dockerfile": DOCKERFILE,
        "verify/run.sh": VERIFY_SH,
        "verify/verify.py": VERIFY_PY,
        "oracle/run.sh": ORACLE_SH,
    }
    for relative, content in files.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        if destination.name == "run.sh":
            destination.chmod(0o755)
    return target

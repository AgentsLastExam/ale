"""Create a minimal self-contained Task that lints and validates."""

from __future__ import annotations

import shutil
from pathlib import Path

from ale.core.errors import TaskDefinitionError
from ale.core.ids import TaskId

__all__ = ["scaffold_task"]

TASK_YAML = """\
spec_type: core/v1
name: {name}
image:
  kind: container

resources: {{cpus: 1, memory_mb: 1024}}
network: {{mode: block}}
timeouts: {{setup: 60, agent: 300, verify: 120}}
artifacts: [/home/user/output]

params:
  greeting: hello

metadata:
  tags: []
"""

INSTRUCTION_MD = """\
Write the word ${greeting} into /home/user/output/result.txt
"""

DOCKERFILE = """\
FROM ghcr.io/agentslastexam/sandbox-base-cli:latest

RUN mkdir -p /home/user/output && chown -R user:user /home/user/output
"""

VERIFY_SH = """\
#!/usr/bin/env bash
set -euo pipefail
exec python3 verify.py
"""

VERIFY_PY = """\
import json
import os

from ale_verify import Verification, checks


with open(os.environ["ALE_PARAMS_JSON"], encoding="utf-8") as handle:
    expected = json.load(handle)["greeting"] + "\\n"

verification = Verification()
verification.check(
    "content",
    checks.text_equals("/home/user/output/result.txt", expected),
)
verification.aggregate("overall")
verification.write()
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

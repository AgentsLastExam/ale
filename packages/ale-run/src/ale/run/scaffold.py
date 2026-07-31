"""Creating a new task folder.

The scaffold is deliberately a *working* task, not a set of blanks: its oracle writes
exactly what its verifier expects, so `ale validate` passes the moment it is created.
An author then changes one thing at a time from a state known to be green, which is a
much shorter path to a first task than filling in placeholders and discovering at the
end which of them was wrong.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ale.core.errors import TaskDefinitionError

__all__ = ["scaffold_task"]

TASK_YAML = """\
# The identifier is not written here: it comes from this folder's path under tasks/.
image: sandbox-base-cli
resources: { cpus: 1, memory_mb: 1024 }
network: { mode: block } # block | allowlist (+ allowed_hosts) | open
timeouts: { setup: 60, agent: 300, verify: 120 }

# Stages are symmetric. Each names the data it needs and the shared libraries it uses;
# scripts are not declared, because this folder's setup/ and verify/ directories are
# copied in when their stage runs and run.sh executes if present.
#
# Anything listed under `verify` is absent while the agent works — that timing, not any
# flag, is what keeps gold answers out of reach.
setup:
  assets: []
  # - repo: agents-last-exam/ale-tasks-assets
  #   revision: <commit>     # a commit, so two runs read the same bytes
  #   path: mydomain/mytask/input
  #   dest: /home/user/input   # your choice, but it has to be somewhere the agent can
  #                            # write — the framework creates declared paths as the
  #                            # agent and never changes ownership, so its home is the
  #                            # natural home for a task's data too
  kits: []
verify:
  assets: []
  kits: []

# Absolute paths holding this task's output. Whether a copy is kept is a run-level
# setting (`artifacts.collect`), not this file's business.
artifacts: [/home/user/output]

# Substituted into instruction.md as ${greeting}. Strict both ways: every declared
# parameter must be used, and every ${placeholder} must be declared.
params:
  greeting: hello

# Optional. Each variant becomes its own task instance with its own identity.
# variants:
#   - { name: base }
#   - { name: loud, params: { greeting: HELLO } }

metadata: { tags: [] }
"""

INSTRUCTION_MD = """\
Write the word ${greeting} into /home/user/output/result.txt

State paths literally, as above. The image is fixed and this task chose these paths, so
there is nothing left for the prompt to compute.
"""

SETUP_SH = """\
#!/usr/bin/env bash
# Runs in the sandbox before the agent, with the task's own data already in place.
#
# The framework creates what this task declared — asset destinations, artifact paths and
# the run's scratch directory. Anything else is this script's to create.
set -euo pipefail

mkdir -p /home/user/output
"""

VERIFY_SH = """\
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$(dirname "$0")/check.py"
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
# The task's own solution, run in place of the agent by `ale validate`.
#
# This is the admission gate: a task nobody can solve is a broken task, and finding that
# out costs one container rather than one agent run.
set -euo pipefail

mkdir -p /home/user/output
printf 'hello\\n' > /home/user/output/result.txt
"""

_FILES = {
    "task.yaml": TASK_YAML,
    "instruction.md": INSTRUCTION_MD,
    "setup/run.sh": SETUP_SH,
    "verify/run.sh": VERIFY_SH,
    "verify/check.py": VERIFY_PY,
    "oracle/run.sh": ORACLE_SH,
}


def scaffold_task(path: Path, *, force: bool = False) -> Path:
    """Create a minimal, self-solving task folder at ``path``."""
    target = path.resolve()
    if target.exists():
        if not force:
            raise TaskDefinitionError(f"{target} already exists; pass --force to overwrite it")
        shutil.rmtree(target)

    for relative, content in _FILES.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        if destination.name == "run.sh":
            destination.chmod(0o755)

    return target

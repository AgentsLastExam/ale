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
  #   dest: /ale/input       # your choice; there is no framework layout
  kits: []
verify:
  assets: []
  kits: []

# Absolute paths holding this task's output. Whether a copy is kept is a run-level
# setting (`artifacts.collect`), not this file's business.
artifacts: [/ale/output]

# Substituted into instruction.md as ${greeting}. Strict both ways: every declared
# parameter must be used, and every ${placeholder} must be declared.
params:
  greeting: hello

# Optional. Each variant becomes its own task instance with its own identity.
# variants:
#   - { name: base }
#   - { name: loud, params: { greeting: HELLO } }

validate: { min_reward: 1.0 }
metadata: { tags: [] }
"""

INSTRUCTION_MD = """\
Write the word ${greeting} into /ale/output/result.txt

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

mkdir -p /ale/output
"""

VERIFY_SH = """\
#!/usr/bin/env bash
# Scores the episode. Runs after the agent, with this folder and any verify-stage assets
# now present in the sandbox.
#
# Write rewards to $ALE_VERDICT_PATH. Exiting non-zero, or writing nothing, is a
# `task_error` — a defect in the task — and is deliberately distinct from a zero score.
set -euo pipefail

actual="$(tr -d '[:space:]' < /ale/output/result.txt 2>/dev/null || true)"

if [ "$actual" = "hello" ]; then
    printf '{"rewards": {"reward": 1.0}}' > "$ALE_VERDICT_PATH"
else
    printf '{"rewards": {"reward": 0.0}}' > "$ALE_VERDICT_PATH"
fi
"""

ORACLE_SH = """\
#!/usr/bin/env bash
# The task's own solution, run in place of the agent by `ale validate`.
#
# This is the admission gate: a task nobody can solve is a broken task, and finding that
# out costs one container rather than one agent run.
set -euo pipefail

mkdir -p /ale/output
printf 'hello\\n' > /ale/output/result.txt
"""

_FILES = {
    "task.yaml": TASK_YAML,
    "instruction.md": INSTRUCTION_MD,
    "setup/run.sh": SETUP_SH,
    "verify/run.sh": VERIFY_SH,
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

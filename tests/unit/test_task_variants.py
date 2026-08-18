from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.errors import TaskDefinitionError
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit


def _task(tmp_path: Path, variants: str = "") -> Path:
    task = tmp_path / "task"
    (task / "image").mkdir(parents=True)
    (task / "verify").mkdir()
    (task / "oracle").mkdir()
    (task / "task.yaml").write_text(
        "spec_type: core/v1\n"
        "name: demo\n"
        "image: {kind: container}\n"
        "params: {count: 3}\n"
        "resources: {cpus: 1, memory_mb: 512}\n"
        "timeouts: {agent: 100}\n"
        f"{variants}"
    )
    (task / "instruction.md").write_text("Write ${count} items")
    (task / "image" / "Dockerfile").write_text(
        "FROM ghcr.io/agentslastexam/container-ubuntu22-base:latest\n"
    )
    for stage in ("verify", "oracle"):
        (task / stage / "run.sh").write_text("#!/bin/sh\n")
    return task


def test_top_level_is_base_and_variant_overlays_only_listed_keys(tmp_path: Path) -> None:
    base, hard = load_tasks(
        _task(
            tmp_path,
            "variants:\n"
            "  - name: hard\n"
            "    params: {count: 10}\n"
            "    resources: {cpus: 4}\n"
            "    timeouts: {agent: 200}\n",
        )
    )
    assert base.spec.variant == "base"
    assert base.spec.params == {"count": 3}
    assert base.spec.resources.memory_mb == 512
    assert hard.spec.params == {"count": 10}
    assert hard.spec.resources.cpus == 4
    assert hard.spec.resources.memory_mb == 512
    assert hard.spec.timeouts.agent == 200
    assert hard.task_digest == base.task_digest
    assert hard.image_source_digest == base.image_source_digest


@pytest.mark.parametrize(
    ("variants", "message"),
    [
        ("variants:\n  - {name: base}\n", "reserved"),
        ("variants:\n  - {name: hard}\n  - {name: hard}\n", "unique"),
        ("variants:\n  - {name: hard, network: {mode: open}}\n", "extra"),
        ("variants:\n  - {name: hard, image: {}}\n", "extra"),
    ],
)
def test_invalid_variant_definitions_fail(
    tmp_path: Path,
    variants: str,
    message: str,
) -> None:
    with pytest.raises(TaskDefinitionError, match=message):
        load_tasks(_task(tmp_path, variants))

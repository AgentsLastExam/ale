from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.errors import TaskDefinitionError
from ale.core.kit import resolve_kit
from ale.run.kits import hash_kit
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = pytest.mark.integration

TASKS = Path(__file__).resolve().parents[3] / "ale-tasks-base"


def test_two_tasks_select_the_same_flat_package_without_inventory() -> None:
    package = resolve_kit(TASKS, "verification_example")
    assert package == TASKS / "kits" / "verification_example"
    assert not (TASKS / "kits.lock.yaml").exists()
    for task_name in ("verification_domain_a", "verification_domain_b"):
        task = next(iter(ManifestTaskset(TASKS / "tasks" / "demo" / task_name).load()))
        assert task.spec.verify.kits == ("verification_example",)


def test_flat_package_hash_tracks_actual_bytes(tmp_path: Path) -> None:
    package = tmp_path / "kits" / "example"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("VALUE = 1\n")
    first = hash_kit(package)
    source.write_text("VALUE = 2\n")
    assert hash_kit(package) != first


@pytest.mark.parametrize("name", ["bad-name", "class", "missing"])
def test_invalid_or_missing_flat_packages_fail(name: str, tmp_path: Path) -> None:
    with pytest.raises(TaskDefinitionError):
        resolve_kit(tmp_path, name)

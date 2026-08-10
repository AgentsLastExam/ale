from __future__ import annotations

import pytest

from ale.core.errors import RegistryError, TaskDefinitionError
from ale.run.sources import parse_task_reference, select_tasks

pytestmark = pytest.mark.unit


class Spec:
    def __init__(self, name: str, variant: str) -> None:
        self.name = name
        self.variant = variant


class Task:
    def __init__(self, name: str, variant: str) -> None:
        self.spec = Spec(name, variant)


def test_selector_defaults_to_base_and_preserves_explicit_order() -> None:
    assert parse_task_reference("tasks/foo").variants == ("base",)
    assert parse_task_reference("tasks/foo@hard").variants == ("hard",)
    assert parse_task_reference("tasks/foo@{base,hard}").variants == ("base", "hard")


@pytest.mark.parametrize(
    "reference",
    ["tasks/foo@", "tasks/foo@{}", "tasks/foo@{base,}", "tasks/foo@{base,base}", "@hard"],
)
def test_malformed_or_duplicate_selectors_fail(reference: str) -> None:
    with pytest.raises(RegistryError):
        parse_task_reference(reference)


def test_selection_applies_to_each_task_in_requested_order() -> None:
    tasks = [
        Task("a", "base"),
        Task("a", "hard"),
        Task("b", "base"),
        Task("b", "hard"),
    ]
    selected = select_tasks(tasks, ("hard", "base"))
    assert [(task.spec.name, task.spec.variant) for task in selected] == [
        ("a", "hard"),
        ("a", "base"),
        ("b", "hard"),
        ("b", "base"),
    ]


def test_missing_collection_variant_lists_available_choices() -> None:
    with pytest.raises(TaskDefinitionError, match=r"available: base"):
        select_tasks([Task("a", "base")], ("hard",))

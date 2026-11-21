"""[verifier.validation].visibility: the agent's read-only copy of the stage that validates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.support import MemorySandbox, RoundsProvider, TurnsHarness, Verifiers, rounds_task
from tests.unit.test_validation_rounds import _run, _task

from ale.core.sandbox import SandboxRole
from ale.core.verdict import Status

pytestmark = pytest.mark.unit

_NOTE = "where you can read them but not change them."


def _files(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


@pytest.mark.asyncio
@pytest.mark.parametrize("validation_dir", [True, False])
async def test_the_stage_that_validates_is_copied_for_the_agent_as_setup_ends(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch, validation_dir: bool
) -> None:
    """validation/, or else tests/ as if copied into it, by the framework, where the first
    prompt says it is; verifiers still stage their own."""
    uploaded: list[tuple[SandboxRole, str, object]] = []
    upload = MemorySandbox.upload_dir

    async def recording(self: MemorySandbox, source: Any, target: Any, *, identity=None):  # type: ignore[no-untyped-def]
        uploaded.append((self.request.role, str(target), identity))
        return await upload(self, source, target, identity=identity)

    monkeypatch.setattr(MemorySandbox, "upload_dir", recording)
    provider = RoundsProvider()
    harness = TurnsHarness()
    task = _task(tmp_path, rounds=1, validation_dir=validation_dir, validation={"visibility": True})
    result = await _run(tmp_path, provider, task, harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert task.folder is not None
    stage = "validation" if validation_dir else "tests"
    (solver, *_) = provider.sandboxes
    assert set(solver.uploads["/opt/validation"]) == _files(task.folder.root / stage)
    # Once, into the first solver, as the framework; each later turn boots from its snapshot.
    assert [
        (role, identity) for role, target, identity in uploaded if target == "/opt/validation"
    ] == [(SandboxRole.SHARED, None)]
    # The round's verifier and the final one each ran the stage the Host staged for them.
    assert f"/opt/ale/{stage}" in provider.sandboxes[1].uploads
    named = "validation set" if validation_dir else "test set that scores your final state"
    assert harness.prompts[0].endswith(
        f"The files of the {named} are at /opt/validation, {_NOTE}\n"
    )
    assert _NOTE not in harness.prompts[1]


@pytest.mark.asyncio
async def test_without_rounds_the_note_alone_follows_the_instruction(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    harness = TurnsHarness()
    task = _task(tmp_path, rounds=0, validation={"visibility": True})
    result = await _run(tmp_path, RoundsProvider(), task, harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    (prompt,) = harness.prompts
    assert prompt == (
        f"{task.spec.instruction}\n\n---\n\n"
        f"The files of the validation set are at /opt/validation, {_NOTE}\n"
    )


@pytest.mark.asyncio
async def test_by_default_no_stage_reaches_the_agent(tmp_path: Path, verifiers: Verifiers) -> None:
    provider = RoundsProvider()
    harness = TurnsHarness()
    task = _task(tmp_path, rounds=1, validation_dir=False)
    assert not task.spec.validation_visible
    assert "validation_visible" not in task.spec.model_dump(mode="json")
    result = await _run(tmp_path, provider, task, harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert not any("/opt/validation" in sandbox.uploads for sandbox in provider.sandboxes)
    assert not any(_NOTE in prompt for prompt in harness.prompts)


def test_a_shown_tests_dir_is_part_of_what_the_agent_depended_on(tmp_path: Path) -> None:
    """A retained solver stays reverifiable across a change to tests/ only while its agent
    could not read tests/."""
    from ale.run.tasksets.manifest import load_tasks

    for visibility in (False, True):
        task = rounds_task(
            tmp_path / str(visibility),
            rounds=1,
            validation_dir=False,
            validation={"visibility": visibility},
        )
        assert task.folder is not None
        (task.folder.root / "tests" / "test.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        changed = load_tasks(task.folder.root)[0].non_verifier_digest != task.non_verifier_digest
        assert changed is visibility

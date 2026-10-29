from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from tests.support import sibling_checkout

from ale.core.taskspec import TaskConfig

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NORMATIVE = (
    ROOT / "README.md",
    ROOT / "docs/README.md",
    ROOT / "docs/specs/task-quality-standard.md",
    ROOT / "docs/specs/task-authoring.md",
    ROOT / "docs/specs/episode-protocol.md",
    ROOT / "docs/specs/sandbox-image.md",
    ROOT / "docs/specs/verification.md",
    ROOT / "docs/specs/security.md",
    ROOT / "docs/guides/development.md",
    ROOT / "docs/specs/lexicon.md",
)


@pytest.mark.parametrize("path", NORMATIVE, ids=lambda path: path.name)
def test_living_task_docs_do_not_recommend_removed_contracts(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    forbidden = (
        "task.yaml",
        "spec_type",
        "core/v1",
        "image.kind",
        "image.ref",
        "image.base_ref",
        "image.user",
        "image/Dockerfile",
        "image/run.ps1",
        "image/assets",
        "verify/run.sh",
        "verify/assets",
        "oracle/run.sh",
        "oracle/assets",
        "resources.sudo",
        "network.mode",
        "COPY --from=ale_assets",
        "$ALE_REPO_PATH/.cache/assets/",
        "setup:\n  assets:",
        "verify:\n  kits:",
        "image:\n  assets:",
        "verify:\n  assets:",
        "path: skills/",
        "path: mcp/",
        "verification is shared-only",
        "retention:\n  solver:",
        "Verification.asset_path",
        "image: {build: verify/image}",
        "image: {build: verify}",
        "image: {ref:",
    )
    assert not [snippet for snippet in forbidden if snippet in text]


def test_authoring_contract_names_the_current_task_shape() -> None:
    text = (ROOT / "docs/specs/task-authoring.md").read_text(encoding="utf-8")
    for required in (
        "task.toml",
        "[ale]",
        "environment/Dockerfile",
        "environment/build.ps1",
        "environment/setup/",
        "tests/test.sh",
        "solution/solve.sh",
        "tools/skills/",
        "tools/mcp/",
        'environment_mode = "separate"',
        "vm-ubuntu24-base",
        "network_justification",
        "artifacts.collect",
    ):
        assert required in text
    for irrelevant in (
        "assets",
        "sandbox-base-",
        "ALE_ASSETS_COLLECTION",
        "There is no Image Tree",
        "Submission checklist",
    ):
        assert irrelevant not in text


def test_authoring_annotated_configuration_matches_the_schema() -> None:
    text = (ROOT / "docs/specs/task-authoring.md").read_text(encoding="utf-8")
    configuration = text.split("```toml\n", 1)[1].split("\n```", 1)[0]
    TaskConfig.model_validate(tomllib.loads(configuration))


def test_task_quality_standard_owns_only_semantic_quality() -> None:
    text = (ROOT / "docs/specs/task-quality-standard.md").read_text(encoding="utf-8")
    for required in (
        "instruction",
        "initial context",
        "verification",
        "without human clarification",
        "Blocked agent network access is the default",
        "reward hacking",
    ):
        assert required in text
    for implementation_detail in (
        "task.toml",
        "assets/",
        "requirement matrix",
        "ale validate",
        "Required final response",
    ):
        assert implementation_detail not in text
    verification = (ROOT / "docs/specs/verification.md").read_text(encoding="utf-8")
    assert "finite number from 0 through 1" in verification
    assert "ALE interprets only `reward`" in verification


@pytest.mark.parametrize(
    "repository",
    (sibling_checkout("ale-tasks-base"), sibling_checkout("ale-tasks-152")),
    ids=("base", "152"),
)
def test_maintained_tasks_use_the_current_configuration(repository: Path) -> None:
    if not repository.is_dir():
        pytest.skip(f"optional Task checkout not present: {repository.name}")
    for path in repository.glob("tasks/**/task.toml"):
        TaskConfig.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def test_current_docs_name_prepare_and_current_schema() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in NORMATIVE)
    for required in ("ale prepare", "RunLock schema 2", "docker_image"):
        assert required in text

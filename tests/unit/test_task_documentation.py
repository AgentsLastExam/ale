from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NORMATIVE = (
    ROOT / "README.md",
    ROOT / "docs/README.md",
    ROOT / "docs/guides/task-authoring.md",
    ROOT / "docs/specs/task-design.md",
    ROOT / "docs/specs/task-folder.md",
    ROOT / "docs/specs/standard-environment.md",
    ROOT / "docs/specs/sandbox-image.md",
    ROOT / "docs/specs/verification.md",
    ROOT / "docs/specs/security.md",
    ROOT / "docs/guides/development.md",
    ROOT / "docs/specs/lexicon.md",
    ROOT.parent / "ale-tasks-base/README.md",
)


@pytest.mark.parametrize("path", NORMATIVE, ids=lambda path: path.name)
def test_living_task_docs_do_not_recommend_removed_contracts(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    forbidden = (
        "spec_type: core/v2",
        "COPY --from=ale_assets",
        "$ALE_REPO_PATH/.cache/assets/",
        "setup:\n  assets:",
        "verify:\n  kits:",
        "image:\n  assets:",
        "verify:\n  assets:",
        "verify/check.py",
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
    text = (ROOT / "docs/guides/task-authoring.md").read_text(encoding="utf-8")
    for required in (
        "spec_type: core/v1",
        "image.kind",
        "image/Dockerfile",
        "verify/verify.py",
        "image/assets/",
        "setup/assets/",
        "verify/assets/",
        "oracle/assets/",
        "tools/skills/",
        "tools/mcp/",
        "environment_mode: separate",
        "sandbox-base-vm-gui",
        "[sandbox_retention]",
    ):
        assert required in text


@pytest.mark.parametrize(
    "repository",
    (ROOT.parent / "ale-tasks-base", ROOT.parent / "ale-tasks-152"),
    ids=("base", "152"),
)
def test_maintained_tasks_declare_an_explicit_image_kind(repository: Path) -> None:
    for manifest in repository.glob("tasks/**/task.yaml"):
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert data["spec_type"] == "core/v1", manifest
        assert data["image"]["kind"] in {"container", "vm"}, manifest


def test_current_docs_name_prepare_and_current_schema() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in NORMATIVE)
    for required in ("ale prepare", "RunLock schema 2", "image.ref"):
        assert required in text

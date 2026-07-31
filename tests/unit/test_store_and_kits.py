"""The two mechanisms that make data and shared code portable."""

from __future__ import annotations

import pytest

from ale.core.errors import TaskDefinitionError
from ale.core.ids import TaskId
from ale.core.kit import resolve_kit, validate_kit_name
from ale.core.store import STORE_ROOT, AssetOrigin, StoreEntry, StoreManifest, data_key
from ale.core.taskspec import (
    AssetMount,
    ImageRef,
    SetupStage,
    TaskSpec,
    VerifyStage,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64


class TestDataKey:
    """Keys are content-derived so pre-baked images survive task renames."""

    def test_same_inputs_give_the_same_key(self) -> None:
        one = data_key("org/assets", "abc123", "hello_inputs")
        two = data_key("org/assets", "abc123", "hello_inputs")
        assert one == two

    def test_revision_change_changes_the_key(self) -> None:
        assert data_key("org/assets", "abc123", "x") != data_key("org/assets", "def456", "x")

    def test_key_is_independent_of_any_task(self) -> None:
        """Two tasks sharing a bundle resolve to one key — and one cached copy."""
        shared = data_key("org/assets", "abc123", "shared_bundle")
        assert shared == data_key("org/assets", "abc123", "shared_bundle")

    def test_store_paths_do_not_contain_task_names(self) -> None:
        entry = StoreEntry(
            key=data_key("org/assets", "abc123", "hello_inputs"),
            component="hello_inputs",
            repo="org/assets",
            revision="abc123",
        )
        assert str(entry.path).startswith(str(STORE_ROOT))
        assert "hello" not in str(entry.path).replace(entry.component, "")


class TestStoreManifest:
    def test_lookup_decides_whether_to_fetch(self) -> None:
        key = data_key("org/assets", "abc123", "hello_inputs")
        manifest = StoreManifest(
            entries=(
                StoreEntry(key=key, component="hello_inputs", repo="org/assets", revision="abc123"),
            )
        )
        assert manifest.contains(key)
        assert manifest.entry(key) is not None
        assert not manifest.contains("0" * 32)

    def test_origins_are_distinguishable(self) -> None:
        assert AssetOrigin.BAKED != AssetOrigin.DOWNLOAD


class TestFlatKit:
    def test_name_is_the_python_package_name(self) -> None:
        assert validate_kit_name("grader_protocol") == "grader_protocol"
        for invalid in ("grader-protocol", "class", "", "two words"):
            with pytest.raises(TaskDefinitionError):
                validate_kit_name(invalid)

    def test_resolves_exact_init_package(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        package = tmp_path / "kits" / "grader_protocol"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        assert resolve_kit(tmp_path, "grader_protocol") == package
        (package / "__init__.py").unlink()
        with pytest.raises(TaskDefinitionError, match="__init__"):
            resolve_kit(tmp_path, "grader_protocol")


class TestStages:
    """Setup and verify are the same shape; only their timing differs."""

    def make(self, **overrides: object) -> TaskSpec:
        base: dict[str, object] = {
            "id": TaskId("demo-hello"),
            "domain": "demo",
            "instruction": "Write to /home/user/output/result.txt",
            "image": ImageRef(name="sandbox-base-cli"),
        }
        return TaskSpec(**(base | overrides))  # type: ignore[arg-type]

    def test_stages_default_to_empty(self) -> None:
        spec = self.make()
        assert spec.setup.assets == () and spec.setup.kits == ()
        assert spec.verify.assets == () and spec.verify.kits == ()

    def test_a_mount_carries_everything_needed_to_find_it(self) -> None:
        """A task is readable on its own: no lookup table to keep in step with it."""
        mount = AssetMount(
            repo="org/assets", revision="abc123", path="demo/hello/input", dest="/home/user/input"
        )
        spec = self.make(setup=SetupStage(assets=(mount,)))
        assert spec.setup.assets[0].repo == "org/assets"
        assert spec.setup.assets[0].dest == "/home/user/input"

    def test_verify_stage_carries_its_own_assets_and_kits(self) -> None:
        spec = self.make(
            verify=VerifyStage(
                assets=(
                    AssetMount(
                        repo="org/assets", revision="abc", path="demo/answers", dest="/gold"
                    ),
                ),
                kits=("grader_protocol",),
            )
        )
        assert spec.verify.assets[0].dest == "/gold"
        assert spec.verify.kits == ("grader_protocol",)

    def test_a_task_chooses_where_its_data_lands(self) -> None:
        """No global layout: a simulation domain puts scenes wherever it needs them."""
        spec = self.make(
            setup=SetupStage(
                assets=(
                    AssetMount(
                        repo="org/sim", revision="abc", path="scenes", dest="/opt/sim/scenes"
                    ),
                )
            ),
            artifacts=("/opt/sim/runs",),
        )
        assert spec.setup.assets[0].dest == "/opt/sim/scenes"
        assert spec.artifacts[0] == "/opt/sim/runs"

"""Contract behavior shared by loaders and runtime."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ale.core.errors import TaskDefinitionError
from ale.core.ids import TaskId, canonical_json, content_hash
from ale.core.taskspec import (
    ImageKind,
    ImageSpec,
    McpSource,
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    SkillSource,
    StdioMcpServer,
    StreamableHttpMcpServer,
    TaskManifestV1,
    TaskMcpSource,
    TaskSpec,
    ToolProvision,
    VerificationMode,
    VerifierResources,
    VerifySpec,
)
from ale.core.template import render_instruction
from ale.core.verdict import Status, Verdict

pytestmark = pytest.mark.unit


def make_spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = {
        "name": TaskId("demo-hello"),
        "image": {"kind": "container"},
        "instruction": "Write hello into /home/user/output/result.txt",
    }
    return TaskSpec(**(base | overrides))  # type: ignore[arg-type]


def test_task_spec_is_strict_core_v1_and_path_independent() -> None:
    spec = make_spec()
    assert spec.spec_type == "core/v1"
    assert spec.id == "demo-hello"
    assert spec.label == "demo-hello"
    assert spec.network.mode is NetworkMode.BLOCK
    assert spec.image == ImageSpec(kind=ImageKind.CONTAINER)
    assert not hasattr(spec, "domain")
    with pytest.raises(ValidationError):
        make_spec(domain="demo")


def test_image_spec_is_explicit_strict_and_nonblank() -> None:
    assert ImageSpec(kind="vm", ref="ghcr.io/acme/disk:v1").kind is ImageKind.VM
    assert ImageSpec(kind="vm", base_ref="/images/base.qcow2").ref is None
    for payload in (
        {},
        {"kind": "unknown"},
        {"kind": "container", "ref": "   "},
        {"kind": "container", "build": "image"},
        {"kind": "vm", "path": "/tmp/disk.qcow2"},
        {"kind": "vm", "base_ref": "   "},
        {"kind": "vm", "ref": "task.qcow2", "base_ref": "base.qcow2"},
        {"kind": "container", "base_ref": "base:latest"},
    ):
        with pytest.raises(ValidationError):
            ImageSpec.model_validate(payload)

    with pytest.raises(ValidationError, match=r"verify\.image\.base_ref"):
        VerifySpec(
            environment_mode="separate",
            resources=VerifierResources(cpus=1, memory_mb=512, storage_mb=None, gpus=0),
            image={"kind": "vm", "base_ref": "/images/base.qcow2"},
        )


def test_task_spec_hash_tracks_rendered_semantics() -> None:
    assert make_spec().spec_hash == make_spec().spec_hash
    assert make_spec().spec_hash != make_spec(instruction="different").spec_hash
    hard = make_spec(variant="hard", params={"n": 10})
    assert hard.label == "demo-hello@hard"
    assert hard.spec_hash != make_spec(params={"n": 3}).spec_hash


def test_task_spec_round_trips() -> None:
    spec = make_spec(params={"rows": 3}, metadata={"category": "cli"})
    restored = TaskSpec.model_validate_json(spec.model_dump_json())
    assert restored == spec


def test_manifest_requires_explicit_name_and_rejects_removed_fields() -> None:
    TaskManifestV1(spec_type="core/v1", name="demo", image={"kind": "container"})
    with pytest.raises(ValidationError):
        TaskManifestV1.model_validate({"spec_type": "core/v1", "name": "demo"})
    for removed in (
        "domain",
        "setup",
        "files",
        "kits",
        "gpu_vram_gb",
    ):
        with pytest.raises(ValidationError):
            TaskManifestV1.model_validate(
                {
                    "spec_type": "core/v1",
                    "name": "demo",
                    "image": {"kind": "container"},
                    removed: {},
                }
            )


@pytest.mark.parametrize(
    "payload",
    [
        {"image": {"assets": {}}},
        {"setup": {"assets": []}},
        {"verify": {"assets": []}},
    ],
)
def test_manifest_rejects_authored_asset_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TaskManifestV1.model_validate(
            {
                "spec_type": "core/v1",
                "name": "demo",
                "image": {"kind": "container"},
                **payload,
            }
        )


def test_manifest_field_set_is_exact() -> None:
    assert set(TaskManifestV1.model_fields) == {
        "spec_type",
        "name",
        "os",
        "environment",
        "image",
        "resources",
        "network",
        "timeouts",
        "artifacts",
        "artifact_excludes",
        "artifact_include_files",
        "artifact_include_directories",
        "artifact_include_regular_files",
        "artifact_preserve_file_directories",
        "artifact_allow_missing",
        "artifact_omit_unreadable_files",
        "tools",
        "params",
        "verify",
        "variants",
        "metadata",
        "extras",
    }


def test_windows_artifacts_use_windows_absolute_path_rules() -> None:
    manifest = TaskManifestV1(
        spec_type="core/v1",
        name="windows-demo",
        os=OperatingSystem.WINDOWS,
        image={"kind": "vm"},
        artifacts=(r"C:\Users\user\output",),
    )
    assert manifest.artifacts == (r"C:\Users\user\output",)
    with pytest.raises(ValidationError, match="absolute"):
        TaskManifestV1.model_validate(manifest.model_dump() | {"artifacts": ("output",)})


def test_network_and_agent_resource_contracts_remain_strict() -> None:
    with pytest.raises(ValidationError):
        NetworkPolicy(mode=NetworkMode.ALLOWLIST)
    with pytest.raises(ValidationError):
        NetworkPolicy(mode=NetworkMode.BLOCK, allowed_hosts=("example.com",))
    assert SkillSource(path="tools/skills/reviewer").path == "tools/skills/reviewer"
    assert TaskMcpSource(path="tools/mcp/search.toml").path == "tools/mcp/search.toml"
    assert McpSource(builtin="cua-desktop").builtin == "cua-desktop"
    with pytest.raises(ValidationError):
        ToolProvision.model_validate({"mcp_servers": [{"builtin": "cua-desktop"}]})


def test_mcp_transports_reject_mixed_fields() -> None:
    StdioMcpServer(name="local", transport="stdio", command="python3")
    StreamableHttpMcpServer(
        name="remote", transport="streamable-http", url="https://example.com/mcp"
    )
    with pytest.raises(ValidationError):
        StdioMcpServer.model_validate(
            {
                "name": "mixed",
                "transport": "stdio",
                "command": "server",
                "url": "https://example.com",
            }
        )


def test_canonical_json_and_hash_are_stable() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_instruction_template_is_strict() -> None:
    assert render_instruction("Solve ${n} cases", {"n": 3}) == "Solve 3 cases"
    with pytest.raises(TaskDefinitionError, match="undeclared"):
        render_instruction("Solve ${n}", {})
    with pytest.raises(TaskDefinitionError, match="unused"):
        render_instruction("no placeholders", {"n": 3})


def test_failed_verification_is_not_a_zero_reward() -> None:
    zero = Verdict.completed({"reward": 0.0})
    broken = Verdict.failed(Status.TASK_ERROR, ValueError("no rewards file"))
    assert zero.rewards == {"reward": 0.0}
    assert broken.rewards is None


def test_shared_and_separate_verification_contracts_are_strict() -> None:
    assert VerifySpec().environment_mode is VerificationMode.SHARED
    resources = VerifierResources(cpus=1, memory_mb=512, storage_mb=None, gpus=0)
    separate = VerifySpec(environment_mode="separate", resources=resources)
    assert separate.resources == resources
    with pytest.raises(ValidationError):
        VerifySpec.model_validate({"environment_mode": "separate"})
    with pytest.raises(ValidationError):
        VerifySpec.model_validate(
            {
                "environment_mode": "shared",
                "resources": {"cpus": 1, "memory_mb": 512, "storage_mb": None, "gpus": 0},
            }
        )
    dedicated = VerifySpec(
        environment_mode="separate",
        image={"kind": "vm", "ref": "ghcr.io/acme/verifier:v1"},
        resources=resources,
    )
    assert dedicated.image == ImageSpec(kind="vm", ref="ghcr.io/acme/verifier:v1")
    with pytest.raises(ValidationError):
        VerifySpec.model_validate(
            {
                "environment_mode": "shared",
                "image": {"kind": "container", "ref": "example/verifier:v1"},
            }
        )
    with pytest.raises(ValidationError):
        VerifySpec.model_validate(
            {
                "environment_mode": "separate",
                "image": "example/verifier:v1",
                "resources": resources.model_dump(),
            }
        )


@pytest.mark.parametrize(
    "artifacts",
    [
        ("relative",),
        ("/output", "/output"),
        ("/output", "/output/result.json"),
    ],
)
def test_artifacts_are_absolute_unique_and_non_overlapping(artifacts: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        TaskManifestV1(
            spec_type="core/v1",
            name="demo",
            image={"kind": "container"},
            artifacts=artifacts,
        )


@pytest.mark.parametrize(
    "excludes",
    [
        ("",),
        ("../outside",),
        ("/absolute",),
        ("a//b",),
        ("a/./b",),
        ("a", "a/b"),
        ("a", "a"),
        ("a\\b",),
    ],
)
def test_invalid_artifact_excludes_are_rejected(excludes: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        TaskManifestV1(
            spec_type="core/v1",
            name="demo",
            image={"kind": "container"},
            artifacts=("/output",),
            artifact_excludes=excludes,
        )


def test_artifact_excludes_require_linux_container_artifacts() -> None:
    values = {
        "spec_type": "core/v1",
        "name": "demo",
        "image": {"kind": "container"},
        "artifacts": ("/output",),
        "artifact_excludes": (".agent_runtime_env",),
    }
    assert TaskManifestV1.model_validate(values).artifact_excludes == (".agent_runtime_env",)
    with pytest.raises(ValidationError):
        TaskManifestV1.model_validate(values | {"artifacts": ()})
    with pytest.raises(ValidationError):
        TaskManifestV1.model_validate(values | {"image": {"kind": "vm"}})
    with pytest.raises(ValidationError):
        TaskManifestV1.model_validate(values | {"os": "windows", "artifacts": (r"C:\\output",)})


def test_allow_missing_artifacts_requires_linux_artifacts() -> None:
    values = {
        "spec_type": "core/v1",
        "name": "demo",
        "image": {"kind": "container"},
        "artifacts": ("/output",),
        "artifact_excludes": (".venv",),
        "artifact_allow_missing": True,
    }
    assert TaskManifestV1.model_validate(values).artifact_allow_missing is True
    default = TaskManifestV1.model_validate(values | {"artifact_allow_missing": False})
    assert default.artifact_allow_missing is False
    linux_vm = values | {"image": {"kind": "vm"}, "artifact_excludes": ()}
    assert TaskManifestV1.model_validate(linux_vm).artifact_allow_missing is True
    # Missing-root support does not enable path filtering for VMs.
    with pytest.raises(ValidationError, match="filtering requires a Linux container"):
        TaskManifestV1.model_validate(values | {"image": {"kind": "vm"}})
    for invalid in (
        {"artifacts": ()},
        {"os": "windows", "artifacts": (r"C:\\output",)},
        {
            "os": "windows",
            "image": {"kind": "vm"},
            "artifact_excludes": (),
            "artifacts": (r"C:\\output",),
        },
    ):
        with pytest.raises(ValidationError):
            TaskManifestV1.model_validate(values | invalid)


@pytest.mark.parametrize(
    "includes",
    [("",), ("../x",), ("/x",), ("a//b",), ("a/./b",), ("a", "a/b"), ("a", "a")],
)
def test_invalid_artifact_include_files_are_rejected(includes: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        TaskManifestV1(
            spec_type="core/v1",
            name="demo",
            image={"kind": "container"},
            artifacts=("/output",),
            artifact_include_files=includes,
        )


def test_artifact_include_files_require_linux_container_and_no_excludes() -> None:
    values = {
        "spec_type": "core/v1",
        "name": "demo",
        "image": {"kind": "container"},
        "artifacts": ("/output",),
        "artifact_include_files": ("a/file",),
    }
    assert TaskManifestV1.model_validate(values).artifact_include_files == ("a/file",)
    assert TaskManifestV1.model_validate(
        values | {"artifact_preserve_file_directories": True}
    ).artifact_preserve_file_directories
    with pytest.raises(ValidationError, match="requires included files"):
        TaskManifestV1.model_validate(
            values | {"artifact_include_files": (), "artifact_preserve_file_directories": True}
        )
    for extra in (
        {"artifacts": ()},
        {"image": {"kind": "vm"}},
        {"os": "windows", "artifacts": (r"C:\\output",)},
        {"artifact_excludes": ("other",)},
    ):
        with pytest.raises(ValidationError):
            TaskManifestV1.model_validate(values | extra)


def test_omitting_unreadable_files_requires_selected_files_and_separate_verification() -> None:
    values = {
        "spec_type": "core/v1",
        "name": "demo",
        "image": {"kind": "container"},
        "artifacts": ("/output",),
        "artifact_include_files": ("result.txt",),
        "artifact_omit_unreadable_files": True,
        "verify": {
            "environment_mode": "separate",
            "resources": {"cpus": 1, "memory_mb": 512, "storage_mb": None, "gpus": 0},
        },
    }
    assert TaskManifestV1.model_validate(values).artifact_omit_unreadable_files is True
    for extra in (
        {"verify": {"environment_mode": "shared"}},
        {"artifact_include_files": ()},
        {"image": {"kind": "vm"}},
        {"os": "windows", "artifacts": (r"C:\\output",)},
    ):
        with pytest.raises(ValidationError):
            TaskManifestV1.model_validate(values | extra)
    assert not TaskManifestV1.model_validate(
        values | {"artifact_omit_unreadable_files": False}
    ).artifact_omit_unreadable_files


def test_artifact_included_directories_are_normalized_and_disjoint() -> None:
    values = {
        "spec_type": "core/v1",
        "name": "demo",
        "image": {"kind": "container"},
        "artifacts": ("/base",),
        "artifact_include_files": ("output/result.txt",),
        "artifact_include_directories": ("input",),
    }
    manifest = TaskManifestV1.model_validate(values)
    assert manifest.artifact_include_directories == ("input",)
    for extra in (
        {"artifact_include_directories": ("../input",)},
        {"artifact_include_directories": ("input/",)},
        {"artifact_include_directories": ("output",)},
        {"artifact_include_directories": ("input", "input/sub")},
        {"artifact_include_directories": ("output/result.txt",)},
        {"artifact_excludes": ("other",)},
        {"artifacts": ()},
        {"image": {"kind": "vm"}},
        {"os": "windows", "artifacts": (r"C:\\base",)},
    ):
        with pytest.raises(ValidationError):
            TaskManifestV1.model_validate(values | extra)


def test_artifact_regular_file_directories_are_validated() -> None:
    values = {
        "spec_type": "core/v1",
        "name": "demo",
        "image": {"kind": "container"},
        "artifacts": ("/base",),
        "artifact_include_files": ("output/model.obj",),
        "artifact_include_regular_files": ({"path": "output", "recursive": True},),
    }
    manifest = TaskManifestV1.model_validate(values)
    assert manifest.artifact_include_regular_files[0].path == "output"
    assert manifest.artifact_include_regular_files[0].recursive is True
    for extra in (
        {"artifact_include_regular_files": ({"path": "../output"},)},
        {"artifact_include_regular_files": ({"path": "output/"},)},
        {"artifact_include_regular_files": ({"path": "/output"},)},
        {"artifact_include_regular_files": ({"path": "output"}, {"path": "output/sub"})},
        {"artifact_include_regular_files": ({"path": "output", "unknown": True},)},
        {"artifact_include_regular_files": ({"path": "output", "pattern": "../*.sgf"},)},
        {"artifact_include_regular_files": ({"path": "output", "pattern": ""},)},
        {"artifact_include_regular_files": ({"path": "output", "unreadable": "ignore"},)},
        {"artifact_include_regular_files": ({"path": "output", "pattern": "dir\\*.sgf"},)},
        {"artifact_include_directories": ("output",)},
        {"artifact_include_files": ("output",)},
        {"artifact_excludes": ("other",)},
        {"artifacts": ()},
        {"image": {"kind": "vm"}},
        {"os": "windows", "artifacts": (r"C:\\base",)},
    ):
        with pytest.raises(ValidationError):
            TaskManifestV1.model_validate(values | extra)


def test_empty_unreadable_regular_files_require_separate_verification() -> None:
    fields = {
        "artifacts": ("/base",),
        "artifact_include_regular_files": ({"path": "output", "unreadable": "empty"},),
    }
    with pytest.raises(ValidationError, match="require separate verification"):
        make_spec(**fields)
    with pytest.raises(ValidationError, match="require separate verification"):
        TaskManifestV1.model_validate(
            {"spec_type": "core/v1", "name": "demo", "image": {"kind": "container"}} | fields
        )
    verify = {
        "environment_mode": "separate",
        "resources": {"cpus": 1, "memory_mb": 1024, "storage_mb": None, "gpus": 0},
    }
    assert (
        make_spec(**(fields | {"verify": verify})).artifact_include_regular_files[0].unreadable
        == "empty"
    )
    assert (
        TaskManifestV1.model_validate(
            {"spec_type": "core/v1", "name": "demo", "image": {"kind": "container"}}
            | fields
            | {"verify": verify}
        )
        .artifact_include_regular_files[0]
        .unreadable
        == "empty"
    )


def test_nonrecursive_regular_selection_can_contain_a_selected_subtree() -> None:
    fields = {
        "artifacts": ("/base",),
        "artifact_include_directories": ("output/submission",),
        "artifact_include_regular_files": ({"path": "output", "pattern": "submission"},),
    }
    spec = make_spec(**fields)
    assert spec.artifact_include_directories == ("output/submission",)
    with pytest.raises(ValidationError, match="overlaps directory"):
        make_spec(
            **(
                fields
                | {"artifact_include_regular_files": ({"path": "output", "recursive": True},)}
            )
        )

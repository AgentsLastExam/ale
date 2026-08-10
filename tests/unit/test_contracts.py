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
    for payload in (
        {},
        {"kind": "unknown"},
        {"kind": "container", "ref": "   "},
        {"kind": "container", "build": "image"},
        {"kind": "vm", "path": "/tmp/disk.qcow2"},
    ):
        with pytest.raises(ValidationError):
            ImageSpec.model_validate(payload)


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
        "environment",
        "image",
        "resources",
        "network",
        "timeouts",
        "artifacts",
        "tools",
        "params",
        "verify",
        "variants",
        "metadata",
        "extras",
    }


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

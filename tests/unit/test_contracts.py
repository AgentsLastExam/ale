"""Contract behavior shared by loaders and runtime."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.support import MINIMAL_TASK_CONFIG, merged_config

from ale.core.ids import TaskId, canonical_json, content_hash, task_component
from ale.core.taskspec import (
    ImageKind,
    ImageSpec,
    McpSource,
    NetworkMode,
    NetworkPolicy,
    SkillSource,
    StdioMcpServer,
    StreamableHttpMcpServer,
    TaskConfig,
    TaskSpec,
    ToolProvision,
    VerificationMode,
    VerifierResources,
    VerifySpec,
)
from ale.core.verdict import Status, Verdict

pytestmark = pytest.mark.unit


def make_spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = {
        "name": TaskId("agentslastexam/demo-hello"),
        "image": {"kind": "container"},
        "instruction": "Write hello into /home/user/output/result.txt",
    }
    return TaskSpec(**(base | overrides))  # type: ignore[arg-type]


def test_task_spec_is_strict_and_path_independent() -> None:
    spec = make_spec()
    assert spec.name == "agentslastexam/demo-hello"
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

    with pytest.raises(ValidationError, match="base ref"):
        VerifySpec(
            environment_mode="separate",
            resources=VerifierResources(cpus=1, memory_mb=512, storage_mb=None, gpus=0),
            image={"kind": "vm", "base_ref": "/images/base.qcow2"},
        )


def test_task_spec_hash_tracks_semantics() -> None:
    assert make_spec().spec_hash == make_spec().spec_hash
    assert make_spec().spec_hash != make_spec(instruction="different").spec_hash
    assert make_spec().spec_hash != make_spec(resources={"cpus": 2}).spec_hash


def test_task_spec_round_trips() -> None:
    spec = make_spec(metadata={"category": "cli"})
    restored = TaskSpec.model_validate_json(spec.model_dump_json())
    assert restored == spec


def config(*layers: dict[str, object]) -> TaskConfig:
    return TaskConfig.model_validate(merged_config(MINIMAL_TASK_CONFIG, *layers))


def test_task_ids_are_org_and_name_and_one_safe_component() -> None:
    assert TaskId("agentslastexam/demo_hello") == "agentslastexam/demo_hello"
    assert task_component(TaskId("agentslastexam/demo-hello")) == "agentslastexam.demo-hello"
    for invalid in ("demo", "a/b/c", "Org/demo", "org/", "/demo", "org/demo.v1", "-org/demo"):
        with pytest.raises(ValueError, match="org/name"):
            TaskId(invalid)


def test_task_config_requires_every_field_whose_default_differs_from_harbor() -> None:
    assert config().environment.kind is ImageKind.CONTAINER
    for table, key in (
        ("", "schema_version"),
        ("task", "name"),
        ("environment", "cpus"),
        ("environment", "memory_mb"),
        ("agent", "user"),
        ("agent", "timeout_sec"),
        ("agent", "network_mode"),
        ("verifier", "timeout_sec"),
    ):
        payload = merged_config(MINIMAL_TASK_CONFIG, {table: {key: None}} if table else {key: None})
        with pytest.raises(ValidationError) as raised:
            TaskConfig.model_validate(payload)
        assert raised.value.errors()[0]["type"] == "missing"
        assert raised.value.errors()[0]["loc"] == ((table, key) if table else (key,))
    for version in ("1.3", "1.4"):
        with pytest.raises(ValidationError, match=r"1\.4\+ale\.1"):
            config({"schema_version": version})


@pytest.mark.parametrize(
    ("layer", "key"),
    [
        ({"steps": [{"name": "one"}]}, "steps"),
        ({"source": "x"}, "source"),
        ({"environment": {"healthcheck": {"command": "true"}}}, "environment.healthcheck"),
        ({"environment": {"network_mode": "public"}}, "environment.network_mode"),
        ({"environment": {"workdir": "/app"}}, "environment.workdir"),
        ({"verifier": {"user": "root"}}, "verifier.user"),
        (
            {"verifier": {"environment": {"cpus": 1, "memory_mb": 512, "os": "linux"}}},
            "verifier.environment.os",
        ),
        ({"artifacts": [{"source": "/home/user/output"}]}, "table-form artifacts"),
    ],
)
def test_harbor_keys_ale_does_not_implement_say_so(layer: dict[str, object], key: str) -> None:
    with pytest.raises(ValidationError, match=f"{key} (is|are) not supported by ALE"):
        config(layer)


def test_task_config_rejects_unknown_keys() -> None:
    for layer in ({"domain": "demo"}, {"ale": {"sudo": True}}, {"task": {"org": "x"}}):
        with pytest.raises(ValidationError, match="Extra inputs"):
            config(layer)


@pytest.mark.parametrize(
    ("layer", "message"),
    [
        ({"environment": {"docker_image": "x", "vm": True}}, "docker_image is a container"),
        ({"environment": {"docker_image": "x", "vm_image": "d"}}, "docker_image is a container"),
        ({"environment": {"docker_image": "x", "os": "windows"}}, "docker_image is a container"),
        ({"environment": {"vm_base": "base.qcow2"}}, "vm_base requires os 'windows'"),
        (
            {"environment": {"os": "windows", "vm_base": "b.qcow2", "vm_image": "i.qcow2"}},
            "mutually exclusive",
        ),
        ({"agent": {"network_mode": "allowlist"}}, "non-empty allowed_hosts"),
        ({"agent": {"allowed_hosts": ["example.org"]}}, "requires network_mode 'allowlist'"),
        (
            {
                "verifier": {
                    "environment_mode": "shared",
                    "environment": {"cpus": 1, "memory_mb": 512},
                }
            },
            "shared",
        ),
        (
            {
                "verifier": {
                    "environment": {"docker_image": "x", "vm": True, "cpus": 1, "memory_mb": 512}
                }
            },
            "docker_image is a container",
        ),
        (
            {
                "environment": {"os": "windows", "vm_image": "w.qcow2"},
                "verifier": {"environment": {"docker_image": "x", "cpus": 1, "memory_mb": 512}},
            },
            "Windows verifier runs in a VM",
        ),
        ({"environment": {"cpus": 1.5}}, "valid integer"),
        ({"agent": {"workdir": "project"}}, "workdir must be an absolute path"),
        ({"environment": {"skills_dir": "skills"}}, "skills_dir must be an absolute path"),
        ({"environment": {"vm": "yes"}}, "valid boolean"),
        ({"agent": {"sudo": "yes"}}, "valid boolean"),
        (
            {
                "environment": {
                    "mcp_servers": [{"name": "s", "transport": "sse", "url": "http://x"}]
                }
            },
            "stdio",
        ),
        (
            {
                "environment": {
                    "mcp_servers": [{"name": "s", "transport": "stdio", "url": "http://x"}]
                }
            },
            "takes no url",
        ),
        (
            {"environment": {"mcp_servers": [{"name": "s", "transport": "streamable-http"}]}},
            "requires url",
        ),
        (
            {
                "environment": {
                    "mcp_servers": [
                        {
                            "name": "s",
                            "transport": "streamable-http",
                            "url": "https://x",
                            "headers": {"Authorization": "Bearer ${TOKEN}"},
                        }
                    ]
                }
            },
            "headers is not supported",
        ),
        (
            {
                "environment": {
                    "mcp_servers": [
                        {"name": "s", "transport": "stdio", "command": "a"},
                        {"name": "s", "transport": "stdio", "command": "b"},
                    ]
                }
            },
            "names must be unique",
        ),
    ],
)
def test_task_config_cross_field_rules(layer: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        config(layer)


def test_verification_mode_follows_harbor() -> None:
    assert config().verifier.mode is VerificationMode.SHARED
    separate = {"verifier": {"environment_mode": "separate"}}
    assert config(separate).verifier.mode is VerificationMode.SEPARATE
    declared = {"verifier": {"environment": {"cpus": 2, "memory_mb": 1024}}}
    assert config(declared).verifier.mode is VerificationMode.SEPARATE


def test_network_modes_map_onto_the_runtime_policy() -> None:
    assert config().agent.network == NetworkPolicy(mode=NetworkMode.BLOCK)
    allowlist = config({"agent": {"network_mode": "allowlist", "allowed_hosts": ["pypi.org"]}})
    assert allowlist.agent.network == NetworkPolicy(
        mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org",)
    )
    assert config({"agent": {"network_mode": "public"}}).agent.network.mode is NetworkMode.OPEN


def test_windows_artifacts_use_windows_absolute_path_rules() -> None:
    windows = {"environment": {"os": "windows", "vm_image": "w.qcow2"}}
    assert config(windows, {"artifacts": [r"C:\Users\user\output"]}).artifacts == (
        r"C:\Users\user\output",
    )
    with pytest.raises(ValidationError, match="absolute"):
        config(windows, {"artifacts": ["output"]})


def test_network_and_agent_resource_contracts_remain_strict() -> None:
    with pytest.raises(ValidationError):
        NetworkPolicy(mode=NetworkMode.ALLOWLIST)
    with pytest.raises(ValidationError):
        NetworkPolicy(mode=NetworkMode.BLOCK, allowed_hosts=("example.com",))
    assert SkillSource(path="/skills/reviewer").path == "/skills/reviewer"
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
        ("/output/../etc",),
        ("/",),
    ],
)
def test_artifacts_are_absolute_unique_and_safe(artifacts: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        config({"artifacts": list(artifacts)})

"""Contract behavior shared by loaders and runtime."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.support import MINIMAL_TASK_CONFIG, merged_config

from ale.core.ids import TaskId, canonical_json, content_hash, task_component
from ale.core.taskspec import (
    SCHEMA_VERSION,
    Artifact,
    ImageKind,
    ImageSpec,
    McpSource,
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    SkillSource,
    SseMcpServer,
    StdioMcpServer,
    StreamableHttpMcpServer,
    TaskConfig,
    TaskSpec,
    ToolProvision,
    VerificationMode,
    VerifierResources,
    VerifySpec,
    host_allowed,
)
from ale.core.verdict import Status, Verdict

pytestmark = pytest.mark.unit
IMAGE = "ghcr.io/acme/image:v1"


def make_spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = {
        "name": TaskId("agentslastexam/demo-hello"),
        "image": {"kind": "container"},
        "instruction": "Write hello into /home/user/output/result.txt",
    }
    return TaskSpec(**(base | overrides))  # type: ignore[arg-type]


_OPEN = NetworkPolicy(mode=NetworkMode.OPEN)
_BLOCK = NetworkPolicy(mode=NetworkMode.BLOCK)
_PYPI = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org",))
_ALLOW_PYPI = {"network_mode": "allowlist", "allowed_hosts": ["pypi.org"]}
_OWN = {"cpus": 1, "memory_mb": 512}


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
    assert ImageSpec(kind=None, ref="ghcr.io/acme/image:v1").kind is None
    for payload in (
        {},
        {"kind": "unknown"},
        {"kind": "container", "ref": "   "},
        {"kind": "container", "build": "image"},
        {"kind": "vm", "path": "/tmp/disk.qcow2"},
        {"kind": "vm", "base_ref": "   "},
        {"kind": "vm", "ref": "task.qcow2", "base_ref": "base.qcow2"},
        {"kind": "container", "base_ref": "base:latest"},
        {"kind": None, "base_ref": "base:latest"},
    ):
        with pytest.raises(ValidationError):
            ImageSpec.model_validate(payload)
    # Only a container is built from the Task folder; a VM is named or built on a base.
    for kind in ("vm", None):
        with pytest.raises(ValidationError, match="only a container image is built"):
            ImageSpec.model_validate({"kind": kind})

    with pytest.raises(ValidationError, match="base ref"):
        VerifySpec(
            environment_mode="separate",
            resources=VerifierResources(cpus=1, memory_mb=512, storage_mb=None, gpus=0),
            image={"kind": "vm", "base_ref": "/images/base.qcow2"},
        )


def test_only_a_linux_registry_image_leaves_its_kind_open() -> None:
    named = {"kind": None, "ref": IMAGE}
    resources = VerifierResources(cpus=1, memory_mb=512, storage_mb=None, gpus=0)
    separate = {"environment_mode": "separate", "resources": resources}
    assert make_spec(image=named).image.kind is None
    assert make_spec(verify={**separate, "image": named}).verify.image is not None
    disk = {"kind": None, "ref": "/images/disk.qcow2"}
    for overrides in (
        {"os": "windows", "image": named},
        {
            "os": "macos",
            "image": {"kind": "vm", "ref": IMAGE},
            "verify": {**separate, "image": named},
        },
        {"image": disk},
        {"verify": {**separate, "image": disk}},
    ):
        with pytest.raises(ValidationError, match="leaves its kind open"):
            make_spec(**overrides)


def test_task_spec_hash_tracks_semantics() -> None:
    assert make_spec().spec_hash == make_spec().spec_hash
    assert make_spec().spec_hash != make_spec(instruction="different").spec_hash
    assert make_spec().spec_hash != make_spec(resources={"cpus": 2}).spec_hash


def test_an_open_verifier_network_keeps_the_spec_hash() -> None:
    """Open is the default, left out of a dump, so a Task that names no verifier network
    keeps the hash it had; any other policy is part of it."""
    assert "network" not in VerifySpec().model_dump()
    assert make_spec(verify={"network": _OPEN}).spec_hash == make_spec().spec_hash
    sealed = make_spec(verify={"network": _BLOCK})
    assert sealed.spec_hash != make_spec().spec_hash
    assert TaskSpec.model_validate_json(sealed.model_dump_json()) == sealed
    with pytest.raises(ValidationError, match="verifier's network"):
        make_spec(verify={"network": _BLOCK}, validation={"network": _OPEN})


@pytest.mark.parametrize(
    ("agent", "verifier", "mode", "expected"),
    [
        (_BLOCK, _OPEN, "shared", _BLOCK),
        (_OPEN, _BLOCK, "shared", _BLOCK),
        (_OPEN, _OPEN, "shared", _OPEN),
        (_BLOCK, _PYPI, "shared", _PYPI),
        (_PYPI, _OPEN, "shared", _PYPI),
        (
            _PYPI,
            NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org", "example.org")),
            "shared",
            NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org", "example.org")),
        ),
        # A verifier of its own applies its network in its own Sandbox.
        (_OPEN, _BLOCK, "separate", _OPEN),
        (_BLOCK, _PYPI, "snapshot", _BLOCK),
    ],
)
def test_a_solver_can_apply_both_phases_networks(
    agent: NetworkPolicy, verifier: NetworkPolicy, mode: str, expected: NetworkPolicy
) -> None:
    """A shared solver is created with the least open mode either phase needs."""
    resources = VerifierResources(cpus=1, memory_mb=512, storage_mb=None, gpus=0)
    placed = {} if mode == "shared" else {"environment_mode": mode, "resources": resources}
    spec = make_spec(network=agent, verify={**placed, "network": verifier})
    assert spec.solver_network == expected


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


def test_task_config_requires_only_its_version_and_name() -> None:
    assert config().environment.image is None
    minimal = TaskConfig.model_validate(
        {"schema_version": SCHEMA_VERSION, "task": {"name": "org/demo"}}
    )
    assert (minimal.environment.cpus, minimal.environment.memory_mb) == (1, 4096)
    assert minimal.environment.build_timeout_sec == 3600
    assert (minimal.agent.user, minimal.agent.timeout_sec) == ("user", 900)
    assert minimal.agent_network.mode is NetworkMode.BLOCK
    assert minimal.verifier.timeout_sec == 600
    for table, key in (("", "schema_version"), ("task", "name")):
        payload = merged_config(MINIMAL_TASK_CONFIG, {table: {key: None}} if table else {key: None})
        with pytest.raises(ValidationError) as raised:
            TaskConfig.model_validate(payload)
        assert raised.value.errors()[0]["type"] == "missing"
        assert raised.value.errors()[0]["loc"] == ((table, key) if table else (key,))
    # The format is named by the ALE version that last changed it, and only that one is
    # read.
    assert SCHEMA_VERSION == "0.2.0"
    for version in ("1.4", "1.4+ale.1", "0.1.1"):
        with pytest.raises(ValidationError, match=r"0\.2\.0"):
            config({"schema_version": version})


@pytest.mark.parametrize(
    ("layer", "key"),
    [
        ({"steps": [{"name": "one"}]}, "steps"),
        ({"source": "x"}, "source"),
        ({"environment": {"tpu": {"type": "v6e", "topology": "2x4"}}}, "environment.tpu"),
        (
            {"verifier": {"environment": {"healthcheck": {"command": "true"}}}},
            "verifier.environment.healthcheck",
        ),
        (
            {"verifier": {"environment": {"cpus": 1, "memory_mb": 512, "os": "linux"}}},
            "verifier.environment.os",
        ),
        ({"artifacts": [{"source": "/o", "service": "db"}]}, "artifact service"),
    ],
)
def test_harbor_keys_ale_does_not_implement_say_so(layer: dict[str, object], key: str) -> None:
    with pytest.raises(ValidationError, match=f"{key} (is|are) not supported by ALE"):
        config(layer)


def test_task_config_rejects_unknown_keys() -> None:
    for layer in (
        {"domain": "demo"},
        {"ale": {"sudo": True}},
        {"task": {"org": "x"}},
        {"environment": {"vm": True}},
    ):
        with pytest.raises(ValidationError, match="Extra inputs"):
            config(layer)


def test_an_mcp_server_s_transport_is_sse_unless_written_as_in_harbor() -> None:
    servers = config(
        {
            "environment": {
                "mcp_servers": [
                    {"name": "a", "url": "https://mcp.example.org/sse"},
                    {"name": "b", "transport": "http", "url": "https://mcp.example.org/mcp"},
                    {
                        "name": "c",
                        "transport": "sse",
                        "url": "https://mcp.example.org/sse",
                        "headers": {"Authorization": "Bearer ${SEARCH_TOKEN}"},
                    },
                ]
            }
        }
    ).environment.mcp_servers
    assert [type(entry.server) for entry in servers] == [
        SseMcpServer,
        StreamableHttpMcpServer,
        SseMcpServer,
    ]


def test_an_artifact_destination_is_harbor_s_relative_host_path() -> None:
    kept = config(
        {"artifacts": [{"source": "/o", "destination": "out/report"}, "/p", {"source": "/q"}]}
    ).artifacts
    assert [artifact.destination for artifact in kept] == ["out/report", None, None]
    # Unset, it leaves every existing specification's identity as it was.
    assert "destination" not in kept[1].model_dump()
    for destinations, message in (
        (["/abs"], "relative path"),
        (["a/../b"], "relative path"),
        (["a\\b"], "relative path"),
        (["."], "relative path"),
        (["0001/x"], "takes a name ALE keeps"),
        (["snapshot.json"], "takes a name ALE keeps"),
        (["out", "out/inner"], "overlap"),
        (["same", "same"], "overlap"),
    ):
        artifacts = [
            {"source": f"/s{index}", "destination": destination}
            for index, destination in enumerate(destinations)
        ]
        with pytest.raises(ValidationError, match=message):
            config({"artifacts": artifacts})


def test_the_verifier_s_account_is_harbor_s_name_or_uid() -> None:
    assert config({"verifier": {"user": "grader"}}).verifier.user == "grader"
    assert config({"verifier": {"user": 1000}}).verifier.user == 1000
    for user in ("Grader", "a b", -1, True):
        with pytest.raises(ValidationError):
            config({"verifier": {"user": user}})
    with pytest.raises(ValidationError, match="Windows verification runs as Administrator"):
        config({"environment": {"os": "windows"}, "verifier": {"user": "grader"}})


def test_docker_image_is_harbor_s_name_for_image() -> None:
    named = config(
        {"environment": {"docker_image": "ghcr.io/acme/task:v1"}},
        {"verifier": {"environment": {"docker_image": "ghcr.io/acme/verifier:v1"}}},
    )
    assert named.environment.image == "ghcr.io/acme/task:v1"
    assert named.verifier.environment is not None
    assert named.verifier.environment.image == "ghcr.io/acme/verifier:v1"
    for layer in (
        {"environment": {"image": "a:1", "docker_image": "a:1"}},
        {"verifier": {"environment": {"image": "a:1", "docker_image": "b:1"}}},
    ):
        with pytest.raises(ValidationError, match="image and docker_image are one field"):
            config(layer)


@pytest.mark.parametrize(
    ("layer", "message"),
    [
        ({"environment": {"image": ""}}, "at least 1 character"),
        ({"agent": {"network_mode": "allowlist"}}, "non-empty allowed_hosts"),
        ({"agent": {"allowed_hosts": ["example.org"]}}, "requires network_mode 'allowlist'"),
        *(
            (
                {"agent": {"network_mode": "allowlist", "allowed_hosts": [host]}},
                "reaches the Host",
            )
            for host in (
                "127.0.0.1",
                "127.0.0.0/8",
                "169.254.169.254",
                "0.0.0.0/0",
                "::1",
                "::/0",
                "::ffff:127.0.0.1",
                "fe80::1",
            )
        ),
        (
            {"agent": {"network_mode": "allowlist", "allowed_hosts": ["10.0.0.1/8"]}},
            "not a hostname",
        ),
        (
            {"agent": {"network_mode": "allowlist", "allowed_hosts": ["foo.*.org"]}},
            "not a hostname",
        ),
        (
            {
                "verifier": {
                    "environment_mode": "shared",
                    "environment": {"cpus": 1, "memory_mb": 512},
                }
            },
            "shared",
        ),
        ({"environment": {"cpus": 1.5}}, "valid integer"),
        ({"environment": {"skills_dir": "skills"}}, "skills_dir must be an absolute path"),
        ({"environment": {"gpu_types": ["H100"]}}, r"\[environment\]\.gpu_types requires gpus"),
        (
            {"verifier": {"environment": {"gpu_types": ["H100"]}}},
            r"\[verifier\.environment\]\.gpu_types requires gpus",
        ),
        (
            {"environment": {"gpu_memory_mb": 8192}},
            r"\[environment\]\.gpu_memory_mb requires gpus",
        ),
        (
            {"verifier": {"environment": {"gpu_memory_mb": 8192}}},
            r"\[verifier\.environment\]\.gpu_memory_mb requires gpus",
        ),
        ({"environment": {"gpus": 1, "gpu_memory_mb": 0}}, "greater than or equal to 1"),
        ({"environment": {"gpus": 1, "gpu_memory_mb": 1.5}}, "valid integer"),
        ({"environment": {"gpus": 1, "gpu_types": []}}, "at least 1 item"),
        ({"environment": {"gpus": 1, "gpu_types": [" - "]}}, "should match pattern"),
        ({"agent": {"sudo": "yes"}}, "valid boolean"),
        ({"environment": {"mcp_servers": [{"name": "s"}]}}, "transport 'sse' requires url"),
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
        *(
            (
                {
                    "environment": {
                        "mcp_servers": [
                            {
                                "name": "s",
                                "transport": "streamable-http",
                                "url": "https://x",
                                "headers": headers,
                            }
                        ]
                    }
                },
                message,
            )
            for headers, message in (
                ({"Authorization": "Bearer sk-written-down"}, "holds a credential"),
                ({"X-Api-Key": "${KEY:-sk-written-down}"}, "holds a credential"),
                ({"Cookie": "session=abc"}, "holds a credential"),
                ({"X Bad": "1"}, "not an HTTP header name"),
                ({"X-Region": "a\nb"}, "line break"),
                ({"X-Region": "${region"}, "must start"),
                ({"X-Region": "${ALE_REGION}"}, "cannot refer"),
            )
        ),
        (
            {
                "environment": {
                    "mcp_servers": [
                        {
                            "name": "s",
                            "transport": "stdio",
                            "command": "a",
                            "headers": {"X-Region": "eu"},
                        }
                    ]
                }
            },
            "takes no url or headers",
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
    assert config().agent_network == NetworkPolicy(mode=NetworkMode.BLOCK)
    allowlist = config({"agent": {"network_mode": "allowlist", "allowed_hosts": ["pypi.org"]}})
    assert allowlist.agent_network == NetworkPolicy(
        mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org",)
    )
    assert config({"agent": {"network_mode": "public"}}).agent_network.mode is NetworkMode.OPEN


@pytest.mark.parametrize(
    ("agent", "environment", "expected"),
    [
        # Nothing named: ALE's default.
        ({}, {}, NetworkPolicy(mode=NetworkMode.BLOCK)),
        # [environment] is Harbor's baseline, which the agent gets unless it names its own.
        ({}, {"network_mode": "public"}, NetworkPolicy(mode=NetworkMode.OPEN)),
        (
            {},
            {"network_mode": "allowlist", "allowed_hosts": ["pypi.org"]},
            NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org",)),
        ),
        ({"network_mode": "no-network"}, {"network_mode": "public"}, NetworkPolicy()),
        # Harbor's deprecated switch, read only without a mode.
        ({}, {"allow_internet": True}, NetworkPolicy(mode=NetworkMode.OPEN)),
        ({}, {"allow_internet": False}, NetworkPolicy(mode=NetworkMode.BLOCK)),
        (
            {},
            {"allow_internet": True, "network_mode": "no-network"},
            NetworkPolicy(mode=NetworkMode.BLOCK),
        ),
    ],
)
def test_the_agent_s_network_resolves_as_harbor_s_does(
    agent: dict[str, object], environment: dict[str, object], expected: NetworkPolicy
) -> None:
    resolved = config({"agent": {"network_mode": None, **agent}, "environment": environment})
    assert resolved.agent_network == expected


@pytest.mark.parametrize(
    ("verifier", "environment", "expected"),
    [
        # Nothing named: open, ALE's default for verification.
        ({}, {}, _OPEN),
        # [verifier]'s own policy wins in every topology.
        ({"network_mode": "no-network"}, {"network_mode": "public"}, _BLOCK),
        ({**_ALLOW_PYPI}, {}, _PYPI),
        (
            {"network_mode": "no-network", "environment": {**_OWN, "network_mode": "public"}},
            {},
            _BLOCK,
        ),
        # A shared verifier has [environment]'s baseline, as in Harbor.
        ({}, {"network_mode": "no-network"}, _BLOCK),
        ({}, {"allow_internet": False}, _BLOCK),
        ({}, {**_ALLOW_PYPI}, _PYPI),
        # A verifier of its own with an environment: that environment's baseline...
        ({"environment": {**_OWN, "network_mode": "no-network"}}, {}, _BLOCK),
        ({"environment": {**_OWN, **_ALLOW_PYPI}}, {}, _PYPI),
        ({"environment": {**_OWN, "allow_internet": False}}, {"network_mode": "public"}, _BLOCK),
        (
            {"environment": {**_OWN, "allow_internet": True, "network_mode": "no-network"}},
            {},
            _BLOCK,
        ),
        # ... and public when it names none, as Harbor's separate environment is, whatever
        # [environment] says.
        ({"environment": {**_OWN}}, {"network_mode": "no-network"}, _OPEN),
        (
            {"environment_mode": "snapshot", "environment": {**_OWN}},
            {"network_mode": "no-network"},
            _OPEN,
        ),
        (
            {"environment_mode": "snapshot", "environment": {**_OWN, "network_mode": "no-network"}},
            {"network_mode": "public"},
            _BLOCK,
        ),
        # Without an environment of its own, [environment]'s baseline again.
        ({"environment_mode": "separate"}, {"network_mode": "no-network"}, _BLOCK),
        ({"environment_mode": "snapshot"}, {"allow_internet": False}, _BLOCK),
    ],
)
def test_the_verifier_s_network_resolves_as_harbor_s_does(
    verifier: dict[str, object], environment: dict[str, object], expected: NetworkPolicy
) -> None:
    resolved = config({"verifier": verifier, "environment": environment})
    assert resolved.verifier_network == expected
    # The agent's own network is never the verifier's.
    assert resolved.agent_network == config({"environment": environment}).agent_network


def test_a_verifier_s_network_obeys_the_agent_s_host_rules() -> None:
    for layer, message in (
        ({"verifier": {"network_mode": "allowlist"}}, "non-empty allowed_hosts"),
        ({"verifier": {"allowed_hosts": ["pypi.org"]}}, "requires network_mode 'allowlist'"),
        (
            {"verifier": {"network_mode": "allowlist", "allowed_hosts": ["169.254.169.254"]}},
            "metadata service",
        ),
        (
            {"verifier": {"environment": {**_OWN, "network_mode": "allowlist"}}},
            "non-empty allowed_hosts",
        ),
        (
            {"verifier": {"environment": {**_OWN, "allowed_hosts": ["pypi.org"]}}},
            "requires network_mode 'allowlist'",
        ),
        ({"verifier": {"network_mode": "offline"}}, "network_mode"),
        ({"verifier": {"environment": {**_OWN, "allow_internet": "yes"}}}, "allow_internet"),
        # validation/ runs with the verifier's network.
        (
            {"validation": {"environment": {**_OWN, "network_mode": "public"}}},
            "verifier's network",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            config(layer)
    verifier = config({"verifier": {"network_mode": "allowlist", "allowed_hosts": ["PyPI.org."]}})
    assert verifier.verifier_network == _PYPI


def test_an_environment_baseline_obeys_the_agent_s_host_rules() -> None:
    for environment, message in (
        ({"network_mode": "allowlist"}, "non-empty allowed_hosts"),
        ({"allowed_hosts": ["pypi.org"]}, "requires network_mode 'allowlist'"),
        ({"network_mode": "allowlist", "allowed_hosts": ["127.0.0.1"]}, "reaches the Host"),
    ):
        with pytest.raises(ValidationError, match=message):
            config({"environment": environment})


def test_windows_artifacts_use_windows_path_rules() -> None:
    windows = {"environment": {"os": "windows", "image": "/images/w.qcow2"}}
    declared = config(windows, {"artifacts": [r"C:\Users\user\output"]}).artifacts
    assert declared == (Artifact(source=r"C:\Users\user\output"),)
    with pytest.raises(ValidationError, match="filesystem roots"):
        config(windows, {"artifacts": ["C:\\"]})


def test_harbor_s_spellings_are_accepted() -> None:
    assert config({"environment": {"os": "Windows", "image": "w.qcow2"}}).environment.os == (
        OperatingSystem.WINDOWS
    )
    http = {"name": "s", "transport": "http", "url": "https://mcp.example.org/mcp"}
    server = config({"environment": {"mcp_servers": [http]}}).environment.mcp_servers[0]
    assert server.transport == "streamable-http"


def test_allowed_hosts_take_harbor_s_normal_form() -> None:
    hosts = ["PyPI.org.", " files.pythonhosted.org", "10.0.0.1", "10.0.0.0/8", "2001:0DB8::1"]
    agent = config({"agent": {"network_mode": "allowlist", "allowed_hosts": hosts}}).agent
    assert agent.allowed_hosts == (
        "pypi.org",
        "files.pythonhosted.org",
        "10.0.0.1",
        "10.0.0.0/8",
        "2001:db8::1",
    )


@pytest.mark.parametrize(
    ("host", "allowed", "admitted"),
    [
        ("10.1.2.3", ("10.0.0.0/8",), True),
        ("11.1.2.3", ("10.0.0.0/8",), False),
        ("10.0.0.1", ("10.0.0.1",), True),
        ("2001:0db8::1", ("2001:db8::1",), True),
        ("2001:db8::1", ("2001:db8::/32",), True),
        ("2001:db8::1", ("10.0.0.0/8",), False),
        ("internal.example.org", ("10.0.0.0/8",), False),
        # A name pattern never admits an address, or `*.0.0.1` would reach the Host.
        ("127.0.0.1", ("*.0.0.1",), False),
    ],
)
def test_addresses_match_addresses_and_ranges(
    host: str, allowed: tuple[str, ...], admitted: bool
) -> None:
    assert host_allowed(host, allowed) is admitted


def test_a_credential_header_stays_off_plain_http_outside_the_sandbox() -> None:
    def server(url: str) -> dict[str, object]:
        return {
            "environment": {
                "mcp_servers": [
                    {
                        "name": "s",
                        "transport": "streamable-http",
                        "url": url,
                        "headers": {"Authorization": "Bearer ${TOKEN}"},
                    }
                ]
            }
        }

    config(server("https://mcp.example.org/mcp"))
    config(server("http://127.0.0.1:8000/mcp"))
    config(server("http://localhost:8000/mcp"))
    with pytest.raises(ValidationError, match="unencrypted"):
        config(server("http://mcp.example.org/mcp"))


def test_network_and_agent_resource_contracts_remain_strict() -> None:
    with pytest.raises(ValidationError):
        NetworkPolicy(mode=NetworkMode.ALLOWLIST)
    with pytest.raises(ValidationError):
        NetworkPolicy(mode=NetworkMode.BLOCK, allowed_hosts=("example.com",))
    assert SkillSource(path="/skills/reviewer").path == "/skills/reviewer"
    assert McpSource(builtin="cua-desktop").builtin == "cua-desktop"
    assert McpSource(builtin="cua-desktop", screenshot_max_dimension=768).screenshot_max_dimension
    for other in ({"builtin": "search"}, {"path": "/mcp/search.toml"}):
        with pytest.raises(ValidationError, match="cua-desktop only"):
            McpSource.model_validate({**other, "screenshot_max_dimension": 768})
    with pytest.raises(ValidationError):
        McpSource(builtin="cua-desktop", screenshot_max_dimension=0)
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
        ("/output/../etc",),
        ("../etc",),
        ("/",),
    ],
)
def test_artifacts_stay_inside_the_filesystem_tree(artifacts: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        config({"artifacts": list(artifacts)})


def test_artifacts_are_paths_or_tables_and_relative_ones_follow_the_agent() -> None:
    declared = config(
        {"artifacts": ["output", {"source": "/srv/app", "exclude": [".venv", "*.pyc"]}]}
    ).artifacts
    assert declared == (
        Artifact(source="output"),
        Artifact(source="/srv/app", exclude=(".venv", "*.pyc")),
    )
    spec = TaskSpec(
        name="agentslastexam/demo",
        image={"kind": "container"},
        instruction="x",
        workdir="project",
        artifacts=declared,
    )
    assert spec.start_directory("/home/user") == "/home/user/project"
    assert spec.artifact_path(declared[0], "/home/user") == "/home/user/project/output"
    assert spec.artifact_path(declared[1], "/home/user") == "/srv/app"
    windows = spec.model_copy(update={"os": OperatingSystem.WINDOWS})
    assert windows.artifact_path(declared[0], r"C:\Users\user") == (r"C:\Users\user\project\output")
    assert spec.model_copy(update={"workdir": "/srv"}).start_directory("/home/user") == "/srv"
    assert spec.model_copy(update={"workdir": None}).start_directory("/home/user") == "/home/user"

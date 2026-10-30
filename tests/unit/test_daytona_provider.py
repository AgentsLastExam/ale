"""The Daytona Provider's own logic, without a Daytona: admission, SSH, the image, the seal."""

from __future__ import annotations

import asyncio
import json
import shlex
import socket
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from ale.core.config import DaytonaProviderConfig, RunConfig
from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Identity,
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    SandboxRequest,
)
from ale.core.taskspec import ImageKind, NetworkMode, NetworkPolicy, Resources
from ale.run import guestd
from ale.run.guestd import tunnel as guest_tunnel
from ale.run.providers import RETAINING_PROVIDERS, ProviderRegistry, daytona_image, provider_named
from ale.run.providers import daytona as daytona_module
from ale.run.providers.daytona import (
    DaytonaProvider,
    DaytonaSandbox,
    _domain_allowlist,
    _egress_probe,
    _image_unavailable,
    _observed_allocation,
    _observed_gpus,
    _parse_ssh,
    _same_model,
    _sandbox_name,
    _seal_daemon,
    _SshHost,
    _Tunnel,
    _tunnel_plan,
)
from ale.run.providers.daytona_api import DaytonaError
from ale.run.providers.daytona_image import ImagePublisher, PublishedImage, image_config

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "a" * 64


def request(**overrides: Any) -> SandboxRequest:
    base: dict[str, Any] = {
        "episode_id": "episode-1",
        "prepared_image": PreparedTaskImage(
            kind=ImageKind.CONTAINER,
            source="solver-local",
            input_identity=DIGEST,
            image_source_identity=DIGEST,
            runtime_ref=DIGEST,
            prepared_identity=DIGEST,
        ),
        "resources": Resources(cpus=2, memory_mb=2048),
        "network": NetworkPolicy(mode=NetworkMode.BLOCK),
    }
    return SandboxRequest(**(base | overrides))


# --- admission ------------------------------------------------------------------


def test_daytona_allocates_whole_gib_and_the_provider_refuses_to_round() -> None:
    provider = DaytonaProvider()
    provider.accepts(request())
    provider.accepts(request(resources=Resources(memory_mb=1024, storage_mb=5120)))
    with pytest.raises(ProviderCapabilityError, match="whole number of GiB"):
        provider.accepts(request(resources=Resources(memory_mb=1536)))
    with pytest.raises(ProviderCapabilityError, match="storage_mb=1000"):
        provider.accepts(request(resources=Resources(memory_mb=1024, storage_mb=1000)))
    provider.accepts(request(resources=Resources(memory_mb=1024, gpus=1)))


def test_the_registry_builds_the_configured_container_provider() -> None:
    docker = ProviderRegistry(RunConfig()).get(ImageKind.CONTAINER)
    assert docker.name == "docker"
    config = RunConfig.model_validate({"container": {"provider": "daytona"}})
    chosen = ProviderRegistry(config).get(ImageKind.CONTAINER)
    assert chosen.name == "daytona"
    assert provider_named("daytona").name == "daytona"
    assert "daytona" in RETAINING_PROVIDERS


def test_the_api_key_is_named_not_stored() -> None:
    config = RunConfig.model_validate({"container": {"provider": "daytona"}})
    assert config.container.daytona.api_key_env == "ALE_DAYTONA_API_KEY"
    assert "dtn" not in config.model_dump_json()


# --- pure helpers ----------------------------------------------------------------


def test_ssh_command_is_read_for_user_host_and_port() -> None:
    assert _parse_ssh("ssh -p 2222 token123@ssh.example.com") == (
        "token123",
        "ssh.example.com",
        "2222",
    )
    assert _parse_ssh("ssh token123@ssh.app.daytona.io") == ("token123", "ssh.app.daytona.io", "22")
    with pytest.raises(ProviderStartError):
        _parse_ssh("echo nothing useful")


def test_the_tunnel_carries_the_gateway_alone_and_never_a_wildcard_address() -> None:
    """The egress proxy is not tunneled: allowlisted hosts are reached directly."""
    local_gateway, listeners, targets = _tunnel_plan(
        request(gateway_url="http://0.0.0.0:41000/v1", proxy_url="http://192.0.2.9:42000")
    )
    assert local_gateway == "http://127.0.0.1:41000/v1"
    assert listeners == [(41000, 0)]
    assert targets == [("127.0.0.1", 41000)]
    assert _tunnel_plan(request()) == (None, [], [])
    with pytest.raises(ProviderCapabilityError, match="explicit port"):
        _tunnel_plan(request(gateway_url="http://host/"))


def allowlist(*hosts: str) -> NetworkPolicy:
    return NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=hosts)


def test_the_allowlist_becomes_daytonas_with_names_and_patterns_as_written() -> None:
    assert _domain_allowlist(("PyPI.org.", "*.pythonhosted.org", "pypi.org")) == (
        "*.pythonhosted.org,pypi.org"
    )
    provider = DaytonaProvider()
    provider.accepts(request(network=allowlist("example.com")))
    for unusable in ("10.0.0.1", "example.com:8443", "https://example.com"):
        with pytest.raises(ProviderCapabilityError, match="takes domain names"):
            provider.accepts(request(network=allowlist(unusable)))
    with pytest.raises(ProviderCapabilityError, match="at most 100"):
        _domain_allowlist(tuple(f"host{index}.example" for index in range(101)))


def test_egress_is_probed_with_an_endpoint_the_allowlist_leaves_out() -> None:
    assert _egress_probe(()) == ("1.1.1.1:443", "one.one.one.one")
    assert _egress_probe(("*.one.one.one",)) == ("8.8.8.8:443", "dns.google")
    with pytest.raises(ProviderCapabilityError, match="covers every endpoint"):
        DaytonaProvider().accepts(request(network=allowlist("one.one.one.one", "dns.google")))


class NetworkApi:
    """Stores network settings as Daytona does, or, when told to, something else."""

    def __init__(self, stores: dict[str, Any] | None = None) -> None:
        self.stores = stores
        self.sent: list[dict[str, Any]] = []

    async def update_network(self, sandbox_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(settings)
        return self.stores if self.stores is not None else {"domainAllowList": None, **settings}


class ProbingClient:
    """A sandbox whose network is as the last setting says: open only when nothing limits it."""

    def __init__(self, api: NetworkApi) -> None:
        self.api = api
        self.envs: list[dict[str, str] | None] = []

    async def exec(self, argv: list[str], *, env: dict[str, str] | None = None, **_: Any):
        if argv[:2] == ["python3", "-c"] and "wrap_socket" in argv[2]:
            is_open = self.api.sent[-1] == {"networkBlockAll": False}
            return 0, "open" if is_open else "closed", "", False
        self.envs.append(env)
        return 0, "", "", False


def network_sandbox(api: NetworkApi, policy: NetworkPolicy) -> tuple[DaytonaSandbox, ProbingClient]:
    client = ProbingClient(api)
    sandbox = DaytonaSandbox(
        sandbox_id="s",
        daytona_id="d",
        request=request(network=policy),
        api=api,  # type: ignore[arg-type]
        ssh=None,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        file_client=client,  # type: ignore[arg-type]
        tunnel=None,
        local_gateway="http://127.0.0.1:41000",
        resolved_image=ResolvedImage(
            kind=ImageKind.CONTAINER,
            prepared_identity=DIGEST,
            observed_identity=DIGEST,
            observed_ref=f"registry/ale-task@{DIGEST}",
        ),
        allocation=ResourceAllocation(
            cpus=2, memory_mb=2048, sudo=False, network_mode=policy.mode, provider="daytona"
        ),
        agent_user="user",
        agent_home="/home/user",
    )
    return sandbox, client


async def test_the_agent_gets_daytonas_allowlist_and_no_proxy_variables() -> None:
    api = NetworkApi()
    sandbox, client = network_sandbox(api, allowlist("example.com"))
    await sandbox.open_egress()
    await sandbox.close_egress()
    assert api.sent == [
        {"networkBlockAll": False},
        {"networkBlockAll": False, "domainAllowList": "example.com"},
    ]
    await sandbox.exec(["true"], identity=Identity.AGENT)
    assert client.envs == [None]

    blocked, _ = network_sandbox(api, NetworkPolicy(mode=NetworkMode.BLOCK))
    await blocked.close_egress()
    assert api.sent[-1] == {"networkBlockAll": True}


async def test_a_setting_daytona_did_not_store_stops_the_phase() -> None:
    api = NetworkApi(stores={"networkBlockAll": True, "domainAllowList": None})
    sandbox, _ = network_sandbox(api, allowlist("example.com"))
    with pytest.raises(ProviderStartError, match="Daytona stored"):
        await sandbox.close_egress()


def test_what_daytona_gave_is_what_the_lock_records() -> None:
    allocation = _observed_allocation(
        request(), {"cpu": 2, "memory": 2, "disk": 3, "networkBlockAll": True}
    )
    assert (allocation.cpus, allocation.memory_mb, allocation.storage_mb) == (2, 2048, 3072)
    with pytest.raises(ProviderStartError, match="Daytona gave 1 CPU"):
        _observed_allocation(request(), {"cpu": 1, "memory": 2, "disk": 3})
    with pytest.raises(ProviderStartError, match="Daytona gave"):
        _observed_allocation(
            request(resources=Resources(cpus=2, memory_mb=2048, storage_mb=8192)),
            {"cpu": 2, "memory": 2, "disk": 3},
        )


def test_names_and_failure_classification() -> None:
    assert _sandbox_name("Episode_7-ab12") == "ale-episode-7-ab12"
    assert len(_sandbox_name("x" * 200)) <= 60
    assert _image_unavailable("failed to resolve source metadata ... 401 Unauthorized")
    assert _image_unavailable("manifest unknown")
    assert not _image_unavailable("cpu exceeds the limit")


# --- SSH -------------------------------------------------------------------------


class FakeApi:
    def __init__(self) -> None:
        self.minted = 0

    async def ssh_command(self, sandbox_id: str, *, minutes: int) -> str:
        self.minted += 1
        return f"ssh -p 22 secret-token-{self.minted}@ssh.example.com"


async def test_an_ssh_token_goes_in_a_private_file_not_on_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daytona_module, "cache_root", lambda: tmp_path / "cache")
    ssh = _SshHost(FakeApi(), "sandbox-1")  # type: ignore[arg-type]
    try:
        argv = await ssh.argv("echo hi")
        assert argv[0] == "ssh" and argv[-1] == "echo hi"
        assert "secret-token-1" not in " ".join(argv)
        config = Path(argv[argv.index("-F") + 1])
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        text = config.read_text()
        assert "User secret-token-1" in text
        assert "ClearAllForwardings yes" in text and "BatchMode yes" in text
        again = await ssh.argv("echo hi")  # every session mints its own token
        assert "secret-token-2" in Path(again[again.index("-F") + 1]).read_text()
    finally:
        ssh.close()
    assert not config.exists()


# --- the seal --------------------------------------------------------------------


class ScriptedClient:
    """Answers the two scripts ``_seal_daemon`` runs: the rule installer and the probe."""

    def __init__(self, reachable: list[str], seal_fails: bool = False) -> None:
        self.reachable = reachable
        self.seal_fails = seal_fails
        self.calls: list[tuple[list[str], str | None]] = []

    async def exec(self, argv: list[str], *, run_as: str | None = None, **_: Any):
        self.calls.append((argv, run_as))
        if run_as is None:
            if self.seal_fails:
                return 1, "", "iptables: Permission denied", False
            return 0, json.dumps([2280, 22220, 22222, 33333, 8080]), "", False
        return 0, json.dumps(self.reachable), "", False


async def test_the_agent_is_probed_on_every_port_the_seal_covers() -> None:
    client = ScriptedClient(reachable=[])
    await _seal_daemon(client, "user")  # type: ignore[arg-type]
    (_, as_root), (probe, as_agent) = client.calls
    assert as_root is None and as_agent == "user"
    assert probe[-1] == "2280,22220,22222,33333,8080"  # the daemon's own extra port too


async def test_a_daemon_the_agent_can_still_reach_stops_the_sandbox() -> None:
    with pytest.raises(ProviderCapabilityError, match=r"127\.0\.0\.1:2280"):
        await _seal_daemon(ScriptedClient(reachable=["127.0.0.1:2280"]), "user")  # type: ignore[arg-type]


async def test_rules_that_cannot_be_installed_stop_the_sandbox() -> None:
    with pytest.raises(ProviderCapabilityError, match="Permission denied"):
        await _seal_daemon(ScriptedClient([], seal_fails=True), "user")  # type: ignore[arg-type]


# --- GPUs ------------------------------------------------------------------------

H100 = (
    "GPU-71bf7b61-c3fa-b26c-37c2-b5abe422cb55, NVIDIA H100 80GB HBM3, 00000000:08:10.0, "
    "580.126.16\n"
)
SANDBOX = {"id": "sandbox-1", "daemonVersion": "0.225.2"}


class GpuClient:
    """Answers ``nvidia-smi`` the way a sandbox with the given GPUs would."""

    def __init__(self, output: str, code: int = 0) -> None:
        self.output = output
        self.code = code
        self.run_as: list[str | None] = []

    async def exec(self, argv: list[str], *, run_as: str | None = None, **_: Any):
        assert argv[0] == "nvidia-smi"
        self.run_as.append(run_as)
        return self.code, self.output, "" if self.code == 0 else "permission denied", False


def gpu_request(gpus: int = 1) -> SandboxRequest:
    return request(resources=Resources(cpus=2, memory_mb=8192, gpus=gpus))


def test_a_gpu_request_names_the_configured_models_and_is_never_spot() -> None:
    published = PublishedImage(f"registry/ale-task@{DIGEST}", DIGEST, "framed", DIGEST)
    body = DaytonaProvider()._body(gpu_request(2), published, "s", None)
    assert (body["gpu"], body["gpuType"], body["spot"]) == (2, ["H100"], False)

    anything = DaytonaProvider(DaytonaProviderConfig(gpu_types=()))
    assert "gpuType" not in anything._body(gpu_request(), published, "s", None)
    assert "gpu" not in DaytonaProvider()._body(request(), published, "s", None)


async def test_the_lock_records_the_gpus_as_the_agent_sees_them() -> None:
    client = GpuClient(H100)
    allocation = await _observed_gpus(client, gpu_request(), SANDBOX, ("H100",))  # type: ignore[arg-type]
    assert client.run_as == ["user"]
    assert allocation.requested_count == 1
    (device,) = allocation.observed_devices
    assert device.model == "NVIDIA H100 80GB HBM3" and device.driver_version == "580.126.16"
    assert allocation.provider_addresses == (device.id,)
    assert allocation.lease_keys == (f"daytona:sandbox-1/{device.id}",)
    assert allocation.runtime_identity == "daytona-daemon/0.225.2"


async def test_gpus_other_than_requested_stop_the_sandbox() -> None:
    with pytest.raises(ProviderStartError, match="attached 1 GPUs, not the 2"):
        await _observed_gpus(GpuClient(H100), gpu_request(2), SANDBOX, ("H100",))  # type: ignore[arg-type]
    with pytest.raises(ProviderStartError, match="gpu_types"):
        await _observed_gpus(GpuClient(H100), gpu_request(), SANDBOX, ("RTX-4090",))  # type: ignore[arg-type]
    with pytest.raises(ProviderCapabilityError, match="agent cannot use"):
        await _observed_gpus(GpuClient("", code=9), gpu_request(), SANDBOX, ())  # type: ignore[arg-type]
    with pytest.raises(ProviderStartError, match="gave 0 GPUs"):
        _observed_allocation(gpu_request(), {"cpu": 2, "memory": 8, "disk": 512})


def test_daytona_gpu_type_names_match_what_nvidia_smi_reports() -> None:
    assert _same_model("H100", "NVIDIA H100 80GB HBM3")
    assert _same_model("RTX-4090", "NVIDIA GeForce RTX 4090")
    assert _same_model("RTX-PRO-6000", "NVIDIA RTX PRO 6000 Blackwell Server Edition")
    assert not _same_model("H100", "NVIDIA H200")


# --- the image -------------------------------------------------------------------


class FakeDocker:
    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str] | None] = []
        self.dockerfile = ""
        self.tmp_path = tmp_path
        self.pushes = 0
        self.local_images = {DIGEST}

    async def __call__(
        self,
        *argv: str,
        environment: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float = 300,
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        self.environments.append(environment)
        verb = argv[0]
        if verb == "buildx":
            context = Path(argv[-1])
            self.dockerfile = (context / "Dockerfile").read_text()
            Path(argv[argv.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.config.digest": "sha256:" + "b" * 64})
            )
            self.local_images.add("sha256:" + "b" * 64)
            return 0, "", ""
        if verb == "image" and argv[1] == "inspect":
            if "{{.Id}}" in argv:
                return (0, argv[-1], "") if argv[-1] in self.local_images else (1, "", "no")
            return (
                0,
                json.dumps(
                    {
                        "Env": ["PATH=/bin", "DISPLAY=:1", "EMPTY="],
                        "User": "user",
                        "WorkingDir": "/home/user",
                        "Entrypoint": None,
                        "Cmd": ["/usr/bin/supervisord", "-c", "/etc/s.conf"],
                    }
                ),
                "",
            )
        if verb == "push":
            self.pushes += 1
            return 0, f"latest: digest: sha256:{'c' * 64} size: 123\n", ""
        return 0, "", ""


class FakeRegistryApi:
    def __init__(self, existing: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.minted = 0
        self.existing = existing

    async def registry_push_access(self) -> dict[str, str]:
        self.minted += 1
        return {
            "registryUrl": "cr.example.io",
            "project": "transient",
            "username": f"robot-{self.minted}",
            "secret": "registry-secret",
            "expiresAt": "2099-01-01T00:00:00.000Z",
        }

    async def registries(self) -> list[dict[str, Any]]:
        return [{"id": "reg-1", "name": "ale-transient"}] if self.existing else []

    async def create_registry(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created.append(body)
        return {"id": "reg-1"}

    async def update_registry(self, registry_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.updated.append({"id": registry_id, **body})
        return {"id": registry_id}


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker(tmp_path)
    monkeypatch.setattr(daytona_image, "_docker", fake)
    monkeypatch.setattr(daytona_image, "cache_root", lambda: tmp_path / "cache")
    return fake


async def test_the_image_is_adapted_pushed_by_digest_and_registered(docker: FakeDocker) -> None:
    api = FakeRegistryApi()
    published = await ImagePublisher(api).publish(DIGEST)  # type: ignore[arg-type]
    assert published.ref == f"cr.example.io/transient/ale-task@sha256:{'c' * 64}"
    assert published.digest == f"sha256:{'c' * 64}"
    assert published.adapted_id == "sha256:" + "b" * 64
    # The layer that lets Daytona's root-run daemon serve the framework, and nothing else.
    assert "USER root" in docker.dockerfile and "iptables" in docker.dockerfile
    assert api.created[0]["name"] == "ale-transient" and api.created[0]["project"] == "transient"
    login = next(call for call in docker.calls if call[0] == "login")
    assert "registry-secret" not in " ".join(login)  # the secret goes in on stdin
    config = docker.environments[docker.calls.index(login)]["DOCKER_CONFIG"]  # type: ignore[index]
    assert not Path(config).exists()  # kept out of the user's Docker configuration, then gone


async def test_a_published_image_is_not_pushed_again_until_it_is_lost(docker: FakeDocker) -> None:
    publisher = ImagePublisher(FakeRegistryApi())  # type: ignore[arg-type]
    first = await publisher.publish(DIGEST)
    assert await publisher.publish(DIGEST) == first
    assert await publisher.lookup(DIGEST) == first
    assert docker.pushes == 1
    await publisher.publish(DIGEST, force=True)
    assert docker.pushes == 2


async def test_the_organization_registry_entry_is_updated_not_duplicated(
    docker: FakeDocker,
) -> None:
    api = FakeRegistryApi(existing=True)
    publisher = ImagePublisher(api)  # type: ignore[arg-type]
    await publisher.publish(DIGEST)
    assert api.created == [] and api.updated[0]["id"] == "reg-1"
    await publisher.refresh_access()
    await publisher.publish(DIGEST, force=True)
    assert api.minted == 2 and len(api.updated) == 2  # new credentials, same entry


async def test_credentials_are_reused_while_they_last(docker: FakeDocker) -> None:
    api = FakeRegistryApi()
    publisher = ImagePublisher(api)  # type: ignore[arg-type]
    await publisher.publish(DIGEST)
    await publisher.publish("sha256:" + "d" * 64)
    assert api.minted == 1


async def test_the_image_is_read_the_way_docker_would_start_it(docker: FakeDocker) -> None:
    config = await image_config(DIGEST)
    assert config.argv == ("/usr/bin/supervisord", "-c", "/etc/s.conf")
    assert config.env == {"PATH": "/bin", "DISPLAY": ":1", "EMPTY": ""}
    assert (config.user, config.workdir) == ("user", "/home/user")


# --- the tunnel, against the real sandbox-side relay -----------------------------


class LocalRelay:
    """Stands in for SSH: runs the real ``tunnel.py`` as a local subprocess."""

    def __init__(self) -> None:
        self.starts = 0

    async def argv(self, remote_command: str) -> list[str]:
        self.starts += 1
        arguments = shlex.split(remote_command.partition("tunnel.py")[2])
        return [sys.executable, guest_tunnel.__file__, *arguments]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_the_host_tunnel_serves_the_sandbox_and_reconnects_when_ssh_drops() -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(65536))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = server.sockets[0].getsockname()[1]
    sandbox_port = free_port()
    relay = LocalRelay()
    tunnel = _Tunnel(
        relay,  # type: ignore[arg-type]
        listeners=[(sandbox_port, 0)],
        targets=[("127.0.0.1", target_port)],
    )
    await tunnel.start(timeout_sec=30)
    try:

        async def ask(message: bytes) -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", sandbox_port)
            writer.write(message)
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(65536), 10)
            writer.close()
            return reply

        assert await ask(b"through the relay") == b"through the relay"

        assert tunnel._process is not None
        tunnel._process.kill()  # the SSH session drops
        for _ in range(100):
            try:
                assert await ask(b"after the drop") == b"after the drop"
                break
            except (OSError, AssertionError):
                await asyncio.sleep(0.3)
        else:
            pytest.fail("the tunnel did not come back")
        assert relay.starts >= 2
    finally:
        await tunnel.close()
        server.close()


async def test_a_relay_that_cannot_listen_fails_the_start_with_its_reason() -> None:
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    tunnel = _Tunnel(
        LocalRelay(),  # type: ignore[arg-type]
        listeners=[(port, 0)],
        targets=[("127.0.0.1", 1)],
    )
    try:
        with pytest.raises(ProviderStartError, match="cannot listen"):
            await tunnel.start(timeout_sec=60)
    finally:
        await tunnel.close()
        taken.close()


def test_the_relay_the_provider_starts_ships_in_every_framework_layer() -> None:
    """A sandbox can only run what the image carries; this was once left out of the list."""
    assert "tunnel.py" in guestd.FILES
    assert (guestd.SOURCE / "tunnel.py").is_file()


class ListingApi:
    async def list_sandboxes(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        assert labels == {"ale.run.managed": "true", "ale.run.retention": "keep"}
        return [
            {
                "id": "live",
                "state": "started",
                "labels": {"ale.run.episode": "e", "ale.run.role": "solver"},
            },
            {"id": "going", "state": "destroying", "labels": {}},
            {"id": "gone", "state": "destroyed", "labels": {}},
        ]


async def test_a_sandbox_daytona_is_deleting_is_no_longer_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALE_DAYTONA_API_KEY", "key")
    provider = DaytonaProvider()
    provider._api_client = ListingApi()  # type: ignore[assignment]
    records = await provider.list_retained()
    assert [record["handle"] for record in records] == ["daytona:live"]
    assert records[0]["running"] is True and records[0]["episode"] == "e"


async def test_without_a_key_there_is_nothing_of_daytonas_to_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALE_DAYTONA_API_KEY", raising=False)
    assert await DaytonaProvider().list_retained() == []


class SharedOrganizationApi:
    """A sandbox another tool made, which wears labels that look like ALE's."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        return {
            "id": sandbox_id,
            "labels": {"ale.managed": "true", "ale.retention": "keep", "ale.native.operation": "x"},
        }

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)


class StartingApi:
    """Daytona as a start sees it: the sandbox exists as soon as the create arrives."""

    def __init__(self, *, reply_delay: float = 0.0, reply_lost: bool = False) -> None:
        self.reply_delay = reply_delay
        self.reply_lost = reply_lost
        self.made: dict[str, str] = {}
        self.deleted: list[str] = []

    async def create_sandbox(self, body: dict[str, Any]) -> dict[str, Any]:
        self.made[body["name"]] = "sandbox-1"
        await asyncio.sleep(self.reply_delay)
        if self.reply_lost:
            raise DaytonaError(0, "TimeoutError: the reply never came")
        return {"id": "sandbox-1"}

    async def get_sandbox(self, name: str) -> dict[str, Any] | None:
        return {"id": self.made[name]} if name in self.made else None

    async def wait_started(self, sandbox_id: str, *, timeout_sec: float) -> dict[str, Any]:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)


class PublishedImages:
    async def publish(self, framed_id: str, *, force: bool = False) -> PublishedImage:
        return PublishedImage(f"registry/ale-task@{DIGEST}", DIGEST, framed_id, DIGEST)


def starting(api: StartingApi) -> DaytonaProvider:
    provider = DaytonaProvider()
    provider._api_client = api  # type: ignore[assignment]
    provider._images_client = PublishedImages()  # type: ignore[assignment]
    return provider


@pytest.mark.parametrize("reply_delay", [0.0, 0.2], ids=["while-starting", "while-creating"])
async def test_a_start_cancelled_midway_deletes_what_daytona_made(reply_delay: float) -> None:
    """Cancelling a run must not leave its sandboxes running until their lifetime ends."""
    api = StartingApi(reply_delay=reply_delay)
    start = asyncio.ensure_future(starting(api)._start(request(), "framed", "s", None))
    await asyncio.sleep(0.05)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert api.deleted == ["sandbox-1"]


async def test_a_create_whose_reply_is_lost_is_found_by_name_and_deleted() -> None:
    api = StartingApi(reply_lost=True)
    with pytest.raises(ProviderStartError, match="reply never came"):
        await starting(api)._start(request(), "framed", "s", None)
    assert api.deleted == ["sandbox-1"]


async def test_a_sandbox_someone_elses_tool_made_is_never_destroyed() -> None:
    api = SharedOrganizationApi()
    provider = DaytonaProvider()
    provider._api_client = api  # type: ignore[assignment]
    with pytest.raises(ProviderCapabilityError, match="not an ALE-retained"):
        await provider.destroy_retained("daytona:their-sandbox")
    with pytest.raises(ProviderCapabilityError, match="not a Daytona sandbox handle"):
        await provider.destroy_retained("docker:their-sandbox")
    assert api.deleted == []

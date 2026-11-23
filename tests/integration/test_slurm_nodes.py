"""Sandboxes of several nodes on Slurm, run on a login node of an explicitly configured
cluster with a partition whose `max_nodes` is at least two."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from orbit.core.sandbox import Identity, ImageRef, SandboxRequest
from orbit.core.taskspec import NetworkMode, NetworkPolicy, Resources
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.providers.slurm import SlurmProvider
from orbit.run.scaffold import scaffold_task
from orbit.run.tasksets.manifest import load_tasks
from tests.support import CONTAINER_BASE, edit_task_config, provider_registry, slurm_config

pytestmark = [pytest.mark.integration, pytest.mark.needs_slurm]

#: Connect to each target, as the agent; print one line each.
PROBE = """
import socket, sys
for target in sys.argv[1:]:
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=5).close()
        print(target, "reached")
    except OSError as error:
        print(target, "refused", type(error).__name__)
"""


#: As root, the default route through `peers`, over rtnetlink, as `ip route replace default
#: dev peers` would, for an image without `ip`.
ROUTE = """
import socket, struct
index = socket.if_nametoindex("peers")
with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as sock:
    for kind, flags in ((25, 0), (24, 0x400)):  # RTM_DELROUTE, then RTM_NEWROUTE with CREATE
        # family, lengths, tos, the main table, boot protocol, link scope, unicast, flags
        body = struct.pack("=BBBBBBBBI", socket.AF_INET, 0, 0, 0, 254, 3, 253, 1, 0)
        body += struct.pack("=HHI", 8, 4, index)  # RTA_OIF
        sock.send(struct.pack("=IHHII", 16 + len(body), kind, 1 | 4 | flags, 1, 0) + body)
        code = struct.unpack_from("=i", sock.recv(4096), 16)[0]
        assert kind == 25 or code == 0, code
print("default dev peers")
"""


def _partition(provider: SlurmProvider, *, gpus: bool = False):  # type: ignore[no-untyped-def]
    for part in provider.config.partitions:
        if part.max_nodes >= 2 and (part.gpu_model is not None or not gpus):
            return part
    pytest.skip(f"no configured partition{' with GPUs' if gpus else ''} spans two nodes")


def _resources(part, **update: object) -> Resources:  # type: ignore[no-untyped-def]
    """Two small nodes of ``part``, with a GPU each where it has them."""
    return Resources(
        cpus=2,
        memory_mb=4096,
        gpus=1 if part.gpu_model else 0,
        gpu_types=(part.gpu_model.name,) if part.gpu_model else (),
        nodes=2,
    ).model_copy(update=update)


async def _request(provider: SlurmProvider, **update: object) -> SandboxRequest:
    image = await provider.prepare_image(ImageRef(kind="container", reference=CONTAINER_BASE))
    base: dict[str, object] = {
        "episode_id": "slurm-nodes",
        "prepared_image": image,
        "resources": _resources(_partition(provider)),
        "network": NetworkPolicy(mode=NetworkMode.BLOCK),
    }
    return SandboxRequest.model_validate(base | update)


def _agent(sandbox):  # type: ignore[no-untyped-def]
    async def agent(command: str) -> tuple[int, str, str]:
        result = await sandbox.exec(["sh", "-c", command], identity=Identity.AGENT)
        return result.exit_code, result.stdout, result.stderr

    return agent


@pytest.mark.asyncio
async def test_a_sandbox_of_two_nodes_reaches_its_peer_and_nothing_else() -> None:
    provider = SlurmProvider(slurm_config())
    request = await _request(provider)
    # The login node's own address on the cluster's network, which the bare nodes reach.
    login = next(
        (
            address[4][0]
            for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            if not address[4][0].startswith("127.")
        ),
        None,
    )
    if login is None:
        pytest.skip("the login node's name resolves to no address but loopback")
    async with await provider.create(request) as sandbox:
        nodes = sandbox.allocation.nodes
        assert len(set(nodes)) == 2 and sandbox.allocation.node == nodes[0]
        assert sandbox.allocation.gpu is None or len(sandbox.allocation.gpu.provider_addresses) == 2
        agent = _agent(sandbox)
        # The caller's exported variables go with the command, but not those of its node.
        code, out, err = await agent(
            "export WORLD=two; orbit-exec node1 'echo $WORLD $ORBIT_NODE_RANK $ORBIT_NODES'"
        )
        assert (code, out.split()) == (0, ["two", "1", "node0,node1"]), err

        code, out, err = await agent(
            "cd /tmp && orbit-exec node1 'hostname; echo $ORBIT_NODE_RANK; id -un; pwd'"
        )
        assert code == 0, err
        assert out.split() == ["node1", "1", "user", "/tmp"]
        assert (await agent("orbit-exec node1 exit 7"))[0] == 7
        assert (await agent("printf over | orbit-exec node1 cat"))[1] == "over"
        assert (await agent("orbit-exec node2 true"))[0] == 255
        # The probe on both nodes; each node's disk is its own.
        await sandbox.write_file("/tmp/probe.py", PROBE.encode(), identity=Identity.AGENT)
        assert (await agent("orbit-exec node1 'cat > /tmp/probe.py' < /tmp/probe.py"))[0] == 0
        # A listener on each node, which the other reaches on the group's network alone.
        code, _, err = await agent(
            "nohup python3 -m http.server 7001 >/dev/null 2>&1 & "
            "orbit-exec node1 'nohup python3 -m http.server 7000 >/dev/null 2>&1 &' && sleep 2"
        )
        assert code == 0, err
        outside = f"{login}:22 1.1.1.1:443 140.82.112.4:443"
        for where, peer in (("", "node1:7000"), ("orbit-exec node1 ", "node0:7001")):
            code, out, err = await agent(f"{where}python3 /tmp/probe.py {peer} {outside}")
            assert code == 0, err
            seen = dict(line.split(" ", 1) for line in out.splitlines())
            assert seen.pop(peer) == "reached", seen
            assert all(result.startswith("refused") for result in seen.values()), seen


@pytest.mark.asyncio
async def test_a_snapshot_of_two_nodes_keeps_each_and_boots_each_from_its_own() -> None:
    """Node 1's disk is in the snapshot and comes back on node 1; a Sandbox of one node
    booted from it is one node, whose `orbit-exec` fails at once, with none of the nodes'
    variables."""
    provider = SlurmProvider(slurm_config())
    request = await _request(provider, planned_saves=1)
    sandbox = await provider.create(request)
    try:
        agent = _agent(sandbox)
        code, _, err = await agent("orbit-exec node1 'echo node1-kept > $HOME/kept.txt'")
        assert code == 0, err
        retained = await sandbox.snapshot(roles=("solver",), reason="nodes")
    finally:
        await sandbox.destroy()
    try:
        booted = request.model_copy(update={"snapshot": retained.handle})
        async with await provider.create(booted) as group:
            agent = _agent(group)
            assert (await agent("orbit-exec node1 'cat $HOME/kept.txt'"))[1].strip() == (
                "node1-kept"
            )
            assert (await agent('test -e "$HOME/kept.txt"'))[0] != 0
        one = request.model_copy(
            update={
                "snapshot": retained.handle,
                "resources": request.resources.model_copy(update={"nodes": 1}),
            }
        )
        async with await provider.create(one) as single:
            assert single.allocation.nodes == ()
            agent = _agent(single)
            code, _, err = await agent("orbit-exec node1 true")
            assert code == 255 and "no daemon serves this node" in err, err
            assert (await agent('echo "${ORBIT_NODES-none} ${NCCL_NET-none}"'))[1].split() == [
                "none",
                "none",
            ]
    finally:
        await provider.destroy_retained(retained.handle)


@pytest.mark.asyncio
async def test_an_open_sandbox_of_two_nodes_reaches_out_and_still_its_peer() -> None:
    provider = SlurmProvider(slurm_config())
    request = await _request(provider, network=NetworkPolicy(mode=NetworkMode.OPEN))
    async with await provider.create(request) as sandbox:
        agent = _agent(sandbox)
        await sandbox.write_file("/tmp/probe.py", PROBE.encode(), identity=Identity.AGENT)
        assert (await agent("orbit-exec node1 'cat > /tmp/probe.py' < /tmp/probe.py"))[0] == 0
        code, _, err = await agent("nohup python3 -m http.server 7001 >/dev/null 2>&1 & sleep 2")
        assert code == 0, err
        code, out, err = await agent(
            "orbit-exec node1 python3 /tmp/probe.py node0:7001 1.1.1.1:443"
        )
        assert code == 0, err
        assert all(line.endswith("reached") for line in out.splitlines()), out


@pytest.mark.asyncio
async def test_root_that_routes_everything_to_the_other_nodes_reaches_only_them() -> None:
    """Root holds `NET_ADMIN` in a Sandbox that runs nested containers; the router, outside
    it, still forwards only the nodes' own traffic."""
    provider = SlurmProvider(slurm_config())
    part = _partition(provider)
    request = await _request(
        provider, resources=_resources(part, container_features=("nested_containers",))
    )
    login = next(
        address[4][0]
        for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        if not address[4][0].startswith("127.")
    )
    async with await provider.create(request) as sandbox:

        async def root(command: str) -> tuple[int, str, str]:
            result = await sandbox.exec(["sh", "-c", command])
            return result.exit_code, result.stdout, result.stderr

        await sandbox.write_file("/tmp/probe.py", PROBE.encode())
        await sandbox.write_file("/tmp/route.py", ROUTE.encode())
        code, out, err = await root(
            "orbit-exec node1 'nohup python3 -m http.server 7000 >/dev/null 2>&1 &' && "
            "python3 /tmp/route.py && sleep 2"
        )
        assert code == 0 and "default dev peers" in out, err
        code, out, err = await root(
            f"python3 /tmp/probe.py node1:7000 {login}:22 1.1.1.1:443 140.82.112.4:443"
        )
        assert code == 0, err
        seen = dict(line.split(" ", 1) for line in out.splitlines())
        assert seen.pop("node1:7000") == "reached", seen
        assert all(result.startswith("refused") for result in seen.values()), seen


#: Trains a small model with DDP over every rank `torchrun` starts on the two nodes; rank 0
#: writes what the verifier checks.
TRAIN = """\
import json, os, time
import torch
import torch.distributed as dist

local = int(os.environ["LOCAL_RANK"])
torch.cuda.set_device(local)
dist.init_process_group("nccl", device_id=torch.device("cuda", local))
torch.manual_seed(0)
layers = [torch.nn.Linear(1024, 4096), torch.nn.ReLU(), torch.nn.Linear(4096, 1)]
model = torch.nn.parallel.DistributedDataParallel(
    torch.nn.Sequential(*layers).cuda(), device_ids=[local]
)
optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
x = torch.randn(256, 1024, device="cuda")
y = x.sum(dim=1, keepdim=True) / 32
losses = []
for step in range(200):
    loss = torch.nn.functional.mse_loss(model(x), y)
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    losses.append(loss.item())
buffer = torch.ones(256 << 20, device="cuda")
for _ in range(3):
    dist.all_reduce(buffer)
torch.cuda.synchronize()
start = time.perf_counter()
for _ in range(10):
    dist.all_reduce(buffer)
torch.cuda.synchronize()
seconds = (time.perf_counter() - start) / 10
world = dist.get_world_size()
if dist.get_rank() == 0:
    os.makedirs("/workspace/out", exist_ok=True)
    with open("/workspace/out/metrics.json", "w") as file:
        json.dump({
            "world": world,
            "nodes": world // int(os.environ["LOCAL_WORLD_SIZE"]),
            "first": losses[0],
            "last": losses[-1],
            "busbw_GBps": 2 * (world - 1) / world * buffer.numel() * 4 / seconds / 1e9,
            "nccl_net": os.environ.get("NCCL_NET"),
        }, file)
dist.destroy_process_group()
"""
SOLVE = """\
#!/usr/bin/env bash
set -euo pipefail
cd /workspace
cp "$(dirname "$0")/train.py" .
# Each node's disk is its own: the program goes to node 1 too.
orbit-exec node1 'cat > /workspace/train.py' < train.py
gpus=$(nvidia-smi -L | wc -l)
launch="torchrun --nnodes=2 --nproc-per-node=$gpus --rdzv-backend=c10d \\
    --rdzv-endpoint=node0:29400 --rdzv-id=orbit train.py"
orbit-exec node1 "cd /workspace && $launch" > node1.log 2>&1 &
peer=$!
$launch
wait "$peer"
"""
VERIFY = """\
import json
from orbit_verify import write

metrics = json.load(open("/workspace/out/metrics.json"))
ok = metrics["nodes"] == 2 and metrics["last"] < metrics["first"] / 2
ok = ok and metrics["nccl_net"] == "IB"
write({"reward": 1.0 if ok else 0.0, **metrics})
"""
DOCKERFILE = """\
FROM docker.io/pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime
RUN apt-get update -qq \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \\
      libibverbs1 ibverbs-providers \\
 && rm -rf /var/lib/apt/lists/* \\
 && useradd -m -u 1000 -s /bin/bash user \\
 && mkdir -p /workspace/out && chown -R user:user /workspace
# An Orbit Task's image keeps running by itself; PyTorch's would start a shell that ends.
CMD ["sleep", "infinity"]
"""


@pytest.mark.asyncio
async def test_a_task_trains_on_two_nodes_and_a_separate_verifier_checks_its_work(
    tmp_path: Path,
) -> None:
    """`torchrun` on two nodes of every GPU each, over InfiniBand, which the nodes must have,
    started by the agent alone with the agent's network sealed, and checked by a separate
    verifier of one node that sees the agent's output."""
    provider = SlurmProvider(slurm_config())
    part = _partition(provider, gpus=True)
    root = scaffold_task(tmp_path / "train-two-nodes")
    (root / "environment" / "Dockerfile").write_text(DOCKERFILE)
    edit_task_config(
        root,
        {
            "artifacts": [{"source": "/workspace/out"}],
            "environment": {
                "cpus": 32,
                "memory_mb": 131072,
                "gpus": part.max_gpus,
                "gpu_types": [part.gpu_model.name],
                "nodes": 2,
            },
            "agent": {"timeout_sec": 1200},
            "verifier": {
                "environment_mode": "separate",
                "timeout_sec": 300,
                "environment": {"cpus": 2, "memory_mb": 4096},
            },
        },
    )
    (root / "solution" / "solve.sh").write_text(SOLVE)
    (root / "solution" / "train.py").write_text(TRAIN)
    (root / "tests" / "test.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nexec "$ORBIT_VERIFY_PYTHON" verify.py\n'
    )
    (root / "tests" / "verify.py").write_text(VERIFY)
    (root / "instruction.md").write_text("Train the model on both nodes.\n")
    task = load_tasks(root)[0]
    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    verification = json.loads((result.run_dir / "verification.json").read_text())
    assert result.verdict.rewards == {"reward": 1.0}, verification
    assert verification["world"] == 2 * part.max_gpus
    assert all(item.outcome == "destroyed" for item in result.record.sandboxes)
    print(json.dumps(verification))

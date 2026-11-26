"""Sandboxes of several nodes on Slurm, run on a login node of an explicitly configured
cluster with a partition whose `max_nodes` is at least two."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from orbit.core.sandbox import Identity, ImageRef, SandboxRequest, SandboxRole
from orbit.core.taskspec import NetworkMode, NetworkPolicy, Resources
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.providers.slurm import SlurmProvider
from orbit.run.providers.slurm.job import cache
from orbit.run.scaffold import scaffold_task
from orbit.run.task_images import prepare_task_image
from orbit.run.tasksets.manifest import load_tasks
from tests.integration.test_volume_artifacts import START_ENGINE
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
async def test_a_sandbox_of_two_nodes_reaches_its_peer_over_ssh_and_nothing_else() -> None:
    provider = SlurmProvider(slurm_config())
    request = await _request(provider, env={"TASK_SETTING": "kept"})
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
        # A session on the other node, as the agent, gets the container's variables, the
        # Task's among them, and that node's own.
        code, out, err = await agent(
            "ssh node1 'hostname; id -un; pwd; echo $TASK_SETTING $ORBIT_NODE_RANK $ORBIT_NODES'"
        )
        assert code == 0, err
        assert out.split() == ["node1", "user", sandbox.agent_home, "kept", "1", "node0,node1"]
        assert (await agent("ssh node1 exit 7"))[0] == 7
        assert (await agent("printf over | ssh node1 cat"))[1] == "over"
        assert (await agent("ssh node2 true"))[0] == 255
        # Root has its own key, and the agent none of root's.
        root = await sandbox.exec(["sh", "-c", "ssh node1 id -un"])
        assert root.ok and root.stdout.strip() == "root", root.stderr
        assert (await agent("ssh root@node1 true"))[0] == 255
        # /shared is one directory both nodes see, the agent's, root's files too; every other
        # path is a node's. Node 0 lists a directory before node 1 adds a module to it.
        code, out, err = await agent(
            "touch /tmp/only0 && mkdir /shared/pkg && ls /shared/pkg && echo zero > /shared/from0"
            " && ssh node1 'cat /shared/from0 && echo one > /shared/from1 && echo x=1 >"
            " /shared/pkg/m.py && test ! -e /tmp/only0' && cat /shared/from1 && cd /shared &&"
            " python3 -c 'from pkg import m; print(m.x)' && stat -c %U /shared/from0 /shared/from1"
        )
        assert code == 0 and out.split() == ["zero", "one", "1", "user", "user"], (out, err)
        made = await sandbox.exec(["sh", "-c", "touch /shared/byroot && stat -c %U /shared/byroot"])
        assert made.ok and made.stdout.strip() == "user", made.stderr
        await sandbox.write_file("/tmp/probe.py", PROBE.encode(), identity=Identity.AGENT)
        assert (await agent("scp -q /tmp/probe.py node1:/tmp/probe.py"))[0] == 0
        # A listener on each node, which the other reaches on the group's network alone.
        code, _, err = await agent(
            "nohup python3 -m http.server 7001 >/dev/null 2>&1 & "
            "ssh node1 'nohup python3 -m http.server 7000 >/dev/null 2>&1 &' && sleep 2"
        )
        assert code == 0, err
        outside = f"{login}:22 1.1.1.1:443 140.82.112.4:443"
        for where, peer in (("", "node1:7000"), ("ssh node1 ", "node0:7001")):
            code, out, err = await agent(f"{where}python3 /tmp/probe.py {peer} {outside}")
            assert code == 0, err
            seen = dict(line.split(" ", 1) for line in out.splitlines())
            assert seen.pop(peer) == "reached", seen
            assert all(result.startswith("refused") for result in seen.values()), seen


@pytest.mark.asyncio
async def test_a_snapshot_of_two_nodes_keeps_each_node_and_the_shared_directory() -> None:
    """Node 1's disk is in the snapshot and comes back on node 1, and the shared directory
    with them: a copy of its own for a Sandbox that may change it, the snapshot's own,
    read-only, for a verifier. A Sandbox of one node booted from it is one node, which no ssh
    reaches beyond, with none of the nodes' variables; and once nothing keeps them, the
    shared directories are gone."""
    provider = SlurmProvider(slurm_config())
    request = await _request(provider, planned_saves=1)
    shared = cache(provider.config) / "shared"
    before = set(shared.iterdir()) if shared.exists() else set()
    sandbox = await provider.create(request)
    try:
        agent = _agent(sandbox)
        code, _, err = await agent(
            "ssh node1 'echo node1-kept > $HOME/kept.txt' && echo shared-kept > /shared/kept"
        )
        assert code == 0, err
        retained = await sandbox.snapshot(roles=("solver",), reason="nodes")
    finally:
        await sandbox.destroy()
    # The Sandbox's own, which its snapshot keeps.
    assert len(set(shared.iterdir()) - before) == 1
    try:
        booted = request.model_copy(update={"snapshot": retained.handle})
        async with await provider.create(booted) as group:
            agent = _agent(group)
            assert (await agent("ssh node1 'cat $HOME/kept.txt'"))[1].strip() == "node1-kept"
            assert (await agent('test -e "$HOME/kept.txt"'))[0] != 0
            code, out, err = await agent("cat /shared/kept && echo changed > /shared/kept")
            assert (code, out.strip()) == (0, "shared-kept"), err
        verifier = request.model_copy(
            update={
                "snapshot": retained.handle,
                "role": SandboxRole.VERIFIER,
                "resources": request.resources.model_copy(update={"nodes": 1}),
            }
        )
        async with await provider.create(verifier) as checking:
            # The snapshot's own, as it was, which the solver's copy changed nothing of.
            seen = await checking.exec(["sh", "-c", "cat /shared/kept; touch /shared/new"])
            assert seen.stdout.strip() == "shared-kept", seen.stderr
            assert not seen.ok and "Read-only" in seen.stderr, seen.stderr
        one = request.model_copy(
            update={
                "snapshot": retained.handle,
                "resources": request.resources.model_copy(update={"nodes": 1}),
            }
        )
        async with await provider.create(one) as single:
            assert single.allocation.nodes == ()
            agent = _agent(single)
            assert (await agent("cat /shared/kept"))[1].strip() == "shared-kept"
            code, _, _ = await agent("timeout 20 ssh -o ConnectTimeout=5 node1 true")
            assert code not in (0, 124)
            assert (await agent('echo "${ORBIT_NODES-none} ${NCCL_NET-none}"'))[1].split() == [
                "none",
                "none",
            ]
    finally:
        await provider.destroy_retained(retained.handle)
    assert set(shared.iterdir()) <= before


@pytest.mark.asyncio
async def test_an_open_sandbox_of_two_nodes_reaches_out_and_still_its_peer() -> None:
    provider = SlurmProvider(slurm_config())
    request = await _request(provider, network=NetworkPolicy(mode=NetworkMode.OPEN))
    async with await provider.create(request) as sandbox:
        agent = _agent(sandbox)
        await sandbox.write_file("/shared/probe.py", PROBE.encode(), identity=Identity.AGENT)
        code, _, err = await agent("nohup python3 -m http.server 7001 >/dev/null 2>&1 & sleep 2")
        assert code == 0, err
        code, out, err = await agent("ssh node1 python3 /shared/probe.py node0:7001 1.1.1.1:443")
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
            "ssh node1 'nohup python3 -m http.server 7000 >/dev/null 2>&1 &' && "
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


#: Docker on every node, each building an image of its own; containers that reach the other
#: node's Sandbox and a container it publishes.
ENGINES = """FROM {base}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \\
    && rm -rf /var/lib/apt/lists/*
"""
BUILD = (
    START_ENGINE + "; docker pull -q busybox:1.36 >/dev/null && "
    "docker run --name built -e MADE_ON=$(hostname) busybox:1.36 "
    "sh -c 'echo $MADE_ON > /made-on; rm /bin/ls' && "
    "docker commit built kept:1 >/dev/null"
)


@pytest.mark.asyncio
async def test_two_nodes_run_containers_that_reach_each_other_and_keep_them_in_a_snapshot(
    tmp_path: Path,
) -> None:
    provider = SlurmProvider(slurm_config())
    part = _partition(provider)
    root = scaffold_task(tmp_path / "engines")
    (root / "environment" / "Dockerfile").write_text(ENGINES.format(base=CONTAINER_BASE))
    # A Task of several nodes, whose image carries the nodes' OpenSSH.
    edit_task_config(root, {"environment": {"nodes": 2}})
    prepared = await prepare_task_image(load_tasks(root)[0], provider)
    request = SandboxRequest(
        episode_id="slurm-nodes-engines",
        prepared_image=prepared,
        planned_saves=1,
        resources=_resources(part, gpus=0, gpu_types=(), container_features=("nested_containers",)),
        network=NetworkPolicy(mode=NetworkMode.OPEN),
    )

    async def root_on(box, command: str) -> str:  # type: ignore[no-untyped-def]
        result = await box.exec(["sh", "-c", command], timeout_sec=900)
        assert result.ok, result.stderr[-2000:]
        return result.stdout

    sandbox = await provider.create(request)
    try:
        await sandbox.write_file("/shared/build.sh", BUILD.encode())
        await root_on(sandbox, "sh /shared/build.sh && ssh node1 sh /shared/build.sh")
        published = await root_on(
            sandbox,
            "ssh node1 'docker run -d -p 10.214.0.2:7100:80 busybox:1.36 sh -c "
            '"echo inner-node1 > /index.html; httpd -f -p 80 -h /"\' >/dev/null && sleep 3 && '
            "docker run --rm busybox:1.36 wget -q -T 10 -O - http://10.214.0.2:7100/",
        )
        assert published.strip() == "inner-node1"
        retained = await sandbox.snapshot(roles=("solver",), reason="engines")
    finally:
        await sandbox.destroy()
    try:
        booted = request.model_copy(update={"snapshot": retained.handle})
        async with await provider.create(booted) as group:
            check = (
                START_ENGINE + "; docker run --rm kept:1 sh -c 'cat /made-on; "
                "test ! -e /bin/ls && echo gone'"
            )
            await group.write_file("/shared/check.sh", check.encode())
            assert (await root_on(group, "sh /shared/check.sh")).split() == ["node0", "gone"]
            assert (await root_on(group, "ssh node1 sh /shared/check.sh")).split() == [
                "node1",
                "gone",
            ]
    finally:
        await provider.destroy_retained(retained.handle)


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
# One copy of the program, in the directory every node sees.
cp "$(dirname "$0")/train.py" /shared/train.py
gpus=$(nvidia-smi -L | wc -l)
launch="torchrun --nnodes=2 --nproc-per-node=$gpus --rdzv-backend=c10d \\
    --rdzv-endpoint=node0:29400 --rdzv-id=orbit /shared/train.py"
ssh node1 "cd /workspace && $launch" > /workspace/node1.log 2>&1 &
peer=$!
cd /workspace
$launch
wait "$peer"
"""
VERIFY = """\
import json, os
from orbit_verify import write

metrics = json.load(open("/workspace/out/metrics.json"))
ok = metrics["nodes"] == 2 and metrics["last"] < metrics["first"] / 2
ok = ok and metrics["nccl_net"] == "IB"
# The agent's shared directory, as it left it, which the verifier only reads.
try:
    open("/shared/verifier-was-here", "w")
    shared = "writable"
except OSError:
    shared = "read-only" if os.path.exists("/shared/train.py") else "missing"
write({"reward": 1.0 if ok and shared == "read-only" else 0.0, "shared": shared, **metrics})
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

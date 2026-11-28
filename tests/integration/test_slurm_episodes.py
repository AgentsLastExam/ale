"""Slurm episodes, run as Orbit runs each, in a batch job of an explicitly configured cluster,
on its first node (``tests.support.provider_in_job``); no Docker is needed there."""

from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath

import pytest

from orbit.core.sandbox import Identity, ImageRef, SandboxRequest
from orbit.core.taskspec import NetworkMode, NetworkPolicy, Resources
from orbit.run.episode import run_episode
from orbit.run.gateway.server import Gateway
from orbit.run.harnesses.base import CliCommand
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.providers.cluster import provider as slurm_provider
from orbit.run.providers.cluster.job import Allocation
from orbit.run.secrets import resolve_task_variables
from orbit.run.tasksets.manifest import load_tasks
from tests.integration.scripted_model import ScriptedModel
from tests.integration.test_validation_rounds import _check as check_rounds
from tests.integration.test_validation_rounds import _episode as rounds_episode
from tests.integration.test_validation_rounds import _task as rounds_task
from tests.integration.test_volume_artifacts import _check as check_volume
from tests.integration.test_volume_artifacts import _episode as volume_episode
from tests.support import CONTAINER_BASE, ScriptedCliHarness, provider_in_job, provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_slurm]

#: Asks the model once through the Gateway at ``argv[1]``, as an agent's CLI does, a question
#: of its own each time, which the Gateway does not answer again from an earlier call.
ASK = """
import json, os, sys, urllib.request
body = {"model": sys.argv[2], "messages": [{"role": "user", "content": os.urandom(8).hex()}]}
request = urllib.request.Request(
    sys.argv[1] + "/v1/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Authorization": "Bearer " + os.environ["GATEWAY_TOKEN"]},
)
print(json.load(urllib.request.urlopen(request, timeout=60))["choices"][0]["message"]["content"])
"""


class GatewayCliHarness(ScriptedCliHarness):
    """The scripted CLI, which first asks the model through the Gateway each turn, as a real
    CLI reaches it: on Slurm, by its Sandbox's tunnel."""

    async def prepare_command(  # type: ignore[no-untyped-def]
        self, prompt_path, sandbox, session, *, native_session_id
    ):
        scripted = await super().prepare_command(
            prompt_path, sandbox, session, native_session_id=native_session_id
        )
        ask = shlex.join(["python3", "-I", "-c", ASK, session.gateway_url, session.model])
        return CliCommand(
            f"{ask} > /dev/null\n{scripted.script}",
            {"GATEWAY_TOKEN": session.token},
            scripted.session_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "snapshot", "separate"])
async def test_an_agent_improves_over_validation_rounds_in_one_slurm_job(
    tmp_path: Path, mode: str
) -> None:
    """Every turn's solver and every verifier, booted from a round's snapshot or of an image
    of its own, runs in the job, from its nodes' own stores."""
    provider = provider_in_job()
    try:
        root = rounds_task(tmp_path / f"rounds-slurm-{mode}", mode)
        result = await rounds_episode(tmp_path, root, provider)
        check_rounds(result)
        assert provider.snapshots == {}
        kept = await provider.list_retained()
        assert not [record for record in kept if record["episode"] == result.episode_id]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "validation"), [("separate", None), ("snapshot", None), ("shared", "separate")]
)
async def test_an_agent_works_in_a_volume_in_one_slurm_job(
    tmp_path: Path, mode: str, validation: str | None
) -> None:
    """Every turn's solver and every verifier, a separate one of the verifier's image
    included, runs in the job, viewing its volume in the node's store."""
    provider = provider_in_job()
    try:
        result = await volume_episode(tmp_path, mode, validation, provider)
        check_volume(result, mode, validation)
        assert provider.snapshots == {}
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_every_turn_reaches_the_gateway_through_its_tunnel_over_slurm_rounds(
    tmp_path: Path,
) -> None:
    """Each turn's solver reaches the Gateway through its node's tunnel, and the save of each
    solver whose tunnel is open succeeds."""
    model = ScriptedModel()
    provider = provider_in_job()
    gateway = Gateway(
        api_key="host-only",
        upstream=await model.start(),
        dialect="openai-chat-completions",
        host="127.0.0.1",
    )
    try:
        gateway_url = await gateway.start()
        task = load_tasks(rounds_task(tmp_path / "rounds-slurm-gateway", "snapshot"))[0]
        resolve_task_variables([task], oracle=False, protected={})
        result = await run_episode(
            task,
            EpisodeProtocol(GatewayCliHarness(), validation_rounds=task.spec.validation_rounds),
            provider_registry(provider),
            run_dir=tmp_path / "runs",
            gateway=gateway,
            gateway_url=gateway_url,
            model="scripted-model",
        )
    finally:
        await gateway.stop()
        await model.stop()
        await provider.close()
    check_rounds(result)
    assert len(model.requests) == 3


@pytest.mark.asyncio
async def test_a_save_outlasts_a_guest_session_whose_srun_was_killed() -> None:
    """A guest session whose process on Orbit's side was killed leaves Podman a session it
    never saw end, which fails the save's first ``podman stop``: the save still keeps the
    Sandbox's disk (the Provider's AGENTS.md)."""
    provider = provider_in_job()
    request = SandboxRequest(
        episode_id="slurm-lost-session",
        prepared_image=await provider.prepare_image(
            ImageRef(kind="container", reference=CONTAINER_BASE)
        ),
        resources=Resources(cpus=2, memory_mb=2048),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
        retention="snapshot",
    )
    sandbox = await provider.create(request)
    kept = str(PurePosixPath(sandbox.agent_home) / "kept.txt")
    try:
        await sandbox.write_file(kept, b"kept", identity=Identity.AGENT)
        lost = sandbox._file_client._transport._proc  # type: ignore[attr-defined]
        lost.kill()
        await lost.wait()
        retained = await sandbox.snapshot(roles=("solver",), reason="test")
    finally:
        await sandbox.destroy()
    try:
        booted = await provider.create(request.model_copy(update={"snapshot": retained.handle}))
        async with booted:
            assert await booted.read_file(kept) == b"kept"
    finally:
        await provider.destroy_retained(retained.handle)
        await provider.close()


@pytest.mark.asyncio
async def test_a_snapshot_slurm_flattens_keeps_the_agents_work_and_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit or a volume's stack that would pass the layers measured is flattened to one:
    here every save is, and each turn and the snapshot verifier go on from what the agent
    left on its disk and in its volume."""
    monkeypatch.setattr(slurm_provider, "MAX_LAYERS", 1)
    saved: list[dict[str, object]] = []
    save = Allocation.save

    async def recorded(job: Allocation, index: str, *, nodes: int, timeout: int = 1800):  # type: ignore[no-untyped-def]
        done = await save(job, index, nodes=nodes, timeout=timeout)
        for rank in range(nodes):
            record = job.directory / "sandboxes" / index / "nodes" / str(rank) / "saved"
            saved.append(json.loads(record.read_text()))
        return done

    monkeypatch.setattr(Allocation, "save", recorded)
    provider = provider_in_job()
    try:
        result = await volume_episode(tmp_path, "snapshot", None, provider)
    finally:
        await provider.close()
    check_volume(result, "snapshot", None)
    # Each round's save and the final one, each one layer; a volume viewed a stack below the
    # first is a merged layer of its own.
    assert [record["layers"] for record in saved] == [1, 1, 1]
    stacks = [stack for record in saved[1:] for stack in record["stacks"].values()]  # type: ignore[union-attr]
    assert stacks and all(
        len(stack["layers"]) == 1 and stack["layers"][0].endswith(".f") for stack in stacks
    )

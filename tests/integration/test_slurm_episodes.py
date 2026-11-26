"""Slurm episodes, run on a login node of an explicitly configured cluster, which shares
Orbit's cache with its compute nodes; no Docker is needed there."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from orbit.run.episode import run_episode
from orbit.run.gateway.server import Gateway
from orbit.run.harnesses.base import CliCommand
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.providers.slurm import SlurmProvider
from orbit.run.providers.slurm.job import Job
from orbit.run.secrets import resolve_task_variables
from orbit.run.tasksets.manifest import load_tasks
from tests.integration.scripted_model import ScriptedModel
from tests.integration.test_validation_rounds import _check as check_rounds
from tests.integration.test_validation_rounds import _episode as rounds_episode
from tests.integration.test_validation_rounds import _task as rounds_task
from tests.integration.test_volume_artifacts import _check as check_volume
from tests.integration.test_volume_artifacts import _episode as volume_episode
from tests.support import ScriptedCliHarness, provider_registry, slurm_config

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
@pytest.mark.parametrize(("mode", "jobs"), [("shared", 1), ("snapshot", 1), ("separate", 4)])
async def test_an_agent_improves_over_validation_rounds_in_one_slurm_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, jobs: int
) -> None:
    """Every turn's solver and every verifier booted from a round's snapshot run in the
    first solver's allocation, from its node's own store: only a separate verifier, from
    an image of its own, gets another."""
    provider = SlurmProvider(slurm_config())
    submitted: list[str] = []
    submit = Job.submit

    async def counted(job: Job, **resources: int | str) -> None:
        # Allocations, not the jobs that prepare images.
        if resources.get("command", "supervise") == "supervise":
            submitted.append(job.name)
        await submit(job, **resources)  # type: ignore[arg-type]

    monkeypatch.setattr(Job, "submit", counted)
    root = rounds_task(tmp_path / f"rounds-slurm-{mode}", mode)
    result = await rounds_episode(tmp_path, root, provider)
    check_rounds(result)
    assert len(submitted) == jobs
    assert provider.snapshots == {}
    kept = await provider.list_retained()
    assert not [record for record in kept if record["episode"] == result.episode_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "validation"), [("separate", None), ("snapshot", None), ("shared", "separate")]
)
async def test_an_agent_works_in_a_volume_in_one_slurm_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, validation: str | None
) -> None:
    """Every turn's solver and every verifier, a separate one of the verifier's image
    included, runs in the first solver's allocation, viewing its volume in the node's
    store."""
    provider = SlurmProvider(slurm_config())
    submitted: list[str] = []
    submit = Job.submit

    async def counted(job: Job, **resources: int | str) -> None:
        # Allocations, not the jobs that prepare images.
        if resources.get("command", "supervise") == "supervise":
            submitted.append(job.name)
        await submit(job, **resources)  # type: ignore[arg-type]

    monkeypatch.setattr(Job, "submit", counted)
    result = await volume_episode(tmp_path, mode, validation, provider)
    check_volume(result, mode, validation)
    assert len(submitted) == 1
    assert provider.snapshots == {}


@pytest.mark.asyncio
async def test_every_turn_reaches_the_gateway_through_its_tunnel_over_slurm_rounds(
    tmp_path: Path,
) -> None:
    """Each turn's solver reaches the Gateway through its node's tunnel, and the save of each
    solver whose tunnel is open succeeds."""
    model = ScriptedModel()
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
            provider_registry(SlurmProvider(slurm_config())),
            run_dir=tmp_path / "runs",
            gateway=gateway,
            gateway_url=gateway_url,
            model="scripted-model",
        )
    finally:
        await gateway.stop()
        await model.stop()
    check_rounds(result)
    assert len(model.requests) == 3

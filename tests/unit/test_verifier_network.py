"""The verifier's own network: ``tests/`` and ``validation/`` run with it in every topology,
and an egress session of the episode's own reaches the hosts of the phase that runs."""

from __future__ import annotations

from typing import Any

import pytest
from tests.support import (
    MemorySandbox,
    RoundsProvider,
    TurnsHarness,
    Verifiers,
    edit_task_config,
    provider_registry,
    rounds_task,
)
from tests.unit.test_runtime_contracts import make_lock
from tests.unit.test_validation_rounds import IMAGE

from orbit.core.config import SandboxRetentionConfig
from orbit.core.harness import AgentRun
from orbit.core.sandbox import ExecResult, SandboxRequest, SandboxRole
from orbit.core.taskspec import NetworkMode, NetworkPolicy, VerificationMode
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.gateway.session import SessionRegistry
from orbit.run.protocol.episode_protocol import (
    EpisodeProtocol,
    KeptSolver,
    ReverificationProtocol,
)
from orbit.run.provenance import ProvenanceInputs
from orbit.run.tasksets.manifest import ManifestTask, load_tasks

pytestmark = pytest.mark.unit

PROXY = "http://127.0.0.1:9"
EXAMPLE = {"network_mode": "allowlist", "allowed_hosts": ["example.com"]}
PYPI = {"network_mode": "allowlist", "allowed_hosts": ["pypi.org"]}
NO_GITHUB = {"network_mode": "blocklist", "blocked_hosts": ["github.com"]}
EXAMPLE_POLICY = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",))
PYPI_POLICY = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org",))
NO_GITHUB_POLICY = NetworkPolicy(mode=NetworkMode.BLOCKLIST, blocked_hosts=("github.com",))
#: Where the episodes open their egress sessions, as the proxy's own registry.
REGISTRY = SessionRegistry()

#: What one phase saw: where it ran, whether the Sandbox was sealed, and the policy the
#: egress session held then, or None while it was suspended or absent.
Seen = tuple[str, bool, NetworkPolicy | None]


def _task(
    tmp_path: Any, mode: str, agent: dict[str, Any], verifier: dict[str, Any]
) -> ManifestTask:
    root = rounds_task(tmp_path, rounds=1, mode=mode).folder.root  # type: ignore[union-attr]
    edit_task_config(root, {"agent": agent, "verifier": verifier})
    task = load_tasks(root)[0]
    task.prepared_image = IMAGE
    return task


def _watch(
    monkeypatch: pytest.MonkeyPatch, registry: SessionRegistry, seen: list[Seen]
) -> type[TurnsHarness]:
    """Record what ``tests/``, ``validation/`` and each turn of the agent saw."""

    def state(name: str, sandbox: Any) -> Seen:
        session = registry.egress(sandbox.request.proxy_token)
        policy = None if session is None else session.policy
        return name, not sandbox.egress_open, policy

    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if list(argv) == ["bash", "test.sh"]:
            seen.append(state(str(options.get("cwd")).rpartition("/")[2], self))
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)

    class Watching(TurnsHarness):
        async def _turn(self, prompt: str, sandbox: Any, session: Any, timeout: float) -> AgentRun:
            seen.append(state("agent", sandbox))
            return await super()._turn(prompt, sandbox, session, timeout)

    return Watching


async def _run(tmp_path: Any, provider: RoundsProvider, task: ManifestTask, harness: Any) -> Any:
    lock = make_lock()
    return await run_episode(
        task,
        EpisodeProtocol(harness, validation_rounds=1),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(),
        proxy_url=PROXY,
        session_registry=REGISTRY,
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
            validation_rounds=1,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate", "snapshot"])
async def test_every_phase_has_its_own_allowlist_through_one_egress_session(
    tmp_path: Any, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    seen: list[Seen] = []
    harness = _watch(monkeypatch, REGISTRY, seen)()
    provider = RoundsProvider()
    task = _task(tmp_path, mode, EXAMPLE, PYPI)
    result = await _run(tmp_path, provider, task, harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure

    # The agent's turns reach its hosts, and validation/ and tests/ the verifier's, sealed.
    example, pypi = EXAMPLE_POLICY, PYPI_POLICY
    assert seen == [
        ("agent", True, example),
        ("validation", True, pypi),
        ("agent", True, example),
        ("tests", True, pypi),
    ]
    solvers = [item for item in provider.requests if item.role is not SandboxRole.VERIFIER]
    verifiers_ = [item for item in provider.requests if item.role is SandboxRole.VERIFIER]
    both = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com", "pypi.org"))
    # A shared solver is created able to apply both; a verifier of its own has its own.
    assert {item.network for item in solvers} == {both if mode == "shared" else example}
    assert {item.network for item in verifiers_} == {pypi}
    tokens = {item.proxy_token for item in provider.requests}
    assert {item.proxy_url for item in provider.requests} == {PROXY}
    # One egress session for every Sandbox of the episode, never the agent's model token,
    # revoked with the episode.
    (token,) = tokens
    assert token and REGISTRY.egress(token) is None and REGISTRY.known(token) is None
    assert {item.verifier_network_mode for item in solvers} == {NetworkMode.ALLOWLIST}
    # Selection asked for the same networks, a verifier booted from the solver's snapshot's
    # included.
    asked = [(needs, needs.verifier) for _, needs in provider.asked]
    assert {each.network_mode for pair in asked for each in pair if each} == {NetworkMode.ALLOWLIST}
    # The RunLock records each phase's network.
    assert result.lock is not None
    assert result.lock.sandbox is not None and result.lock.sandbox.network == example
    assert result.lock.verification.network == pypi


@pytest.mark.asyncio
async def test_a_blocklist_and_an_allowlist_share_one_solver_and_one_egress_session(
    tmp_path: Any, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocklist is proxied as an allowlist is, so a shared solver created with the agent's
    blocklist applies the verifier's allowlist, each phase's policy held by one egress
    session in turn."""
    seen: list[Seen] = []
    harness = _watch(monkeypatch, REGISTRY, seen)()
    provider = RoundsProvider()
    result = await _run(tmp_path, provider, _task(tmp_path, "shared", NO_GITHUB, PYPI), harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert seen == [
        ("agent", True, NO_GITHUB_POLICY),
        ("validation", True, PYPI_POLICY),
        ("agent", True, NO_GITHUB_POLICY),
        ("tests", True, PYPI_POLICY),
    ]
    solvers = [item for item in provider.requests if item.role is not SandboxRole.VERIFIER]
    assert {item.network for item in solvers} == {NO_GITHUB_POLICY}
    assert {item.proxy_url for item in provider.requests} == {PROXY}
    assert {needs.network_mode for _, needs in provider.asked} == {NetworkMode.BLOCKLIST}
    assert {item.verifier_network_mode for item in solvers} == {NetworkMode.ALLOWLIST}
    applied = [
        (record["policy"], record["requested_mode"])
        for record in _trace(result)
        if record["kind"] == "policy_applied"
    ]
    assert applied == [("sealed", "blocklist"), ("sealed", "allowlist")] * 2
    assert result.lock is not None and result.lock.sandbox is not None
    assert result.lock.sandbox.network == NO_GITHUB_POLICY
    assert result.lock.verification.network == PYPI_POLICY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "verifier", "solver", "expected"),
    [
        # Orbit's defaults: the agent sealed, verification open.
        ({}, {}, NetworkMode.BLOCK, [True, False, True, False]),
        # A public agent and a sealed verifier: the shared solver is created sealable.
        (
            {"network_mode": "public"},
            {"network_mode": "no-network"},
            NetworkMode.BLOCK,
            [False, True, False, True],
        ),
        ({"network_mode": "public"}, {}, NetworkMode.OPEN, [False, False, False, False]),
    ],
)
async def test_a_shared_solver_applies_each_phase_s_network(
    tmp_path: Any,
    verifiers: Verifiers,
    monkeypatch: pytest.MonkeyPatch,
    agent: dict[str, Any],
    verifier: dict[str, Any],
    solver: NetworkMode,
    expected: list[bool],
) -> None:
    seen: list[Seen] = []
    harness = _watch(monkeypatch, REGISTRY, seen)()
    provider = RoundsProvider()
    result = await _run(tmp_path, provider, _task(tmp_path, "shared", agent, verifier), harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert [sealed for _, sealed, _ in seen] == expected
    assert provider.requests[0].network.mode is solver
    # Selection asked for a solver that can apply both, and round verifiers for the
    # verifier's own.
    verifier_mode = (
        NetworkMode.BLOCK if verifier.get("network_mode") == "no-network" else NetworkMode.OPEN
    )
    assert {(needs.network_mode, needs.verifier.network_mode) for _, needs in provider.asked} == {
        (solver, verifier_mode)
    }
    # No phase has an allowlist, so no Sandbox is given the proxy or an egress token.
    assert {(item.proxy_url, item.proxy_token) for item in provider.requests} == {("", "")}
    applied = [
        (record["phase"], record["policy"])
        for record in _trace(result)
        if record["kind"] == "policy_applied"
    ]
    assert [phase for phase, _ in applied] == ["agent", "validate", "agent", "verify"]
    assert [policy == "sealed" for _, policy in applied] == expected


@pytest.mark.parametrize(
    ("agent", "verifier", "solver"),
    [
        ({"network_mode": "public"}, {"network_mode": "no-network"}, NetworkMode.BLOCK),
        ({"network_mode": "no-network"}, EXAMPLE, NetworkMode.ALLOWLIST),
        ({"network_mode": "public"}, NO_GITHUB, NetworkMode.BLOCKLIST),
    ],
)
def test_a_solver_that_runs_validation_can_apply_the_verifiers_network(
    tmp_path: Any, agent: dict[str, Any], verifier: dict[str, Any], solver: NetworkMode
) -> None:
    """``tests/`` has a Sandbox of its own, but ``validation/`` runs in the solver, so the
    solver is created able to apply the verifier's network as well as the agent's."""
    root = rounds_task(
        tmp_path, rounds=1, mode="separate", validation={"environment_mode": "shared"}
    ).folder.root  # type: ignore[union-attr]
    edit_task_config(root, {"agent": agent, "verifier": verifier})
    spec = load_tasks(root)[0].spec
    assert spec.verify.environment_mode is VerificationMode.SEPARATE
    assert spec.validation_spec.environment_mode is VerificationMode.SHARED
    assert spec.solver_network.mode is solver


def _trace(result: Any) -> list[dict[str, Any]]:
    import json

    lines = (result.run_dir / "trace.execution.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.asyncio
async def test_reverification_reconnects_a_kept_solver_with_its_own_egress_session(
    tmp_path: Any, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared verifier of a kept solver: the Sandbox is reconnected for the reverifying
    episode, with that episode's egress session, so the relay its seal starts reaches the
    verifier's hosts."""
    seen: list[Seen] = []
    _watch(monkeypatch, REGISTRY, seen)
    provider = RoundsProvider()
    task = _task(tmp_path, "shared", {}, PYPI)
    kept = SandboxRequest(
        episode_id="source",
        prepared_image=IMAGE,
        retention="keep",
        role=SandboxRole.SHARED,
        resources=task.spec.resources,
        network=task.spec.solver_network,
    )
    attached: list[SandboxRequest] = []

    async def attach(request: SandboxRequest) -> MemorySandbox:
        attached.append(request)
        sandbox = MemorySandbox(provider, request)
        sandbox.files["/opt/orbit/agent-episode"] = b"source"
        return sandbox

    result = await run_episode(
        task,
        ReverificationProtocol(KeptSolver(kept, attach), tmp_path / "source", None),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
        proxy_url=PROXY,
        session_registry=REGISTRY,
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    (request,) = attached
    assert request.network == PYPI_POLICY
    assert request.proxy_url == PROXY and request.proxy_token
    assert seen == [("tests", True, PYPI_POLICY)]

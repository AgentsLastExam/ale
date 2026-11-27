"""What a Run gives its agent: variables of its own."""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from tests.support import (
    TURNS_LOG,
    MemorySandbox,
    RoundsProvider,
    TurnsHarness,
    Verifiers,
    provider_registry,
    rounds_task,
    write_task,
)
from tests.unit.test_episode_retries import write_result
from tests.unit.test_runtime_contracts import DIGEST, make_lock

from orbit.core.config import RunConfig, load_run_config
from orbit.core.errors import ConfigError
from orbit.core.harness import AgentRun, HarnessSession
from orbit.core.lock import AgentVariableProvenance, RunLock
from orbit.core.sandbox import ExecResult, PreparedTaskImage
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.harnesses.builtin import NopHarness, OracleHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.provenance import ProvenanceInputs, agent_provenance
from orbit.run.providers import ProviderRegistry
from orbit.run.scaffold import scaffold_task
from orbit.run.secrets import resolve_task_variables
from orbit.run.tasksets.manifest import ManifestTask, load_task_folder

pytestmark = pytest.mark.unit

IMAGE = PreparedTaskImage(
    kind="container",
    source="external-ref",
    input_identity=DIGEST,
    runtime_ref="image@" + DIGEST,
    prepared_identity=DIGEST,
    resolved_reference="image@" + DIGEST,
)
SECRET = "wandb-0123456789abcdef"
PREFIX = "You are part of an ablation.\n"
SUFFIX = "Log every run to Weights & Biases.\n"


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """The checkout's ``.env``, written by the test."""
    path = tmp_path / "checkout.env"
    path.write_text("")
    monkeypatch.setenv("ORBIT_ENV_FILE", str(path))
    return path.write_text


def _task(tmp_path: Path, **options: Any) -> ManifestTask:
    task = rounds_task(tmp_path, **options)
    task.prepared_image = IMAGE
    return task


async def _run(
    tmp_path: Path, task: ManifestTask, harness: Any, *, rounds: int = 1, **protocol: Any
) -> Any:
    lock = make_lock()
    return await run_episode(
        task,
        EpisodeProtocol(harness, validation_rounds=rounds, **protocol),
        provider_registry(RoundsProvider()),
        run_dir=tmp_path / "runs",
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
            validation_rounds=rounds,
        ),
    )


# --- the variables ---


def test_agent_env_is_part_of_the_run_configuration(tmp_path: Path) -> None:
    path = tmp_path / "run.toml"
    path.write_text('[agent.env]\nWANDB_API_KEY = "${WANDB_API_KEY}"\nWANDB_MODE = "online"\n')
    config = load_run_config(run_path=path)
    assert config.agent.env == {"WANDB_API_KEY": "${WANDB_API_KEY}", "WANDB_MODE": "online"}
    hashes = {
        RunConfig().config_hash,
        config.config_hash,
        load_run_config(run_path=path, overrides=["agent.env.WANDB_MODE=offline"]).config_hash,
    }
    assert len(hashes) == 3


def test_the_lock_records_what_the_run_gave_its_agent_never_a_value() -> None:
    settings = RunConfig.model_validate({"agent": {"env": {"WANDB_API_KEY": "${WANDB_API_KEY}"}}})
    sources = (AgentVariableProvenance(name="WANDB_API_KEY", source="env_file"),)
    given = agent_provenance(NopHarness(), "model", settings, agent_env=sources)
    assert given.env == sources
    lock = make_lock().model_copy(update={"agent": given})
    recorded = json.loads(RunLock.model_validate_json(lock.model_dump_json()).model_dump_json())
    assert recorded["agent"]["env"] == [{"name": "WANDB_API_KEY", "source": "env_file"}]
    # An oracle receives none, and a lock without them names none.
    oracle = agent_provenance(OracleHarness(), "", settings)
    assert oracle.env == ()
    dumped = json.loads(make_lock().model_copy(update={"agent": oracle}).model_dump_json())
    assert "env" not in dumped["agent"]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"WANDB_API_KEY": "written-down"}, r"WANDB_API_KEY holds a credential"),
        ({"SERVICE_TOKEN": "${SERVICE_TOKEN:-written}"}, r"SERVICE_TOKEN holds a credential"),
        ({"DATABASE": "${DB_PASSWORD:-written}"}, r"DATABASE holds a credential"),
        ({"ORBIT_HOME": "x"}, r"ORBIT_HOME: the ORBIT_ prefix is reserved"),
        ({"KEY": "${ORBIT_OPENAI_API_KEY}"}, r"the Host's ORBIT_OPENAI_API_KEY never enters"),
        ({"1ST": "x"}, r"'1ST' is not an environment variable name"),
        ({"HEADER": "Bearer ${WANDB_API_KEY}"}, r"HEADER: only a whole value \$\{NAME\} is"),
    ],
)
def test_agent_env_takes_a_task_env_s_form_and_its_credentials_are_references(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        RunConfig.model_validate({"agent": {"env": env}})


def test_agent_env_accepts_what_a_task_env_does() -> None:
    env = {
        "WANDB_API_KEY": "${WANDB_API_KEY}",
        "OPTIONAL_TOKEN": "${OPTIONAL_TOKEN:-}",
        "WANDB_MODE": "${WANDB_MODE:-online}",
        "MAX_TOKENS": "4096",
    }
    assert RunConfig.model_validate({"agent": {"env": env}}).agent.env == env


def _plain_task(root: Path, config: dict[str, Any], name: str) -> ManifestTask:
    return load_task_folder(write_task(root, name=name, config=config))


def test_agent_env_resolves_once_for_every_task_from_the_env_file(
    tmp_path: Path, env_file: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file(f"WANDB_API_KEY={SECRET}\nWANDB_ENTITY=lab\n")
    monkeypatch.setenv("SHELL_ONLY", "from-the-operator-shell")
    agent = {
        "WANDB_API_KEY": "${WANDB_API_KEY}",
        "WANDB_ENTITY": "${WANDB_ENTITY}",
        "WANDB_MODE": "${WANDB_MODE:-online}",
        "PROJECT": "ablation",
    }
    tasks = [
        _plain_task(tmp_path / name, {}, f"agentslastexam/{name}") for name in ("first", "second")
    ]
    sources = resolve_task_variables(tasks, oracle=False, protected={}, agent=agent)
    assert [(item.name, item.source) for item in sources] == [
        ("WANDB_API_KEY", "env_file"),
        ("WANDB_ENTITY", "env_file"),
        ("WANDB_MODE", "default"),
        ("PROJECT", "literal"),
    ]
    for task in tasks:
        assert task.variables is not None
        assert task.variables.agent == {
            "WANDB_API_KEY": SECRET,
            "WANDB_ENTITY": "lab",
            "WANDB_MODE": "online",
            "PROJECT": "ablation",
        }
        # Only the credential is a secret, and it is every record's to leave out.
        assert task.variables.secrets == (SECRET,)
        # The Task's own records name only the Task's variables.
        assert task.variables.provenance == ()

    # One missing variable is named once, for the Run, beside each Task's own.
    with pytest.raises(ConfigError) as raised:
        resolve_task_variables(
            [
                _plain_task(
                    tmp_path / "missing",
                    {"environment": {"env": {"A": "${A}"}}},
                    "agentslastexam/missing",
                )
            ],
            oracle=False,
            protected={},
            agent={"KEY": "${SHELL_ONLY}"},
        )
    message = str(raised.value)
    assert "the Run: SHELL_ONLY ([agent.env]) not set" in message
    assert "agentslastexam/missing: A ([environment].env) not set" in message


def test_agent_env_never_carries_a_host_credential(
    tmp_path: Path, env_file: Callable[[str], None]
) -> None:
    env_file("OPENAI_API_KEY=sk-host-model-key\n")
    with pytest.raises(ConfigError) as raised:
        resolve_task_variables(
            [_plain_task(tmp_path, {}, "agentslastexam/demo")],
            oracle=False,
            protected={"ORBIT_OPENAI_API_KEY": "sk-host-model-key"},
            agent={"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
        )
    message = str(raised.value)
    assert (
        "the Run: [agent.env] OPENAI_API_KEY holds the Host credential ORBIT_OPENAI_API_KEY"
        in message
    )
    assert "sk-host-model-key" not in message


def test_a_name_a_task_also_sets_fails_naming_both(
    tmp_path: Path, env_file: Callable[[str], None]
) -> None:
    config = {
        "environment": {"env": {"REGION": "eu"}},
        "verifier": {"env": {"WANDB_API_KEY": "${WANDB_API_KEY}"}},
        "solution": {"env": {"ANSWER": "42"}},
    }
    task = _plain_task(tmp_path, config, "agentslastexam/demo")
    env_file(f"WANDB_API_KEY={SECRET}\n")
    agent = {"REGION": "us", "WANDB_API_KEY": "${WANDB_API_KEY}", "ANSWER": "0", "OWN": "x"}
    with pytest.raises(ConfigError) as raised:
        resolve_task_variables([task], oracle=False, protected={}, agent=agent)
    problems = str(raised.value).splitlines()[1:]
    assert [line.strip() for line in problems] == [
        "agentslastexam/demo: [agent.env] REGION is also set by [environment].env",
        "agentslastexam/demo: [agent.env] WANDB_API_KEY is also set by [verifier].env",
        "agentslastexam/demo: [agent.env] ANSWER is also set by [solution].env",
    ]


class _Printing(TurnsHarness):
    """Each turn runs a command that prints ``WANDB_API_KEY``, and answers with it in its
    native session."""

    async def _turn(
        self, prompt: str, sandbox: Any, session: HarnessSession, timeout_sec: float
    ) -> AgentRun:
        printed = (await sandbox.exec(["printenv", "WANDB_API_KEY"])).stdout.strip()
        run = await super()._turn(prompt, sandbox, session, timeout_sec)
        *earlier, last = (await sandbox.read_file(TURNS_LOG)).decode().splitlines()
        event = json.loads(last)
        event["agent"] += f" saw {printed}"
        await sandbox.write_file(
            TURNS_LOG, "".join(f"{line}\n" for line in (*earlier, json.dumps(event))).encode()
        )
        return AgentRun(exit_code=0, final_message=event["agent"], continuation=run.continuation)


@pytest.mark.asyncio
async def test_agent_env_reaches_every_agent_turn_and_no_other_command_unrecorded(
    tmp_path: Path,
    env_file: Callable[[str], None],
    verifiers: Verifiers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[tuple[str, ...], dict[str, str]]] = []
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        env = dict(options.get("env") or {})
        commands.append((tuple(argv), env))
        if list(argv) == ["printenv", "WANDB_API_KEY"]:
            text = env.get("WANDB_API_KEY", "") + "\n"
            if options.get("output_sink") is not None:
                await options["output_sink"]("stdout", text.encode())
            return ExecResult(exit_code=0, stdout=text)
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    env_file(f"WANDB_API_KEY={SECRET}\n")
    task = _task(
        tmp_path,
        rounds=1,
        environment={"cpus": 1, "memory_mb": 1024, "env": {"REGION": "eu"}},
    )
    resolve_task_variables(
        [task], oracle=False, protected={}, agent={"WANDB_API_KEY": "${WANDB_API_KEY}"}
    )
    harness = _Printing()
    result = await _run(tmp_path, task, harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure

    # Both turns' commands, beside the Task's [environment].env; no other command, setup, the
    # Harness's install and either verifier, of the round or of tests/, among them.
    given = [(argv, env) for argv, env in commands if "WANDB_API_KEY" in env]
    assert [(argv, env["WANDB_API_KEY"], env["REGION"]) for argv, env in given] == [
        (("printenv", "WANDB_API_KEY"), SECRET, "eu")
    ] * 2
    assert len(commands) > len(given) and len(verifiers.envs) == 2
    assert all(env["REGION"] == "eu" for env in verifiers.envs)
    # The agent read it, and no record of the episode holds it.
    holding = [
        str(path.relative_to(result.run_dir))
        for path in result.run_dir.rglob("*")
        if path.is_file() and SECRET.encode() in path.read_bytes()
    ]
    assert holding == []
    trace = (result.run_dir / "trace.execution.jsonl").read_text()
    assert trace.count("[REDACTED]") >= 2
    trajectory = (result.run_dir / "trajectory.json").read_text()
    assert "saw [REDACTED]" in trajectory


@pytest.mark.asyncio
async def test_the_oracle_never_receives_agent_env(
    tmp_path: Path,
    env_file: Callable[[str], None],
    verifiers: Verifiers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envs: list[dict[str, str]] = []
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        envs.append(dict(options.get("env") or {}))
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    env_file(f"WANDB_API_KEY={SECRET}\n")
    task = _task(
        tmp_path, rounds=0, environment={"cpus": 1, "memory_mb": 1024, "env": {"REGION": "eu"}}
    )
    resolve_task_variables(
        [task], oracle=True, protected={}, agent={"WANDB_API_KEY": "${WANDB_API_KEY}"}
    )
    result = await _run(tmp_path, task, OracleHarness(), rounds=0)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # The oracle's commands receive the Task's variables, and never the agent's.
    assert any(env.get("REGION") == "eu" for env in envs)
    assert not any("WANDB_API_KEY" in env for env in envs)


# --- the commands ---


async def _prepared(tasks: Any, _providers: Any, _continued: Any = None) -> None:
    for task in tasks:
        task.prepared_image = IMAGE


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["nop", "oracle"])
async def test_orbit_run_gives_the_env_to_an_agent_and_none_to_the_oracle(
    tmp_path: Path, env_file: Callable[[str], None], monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    task_cli = import_module("orbit.run.cli.tasks")
    env_file(f"WANDB_API_KEY={SECRET}\n" if agent == "nop" else "")
    seen: list[tuple[Any, Any]] = []

    async def execute(task: Any, protocol: Any, _providers: Any, **kwargs: Any) -> Any:
        seen.append((task.variables, kwargs["provenance"].agent))
        return write_result(kwargs["run_dir"] / kwargs["episode_id"], Status.COMPLETED, 1)

    monkeypatch.setattr(task_cli, "_prepare_images", _prepared)
    monkeypatch.setattr(task_cli, "run_episode", execute)
    monkeypatch.setattr(ProviderRegistry, "preflight", AsyncMock())
    settings = RunConfig.model_validate(
        {
            "agent": {
                "name": agent,
                "env": {"WANDB_API_KEY": "${WANDB_API_KEY}"},
            }
        }
    )
    task = scaffold_task(tmp_path / "task")
    assert await task_cli._run_one(str(task), settings, tmp_path / "runs", "run") == 0
    ((variables, provenance),) = seen
    if agent == "nop":
        assert variables.agent == {"WANDB_API_KEY": SECRET}
        assert SECRET in variables.secrets
        assert provenance.env == (AgentVariableProvenance(name="WANDB_API_KEY", source="env_file"),)
    else:
        # Not even resolved: the oracle needs none of the .env's agent keys.
        assert variables.agent == {}
        assert provenance.env == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "resolved"), [("calibrate", False), ("reverify", True)])
async def test_calibrate_never_resolves_agent_env_and_reverify_does_to_redact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, resolved: bool
) -> None:
    task_cli = import_module("orbit.run.cli.tasks")
    agents: list[Any] = []

    def resolve(tasks: Any, *, oracle: bool, protected: Any, agent: Any = None) -> Any:
        agents.append(agent)
        raise ConfigError("resolution observed")

    monkeypatch.setattr(task_cli, "resolve_task_variables", resolve)
    # A source episode that kept its solver; resolution stops the command before it is read.
    monkeypatch.setattr(task_cli, "_retained_source_episodes", lambda _source: ((None,) * 4,))
    settings = RunConfig.model_validate({"agent": {"env": {"WANDB_MODE": "online"}}})
    task, runs = str(scaffold_task(tmp_path / "task")), tmp_path / "runs"
    if command == "calibrate":
        code = await task_cli._calibrate(task, settings, runs)
    else:
        code = await task_cli._reverify(task, tmp_path / "source", settings, runs)
    assert code != 0
    assert agents == [{"WANDB_MODE": "online"} if resolved else None]

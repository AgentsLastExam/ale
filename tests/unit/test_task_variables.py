"""Task variables: Harbor's env tables, resolved on the Host, received by their commands only."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support import write_task

from ale.core.episode import Phase
from ale.core.errors import ConfigError, TaskDefinitionError
from ale.core.sandbox import ExecResult
from ale.core.task import ResolvedVariables
from ale.core.taskspec import OperatingSystem, TaskVariables, VerificationMode, VerifySpec
from ale.core.trace import read_jsonl
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import NopHarness
from ale.run.lint import lint_repository
from ale.run.protocol.episode_protocol import EpisodeProtocol, _RecordedSandbox
from ale.run.recording import EpisodeRecording
from ale.run.secrets import host_credentials, resolve_task_variables
from ale.run.tasksets.manifest import ManifestTask, load_task_folder

pytestmark = pytest.mark.unit


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """The checkout's ``.env``, written by the test."""
    path = tmp_path / "checkout.env"
    path.write_text("")
    monkeypatch.setenv("ALE_ENV_FILE", str(path))
    return path.write_text


def _task(root: Path, config: dict[str, Any], *, name: str = "agentslastexam/demo") -> Path:
    return write_task(root, name=name, config=config)


def _resolved(
    root: Path,
    config: dict[str, Any],
    *,
    oracle: bool = True,
    protected: dict[str, str] | None = None,
) -> ManifestTask:
    task = load_task_folder(_task(root, config))
    resolve_task_variables([task], oracle=oracle, protected=protected or {})
    return task


def test_env_tables_reach_the_commands_harbor_gives_them(tmp_path: Path) -> None:
    config = {
        "environment": {"env": {"SHARED": "environment", "ONLY": "environment"}},
        "verifier": {"env": {"SHARED": "verifier"}},
        "solution": {"env": {"ONLY": "solution"}},
    }
    variables = load_task_folder(_task(tmp_path / "a", config)).spec.variables
    assert variables.environment == {"SHARED": "environment", "ONLY": "environment"}
    assert variables.oracle() == {"SHARED": "environment", "ONLY": "solution"}
    shared = {"SHARED": "verifier", "ONLY": "environment"}
    assert variables.verification(VerificationMode.SHARED) == shared
    # A separate verifier gets a copy of [environment], so its env too, as in Harbor.
    assert variables.verification(VerificationMode.SEPARATE) == shared

    config["verifier"]["environment"] = {"cpus": 1, "memory_mb": 512, "env": {"OWN": "own"}}
    variables = load_task_folder(_task(tmp_path / "b", config)).spec.variables
    assert variables.verification(VerificationMode.SEPARATE) == {"OWN": "own", "SHARED": "verifier"}


@pytest.mark.parametrize(
    ("layer", "message"),
    [
        ({"environment": {"env": {"1ST": "x"}}}, r"environment\.env: '1ST' is not an environment"),
        ({"solution": {"env": {"ALE_HOME": "x"}}}, r"solution\.env: ALE_HOME: the ALE_ prefix"),
        (
            {"verifier": {"env": {"KEY": "${ALE_OPENAI_API_KEY}"}}},
            r"verifier\.env: KEY: a Task cannot refer to the Host's ALE_OPENAI_API_KEY",
        ),
    ],
)
def test_variable_names_are_checked_when_the_task_loads(
    tmp_path: Path, layer: dict[str, Any], message: str
) -> None:
    with pytest.raises(TaskDefinitionError, match=message):
        load_task_folder(_task(tmp_path, layer))


def test_references_resolve_from_the_env_file_alone(
    tmp_path: Path, env_file: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file("SERVICE_TOKEN=tok-from-file\nEMPTY=\n")
    monkeypatch.setenv("SHELL_ONLY", "from-the-operator-shell")
    env = {
        "SERVICE_TOKEN": "${SERVICE_TOKEN}",
        "FALLBACK": "${EMPTY:-fallback}",
        "OPTIONAL": "${UNSET:-}",
        "PLAIN": "literal",
        "HEADER": "Bearer ${SERVICE_TOKEN}",
    }
    task = _resolved(tmp_path / "ok", {"environment": {"env": env}})
    assert task.variables is not None
    assert task.variables.values.environment == {
        "SERVICE_TOKEN": "tok-from-file",
        "FALLBACK": "fallback",
        "OPTIONAL": "",
        "PLAIN": "literal",
        "HEADER": "Bearer ${SERVICE_TOKEN}",
    }
    assert [(item.name, item.source) for item in task.variables.provenance] == [
        ("SERVICE_TOKEN", "env_file"),
        ("FALLBACK", "default"),
        ("OPTIONAL", "default"),
        ("PLAIN", "literal"),
        ("HEADER", "literal"),
    ]

    # Exported only in the shell, or empty in the file: both are unset.
    with pytest.raises(ConfigError, match=r"SHELL_ONLY \(\[environment\]\.env\), EMPTY"):
        _resolved(
            tmp_path / "missing",
            {"environment": {"env": {"A": "${SHELL_ONLY}", "B": "${EMPTY}"}}},
        )


def test_every_missing_variable_of_every_task_is_named_at_once(
    tmp_path: Path, env_file: Callable[[str], None]
) -> None:
    first = load_task_folder(
        _task(
            tmp_path / "first",
            {"environment": {"env": {"A": "${A}"}}, "verifier": {"env": {"B": "${B}"}}},
            name="agentslastexam/first",
        )
    )
    second = load_task_folder(
        _task(
            tmp_path / "second", {"verifier": {"env": {"C": "${C}"}}}, name="agentslastexam/second"
        )
    )
    with pytest.raises(ConfigError) as raised:
        resolve_task_variables([first, second], oracle=False, protected={})
    message = str(raised.value)
    assert "agentslastexam/first: A ([environment].env), B ([verifier].env) not set" in message
    assert "agentslastexam/second: C ([verifier].env) not set" in message


def test_solution_variables_are_resolved_for_the_oracle_only(
    tmp_path: Path, env_file: Callable[[str], None]
) -> None:
    config = {"solution": {"env": {"ANSWER": "${ANSWER}"}}}
    task = _resolved(tmp_path / "agent", config, oracle=False)
    assert task.variables is not None and task.variables.values.solution == {}
    with pytest.raises(ConfigError, match=r"ANSWER \(\[solution\]\.env\)"):
        _resolved(tmp_path / "oracle", config, oracle=True)


def test_a_host_credential_never_becomes_a_task_variable(
    tmp_path: Path, env_file: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALE_OPENAI_API_KEY", "sk-host-model-key")
    monkeypatch.setenv("UPSTREAM_KEY", "upstream-key-named-by-the-run")
    monkeypatch.setenv("ALE_OPENAI_BASE_URL", "https://api.example.org")
    env_file("OPENAI_API_KEY=sk-host-model-key\nKEY=upstream-key-named-by-the-run\n")
    protected = host_credentials(("UPSTREAM_KEY", ""))
    assert {"ALE_OPENAI_API_KEY", "UPSTREAM_KEY"} <= set(protected)
    assert "ALE_OPENAI_BASE_URL" not in protected
    with pytest.raises(ConfigError) as raised:
        _resolved(
            tmp_path,
            {
                "environment": {"env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"}},
                "verifier": {"env": {"K": "${KEY}"}},
            },
            protected=protected,
        )
    message = str(raised.value)
    assert (
        "[environment].env OPENAI_API_KEY holds the Host credential ALE_OPENAI_API_KEY" in message
    )
    assert "[verifier].env K holds the Host credential UPSTREAM_KEY" in message
    assert "sk-host-model-key" not in message


def test_credentials_from_the_env_file_are_the_secrets(
    tmp_path: Path, env_file: Callable[[str], None]
) -> None:
    env_file(
        "SERVICE_TOKEN=tok-service\nDB_PASSWORD=pw-database\nCOUNT=4096\nFLAG=1\nREGION=eu-west\n"
    )
    env = {
        "SERVICE_TOKEN": "${SERVICE_TOKEN}",
        "DATABASE": "${DB_PASSWORD}",
        "MAX_TOKENS": "${COUNT}",
        "USE_AUTH": "${FLAG}",
        "REGION": "${REGION}",
        "LITERAL_TOKEN": "written-in-the-task",
        "DEFAULT_KEY": "${UNSET_KEY:-written-default}",
    }
    task = _resolved(tmp_path, {"environment": {"env": env}})
    assert task.variables is not None
    assert task.variables.secrets == ("pw-database", "tok-service")


async def test_an_episode_refuses_variables_nobody_resolved(tmp_path: Path) -> None:
    task = load_task_folder(_task(tmp_path, {"environment": {"env": {"A": "a"}}}))
    with pytest.raises(TaskDefinitionError, match="its variables were not resolved"):
        await run_episode(task, None, None, run_dir=tmp_path / "runs")  # type: ignore[arg-type]


def test_lint_refuses_written_credentials_and_explains_embedded_references(
    tmp_path: Path,
) -> None:
    env = {
        "API_KEY": "sk-written-down",
        "SERVICE_TOKEN": "${SERVICE_TOKEN:-written-default}",
        "DATABASE": "${DB_PASSWORD:-written-default}",
        "GOOD_TOKEN": "${GOOD_TOKEN}",
        "OPTIONAL_TOKEN": "${OPTIONAL_TOKEN:-}",
        "MAX_TOKENS": "4096",
        "HEADER": "Bearer ${GOOD_TOKEN}",
    }
    task = _task(
        tmp_path / "task",
        {
            "environment": {"env": env},
            "verifier": {"environment": {"cpus": 1, "memory_mb": 512, "env": {"PASSWORD": "x"}}},
        },
    )
    messages = [item.message for item in lint_repository(task)]
    assert messages == [
        f"[environment].env {key} holds a credential; write ${{NAME}} with no default and set "
        "NAME in the checkout's .env"
        for key in ("API_KEY", "SERVICE_TOKEN", "DATABASE")
    ] + [
        "[environment].env HEADER: only a whole value ${NAME} is resolved; a ${...} inside "
        "other text stays as written",
        "[verifier.environment].env PASSWORD holds a credential; write ${NAME} with no default "
        "and set NAME in the checkout's .env",
    ]


class _Sandbox:
    """Records each command's environment and answers with the given output."""

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.envs: list[dict[str, str] | None] = []

    async def exec(self, argv, *, env=None, output_sink=None, **_: object) -> ExecResult:  # type: ignore[no-untyped-def]
        self.envs.append(env)
        if output_sink is not None:
            await output_sink("stdout", self.stdout.encode())
        return ExecResult(exit_code=0, stdout=self.stdout)


def _context(tmp_path: Path, variables: TaskVariables, *, secrets: tuple[str, ...] = ()) -> Any:
    recording = EpisodeRecording(tmp_path)
    return SimpleNamespace(
        execution=recording.execution,
        blobs=recording.blobs,
        episode_id="episode",
        home="/home/user",
        session=SimpleNamespace(token="episode-token"),
        spec=SimpleNamespace(
            os=OperatingSystem.LINUX,
            verify=VerifySpec(
                environment_mode="separate",
                resources={"cpus": 1, "memory_mb": 512, "storage_mb": None, "gpus": 0},
            ),
        ),
        variables=ResolvedVariables(values=variables, secrets=secrets),
        secrets=("episode-token", *secrets),
        verification_command_env={},
        phase_timeout_sec=1.0,
        phase_deadline=None,
        run_dir=tmp_path,
        harness_execution_counter=0,
        phases=[],
    )


async def test_each_stage_receives_its_tables_beneath_the_framework_variables(
    tmp_path: Path,
) -> None:
    variables = TaskVariables(
        environment={"SHARED": "environment", "ONLY": "environment"},
        verifier={"SHARED": "verifier"},
        verifier_environment={"OWN": "own"},
    )
    ctx = _context(tmp_path, variables)
    sandbox = _Sandbox()
    protocol = EpisodeProtocol(NopHarness())
    for phase in (Phase.SETUP, Phase.VERIFY):
        await protocol._run_stage(ctx, sandbox, PurePosixPath("/opt/ale/stage"), phase)  # type: ignore[arg-type]
    setup, verify = sandbox.envs
    assert setup is not None and verify is not None
    assert {key: setup[key] for key in ("SHARED", "ONLY")} == variables.environment
    assert setup["ALE_STAGE_DIR"] == "/opt/ale/stage"
    # The separate verifier declared its own [verifier.environment].env.
    assert "ONLY" not in verify and verify["OWN"] == "own" and verify["SHARED"] == "verifier"


async def test_a_command_s_own_variables_win_over_the_task_s(tmp_path: Path) -> None:
    ctx = _context(tmp_path, TaskVariables())
    sandbox = _Sandbox()
    recorded = _RecordedSandbox(
        ctx,
        sandbox,
        component="harness-launch",
        env={"A": "task", "B": "task"},  # type: ignore[arg-type]
    )
    await recorded.exec(["agent"], env={"B": "harness"})
    await recorded.exec(["agent"])
    assert sandbox.envs == [{"A": "task", "B": "harness"}, {"A": "task", "B": "task"}]


async def test_stage_output_never_records_a_secret(tmp_path: Path) -> None:
    ctx = _context(tmp_path, TaskVariables(), secrets=("tok-service",))
    sandbox = _Sandbox(stdout="setup saw tok-service\n")
    await EpisodeProtocol(NopHarness())._run_stage(
        ctx,
        sandbox,
        PurePosixPath("/opt/ale/setup"),
        Phase.SETUP,  # type: ignore[arg-type]
    )
    trace = (tmp_path / "trace.execution.jsonl").read_text()
    assert "tok-service" not in trace
    (finished,) = [
        record
        for record in read_jsonl(tmp_path / "trace.execution.jsonl").records
        if record["kind"] == "command_finished"
    ]
    assert finished["stdout"]["inline"] == "setup saw [REDACTED]\n"

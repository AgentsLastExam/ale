from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest
import yaml
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, ConfigError, HarnessLimitError
from ale.core.harness import TrajectoryParseContext
from ale.core.taskspec import OperatingSystem
from ale.core.trajectory import AtifTrajectory
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.base import INVOCATIONS_PATH, STDOUT_PATH
from ale.run.harnesses.hermes import HermesHarness
from ale.run.harnesses.hermes.adapter import COMMIT
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

STATE = "/home/agent/.hermes-ale"
SESSION_ID = "local-0348e9898df56a45c55bf29ac2e74de9de357de000438db7a45146582053fb20"


def test_preset_pins_the_official_commit() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "hermes.toml")
    harness = _harness(config)
    assert isinstance(harness, HermesHarness)
    assert harness.gateway_dialect == config.gateway.dialect == "openai-chat-completions"
    assert config.gateway.provider == "openai"
    assert harness.integrity() == f"git:NousResearch/hermes-agent@{COMMIT}"
    assert set(harness.supported_os) == set(OperatingSystem)


@pytest.mark.parametrize(
    "settings",
    [{"max_turn": 3}, {"max_turns": 0}, {"max_turns": "unlimited"}, {"reasoning_effort": "ultra"}],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        HermesHarness(settings=settings)


def test_other_versions_and_dialects_fail_closed() -> None:
    with pytest.raises(ConfigError, match="pins commit"):
        HermesHarness(cli_version="0.22.4")
    with pytest.raises(ConfigError, match="dialect"):
        HermesHarness(gateway_dialect="gemini")


@pytest.mark.parametrize(
    ("dialect", "transport", "api"),
    [
        ("anthropic", "anthropic_messages", "http://gateway"),
        ("openai-chat-completions", "chat_completions", "http://gateway/v1"),
        ("openai-responses", "codex_responses", "http://gateway/v1"),
    ],
)
async def test_the_gateway_is_the_one_provider(
    tmp_path: Path, dialect: str, transport: str, api: str
) -> None:
    sandbox = FakeSandbox()
    await HermesHarness(gateway_dialect=dialect).install_resources(
        sandbox,  # type: ignore[arg-type]
        session(),
        resources(tmp_path),
    )
    config = yaml.safe_load(sandbox.files[f"{STATE}/config.yaml"])
    assert config["model"] == {"provider": "ale", "default": "test-model"}
    assert config["providers"]["ale"] == {
        "api": api,
        "key_env": "ALE_GATEWAY_TOKEN",
        "transport": transport,
        "default_model": "test-model",
        "discover_models": False,
    }
    assert config["updates"] == {"check": False}
    assert config["auth"] == {"adopt_external_logins": False}
    assert config["mcp_servers"] == {
        "task-proof": {
            "command": "python3",
            "args": ["/home/agent/server.py"],
            "env": {"PROOF_HOME": "/home/agent"},
        }
    }
    assert sandbox.uploads == [(str(tmp_path / "resource-proof"), f"{STATE}/skills/resource-proof")]
    assert b"token\n" not in sandbox.files[f"{STATE}/config.yaml"]


@pytest.mark.parametrize("native_session_id", [None, SESSION_ID])
async def test_command_streams_events_and_exports_the_sessions(
    native_session_id: str | None,
) -> None:
    command = await HermesHarness(
        settings={"max_turns": 50, "reasoning_effort": "low"}
    ).prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=native_session_id,
    )
    assert command.session_id == native_session_id
    # A background gateway an earlier invocation started would serve with its old token.
    assert command.script.startswith("/home/agent/.local/bin/hermes gateway stop < /dev/null")
    assert (
        "/home/agent/.local/bin/hermes chat --query-file /home/agent/.ale-harness/prompt.md "
        "--format stream-json --yolo"
    ) in command.script
    assert ("--resume " + SESSION_ID in command.script) is (native_session_id is not None)
    assert "--max-turns 50 --reasoning low < /dev/null" in command.script
    assert "/home/agent/.local/bin/hermes sessions export /home/agent/.hermes-ale/exports/" in (
        command.script
    )
    assert command.script.endswith('exit "$status"')
    assert command.env["HERMES_HOME"] == STATE
    assert command.env["ALE_GATEWAY_TOKEN"] == "token"


def events(*items: dict[str, object]) -> str:
    return "".join(json.dumps(item) + "\n" for item in items)


def result(**fields: object) -> dict[str, object]:
    return {"type": "result", "session_id": SESSION_ID, "exit_code": 0, "text": "done", **fields}


def test_result_is_the_last_result_event() -> None:
    harness = HermesHarness()
    done = harness.read_result(events({"type": "system", "subtype": "init"}, result()))
    assert (done.session_id, done.final_message, done.error) == (SESSION_ID, "done", None)
    missing = harness.read_result(
        events(result(session_id="", exit_code=1, text="", error="No session found"))
    )
    assert (missing.session_id, missing.error) == (None, "No session found")
    assert harness.read_result("").error == "hermes reported no result"


async def test_a_failed_turn_fails_and_the_turn_limit_is_typed() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = events(result(exit_code=1, text="Something went wrong")).encode()
    with pytest.raises(AgentError, match="exit code 1"):
        await HermesHarness().launch("go", sandbox, session(), timeout_sec=60)  # type: ignore[arg-type]
    limited = HermesHarness(settings={"max_turns": 5})
    assert isinstance(limited.classify_failure("reached max_turns (5)"), HarnessLimitError)
    assert not isinstance(HermesHarness().classify_failure("reached max_turns"), HarnessLimitError)


def test_session_files_are_the_sessions_exports() -> None:
    harness = HermesHarness()
    exports = PurePosixPath(".hermes-ale/exports")
    assert harness.is_session_file(exports / SESSION_ID / "20261026T000000Z.jsonl", SESSION_ID)
    assert not harness.is_session_file(exports / f"{SESSION_ID}.jsonl", SESSION_ID)
    assert not harness.is_session_file(exports / SESSION_ID / "x.json", SESSION_ID)


def recorded_context(root: Path, **updates: object) -> TrajectoryParseContext:
    first = json.loads((root / INVOCATIONS_PATH).read_text().splitlines()[0])
    stdout = (root / STDOUT_PATH).read_text()
    session_id = HermesHarness().read_result(stdout).session_id
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "hermes-trajectory",
        "instruction": first["prompt"],
        "logs_dir": root,
        "model": "scripted-model",
        "agent_version": "0.22.5",
        "blobs": BlobStore(root),
        "session_id": session_id,
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def record(root: Path, *prompts: str, stdout: str | list[str]) -> None:
    """Each invocation's prompt; a list gives each invocation its own stdout."""
    parts = stdout if isinstance(stdout, list) else [""] * (len(prompts) - 1) + [stdout]
    offsets = [sum(len(part.encode()) for part in parts[:index]) for index in range(len(parts))]
    (root / STDOUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / STDOUT_PATH).write_text("".join(parts))
    (root / INVOCATIONS_PATH).write_text(
        "".join(
            json.dumps({"prompt": prompt, "session_id": None, "stdout_offset": offset}) + "\n"
            for prompt, offset in zip(prompts, offsets, strict=True)
        )
    )


def write_export(root: Path, *sessions: dict[str, object]) -> None:
    path = root / ".hermes-ale/exports" / SESSION_ID / "20261026T000000Z.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in sessions))


def call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}


def user(text: str) -> dict[str, object]:
    return {"role": "user", "content": text}


def agent(text: str, *calls: dict[str, object]) -> dict[str, object]:
    return {"role": "assistant", "content": text, "tool_calls": list(calls) or None}


def tool(call_id: str, content: str) -> dict[str, object]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def child(
    session_id: str, parent: str, *messages: dict[str, object], **markers: object
) -> dict[str, object]:
    """A session Hermes spawned from ``parent``: delegated unless other markers say so."""
    return {
        "id": session_id,
        "parent_session_id": parent,
        "model_config": json.dumps(markers or {"_delegate_from": parent}),
        "messages": list(messages),
    }


def parse(
    tmp_path: Path, prompts: list[str], *sessions: dict[str, object], stdout: str | None = None
) -> AtifTrajectory:
    record(tmp_path, *prompts, stdout=stdout or events(*(result() for _ in prompts)))
    write_export(tmp_path, *sessions)
    return HermesHarness().parse_trajectory(recorded_context(tmp_path))


def refs(trajectory: AtifTrajectory) -> list[str]:
    return [
        ref.trajectory_id
        for step in trajectory.steps
        if step.observation
        for result in step.observation.results
        for ref in result.subagent_trajectory_ref or ()
    ]


def test_delegation_mcp_names_and_failures_convert(tmp_path: Path) -> None:
    (tmp_path / ".hermes-ale/config.yaml").parent.mkdir(parents=True)
    (tmp_path / ".hermes-ale/config.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {"task-proof": {"command": "x"}}})
    )
    trajectory = parse(
        tmp_path,
        ["Delegate."],
        {
            "id": SESSION_ID,
            "messages": [
                user("Delegate."),
                {
                    **agent(
                        "",
                        call("c1", "delegate_task", {"tasks": [{"goal": "Sum."}]}),
                        call("c2", "mcp__task_proof__derive", {}),
                    ),
                    "reasoning_content": "plan",
                },
                tool("c1", '{"results": []}'),
                tool("c2", "fragment"),
            ],
        },
        child("child", SESSION_ID, user("Sum."), agent("Summed.")),
        stdout=events(result(exit_code=1, text="Something failed")),
    )

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Delegate."),
        ("agent", ""),
        ("system", "hermes ended the turn with exit code 1: Something failed"),
    ]
    step = trajectory.steps[1]
    assert step.reasoning_content == "plan"
    _, derive = step.tool_calls or ()
    assert derive.extra == {"ale": {"mcp": {"server": "task-proof", "tool": "derive"}}}
    delegated, mcp = step.observation.results  # type: ignore[union-attr]
    (ref,) = delegated.subagent_trajectory_ref or ()
    assert (ref.session_id, ref.trajectory_id) == ("child", "hermes-trajectory-subagent-child")
    (embedded,) = trajectory.subagent_trajectories or ()
    assert [step.message for step in embedded.steps] == ["Sum.", "Summed."]
    assert mcp.content == "fragment"
    assert not trajectory.extra


def test_nested_delegations_embed_in_their_spawners(tmp_path: Path) -> None:
    trajectory = parse(
        tmp_path,
        ["Go."],
        {
            "id": SESSION_ID,
            "messages": [
                user("Go."),
                # The legacy single goal, and a batch sent as a JSON string.
                agent("", call("c1", "delegate_task", {"goal": "Plan."})),
                tool("c1", "planned"),
                agent("", call("c2", "delegate_task", {"tasks": '[{"goal": "Twin."}]'})),
                tool("c2", "twins"),
                agent("Done."),
            ],
        },
        child(
            "plan",
            SESSION_ID,
            user("Plan."),
            agent("", call("p1", "delegate_task", {"tasks": [{"goal": "Leaf."}]})),
            tool("p1", "leafed"),
        ),
        child("leaf", "plan", user("Leaf."), agent("Leafed.")),
        # Two children with one goal: neither is the call's.
        child("twin-a", SESSION_ID, user("Twin.")),
        child("twin-b", SESSION_ID, user("Twin.")),
        # Delegated by an unlinked child, and therefore unlinked too.
        child("twin-leaf", "twin-a", user("Deeper.")),
        # Other children of the session are not subagents.
        child("branch", SESSION_ID, user("Go."), _branched_from=SESSION_ID),
        child("reset", SESSION_ID, user("Go."), _reset_from=SESSION_ID),
        {**child("aux", SESSION_ID, user("Title?")), "source": "tool", "model_config": "{}"},
        child("elsewhere", "another-root", user("Plan.")),
    )

    (plan,) = trajectory.subagent_trajectories or ()
    assert plan.trajectory_id == "hermes-trajectory-subagent-plan"
    assert refs(trajectory) == [plan.trajectory_id]
    (leaf,) = plan.subagent_trajectories or ()
    assert leaf.trajectory_id == "hermes-trajectory-subagent-plan-subagent-leaf"
    assert refs(plan) == [leaf.trajectory_id]
    assert [step.message for step in leaf.steps] == ["Leaf.", "Leafed."]
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unlinked_subagent", "session_id": "twin-a"},
        {"reason": "unlinked_subagent", "session_id": "twin-b"},
        {"reason": "unlinked_subagent", "session_id": "twin-leaf"},
    ]


def test_compression_continuations_continue_the_conversation(tmp_path: Path) -> None:
    """A session rotated by compression goes on in its continuation, after a summary."""
    first = {
        "id": "head",
        "end_reason": "compression",
        "messages": [user("One."), agent("Did one."), user("Two.")],
    }
    tip = {
        **child(
            SESSION_ID,
            "head",
            {"role": "user", "content": "Summary.", "_compressed_summary": True},
            user("Two."),
            agent("", call("c1", "delegate_task", {"tasks": [{"goal": "Help."}]})),
            tool("c1", "helped"),
            user("Three."),
            agent("Did three."),
        ),
        "model_config": json.dumps({"_usage_anchor": {}}),
        "end_reason": "compression",
    }
    trajectory = parse(
        tmp_path,
        ["One.", "Two.", "Three.", "Four."],
        first,
        tip,
        child("helper", SESSION_ID, user("Help.")),
        # A continuation is never a subagent, even beside a delegated child.
        {**child("next", SESSION_ID, user("Four.")), "model_config": "{}"},
        # A child delegated before the compression is still not a continuation.
        child("late-helper", "head", user("Help later.")),
    )

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "One."),
        ("agent", "Did one."),
        ("user", "Two."),
        ("system", "Summary."),
        ("agent", ""),
        ("user", "Three."),
        ("agent", "Did three."),
        ("user", "Four."),
    ]
    assert refs(trajectory) == ["hermes-trajectory-subagent-helper"]
    # All four prompts are in the conversation; only the delegated child is a subagent.
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unlinked_subagent", "session_id": "late-helper"}
    ]


def row(
    message: dict[str, object], at: int, uid: str, *, active: int, compacted: int = 0
) -> dict[str, object]:
    """A stored message: shown while active, archived when compacted, else removed."""
    flags = {"active": active, "compacted": compacted}
    return {**message, "timestamp": at, "message_uid": uid, **flags}


def summary(message: dict[str, object]) -> dict[str, object]:
    return {**message, "_compressed_summary": True}


def test_in_place_compaction_shows_each_message_once(tmp_path: Path) -> None:
    """Archived turns stay; a carried copy and a removed row do not repeat."""
    trajectory = parse(
        tmp_path,
        ["One.", "Two.", "Three."],
        {
            "id": SESSION_ID,
            "messages": [
                row(user("One."), 1, "u1", active=0, compacted=1),
                row(agent("Did one."), 2, "a1", active=0, compacted=1),
                row(user("Undone."), 3, "u2", active=0),
                row(user("Two."), 4, "u3", active=0),
                row(agent("Did two."), 5, "a3", active=0),
                # The new generation: the protected head, the summary carried by the
                # first tail message, and the rest of the tail.
                row(user("One."), 1, "u1", active=1),
                row(summary(user("Context: Summary. Two.")), 4, "u3", active=1),
                row(agent("Did two."), 5, "a3", active=1),
                row(user("Three."), 6, "u4", active=1),
            ],
        },
    )

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "One."),
        ("agent", "Did one."),
        ("system", "Context: Summary. Two."),
        ("user", "Two."),
        ("agent", "Did two."),
        ("user", "Three."),
    ]
    assert not trajectory.extra


def test_without_an_export_each_turn_comes_from_its_events(tmp_path: Path) -> None:
    record(
        tmp_path,
        "Go.",
        stdout=events(
            {"type": "system", "subtype": "init"},
            {
                "type": "tool_use",
                "name": "terminal",
                "tool_call_id": "t1",
                "input": {"command": "ls"},
            },
            {"type": "tool_result", "name": "terminal", "tool_call_id": "t1", "output": "a"},
            {"type": "text", "text": "Do"},
            {"type": "text", "text": "ne"},
            result(tokens={"input": 22, "output": 14, "cache_read": 0}),
        ),
    )
    trajectory = HermesHarness().parse_trajectory(recorded_context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Go."),
        ("agent", ""),
        ("agent", "Done"),
    ]
    assert trajectory.steps[1].observation.results[0].content == "a"  # type: ignore[union-attr]
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 22
    assert trajectory.extra["ale"]["parse_issues"][0]["reason"] == "native_session_unavailable"  # type: ignore[index]


def test_a_launch_and_its_resume_convert_in_native_order(tmp_path: Path) -> None:
    prompts = ["Write the file.", "Write it again."]
    messages: list[dict[str, object]] = []
    for turn, prompt in enumerate(prompts, 1):
        messages += [
            user(prompt),
            agent("", call(f"call-{turn}", "terminal", {"command": "ls"})),
            tool(f"call-{turn}", "ok"),
            agent("Done."),
        ]
    trajectory = parse(
        tmp_path,
        prompts,
        # Hermes stamps each message, which tells the two turns' equal replies apart.
        {
            "id": SESSION_ID,
            "messages": [{**item, "timestamp": at} for at, item in enumerate(messages)],
        },
        # Each result counts the tokens of the session so far.
        stdout=[events(result(tokens={"input": 10 * turn})) for turn in (1, 2)],
    )

    assert trajectory.session_id == SESSION_ID
    assert not trajectory.extra
    assert [(step.source, step.message) for step in trajectory.steps] == [
        step for prompt in prompts for step in [("user", prompt), ("agent", ""), ("agent", "Done.")]
    ]
    for turn, step in enumerate(trajectory.steps[1::3], 1):
        (tool_call,) = step.tool_calls or ()
        assert (tool_call.tool_call_id, tool_call.function_name) == (f"call-{turn}", "terminal")
        (output,) = step.observation.results  # type: ignore[union-attr]
        assert (output.source_call_id, output.content) == (tool_call.tool_call_id, "ok")
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 20


def test_a_delegation_links_the_child_by_its_goal(tmp_path: Path) -> None:
    trajectory = parse(
        tmp_path,
        ["Delegate."],
        {
            "id": SESSION_ID,
            "messages": [
                user("Delegate."),
                agent("", call("c1", "terminal", {"command": "ls"})),
                tool("c1", "ok"),
                agent("", call("c2", "delegate_task", {"tasks": [{"goal": "Child."}]})),
                tool("c2", "done"),
            ],
        },
        child(
            "child",
            SESSION_ID,
            user("Child."),
            agent("", call("cc", "terminal", {"command": "ls"})),
            tool("cc", "ok"),
        ),
    )

    assert not trajectory.extra
    (embedded,) = trajectory.subagent_trajectories or ()
    assert embedded.session_id == "child"
    assert embedded.trajectory_id == "hermes-trajectory-subagent-child"
    assert refs(trajectory) == [embedded.trajectory_id]
    assert [(step.source, step.message) for step in embedded.steps] == [
        ("user", "Child."),
        ("agent", ""),
    ]
    (tool_call,) = embedded.steps[1].tool_calls or ()
    assert (tool_call.tool_call_id, tool_call.function_name) == ("cc", "terminal")
    assert embedded.steps[1].observation.results[0].content == "ok"  # type: ignore[union-attr]


def test_compactions_keep_each_call_once(tmp_path: Path) -> None:
    """Two in-place compactions: one folds its summary into the tool call it carries,
    the next stands alone in the middle of a turn."""
    trajectory = parse(
        tmp_path,
        ["One.", "Two."],
        {
            "id": SESSION_ID,
            "messages": [
                row(user("One."), 1, "u1", active=0, compacted=1),
                row(agent("", call("c1", "terminal", {})), 2, "a1", active=0, compacted=1),
                row(tool("c1", "ok"), 3, "t1", active=0, compacted=1),
                row(agent("Did one."), 4, "f1", active=0, compacted=1),
                row(user("Two."), 5, "u2", active=0, compacted=1),
                # Removed: the first compaction carries these forward.
                row(agent("", call("c2", "terminal", {})), 6, "a2", active=0),
                row(tool("c2", "ok"), 7, "t2", active=0),
                row(agent("Did two."), 8, "f2", active=0),
                # The first compaction: the head, then the summary in the carried call.
                row(user("One."), 1, "u1", active=0, compacted=1),
                row(
                    summary(agent("Summary one.", call("c2", "terminal", {}))),
                    9,
                    "a2",
                    active=0,
                    compacted=1,
                ),
                row(tool("c2", "ok"), 10, "t2", active=0, compacted=1),
                row(agent("Did two."), 11, "f2", active=0),
                # The second compaction: a summary of its own, then the tail it carries.
                row(summary(user("Summary two.")), 12, "s2", active=1),
                row(agent("Did two."), 11, "f2", active=1),
            ],
        },
    )

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "One."),
        ("agent", ""),
        ("agent", "Did one."),
        ("user", "Two."),
        ("system", "Summary one."),
        ("agent", ""),
        ("system", "Summary two."),
        ("agent", "Did two."),
    ]
    calls = [call.tool_call_id for step in trajectory.steps for call in step.tool_calls or ()]
    results = [
        result.source_call_id
        for step in trajectory.steps
        if step.observation
        for result in step.observation.results
    ]
    assert calls == results == ["c1", "c2"]
    assert not trajectory.extra


async def test_windows_runs_the_launcher_the_installer_publishes() -> None:
    class WindowsSandbox(FakeSandbox):
        async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            if list(argv)[:2] == ["python", "-c"]:
                from ale.core.sandbox import ExecResult

                return ExecResult(exit_code=0, stdout="C:/Windows/System32")
            return await super().exec(argv, **kwargs)

    command = await HermesHarness().prepare_command(
        "C:/Users/agent/.ale-harness/prompt.md",
        WindowsSandbox(),  # type: ignore[arg-type]
        session(home="C:/Users/agent", os=OperatingSystem.WINDOWS),
        native_session_id=None,
    )
    # gateway stop, chat and sessions export.
    assert command.script.count("C:/Users/agent/.hermes-ale/bin/hermes.exe ") == 3
    assert command.env["HERMES_HOME"] == "C:/Users/agent/.hermes-ale"

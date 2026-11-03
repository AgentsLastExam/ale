"""Each shipped CLI continues its own native session in the solver an earlier episode kept,
or in two Sandboxes booted at once from its snapshot.

The Tasks share one image. The first gives the agent a code word to remember and has it
start a log; the second, run with ``--continue-from``, asks for the word and another log
line. Only the agent's session holds the word and only the retained solver holds the log,
so the second score shows that both carried over. Real models through the real Gateway,
each run serving its own, as an operator's runs would.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from tests.support import edit_task_config

from .trajectory import LIVE, run_task

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]

CLAUDE = "claude-sonnet-5-5"
GPT = "gpt-6.1-terra"
#: Each shipped CLI, the model it runs and its Gateway overrides. ``None`` keeps the
#: preset's model. A CLI whose preset provider or model no key here reaches runs GPT;
#: grok-build then speaks Chat Completions, since it rejects unknown Responses events.
AGENTS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "antigravity-cli": (None, ()),
    "claude-code": (CLAUDE, ()),
    "codex-cli": (GPT, ()),
    "droid": (CLAUDE, ()),
    "forgecode": (CLAUDE, ()),
    "grok-build": (GPT, ('gateway.provider="openai"', 'gateway.dialect="openai-chat-completions"')),
    "hermes": (GPT, ()),
    "kimi-code": (GPT, ('gateway.provider="openai"',)),
    "muse-code": (GPT, ('gateway.provider="openai"',)),
    "openclaw-cli": (GPT, ()),
    "openhands-cli": (CLAUDE, ()),
    "pi-cli": (GPT, ()),
    "qwen-code": (GPT, ('gateway.provider="openai"',)),
    "zcode": (None, ()),
}
FIRST = (
    "Remember this code word: {word}. Keep it in mind only; a later request will ask you to "
    "write it to a file.\n"
    "Create /home/user/output/log.txt containing exactly one line: first\n"
)
SECOND = (
    "Append one line, second, to /home/user/output/log.txt. Then write the code word I "
    "gave you earlier, and nothing else, into /home/user/output/word.txt.\n"
)
VERIFY_FIRST = """\
from pathlib import Path
from ale_verify import write
output = Path("/home/user/output")
log = output / "log.txt"
lines = [line.strip() for line in log.read_text().splitlines()] if log.is_file() else []
names = sorted(path.name for path in output.iterdir())
write({"reward": float(names == ["log.txt"] and lines == ["first"])})
"""
VERIFY_SECOND = """\
from pathlib import Path
from ale_verify import write
output = Path("/home/user/output")
log, word = output / "log.txt", output / "word.txt"
lines = [line.strip() for line in log.read_text().splitlines()] if log.is_file() else []
said = word.read_text().strip() if word.is_file() else ""
scores = {{"log": float(lines == ["first", "second"]), "word": float(said == {word!r})}}
write({{"reward": sum(scores.values()) / 2, **scores}})
"""


def _task(root: Path, instruction: str, verify: str) -> Path:
    scaffold_task(root)
    (root / "instruction.md").write_text(instruction)
    edit_task_config(root, {"agent": {"timeout_sec": 900}})
    (root / "tests/verify.py").write_text(verify)
    return root


def _read(episode: Path, name: str) -> dict[str, Any]:
    return json.loads((episode / name).read_text())


async def _destroy_kept(runs: Path) -> None:
    provider = DockerProvider()
    retained = {item["handle"] for item in await provider.list_retained()}
    for result in runs.rglob("result.json"):
        for item in json.loads(result.read_text()).get("sandboxes", ()):
            if item.get("handle") in retained:
                await provider.destroy_retained(item["handle"])


@pytest.mark.parametrize("retention", ["keep", "snapshot"])
@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_a_cli_continues_its_session_from_its_retained_solver(
    tmp_path: Path, agent: str, retention: str
) -> None:
    model, gateway = AGENTS[agent]
    word = f"heron-{secrets.token_hex(3)}"
    first_task = _task(tmp_path / "codeword-first", FIRST.format(word=word), VERIFY_FIRST)
    # A kept solver is continued once; a snapshot by two forks at once.
    forks = range(1 if retention == "keep" else 2)
    second_tasks = [
        _task(tmp_path / f"codeword-second-{fork}", SECOND, VERIFY_SECOND.format(word=word))
        for fork in forks
    ]
    runs = tmp_path / "runs"

    def second(fork: int) -> Path:
        return run_task(
            second_tasks[fork],
            runs,
            run_id=f"second-{fork}",
            agent=agent,
            model=model,
            settings=gateway,
            options=("--continue-from", str(first)),
            timeout=1800,
        )

    try:
        first = run_task(
            first_task,
            runs,
            run_id="first",
            agent=agent,
            model=model,
            settings=(*gateway, f'sandbox_retention.solver="{retention}"'),
            timeout=1800,
        )
        started = _read(first, "result.json")
        assert started["status"] == "completed", started.get("failure")
        assert started["rewards"] == {"reward": 1.0}, _read(first, "verification.json")
        assert started["continuation"]["harness"] == agent

        with ThreadPoolExecutor(len(forks)) as pool:
            seconds = list(pool.map(second, forks))
        continued = [_read(episode, "result.json") for episode in seconds]
        for episode, result in zip(seconds, continued, strict=True):
            assert result["status"] == "completed", result.get("failure")
            assert result["rewards"] == {"reward": 1.0}, _read(episode, "verification.json")
    finally:
        asyncio.run(_destroy_kept(runs))

    before = _read(first, "trajectory.json")
    for episode, result in zip(seconds, continued, strict=True):
        # One native session across the episodes; each continuation records its source.
        assert result["continuation"] == started["continuation"]
        resumed = _read(episode, "lock.json")["resumed_from"]
        assert (resumed["episode_id"], resumed["retention"]) == (started["episode_id"], retention)
        assert [item["outcome"] for item in result["sandboxes"]] == ["destroyed"]

        # Each continuation's trajectory is only its own part of the session.
        after = _read(episode, "trajectory.json")
        assert after["schema_version"] == "ATIF-v1.8"
        assert after["session_id"] == before["session_id"]
        ale = after["extra"]["ale"]
        issues = [issue["reason"] for issue in ale.get("parse_issues", ())]
        assert "continuation_boundary_not_found" not in issues, ale["parse_issues"]
        assert ale["resumed_from"] == {
            "episode_id": started["episode_id"],
            "trajectory_id": before["trajectory_id"],
            "native_steps": len(before["steps"]),
        }
        users = [step["message"] for step in after["steps"] if step["source"] == "user"]
        assert any("code word I gave you earlier" in str(message) for message in users), users
        assert not any(
            word in json.dumps(step) for step in after["steps"] if step["source"] == "user"
        )

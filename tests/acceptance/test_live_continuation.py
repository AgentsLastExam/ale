"""Each shipped CLI continues its own native session in the solver an earlier episode kept.

Two Tasks share one image. The first gives the agent a code word to remember and has it
start a log; the second, run with ``--continue-from``, asks for the word and another log
line. Only the agent's session holds the word and only the kept Sandbox holds the log, so
the second score shows that both carried over. Real models through the real Gateway.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task

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
    manifest = root / "task.yaml"
    manifest.write_text(manifest.read_text().replace("agent: 300", "agent: 900"))
    (root / "verify/verify.py").write_text(verify)
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


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_a_cli_continues_its_session_in_the_kept_solver(tmp_path: Path, agent: str) -> None:
    model, gateway = AGENTS[agent]
    word = f"heron-{secrets.token_hex(3)}"
    first_task = _task(tmp_path / "codeword-first", FIRST.format(word=word), VERIFY_FIRST)
    second_task = _task(tmp_path / "codeword-second", SECOND, VERIFY_SECOND.format(word=word))
    runs = tmp_path / "runs"
    try:
        first = run_task(
            first_task,
            runs,
            run_id="first",
            agent=agent,
            model=model,
            settings=(*gateway, 'sandbox_retention.solver="keep"'),
            timeout=1800,
        )
        started = _read(first, "result.json")
        assert started["status"] == "completed", started.get("failure")
        assert started["rewards"] == {"reward": 1.0}, _read(first, "verification.json")
        assert started["continuation"]["harness"] == agent

        second = run_task(
            second_task,
            runs,
            run_id="second",
            agent=agent,
            model=model,
            settings=gateway,
            options=("--continue-from", str(first)),
            timeout=1800,
        )
        continued = _read(second, "result.json")
        assert continued["status"] == "completed", continued.get("failure")
        assert continued["rewards"] == {"reward": 1.0}, _read(second, "verification.json")
    finally:
        asyncio.run(_destroy_kept(runs))

    # One native session across both episodes; the second records where it came from.
    assert continued["continuation"] == started["continuation"]
    assert _read(second, "lock.json")["resumed_from"]["episode_id"] == started["episode_id"]
    assert [item["outcome"] for item in continued["sandboxes"]] == ["destroyed"]

    # The second trajectory is only its own part of the session, linked to the first.
    before, after = _read(first, "trajectory.json"), _read(second, "trajectory.json")
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
    assert not any(word in json.dumps(step) for step in after["steps"] if step["source"] == "user")

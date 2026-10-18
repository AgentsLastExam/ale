from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path

import pytest
from aiohttp import web

from ale.core.config import LLMJudgeConfig, VerificationConfig
from ale.core.trace import read_jsonl
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import load_tasks
from ale_verify import ScoredChoice, _llm
from tests.support import CONTAINER_BASE, provider_registry

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_direct_llm_call_uses_provider_without_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    async def response(request: web.Request) -> web.Response:
        requests.append(await request.json())
        return web.json_response(
            {
                "id": "response-1",
                "output_text": '{"choice":"yes","reasoning":"Correct."}',
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", response)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    monkeypatch.setenv("JUDGE_KEY", "secret")
    try:
        choice, _, invocation = await asyncio.to_thread(
            _llm.run,
            invocation_id="judge-1",
            name="correctness",
            prompt="Judge.",
            rubric={
                "no": ScoredChoice(0, "Wrong."),
                "yes": ScoredChoice(1, "Correct."),
            },
            evidence=(),
            config={
                "model": "gpt-5-mini",
                "reasoning_effort": "medium",
                "base_url": f"http://127.0.0.1:{port}",
                "api_key_env": "JUDGE_KEY",
            },
        )
    finally:
        await runner.cleanup()

    assert choice == "yes"
    assert requests[0]["model"] == "gpt-5-mini"
    assert invocation.attempts[0].usage == {"input_tokens": 2, "output_tokens": 1}
    assert "secret" not in json.dumps(invocation, default=str)


def judged_task(root: Path) -> Path:
    task = root / "tasks" / "judged"
    for stage in ("image", "setup", "verify", "oracle"):
        (task / stage).mkdir(parents=True)
    (task / "task.yaml").write_text(
        textwrap.dedent("""
        spec_type: core/v1
        name: judged
        image: {kind: container}
        resources: {cpus: 1, memory_mb: 512}
        network: {mode: block}
        timeouts: {setup: 60, agent: 60, verify: 120}
        artifacts: [/home/user/output]
        """).strip()
    )
    (task / "instruction.md").write_text("Write blue to /home/user/output/answer.txt\n")
    (task / "image" / "Dockerfile").write_text(
        f"FROM {CONTAINER_BASE}\n"
        "RUN mkdir -p /home/user/output && chown -R user:user /home/user/output\n"
    )
    (task / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p /home/user/output\n"
        'test -z "${JUDGE_KEY-}"\n'
        "printf absent > /home/user/output/setup-key\n"
    )
    (task / "oracle" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf blue > /home/user/output/answer.txt\n"
    )
    (task / "verify" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
    )
    (task / "verify" / "verify.py").write_text(
        "import os\n"
        "from ale_verify import CheckResult, Verification, checks\n"
        "v = Verification()\n"
        "v.check('credential_scope', checks.text_equals('/home/user/output/setup-key', 'absent'))\n"
        "v.check('key_withheld', CheckResult(0.0 if 'JUDGE_KEY' in os.environ else 1.0))\n"
        "v.judge(\n"
        "    'llm', 'correctness', prompt='Judge the answer.',\n"
        "    rubric={\n"
        "        'no': {'score': 0.0, 'description': 'Incorrect.'},\n"
        "        'yes': {'score': 1.0, 'description': 'Correct.'},\n"
        "    },\n"
        "    files=['/home/user/output/answer.txt'],\n"
        ")\n"
        "v.write()\n"
    )
    for script in task.glob("*/run.sh"):
        script.chmod(0o755)
    return task


async def run_judged_task(tmp_path: Path, provider, *, path: str) -> None:  # type: ignore[no-untyped-def]
    """A Judge in the verifier reaches a Host endpoint through the Gateway, keyless."""
    secret = "sandbox-direct-secret"
    seen: list[str] = []
    verdict = '{"choice":"yes","reasoning":"The answer is correct."}'

    async def respond(request: web.Request) -> web.Response:
        seen.append(request.headers.get("x-api-key") or request.headers.get("authorization", ""))
        if request.path.endswith("/chat/completions"):
            payload: dict[str, object] = {
                "id": "mock-chat-1",
                "choices": [{"message": {"role": "assistant", "content": verdict}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }
        elif request.path.endswith("/messages"):
            payload = {
                "id": "mock-message-1",
                "content": [{"type": "text", "text": verdict}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        else:
            payload = {
                "id": "mock-response-1",
                "output_text": verdict,
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        return web.json_response(payload)

    app = web.Application()
    for route in ("/v1/responses", "/v1/chat/completions", "/v1/messages"):
        app.router.add_post(route, respond)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    upstream = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]
    os.environ["JUDGE_KEY"] = secret
    gateway = Gateway()
    gateway_url = await gateway.start()
    task = load_tasks(judged_task(tmp_path / "repo"))[0]
    try:
        result = await run_episode(
            task,
            StandardEnvironment(OracleHarness()),
            provider_registry(provider),
            run_dir=tmp_path / "runs",
            gateway=gateway,
            gateway_url=gateway_url,
            verification_config=VerificationConfig(
                llm=LLMJudgeConfig(
                    model="gpt-5-mini",
                    reasoning_effort="medium",
                    base_url=upstream + path,
                    api_key_env="JUDGE_KEY",
                )
            ),
        )
    finally:
        os.environ.pop("JUDGE_KEY", None)
        await gateway.stop()
        await runner.cleanup()

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {
        "credential_scope": 1.0,
        "key_withheld": 1.0,
        "correctness": 1.0,
        "overall": 1.0,
    }
    assert seen and all(secret in header for header in seen)
    record = json.loads((result.run_dir / "verification.json").read_text())
    invocation = record["judge_invocations"][0]
    assert invocation["attempts"][0]["endpoint_identity"].endswith(path)
    assert invocation["attempts"][0]["usage"] in (
        {"input_tokens": 2, "output_tokens": 1},
        {"prompt_tokens": 2, "completion_tokens": 1},
    )
    calls = [
        record
        for record in read_jsonl(result.run_dir / "trace.transport.jsonl").records
        if record["kind"] == "call"
    ]
    assert [call["purpose"] for call in calls] == ["judge"]
    for file in result.run_dir.rglob("*"):
        if file.is_file():
            assert secret.encode() not in file.read_bytes()


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_responses_judge_runs_through_the_gateway(tmp_path: Path) -> None:
    await run_judged_task(tmp_path, DockerProvider(), path="/v1/responses")


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_chat_completions_judge_runs_through_the_gateway(tmp_path: Path) -> None:
    await run_judged_task(tmp_path, DockerProvider(), path="/v1/chat/completions")


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_anthropic_messages_judge_runs_through_the_gateway(tmp_path: Path) -> None:
    await run_judged_task(tmp_path, DockerProvider(), path="/v1/messages")

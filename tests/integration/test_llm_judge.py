from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest
from aiohttp import web

from ale.core.config import LLMJudgeConfig, VerificationConfig
from ale.core.trace import read_jsonl
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import load_tasks
from ale_verify import _llm
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
        response_data, invocation = await asyncio.to_thread(
            _llm.run,
            invocation_id="judge-1",
            name="correctness",
            prompt="Judge.",
            output_schema={"type": "object"},
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

    assert response_data["choice"] == "yes"
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
        'test -z "${ALE_OPENAI_API_KEY-}${ALE_ANTHROPIC_API_KEY-}"\n'
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
        "from ale_verify import checks, judge, write\n"
        "v = {}\n"
        "v['credential_scope'] = checks.text_equals('/home/user/outpu"
        "t/setup-key', 'absent')['score']\n"
        "v['key_withheld'] = 0.0 if {'ALE_OPENAI_API_KEY', 'ALE_ANTHROPIC_A"
        "PI_KEY'} & set(os.environ) else 1.0\n"
        "v['correctness'] = {'no': 0.0, 'yes': 1.0}[judge('llm', 'cor"
        "rectness', prompt='Judge the answer.', files=['/home/user/ou"
        "tput/answer.txt'], output_schema={'type': 'object', 'propert"
        "ies': {'choice': {'enum': ['no', 'yes']}, 'reasoning': {'typ"
        "e': 'string'}}, 'required': ['choice', 'reasoning']})['choic"
        "e']]\n"
        'write({"overall": sum(v.values()) / len(v), **v})\n'
    )
    for script in task.glob("*/run.sh"):
        script.chmod(0o755)
    return task


#: The model provider and dialect each mock endpoint path stands for.
_ROUTES = {
    "/v1/responses": ("openai", "openai-responses"),
    "/v1/chat/completions": ("openai", "openai-chat-completions"),
    "/v1/messages": ("anthropic", "anthropic"),
}


async def run_judged_task(  # type: ignore[no-untyped-def]
    tmp_path: Path, provider, monkeypatch: pytest.MonkeyPatch, *, path: str, crash: bool = False
) -> None:
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
    model_provider, dialect = _ROUTES[path]
    monkeypatch.setenv(f"ALE_{model_provider.upper()}_API_KEY", secret)
    monkeypatch.setenv(f"ALE_{model_provider.upper()}_BASE_URL", upstream)
    gateway = Gateway()
    gateway_url = await gateway.start()
    task_root = judged_task(tmp_path / "repo")
    if crash:
        script = task_root / "verify/verify.py"
        script.write_text(script.read_text() + "raise RuntimeError('after Judge and write')\n")
    task = load_tasks(task_root)[0]
    try:
        result = await run_episode(
            task,
            EpisodeProtocol(OracleHarness()),
            provider_registry(provider),
            run_dir=tmp_path / "runs",
            gateway=gateway,
            gateway_url=gateway_url,
            verification_config=VerificationConfig(
                llm=LLMJudgeConfig(
                    provider=model_provider,  # type: ignore[arg-type]
                    dialect=dialect,  # type: ignore[arg-type]
                    model="gpt-5-mini",
                    reasoning_effort="medium",
                )
            ),
        )
    finally:
        await gateway.stop()
        await runner.cleanup()

    if crash:
        assert result.verdict.status is Status.TASK_ERROR
        assert result.verdict.rewards is None
        assert not (result.run_dir / "verification.json").exists()
    else:
        assert result.verdict.status is Status.COMPLETED, result.verdict.failure
        assert result.verdict.rewards == {"overall": 1.0}
        assert json.loads((result.run_dir / "verification.json").read_text())["overall"] == 1
    assert seen and all(secret in header for header in seen)
    invocation = next(
        json.loads(line)["invocation"]
        for line in (result.run_dir / "llm-judge.jsonl").read_text().splitlines()
        if json.loads(line)["event"] == "completed"
    )
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
async def test_responses_judge_runs_through_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_judged_task(tmp_path, DockerProvider(), monkeypatch, path="/v1/responses")


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_chat_completions_judge_runs_through_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_judged_task(tmp_path, DockerProvider(), monkeypatch, path="/v1/chat/completions")


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_anthropic_messages_judge_runs_through_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_judged_task(tmp_path, DockerProvider(), monkeypatch, path="/v1/messages")


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_judge_logs_survive_a_later_task_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_judged_task(tmp_path, DockerProvider(), monkeypatch, path="/v1/responses", crash=True)

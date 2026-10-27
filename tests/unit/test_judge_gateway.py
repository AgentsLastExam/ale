"""Judges reach their own endpoint through the Gateway, with a key the sandbox never sees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from ale.core.config import VerificationConfig
from ale.core.trace import read_jsonl
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.recording import EpisodeRecording
from ale.run.verification import judge_endpoints, open_judge_sessions, verifier_judge_config

pytestmark = pytest.mark.unit

JUDGE_KEY = "sk-judge-real-credential"


@pytest.mark.asyncio
async def test_a_judge_session_reaches_its_own_endpoint_and_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    async def responses(request: web.Request) -> web.Response:
        payload = await request.json()
        seen.append((request.headers.get("authorization", ""), payload["model"]))
        return web.json_response(
            {
                "id": f"resp-{len(seen)}",
                "status": "completed",
                "output_text": '{"choice":"yes","reasoning":"ok"}',
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }
        )

    app = web.Application()
    app.router.add_post("/judge/v1/responses", responses)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    upstream = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/judge"

    monkeypatch.setenv("ALE_OPENAI_API_KEY", JUDGE_KEY)
    monkeypatch.setenv("ALE_OPENAI_BASE_URL", upstream)
    config = VerificationConfig.model_validate(
        {
            "llm": {
                "provider": "openai",
                "dialect": "openai-responses",
                "model": "judge-model",
                "reasoning_effort": "medium",
            }
        }
    )
    assert judge_endpoints(config) == {"llm": f"{upstream}/v1/responses"}
    gateway = Gateway(host="127.0.0.1")
    url = await gateway.start()
    recording = EpisodeRecording(tmp_path)
    try:
        sessions = open_judge_sessions(gateway, config, episode_id="e1", trace=recording.transport)
        token = sessions["llm"].token
        payload, env = verifier_judge_config(config, {"llm": token}, url)
        assert payload["llm"] == {
            "model": "judge-model",
            "reasoning_effort": "medium",
            "base_url": f"{url}/v1/responses",
            "api_key_env": "ALE_LLM_JUDGE_TOKEN",
        }
        assert env == {"ALE_LLM_JUDGE_TOKEN": token}
        assert JUDGE_KEY not in json.dumps(payload)

        body = {"model": "anything", "input": "judge this"}
        async with ClientSession() as client:
            for _ in range(2):
                async with client.post(
                    f"{url}/v1/responses",
                    json=body,
                    headers={"authorization": f"Bearer {token}"},
                ) as response:
                    assert response.status == 200
            async with client.post(
                f"{url}/v1/messages", json=body, headers={"x-api-key": token}
            ) as wrong:
                assert wrong.status == 404
    finally:
        for opened in sessions.values():
            gateway.close_session(opened)
        await gateway.stop()
        await runner.cleanup()

    # Two identical requests both reached the Judge: a retried verdict samples again.
    assert seen == [(f"Bearer {JUDGE_KEY}", "judge-model")] * 2
    calls = [
        r for r in read_jsonl(tmp_path / "trace.transport.jsonl").records if r["kind"] == "call"
    ]
    assert [call["purpose"] for call in calls] == ["judge", "judge"]


def test_a_judge_without_its_key_gets_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALE_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ALE_ANTHROPIC_BASE_URL", raising=False)
    config = VerificationConfig.model_validate(
        {
            "agent": {
                "provider": "anthropic",
                "adapter": "claude-code",
                "version": "2.0.0",
                "model": "claude-x",
                "reasoning_effort": "high",
            }
        }
    )
    assert judge_endpoints(config) == {"agent": "https://api.anthropic.com"}
    assert open_judge_sessions(Gateway(), config, episode_id="e", trace=None) == {}  # type: ignore[arg-type]
    payload, env = verifier_judge_config(config, {"agent": "tok"}, "http://gw:1")
    assert payload["agent"]["base_url"] == "http://gw:1"
    assert env == {"ALE_AGENT_JUDGE_TOKEN": "tok"}
    assert GatewaySession(episode_id="e", model="m").purpose == "agent"

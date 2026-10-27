from __future__ import annotations

import json

import pytest
from aiohttp import ClientSession

from ale.core.trace import read_jsonl
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.recording import EpisodeRecording
from ale.run.secrets import provider_credentials

from .trajectory import LIVE, MODELS

pytestmark = [pytest.mark.needs_llm, LIVE]


@pytest.mark.parametrize("provider", ["xai", "openai"])
async def test_real_chat_endpoint_through_gateway(tmp_path, provider: str) -> None:
    model = MODELS[provider]
    key, upstream = provider_credentials(provider)
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(
        api_key=key,
        upstream=upstream,
        dialect="openai-chat-completions",
        host="127.0.0.1",
    )
    await gateway.start()
    session = gateway.open_session(
        GatewaySession(episode_id=f"live-chat-{provider}", model=model),
        recording.transport,
    )
    try:
        async with (
            ClientSession() as client,
            client.post(
                f"{gateway.base_url}/v1/chat/completions",
                json={
                    "model": "must-be-overridden",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Reply exactly CHAT-GATEWAY-OK.",
                        }
                    ],
                    "max_completion_tokens": 256,
                },
                headers={"authorization": f"Bearer {session.token}"},
            ) as response,
        ):
            raw = await response.read()
            assert response.status == 200, raw.decode("utf-8", "replace")
            payload = json.loads(raw)
    finally:
        gateway.close_session(session)
        await gateway.stop()

    assert payload["model"].startswith(model)
    assert payload["id"]
    assert payload["usage"]["prompt_tokens"] > 0
    assert payload["usage"]["completion_tokens"] > 0
    assert payload["choices"][0]["message"]["content"].strip() == "CHAT-GATEWAY-OK"
    (record,) = read_jsonl(recording.transport.path).records
    assert record["model"] == model
    assert record["provider_response_id"] == payload["id"]
    assert record["input_tokens"] == payload["usage"]["prompt_tokens"]
    assert record["output_tokens"] == payload["usage"]["completion_tokens"]

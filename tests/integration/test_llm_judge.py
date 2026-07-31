from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path

import pytest
from aiohttp import web

from ale.core.config import LLMJudgeConfig, VerificationConfig
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.tasksets.manifest import ManifestTaskset
from ale_verify import ScoredChoice, _llm

pytestmark = pytest.mark.integration

IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"
QEMU_IMAGE = Path(
    os.environ.get(
        "ALE_QEMU_IMAGE",
        Path.home() / ".cache/ale/images/ale-ubuntu-desktop.qcow2",
    )
)


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
            reference=None,
            trajectory=None,
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


def direct_llm_task(root: Path) -> Path:
    task = root / "tasks" / "direct-llm"
    for stage in ("setup", "verify", "oracle"):
        (task / stage).mkdir(parents=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    (task / "task.yaml").write_text(
        textwrap.dedent(f"""
        image: {IMAGE}
        resources: {{ cpus: 1, memory_mb: 512 }}
        network: {{ mode: block }}
        timeouts: {{ setup: 60, agent: 60, verify: 120 }}
        artifacts: [/home/user/output]
        """).strip()
    )
    (task / "instruction.md").write_text("Write blue to /home/user/output/answer.txt\n")
    (task / "setup" / "server.py").write_text(
        "import json\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_POST(self):\n"
        "        length = int(self.headers.get('content-length', '0'))\n"
        "        self.rfile.read(length)\n"
        '        verdict = \'{"choice":"yes","reasoning":"The answer is correct."}\'\n'
        "        if self.path.endswith('/chat/completions'):\n"
        "            payload = {'id':'mock-chat-1','choices':[{'message':"
        "{'role':'assistant','content':verdict}}],"
        "'usage':{'prompt_tokens':2,'completion_tokens':1}}\n"
        "        elif self.path.endswith('/messages'):\n"
        "            payload = {'id':'mock-message-1','content':["
        "{'type':'text','text':verdict}],"
        "'usage':{'input_tokens':2,'output_tokens':1}}\n"
        "        else:\n"
        "            payload = {'id':'mock-response-1','output_text':verdict,"
        "'usage':{'input_tokens':2,'output_tokens':1}}\n"
        "        body = json.dumps(payload).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('content-type', 'application/json')\n"
        "        self.send_header('content-length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *_args): pass\n"
        "HTTPServer(('127.0.0.1', 18765), Handler).handle_request()\n"
    )
    (task / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p /home/user/output\n"
        'test -z "${JUDGE_KEY-}"\n'
        "printf absent > /home/user/output/setup-key\n"
        'nohup python3 "$(dirname "$0")/server.py" >/tmp/mock-llm.log 2>&1 &\n'
        "sleep 1\n"
    )
    (task / "oracle" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf blue > /home/user/output/answer.txt\n"
    )
    (task / "verify" / "run.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nexec python3 "$(dirname "$0")/check.py"\n'
    )
    (task / "verify" / "check.py").write_text(
        "from ale_verify import Verification, checks\n"
        "v = Verification()\n"
        "v.check('credential_scope', checks.text_equals('/home/user/output/setup-key', 'absent'))\n"
        "v.judge(\n"
        "    'llm', 'correctness', prompt='Judge the answer.',\n"
        "    rubric={\n"
        "        'no': {'score': 0.0, 'description': 'Incorrect.'},\n"
        "        'yes': {'score': 1.0, 'description': 'Correct.'},\n"
        "    },\n"
        "    files=['/home/user/output/answer.txt'],\n"
        ")\n"
        "v.aggregate('overall')\n"
        "v.write()\n"
    )
    for script in task.glob("*/run.sh"):
        script.chmod(0o755)
    return task


async def run_direct_llm_task(
    tmp_path: Path,
    provider,  # type: ignore[no-untyped-def]
    *,
    base_url: str = "http://127.0.0.1:18765",
) -> None:
    secret = "sandbox-direct-secret"
    os.environ["JUDGE_KEY"] = secret
    task_root = direct_llm_task(tmp_path / "repo")
    task = next(iter(ManifestTaskset(task_root).load()))
    try:
        result = await run_episode(
            task,
            StandardEnvironment(OracleHarness()),
            provider,
            run_dir=tmp_path / "runs",
            verification_config=VerificationConfig(
                llm=LLMJudgeConfig(
                    model="gpt-5-mini",
                    reasoning_effort="medium",
                    base_url=base_url,
                    api_key_env="JUDGE_KEY",
                )
            ),
        )
    finally:
        os.environ.pop("JUDGE_KEY", None)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {
        "credential_scope": 1.0,
        "correctness": 1.0,
        "overall": 1.0,
    }
    record = json.loads((result.run_dir / "verification.json").read_text())
    invocation = record["judge_invocations"][0]
    assert invocation["attempts"][0]["endpoint_identity"] == base_url
    assert invocation["attempts"][0]["usage"] in (
        {"input_tokens": 2, "output_tokens": 1},
        {"prompt_tokens": 2, "completion_tokens": 1},
    )
    assert invocation["id"] not in (result.run_dir / "trace.transport.jsonl").read_text()
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_direct_llm_judge_runs_inside_docker_sandbox(tmp_path: Path) -> None:
    await run_direct_llm_task(tmp_path, DockerProvider())


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_direct_chat_completions_judge_runs_inside_docker_sandbox(
    tmp_path: Path,
) -> None:
    await run_direct_llm_task(
        tmp_path,
        DockerProvider(),
        base_url="http://127.0.0.1:18765/v1/chat/completions",
    )


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_direct_anthropic_messages_judge_runs_inside_docker_sandbox(
    tmp_path: Path,
) -> None:
    await run_direct_llm_task(
        tmp_path,
        DockerProvider(),
        base_url="http://127.0.0.1:18765/v1/messages",
    )


@pytest.mark.needs_kvm
@pytest.mark.skipif(not QEMU_IMAGE.is_file(), reason=f"no QEMU image at {QEMU_IMAGE}")
@pytest.mark.asyncio
async def test_direct_llm_judge_runs_inside_qemu_sandbox(tmp_path: Path) -> None:
    await run_direct_llm_task(tmp_path, QemuProvider(image=QEMU_IMAGE))

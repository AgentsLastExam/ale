"""Each CLI's provider-side web tools, checked end to end with the Task network closed and open.

Some agent programs have their model provider search the web on its own side, such as
Claude Code's WebSearch or Codex's hosted ``web_search``; that access never passes the
Sandbox's network policy. ``SEARCHES`` says how a model attempts a search through each
CLI. All cases share one Sandbox (``tests.support.cli_sandbox``) behind one Gateway: each
CLI is installed once, and a case launches it with a scripted model twice, as an episode
of a Task whose network is ``open`` and then of one whose network is closed.

A case checks what the Gateway saw:

- with the network closed, no request asks for a provider-side network tool, by the
  Gateway's own reading of its dialect, and the Gateway refused none: the CLI did not
  offer one, or its search failed before it asked;
- with the network open, a CLI with such a search sends at least one request that asks
  for it, and the Gateway forwards it; a CLI without one asks for none.

An agent can also skip its CLI and write a request itself with its episode token. For
each dialect, ``DIRECT`` is such a request, sent from inside the Sandbox: the Gateway
refuses it with the network closed and forwards it with the network open.

The Sandbox itself stays sealed in every run: what is checked is the Harness's
configuration and the Gateway's policy, both of which follow the Task's network mode.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from ale.core.harness import EffectiveAgentResources, HarnessSession
from ale.core.sandbox import Identity, Sandbox
from ale.core.trace import read_jsonl
from ale.run.gateway import anthropic, gemini, openai_chat_completions, openai_responses
from ale.run.gateway.server import REQUEST_PATHS, Gateway
from ale.run.gateway.session import GatewaySession, Upstream
from ale.run.harnesses._npm import agent_python
from ale.run.harnesses.antigravity_cli import AntigravityCliHarness
from ale.run.harnesses.base import CliHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.droid import DroidHarness
from ale.run.harnesses.forgecode import ForgecodeHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.hermes import HermesHarness
from ale.run.harnesses.kimi_code import KimiCodeHarness
from ale.run.harnesses.muse_code import MuseCodeHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness
from ale.run.harnesses.openhands_cli import OpenHandsCliHarness
from ale.run.harnesses.pi_cli import PiCliHarness
from ale.run.harnesses.qwen_code import QwenCodeHarness
from ale.run.harnesses.zcode import ZCodeHarness
from ale.run.recording import EpisodeRecording
from tests.integration.cli_desktop import MODELS, cli_harnesses
from tests.integration.scripted_model import Call, ScriptedModel
from tests.support import cli_os, cli_sandbox

PROMPT = "Search the web for the ALE web tools proof, then answer."
QUERY = "ale-web-tools-proof"
#: Matches no tool, so a model with no script answers at once.
_NO_TOOL = r"(?!)"
#: The Gateway's reading of each dialect: the provider-side network tools a request asks for.
WEB_TOOLS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "anthropic": anthropic.web_tools,
    "gemini": gemini.web_tools,
    "openai-chat-completions": openai_chat_completions.web_tools,
    "openai-responses": openai_responses.web_tools,
}


@dataclass(frozen=True)
class Search:
    """How a model attempts a provider-side search through one CLI, if it can."""

    #: The Gateway dialect the CLI speaks by default, which the case runs in.
    dialect: str
    #: Whether the CLI has a provider-side search, which it asks for when the network is open.
    searches: bool = False
    #: The native call that attempts the search; ``None`` where the CLI asks for the tool in
    #: its requests without the model calling anything, or has no search.
    call: Call | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    dotted_namespaces: bool = False


#: How a model attempts a provider-side search, for every CLI Harness.
SEARCHES: dict[type[CliHarness], Search] = {
    # WebSearch is a request of its own carrying Anthropic's web_search server tool.
    ClaudeCodeHarness: Search("anthropic", True, ("WebSearch", {"query": QUERY})),
    # Codex offers OpenAI's hosted web_search in every request.
    CodexCliHarness: Search("openai-responses", True),
    # web_search is a request of its own carrying xAI's hosted web_search.
    GrokBuildHarness: Search("openai-responses", True, ("web_search", {"query": QUERY})),
    # In hosted mode every request offers Meta's hosted search. Reminder agents are off:
    # their own model calls would race the scripted loop.
    MuseCodeHarness: Search(
        "openai-responses",
        True,
        settings={"reminders": False},
        model="muse-spark-1.3",
        dotted_namespaces=True,
    ),
    # search_web is a request of its own carrying Google Search grounding.
    AntigravityCliHarness: Search("gemini", True, ("search_web", {"query": QUERY})),
    # WebSearch is a request of its own carrying the web_search server tool, in Messages.
    ZCodeHarness: Search("anthropic", True, ("WebSearch", {"query": QUERY})),
    # Their hosted search answers only for their vendors' own URLs, never the Gateway's.
    OpenClawCliHarness: Search("openai-responses"),
    QwenCodeHarness: Search("openai-chat-completions"),
    # No provider-side network tool under ALE's configuration.
    DroidHarness: Search("anthropic"),
    ForgecodeHarness: Search("anthropic"),
    HermesHarness: Search("openai-chat-completions"),
    KimiCodeHarness: Search("openai-chat-completions"),
    OpenHandsCliHarness: Search("anthropic"),
    PiCliHarness: Search("openai-chat-completions"),
}


@dataclass(frozen=True)
class Case:
    harness: Callable[[], CliHarness]
    search: Search
    model: str


def _cases() -> dict[str, Case]:
    cases = {}
    for harness in cli_harnesses():
        search = SEARCHES.get(harness)
        if search is None:
            continue
        cases[harness.name] = Case(
            partial(harness, settings=search.settings),
            search,
            search.model or MODELS[search.dialect],
        )
    return cases


CASES = _cases()


# --- the shared Sandbox ---


@dataclass(frozen=True)
class Bench:
    """The Sandbox every case runs in, and the Gateway and model behind it."""

    sandbox: Sandbox
    home: str
    gateway: Gateway
    model: ScriptedModel
    upstream: str


@asynccontextmanager
async def bench(work: Path, harnesses: Iterable[CliHarness]) -> AsyncIterator[Bench]:
    """A sealed Sandbox with each of ``harnesses`` installed once, behind one Gateway."""
    model = ScriptedModel(tool_pattern=_NO_TOOL)
    upstream = await model.start()
    # A Gemini request names its model and method in its path, which only the Gateway's
    # own upstream keeps; every other dialect reaches the model as a session's upstream.
    gateway = Gateway(api_key="host-only", upstream=upstream, dialect="gemini", host="0.0.0.0")
    await gateway.start()
    try:
        async with cli_sandbox("cli-web-tools", gateway.base_url, work) as (sandbox, home):
            await sandbox.open_egress()
            for harness in harnesses:
                await harness.install(sandbox)
            await sandbox.close_egress()
            yield Bench(sandbox, home, gateway, model, upstream)
    finally:
        await gateway.stop()
        await model.stop()


# --- one case ---


@dataclass(frozen=True)
class Seen:
    """What the Gateway saw of one launch."""

    #: The provider-side network tools each forwarded request asked for, where it asked.
    asked: list[list[str]]
    #: How many requests the Gateway refused for asking for one.
    refused: int
    exit_code: int


async def attempt_search(bench: Bench, case: Case, *, open_network: bool, work: Path) -> Seen:
    """Launch the case's CLI once in ``bench``, as an episode of a Task whose network is
    ``open`` or closed, and say what the Gateway saw."""
    harness = case.harness()
    dialect = case.search.dialect
    bench.model.script = (case.search.call,) if case.search.call else ()
    bench.model.dotted_namespaces = case.search.dotted_namespaces
    bench.model.requests.clear()
    episode_id = f"web-tools-{harness.name}-{'open' if open_network else 'closed'}"
    recording = EpisodeRecording(work)
    upstream = (
        None
        if dialect == "gemini"
        else Upstream(
            dialect=dialect, endpoint=bench.upstream + REQUEST_PATHS[dialect], api_key="host-only"
        )
    )
    gateway_session = bench.gateway.open_session(
        GatewaySession(
            episode_id=episode_id,
            model=case.model,
            upstream=upstream,
            provider_web_tools=open_network,
        ),
        recording.transport,
    )
    try:
        # As a fresh Sandbox would: nothing an earlier launch of this CLI left remains.
        await bench.sandbox.exec(
            ["rm", "-rf", "--", *(f"{bench.home}/{path}" for path in harness.logs)],
            identity=Identity.AGENT,
        )
        session = HarnessSession(
            episode_id=episode_id,
            gateway_url=bench.sandbox.gateway_url or bench.gateway.base_url,
            token=gateway_session.token,
            model=case.model,
            sandbox_id=bench.sandbox.sandbox_id,
            home=bench.home,
            os=cli_os(),
            provider_web_tools=open_network,
        )
        resources = EffectiveAgentResources()
        harness.validate_resources(resources)
        await harness.install_resources(bench.sandbox, session, resources)
        run = await harness.launch(PROMPT, bench.sandbox, session, timeout_sec=600)
    finally:
        bench.gateway.close_session(gateway_session)

    calls = [
        record
        for record in read_jsonl(recording.directory / "trace.transport.jsonl").records
        if record["kind"] == "call"
    ]
    return Seen(
        asked=[WEB_TOOLS[dialect](body) for _, body in bench.model.requests],
        refused=sum(call.get("refusal_limit") == "web_tools" for call in calls),
        exit_code=run.exit_code,
    )


# --- the agent's own request ---

#: Per dialect, a request for the provider's web search an agent could write itself.
DIRECT: dict[str, tuple[str, dict[str, Any]]] = {
    "anthropic": (
        "/v1/messages",
        {
            "model": "any",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": QUERY}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        },
    ),
    "openai-chat-completions": (
        "/v1/chat/completions",
        {
            "model": "any",
            "messages": [{"role": "user", "content": QUERY}],
            "web_search_options": {},
        },
    ),
    "openai-responses": (
        "/v1/responses",
        {
            "model": "any",
            "input": [{"role": "user", "content": QUERY}],
            "tools": [{"type": "web_search"}],
        },
    ),
    "gemini": (
        "/v1beta/models/any:generateContent",
        {
            "contents": [{"role": "user", "parts": [{"text": QUERY}]}],
            "tools": [{"googleSearch": {}}],
        },
    ),
}
#: Posts argv[2] as JSON to argv[1] with the episode token in every header a dialect reads,
#: and prints the status.
_POST = """
import json, sys, urllib.error, urllib.request
token = sys.argv[3]
request = urllib.request.Request(sys.argv[1], data=sys.argv[2].encode(), headers={
    "content-type": "application/json", "authorization": "Bearer " + token,
    "x-goog-api-key": token,
})
try:
    print(urllib.request.urlopen(request, timeout=60).status)
except urllib.error.HTTPError as error:
    print(error.code)
"""


async def direct_request(bench: Bench, dialect: str, *, open_network: bool) -> tuple[int, int]:
    """Send ``DIRECT[dialect]`` from inside the Sandbox; return its status and how many
    requests reached the model."""
    bench.model.script = ()
    bench.model.requests.clear()
    upstream = (
        None
        if dialect == "gemini"
        else Upstream(
            dialect=dialect, endpoint=bench.upstream + REQUEST_PATHS[dialect], api_key="host-only"
        )
    )
    session = bench.gateway.open_session(
        GatewaySession(
            episode_id=f"web-tools-direct-{dialect}",
            model="run-model",
            upstream=upstream,
            provider_web_tools=open_network,
        )
    )
    path, body = DIRECT[dialect]
    try:
        result = await bench.sandbox.exec(
            [
                agent_python(cli_os()),
                "-Ic",
                _POST,
                (bench.sandbox.gateway_url or bench.gateway.base_url) + path,
                json.dumps(body),
                session.token,
            ],
            identity=Identity.AGENT,
            timeout_sec=120,
        )
    finally:
        bench.gateway.close_session(session)
    assert result.exit_code == 0, result
    return int(result.stdout.strip().splitlines()[-1]), len(bench.model.requests)

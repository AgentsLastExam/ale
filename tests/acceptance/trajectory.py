"""Small post-run checks shared by live autonomous-harness acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from ale.run.secrets import provider_credentials

LIVE = pytest.mark.skipif(
    os.environ.get("ALE_RUN_LIVE_ACCEPTANCE") != "1",
    reason="set ALE_RUN_LIVE_ACCEPTANCE=1 to make real model calls",
)
MODEL = os.environ.get("ALE_LIVE_MODEL", "claude-sonnet-5")
ENGINE_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = ENGINE_ROOT.parent


def task_path(variable: str, default: Path) -> Path:
    return Path(os.environ.get(variable, default)).expanduser().resolve()


def run_task(
    reference: Path,
    runs_dir: Path,
    *,
    run_id: str,
    settings: tuple[str, ...] = (),
) -> Path:
    executable = Path(sys.executable).parent / "ale"
    command = [
        str(executable),
        "run",
        str(reference),
        "--agent",
        "claude-code",
        "--model",
        MODEL,
        "--runs-dir",
        str(runs_dir),
        "--run-id",
        run_id,
        "--no-resume",
    ]
    for setting in settings:
        command.extend(("--set", setting))
    command.extend(("--set", 'logging.native_logs="debug"'))
    result = subprocess.run(
        command,
        cwd=ENGINE_ROOT,
        text=True,
        capture_output=True,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    episodes = [path.parent for path in (runs_dir / run_id).glob("*/lock.json")]
    assert len(episodes) == 1, result.stdout + result.stderr
    return episodes[0]


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def audit_episode(
    episode: Path, *, model: str = MODEL
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    transport = jsonl(episode / "trace.transport.jsonl")
    trajectory = json.loads((episode / "trajectory.json").read_text())
    execution = jsonl(episode / "trace.execution.jsonl")
    result = json.loads((episode / "result.json").read_text())
    native = jsonl(episode / "logs/claude-code/transcript.jsonl")
    lock = json.loads((episode / "lock.json").read_text())

    calls = [record for record in transport if record["kind"] == "call"]
    assert calls
    assert all(record["model"] == model for record in calls)
    assert all(
        record["disposition"] == "forwarded" and record.get("upstream_status") is None
        for record in calls
    )
    verify_commands = [
        record
        for record in execution
        if record["kind"] == "command_finished" and record["phase"] == "verify"
    ]
    assert verify_commands and verify_commands[-1]["outcome"] == "succeeded"
    assert result["status"] == "completed"
    assert result["rewards"] and all(value == 1.0 for value in result["rewards"].values())
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert trajectory["steps"][0]["source"] == "user"
    assert lock["agent"]["harness"] == "claude-code"
    assert lock["agent"]["model"] == model
    assert lock["agent"]["version"] == "2.1.220"
    return transport, trajectory["steps"], native, lock


def trajectory_mcp_calls(steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for step in steps:
        results = {
            result.get("source_call_id"): result
            for result in (step.get("observation") or {}).get("results", [])
        }
        for call in step.get("tool_calls") or []:
            mcp = ((call.get("extra") or {}).get("ale") or {}).get("mcp")
            if mcp:
                calls[call["tool_call_id"]] = {
                    "call": call,
                    "mcp": mcp,
                    "result": results.get(call["tool_call_id"]),
                }
    return calls


def native_mcp_pairs(native: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    pairs: dict[str, dict[str, Any]] = {}
    for event in native:
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and str(part.get("name", "")).startswith("mcp__"):
                pairs[str(part["id"])] = {"use": part}
            elif part.get("type") == "tool_result" and str(part.get("tool_use_id")) in pairs:
                pairs[str(part["tool_use_id"])]["result"] = part
    return pairs


def llm_audit_episode(episode: Path, *, requirement: str) -> dict[str, Any]:
    """Use an independent model call to judge the retained canonical evidence."""
    api_key, upstream = provider_credentials()
    evidence = {
        "trajectory": json.loads((episode / "trajectory.json").read_text()),
        "result": json.loads((episode / "result.json").read_text()),
        "transport": jsonl(episode / "trace.transport.jsonl"),
        "execution": jsonl(episode / "trace.execution.jsonl"),
        "requirement": requirement,
    }
    payload = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 1200,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Audit this agent episode. Decide only from the supplied canonical "
                        "evidence whether the requirement was actually satisfied by the "
                        "agent, not merely claimed in text. Return strict JSON under 800 "
                        "characters with keys valid (boolean), evidence (at most four "
                        "strings of at most 120 characters), and reason (at most 200 "
                        "characters). " + json.dumps(evidence, ensure_ascii=False)
                    ),
                }
            ],
        },
        ensure_ascii=False,
    ).encode()
    request = urllib.request.Request(
        f"{upstream.rstrip('/')}/v1/messages",
        data=payload,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.loads(response.read())
    text = "".join(
        item.get("text", "")
        for item in body.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ).strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    audit = json.loads(text)
    assert audit.get("valid") is True, audit
    assert audit.get("evidence"), audit
    return audit

"""Read copied CLI evidence without mixing adjacent invocations."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ale.core.harness import TrajectoryParseContext
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses.base import INVOCATIONS_NAME, STDOUT_NAME


def read_invocations(logs_dir: Path) -> list[dict[str, Any]]:
    path = logs_dir / INVOCATIONS_NAME
    if not path.is_file():
        return []
    records = []
    for line in path.read_bytes().splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("stdout_offset"), int):
            records.append(value)
    return records


def stdout_segments(context: TrajectoryParseContext) -> list[str]:
    path = context.logs_dir / STDOUT_NAME
    if not path.is_file():
        return []
    data = path.read_bytes()
    offsets = [record["stdout_offset"] for record in read_invocations(context.logs_dir)]
    offsets = sorted(set(offset for offset in offsets if 0 <= offset <= len(data)))
    if not offsets or offsets[0] != 0:
        offsets.insert(0, 0)
    return [
        data[start:end].decode("utf-8", "replace")
        for start, end in zip(offsets, [*offsets[1:], len(data)], strict=True)
    ]


def jsonl_events(
    context: TrajectoryParseContext,
    path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    segments = (
        stdout_segments(context)
        if path is None
        else ([path.read_text(encoding="utf-8", errors="replace")] if path.is_file() else [])
    )
    events, issues = [], []
    running = context.incomplete_reason == "running"
    for segment_index, segment in enumerate(segments):
        for line_number, line in enumerate(segment.splitlines(keepends=True), 1):
            if not line.strip():
                continue
            if running and segment_index == len(segments) - 1 and not line.endswith("\n"):
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("expected a JSON object")
                events.append(event)
            except ValueError as exc:
                issues.append(
                    {"line": line_number, "reason": "malformed_json", "detail": str(exc)[:500]}
                )
    return events, issues


def evidence_scope(trajectory: AtifTrajectory, context: TrajectoryParseContext) -> AtifTrajectory:
    extra = dict(trajectory.extra or {})
    ale = dict(extra.get("ale") or {})
    ale["native_logs_enabled"] = context.native_logs_enabled
    ale["evidence_scope"] = "stdout_and_native" if context.native_logs_enabled else "stdout_only"
    extra["ale"] = ale
    return trajectory.model_copy(update={"extra": extra})


def unconverted_native_files(
    context: TrajectoryParseContext, paths: Iterable[Path]
) -> list[dict[str, Any]]:
    """Keep raw evidence when collected sessions are outside a parser's coverage."""
    paths = sorted(paths)
    if not paths:
        return []
    return [
        {
            "reason": "unconverted_native_files",
            "count": len(paths),
            "sources": [path.relative_to(context.logs_dir).as_posix() for path in paths[:10]],
        }
    ]

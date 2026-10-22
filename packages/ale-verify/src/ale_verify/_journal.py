"""Durable Judge events, independent of task results and process success."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from ._io import redact


def journal(
    kind: Literal["llm", "agent"], event: dict[str, Any], secrets: tuple[str, ...] = ()
) -> None:
    path = os.environ.get(f"ALE_{kind.upper()}_JUDGE_LOG_PATH")
    if not path:
        return
    tokens = tuple(
        os.environ.get(key, "") for key in ("ALE_LLM_JUDGE_TOKEN", "ALE_AGENT_JUDGE_TOKEN")
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Redact values before serialization so escaped characters cannot defeat redaction.
    def cleaned(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value, (*secrets, *tokens))
        if isinstance(value, dict):
            return {cleaned(key): cleaned(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [cleaned(item) for item in value]
        return value

    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(cleaned(event), ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

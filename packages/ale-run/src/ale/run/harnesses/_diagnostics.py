"""Bounded, credential-safe diagnostics for native process failures."""

import json

from ale.run.recording import Redactor


def error_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(filter(None, (error_text(item) for item in value)))
    if isinstance(value, dict):
        return "; ".join(
            filter(
                None,
                (
                    error_text(value.get(key))
                    for key in ("message", "detail", "error", "errors", "code", "type")
                ),
            )
        )
    return ""


def plain_output(text: str) -> str:
    lines = []
    for line in text.splitlines():
        try:
            json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
    return "\n".join(lines)


def failure_detail(
    harness: str,
    exit_code: int,
    *,
    native: str = "",
    stderr: str = "",
    stdout: str = "",
    token: str = "",
) -> str:
    redact = Redactor((token,))
    parts = [f"{harness} failed (exit code {exit_code})"]
    if native.strip():
        parts.append("native: " + redact(native.strip())[:1600])
    if stderr.strip():
        parts.append("stderr: " + redact(stderr.strip())[-600:])
    if not native.strip() and (plain := plain_output(stdout).strip()):
        parts.append("stdout: " + redact(plain)[-600:])
    return "; ".join(parts)

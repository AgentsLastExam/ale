"""Wire protocol for the guest service.

JSON Lines in both directions. One request per line, one terminal response per request,
optional interim events in between for streamed operations.

Standard library only, and no engine imports: this module is copied into images whose
Python we do not control.
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1

MAX_LINE_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 4 * 1024 * 1024

# Error codes are part of the contract: the host maps them to typed failures.
ERR_BAD_REQUEST = "bad_request"
ERR_NOT_FOUND = "not_found"
ERR_PERMISSION = "permission"
ERR_TIMEOUT = "timeout"
ERR_UNSUPPORTED = "unsupported"
ERR_INTERNAL = "internal"


class ProtocolError(Exception):
    """A malformed or unsupported message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def encode(payload: dict[str, Any]) -> str:
    """Serialise one message as a single line."""
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise ProtocolError(ERR_BAD_REQUEST, "message exceeds the line limit; chunk it")
    return line


def decode(line: str) -> dict[str, Any]:
    """Parse one message line."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(ERR_BAD_REQUEST, f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(ERR_BAD_REQUEST, "message must be a JSON object")
    return payload


def request(req_id: int, op: str, **params: Any) -> dict[str, Any]:
    return {"id": req_id, "op": op, "params": params}


def ok(req_id: int, **data: Any) -> dict[str, Any]:
    return {"id": req_id, "ok": True, "data": data}


def err(req_id: int, code: str, message: str) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "error": {"code": code, "message": message}}


def event(req_id: int, name: str, **data: Any) -> dict[str, Any]:
    return {"id": req_id, "event": name, "data": data}

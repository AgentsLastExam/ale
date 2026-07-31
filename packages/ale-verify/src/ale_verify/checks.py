"""Small deterministic verification checks using only the standard library."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from ._io import digest
from ._records import CheckResult, EvidenceReference


def file_exists(path: str | os.PathLike[str]) -> CheckResult:
    exists = os.path.isfile(path)
    return CheckResult(float(exists), "file exists" if exists else "file is missing", exists)


def file_missing(path: str | os.PathLike[str]) -> CheckResult:
    missing = not os.path.exists(path)
    return CheckResult(float(missing), "file is missing" if missing else "path exists", missing)


def text_equals(path: str | os.PathLike[str], expected: str) -> CheckResult:
    return _text(path, lambda actual: actual == expected, "text equals expected", expected)


def text_contains(path: str | os.PathLike[str], expected: str) -> CheckResult:
    return _text(path, lambda actual: expected in actual, "text contains expected", expected)


def text_regex(path: str | os.PathLike[str], pattern: str, flags: int = 0) -> CheckResult:
    compiled = re.compile(pattern, flags)
    return _text(
        path,
        lambda actual: compiled.search(actual) is not None,
        "text matches regex",
        pattern,
    )


def json_value(path: str | os.PathLike[str], json_path: str, expected: object) -> CheckResult:
    parts = _json_parts(json_path)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        for part in parts:
            value = value[int(part)] if isinstance(value, list) else value[part]
        matched = value == expected
        return CheckResult(
            float(matched),
            "JSON value matched" if matched else "JSON value differed",
            value,
            _evidence(path),
        )
    except (FileNotFoundError, KeyError, IndexError, TypeError, ValueError) as exc:
        return CheckResult(0.0, f"JSON check failed: {exc}")


def numeric(
    actual: float,
    expected: float,
    abs_tolerance: float = 0.0,
    rel_tolerance: float = 0.0,
) -> CheckResult:
    values = (float(actual), float(expected), float(abs_tolerance), float(rel_tolerance))
    if any(not math.isfinite(value) for value in values) or values[2] < 0 or values[3] < 0:
        raise ValueError("numeric values and tolerances must be finite and tolerances non-negative")
    matched = math.isclose(values[0], values[1], abs_tol=values[2], rel_tol=values[3])
    return CheckResult(
        float(matched),
        "numeric value matched" if matched else "numeric value differed",
        values[0],
    )


def command(
    argv: list[str] | tuple[str, ...],
    cwd: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 30,
    exit_code: int = 0,
    stdout: str | re.Pattern[str] | None = None,
    stderr: str | re.Pattern[str] | None = None,
) -> CheckResult:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("command argv must be a non-empty list or tuple")
    if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
        raise ValueError("command timeout_seconds must be finite and positive")
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CheckResult(
            0.0,
            "command timed out",
            {"timed_out": True, "stdout": _bounded(exc.stdout), "stderr": _bounded(exc.stderr)},
        )
    matched = completed.returncode == exit_code
    if stdout is not None:
        matched = matched and _matches(completed.stdout, stdout)
    if stderr is not None:
        matched = matched and _matches(completed.stderr, stderr)
    return CheckResult(
        float(matched),
        "command matched" if matched else "command output differed",
        {
            "exit_code": completed.returncode,
            "stdout": _bounded(completed.stdout),
            "stderr": _bounded(completed.stderr),
        },
    )


def csv_value(path: str | os.PathLike[str], row: int, column: int, expected: str) -> CheckResult:
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        value = rows[int(row)][int(column)]
        matched = value == expected
        return CheckResult(
            float(matched),
            "CSV value matched" if matched else "CSV value differed",
            value,
            _evidence(path),
        )
    except (FileNotFoundError, IndexError, TypeError, ValueError, csv.Error) as exc:
        return CheckResult(0.0, f"CSV check failed: {exc}")


def sqlite_value(
    path: str | os.PathLike[str],
    query: str,
    expected: object,
    parameters: tuple[object, ...] = (),
) -> CheckResult:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("SQLite query must be non-empty")
    try:
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(os.fspath(path))}?mode=ro",
            uri=True,
        )
        try:
            row = connection.execute(query, tuple(parameters)).fetchone()
        finally:
            connection.close()
        value = None if row is None else row[0]
        matched = value == expected
        return CheckResult(
            float(matched),
            "SQLite value matched" if matched else "SQLite value differed",
            value,
            _evidence(path),
        )
    except (OSError, sqlite3.Error) as exc:
        return CheckResult(0.0, f"SQLite check failed: {exc}")


def http(
    url: str,
    status: int = 200,
    contains: str | None = None,
    timeout_seconds: float = 10,
) -> CheckResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise ValueError("HTTP checks are limited to local URLs")
    try:
        with urllib.request.urlopen(url, timeout=float(timeout_seconds)) as response:
            body = response.read(1024 * 1024).decode("utf-8")
            matched = response.status == status and (contains is None or contains in body)
            return CheckResult(
                float(matched),
                "HTTP response matched" if matched else "HTTP response differed",
                {"status": response.status, "body": _bounded(body)},
            )
    except (OSError, UnicodeDecodeError) as exc:
        return CheckResult(0.0, f"HTTP check failed: {exc}")


def trajectory_tool_used(name: str) -> CheckResult:
    used = name in _trajectory_tools()
    return CheckResult(
        float(used),
        "tool was used" if used else "tool was not used",
        name,
        _trajectory_evidence(),
    )


def trajectory_tool_not_used(name: str) -> CheckResult:
    used = name in _trajectory_tools()
    return CheckResult(
        float(not used),
        "tool was not used" if not used else "tool was used",
        name,
        _trajectory_evidence(),
    )


def trajectory_turn_count(minimum: int | None = None, maximum: int | None = None) -> CheckResult:
    if minimum is None and maximum is None:
        raise ValueError("set minimum or maximum")
    count = sum(1 for step in _trajectory()["steps"] if step.get("source") == "agent")
    matched = (minimum is None or count >= minimum) and (maximum is None or count <= maximum)
    return CheckResult(
        float(matched),
        "turn count matched" if matched else "turn count differed",
        count,
        _trajectory_evidence(),
    )


def _text(path, predicate, success: str, expected: object) -> CheckResult:  # type: ignore[no-untyped-def]
    try:
        with open(path, encoding="utf-8") as handle:
            actual = handle.read()
        matched = predicate(actual)
        return CheckResult(
            float(matched),
            success if matched else "text differed",
            _bounded(actual),
            _evidence(path),
        )
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        return CheckResult(0.0, f"text check failed: {exc}", expected)


def _matches(actual: str, expected: str | re.Pattern[str]) -> bool:
    if hasattr(expected, "search"):
        return expected.search(actual) is not None
    return actual == expected


def _json_parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path or path.startswith(".") or path.endswith("."):
        raise ValueError("JSON path must be a non-empty dotted path")
    parts = path.split(".")
    if any(not part or not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        raise ValueError("JSON path contains unsafe syntax")
    return parts


def _trajectory() -> dict[str, object]:
    path = os.environ.get("ALE_TRAJECTORY_PATH")
    if not path:
        raise RuntimeError("ALE_TRAJECTORY_PATH is not configured")
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != "ATIF-v1.7" or not isinstance(payload.get("steps"), list):
        raise ValueError("trusted trajectory is malformed")
    return payload


def _trajectory_tools() -> set[object]:
    return {
        call.get("function_name")
        for step in _trajectory()["steps"]  # type: ignore[union-attr]
        for call in step.get("tool_calls") or ()
        if isinstance(call, dict)
    }


def _trajectory_evidence() -> tuple[EvidenceReference, ...]:
    path = os.environ.get("ALE_TRAJECTORY_PATH")
    return _evidence(path, kind="solver_trajectory") if path else ()


def _evidence(path: str | os.PathLike[str], kind: str = "file") -> tuple[EvidenceReference, ...]:
    try:
        raw = Path(path).read_bytes()
        return (
            EvidenceReference(
                kind=kind,  # type: ignore[arg-type]
                location=os.fspath(path),
                sha256=digest(raw),
                size_bytes=len(raw),
            ),
        )
    except OSError:
        return ()


def _bounded(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[:4096]

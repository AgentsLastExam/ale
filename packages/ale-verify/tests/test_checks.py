from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from ale_verify import checks


def test_file_text_regex_and_json_checks(tmp_path: Path) -> None:
    text = tmp_path / "value.txt"
    text.write_text("hello 42\n")
    data = tmp_path / "value.json"
    data.write_text('{"nested":{"value":7}}')
    assert checks.file_exists(text).score == 1
    assert checks.file_missing(tmp_path / "missing").score == 1
    assert checks.text_equals(text, "hello 42\n").score == 1
    assert checks.text_contains(text, "42").score == 1
    assert checks.text_regex(text, r"\d+").score == 1
    assert checks.json_value(data, "nested.value", 7).score == 1
    assert checks.json_value(data, "nested.value", 8).score == 0


def test_command_numeric_csv_and_sqlite_checks(tmp_path: Path) -> None:
    csv_path = tmp_path / "value.csv"
    csv_path.write_text("a,b\nx,7\n")
    db = tmp_path / "value.db"
    with sqlite3.connect(db) as connection:
        connection.execute("create table values_ (value integer)")
        connection.execute("insert into values_ values (7)")
    assert checks.command([sys.executable, "-c", "print('ok')"], stdout="ok\n").score == 1
    assert (
        checks.command(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            timeout_seconds=0.01,
        ).score
        == 0
    )
    assert checks.numeric(1.01, 1, abs_tolerance=0.02).score == 1
    assert checks.csv_value(csv_path, 1, 1, "7").score == 1
    assert checks.sqlite_value(db, "select value from values_", 7).score == 1


def test_loopback_http_and_trajectory_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self, _size: int = -1) -> bytes:
            return b"ready"

    monkeypatch.setattr(checks.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert checks.http("http://127.0.0.1:8080/health", contains="ready").score == 1
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "steps": [
                    {
                        "source": "agent",
                        "tool_calls": [{"function_name": "shell"}],
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("ALE_TRAJECTORY_PATH", str(trajectory))
    assert checks.trajectory_tool_used("shell").score == 1
    assert checks.trajectory_tool_not_used("browser").score == 1
    assert checks.trajectory_turn_count(minimum=1, maximum=1).score == 1
    with pytest.raises(ValueError, match="local"):
        checks.http("https://example.com")


@pytest.mark.parametrize("path", ["", ".bad", "bad.", "a..b", "a[0]"])
def test_invalid_json_paths_are_authored_errors(tmp_path: Path, path: str) -> None:
    source = tmp_path / "value.json"
    source.write_text("{}")
    with pytest.raises(ValueError, match="JSON path"):
        checks.json_value(source, path, None)

"""Transport and execution trace contracts and durable JSONL behavior."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from ale.core.blob import BlobText, InlineText
from ale.core.sandbox import ExecResult
from ale.core.trace import (
    CommandFinished,
    ExecutionEvent,
    PhaseStarted,
    TransportCall,
    TransportEvent,
    TransportReplay,
    read_jsonl,
)
from ale.run.recording import (
    BlobStore,
    CommandRecorder,
    EpisodeRecording,
    JsonlWriter,
    execution_logging,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "1" * 64


def test_transport_union_is_strict_and_discriminated() -> None:
    call = TransportCall(
        seq=0,
        episode_id="episode",
        call_id="call-1",
        model="model",
        request_digest=DIGEST,
        disposition="forwarded",
    )
    assert TypeAdapter(TransportEvent).validate_python(call.model_dump()).kind == "call"
    replay = TransportReplay(
        seq=1,
        episode_id="episode",
        call_id="call-1",
        request_digest=DIGEST,
        reason="completed-cache",
    )
    assert replay.charged is False
    with pytest.raises(ValidationError):
        TransportCall.model_validate(call.model_dump() | {"unknown": True})


def test_execution_union_requires_command_terminal_details() -> None:
    event = CommandFinished(
        seq=1,
        episode_id="episode",
        phase="setup",
        component="task-stage",
        execution_id="exec-1",
        outcome="succeeded",
        exit_code=0,
        duration_ms=10,
        stdout=InlineText(inline="ok\n", size_bytes=3),
        stderr=InlineText(inline="", size_bytes=0),
    )
    parsed = TypeAdapter(ExecutionEvent).validate_python(event.model_dump())
    assert parsed.kind == "command_finished"
    with pytest.raises(ValidationError):
        PhaseStarted(
            seq=0,
            episode_id="episode",
            phase="setup",
            component="standard",
            level="verbose",  # type: ignore[arg-type]
        )


def test_jsonl_writer_resumes_sequence_and_keeps_complete_lines(tmp_path: Path) -> None:
    path = tmp_path / "trace.execution.jsonl"
    writer = JsonlWriter(path)
    assert writer.append({"kind": "one"}) == 0
    assert writer.append({"kind": "two"}, durable=True) == 1

    reopened = JsonlWriter(path)
    assert reopened.append({"kind": "three"}) == 2
    result = read_jsonl(path)
    assert [record["seq"] for record in result.records] == [0, 1, 2]
    assert result.torn_suffix is None


def test_reader_returns_complete_prefix_and_reports_torn_suffix(tmp_path: Path) -> None:
    path = tmp_path / "trace.execution.jsonl"
    path.write_text('{"seq":0,"kind":"ok"}\n{"seq":1,', encoding="utf-8")
    result = read_jsonl(path)
    assert result.records == ({"seq": 0, "kind": "ok"},)
    assert result.torn_suffix == '{"seq":1,'


@pytest.mark.asyncio
async def test_command_output_is_redacted_across_chunks_and_finalized_once(
    tmp_path: Path,
) -> None:
    recording = EpisodeRecording(tmp_path)
    recorder = CommandRecorder(
        execution=recording.execution,
        blobs=recording.blobs,
        episode_id="episode",
        phase="setup",
        component="task-stage",
        execution_id="exec-1",
        actor="task",
        argv=["bash", "run.sh"],
        secrets=("top-secret-token",),
    )
    await recorder.write("stdout", b"before top-sec")
    await recorder.write("stdout", b"ret-token after")
    recorder.finish(ExecResult(exit_code=0, duration_ms=5))

    records = read_jsonl(recording.execution.path).records
    assert [record["kind"] for record in records] == [
        "command_started",
        "command_finished",
    ]
    assert records[-1]["stdout"]["inline"] == "before [REDACTED] after"
    assert not recording.blobs.partial_path("exec-1", "stdout").exists()


def test_execution_logger_routes_only_bound_records(tmp_path: Path) -> None:
    recording = EpisodeRecording(tmp_path)
    logger = logging.getLogger("ale.execution")
    logger.info("unbound")
    with execution_logging(
        recording.execution,
        episode_id="episode",
        phase="verify",
        component="verifier",
    ):
        logger.warning("diagnostic", extra={"ale_data": {"attempt": 2}})
    records = read_jsonl(recording.execution.path).records
    assert len(records) == 1
    assert records[0]["message"] == "diagnostic"
    assert records[0]["data"] == {"attempt": 2}


def test_reopen_recovers_partial_output_as_an_incomplete_blob(tmp_path: Path) -> None:
    partial = BlobStore(tmp_path).partial_path("exec-9", "stderr")
    partial.write_text("last line before crash")
    recording = EpisodeRecording(tmp_path)
    (event,) = read_jsonl(recording.execution.path).records
    assert event["kind"] == "partial_output_recovered"
    output = BlobText.model_validate(event["output"])
    assert output.blob.complete is False
    assert (tmp_path / output.blob.path).read_text() == "last line before crash"
    assert not partial.exists()

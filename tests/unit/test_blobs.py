"""Episode-local blob and inline-text contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ale.core.blob import BlobRef, InlineText, MediaClass
from ale.run.recording import INLINE_TEXT_LIMIT, BlobStore, EpisodeRecording

pytestmark = pytest.mark.unit


def test_blob_ref_rejects_unsafe_or_mismatched_paths() -> None:
    digest = "sha256:" + "a" * 64
    ref = BlobRef(
        path=f"blobs/image/sha256-{'a' * 64}.png",
        media_type="image/png",
        size_bytes=3,
        sha256=digest,
    )
    assert ref.media_class is MediaClass.IMAGE
    for path in (
        "/tmp/blob.png",
        "../blobs/image/file.png",
        f"blobs/text/sha256-{'a' * 64}.png",
    ):
        with pytest.raises(ValidationError):
            BlobRef(
                path=path,
                media_type="image/png",
                size_bytes=3,
                sha256=digest,
            )


def test_exact_inline_boundary_stays_inline(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    inline = store.text("x" * INLINE_TEXT_LIMIT)
    assert isinstance(inline, InlineText)
    external = store.text("x" * (INLINE_TEXT_LIMIT + 1))
    assert isinstance(external, BlobRef)
    assert external.path.startswith("blobs/text/")


def test_content_addressing_deduplicates_and_uses_safe_mime_extension(
    tmp_path: Path,
) -> None:
    store = BlobStore(tmp_path)
    first = store.put(b"\x89PNG", media_type="image/png")
    second = store.put(b"\x89PNG", media_type="image/png")
    assert first == second
    assert first.path.endswith(".png")
    assert len(list((tmp_path / "blobs" / "image").iterdir())) == 1


def test_media_classes_cover_large_payload_families() -> None:
    assert {item.value for item in MediaClass} == {
        "image",
        "audio",
        "video",
        "document",
        "text",
        "binary",
    }


def test_recording_verifies_referenced_blob_bytes(tmp_path: Path) -> None:
    recording = EpisodeRecording(tmp_path)
    ref = recording.blobs.put(b"payload", media_type="application/octet-stream")
    recording.trajectory_path.write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "agent": {"name": "x", "version": "1"},
                "steps": [
                    {
                        "step_id": 1,
                        "source": "user",
                        "message": "x",
                        "extra": {"ale": {"attachments": [ref.model_dump()]}},
                    }
                ],
            }
        )
    )
    recording.verify_blob_references()
    (tmp_path / ref.path).write_bytes(b"changed")
    with pytest.raises(ValueError, match=r"size mismatch|digest mismatch"):
        recording.verify_blob_references()

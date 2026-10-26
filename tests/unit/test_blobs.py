"""Episode-local blob and inline-text contracts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from ale.core.blob import BlobRef, InlineText, MediaClass
from ale.run import recording
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


def test_concurrent_same_blob_writes_deduplicate_without_temporary_file_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BlobStore(tmp_path)
    workers = 8
    ready = Barrier(workers)
    replace = recording.os.replace

    def replace_together(source: Path, target: Path) -> None:
        ready.wait(timeout=5)
        replace(source, target)

    monkeypatch.setattr(recording.os, "replace", replace_together)
    payload = b"shared image payload" * 1024
    with ThreadPoolExecutor(max_workers=workers) as executor:
        writes = [
            executor.submit(store.put, payload, media_type="image/png") for _ in range(workers)
        ]
        refs = [write.result() for write in writes]
    assert refs == [refs[0]] * workers
    assert (tmp_path / refs[0].path).read_bytes() == payload
    assert list((tmp_path / "blobs/image").iterdir()) == [tmp_path / refs[0].path]


def test_failed_blob_publication_removes_its_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("publication failed")

    monkeypatch.setattr(recording.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failed"):
        BlobStore(tmp_path).put(b"image", media_type="image/png")
    assert list((tmp_path / "blobs/image").iterdir()) == []


@pytest.mark.parametrize(
    ("media_type", "extension"),
    [
        ("audio/wav", "wav"),
        ("audio/mpeg", "mp3"),
        ("audio/mp4", "m4a"),
        ("audio/aac", "aac"),
        ("audio/ogg", "ogg"),
        ("audio/flac", "flac"),
        ("audio/webm", "webm"),
        ("audio/aiff", "aiff"),
    ],
)
def test_atif_audio_blobs_have_stable_extensions(
    tmp_path: Path, media_type: str, extension: str
) -> None:
    ref = BlobStore(tmp_path).put(b"recorded audio", media_type=media_type)
    assert ref.path.startswith("blobs/audio/")
    assert ref.path.endswith(f".{extension}")
    assert (tmp_path / ref.path).read_bytes() == b"recorded audio"


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
                "schema_version": "ATIF-v1.8",
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

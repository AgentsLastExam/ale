from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.blob import BlobRef, InlineText
from ale.run.recording import INLINE_TEXT_LIMIT, BlobStore, EpisodeRecording

pytestmark = pytest.mark.integration


def test_common_blob_policy_deduplicates_all_payload_families(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    refs = [
        store.put(b"same", media_type=media_type)
        for media_type in (
            "image/png",
            "audio/wav",
            "video/mp4",
            "application/pdf",
            "text/plain",
            "application/octet-stream",
        )
    ]
    assert [ref.media_class.value for ref in refs] == [
        "image",
        "audio",
        "video",
        "document",
        "text",
        "binary",
    ]
    assert store.put(b"same", media_type="image/png") == refs[0]
    assert len(list((tmp_path / "blobs/image").iterdir())) == 1


def test_ten_identical_payloads_share_one_resolvable_blob(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    refs = [store.put(b"immutable", media_type="application/pdf") for _ in range(10)]

    assert len(refs) == 10
    assert len({ref.path for ref in refs}) == 1
    assert (tmp_path / refs[0].path).read_bytes() == b"immutable"
    assert len(list((tmp_path / "blobs/document").iterdir())) == 1


def test_inline_boundary_and_partial_recovery(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    assert isinstance(store.text("x" * INLINE_TEXT_LIMIT), InlineText)
    assert isinstance(store.text("x" * (INLINE_TEXT_LIMIT + 1)), BlobRef)
    partial = store.partial_path("exec-1", "stdout")
    partial.write_text("received before crash")
    EpisodeRecording(tmp_path)
    recovered = [path for path in (tmp_path / "blobs/text").iterdir() if path.is_file()]
    assert any(path.read_text() == "received before crash" for path in recovered)
    assert not partial.exists()
    assert not list((tmp_path / "blobs").rglob("*.tmp"))

"""Typed references for episode-local external payloads."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "BlobRef",
    "BlobSink",
    "BlobText",
    "InlineText",
    "InlineTextOrBlob",
    "MediaClass",
    "media_class_for",
]

_STRICT = ConfigDict(frozen=True, extra="forbid")
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class MediaClass(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    TEXT = "text"
    BINARY = "binary"


def media_class_for(media_type: str) -> MediaClass:
    kind = media_type.partition(";")[0].strip().lower()
    if kind.startswith("image/"):
        return MediaClass.IMAGE
    if kind.startswith("audio/"):
        return MediaClass.AUDIO
    if kind.startswith("video/"):
        return MediaClass.VIDEO
    if kind.startswith("text/") or kind in {"application/json", "application/xml"}:
        return MediaClass.TEXT
    if kind in {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        return MediaClass.DOCUMENT
    return MediaClass.BINARY


class BlobRef(BaseModel):
    model_config = _STRICT

    path: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=_DIGEST)
    complete: bool = True

    @property
    def media_class(self) -> MediaClass:
        parts = PurePosixPath(self.path).parts
        return MediaClass(parts[1]) if len(parts) > 2 else media_class_for(self.media_type)

    @model_validator(mode="after")
    def _safe_consistent_path(self) -> Self:
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or "\\" in self.path:
            raise ValueError("blob path must be normalized and episode-relative")
        if self.path.startswith("blobs/.partial/"):
            if self.complete:
                raise ValueError("partial blobs must be incomplete")
            return self
        if len(path.parts) != 3 or path.parts[0] != "blobs":
            raise ValueError("final blob path must be blobs/<media-class>/<digest>")
        expected = media_class_for(self.media_type)
        if path.parts[1] != expected.value:
            raise ValueError("blob path media class does not match media type")
        digest = self.sha256.removeprefix("sha256:")
        if not path.name.startswith(f"sha256-{digest}"):
            raise ValueError("blob filename does not match sha256")
        suffix = path.name.removeprefix(f"sha256-{digest}")
        if suffix and (not suffix.startswith(".") or not suffix[1:].isalnum() or len(suffix) > 10):
            raise ValueError("blob extension is unsafe")
        return self


class InlineText(BaseModel):
    model_config = _STRICT

    inline: str
    size_bytes: int = Field(ge=0)
    complete: bool = True

    @model_validator(mode="after")
    def _size_matches(self) -> Self:
        if self.size_bytes != len(self.inline.encode("utf-8")):
            raise ValueError("inline size_bytes does not match UTF-8 content")
        return self


class BlobText(BaseModel):
    model_config = _STRICT

    blob: BlobRef


InlineTextOrBlob = InlineText | BlobText


class BlobSink(Protocol):
    def put(self, data: bytes, *, media_type: str, complete: bool = True) -> BlobRef: ...

    def text(self, text: str, *, complete: bool = True) -> InlineText | BlobRef: ...

    def partial_path(self, execution_id: str, stream: str) -> Path: ...

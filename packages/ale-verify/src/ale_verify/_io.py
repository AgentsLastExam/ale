"""Local verify configuration, evidence, sanitization, and atomic files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._records import EvidenceReference

MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_REFERENCE_BYTES = 256 * 1024
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class VerificationError(RuntimeError):
    """Base class for sandbox-side verification failures."""


class ConfigurationError(VerificationError):
    """A requested Judge lacks usable run-owned configuration."""


class EvidenceError(VerificationError):
    """Judge evidence is unsafe, missing, or too large."""


class JudgeError(VerificationError):
    """A Judge failed after recording its attempts."""

    def __init__(self, message: str, invocation: object | None = None) -> None:
        super().__init__(message)
        self.invocation = invocation


def atomic_json(path: str | os.PathLike[str], payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_config() -> dict[str, dict[str, str]]:
    path = os.environ.get("ALE_VERIFY_CONFIG_PATH")
    if not path:
        return {}
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"verification config could not be read: {exc}") from exc
    if not isinstance(loaded, dict) or set(loaded) - {"llm", "agent"}:
        raise ConfigurationError("verification config must contain only llm and agent sections")
    normalized: dict[str, dict[str, str]] = {}
    for kind, section in loaded.items():
        if section is None:
            continue
        required = {"model", "reasoning_effort", "base_url", "api_key_env"}
        if kind == "agent":
            required.update({"adapter", "version"})
        if not isinstance(section, dict) or set(section) != required:
            raise ConfigurationError(f"verification {kind} config fields are invalid")
        if any(not isinstance(value, str) or not value.strip() for value in section.values()):
            raise ConfigurationError(f"verification {kind} config values must be non-empty strings")
        parsed = urlsplit(section["base_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError(f"verification {kind} base_url must be absolute HTTP(S)")
        if not _ENV_NAME.fullmatch(section["api_key_env"]):
            raise ConfigurationError(f"verification {kind} api_key_env is invalid")
        if kind == "agent" and section["adapter"] not in {"codex-cli", "claude-code"}:
            raise ConfigurationError("verification agent adapter is unsupported")
        if kind == "agent" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", section["version"]):
            raise ConfigurationError("verification agent version must be an exact semver")
        normalized[kind] = {str(key): str(value) for key, value in section.items()}
    return normalized


def read_text_evidence(
    path: str | os.PathLike[str],
    *,
    kind: str = "file",
    max_bytes: int = MAX_EVIDENCE_BYTES,
) -> tuple[str, EvidenceReference]:
    authored = os.fspath(path)
    if not os.path.isabs(authored):
        raise EvidenceError(f"evidence paths must be absolute: {authored}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(authored, flags)
    except OSError as exc:
        raise EvidenceError(f"evidence could not be opened: {authored}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceError(f"evidence must be a regular file: {authored}")
        if metadata.st_size > max_bytes:
            raise EvidenceError(f"evidence exceeds {max_bytes} bytes: {authored}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise EvidenceError(f"evidence exceeds {max_bytes} bytes: {authored}")
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"evidence is not UTF-8 text: {authored}") from exc
    return text, EvidenceReference(
        kind=kind,  # type: ignore[arg-type]
        location=authored,
        sha256=digest(raw),
        size_bytes=len(raw),
    )


def inline_evidence(
    text: str,
    *,
    kind: str,
    location: str | None = None,
    max_bytes: int = MAX_REFERENCE_BYTES,
) -> tuple[str, EvidenceReference]:
    if not isinstance(text, str):
        raise EvidenceError("inline evidence must be text")
    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        raise EvidenceError(f"inline evidence exceeds {max_bytes} bytes")
    return text, EvidenceReference(
        kind=kind,  # type: ignore[arg-type]
        location=location,
        sha256=digest(raw),
        size_bytes=len(raw),
    )


def digest(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def endpoint_identity(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path.rstrip('/')}"


def configured_secrets(config: dict[str, dict[str, str]]) -> tuple[str, ...]:
    return tuple(
        value
        for section in config.values()
        if (value := os.environ.get(section["api_key_env"], ""))
    )


def redact(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "sk-[REDACTED]", text)
    return text


def sanitize(value: object, secrets: tuple[str, ...] = ()) -> str:
    return redact(value, secrets)[:2_000]

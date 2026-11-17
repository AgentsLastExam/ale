"""Wire contracts and HMAC authentication helpers for runtime Task intake."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "DEFAULT_INTAKE_PORT",
    "DEFAULT_MAX_SKEW_SEC",
    "HEADER_DISPATCH_ID",
    "HEADER_IDEMPOTENCY_KEY",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "INTAKE_PROTOCOL_VERSION",
    "INTAKE_RECEIPT_VERSION",
    "INTAKE_STATUS_VERSION",
    "IntakeArtifactMetadata",
    "IntakeAuthorMetadata",
    "IntakeDispatchRequest",
    "IntakeExecutionMode",
    "IntakeReceipt",
    "IntakeReviewMetadata",
    "IntakeStatus",
    "IntakeTaskMetadata",
    "IntakeTaskView",
    "IntakeWebhookPayload",
    "dispatch_hmac_digest",
    "sign_dispatch",
    "sign_webhook",
    "verify_dispatch_signature",
    "verify_webhook_signature",
]

INTAKE_PROTOCOL_VERSION = "ale.task.intake.v1"
INTAKE_RECEIPT_VERSION = "ale.task.intake.receipt.v1"
INTAKE_STATUS_VERSION = "ale.task.intake.status.v1"

DEFAULT_INTAKE_PORT = 7472
DEFAULT_MAX_SKEW_SEC = 300

HEADER_DISPATCH_ID = "X-ALE-Dispatch-Id"
HEADER_IDEMPOTENCY_KEY = "X-ALE-Idempotency-Key"
HEADER_TIMESTAMP = "X-ALE-Timestamp"
HEADER_SIGNATURE = "X-ALE-Signature"

_STRICT = ConfigDict(frozen=True, extra="forbid")
_FLEXIBLE_NESTED = ConfigDict(frozen=True, extra="ignore")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class IntakeExecutionMode(StrEnum):
    """How the intake runtime processes an accepted Task bundle."""

    LINT = "lint"
    PREPARE = "prepare"
    VALIDATE = "validate"
    RUN = "run"


class IntakeStatus(StrEnum):
    """Lifecycle status reported by intake and mirrored by Brewer webhooks."""

    ACKNOWLEDGED = "acknowledged"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        """Whether this intake item has reached a final state."""
        return self in {IntakeStatus.COMPLETED, IntakeStatus.FAILED}


def dispatch_hmac_digest(
    secret: str, timestamp: str, idempotency_key: str, metadata_bytes: bytes
) -> str:
    """Compute the lowercase hex HMAC-SHA256 over ``<ts>.<idempotency_key>.<metadata>``."""
    prefix = f"{timestamp}.{idempotency_key}.".encode()
    return hmac.new(secret.encode("utf-8"), prefix + metadata_bytes, hashlib.sha256).hexdigest()


def sign_dispatch(secret: str, timestamp: str, idempotency_key: str, metadata_bytes: bytes) -> str:
    """Format the ``X-ALE-Signature`` header value for an outbound intake dispatch."""
    return f"sha256={dispatch_hmac_digest(secret, timestamp, idempotency_key, metadata_bytes)}"


def verify_dispatch_signature(
    *,
    secret: str,
    signature_header: str,
    timestamp_header: str,
    idempotency_key: str,
    metadata_bytes: bytes,
    now: float | None = None,
    max_skew_sec: int = DEFAULT_MAX_SKEW_SEC,
) -> tuple[bool, str]:
    """Verify an inbound ``X-ALE-Signature`` dispatch request against ``secret``."""
    if not secret:
        return False, "intake HMAC secret is not configured"
    if not idempotency_key.strip():
        return False, f"missing {HEADER_IDEMPOTENCY_KEY} header"
    raw_ts = timestamp_header.strip()
    if not raw_ts:
        return False, f"missing {HEADER_TIMESTAMP} header"
    try:
        parsed_ts = int(raw_ts)
    except ValueError:
        return False, f"invalid {HEADER_TIMESTAMP} header"
    if max_skew_sec > 0:
        current = time.time() if now is None else now
        if abs(current - parsed_ts) > max_skew_sec:
            return False, f"stale {HEADER_TIMESTAMP} outside +/-{max_skew_sec}s window"
    raw_sig = signature_header.strip()
    if not raw_sig:
        return False, f"missing {HEADER_SIGNATURE} header"
    provided = (
        raw_sig[7:].strip().lower() if raw_sig.lower().startswith("sha256=") else raw_sig.lower()
    )
    expected = dispatch_hmac_digest(secret, raw_ts, idempotency_key.strip(), metadata_bytes)
    if not hmac.compare_digest(provided, expected):
        return False, f"invalid {HEADER_SIGNATURE}"
    return True, ""


def sign_webhook(secret: str, raw_body: bytes) -> str:
    """Compute the ``X-ALE-Signature`` header value for a Brewer status webhook."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(secret: str, signature_header: str, raw_body: bytes) -> bool:
    """Verify an ``X-ALE-Signature`` webhook header against ``raw_body``."""
    if not secret or not signature_header.strip():
        return False
    raw_sig = signature_header.strip()
    provided = (
        raw_sig[7:].strip().lower() if raw_sig.lower().startswith("sha256=") else raw_sig.lower()
    )
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


class IntakeTaskMetadata(BaseModel):
    """Task descriptive fields enclosed in an ``ale.task.intake.v1`` dispatch."""

    model_config = _FLEXIBLE_NESTED

    task_name: str = Field(min_length=1, max_length=255)
    task_repo: str = ""
    domain: str = ""
    title: str = ""
    summary: str = ""
    submission_id: str = ""
    job_id: str = ""
    source_type: str = "web_ui"
    source_skill: str = ""
    source_ref: str = ""


class IntakeAuthorMetadata(BaseModel):
    """Originating author identity attached by Brewer."""

    model_config = _FLEXIBLE_NESTED

    id: str = ""
    email: str = ""
    name: str = ""


class IntakeReviewMetadata(BaseModel):
    """Human and automated review gate context attached by Brewer."""

    model_config = _FLEXIBLE_NESTED

    decision: str = "approve"
    reviewed_by: str = ""
    review_note: str = ""
    ai_review: dict[str, Any] | None = None


class IntakeArtifactMetadata(BaseModel):
    """Sealed bundle metadata and optional signed pull URL."""

    model_config = _FLEXIBLE_NESTED

    id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=512)
    sha256: str = Field(min_length=64, max_length=71)
    size_bytes: int = Field(ge=0)
    download_url: str = ""
    offline_report: dict[str, Any] | None = None
    tree_manifest: dict[str, Any] | None = None

    # --- validation ---

    @field_validator("sha256")
    @classmethod
    def _normalize_sha256(cls, value: str) -> str:
        normalized = value.strip().lower().removeprefix("sha256:")
        if not _SHA256_HEX.fullmatch(normalized):
            raise ValueError("artifact.sha256 must be a 64-character hexadecimal SHA-256 digest")
        return normalized


class IntakeDispatchRequest(BaseModel):
    """Inbound payload delivered to ``POST /v1/intake/tasks``."""

    model_config = _STRICT

    schema_version: Literal["ale.task.intake.v1"] = INTAKE_PROTOCOL_VERSION
    dispatch_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    submitted_at: str = ""
    task: IntakeTaskMetadata
    author: IntakeAuthorMetadata = IntakeAuthorMetadata()
    review: IntakeReviewMetadata = IntakeReviewMetadata()
    artifact: IntakeArtifactMetadata
    status_webhook_url: str | None = None
    execution_mode: IntakeExecutionMode | None = None


class IntakeReceipt(BaseModel):
    """Synchronous acceptance receipt returned by ``POST /v1/intake/tasks``."""

    model_config = _STRICT

    schema_version: Literal["ale.task.intake.receipt.v1"] = INTAKE_RECEIPT_VERSION
    id: str = Field(min_length=1)
    ale_task_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    runtime: str = "ale"
    dispatch_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: IntakeStatus
    created: bool
    execution_mode: IntakeExecutionMode
    status_url: str = Field(min_length=1)
    received_at: str = Field(min_length=1)


class IntakeTaskView(BaseModel):
    """Current status and execution summary returned by ``GET /v1/intake/tasks/{ref}``."""

    model_config = _STRICT

    schema_version: Literal["ale.task.intake.status.v1"] = INTAKE_STATUS_VERSION
    id: str = Field(min_length=1)
    ale_task_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    runtime: str = "ale"
    dispatch_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: IntakeStatus
    execution_mode: IntakeExecutionMode
    attempt_count: int = Field(ge=0)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    detail: str = ""
    result: dict[str, Any] | None = None
    received_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    started_at: str | None = None
    completed_at: str | None = None
    webhook_delivered_at: str | None = None
    webhook_last_error: str = ""


class IntakeWebhookPayload(BaseModel):
    """Outbound status callback sent to Brewer ``POST /api/v1/webhooks/ale-status``."""

    model_config = _STRICT

    dispatch_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    status: IntakeStatus
    ale_task_id: str | None = Field(default=None, max_length=255)
    detail: str = Field(default="", max_length=4000)
    response_body: dict[str, Any] | None = None

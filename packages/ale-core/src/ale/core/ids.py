"""Task identifiers and canonical serialization.

An identifier is ``<domain>/[<group>/]<task>[@<variant>]``. It is derived from a task's
path inside its repository and never written into a manifest, so moving a folder is the
only way to rename a task — there is no second source of truth to drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = ["TaskId", "canonical_json", "content_hash"]

_SEGMENT = r"[a-z0-9][a-z0-9_]*"
# At least two segments: a bare segment names a domain, not a task.
_TASK_ID = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})+(?:@{_SEGMENT})?$")


class TaskId(str):
    """A validated task identifier.

    Subclasses ``str`` so it serialises transparently and can be used as a dict key.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        if not _TASK_ID.match(value):
            raise ValueError(
                f"invalid task id {value!r}: expected <domain>/[<group>/]<task>[@<variant>] "
                f"with lowercase segments"
            )
        if value.count("@") > 1:
            raise ValueError(f"invalid task id {value!r}: at most one variant suffix")
        return super().__new__(cls, value)

    @property
    def domain(self) -> str:
        """The first segment: the namespace that maps to a task repository."""
        return self.split("/", 1)[0]

    @property
    def variant(self) -> str | None:
        """The variant name, or ``None`` for a single-variant task."""
        _, _, variant = self.partition("@")
        return variant or None

    @property
    def family(self) -> str:
        """The identifier without its variant suffix — the aggregation key."""
        return self.partition("@")[0]

    def with_variant(self, name: str) -> TaskId:
        """Return this identifier with ``name`` as its variant."""
        return TaskId(f"{self.family}@{name}")

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Validate from a plain string and serialise back to one."""
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.to_string_ser_schema(),
        )


def canonical_json(value: Any) -> str:
    """Serialise ``value`` deterministically.

    Sorted keys, no insignificant whitespace, UTF-8 preserved. Every hash in ALE is
    computed over this form, so two processes always agree on what a value *is*.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    """Return ``sha256:<hex>`` over the canonical JSON form of ``value``."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"

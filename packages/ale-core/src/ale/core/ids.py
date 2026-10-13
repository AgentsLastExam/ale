"""Task identifiers and canonical serialization.

A Task identifier is the explicit ``name`` in ``task.yaml``: an opaque, unique, readable
slug that is never derived from or parsed into a folder path.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = ["TaskId", "canonical_json", "content_hash"]

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TaskId(str):
    """A validated, opaque task identifier.

    Subclasses ``str`` so it serialises transparently and works as a mapping key.
    Deliberately has no accessors: anything the framework needs to *know* about a task
    — its domain, its variant, where its data lives — is a field somewhere, never a
    substring here.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        if not _SLUG.match(value):
            raise ValueError(
                f"invalid task id {value!r}: expected a flat lowercase slug such as "
                f"'demo-hello' (letters, digits, '-' and '_')"
            )
        return super().__new__(cls, value)

    # --- validation ---

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

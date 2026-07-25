"""Task identifiers and canonical serialization.

A task identifier is an **opaque label**: a flat slug derived once from the task's
folder path (``tasks/demo/hello`` → ``demo-hello``) and never parsed afterwards.

That rule is load-bearing. The previous framework encoded a task's path into data
locations and into prompts, so renaming a domain or regrouping tasks rippled through
sandbox layouts, cached data and instruction text. Here the domain, the variant and
every storage location are their own fields; the identifier only has to be unique and
readable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = ["TaskId", "canonical_json", "content_hash", "slugify_path"]

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


def slugify_path(relative_path: str) -> TaskId:
    """Flatten a task folder path into an identifier.

    ``demo/hello`` → ``demo-hello``; ``uav/control/drone_hover`` →
    ``uav-control-drone_hover``. Directory depth stops mattering the moment the
    identifier exists, so domains organise their folders however suits them.
    """
    parts = [part for part in relative_path.strip("/").split("/") if part]
    if not parts:
        raise ValueError("cannot derive a task id from an empty path")
    return TaskId("-".join(parts))


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

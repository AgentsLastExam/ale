"""JSON boundaries shared by results and both Judge adapters."""

from __future__ import annotations

import json
import re
from typing import Any

import fastjsonschema


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def json_object(raw: str | bytes) -> dict[str, Any]:
    value = json.loads(raw, object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    json.dumps(value, allow_nan=False)
    return value


def schema_validator(schema: dict[str, Any] | None):  # type: ignore[no-untyped-def]
    if schema is None:
        return lambda value: value
    if not isinstance(schema, dict):
        raise ValueError("output_schema must be a JSON Schema object")
    dialect = schema.get("$schema", "http://json-schema.org/draft-07/schema#")
    if dialect not in {
        f"{scheme}://json-schema.org/draft-{draft}/schema#"
        for scheme in ("http", "https")
        for draft in ("04", "06", "07")
    }:
        raise ValueError("output_schema supports JSON Schema drafts 04, 06 and 07")

    def check_refs(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None and (
                not isinstance(reference, str) or not reference.startswith("#")
            ):
                raise ValueError("output_schema references must be local fragments")
            for item in value.values():
                check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)

    check_refs(schema)

    # Schema resolution is self-contained, reproducible and never downloads code/data.
    def reject_remote(uri: str) -> None:
        raise ValueError(f"external schema references are unsupported: {uri}")

    return fastjsonschema.compile(
        schema,
        handlers={"http": reject_remote, "https": reject_remote, "file": reject_remote},
        use_default=False,
        use_formats=False,
    )


def response_object(text: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, flags=re.S)
    value = json_object(fenced.group(1) if fenced else text)
    try:
        schema_validator(schema)(value)
    except fastjsonschema.JsonSchemaException as exc:
        raise ValueError(exc.message) from exc
    return value

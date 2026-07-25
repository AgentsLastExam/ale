"""Strict instruction rendering.

Instructions carry `${param}` placeholders and nothing else. Three rules matter:

1. ``${name}`` syntax (``string.Template``), not ``{name}`` — instructions routinely
   contain JSON and code, and brace-based formatting mangles them.
2. Rendering is strict in both directions: an unresolved placeholder *and* an unused
   declared parameter are errors. Converting a large legacy corpus produces both kinds
   of typo, and a silent one becomes a wrong prompt in a scored run.
3. Legacy path templating is rejected outright. Paths are literal now (see
   ``docs/adr/0005-workspace-and-templating.md``); a leftover ``{self.input_dir}`` in a
   converted task is a conversion bug, not a style issue.
"""

from __future__ import annotations

import re
from string import Template
from typing import Any

from ale.core.errors import TaskDefinitionError

__all__ = ["find_legacy_patterns", "render_instruction"]

# Patterns that only appear in prompts written against the legacy path-templating model.
_LEGACY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("legacy Python attribute interpolation", re.compile(r"\{self\.[A-Za-z_][A-Za-z0-9_]*\}")),
    ("legacy Windows task root", re.compile(r"[Ee]:\\+agenthle", re.IGNORECASE)),
    ("legacy Linux task root", re.compile(r"/media/user/data/agenthle")),
)

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def find_legacy_patterns(text: str) -> list[str]:
    """Return human-readable descriptions of legacy templating found in ``text``."""
    return [name for name, pattern in _LEGACY_PATTERNS if pattern.search(text)]


def render_instruction(text: str, params: dict[str, Any], *, where: str = "instruction") -> str:
    """Render ``text`` with ``params``.

    Raises:
        TaskDefinitionError: on a legacy path pattern, an unresolved placeholder, an
            unused declared parameter, or a malformed ``$`` escape.
    """
    legacy = find_legacy_patterns(text)
    if legacy:
        raise TaskDefinitionError(
            f"{where} still uses {', '.join(legacy)}; paths are literal now "
            f"(see docs/adr/0005-workspace-and-templating.md)"
        )

    used = set(_PLACEHOLDER.findall(text))
    declared = set(params)

    if missing := sorted(used - declared):
        raise TaskDefinitionError(
            f"{where} references undeclared parameter(s): {', '.join(missing)}"
        )
    if unused := sorted(declared - used):
        raise TaskDefinitionError(
            f"{where} declares unused parameter(s): {', '.join(unused)}; remove them or use them"
        )

    try:
        return Template(text).substitute(params)
    except ValueError as exc:  # malformed placeholder, e.g. a bare '$'
        raise TaskDefinitionError(f"{where} has malformed template syntax: {exc}") from exc

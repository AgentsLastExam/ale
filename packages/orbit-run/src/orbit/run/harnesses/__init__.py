"""Agent harnesses.

Two families, distinguished by who owns the interaction loop:

* autonomous — the agent runs its own loop; we supply a prompt and accept the result.
* policy — the framework owns the observe/act loop and asks the harness per step.

Harnesses reach sandboxes through the ``ale.core`` contract, never through a provider.
"""

from __future__ import annotations

import functools
import importlib
import pkgutil


@functools.cache
def credential_files() -> tuple[str, ...]:
    """The credential files every Harness here declares, relative to the agent's home.

    Sanitation removes them all, whichever Harness ran: a solver kept or saved may hold
    another Harness's from an earlier episode it continued.
    """
    from ale.core.harness import Harness

    for module in pkgutil.iter_modules(__path__):
        if module.ispkg:
            importlib.import_module(f"{__name__}.{module.name}")
    found: set[str] = set()
    pending = [Harness]
    while pending:
        for child in pending.pop().__subclasses__():
            found.update(child.credentials)
            pending.append(child)
    return tuple(sorted(found))

"""ALE core contracts.

This package defines every shape that crosses a boundary in ALE: task
specifications, evaluation results, traces, provenance records, and the abstract
interfaces that providers, harnesses and environments implement.

Nothing here may import ``ale.run`` (Constitution IV: structural decoupling) and
nothing here may depend on a third-party evaluation framework (Constitution I:
contracts first).
"""

from ale.core.taskspec import ImageKind, ImageSpec
from ale.core.validation import (
    TaskValidationObservation,
    ValidationAttempt,
    ValidationEngine,
    ValidationNotice,
    ValidationObservation,
)

__all__ = [
    "ImageKind",
    "ImageSpec",
    "TaskValidationObservation",
    "ValidationAttempt",
    "ValidationEngine",
    "ValidationNotice",
    "ValidationObservation",
    "__version__",
]

__version__ = "0.1.0"

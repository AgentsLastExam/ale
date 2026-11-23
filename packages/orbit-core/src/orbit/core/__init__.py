"""ALE core contracts.

This package defines every shape that crosses a boundary in ALE: task
specifications, evaluation results, traces, provenance records, and the abstract
interfaces that providers and harnesses implement.

Nothing here may import ``ale.run`` (Constitution II: structural decoupling) and
nothing here may depend on a third-party evaluation framework (Constitution I:
contracts first).
"""

from ale.core.calibration import (
    CalibrationAttempt,
    CalibrationCheck,
    CalibrationEngine,
    CalibrationNotice,
    CalibrationObservation,
    CaseCalibrationObservation,
    TaskCalibrationObservation,
)
from ale.core.taskspec import ImageKind, ImageSpec

__all__ = [
    "CalibrationAttempt",
    "CalibrationCheck",
    "CalibrationEngine",
    "CalibrationNotice",
    "CalibrationObservation",
    "CaseCalibrationObservation",
    "ImageKind",
    "ImageSpec",
    "TaskCalibrationObservation",
    "__version__",
]

__version__ = "0.2.0"

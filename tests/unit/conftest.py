"""Fixtures the unit tests share."""

from __future__ import annotations

from typing import Any

import pytest
from tests.support import MemorySandbox, Verifiers

from ale.core.sandbox import ExecResult


@pytest.fixture
def verifiers(monkeypatch: pytest.MonkeyPatch) -> Verifiers:
    """``validation/`` and ``tests/`` scored in every :class:`MemorySandbox`, as
    :class:`Verifiers` scores them."""
    scoring = Verifiers()
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if not await scoring(self, argv, options):
            return ExecResult(exit_code=1, stderr="verify failed")
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    return scoring

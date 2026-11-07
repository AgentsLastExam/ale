"""The desktop screenshot test covers every CLI Harness in every Gateway dialect it accepts."""

from __future__ import annotations

from tests.integration.cli_desktop import CASES, CLIS, cli_harnesses, dialects


def test_every_cli_harness_says_how_a_model_reaches_the_screenshot() -> None:
    assert [harness.name for harness in cli_harnesses() if harness not in CLIS] == []


def test_every_dialect_a_cli_accepts_is_a_case() -> None:
    for harness, reach in CLIS.items():
        for dialect in dialects(harness, reach):
            assert f"{harness.name}-{dialect}" in CASES

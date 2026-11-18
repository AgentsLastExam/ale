"""The web tools test covers every CLI Harness."""

from __future__ import annotations

from tests.integration.cli_desktop import cli_harnesses
from tests.integration.cli_web_tools import SEARCHES


def test_every_cli_harness_says_how_a_model_attempts_a_provider_search() -> None:
    assert [harness.name for harness in cli_harnesses() if harness not in SEARCHES] == []

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale.core.errors import TrajectoryConversionError
from ale.run.environments.standard import _exact_token_metrics

pytestmark = pytest.mark.unit


def test_exact_token_evidence_is_consumed_without_retokenization(tmp_path: Path) -> None:
    path = tmp_path / "logs/gateway/tokens/call-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "prompt_token_ids": [1, 2],
                "completion_token_ids": [3],
                "logprobs": [-0.1],
                "sampled_mask": [False, False, True],
                "temperature": 0.7,
            }
        )
    )

    metrics = _exact_token_metrics(tmp_path, "call-1")

    assert metrics is not None
    assert metrics.prompt_token_ids == [1, 2]
    assert metrics.completion_token_ids == [3]
    assert metrics.logprobs == [-0.1]
    assert metrics.extra == {
        "ale": {
            "sampled_mask": [False, False, True],
            "temperature": 0.7,
        }
    }
    assert not path.exists()


def test_invalid_exact_token_evidence_is_retained_for_diagnosis(tmp_path: Path) -> None:
    path = tmp_path / "logs/gateway/tokens/call-1.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"prompt_token_ids":"not-a-list"}')

    with pytest.raises(TrajectoryConversionError, match="invalid exact token evidence"):
        _exact_token_metrics(tmp_path, "call-1")

    assert path.is_file()

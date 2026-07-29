from __future__ import annotations

import pytest

from ale.core.errors import IncompleteTrainingDataError
from ale.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifMetrics,
    AtifStep,
    AtifTrajectory,
)
from ale.run.exporters import prime_rl_samples

pytestmark = pytest.mark.unit


def trajectory(metrics: AtifMetrics) -> AtifTrajectory:
    return AtifTrajectory(
        agent=AtifAgent(name="agent", version="1", model_name="model"),
        steps=[
            AtifStep(step_id=1, source="user", message="prompt"),
            AtifStep(step_id=2, source="agent", message="answer", metrics=metrics),
        ],
    )


def exact_metrics() -> AtifMetrics:
    return AtifMetrics(
        prompt_token_ids=[1, 2],
        completion_token_ids=[3, 4],
        logprobs=[-0.1, -0.2],
        extra={
            "ale": {
                "sampled_mask": [False, False, True, True],
                "temperature": 0.7,
            }
        },
    )


def test_exact_training_data_converts_without_retokenization() -> None:
    (sample,) = prime_rl_samples(trajectory(exact_metrics()), env_name="ale")
    assert sample.token_ids == [1, 2, 3, 4]
    assert sample.mask == [False, False, True, True]
    assert sample.logprobs == [0.0, 0.0, -0.1, -0.2]
    assert sample.temperatures == [0.7] * 4


def test_multimodal_training_data_requires_and_preserves_exact_processor_state() -> None:
    metrics = exact_metrics().model_copy(
        update={
            "extra": {
                "ale": {
                    "sampled_mask": [False, False, True, True],
                    "temperature": 0.7,
                    "mm_kwargs": {"pixel_values": "provider-owned-reference"},
                    "mm_token_type_ids": [1, 1, 0, 0],
                }
            }
        }
    )
    multimodal = trajectory(metrics)
    multimodal = multimodal.model_copy(
        update={
            "steps": [
                multimodal.steps[0].model_copy(
                    update={
                        "message": [
                            AtifContentPart(type="text", text="prompt"),
                            AtifContentPart(
                                type="image",
                                source=AtifImageSource(
                                    media_type="image/png",
                                    path="blobs/image/example.png",
                                ),
                            ),
                        ]
                    }
                ),
                multimodal.steps[1],
            ]
        }
    )

    (sample,) = prime_rl_samples(multimodal)

    assert sample.mm_kwargs == {"pixel_values": "provider-owned-reference"}
    assert sample.mm_token_type_ids == [1, 1, 0, 0]


def test_multimodal_training_data_rejects_missing_processor_state() -> None:
    multimodal = trajectory(exact_metrics())
    multimodal = multimodal.model_copy(
        update={
            "steps": [
                multimodal.steps[0].model_copy(
                    update={
                        "message": [
                            AtifContentPart(
                                type="image",
                                source=AtifImageSource(
                                    media_type="image/png",
                                    path="blobs/image/example.png",
                                ),
                            )
                        ]
                    }
                ),
                multimodal.steps[1],
            ]
        }
    )

    with pytest.raises(IncompleteTrainingDataError, match="multimodal processor"):
        prime_rl_samples(multimodal)


@pytest.mark.parametrize(
    "metrics",
    [
        AtifMetrics(),
        exact_metrics().model_copy(update={"logprobs": [-0.1]}),
        exact_metrics().model_copy(
            update={"extra": {"ale": {"sampled_mask": [True], "temperature": 0.7}}}
        ),
    ],
)
def test_incomplete_or_misaligned_training_data_fails(metrics: AtifMetrics) -> None:
    with pytest.raises(IncompleteTrainingDataError):
        prime_rl_samples(trajectory(metrics))

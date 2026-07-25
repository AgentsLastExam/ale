"""Contract behaviour that everything else depends on."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ale.core.errors import TaskDefinitionError
from ale.core.ids import TaskId, canonical_json, content_hash
from ale.core.taskspec import (
    ImageRef,
    NetworkMode,
    NetworkPolicy,
    ScriptStep,
    TaskSpec,
    ValidateSpec,
)
from ale.core.template import render_instruction
from ale.core.verdict import Status, Verdict

pytestmark = pytest.mark.unit


def make_spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = {
        "id": TaskId("demo/hello"),
        "instruction": "Write hello into /ale/output/result.txt",
        "image": ImageRef(name="sandbox-base-cli", tag="0.1.0"),
    }
    return TaskSpec(**(base | overrides))  # type: ignore[arg-type]


class TestTaskId:
    def test_parses_domain_family_and_variant(self) -> None:
        tid = TaskId("robotics/uav/drone_hover@hard")
        assert tid.domain == "robotics"
        assert tid.family == "robotics/uav/drone_hover"
        assert tid.variant == "hard"

    def test_single_variant_task_has_no_suffix(self) -> None:
        tid = TaskId("demo/hello")
        assert tid.variant is None
        assert tid.family == "demo/hello"

    @pytest.mark.parametrize(
        "bad", ["Demo/hello", "demo", "demo//hello", "demo/hello@a@b", "demo/hello@", "-x/y"]
    )
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError):
            TaskId(bad)


class TestCanonicalHashing:
    def test_key_order_does_not_change_the_hash(self) -> None:
        assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})

    def test_canonical_json_is_compact_and_sorted(self) -> None:
        assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'

    def test_spec_hash_tracks_the_rendered_instruction(self) -> None:
        one = make_spec()
        two = make_spec(instruction="Write goodbye into /ale/output/result.txt")
        assert one.spec_hash != two.spec_hash
        assert one.spec_hash == make_spec().spec_hash

    def test_variants_have_distinct_identities(self) -> None:
        base = make_spec(id=TaskId("demo/hello@base"))
        hard = make_spec(id=TaskId("demo/hello@hard"))
        assert base.family == hard.family == "demo/hello"
        assert base.spec_hash != hard.spec_hash


class TestTaskSpec:
    def test_defaults_are_deny_by_default(self) -> None:
        spec = make_spec()
        assert spec.network.mode is NetworkMode.BLOCK
        assert spec.validate_.mode == "oracle"

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            make_spec(nonsense=True)

    def test_is_immutable(self) -> None:
        spec = make_spec()
        with pytest.raises(ValidationError):
            spec.instruction = "changed"  # type: ignore[misc]

    def test_round_trips_through_json(self) -> None:
        spec = make_spec(setup=(ScriptStep(script="setup/prepare.sh"),))
        restored = TaskSpec.model_validate_json(spec.model_dump_json(by_alias=True))
        assert restored == spec
        assert restored.spec_hash == spec.spec_hash

    def test_allowlist_requires_hosts(self) -> None:
        with pytest.raises(ValidationError):
            NetworkPolicy(mode=NetworkMode.ALLOWLIST)
        with pytest.raises(ValidationError):
            NetworkPolicy(mode=NetworkMode.BLOCK, allowed_hosts=("example.com",))

    def test_manual_validation_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError):
            ValidateSpec(mode="manual")
        assert ValidateSpec(mode="manual", reason="human judged").reason


class TestTemplate:
    def test_renders_declared_parameters(self) -> None:
        assert render_instruction("Solve ${n} cases", {"n": 3}) == "Solve 3 cases"

    def test_leaves_json_braces_alone(self) -> None:
        text = 'Write {"ok": true} and ${n}'
        assert render_instruction(text, {"n": 1}) == 'Write {"ok": true} and 1'

    def test_rejects_undeclared_placeholder(self) -> None:
        with pytest.raises(TaskDefinitionError, match="undeclared"):
            render_instruction("Solve ${n}", {})

    def test_rejects_unused_parameter(self) -> None:
        with pytest.raises(TaskDefinitionError, match="unused"):
            render_instruction("no placeholders", {"n": 3})

    @pytest.mark.parametrize(
        "legacy",
        [
            "Read {self.input_dir}/data.csv",
            r"Write to E:\agenthle\out",
            "Look in /media/user/data/agenthle",
        ],
    )
    def test_rejects_legacy_path_templating(self, legacy: str) -> None:
        with pytest.raises(TaskDefinitionError, match="legacy"):
            render_instruction(legacy, {})


class TestVerdict:
    def test_completed_requires_rewards(self) -> None:
        with pytest.raises(ValidationError):
            Verdict(status=Status.COMPLETED)

    def test_failed_requires_failure_info(self) -> None:
        with pytest.raises(ValidationError):
            Verdict(status=Status.TASK_ERROR)

    def test_primary_reward_resolves(self) -> None:
        verdict = Verdict.completed({"reward": 0.75, "steps": 4.0})
        assert verdict.primary_reward == 0.75
        assert verdict.status.is_scored

    def test_primary_must_exist_in_rewards(self) -> None:
        with pytest.raises(ValidationError):
            Verdict.completed({"score": 1.0}, primary="reward")

    def test_failures_are_not_scored(self) -> None:
        verdict = Verdict.failed(Status.TASK_ERROR, ValueError("bad json"), phase="verify")
        assert not verdict.status.is_scored
        assert verdict.primary_reward is None
        assert verdict.failure is not None
        assert verdict.failure.error_class == "ValueError"
        assert verdict.failure.phase == "verify"

    def test_task_error_is_distinct_from_zero_reward(self) -> None:
        zero = Verdict.completed({"reward": 0.0})
        broken = Verdict.failed(Status.TASK_ERROR, ValueError("no rewards file"))
        assert zero.status is Status.COMPLETED
        assert zero.primary_reward == 0.0
        assert broken.primary_reward is None

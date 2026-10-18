from __future__ import annotations

from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ale.core.config import RunConfig, VerificationConfig
from ale.core.errors import ConfigError
from ale.run import verification
from ale.run.scaffold import scaffold_task
from ale.run.verification import judge_kinds, preflight_judges
from ale_verify import ConfigurationError

pytestmark = pytest.mark.unit

LLM = {
    "model": "judge-model",
    "reasoning_effort": "medium",
    "base_url": "https://judge.test",
    "api_key_env": "JUDGE_KEY",
}


def verifier(root: Path, source: str) -> Path:
    directory = root / "verify"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "verify.py").write_text(source)
    return directory


def test_judge_kinds_are_read_from_the_verifier_source(tmp_path: Path) -> None:
    llm = verifier(tmp_path / "a", 'v.judge("llm", "quality", prompt="p", rubric=r)\n')
    agent = verifier(tmp_path / "b", 'v.judge(kind="agent", name="works")\n')
    plain = verifier(tmp_path / "c", 'v.check("exists", result)\n')
    broken = verifier(tmp_path / "d", "def broken(:\n")
    assert judge_kinds([llm]) == {"llm"}
    assert judge_kinds([agent, plain, broken]) == {"agent"}
    assert judge_kinds([plain, broken]) == frozenset()


def test_a_judge_kind_chosen_at_run_time_counts_as_both(tmp_path: Path) -> None:
    dynamic = verifier(tmp_path, "for kind in kinds:\n    v.judge(kind, name)\n")
    assert judge_kinds([dynamic]) == {"llm", "agent"}


def test_structured_judge_requires_only_the_llm_judge(tmp_path: Path) -> None:
    directory = verifier(tmp_path, 'v.judge_json("answers", prompt="p")\n')
    assert judge_kinds([directory]) == {"llm"}


@pytest.mark.asyncio
async def test_preflight_probes_each_used_kind_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed = []
    monkeypatch.setattr(verification, "probe_judge", lambda kind, config: probed.append(kind))
    dirs = [verifier(tmp_path / name, 'v.judge("llm", "q")\n') for name in ("a", "b")]
    await preflight_judges(dirs, VerificationConfig.model_validate({"llm": LLM}))
    assert probed == ["llm"]


@pytest.mark.asyncio
async def test_preflight_rejects_a_missing_or_unreachable_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs = [verifier(tmp_path, 'v.judge("llm", "q")\n')]
    with pytest.raises(ConfigError, match=r"verification\.llm is not configured"):
        await preflight_judges(dirs, VerificationConfig())

    def unreachable(kind: str, config: dict[str, str]) -> None:
        raise ConfigurationError("Judge endpoint judge.test is unavailable")

    monkeypatch.setattr(verification, "probe_judge", unreachable)
    with pytest.raises(ConfigError, match=r"llm Judge preflight failed: .*unavailable"):
        await preflight_judges(dirs, VerificationConfig.model_validate({"llm": LLM}))


@pytest.mark.asyncio
async def test_run_stops_before_any_image_or_agent_when_its_judge_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task_cli = import_module("ale.run.cli.tasks")
    task_path = scaffold_task(tmp_path / "task")
    (task_path / "verify" / "judged.py").write_text('v.judge("llm", "quality")\n')
    prepare = AsyncMock(side_effect=AssertionError("must not prepare images"))
    monkeypatch.setattr(task_cli, "_prepare_images", prepare)
    code = await task_cli._run_one(str(task_path), RunConfig(), tmp_path / "runs")
    assert code == task_cli.EXIT_BAD_REFERENCE
    assert "verification.llm is not configured" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()

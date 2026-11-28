"""The dispatcher's side of running episodes as batch jobs, against a scheduler stand-in."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import ClassVar

import pytest

from orbit.core.config import RunConfig, SlurmProviderConfig
from orbit.core.result import ResultRecord
from orbit.core.verdict import Status
from orbit.run.providers.cluster import episodes

pytestmark = pytest.mark.unit


class FakeJob:
    """A job whose run is the ``behaviour`` the test gives it, as soon as it is submitted."""

    made: ClassVar[list[FakeJob]] = []

    def __init__(self, root: Path, behaviour) -> None:
        self.name = f"orbit-{len(FakeJob.made):04d}"
        self.directory = root / self.name
        self.id = str(100 + len(FakeJob.made))
        self.workload_manager = "slurm"
        self.job_label = f"slurm job {self.id}"
        self.behaviour = behaviour
        self.submitted: dict | None = None
        self.stopped = False
        FakeJob.made.append(self)

    def stage(self, spec, *, name="allocation.json") -> None:
        self.directory.mkdir(mode=0o700)

    async def submit(self, **kwargs) -> None:
        self.submitted = kwargs
        self.behaviour(self)

    async def state(self) -> str:
        return ""

    async def ended(self) -> str:
        return "NODE_FAIL"

    async def pending_reason(self) -> str:
        return "Resources"

    async def stop(self, *, cancel: bool = False) -> None:
        self.stopped = True
        self.cancelled = cancel


def _spec(tmp_path: Path, **update) -> episodes.EpisodeJobSpec:
    return episodes.EpisodeJobSpec(
        run_dir=str(tmp_path / "runs" / "r1"),
        episode_id="org.task-0001",
        task=str(tmp_path / "task"),
        task_digest="sha256:" + "0" * 64,
        config=RunConfig().model_dump(mode="json"),
        provenance={},
        prepared={},
        validation_rounds=0,
        cpus=2,
        memory_mb=4096,
        gpus=0,
        nodes=1,
        walltime_minutes=60,
        **update,
    )


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    FakeJob.made = []
    behaviours: list = []

    def create_job(config, partition):
        return FakeJob(tmp_path / "cache", behaviours.pop(0))

    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(episodes, "create_job", create_job)
    monkeypatch.setattr(episodes, "SETTLE_SEC", 0)
    monkeypatch.setattr(episodes, "POLL_SEC", 0)
    config = SlurmProviderConfig(shared_fs_dir=str(tmp_path / "cache"))
    size = type(
        "Size", (), {"partition": None, "cpus": 3, "memory_mb": 6656, "gpus": 0, "nodes": 1}
    )
    return episodes.Dispatcher(config, Path("/engine/python3")), size, behaviours


def _finishes(kind: str, result: bool = False):
    def run(job: FakeJob) -> None:
        spec = episodes.read_spec(job.directory)
        handed = job.directory / episodes.CONTINUATION
        job.received = json.loads(handed.read_text()) if spec.continuation else None
        if result:
            episode = Path(spec.run_dir) / spec.episode_id
            episode.mkdir(parents=True, exist_ok=True)
            (episode / "result.json").write_text("{}")
        if kind == "continuation":
            (job.directory / episodes.CONTINUATION).write_text(
                json.dumps({"cut": len(FakeJob.made)})
            )
        episodes.finish(job.directory, kind, "it broke" if kind == "error" else "")

    return run


async def test_an_attempt_is_one_job_that_writes_the_result(tmp_path, dispatch):
    dispatcher, size, behaviours = dispatch
    behaviours.append(_finishes("result", result=True))
    await dispatcher.attempt(_spec(tmp_path), size, "ORBIT_X_API_KEY=k\n")
    (job,) = FakeJob.made
    assert job.submitted["argv"] == [
        "/engine/python3", "-I", "-m", "orbit.run.cli", "episode-job", ".",
    ]  # fmt: skip
    assert (job.submitted["cpus"], job.submitted["memory_mb"], job.submitted["minutes"]) == (
        3,
        6656,
        60,
    )
    credentials = job.directory / episodes.CREDENTIALS
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    # It wrote its outcome and ends by itself: it is waited for, not cancelled.
    assert job.stopped and not job.cancelled


async def test_each_hand_off_continues_in_a_new_job_from_the_last_continuation(tmp_path, dispatch):
    dispatcher, size, behaviours = dispatch
    behaviours += [
        _finishes("continuation"),
        _finishes("continuation"),
        _finishes("result", result=True),
    ]
    await dispatcher.attempt(_spec(tmp_path), size, "")
    first, second, third = FakeJob.made
    assert episodes.read_spec(first.directory).continuation is None
    assert first.received is None
    for job, cut in ((second, 1), (third, 2)):
        assert episodes.read_spec(job.directory).continuation == episodes.CONTINUATION
        assert job.received == {"cut": cut}
        # It holds the episode's tokens.
        handed = job.directory / episodes.CONTINUATION
        assert stat.S_IMODE(handed.stat().st_mode) == 0o600
    assert all(job.stopped for job in FakeJob.made)


async def test_a_job_lost_without_its_outcome_fails_the_attempt(tmp_path, dispatch):
    dispatcher, size, behaviours = dispatch
    behaviours.append(lambda job: None)
    spec = _spec(tmp_path)
    await dispatcher.attempt(spec, size, "")
    record = ResultRecord.model_validate_json(
        (Path(spec.run_dir) / spec.episode_id / "result.json").read_text()
    )
    assert record.status is Status.ENV_ERROR
    assert "NODE_FAIL" in record.failure.message
    assert FakeJob.made[0].cancelled


async def test_a_job_that_fails_says_why_in_the_result(tmp_path, dispatch):
    dispatcher, size, behaviours = dispatch
    behaviours.append(_finishes("error"))
    spec = _spec(tmp_path)
    await dispatcher.attempt(spec, size, "")
    record = ResultRecord.model_validate_json(
        (Path(spec.run_dir) / spec.episode_id / "result.json").read_text()
    )
    assert record.failure.message == "it broke"


def test_credentials_carry_the_env_file_and_the_overrides_of_this_process(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ORBIT_ANTHROPIC_API_KEY=from-file\nTASK_TOKEN=t\n")
    text = episodes.credentials(env, {"ORBIT_ANTHROPIC_API_KEY": "from-env"})
    from orbit.run.secrets import read_env_file

    (tmp_path / "out.env").write_text(text)
    assert read_env_file(tmp_path / "out.env") == {
        "ORBIT_ANTHROPIC_API_KEY": "from-env",
        "TASK_TOKEN": "t",
    }


def test_a_cluster_is_the_runs_only_provider():
    slurm = {"partitions": [{"name": "cpu", "max_cpus": 4, "max_memory_mb": 8192}]}
    RunConfig.model_validate({"sandbox": {"providers": ["slurm"], "slurm": slurm}})
    with pytest.raises(ValueError, match="only Provider"):
        RunConfig.model_validate(
            {"sandbox": {"providers": ["slurm", "docker:runc"], "slurm": slurm}}
        )

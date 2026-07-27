"""Provenance, configuration, tracing and the guest protocol."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ale.core.config import GatewayLimits, RunConfig, load_run_config, merge_layers, parse_override
from ale.core.errors import ConfigError, ProvenanceIncompleteError, ProviderCapabilityError
from ale.core.ids import TaskId
from ale.core.lock import (
    AgentProvenance,
    FrameworkProvenance,
    GatewayProvenance,
    ImageProvenance,
    RunLock,
    TaskProvenance,
    TaskSource,
)
from ale.core.sandbox import Capabilities
from ale.core.taskspec import ImageRef, NetworkMode, NetworkPolicy, Resources, TaskSpec
from ale.core.trace import (
    DesktopAction,
    ExecRecord,
    TimingRecord,
    TraceLayer,
    TraceWriter,
    TransportRecord,
    read_records,
)
from ale.run.episode import _DiscardedArtifacts, _write_timing

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64

GUESTD = Path(__file__).resolve().parents[2] / "packages/ale-run/src/ale/run/guestd/main.py"


def make_lock(**overrides: object) -> RunLock:
    base: dict[str, object] = {
        "task": TaskProvenance(
            id=TaskId("demo-hello"),
            domain="demo",
            spec_hash=DIGEST,
            source=TaskSource(
                kind="registry",
                repo="https://example.invalid/x.git",
                commit="abc",
                path="tasks/demo/hello",
            ),
        ),
        "image": ImageProvenance(ref="sandbox-base-cli:0.1.0", digest=DIGEST),
        "agent": AgentProvenance(
            harness="claude-code",
            family="autonomous",
            version="2.1.170",
            integrity=f"baked:{DIGEST}",
            model="claude-opus-4-8",
        ),
        "framework": FrameworkProvenance(version="0.1.0", commit="deadbee"),
        "gateway": GatewayProvenance(dialect="anthropic"),
        "config_hash": DIGEST,
        "seed": 7,
    }
    return RunLock(**(base | overrides))  # type: ignore[arg-type]


class TestRunLock:
    def test_accepts_a_complete_record(self) -> None:
        make_lock().require_reportable()

    def test_rejects_a_tag_where_a_digest_belongs(self) -> None:
        with pytest.raises(ValidationError):
            ImageProvenance(ref="x:latest", digest="latest")

    def test_registry_source_requires_a_resolved_commit(self) -> None:
        with pytest.raises(ValidationError):
            TaskSource(kind="registry", repo="https://example.invalid/x.git", path="tasks/x")

    def test_local_source_is_not_reportable(self) -> None:
        lock = make_lock(
            task=TaskProvenance(
                id=TaskId("demo-hello"),
                domain="demo",
                spec_hash=DIGEST,
                source=TaskSource(kind="local", path="/home/dev/tasks/hello"),
            )
        )
        with pytest.raises(ProvenanceIncompleteError, match="local path"):
            lock.require_reportable()

    def test_unresolved_framework_commit_is_not_reportable(self) -> None:
        lock = make_lock(framework=FrameworkProvenance(version="0.1.0", commit="unknown"))
        with pytest.raises(ProvenanceIncompleteError, match="commit"):
            lock.require_reportable()


class TestConfigLayering:
    def test_precedence_is_cli_over_run_over_preset(self) -> None:
        merged = merge_layers(
            preset={"agent": {"model": "preset-model", "name": "claude-code"}},
            run={"agent": {"model": "run-model"}},
            overrides=["agent.model=cli-model"],
        )
        assert merged["agent"]["model"] == "cli-model"
        assert merged["agent"]["name"] == "claude-code"  # untouched keys survive

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("agent.kwargs.max_turns=50", 50),
            ("gateway.limits.max_cost_usd=2.5", 2.5),
            ("resume=false", False),
            ("agent.model=claude-opus-4-8", "claude-opus-4-8"),
            ('agent.model="quoted value"', "quoted value"),
        ],
    )
    def test_override_values_are_toml_typed(self, text: str, expected: object) -> None:
        _, value = parse_override(text)
        assert value == expected

    def test_malformed_override_is_an_error(self) -> None:
        with pytest.raises(ConfigError):
            parse_override("no-equals-sign")

    def test_unknown_key_is_an_error_not_a_no_op(self, tmp_path: Path) -> None:
        run = tmp_path / "run.toml"
        run.write_text("[agent]\nmodle = 'typo'\n")  # note the typo
        with pytest.raises(ConfigError):
            load_run_config(run_path=run)

    def test_config_hash_tracks_effective_values(self) -> None:
        one = RunConfig()
        two = RunConfig(gateway={"limits": GatewayLimits(max_cost_usd=1.0)})  # type: ignore[arg-type]
        assert one.config_hash != two.config_hash
        assert one.config_hash == RunConfig().config_hash

    def test_budget_ceilings_are_on_by_default(self) -> None:
        limits = RunConfig().gateway.limits
        assert limits.max_total_tokens and limits.max_cost_usd


class TestCapabilities:
    def test_rejects_unsupported_network_mode(self) -> None:
        caps = Capabilities(network_modes=frozenset({NetworkMode.OPEN}))
        with pytest.raises(ProviderCapabilityError, match="network mode"):
            caps.check(Resources(), NetworkPolicy(mode=NetworkMode.BLOCK))

    def test_accepts_a_satisfiable_request(self) -> None:
        caps = Capabilities(
            gui=True, network_modes=frozenset({NetworkMode.BLOCK}), max_cpus=4, max_memory_mb=8192
        )
        caps.check(Resources(cpus=2, memory_mb=2048), NetworkPolicy())


class TestTrace:
    def test_writes_and_reads_records(self, tmp_path: Path) -> None:
        writer = TraceWriter(tmp_path)
        writer.write_semantic(
            ExecRecord(seq=writer.next_seq(TraceLayer.SEMANTIC), argv_digest=DIGEST, exit_code=0)
        )
        records = list(read_records(writer.path(TraceLayer.SEMANTIC)))
        assert records[0]["kind"] == "exec"
        assert records[0]["seq"] == 0

    def test_torn_final_line_does_not_hide_earlier_records(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.semantic.jsonl"
        path.write_text(json.dumps({"seq": 0, "kind": "note", "message": "ok"}) + '\n{"seq": 1,')
        assert [r["seq"] for r in read_records(path)] == [0]

    def test_desktop_actions_are_typed(self) -> None:
        action = DesktopAction(type="click", coordinate=(512, 340))
        assert action.coordinate == (512, 340)
        with pytest.raises(ValidationError):
            DesktopAction(type="teleport")  # type: ignore[arg-type]


class TestGuestProtocol:
    """The guest service is exercised as a subprocess: that is how it really runs."""

    def _talk(self, *messages: dict[str, object]) -> list[dict[str, object]]:
        payload = "\n".join(json.dumps(m) for m in messages) + "\n"
        proc = subprocess.run(
            [sys.executable, str(GUESTD), "--stdio"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_health_reports_the_protocol_version(self) -> None:
        (reply,) = self._talk({"id": 1, "op": "health"})
        assert reply["ok"] is True
        assert reply["data"]["proto"] == 1  # type: ignore[index]

    def test_exec_streams_output_then_reports_exit_code(self) -> None:
        replies = self._talk({"id": 2, "op": "exec", "params": {"argv": ["echo", "hi"]}})
        assert any(r.get("event") == "stdout_chunk" for r in replies)
        assert replies[-1]["data"]["exit_code"] == 0  # type: ignore[index]

    def test_file_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "probe.txt"
        replies = self._talk(
            {"id": 3, "op": "write_file", "params": {"path": str(target), "b64": "aGVsbG8="}},
            {"id": 4, "op": "read_file", "params": {"path": str(target)}},
        )
        assert target.read_bytes() == b"hello"
        assert any(r.get("event") == "chunk" for r in replies)

    def test_unknown_operation_is_reported_not_fatal(self) -> None:
        replies = self._talk({"id": 5, "op": "nope"}, {"id": 6, "op": "health"})
        assert replies[0]["error"]["code"] == "unsupported"  # type: ignore[index]
        assert replies[1]["ok"] is True  # the session survives

    def test_missing_file_is_a_typed_error(self) -> None:
        (reply,) = self._talk({"id": 7, "op": "read_file", "params": {"path": "/no/such/file"}})
        assert reply["error"]["code"] == "not_found"  # type: ignore[index]


class TestEpisodeTiming:
    """Where the wall clock went — one duration cannot tell you what to fix."""

    def test_shares_sum_to_the_total(self) -> None:
        record = TimingRecord(seq=0, total_ms=1000, model_ms=700, sandbox_ms=200, framework_ms=100)
        assert record.model_ms + record.sandbox_ms + record.framework_ms == record.total_ms

    def test_split_is_computed_from_recorded_evidence(self, tmp_path: Path) -> None:
        """Model time comes from the gateway's own records, so it cannot be asserted."""
        trace = TraceWriter(tmp_path)
        trace.write_transport(
            TransportRecord(
                seq=0,
                episode_id="e",
                model="m",
                request_digest=DIGEST,
                latency_ms=600,
            )
        )
        trace.write_semantic(ExecRecord(seq=0, argv_digest=DIGEST, exit_code=0, duration_ms=150))

        _write_timing(trace, tmp_path, duration_sec=1.0)

        (timing,) = [
            r for r in read_records(trace.path(TraceLayer.SEMANTIC)) if r["kind"] == "timing"
        ]
        assert timing["model_ms"] == 600
        assert timing["sandbox_ms"] == 150
        assert timing["framework_ms"] == 250

    def test_concurrent_work_cannot_produce_a_negative_share(self, tmp_path: Path) -> None:
        """Overlapping calls can sum past the wall clock; the remainder must stay real."""
        trace = TraceWriter(tmp_path)
        for seq in range(3):
            trace.write_transport(
                TransportRecord(
                    seq=seq, episode_id="e", model="m", request_digest=DIGEST, latency_ms=900
                )
            )

        _write_timing(trace, tmp_path, duration_sec=1.0)

        (timing,) = [
            r for r in read_records(trace.path(TraceLayer.SEMANTIC)) if r["kind"] == "timing"
        ]
        assert timing["model_ms"] == 1000
        assert timing["framework_ms"] == 0


class TestArtifactPolicy:
    """Declaring an output and keeping a copy of it are separate decisions."""

    def test_a_task_declares_paths_not_dispositions(self) -> None:
        spec = TaskSpec(
            id=TaskId("demo-hello"),
            domain="demo",
            instruction="write",
            image=ImageRef(name="sandbox-base-cli"),
            artifacts=("/ale/output",),
        )
        assert spec.artifacts == ("/ale/output",)

    def test_the_run_decides_whether_to_keep_them(self) -> None:
        assert RunConfig().artifacts.collect == "host"
        dropped = RunConfig.model_validate({"artifacts": {"collect": "none"}})
        assert dropped.artifacts.collect == "none"

    def test_the_policy_is_part_of_the_run_identity(self) -> None:
        """Two runs that kept different things are not the same run."""
        kept = RunConfig()
        dropped = RunConfig.model_validate({"artifacts": {"collect": "none"}})
        assert kept.config_hash != dropped.config_hash

    @pytest.mark.asyncio
    async def test_discarding_still_satisfies_the_sink(self) -> None:
        """Environments collect unconditionally; the sink is where the policy lives."""
        sink = _DiscardedArtifacts(Path("/nowhere"))
        assert await sink.collect(None, "/ale/output", "output") == sink.path("output")


class TestFailedModelCalls:
    def test_an_upstream_failure_is_still_recorded(self) -> None:
        """Silence would read as an idle agent; it was a provider returning 529s."""
        record = TransportRecord(
            seq=0, episode_id="e", model="m", request_digest=DIGEST, upstream_status=529
        )
        assert record.upstream_status == 529
        assert record.input_tokens == 0  # a failed call bought nothing

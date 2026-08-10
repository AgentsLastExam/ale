"""Provenance, configuration, tracing and the guest protocol."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ale.core.config import (
    AssetCollectionConfig,
    GatewayLimits,
    LoggingPolicy,
    RunConfig,
    ale_repo_path,
    load_run_config,
    merge_layers,
    parse_override,
    resolve_asset_collection,
)
from ale.core.errors import ConfigError, ProvenanceIncompleteError, ProviderCapabilityError
from ale.core.lock import (
    AgentProvenance,
    FrameworkProvenance,
    GatewayProvenance,
    ImageProvenance,
    ResourceProvenance,
    RunLock,
    TaskProvenance,
    TaskSource,
)
from ale.core.sandbox import Capabilities, ImageRef, ResourceAllocation
from ale.core.taskspec import (
    ImageKind,
    ImageSpec,
    NetworkMode,
    NetworkPolicy,
    Resources,
    TaskSpec,
)
from ale.core.trace import TransportCall

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64

GUESTD = Path(__file__).resolve().parents[2] / "packages/ale-run/src/ale/run/guestd/main.py"


def make_lock(**overrides: object) -> RunLock:
    base: dict[str, object] = {
        "task": TaskProvenance(
            name="demo-hello",
            variant="base",
            spec_hash=DIGEST,
            content_digest=DIGEST,
            source=TaskSource(
                kind="registry",
                repo="https://example.invalid/x.git",
                commit="abc",
                path="tasks/demo/hello",
            ),
        ),
        "image": ImageProvenance(
            declaration=ImageSpec(kind="container"),
            source="local",
            input_identity=DIGEST,
            image_source_identity=DIGEST,
            prepared_identity=DIGEST,
            runtime_ref="ale-task:fixture",
            provider="docker",
            observed_identity=DIGEST,
            observed_ref="ale-task:fixture",
        ),
        "resources": ResourceProvenance(
            requested=Resources(),
            effective=ResourceAllocation(
                cpus=1,
                memory_mb=1024,
                sudo=False,
                network_mode="block",
                provider="docker",
            ),
        ),
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
            ImageProvenance(
                declaration=ImageSpec(kind="container"),
                source="local",
                input_identity="latest",
                image_source_identity=DIGEST,
                prepared_identity=DIGEST,
                runtime_ref="x:latest",
                provider="docker",
                observed_identity=DIGEST,
                observed_ref="x:latest",
            )

    def test_registry_source_requires_a_resolved_commit(self) -> None:
        with pytest.raises(ValidationError):
            TaskSource(kind="registry", repo="https://example.invalid/x.git", path="tasks/x")

    def test_local_source_is_not_reportable(self) -> None:
        lock = make_lock(
            task=TaskProvenance(
                name="demo-hello",
                variant="base",
                spec_hash=DIGEST,
                content_digest=DIGEST,
                source=TaskSource(kind="local", path="/home/dev/tasks/hello"),
            )
        )
        with pytest.raises(ProvenanceIncompleteError, match="local path"):
            lock.require_reportable()

    def test_unresolved_framework_commit_is_not_reportable(self) -> None:
        lock = make_lock(framework=FrameworkProvenance(version="0.1.0", commit="unknown"))
        with pytest.raises(ProvenanceIncompleteError, match="commit"):
            lock.require_reportable()

    def test_pre_release_schema_version_does_not_change(self) -> None:
        assert make_lock().framework.schema_version == 2


class TestConfigLayering:
    def test_ale_repo_path_is_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = tmp_path / "ale"
        (checkout / "packages" / "ale-run").mkdir(parents=True)
        (checkout / "pyproject.toml").write_text("[project]\nname='ale'\n")
        monkeypatch.setenv("ALE_REPO_PATH", str(checkout))
        assert ale_repo_path() == checkout
        monkeypatch.setenv("ALE_REPO_PATH", "relative")
        with pytest.raises(ConfigError, match="absolute"):
            ale_repo_path()

    def test_asset_collection_cli_overrides_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALE_ASSETS_COLLECTION", "user/assets-env")
        assert resolve_asset_collection().collection_slug == "user/assets-env"
        resolved = resolve_asset_collection("user/assets-cli")
        assert resolved == AssetCollectionConfig(
            collection_slug="user/assets-cli",
            source="cli",
        )

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
            ("agent.settings.max_turns=50", 50),
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

    def test_gateway_ceilings_are_explicitly_unlimited_by_default(self) -> None:
        limits = RunConfig().gateway.limits
        assert limits.model_dump() == {
            "max_model_calls": "unlimited",
            "max_input_tokens": "unlimited",
            "max_output_tokens": "unlimited",
            "max_total_tokens": "unlimited",
            "max_cost_usd": "unlimited",
        }

    @pytest.mark.parametrize(
        "payload",
        [
            {"max_model_calls": "default"},
            {"max_model_calls": 0},
            {"max_input_tokens": -1},
            {"max_output_tokens": False},
        ],
    )
    def test_gateway_limits_accept_positive_or_unlimited_only(
        self, payload: dict[str, object]
    ) -> None:
        with pytest.raises(ValidationError):
            GatewayLimits.model_validate(payload)
        assert GatewayLimits(max_model_calls="unlimited").max_model_calls == "unlimited"

    def test_logging_retention_defaults_are_explicit_and_strict(self) -> None:
        assert LoggingPolicy().model_dump() == {
            "native_logs": "minimal",
            "transport_payloads": "digests",
            "token_data": "none",
        }
        assert LoggingPolicy(token_data="exact").token_data == "exact"
        with pytest.raises(ValidationError):
            LoggingPolicy.model_validate({"native_logs": "sometimes"})
        with pytest.raises(ValidationError):
            LoggingPolicy.model_validate({"unknown": True})


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


class TestArtifactPolicy:
    """Declaring an output and keeping a copy of it are separate decisions."""

    def test_a_task_declares_paths_not_dispositions(self) -> None:
        spec = TaskSpec(
            name="demo-hello",
            instruction="write",
            image={"kind": "container"},
            artifacts=("/home/user/output",),
        )
        assert spec.artifacts == ("/home/user/output",)
        assert spec.image.kind is ImageKind.CONTAINER
        assert ImageRef(kind="container", reference="local:build").kind == "container"

    def test_the_run_decides_whether_to_keep_them(self) -> None:
        assert RunConfig().artifacts.collect == "host"
        dropped = RunConfig.model_validate({"artifacts": {"collect": "none"}})
        assert dropped.artifacts.collect == "none"

    def test_the_policy_is_part_of_the_run_identity(self) -> None:
        """Two runs that kept different things are not the same run."""
        kept = RunConfig()
        dropped = RunConfig.model_validate({"artifacts": {"collect": "none"}})
        assert kept.config_hash != dropped.config_hash


class TestFailedModelCalls:
    def test_an_upstream_failure_is_still_recorded(self) -> None:
        """Silence would read as an idle agent; it was a provider returning 529s."""
        record = TransportCall(
            episode_id="e",
            call_id="call-1",
            model="m",
            request_digest=DIGEST,
            disposition="failed",
            upstream_status=529,
        )
        assert record.upstream_status == 529
        assert record.input_tokens == 0  # a failed call bought nothing

"""Autonomous preset selection and layering."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ale.core.config import RunConfig, load_run_config, select_agent_name
from ale.core.errors import ConfigError
from ale.run.cli.tasks import PRESET_DIR, _config
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.provenance import agent_provenance
from ale.run.secrets import provider_credentials, provider_endpoint

pytestmark = pytest.mark.unit
main = import_module("ale.run.cli.main")


def test_selecting_claude_loads_the_complete_preset() -> None:
    settings = load_run_config(preset_path=PRESET_DIR / "claude-code.toml")

    assert settings.agent.name == "claude-code"
    assert settings.agent.version == "2.1.227"
    assert settings.agent.settings == {
        "max_turns": "unlimited",
        "permission_mode": "bypassPermissions",
        "allowed_tools": [],
        "disallowed_tools": [],
        "append_system_prompt": "default",
        "effort": "default",
    }
    assert settings.preset_name == "claude-code"
    assert settings.preset_digest and settings.preset_digest.startswith("sha256:")


def test_cli_then_run_then_preset_precedence(tmp_path: Path) -> None:
    run = tmp_path / "run.toml"
    run.write_text("[agent]\nname='claude-code'\n[agent.settings]\nmax_turns=20\n")
    settings = load_run_config(
        preset_path=PRESET_DIR / "claude-code.toml",
        run_path=run,
        overrides=["agent.settings.max_turns=30"],
    )
    assert settings.agent.settings["max_turns"] == 30
    assert settings.agent.settings["permission_mode"] == "bypassPermissions"


def test_resource_collections_are_additive(tmp_path: Path) -> None:
    run = tmp_path / "run.toml"
    run.write_text("[agent]\nskills=[{path='run-skill'}]\n")
    preset = tmp_path / "claude-code.toml"
    preset.write_text(
        "[agent]\nname='claude-code'\nskills=[{path='preset-skill'}]\nmcp_servers=[]\n"
    )
    settings = load_run_config(
        preset_path=preset,
        run_path=run,
        overrides=["agent.skills=[{path='cli-skill'}]"],
    )
    assert [Path(source.path).name for source in settings.agent.skills] == [
        "preset-skill",
        "run-skill",
        "cli-skill",
    ]
    assert [source.origin for source in settings.agent.skills] == ["preset", "run", "cli"]


def test_run_file_selects_agent_when_cli_option_is_absent(tmp_path: Path) -> None:
    run = tmp_path / "run.toml"
    run.write_text(
        "container.provider='docker'\ncontainer.gpus=[0,1]\n"
        "vm.provider='qemu'\n[agent]\nname='claude-code'\n"
    )
    assert select_agent_name(run_path=run) == "claude-code"
    settings = _config(run, None, agent=None, model=None)
    assert settings.container.gpus == (0, 1)
    assert settings.vm.provider == "qemu"


@pytest.mark.parametrize("command", ["run", "validate", "prepare", "reverify"])
def test_cli_inherits_runtime_configuration_with_explicit_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    runtime_config = tmp_path / "runtime.toml"
    runtime_config.write_text(
        "[agent]\nname='nop'\n"
        "[verification.llm]\nprovider='openai'\ndialect='openai-responses'\n"
        "model='runtime-judge'\nreasoning_effort='medium'\n"
    )
    explicit_config = tmp_path / "explicit.toml"
    explicit_config.write_text(runtime_config.read_text().replace("runtime-judge", "explicit"))
    monkeypatch.setenv("ALE_RUN_CONFIG", str(runtime_config))
    captured: list[RunConfig] = []

    async def workflow(*args: object) -> int:
        captured.append(next(value for value in args if isinstance(value, RunConfig)))
        return 0

    for name in ("_run_one", "_validate", "_prepare_only", "_reverify"):
        monkeypatch.setattr(main, name, workflow)
    argv = [command, "tasks/example"]
    if command == "reverify":
        argv.append(str(tmp_path / "source-run"))
    runner = CliRunner()
    for flags, expected in (
        ([], "runtime-judge"),
        (["--set", 'verification.llm.model="cli-judge"'], "cli-judge"),
        (["--config", str(explicit_config)], "explicit"),
    ):
        result = runner.invoke(main.app, [*argv, *flags])
        assert result.exit_code == 0, result.output
        judge = captured[-1].verification.llm
        assert judge is not None
        assert judge.model == expected
        assert judge.reasoning_effort == "medium"


def test_bad_runtime_configuration_fails_without_starting_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_config = tmp_path / "missing.toml"
    monkeypatch.setenv("ALE_RUN_CONFIG", str(runtime_config))
    result = CliRunner().invoke(main.app, ["validate", "tasks/example"])
    assert result.exit_code == 3
    assert "configuration error: config file not found" in result.output


def test_legacy_single_provider_config_is_rejected(tmp_path: Path) -> None:
    run = tmp_path / "run.toml"
    run.write_text("provider='qemu'\n")
    with pytest.raises(ConfigError):
        load_run_config(run_path=run)


def test_unknown_run_and_harness_keys_are_errors(tmp_path: Path) -> None:
    run = tmp_path / "run.toml"
    run.write_text("[agent]\nmodle='typo'\n")
    with pytest.raises(ConfigError):
        load_run_config(run_path=run)


def test_preset_and_effective_settings_are_recorded() -> None:
    settings = load_run_config(preset_path=PRESET_DIR / "claude-code.toml")
    harness = ClaudeCodeHarness(
        cli_version=settings.agent.version,
        settings=settings.agent.settings,
    )
    provenance = agent_provenance(harness, settings.agent.model, settings)

    assert provenance.preset
    assert provenance.preset.name == "claude-code"
    assert provenance.settings["max_turns"] == "unlimited"
    assert provenance.native_limits == {"max_turns": "unlimited"}


def test_a_provider_key_pairs_only_with_that_providers_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALE_ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.delenv("ALE_ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ALE_OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ALE_OPENAI_BASE_URL", "https://gateway.example/")
    monkeypatch.delenv("ALE_XAI_API_KEY", raising=False)

    assert provider_endpoint("anthropic") == ("ALE_ANTHROPIC_API_KEY", "https://api.anthropic.com")
    assert provider_credentials("anthropic") == ("anthropic-key", "https://api.anthropic.com")
    assert provider_credentials("openai") == ("openai-key", "https://gateway.example")
    with pytest.raises(ConfigError, match="ALE_XAI_API_KEY is not set"):
        provider_credentials("xai")


def test_a_one_off_endpoint_must_name_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALE_ANTHROPIC_API_KEY", "default-key")
    monkeypatch.setenv("ALE_ANTHROPIC_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("PROXY_KEY", "proxy-key")

    with pytest.raises(ConfigError, match="api-key-env"):
        provider_credentials("anthropic", base_url="https://api.anthropic.com")
    assert provider_credentials(
        "anthropic", base_url="https://proxy.example/anthropic", key_var="PROXY_KEY"
    ) == ("proxy-key", "https://proxy.example/anthropic")
    assert provider_credentials("anthropic", base_url="https://gateway.example/") == (
        "default-key",
        "https://gateway.example",
    )


@pytest.mark.parametrize(
    ("name", "provider"),
    [
        ("claude-code", "anthropic"),
        ("codex-cli", "openai"),
        ("grok-build", "xai"),
        ("kimi-code", "moonshot"),
        ("muse-code", "meta"),
        ("openclaw-cli", "openai"),
        ("qwen-code", "qwen"),
    ],
)
def test_each_preset_names_its_provider_and_no_endpoint(name: str, provider: str) -> None:
    gateway = load_run_config(preset_path=PRESET_DIR / f"{name}.toml").gateway

    assert gateway.provider == provider
    assert gateway.base_url == gateway.api_key_env == ""

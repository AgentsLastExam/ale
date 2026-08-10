"""Autonomous preset selection and layering."""

from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.config import load_run_config, select_agent_name
from ale.core.errors import ConfigError
from ale.run.cli.main import PRESET_DIR, _config
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.provenance import agent_provenance

pytestmark = pytest.mark.unit


def test_selecting_claude_loads_the_complete_preset() -> None:
    settings = load_run_config(preset_path=PRESET_DIR / "claude-code.toml")

    assert settings.agent.name == "claude-code"
    assert settings.agent.version == "2.1.220"
    assert settings.agent.settings == {
        "max_turns": "unlimited",
        "max_budget_usd": "unlimited",
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
    assert provenance.native_limits == {
        "max_turns": "unlimited",
        "max_budget_usd": "unlimited",
    }

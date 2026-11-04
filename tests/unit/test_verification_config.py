from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ale.core.config import RunConfig

pytestmark = pytest.mark.unit


def test_verification_sections_are_optional_and_strict() -> None:
    assert RunConfig().verification.llm is None
    assert RunConfig().verification.agent is None
    configured = RunConfig.model_validate(
        {
            "verification": {
                "llm": {
                    "provider": "anthropic",
                    "dialect": "anthropic",
                    "model": "judge-model",
                    "reasoning_effort": "medium",
                },
                "agent": {
                    "provider": "openai",
                    "adapter": "codex-cli",
                    "version": "1.2.3",
                    "model": "agent-model",
                    "reasoning_effort": "high",
                },
            }
        }
    )
    assert configured.verification.agent is not None
    assert configured.verification.agent.adapter == "codex-cli"


_LLM = {
    "provider": "openai",
    "dialect": "openai-responses",
    "model": "m",
    "reasoning_effort": "low",
}


@pytest.mark.parametrize(
    "llm",
    [
        {**_LLM, "model": ""},
        {**_LLM, "reasoning_effort": " "},
        {**_LLM, "provider": "example"},
        {**_LLM, "dialect": "gemini"},
        {key: value for key, value in _LLM.items() if key != "provider"},
        {key: value for key, value in _LLM.items() if key != "dialect"},
    ],
)
def test_invalid_judge_configuration_is_rejected(llm: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"verification": {"llm": llm}})


def test_judges_name_a_provider_never_a_key_or_endpoint() -> None:
    config = RunConfig.model_validate({"verification": {"llm": _LLM}})
    dumped = config.model_dump(mode="json")
    assert dumped["verification"]["llm"] == _LLM


@pytest.mark.parametrize("adapter", ["codex", "claude", "openclaw-cli", ""])
def test_agent_adapter_is_closed(adapter: str) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "verification": {
                    "agent": {
                        "provider": "openai",
                        "adapter": adapter,
                        "version": "1.2.3",
                        "model": "m",
                        "reasoning_effort": "medium",
                    }
                }
            }
        )


@pytest.mark.parametrize(
    "field", ["base_url", "api_key_env", "profile", "gateway", "harness", "retry_count"]
)
def test_legacy_verification_fields_are_forbidden(field: str) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"verification": {"llm": {**_LLM, field: "legacy"}}})


@pytest.mark.parametrize("version", [None, "latest", "1", "1.2", "^1.2.3", "1.2.x"])
def test_agent_judge_requires_exact_numeric_semver(version: str | None) -> None:
    agent = {
        "provider": "openai",
        "adapter": "codex-cli",
        "model": "m",
        "reasoning_effort": "medium",
    }
    if version is not None:
        agent["version"] = version
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"verification": {"agent": agent}})


def test_sandbox_retention_is_run_owned_and_defaults_to_destroy() -> None:
    assert RunConfig().sandbox_retention.model_dump() == {
        "solver": "destroy",
        "verifier": "destroy",
    }
    configured = RunConfig.model_validate(
        {"sandbox_retention": {"solver": "keep", "verifier": "destroy"}}
    )
    assert configured.sandbox_retention.solver == "keep"
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"sandbox_retention": {"solver": "sometimes"}})


@pytest.mark.parametrize(
    ("platform", "operating_system", "directory"),
    [
        ("linux", "linux", "/logs/verifier"),
        ("win32", "windows", "C:/logs/verifier"),
        ("darwin", "macos", "/opt/ale/logs/verifier"),
    ],
)
def test_engine_and_verifier_write_to_the_same_platform_directory(
    platform, operating_system, directory
):
    from ale.core.taskspec import OperatingSystem
    from ale.run.protocol.episode_protocol import _verifier_dir, _verifier_dir_command

    # Reload only the verifier package in an isolated child; do not change the
    # test runner's platform or write any framework directory on the Host.
    code = """
import importlib,json,sys
import ale_verify
sys.platform=sys.argv[1]
importlib.reload(ale_verify)
written=[]
ale_verify.atomic_json=lambda path, data: written.append(path.as_posix())
ale_verify.write({'reward':0.5})
print(json.dumps(written))
"""
    written = json.loads(subprocess.check_output([sys.executable, "-I", "-c", code, platform]))
    system = OperatingSystem(operating_system)
    assert _verifier_dir(system).as_posix() == directory
    assert written == [f"{directory}/verification.json", f"{directory}/reward.json"]
    if system is not OperatingSystem.WINDOWS:
        for remove in (False, True):
            assert _verifier_dir_command(system, remove=remove)[-2:] == [
                directory.rsplit("/", 1)[0],
                directory,
            ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX verifier directory commands")
@pytest.mark.parametrize("entry", ["stale", "parent-link", "verifier-link"])
def test_posix_verifier_reset_and_remove_on_native_tools(tmp_path: Path, entry: str) -> None:
    from ale.run.protocol.episode_protocol import _POSIX_VERIFIER_REMOVE, _POSIX_VERIFIER_RESET

    parent, verifier = tmp_path / "logs", tmp_path / "logs/verifier"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")
    if entry == "parent-link":
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.mkdir(mode=0o777)
        if entry == "verifier-link":
            verifier.symlink_to(outside, target_is_directory=True)
        else:
            verifier.mkdir(mode=0o777)
            (verifier / "reward.json").write_text('{"reward":1}')

    # Exercise native BSD/GNU utilities without sudo or Host framework paths.
    # Only the ownership target changes to the test runner's own account.
    reset = _POSIX_VERIFIER_RESET.replace("chown -- 0:0", f"chown -- {os.getuid()}:{os.getgid()}")
    subprocess.run(["sh", "-c", reset, "sh", str(parent), str(verifier)], check=True)
    assert not parent.is_symlink() and not verifier.is_symlink()
    assert parent.stat().st_uid == os.getuid() and parent.stat().st_mode & 0o022 == 0
    assert verifier.stat().st_uid == os.getuid() and verifier.stat().st_mode & 0o777 == 0o700
    assert not list(verifier.iterdir())
    subprocess.run(
        ["sh", "-c", _POSIX_VERIFIER_REMOVE, "sh", str(parent), str(verifier)], check=True
    )
    assert not verifier.exists()
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_text() == "unchanged"

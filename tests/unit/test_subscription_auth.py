from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ale.core.config import AgentConfig, load_run_config
from ale.core.errors import (
    SubscriptionAuthenticationError,
    SubscriptionCompatibilityError,
    SubscriptionEntitlementError,
    SubscriptionProfileError,
    SubscriptionProviderLimitError,
    SubscriptionUnavailableError,
)
from ale.core.lock import AuthenticationProvenance
from ale.core.sandbox import ExecResult
from ale.run.agent_resources import continuation_fingerprint
from ale.run.cli.main import app
from ale.run.subscription import (
    ProfileLease,
    classify_subscription_error,
    resolve_authentication,
)

pytestmark = pytest.mark.unit


def isolated_auth_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    environment = tmp_path / ".env"
    environment.write_text("")
    monkeypatch.setenv("ALE_ENV_FILE", str(environment))
    return tmp_path / ".ale" / "auth"


def test_run_help_describes_authentication_selection() -> None:
    result = CliRunner().invoke(app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--auth" in result.output
    assert all(value in result.output for value in ("auto", "api-key", "subscription"))


def test_authentication_defaults_to_auto_and_tracks_source(tmp_path: Path) -> None:
    run = tmp_path / "run.toml"
    run.write_text("[agent]\nname='codex-cli'\nauthentication='subscription'\n")

    default = load_run_config()
    configured = load_run_config(run_path=run)
    overridden = load_run_config(
        run_path=run,
        overrides=["agent.authentication='api-key'"],
    )

    assert default.agent.authentication == "auto"
    assert default.authentication_source == "default"
    assert configured.authentication_source == "run"
    assert overridden.authentication_source == "cli"


def test_auto_ignores_the_users_default_codex_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    ordinary = home / ".codex" / "auth.json"
    ordinary.parent.mkdir(parents=True)
    ordinary.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "x"}}))
    isolated_auth_root(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(home))

    auth = resolve_authentication(AgentConfig(name="codex-cli", authentication="auto"))

    assert auth.mode == "api-key"
    assert auth.profile is None


def test_auto_selects_only_the_isolated_codex_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = isolated_auth_root(tmp_path, monkeypatch) / "codex-cli" / "auth.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "x"}}))
    profile.chmod(0o600)

    auth = resolve_authentication(AgentConfig(name="codex-cli", authentication="auto"))

    assert auth.mode == "subscription"
    assert auth.profile == profile
    assert auth.provider_hosts == frozenset({"chatgpt.com", "auth.openai.com"})
    assert auth.profile_slot_id.startswith("sha256:")


def test_claude_subscription_provenance_names_the_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_auth_root(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    auth = resolve_authentication(AgentConfig(name="claude-code", authentication="subscription"))
    provenance = auth.provenance(source="cli", cli_version="2.1.227")

    assert provenance.transport == "subscription-relay"
    assert provenance.credential_exposed_to_agent is False
    assert provenance.gateway_observability == "unavailable"


def test_explicit_subscription_requires_an_isolated_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_auth_root(tmp_path, monkeypatch)

    with pytest.raises(SubscriptionUnavailableError, match="CODEX_HOME"):
        resolve_authentication(AgentConfig(name="codex-cli", authentication="subscription"))


def test_subscription_rejects_malformed_profile_before_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = isolated_auth_root(tmp_path, monkeypatch) / "grok-build" / "auth.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("not-json")
    profile.chmod(0o600)
    resolved = resolve_authentication(AgentConfig(name="grok-build", authentication="subscription"))

    with pytest.raises(SubscriptionProfileError, match="grok login"):
        ProfileLease(resolved).preflight()


def test_subscription_is_explicitly_unsupported_for_other_harnesses() -> None:
    with pytest.raises(SubscriptionUnavailableError, match="does not support"):
        resolve_authentication(AgentConfig(name="openclaw-cli", authentication="subscription"))


@pytest.mark.parametrize(
    ("detail", "error_type"),
    [
        ("401 Unauthorized: login required", SubscriptionAuthenticationError),
        ("error: unexpected argument --print", SubscriptionCompatibilityError),
        ("The requested model is not available for this account", SubscriptionEntitlementError),
        ("429 rate limit: weekly quota reached", SubscriptionProviderLimitError),
    ],
)
def test_native_provider_failures_are_typed_without_api_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detail: str,
    error_type: type[Exception],
) -> None:
    profile = isolated_auth_root(tmp_path, monkeypatch) / "codex-cli" / "auth.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "x"}}))
    profile.chmod(0o600)
    resolved = resolve_authentication(AgentConfig(name="codex-cli", authentication="subscription"))

    error = classify_subscription_error("codex-cli", detail)

    assert isinstance(error, error_type)
    assert resolved.mode == "subscription"


def test_authentication_provenance_has_no_raw_credential_field() -> None:
    record = AuthenticationProvenance(
        requested="auto",
        effective="subscription",
        selection_source="default",
        provider="openai",
        profile_slot_id="sha256:" + "a" * 64,
        transport="native-proxy",
        credential_exposed_to_agent=True,
        gateway_observability="unavailable",
        validated_cli_version="0.146.0",
    )

    payload = record.model_dump(mode="json")
    assert not ({"token", "secret", "credential_value", "profile_path"} & payload.keys())
    assert payload["profile_slot_id"] == "sha256:" + "a" * 64


def test_continuation_fingerprint_binds_authentication_and_profile() -> None:
    values = {
        "harness": "codex-cli",
        "model": "gpt-5",
        "settings": {},
        "resources_digest": "sha256:" + "a" * 64,
    }
    api = continuation_fingerprint(**values)
    subscription = continuation_fingerprint(
        **values,
        authentication="subscription",
        profile_slot_id="sha256:" + "b" * 64,
    )
    other_profile = continuation_fingerprint(
        **values,
        authentication="subscription",
        profile_slot_id="sha256:" + "c" * 64,
    )

    assert len({api, subscription, other_profile}) == 3


class FakeSandbox:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_file(self, path, data, **kwargs):  # type: ignore[no-untyped-def]
        self.files[str(path)] = data

    async def read_file(self, path):  # type: ignore[no-untyped-def]
        return self.files[str(path)]

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        if argv[:2] == ["rm", "-f"]:
            self.files.pop(str(argv[2]), None)
        return ExecResult(exit_code=0)


async def test_profile_lease_stages_and_atomically_persists_isolated_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = isolated_auth_root(tmp_path, monkeypatch) / "codex-cli" / "auth.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "old"}}))
    profile.chmod(0o600)
    resolved = resolve_authentication(AgentConfig(name="codex-cli", authentication="subscription"))
    sandbox = FakeSandbox()

    async with ProfileLease(resolved) as lease:
        await lease.stage(sandbox, "/home/agent")  # type: ignore[arg-type]
        guest = "/home/agent/.codex-ale/auth.json"
        assert json.loads(sandbox.files[guest])["tokens"]["refresh_token"] == "old"
        sandbox.files[guest] = json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"refresh_token": "new"}}
        ).encode()
        assert await lease.persist(sandbox, "/home/agent") == "persisted"  # type: ignore[arg-type]
        assert await lease.cleanup(sandbox, "/home/agent")  # type: ignore[arg-type]
        assert guest not in sandbox.files

    assert json.loads(profile.read_text())["tokens"]["refresh_token"] == "new"
    assert profile.stat().st_mode & 0o777 == 0o600


async def test_profile_lease_preserves_host_copy_when_guest_state_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = isolated_auth_root(tmp_path, monkeypatch) / "grok-build" / "auth.json"
    profile.parent.mkdir(parents=True)
    original = json.dumps({"refresh_token": "old"})
    profile.write_text(original)
    profile.chmod(0o600)
    resolved = resolve_authentication(AgentConfig(name="grok-build", authentication="subscription"))
    sandbox = FakeSandbox()

    async with ProfileLease(resolved) as lease:
        await lease.stage(sandbox, "/home/agent")  # type: ignore[arg-type]
        sandbox.files["/home/agent/.grok-ale/auth.json"] = b"not-json"
        with pytest.raises(Exception, match="valid JSON"):
            await lease.persist(sandbox, "/home/agent")  # type: ignore[arg-type]

    assert profile.read_text() == original


async def test_profile_lock_serializes_two_ale_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    profile = isolated_auth_root(tmp_path, monkeypatch) / "grok-build" / "auth.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"refresh_token": "old"}))
    profile.chmod(0o600)
    resolved = resolve_authentication(AgentConfig(name="grok-build", authentication="subscription"))
    first = ProfileLease(resolved)
    second = ProfileLease(resolved)

    await first.__aenter__()
    waiting = asyncio.create_task(second.__aenter__())
    await asyncio.sleep(0.05)
    assert not waiting.done()
    await first.__aexit__(None, None, None)
    await asyncio.wait_for(waiting, timeout=1)
    await second.__aexit__(None, None, None)


async def test_cancelled_profile_wait_does_not_leak_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    profile = isolated_auth_root(tmp_path, monkeypatch) / "grok-build" / "auth.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"refresh_token": "old"}))
    profile.chmod(0o600)
    resolved = resolve_authentication(AgentConfig(name="grok-build", authentication="subscription"))
    first = ProfileLease(resolved)
    waiting = ProfileLease(resolved)

    await first.__aenter__()
    task = asyncio.create_task(waiting.__aenter__())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await first.__aexit__(None, None, None)

    third = ProfileLease(resolved)
    await asyncio.wait_for(third.__aenter__(), timeout=1)
    await third.__aexit__(None, None, None)


def test_profile_lease_rejects_overexposed_host_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = isolated_auth_root(tmp_path, monkeypatch) / "grok-build" / "auth.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"refresh_token": "old"}))
    profile.chmod(0o644)
    resolved = resolve_authentication(AgentConfig(name="grok-build", authentication="subscription"))

    with pytest.raises(Exception, match="0600"):
        ProfileLease(resolved)._acquire()

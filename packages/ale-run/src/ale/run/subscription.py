"""Checkout-local subscription credentials for the host-side model Gateway."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal

from aiohttp import ClientSession, ClientTimeout

from ale.core.config import AgentConfig
from ale.core.errors import (
    AgentError,
    SubscriptionAuthenticationError,
    SubscriptionCompatibilityError,
    SubscriptionEntitlementError,
    SubscriptionProfileError,
    SubscriptionProviderLimitError,
    SubscriptionUnavailableError,
)
from ale.core.lock import AuthenticationProvenance
from ale.run.secrets import find_env_file, load_env

__all__ = [
    "ResolvedAuthentication",
    "SubscriptionCredential",
    "auth_root",
    "classify_subscription_error",
    "resolve_authentication",
]

Provider = Literal["anthropic", "openai", "xai"]

_PROVIDERS: dict[str, tuple[Provider, str | None, str]] = {
    "claude-code": (
        "anthropic",
        None,
        "run claude setup-token with checkout-local CLAUDE_CONFIG_DIR, then put "
        "CLAUDE_CODE_OAUTH_TOKEN in this checkout's .env",
    ),
    "codex-cli": (
        "openai",
        "auth.json",
        "run codex login with CODEX_HOME=$PWD/.ale/auth/codex-cli",
    ),
    "grok-build": (
        "xai",
        "auth.json",
        "run grok login with GROK_HOME=$PWD/.ale/auth/grok-build",
    ),
}
_UPSTREAMS = {
    "claude-code": "https://api.anthropic.com",
    "codex-cli": "https://chatgpt.com/backend-api/codex",
    "grok-build": "https://cli-chat-proxy.grok.com",
}
_PATHS = {
    "anthropic": "/v1/messages",
    "openai-chat-completions": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
}
_REFRESH_WINDOW = timedelta(minutes=5)
_HTTP_TIMEOUT = ClientTimeout(total=30)


@dataclass(frozen=True)
class ResolvedAuthentication:
    harness: str
    requested: Literal["auto", "api-key", "subscription"]
    mode: Literal["api-key", "subscription"]
    provider: Provider | None = None
    profile: Path | None = None
    profile_slot_id: str | None = None
    token: str = ""

    def provenance(
        self,
        *,
        source: Literal["cli", "run", "preset", "default"],
        cli_version: str,
    ) -> AuthenticationProvenance:
        return AuthenticationProvenance(
            requested=self.requested,
            effective=self.mode,
            selection_source=source,
            provider=self.provider,
            profile_slot_id=self.profile_slot_id,
            transport="gateway",
            credential_exposed_to_agent=False,
            gateway_observability="available",
            validated_cli_version=cli_version,
        )


def auth_root() -> Path:
    if environment := find_env_file():
        return environment.parent / ".ale" / "auth"
    for start in (Path.cwd().resolve(), Path(__file__).resolve()):
        for directory in (start, *start.parents):
            if (directory / "pyproject.toml").is_file() and (
                directory / "packages" / "ale-run"
            ).is_dir():
                return directory / ".ale" / "auth"
    return Path.cwd().resolve() / ".ale" / "auth"


def resolve_authentication(agent: AgentConfig) -> ResolvedAuthentication:
    requested = agent.authentication
    provider = _PROVIDERS.get(agent.name)
    if provider is None:
        if requested == "subscription":
            raise SubscriptionUnavailableError(
                f"{agent.name} does not support subscription authentication"
            )
        return ResolvedAuthentication(harness=agent.name, requested=requested, mode="api-key")

    provider_name, filename, recovery = provider
    profile = auth_root() / agent.name / filename if filename else None
    token = ""
    if agent.name == "claude-code":
        load_env()
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    available = bool(token) if profile is None else profile.is_file()

    if requested == "api-key" or (requested == "auto" and not available):
        return ResolvedAuthentication(
            harness=agent.name,
            requested=requested,
            mode="api-key",
            provider=provider_name,
        )
    if not available:
        raise SubscriptionUnavailableError(
            f"isolated subscription profile for {agent.name} is absent; {recovery}"
        )

    source = (
        f"{agent.name}:checkout-env" if profile is None else f"{agent.name}:{profile.resolve()}"
    )
    return ResolvedAuthentication(
        harness=agent.name,
        requested=requested,
        mode="subscription",
        provider=provider_name,
        profile=profile,
        profile_slot_id="sha256:" + hashlib.sha256(source.encode()).hexdigest(),
        token=token,
    )


class SubscriptionCredential:
    """Supply current subscription headers; lock only the rare token refresh."""

    def __init__(
        self,
        authentication: ResolvedAuthentication,
        *,
        cli_version: str,
        dialect: str,
    ) -> None:
        if authentication.mode != "subscription":
            raise ValueError("SubscriptionCredential requires subscription authentication")
        if authentication.harness == "claude-code" and dialect != "anthropic":
            raise SubscriptionCompatibilityError("claude-code requires the anthropic dialect")
        if authentication.harness == "codex-cli" and dialect != "openai-responses":
            raise SubscriptionCompatibilityError("codex-cli requires the openai-responses dialect")
        if dialect not in _PATHS:
            raise SubscriptionCompatibilityError(f"unsupported subscription dialect {dialect!r}")
        self.authentication = authentication
        self.cli_version = cli_version
        self.dialect = dialect
        self.upstream = _UPSTREAMS[authentication.harness]
        self.upstream_path = (
            "/responses" if authentication.harness == "codex-cli" else _PATHS[dialect]
        )
        self.supports_output_limit = authentication.harness != "codex-cli"
        self._token = authentication.token
        self._headers: dict[str, str] = {}
        self._expires_at: datetime | None = None

    async def preflight(self) -> None:
        """Validate and, if necessary, refresh before provisioning a sandbox."""
        if self.authentication.profile is not None:
            self._load_profile()
            if self._needs_refresh() and not await self.refresh():
                raise SubscriptionAuthenticationError(
                    f"{self.authentication.harness} subscription token could not be refreshed"
                )
        self._set_headers()

    async def headers(self) -> dict[str, str]:
        if self._needs_refresh():
            await self.refresh()
        return dict(self._headers)

    async def refresh(self) -> bool:
        profile = self.authentication.profile
        if profile is None:
            return False
        stale_token = self._token
        lock = await asyncio.to_thread(self._lock_profile, profile)
        try:
            payload = self._read_profile(profile)
            self._apply(payload)
            if self._token != stale_token and not self._needs_refresh():
                return True
            updated = await self._refresh_payload(payload)
            await asyncio.to_thread(self._atomic_write, profile, updated)
            self._apply(updated)
            return True
        except SubscriptionProfileError:
            raise
        except Exception:
            return False
        finally:
            await asyncio.to_thread(self._unlock_profile, lock)

    def _set_headers(self) -> None:
        if not self._token:
            raise SubscriptionProfileError(
                f"{self.authentication.harness} subscription profile has no access token"
            )
        headers = {"authorization": f"Bearer {self._token}"}
        if self.authentication.harness == "codex-cli":
            headers.update(self._headers)
        elif self.authentication.harness == "grok-build":
            headers.update(
                {
                    "X-XAI-Token-Auth": "xai-grok-cli",
                    "x-authenticateresponse": "authenticate-response",
                    "x-grok-client-version": self.cli_version,
                    "x-grok-client-identifier": "grok-shell",
                    "x-grok-client-mode": "headless",
                }
            )
        self._headers = headers

    def _load_profile(self) -> None:
        profile = self.authentication.profile
        assert profile is not None
        self._apply(self._read_profile(profile))

    def _apply(self, payload: dict[str, Any]) -> None:
        if self.authentication.harness == "codex-cli":
            if payload.get("auth_mode") != "chatgpt" or not isinstance(
                tokens := payload.get("tokens"), dict
            ):
                raise SubscriptionProfileError("isolated Codex profile is not a ChatGPT login")
            self._token = _required_string(tokens, "access_token", "Codex")
            account_id = _required_string(tokens, "account_id", "Codex")
            self._headers = {"ChatGPT-Account-ID": account_id}
            self._expires_at = _jwt_expiry(self._token)
        else:
            entry = _grok_entry(payload)
            self._token = _required_string(entry, "key", "Grok")
            self._headers = {}
            self._expires_at = _timestamp(entry.get("expires_at")) or _jwt_expiry(self._token)
        self._set_headers()

    def _needs_refresh(self) -> bool:
        return (
            self._expires_at is not None and self._expires_at <= datetime.now(UTC) + _REFRESH_WINDOW
        )

    async def _refresh_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.authentication.harness == "codex-cli":
            return await self._refresh_codex(payload)
        return await self._refresh_grok(payload)

    async def _refresh_codex(self, payload: dict[str, Any]) -> dict[str, Any]:
        tokens = payload["tokens"]
        refresh_token = _required_string(tokens, "refresh_token", "Codex")
        async with (
            ClientSession(timeout=_HTTP_TIMEOUT) as client,
            client.post(
                "https://auth.openai.com/oauth/token",
                json={
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            ) as response,
        ):
            if response.status >= 400:
                raise SubscriptionAuthenticationError(
                    f"Codex token refresh returned HTTP {response.status}"
                )
            refreshed = await response.json()
        for name in ("id_token", "access_token", "refresh_token"):
            if isinstance(refreshed.get(name), str) and refreshed[name]:
                tokens[name] = refreshed[name]
        payload["last_refresh"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return payload

    async def _refresh_grok(self, payload: dict[str, Any]) -> dict[str, Any]:
        entry = _grok_entry(payload)
        issuer = _required_string(entry, "oidc_issuer", "Grok").rstrip("/")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": _required_string(entry, "refresh_token", "Grok"),
            "client_id": _required_string(entry, "oidc_client_id", "Grok"),
        }
        for name in ("principal_type", "principal_id"):
            if isinstance(entry.get(name), str) and entry[name]:
                form[name] = entry[name]
        async with ClientSession(timeout=_HTTP_TIMEOUT) as client:
            async with client.get(f"{issuer}/.well-known/openid-configuration") as response:
                if response.status >= 400:
                    raise SubscriptionAuthenticationError(
                        f"Grok OIDC discovery returned HTTP {response.status}"
                    )
                discovery = await response.json()
            endpoint = _required_string(discovery, "token_endpoint", "Grok OIDC")
            async with client.post(endpoint, data=form) as response:
                if response.status >= 400:
                    raise SubscriptionAuthenticationError(
                        f"Grok token refresh returned HTTP {response.status}"
                    )
                refreshed = await response.json()
        entry["key"] = _required_string(refreshed, "access_token", "Grok OIDC")
        if isinstance(refreshed.get("refresh_token"), str) and refreshed["refresh_token"]:
            entry["refresh_token"] = refreshed["refresh_token"]
        if isinstance(refreshed.get("expires_in"), int):
            expires = datetime.now(UTC) + timedelta(seconds=refreshed["expires_in"])
            entry["expires_at"] = expires.isoformat().replace("+00:00", "Z")
        return payload

    def _read_profile(self, profile: Path) -> dict[str, Any]:
        try:
            info = profile.lstat()
            if not stat.S_ISREG(info.st_mode) or profile.is_symlink():
                raise SubscriptionProfileError(
                    "isolated subscription profile must be a regular file"
                )
            if info.st_uid != os.getuid():
                raise SubscriptionProfileError(
                    "isolated subscription profile must be owned by the user"
                )
            if info.st_mode & 0o077:
                raise SubscriptionProfileError("isolated subscription profile must have mode 0600")
            payload = json.loads(profile.read_bytes())
        except SubscriptionProfileError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            recovery = _PROVIDERS[self.authentication.harness][2]
            raise SubscriptionProfileError(
                f"isolated {self.authentication.harness} profile is unreadable; "
                f"recover by: {recovery}"
            ) from exc
        if not isinstance(payload, dict) or not payload:
            raise SubscriptionProfileError("isolated subscription profile is empty")
        return payload

    @staticmethod
    def _lock_profile(profile: Path) -> BinaryIO:
        lock_path = profile.with_name(profile.name + ".ale.lock")
        lock = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return lock

    @staticmethod
    def _unlock_profile(lock: BinaryIO) -> None:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    @staticmethod
    def _atomic_write(profile: Path, payload: dict[str, Any]) -> None:
        temporary = profile.with_name(profile.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, profile)
            directory = os.open(profile.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def _required_string(payload: dict[str, Any], key: str, owner: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SubscriptionProfileError(f"{owner} subscription profile has no {key}")
    return value


def _grok_entry(payload: dict[str, Any]) -> dict[str, Any]:
    for value in payload.values():
        if isinstance(value, dict) and isinstance(value.get("key"), str):
            return value
    raise SubscriptionProfileError("isolated Grok profile has no login entry")


def _jwt_expiry(token: str) -> datetime | None:
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return datetime.fromtimestamp(float(claims["exp"]), UTC)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_subscription_error(harness: str, detail: str) -> Exception:
    """Classify stable provider failure signatures; leave unknown failures alone."""
    lowered = detail.lower()
    if any(
        marker in lowered
        for marker in (
            "unknown option",
            "unexpected argument",
            "invalid configuration",
            "failed to parse config",
            "unsupported output format",
        )
    ):
        return SubscriptionCompatibilityError(
            f"{harness} no longer matches ALE's pinned subscription contract: {detail[-500:]}"
        )
    if any(
        marker in lowered
        for marker in (
            "rate limit",
            "rate_limit",
            "quota",
            "usage limit",
            "weekly limit",
            "too many requests",
            "concurrency limit",
            " 429",
            "429 ",
        )
    ):
        return SubscriptionProviderLimitError(f"{harness} subscription limit: {detail[-500:]}")
    if any(
        marker in lowered
        for marker in (
            "unauthorized",
            "authentication failed",
            "login required",
            "not logged in",
            "invalid token",
            "token expired",
            "sign in",
            " 401",
            "401 ",
        )
    ):
        recovery = _PROVIDERS.get(harness, (None, None, "run the native login"))[2]
        return SubscriptionAuthenticationError(
            f"{harness} subscription login failed: {detail[-500:]}; recover by: {recovery}"
        )
    if any(
        marker in lowered
        for marker in (
            "model is not available",
            "model not available",
            "does not have access",
            "model access",
            "unsupported model",
            "not entitled",
        )
    ):
        return SubscriptionEntitlementError(
            f"{harness} subscription entitlement failed: {detail[-500:]}"
        )
    return AgentError(f"{harness} failed: {detail[-500:]}")

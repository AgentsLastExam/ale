"""Native subscription authentication without touching ordinary Host agent profiles."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

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
from ale.core.sandbox import Identity, Sandbox
from ale.run.secrets import find_env_file, load_env

__all__ = [
    "ProfileLease",
    "ResolvedAuthentication",
    "auth_root",
    "classify_subscription_error",
    "resolve_authentication",
]

Provider = Literal["anthropic", "openai", "xai"]

_PROVIDERS: dict[str, tuple[Provider, frozenset[str], str | None, str]] = {
    "claude-code": (
        "anthropic",
        frozenset({"api.anthropic.com", "platform.claude.com"}),
        None,
        "run claude setup-token with checkout-local CLAUDE_CONFIG_DIR, then put "
        "CLAUDE_CODE_OAUTH_TOKEN in this checkout's .env",
    ),
    "codex-cli": (
        "openai",
        frozenset({"chatgpt.com", "auth.openai.com"}),
        "auth.json",
        "run codex login with CODEX_HOME=$PWD/.ale/auth/codex-cli",
    ),
    "grok-build": (
        "xai",
        frozenset({"cli-chat-proxy.grok.com", "auth.x.ai"}),
        "auth.json",
        "run grok login with GROK_HOME=$PWD/.ale/auth/grok-build",
    ),
}


@dataclass(frozen=True)
class ResolvedAuthentication:
    harness: str
    requested: Literal["auto", "api-key", "subscription"]
    mode: Literal["api-key", "subscription"]
    provider: Provider | None = None
    provider_hosts: frozenset[str] = frozenset()
    profile: Path | None = None
    profile_slot_id: str | None = None
    token: str = ""
    mutable: bool = False

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
            transport=(
                "subscription-relay"
                if self.mode == "subscription" and self.harness == "claude-code"
                else "native-proxy"
                if self.mode == "subscription"
                else "gateway"
            ),
            credential_exposed_to_agent=(
                self.mode == "subscription" and self.harness != "claude-code"
            ),
            gateway_observability=("unavailable" if self.mode == "subscription" else "available"),
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

    provider_name, hosts, filename, recovery = provider
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
    slot_id = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    return ResolvedAuthentication(
        harness=agent.name,
        requested=requested,
        mode="subscription",
        provider=provider_name,
        provider_hosts=hosts,
        profile=profile,
        profile_slot_id=slot_id,
        token=token,
        mutable=profile is not None,
    )


def classify_subscription_error(harness: str, detail: str) -> Exception:
    """Classify only stable provider failure signatures; leave unknown failures alone."""
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
        recovery = _PROVIDERS.get(harness, (None, frozenset(), None, "run the native login"))[3]
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


class ProfileLease:
    """One isolated native profile held for the complete episode."""

    def __init__(self, authentication: ResolvedAuthentication) -> None:
        if authentication.mode != "subscription":
            raise ValueError("ProfileLease requires subscription authentication")
        self.authentication = authentication
        self.credential = authentication.token.encode()
        self._lock: BinaryIO | None = None

    async def __aenter__(self) -> ProfileLease:
        while not self._acquire(blocking=False):
            await asyncio.sleep(0.1)
        return self

    async def __aexit__(self, *_: object) -> None:
        await asyncio.shield(asyncio.to_thread(self._release))

    def preflight(self) -> None:
        """Validate an isolated profile before provisioning any Task Sandbox."""
        profile = self.authentication.profile
        if profile is None:
            return
        try:
            self._validate_host_profile(profile)
            self._validate_json(profile.read_bytes())
        except (OSError, SubscriptionProfileError) as exc:
            recovery = _PROVIDERS[self.authentication.harness][3]
            raise SubscriptionProfileError(f"{exc}; recover by: {recovery}") from exc

    def _acquire(self, *, blocking: bool = True) -> bool:
        profile = self.authentication.profile
        if profile is None:
            return True
        lock_path = profile.with_name(profile.name + ".ale.lock")
        try:
            lock = lock_path.open("a+b")
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                lock.close()
                return False
            self.preflight()
            credential = profile.read_bytes()
        except Exception:
            if "lock" in locals():
                lock.close()
            raise
        self._lock = lock
        self.credential = credential
        return True

    def _release(self) -> None:
        if self._lock is None:
            return
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()
        self._lock = None

    async def stage(self, sandbox: Sandbox, home: str) -> None:
        guest = self.guest_path(home)
        if guest is None:
            return
        prepared = await sandbox.exec(
            ["mkdir", "-p", str(PurePosixPath(guest).parent)], identity=Identity.AGENT
        )
        if not prepared.ok:
            raise SubscriptionProfileError(
                f"could not prepare staged {self.authentication.harness} profile"
            )
        await sandbox.write_file(guest, self.credential, identity=Identity.AGENT)
        result = await sandbox.exec(["chmod", "0600", guest], identity=Identity.AGENT)
        if not result.ok:
            raise SubscriptionProfileError(
                f"could not protect staged {self.authentication.harness} credential"
            )

    async def persist(
        self, sandbox: Sandbox, home: str
    ) -> Literal["not-applicable", "unchanged", "persisted"]:
        profile = self.authentication.profile
        guest = self.guest_path(home)
        if profile is None or guest is None:
            return "not-applicable"
        try:
            updated = await sandbox.read_file(guest)
        except Exception as exc:
            raise SubscriptionProfileError(
                f"staged {self.authentication.harness} credential is missing"
            ) from exc
        self._validate_json(updated)
        if updated == self.credential:
            return "unchanged"
        await asyncio.to_thread(self._atomic_replace, profile, updated)
        self.credential = updated
        return "persisted"

    async def cleanup(self, sandbox: Sandbox, home: str) -> bool:
        guest = self.guest_path(home)
        if guest is None:
            return True
        result = await sandbox.exec(["rm", "-f", guest], identity=Identity.AGENT)
        return result.ok

    def guest_path(self, home: str) -> str | None:
        return {
            "codex-cli": f"{home}/.codex-ale/auth.json",
            "grok-build": f"{home}/.grok-ale/auth.json",
        }.get(self.authentication.harness)

    def _validate_host_profile(self, profile: Path) -> None:
        try:
            info = profile.lstat()
        except OSError as exc:
            raise SubscriptionProfileError(
                f"isolated {self.authentication.harness} profile is unreadable"
            ) from exc
        if not stat.S_ISREG(info.st_mode) or profile.is_symlink():
            raise SubscriptionProfileError("isolated subscription profile must be a regular file")
        if info.st_uid != os.getuid():
            raise SubscriptionProfileError(
                "isolated subscription profile must be owned by the user"
            )
        if info.st_mode & 0o077:
            raise SubscriptionProfileError("isolated subscription profile must have mode 0600")

    def _validate_json(self, raw: bytes) -> None:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SubscriptionProfileError(
                f"{self.authentication.harness} credential is not valid JSON"
            ) from exc
        if not isinstance(payload, dict) or not payload:
            raise SubscriptionProfileError(
                f"{self.authentication.harness} credential is not a non-empty JSON object"
            )
        if self.authentication.harness == "codex-cli" and payload.get("auth_mode") != "chatgpt":
            raise SubscriptionProfileError("isolated Codex profile is not a ChatGPT login")

    @staticmethod
    def _atomic_replace(profile: Path, raw: bytes) -> None:
        temporary = profile.with_name(profile.name + ".tmp")
        try:
            with temporary.open("wb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(raw)
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

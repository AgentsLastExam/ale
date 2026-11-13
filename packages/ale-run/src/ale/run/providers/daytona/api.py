"""The part of Daytona's REST API the Provider uses.

Everything here is Host-side and holds the API key; a sandbox never sees it. Requests are
retried on the statuses Daytona uses to say "later" and nothing else, so a refusal
(a quota, a tier, a bad name) reaches the caller as what it is.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import aiohttp

from ale.core.errors import ConfigError, ProviderCapabilityError, ProviderStartError

__all__ = ["DEFAULT_API_URL", "DaytonaApi", "DaytonaError"]

DEFAULT_API_URL = "https://app.daytona.io/api"
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_ATTEMPTS = 4
#: Sandbox states from which no later state is "started".
_FAILED_STATES = frozenset({"error", "build_failed", "destroyed", "destroying"})
#: How long a create keeps waiting out Daytona's rate limit before failing.
CREATE_RATE_LIMIT_SEC = 300.0


class DaytonaError(ProviderStartError):
    """Daytona answered with an error status."""

    def __init__(self, status: int, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(f"Daytona API {status}: {message}")
        self.status = status
        self.reason = message
        self.retry_after = retry_after
        """Seconds Daytona asked the caller to wait, when it said."""


class DaytonaApi:
    """A thin asynchronous client."""

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        self._key = api_key
        self._base = (base_url or os.environ.get("ALE_DAYTONA_API_URL") or DEFAULT_API_URL).rstrip(
            "/"
        )

    @classmethod
    def from_environment(cls, variable: str = "ALE_DAYTONA_API_KEY") -> DaytonaApi:
        key = os.environ.get(variable, "").strip()
        if not key:
            raise ConfigError(
                f"{variable} is not set. Put your Daytona API key in the checkout's .env "
                "(copy .env.example) or name another variable with container.daytona.api_key_env."
            )
        return cls(key)

    # --- transport -------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        params: dict[str, Any] | None = None,
        timeout_sec: float = 60,
        accept: tuple[int, ...] = (200, 201, 204),
        attempts: int = _ATTEMPTS,
    ) -> Any:
        """One call, retried while Daytona says to try again. Returns the decoded body."""
        last: DaytonaError | None = None
        for attempt in range(attempts):
            try:
                # A session per call: nothing to close, and the Provider contract has no
                # lifecycle hook to close one with.
                async with (
                    aiohttp.ClientSession() as session,
                    session.request(
                        method,
                        f"{self._base}{path}",
                        json=body,
                        params=params,
                        headers={"Authorization": f"Bearer {self._key}"},
                        timeout=aiohttp.ClientTimeout(total=timeout_sec),
                    ) as response,
                ):
                    text = await response.text()
                    if response.status in accept:
                        return json.loads(text) if text.strip() else None
                    wait = _seconds(response.headers.get("Retry-After"))
                    last = DaytonaError(response.status, _message(text), retry_after=wait)
                    if response.status not in _RETRY_STATUSES:
                        raise last
                    delay = wait if wait is not None else float(2**attempt)
            except (aiohttp.ClientError, TimeoutError) as exc:
                last = DaytonaError(0, f"{type(exc).__name__}: {exc}")
                delay = float(2**attempt)
            if attempt + 1 < attempts:
                await asyncio.sleep(min(delay, 30))
        assert last is not None
        raise last

    # --- sandboxes -------------------------------------------------------------

    async def create_sandbox(self, body: dict[str, Any]) -> dict[str, Any]:
        """Retried only while Daytona rate-limits it, for up to ``CREATE_RATE_LIMIT_SEC``.

        A rate-limited create made nothing. Any other failure is not retried: a repeated
        create could leave two sandboxes under one name.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CREATE_RATE_LIMIT_SEC
        attempt = 0
        while True:
            try:
                return await self.request(
                    "POST", "/sandbox", body=body, timeout_sec=120, attempts=1
                )
            except DaytonaError as exc:
                if exc.status == 400:
                    raise ProviderCapabilityError(
                        f"Daytona refused the sandbox request: {exc.reason}"
                    ) from exc
                remaining = deadline - loop.time()
                if exc.status != 429 or remaining <= 0:
                    raise
                attempt += 1
                delay = exc.retry_after if exc.retry_after is not None else 2.0**attempt
                await asyncio.sleep(min(max(delay, 0.5), 60.0, remaining))

    async def get_sandbox(self, sandbox: str) -> dict[str, Any] | None:
        """The sandbox by id or name, or ``None`` once it is gone."""
        try:
            return await self.request("GET", f"/sandbox/{sandbox}")
        except DaytonaError as exc:
            if exc.status == 404:
                return None
            raise

    async def wait_started(self, sandbox_id: str, *, timeout_sec: float) -> dict[str, Any]:
        """Poll until the sandbox runs; a failed build or start raises with Daytona's reason."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        state = "unknown"
        while loop.time() < deadline:
            data = await self.get_sandbox(sandbox_id)
            if data is None:
                raise ProviderStartError(f"Daytona sandbox {sandbox_id} disappeared while starting")
            state = str(data.get("state"))
            if state == "started":
                return data
            if state in _FAILED_STATES:
                reason = data.get("errorReason") or "no reason given"
                raise ProviderStartError(f"Daytona sandbox entered {state}: {reason}")
            await asyncio.sleep(1.5)
        raise ProviderStartError(
            f"Daytona sandbox {sandbox_id} was still {state} after {timeout_sec:g}s"
        )

    async def delete_sandbox(self, sandbox_id: str, *, timeout_sec: float = 120) -> None:
        """Idempotent: a sandbox that is already gone is success. Waits until it is."""
        try:
            await self.request("DELETE", f"/sandbox/{sandbox_id}", accept=(200, 202, 204))
        except DaytonaError as exc:
            if exc.status != 404:
                raise
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        while loop.time() < deadline:
            data = await self.get_sandbox(sandbox_id)
            if data is None or data.get("state") == "destroyed":
                return
            await asyncio.sleep(2)
        raise ProviderStartError(
            f"Daytona sandbox {sandbox_id} was not removed in {timeout_sec:g}s"
        )

    async def list_sandboxes(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        """Every sandbox carrying all of ``labels``, across pages."""
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"labels": json.dumps(labels), "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.request("GET", "/sandbox", params=params)
            found.extend(page.get("items") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                return found

    async def ssh_command(self, sandbox_id: str, *, minutes: int) -> str:
        """The ssh command line for a fresh access token (it carries the token)."""
        data = await self.request(
            "POST", f"/sandbox/{sandbox_id}/ssh-access", params={"expiresInMinutes": minutes}
        )
        return str(data["sshCommand"])

    async def update_network(self, sandbox_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Change a running sandbox's network settings; returns the sandbox as stored.

        ``networkBlockAll: false`` on its own also clears any allowlist.
        """
        try:
            return await self.request(
                "POST", f"/sandbox/{sandbox_id}/network-settings", body=settings
            )
        except DaytonaError as exc:
            if exc.status in {400, 402, 403}:
                raise ProviderCapabilityError(
                    "Daytona would not change the sandbox's network settings at runtime "
                    f"(this needs a Tier 3 or Tier 4 organization): {exc.reason}"
                ) from exc
            raise

    # --- registries ------------------------------------------------------------

    async def registry_push_access(self) -> dict[str, Any]:
        return await self.request("GET", "/docker-registry/registry-push-access")

    async def registries(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/docker-registry") or []

    async def create_registry(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/docker-registry", body=body)

    async def update_registry(self, registry_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PATCH", f"/docker-registry/{registry_id}", body=body)


def _message(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:300]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or text)[:300]
    return text.strip()[:300]


def _seconds(header: str | None) -> float | None:
    """A ``Retry-After`` given in seconds; any other form leaves the caller's backoff."""
    try:
        return max(0.0, float(header)) if header else None
    except ValueError:
        return None

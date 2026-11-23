"""How a sealed Sandbox under a proxied policy, ``allowlist`` or ``blocklist``, reaches the
Host's egress proxy.

The proxy knows an episode by its egress session's token, which many programs cannot
present: a browser sends proxy credentials only in answer to a challenge, and Chromium
ignores those in the proxy variables. So every Provider that seals a Sandbox to the Host's
proxy has guestd relay a loopback port to it with the token (``guestd/egress.py``). While
the Sandbox is sealed, the proxy variables of every command, the agent's and a verifier's,
name the relay, and on a desktop so does the agent account's system proxy. The proxy admits
what the policy of the phase that runs admits.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from ale.core.errors import GuestUnreachableError, ProviderStartError
from ale.core.sandbox import ResourceAllocation, SandboxRequest
from ale.run.transport import GuestClient

__all__ = ["start", "stop", "variables"]


async def start(
    client: GuestClient,
    request: SandboxRequest,
    allocation: ResourceAllocation,
    *,
    proxy: str,
    host: str,
) -> str | None:
    """Relay the Sandbox to ``proxy``, the Host proxy as the guest reaches it at ``host``.

    Returns the relay's address, or None when the Sandbox has no proxy to reach.
    """
    if not request.network.proxied or not request.proxy_url:
        return None
    if request.proxy_token:
        proxy = proxy.replace("://", f"://{quote(request.proxy_token, safe='')}:@", 1)
    try:
        return await client.egress_relay(
            proxy, bypass=_bypass(host), desktop=allocation.desktop_resolution is not None
        )
    except (GuestUnreachableError, TimeoutError) as error:
        raise ProviderStartError(f"could not start the egress relay: {error}") from error


async def stop(client: GuestClient, request: SandboxRequest) -> None:
    """Stop the relay and give the agent account its own system proxy back.

    Nothing that ran before is assumed: a guest left sealed by an earlier process, or by a
    failure halfway, restores just the same, and one with nothing to restore is left as it
    is. A failure is logged rather than raised, since the agent may have broken its own
    desktop, and that must not keep the framework's phases from running.
    """
    if not request.network.proxied:
        return
    try:
        await client.egress_relay(None)
    except (GuestUnreachableError, TimeoutError) as error:
        logging.getLogger("ale.execution").warning(
            "could not give the agent account its own system proxy back: %s", error
        )


def variables(relay: str, host: str) -> dict[str, str]:
    """The proxy variables of every command while the Sandbox is sealed."""
    proxy = f"http://{relay}"
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": ",".join(_bypass(host)),
    }


def _bypass(host: str) -> list[str]:
    """The Host's own address, which the Gateway is reached at, and the guest's loopback."""
    return list(dict.fromkeys([host, "localhost", "127.0.0.1"]))

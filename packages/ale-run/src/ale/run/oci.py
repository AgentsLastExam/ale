"""A small client for the OCI distribution API, for the registry artifacts ALE moves itself.

Credentials are Docker's own for the registry: a credential helper or an ``auths`` entry in
``$DOCKER_CONFIG/config.json`` (default ``~/.docker``), so ``docker login`` is the only
setup. Without them the registry is asked anonymously, which is enough for public images.
Raw helper output and token responses never reach an error message.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from yarl import URL

from ale.core.errors import ProviderStartError

__all__ = ["MANIFEST", "Reference", "Registry", "retrying"]

MANIFEST = "application/vnd.oci.image.manifest.v1+json"

#: Manifest media types a registry may answer with, so a reference to something else (a
#: Docker image, an index) is reported for what it is instead of as a missing manifest.
_ACCEPT = ", ".join(
    (
        MANIFEST,
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)
_MAX_MANIFEST = 4 << 20
_MAX_TOKEN = 1 << 20
_REDIRECTS = (301, 302, 303, 307, 308)

_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REFERENCE = re.compile(
    rf"(?P<registry>[^/]+)/(?P<repository>{_COMPONENT}(?:/{_COMPONENT})*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
)
_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')
_ATTEMPTS = 5


class _Unavailable(Exception):
    """The registry answered with a status worth retrying (429 or 5xx)."""


#: Failures a later attempt can get past: a dropped or refused connection, a stalled read,
#: or the registry saying it is busy.
_TRANSIENT = (aiohttp.ClientError, TimeoutError, _Unavailable)


@dataclass(frozen=True)
class Reference:
    """``registry/repository[:tag][@digest]``, always naming its registry host."""

    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @classmethod
    def parse(cls, text: str) -> Reference:
        match = _REFERENCE.fullmatch(text.strip())
        registry = match["registry"] if match else ""
        if match is None or not ("." in registry or ":" in registry or registry == "localhost"):
            raise ProviderStartError(
                f"not an OCI reference with a registry host, such as ghcr.io/owner/name:tag: {text}"
            )
        return cls(registry, match["repository"], match["tag"], match["digest"])

    def pinned(self, digest: str) -> str:
        return f"{self.registry}/{self.repository}@{digest}"

    def __str__(self) -> str:
        tag = f":{self.tag}" if self.tag else ""
        digest = f"@{self.digest}" if self.digest else ""
        return f"{self.registry}/{self.repository}{tag}{digest}"


async def retrying[T](operation: Callable[[], Awaitable[T]], what: str) -> T:
    """Run ``operation`` again after a transient failure, waiting longer each time."""
    for attempt in range(_ATTEMPTS):
        try:
            return await operation()
        except _TRANSIENT as error:
            if attempt == _ATTEMPTS - 1:
                raise ProviderStartError(f"{what} failed: {_describe(error)}") from None
            await asyncio.sleep(2**attempt)
    raise AssertionError("unreachable")


class Registry:
    """One repository of one registry, authorised to pull, or to push as well."""

    def __init__(self, reference: Reference, *, push: bool = False, connections: int = 8) -> None:
        self.reference = reference
        scheme = "http" if _on_this_machine(reference.registry) else "https"
        self._base = URL(f"{scheme}://{reference.registry}")
        self._scope = f"repository:{reference.repository}:{'pull,push' if push else 'pull'}"
        self._connections = connections
        self._authorization: str | None = None
        self._renewal = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Registry:
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=self._connections + 2),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120),
            # Digests cover a blob's stored bytes, so nothing may re-encode them in transit.
            headers={"Accept-Encoding": "identity"},
            auto_decompress=False,
        )
        try:
            await retrying(self._authorize, f"contacting {self.reference.registry}")
        except BaseException:
            await self._session.close()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()

    # --- manifests ---

    async def manifest(self, reference: str) -> tuple[bytes, str]:
        """The manifest a tag or digest names, as its exact bytes and media type."""

        async def fetch() -> tuple[bytes, str]:
            async with self._request(
                "GET",
                f"manifests/{reference}",
                headers={"Accept": _ACCEPT},
                what=f"{self.reference}",
            ) as response:
                data = await _read_bounded(response, _MAX_MANIFEST, f"{self.reference}'s manifest")
                return data, response.headers.get("Content-Type", "").partition(";")[0]

        return await retrying(fetch, f"reading the manifest of {self.reference}")

    async def has_manifest(self, reference: str) -> bool:
        async def head() -> bool:
            async with self._request(
                "HEAD",
                f"manifests/{reference}",
                headers={"Accept": _ACCEPT},
                statuses=(200, 404),
                what=f"{self.reference}",
            ) as response:
                return response.status == 200

        return await retrying(head, f"checking {self.reference}")

    async def put_manifest(self, tag: str, data: bytes, media_type: str) -> str:
        """Store a manifest under ``tag`` and return the digest the registry gave it."""

        async def put() -> str:
            async with self._request(
                "PUT",
                f"manifests/{tag}",
                headers={"Content-Type": media_type},
                body=lambda: data,
                statuses=(201,),
                what=f"publishing {self.reference}",
            ) as response:
                return response.headers.get("Docker-Content-Digest", "")

        return await retrying(put, f"publishing the manifest of {self.reference}")

    # --- blobs ---

    @asynccontextmanager
    async def blob(self, digest: str, offset: int = 0) -> AsyncIterator[aiohttp.ClientResponse]:
        """A blob's bytes from ``offset`` on; the status is 206 when the range was honoured.

        Registries commonly redirect blob reads to storage with a signed URL, which is
        followed without this registry's credentials.
        """
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with self._request(
            "GET",
            f"blobs/{digest}",
            headers=headers,
            statuses=(200, 206, *_REDIRECTS),
            what=f"blob {digest} of {self.reference}",
        ) as response:
            if response.status in (200, 206):
                yield response
                return
            location = URL(response.headers.get("Location", ""), encoded=True)
        assert self._session is not None
        async with self._session.get(self._base.join(location), headers=headers) as redirected:
            _check(redirected, (200, 206), f"blob {digest} of {self.reference}")
            yield redirected

    async def push_blob(self, digest: str, size: int, open_body: Callable[[], object]) -> None:
        """Upload a blob in one request unless the repository already has it.

        ``open_body`` returns the bytes, or a fresh async iterator over them, for each try.
        """

        async def push() -> None:
            async with self._request(
                "HEAD", f"blobs/{digest}", statuses=(200, 404, *_REDIRECTS)
            ) as response:
                # A registry that keeps blobs in object storage redirects to one it has.
                if response.status != 404:
                    return
            async with self._request(
                "POST", "blobs/uploads/", statuses=(202,), body=lambda: b""
            ) as response:
                location = response.headers.get("Location", "")
            separator = "&" if "?" in location else "?"
            target = self._base.join(URL(f"{location}{separator}digest={digest}", encoded=True))
            async with self._request(
                "PUT",
                target,
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                body=open_body,
                statuses=(201,),
            ):
                return

        await retrying(push, f"uploading blob {digest} to {self.reference}")

    # --- transport ---

    def _url(self, path: str | URL) -> URL:
        if isinstance(path, URL):
            return path
        return self._base / "v2" / self.reference.repository / path

    @asynccontextmanager
    async def _request(
        self,
        method: str,
        path: str | URL,
        *,
        headers: dict[str, str] | None = None,
        body: Callable[[], object] | None = None,
        statuses: tuple[int, ...] = (200,),
        what: str = "",
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """Send one request, renewing the token once if the registry let it expire."""
        assert self._session is not None
        url = self._url(path)
        for renewed in (False, True):
            sent = dict(headers or {})
            authorization = self._authorization
            # An upload location may name another host, which must not see this token.
            if authorization and url.host == self._base.host:
                sent["Authorization"] = authorization
            response = await self._session.request(
                method,
                url,
                headers=sent,
                data=body() if body is not None else None,
                allow_redirects=False,
            )
            try:
                if response.status == 401 and not renewed:
                    challenge = response.headers.get("WWW-Authenticate", "")
                    response.release()
                    async with self._renewal:
                        # Concurrent requests share one renewal.
                        if self._authorization == authorization:
                            await self._authenticate(challenge)
                    continue
                _check(response, statuses, what or f"{method} {url.path}")
                yield response
            finally:
                response.release()
            return

    async def _authorize(self) -> None:
        assert self._session is not None
        async with self._session.get(self._base / "v2/", allow_redirects=False) as response:
            if response.status == 401:
                await self._authenticate(response.headers.get("WWW-Authenticate", ""))
            else:
                _check(response, (200,), f"contacting {self.reference.registry}")

    async def _authenticate(self, challenge: str) -> None:
        assert self._session is not None
        scheme, _, parameters = challenge.partition(" ")
        fields = dict(_CHALLENGE.findall(parameters))
        credentials = await _docker_credentials(self.reference.registry)
        if scheme.lower() == "basic":
            if credentials is None:
                raise ProviderStartError(
                    f"{self.reference.registry} requires credentials; run docker login"
                )
            self._authorization = _basic(*credentials)
            return
        if scheme.lower() != "bearer" or not fields.get("realm"):
            raise ProviderStartError(f"{self.reference.registry} sent no usable auth challenge")
        query = {"scope": self._scope}
        if fields.get("service"):
            query["service"] = fields["service"]
        headers = {"Authorization": _basic(*credentials)} if credentials else {}
        async with self._session.get(
            URL(fields["realm"]).update_query(query), headers=headers
        ) as response:
            if response.status in (401, 403):
                raise ProviderStartError(
                    f"{self.reference.registry} refused access to {self.reference.repository}; "
                    "check docker login"
                )
            _check(response, (200,), f"authenticating to {self.reference.registry}")
            data = await _read_bounded(response, _MAX_TOKEN, f"{self.reference.registry}'s token")
        try:
            answer = json.loads(data)
            token = answer.get("token") or answer.get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise ProviderStartError(f"{self.reference.registry} returned no access token")
        self._authorization = f"Bearer {token}"


def _on_this_machine(registry: str) -> bool:
    """Whether a registry is on this machine, the only kind reached over plain HTTP."""
    if registry.startswith("["):
        host = registry[1 : registry.find("]")]
    else:
        host = registry.partition(":")[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _read_bounded(response: aiohttp.ClientResponse, limit: int, what: str) -> bytes:
    data = bytearray()
    async for piece in response.content.iter_any():
        data += piece
        if len(data) > limit:
            raise ProviderStartError(f"{what} is larger than {limit} bytes")
    return bytes(data)


def _check(response: aiohttp.ClientResponse, statuses: tuple[int, ...], what: str) -> None:
    if response.status in statuses:
        return
    if response.status == 429 or response.status >= 500:
        raise _Unavailable(f"HTTP {response.status}")
    if response.status in (401, 403):
        raise ProviderStartError(
            f"{what}: access denied (HTTP {response.status}); check docker login"
        )
    if response.status == 404:
        raise ProviderStartError(f"{what}: not found")
    raise ProviderStartError(f"{what}: HTTP {response.status}")


def _describe(error: BaseException) -> str:
    return str(error) if isinstance(error, _Unavailable) else type(error).__name__


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


async def _docker_credentials(registry: str) -> tuple[str, str] | None:
    """The username and secret Docker would send to ``registry``, if it has any."""
    config_file = Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ProviderStartError(f"Docker configuration {config_file} is unreadable") from None
    if not isinstance(config, dict):
        return None
    helpers = config.get("credHelpers")
    helper = (helpers.get(registry) if isinstance(helpers, dict) else None) or config.get(
        "credsStore"
    )
    if isinstance(helper, str) and helper:
        return await _helper_credentials(helper, registry)
    auths = config.get("auths") if isinstance(config.get("auths"), dict) else {}
    # Older Docker versions saved a login under a URL such as https://ghcr.io/v1/.
    entry = next(
        (
            value
            for key, value in auths.items()
            if key.removeprefix("https://").removeprefix("http://").split("/")[0] == registry
        ),
        None,
    )
    encoded = entry.get("auth") if isinstance(entry, dict) else None
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        username, separator, password = base64.b64decode(encoded).decode().partition(":")
    except ValueError:
        raise ProviderStartError(f"Docker's saved login for {registry} is malformed") from None
    return (username, password) if separator else None


async def _helper_credentials(helper: str, registry: str) -> tuple[str, str] | None:
    try:
        process = await asyncio.create_subprocess_exec(
            f"docker-credential-{helper}",
            "get",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise ProviderStartError(f"Docker credential helper {helper} is not installed") from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(registry.encode()), 60)
    except TimeoutError:
        process.kill()
        raise ProviderStartError(f"Docker credential helper {helper} did not answer") from None
    if process.returncode != 0:
        return None  # the helper has nothing stored for this registry
    try:
        answer = json.loads(stdout)
        return str(answer["Username"]), str(answer["Secret"])
    except (ValueError, KeyError, TypeError):
        raise ProviderStartError(f"Docker credential helper {helper} answered malformed") from None

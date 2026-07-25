"""Resolving image references.

Task manifests name images the short way (``sandbox-base-cli``) because that is what
authors should have to know. Turning a short name into a registry path is a deployment
detail, so it lives here rather than in the manifest — moving the images to a different
registry is then one constant, not an edit to every task.
"""

from __future__ import annotations

import asyncio

from ale.core.errors import ProviderStartError
from ale.core.taskspec import ImageRef

__all__ = ["DEFAULT_REGISTRY", "resolve_digest", "resolve_ref"]

DEFAULT_REGISTRY = "ghcr.io/agentslastexam"


def resolve_ref(image: ImageRef) -> str:
    """Expand a short image name to a full reference.

    A name containing a slash or a registry host is already qualified and is left alone,
    so a task can point anywhere when it needs to.
    """
    name = image.name
    if "/" in name or name.startswith("localhost"):
        return f"{name}:{image.tag}"
    return f"{DEFAULT_REGISTRY}/{name}:{image.tag}"


async def resolve_digest(reference: str, *, runtime: str = "docker") -> str:
    """Read the digest of a local image.

    Recorded in provenance so a moved tag is detectable: two runs that name the same
    tag but ran different bytes will not look comparable.
    """
    proc = await asyncio.create_subprocess_exec(
        runtime,
        "image",
        "inspect",
        "--format",
        "{{index .RepoDigests 0}}{{if not .RepoDigests}}{{.Id}}{{end}}",
        reference,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ProviderStartError(
            f"could not inspect {reference}: {stderr.decode('utf-8', 'replace').strip()}"
        )
    value = stdout.decode().strip()
    return value.partition("@")[2] or value

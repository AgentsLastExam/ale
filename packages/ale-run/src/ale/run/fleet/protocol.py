"""What the control plane, the node agents and a Run send each other.

Every message is a strict model, so a peer running another protocol version is refused at
the boundary rather than half understood. The control plane imports no Provider: a Node
describes what it can start as Sandbox profiles, and a Run describes what it needs as a
demand, so placement compares data.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ale.core.sandbox import (
    ExecResult,
    Identity,
    ImageRef,
    ResolvedImage,
    ResourceAllocation,
    SandboxFile,
    SandboxRequest,
)
from ale.core.taskspec import ImageKind, NetworkMode, OperatingSystem, Resources, TaskSpec

__all__ = [
    "IDENTIFIER",
    "IMAGE_KEY",
    "NODE_NAME",
    "PROTOCOL_VERSION",
    "AcquireRequest",
    "Arch",
    "AssignedNode",
    "Assignment",
    "CreateSandbox",
    "EpisodeReport",
    "ErrorBody",
    "ExecCall",
    "ExecDone",
    "GpuPool",
    "Grant",
    "Heartbeat",
    "HeartbeatReply",
    "ImageHolding",
    "ImageInputs",
    "ImageState",
    "LeaseRenewal",
    "Listing",
    "NodeDeclaration",
    "NodeResources",
    "Placement",
    "PlanItem",
    "PlanReply",
    "Platform",
    "RefusedRequest",
    "RosterEntry",
    "RunPlan",
    "SandboxDemand",
    "SandboxDescription",
    "SandboxProfile",
    "SandboxStatus",
    "StartReport",
    "sign_grant",
    "verify_grant",
]

#: Peers speaking another version refuse each other instead of guessing.
PROTOCOL_VERSION = 1

#: A Node name: it appears in URLs, Sandbox records and operator commands.
NODE_NAME = r"^[a-z0-9][a-z0-9-]{0,62}$"
#: A Run or Sandbox identifier chosen by the Run.
IDENTIFIER = r"^[a-z0-9][a-z0-9-]{7,63}$"

_STRICT = ConfigDict(frozen=True, extra="forbid")


class Arch(StrEnum):
    """The instruction set a Sandbox runs, which its image fixes."""

    X86_64 = "x86_64"
    ARM64 = "arm64"


class SandboxStatus(StrEnum):
    """Where one requested Sandbox is, as the control plane records it.

    A Sandbox holds its Node's resources from ``placed`` until the Node no longer lists it,
    including while it is being destroyed: a macOS VM that is shutting down still counts
    against the machine's two.
    """

    QUEUED = "queued"
    """No Node can take it now; it waits its Run's turn."""
    PLACED = "placed"
    """Resources are reserved on a Node and the Run holds a grant to create it there."""
    STARTING = "starting"
    """The Node reports it is creating the Sandbox."""
    READY = "ready"
    RELEASING = "releasing"
    """The Run released it; the Node has not yet confirmed it gone."""
    RELEASED = "released"
    FAILED = "failed"
    """No Node can ever serve the demand, or the Node failed to create it."""
    LOST = "lost"
    """The Node stopped reporting it: it went offline, restarted or dropped the Sandbox.

    Not final while the Node has not restarted: a Node that comes back and lists the
    Sandbox again restores it."""

    @property
    def holds_resources(self) -> bool:
        return self in {
            SandboxStatus.PLACED,
            SandboxStatus.STARTING,
            SandboxStatus.READY,
            SandboxStatus.RELEASING,
        }

    @property
    def final(self) -> bool:
        return self in {SandboxStatus.RELEASED, SandboxStatus.FAILED}


# --- what a Node declares ---------------------------------------------------------------


class SandboxProfile(BaseModel):
    """One combination of kind, operating system and architecture a Node can start.

    A Node lists real combinations rather than independent lists of systems and
    architectures: a Mac that runs macOS and arm64 Linux does not run x86-64 Windows.
    Each profile is one of the Node's Providers' :class:`~ale.core.sandbox.Capabilities`
    for one operating system, as the Provider's own preflight and capabilities reported.
    """

    model_config = _STRICT

    provider: str = Field(min_length=1)
    """The Node's Provider that serves this profile."""
    kind: ImageKind
    os: OperatingSystem
    arch: Arch
    gui: bool
    network_modes: frozenset[NetworkMode]
    snapshot: bool = False
    gpus: bool = False
    """Whether the Provider attaches the Node's GPUs."""
    max_cpus: int | None = Field(default=None, ge=1)
    max_memory_mb: int | None = Field(default=None, ge=1)
    macos_vm_slots: int = Field(default=0, ge=0)
    """The Node's macOS VM slots each Sandbox of this profile holds."""


class GpuPool(BaseModel):
    model_config = _STRICT

    model: str = Field(min_length=1)
    count: int = Field(ge=1)


class NodeResources(BaseModel):
    """What a Node can give Sandboxes, after what the machine keeps for itself."""

    model_config = _STRICT

    cpus: int = Field(ge=0)
    memory_mb: int = Field(ge=0)
    disk_gb: int = Field(ge=0)
    gpus: tuple[GpuPool, ...] = ()
    macos_vm_slots: int = Field(default=0, ge=0)
    """macOS VMs the whole physical machine may run at once, whichever Provider starts them.

    Apple's Virtualization framework refuses a third, so the count is per machine and is
    separate from CPU and memory."""


class Platform(BaseModel):
    model_config = _STRICT

    os: str = Field(min_length=1)
    os_version: str = ""
    arch: Arch


class NodeDeclaration(BaseModel):
    """What a node agent probed on its machine, sent when it registers.

    Probed, never configured: an operator can narrow it (labels, reserved resources, fewer
    Providers) but cannot claim what the machine did not show.
    """

    model_config = _STRICT

    protocol: int = PROTOCOL_VERSION
    name: str = Field(pattern=NODE_NAME)
    boot_id: str = Field(min_length=1)
    """Changes when the machine or the node agent restarts, which ends every Sandbox it had."""
    address: str = Field(pattern=r"^https?://")
    """Where Runs reach the node agent."""
    agent_version: str = Field(min_length=1)
    engine_commit: str = Field(min_length=1)
    framework: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    """The identity of the framework its engine installs into every Sandbox."""
    platform: Platform
    labels: dict[str, str] = Field(default_factory=dict)
    resources: NodeResources
    profiles: tuple[SandboxProfile, ...]


class RosterEntry(BaseModel):
    """One Sandbox a Node holds, as the Node sees it."""

    model_config = _STRICT

    sandbox_id: str = Field(pattern=IDENTIFIER)
    run_id: str = Field(pattern=IDENTIFIER)
    state: Literal["creating", "ready", "destroying"]
    lease_expires_at: float
    """Unix time at which the Node destroys it unless its Run renews the lease."""
    create_sec: float | None = Field(default=None, ge=0)
    """Seconds its Provider took from the create request to a ready Sandbox, once ready."""


#: An image as the Fleet names it, by what it is prepared from (see :class:`SandboxDemand`).
IMAGE_KEY = r"^sha256:[0-9a-f]{64}$"


class ImageHolding(BaseModel):
    """An image a Node prepared and keeps, by its key."""

    model_config = _STRICT

    key: str = Field(pattern=IMAGE_KEY)
    bytes: int = Field(ge=0)
    """What it takes on the Node's disk."""
    prepare_sec: float = Field(ge=0)
    """How long the Node took to prepare it."""


class Heartbeat(BaseModel):
    """What changes quickly on a Node, sent every few seconds with the full roster."""

    model_config = _STRICT

    boot_id: str = Field(min_length=1)
    roster: tuple[RosterEntry, ...] = ()
    images: tuple[ImageHolding, ...] = ()
    disk_free_gb: int = Field(default=0, ge=0)


class HeartbeatReply(BaseModel):
    model_config = _STRICT

    registered: bool
    """False when the control plane has no declaration for this boot: register again."""
    destroy: tuple[str, ...] = ()
    """Sandboxes the Node holds that the ledger does not, or that their Run released."""
    draining: bool = False


# --- what a Run asks for ----------------------------------------------------------------


class SandboxDemand(BaseModel):
    """What one Sandbox needs from a Node, derived from its SandboxRequest."""

    model_config = _STRICT

    kind: ImageKind
    os: OperatingSystem
    arch: Arch
    network_mode: NetworkMode
    resources: Resources
    image: str = Field(pattern=IMAGE_KEY)
    """The image's key: the identity of what it is prepared from, which a Run knows before any
    Node prepares it. Each Node prepares an image itself and keeps it under this key."""
    framework: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    """The Run's framework: a Sandbox carries the framework of the engine driving it, so only
    a Node running the same one can serve it."""


class AcquireRequest(BaseModel):
    """Ask for one Sandbox. Sending it again with the same ``sandbox_id`` asks nothing new."""

    model_config = _STRICT

    sandbox_id: str = Field(pattern=IDENTIFIER)
    run_id: str = Field(pattern=IDENTIFIER)
    demand: SandboxDemand
    episode_id: str = ""
    role: str = ""


class Placement(BaseModel):
    """The control plane's answer about one Sandbox."""

    model_config = _STRICT

    sandbox_id: str
    status: SandboxStatus
    reason: str = ""
    node: str | None = None
    address: str | None = None
    """The node agent's URL, once placed."""
    grant: str | None = None
    """What the Node accepts as proof that this Sandbox was placed there, once placed."""

    # --- validation ---

    @model_validator(mode="after")
    def _placed_has_a_grant(self) -> Self:
        if self.status is SandboxStatus.PLACED and not (self.node and self.address and self.grant):
            raise ValueError("a placed Sandbox names its Node, address and grant")
        return self


class StartReport(BaseModel):
    """How long a Run's Sandbox took to start, in the segments only the Run observes."""

    model_config = _STRICT

    queue_sec: float = Field(ge=0)
    """From the request to its placement."""
    image_sec: float = Field(default=0, ge=0)
    """Getting the image ready on its Node: preparing it, or checking the one it holds."""
    create_sec: float | None = Field(default=None, ge=0)
    """The Node's Provider's own time to create it."""


class PlanItem(BaseModel):
    """One Task of a Run's plan: what its Sandboxes need and how many episodes it runs."""

    model_config = _STRICT

    task: str = Field(min_length=1)
    demand: SandboxDemand
    episodes: int = Field(ge=1)


class RunPlan(BaseModel):
    """Everything a Run will ask for, sent before it asks, to place images and show progress."""

    model_config = _STRICT

    label: str = ""
    """What operators call the Run, such as its ALE run ID."""
    concurrency: int = Field(ge=1)
    items: tuple[PlanItem, ...]


class AssignedNode(BaseModel):
    model_config = _STRICT

    name: str
    address: str


class Assignment(BaseModel):
    """The Nodes a plan's image goes to, by its key; placement counts no preparation there."""

    model_config = _STRICT

    image: str
    nodes: tuple[AssignedNode, ...]


class PlanReply(BaseModel):
    model_config = _STRICT

    assignments: tuple[Assignment, ...]


class EpisodeReport(BaseModel):
    """Where one of a Run's episodes is, for progress pages."""

    model_config = _STRICT

    task: str = Field(min_length=1)
    status: Literal["running", "completed", "failed", "infrastructure"]
    """``infrastructure`` is a failure of the machinery, not of the agent or the Task."""


class RefusedRequest(BaseModel):
    """A Node refused a placed Sandbox before creating anything; place it elsewhere."""

    model_config = _STRICT

    node: str = Field(pattern=NODE_NAME)
    reason: str = Field(min_length=1)
    unreachable: bool = False
    """The Run could not reach the Node at all, so none of its Sandboxes goes there again."""


# --- grants ------------------------------------------------------------------------------


class Grant(BaseModel):
    """The terms of one placement, signed by the control plane and checked by the Node.

    The Node creates a Sandbox only on a grant, so every Sandbox it holds is one the ledger
    reserved resources for, and it admits the grant's demand rather than whatever the
    creating request claims.
    """

    model_config = _STRICT

    sandbox_id: str = Field(pattern=IDENTIFIER)
    run_id: str = Field(pattern=IDENTIFIER)
    node: str = Field(pattern=NODE_NAME)
    demand: SandboxDemand
    expires_at: float
    """Unix time after which the Node refuses to start creating it."""


def _key(token: str) -> bytes:
    return hashlib.sha256(b"ale-fleet-grant\0" + token.encode()).digest()


def sign_grant(grant: Grant, token: str) -> str:
    payload = base64.urlsafe_b64encode(grant.model_dump_json().encode()).rstrip(b"=")
    signature = hmac.new(_key(token), payload, hashlib.sha256).digest()
    return f"{payload.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def verify_grant(text: str, token: str, *, node: str, now: float | None = None) -> Grant:
    """The grant ``text`` carries, or :class:`ValueError` saying why it is not valid here."""
    payload, separator, signature = text.partition(".")
    if not separator:
        raise ValueError("malformed grant")
    expected = hmac.new(_key(token), payload.encode(), hashlib.sha256).digest()
    try:
        given = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError as error:
        raise ValueError("malformed grant") from error
    if not hmac.compare_digest(expected, given):
        raise ValueError("the grant's signature does not match")
    grant = Grant.model_validate(body)
    if grant.node != node:
        raise ValueError(f"the grant places the Sandbox on {grant.node}, not {node}")
    if (time.time() if now is None else now) > grant.expires_at:
        raise ValueError("the grant expired before the Sandbox was created")
    return grant


# --- the node agent's API ----------------------------------------------------------------


class ImageInputs(BaseModel):
    """What a Node prepares one image from, apart from the build context the Run sends.

    Either one of a Task's images, the ``role`` image of ``task``, which the Node prepares
    exactly as a local Run would from the Task's build context, or a bare ``reference`` the
    Node's Provider resolves as it is.
    """

    model_config = _STRICT

    key: str = Field(pattern=IMAGE_KEY)
    run_id: str = Field(pattern=IDENTIFIER)
    """The Run asking. A Node prepares an image once per Run, as a local Run does, so a moved
    tag or a changed builder reaches it, and reuses it within the Run."""
    os: OperatingSystem
    task: TaskSpec | None = None
    role: Literal["solver", "verifier"] = "solver"
    reference: ImageRef | None = None

    # --- validation ---

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.task is None) == (self.reference is None):
            raise ValueError("an image is prepared from a Task or from a reference, not both")
        return self


class ImageState(BaseModel):
    """What a Node keeps toward one image."""

    model_config = _STRICT

    inputs: bool
    """Whether it holds the image's build context, which the Run sends otherwise."""


class CreateSandbox(BaseModel):
    """Create the granted Sandbox. Sending it again joins the creation already under way."""

    model_config = _STRICT

    grant: str
    request: SandboxRequest
    """As the Run would hand it to a local Provider, with the image this Node prepared for
    the grant's key. The Gateway and proxy URLs are the Run's own, and the node agent
    replaces them with its tunnel's."""
    gateway_target: int | None = Field(default=None, ge=0, le=255)
    """The index of the request's Gateway on the Run's tunnel."""
    proxy_target: int | None = Field(default=None, ge=0, le=255)
    """The index of the request's egress proxy on the Run's tunnel."""
    lease_sec: float = Field(gt=0, le=3600)


class SandboxDescription(BaseModel):
    """A ready Sandbox, as the Run's Sandbox object needs to describe it."""

    model_config = _STRICT

    sandbox_id: str
    node: str
    provider: str
    resolved_image: ResolvedImage
    allocation: ResourceAllocation
    agent_user: str
    agent_home: str
    gateway_url: str | None
    """The Gateway's address inside the Sandbox."""
    create_sec: float = Field(ge=0)
    """Seconds the Node's Provider took to create it."""


class LeaseRenewal(BaseModel):
    model_config = _STRICT

    lease_sec: float = Field(gt=0, le=3600)


class ExecCall(BaseModel):
    """The first message on an exec stream; output follows as binary frames."""

    model_config = _STRICT

    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout_sec: float | None = None
    identity: Identity = Identity.FRAMEWORK
    stream: bool = False
    """Send output as it is produced, for a caller that watches it."""


class ErrorBody(BaseModel):
    """An error the node agent's Sandbox raised, named so the Run raises the same kind."""

    model_config = _STRICT

    error: str
    message: str


class ExecDone(BaseModel):
    model_config = _STRICT

    result: ExecResult | None = None
    error: ErrorBody | None = None


class Listing(BaseModel):
    model_config = _STRICT

    files: tuple[SandboxFile, ...]


#: Binary exec frames start with one byte naming the stream.
STDOUT, STDERR = 1, 2

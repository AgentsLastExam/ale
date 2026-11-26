"""Strict public contracts for one self-contained Orbit Task."""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Iterable, Mapping
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, NamedTuple, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from orbit.core.ids import TaskId, content_hash

__all__ = [
    "DEFAULT_SOLUTION",
    "HARBOR_ACCOUNT",
    "SCHEMA_VERSION",
    "SOLUTION_CASE",
    "AgentTable",
    "Artifact",
    "Author",
    "CalibrationCase",
    "ContainerFeature",
    "EnvironmentTable",
    "Expectation",
    "Healthcheck",
    "ImageKind",
    "ImageSpec",
    "McpHeaders",
    "McpServer",
    "McpServerTable",
    "McpSource",
    "NetworkMode",
    "NetworkPolicy",
    "OperatingSystem",
    "PhaseTimeouts",
    "RemoteMcpServer",
    "Resources",
    "SkillSource",
    "SolutionTable",
    "SseMcpServer",
    "StdioMcpServer",
    "StreamableHttpMcpServer",
    "TaskConfig",
    "TaskEnv",
    "TaskSpec",
    "TaskTable",
    "TaskVariables",
    "ToolProvision",
    "ValidationTable",
    "VariableReference",
    "VerificationMode",
    "VerifierEnvironmentTable",
    "VerifierResources",
    "VerifierTable",
    "VerifySpec",
    "host_listed",
    "is_credential_header",
    "is_credential_name",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ImageKind(StrEnum):
    CONTAINER = "container"
    VM = "vm"


class OperatingSystem(StrEnum):
    LINUX = "linux"
    WINDOWS = "windows"
    MACOS = "macos"


def _task_path(path: str, operating_system: OperatingSystem) -> PurePosixPath | PureWindowsPath:
    path_type = PureWindowsPath if operating_system is OperatingSystem.WINDOWS else PurePosixPath
    return path_type(path)


#: Names the Host's artifact copies keep for Orbit: its inventory, and a numbered directory per
#: declaration without a ``destination``.
_RESERVED_DESTINATION = re.compile(r"snapshot\.json|[0-9]+")


class Artifact(BaseModel):
    """One ``artifacts`` entry: a path or glob, below where the agent starts when relative.

    A plain string when nothing is left out. ``exclude`` takes ``tar --exclude`` patterns, as
    in Harbor: they match after any ``/``, so ``.venv`` is that name at any depth.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    exclude: tuple[str, ...] = ()
    destination: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    """Where the Host keeps the copy, relative to the episode's ``artifacts/``, as in Harbor;
    a numbered directory there otherwise. The copy restored for a verifier keeps ``source``."""

    # --- validation ---

    @model_validator(mode="before")
    @classmethod
    def _plain_path(cls, data: Any) -> Any:
        return {"source": data} if isinstance(data, str) else data

    @field_validator("destination")
    @classmethod
    def _relative_destination(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if "\\" in value or path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError(
                "an artifact destination is a relative path with forward slashes and no '..'"
            )
        if _RESERVED_DESTINATION.fullmatch(path.parts[0]):
            raise ValueError(
                f"artifact destination {value!r} takes a name Orbit keeps for its own copies"
            )
        return path.as_posix()


def _validate_artifact_paths(
    artifacts: tuple[Artifact, ...], operating_system: OperatingSystem
) -> None:
    for artifact in artifacts:
        path = _task_path(artifact.source, operating_system)
        if ".." in path.parts or (path.is_absolute() and path == path.parent):
            raise ValueError("artifact paths must not be filesystem roots or contain '..'")
    destinations = [
        PurePosixPath(artifact.destination) for artifact in artifacts if artifact.destination
    ]
    for index, first in enumerate(destinations):
        for second in destinations[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError(f"artifact destinations {first} and {second} overlap")


_ACCOUNT = r"^[a-z_][a-z0-9_-]{0,31}$"
_DNS_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ALLOWED_HOST = re.compile(rf"(?:\*\.)?{_DNS_LABEL}(?:\.{_DNS_LABEL})*")
#: The egress proxy runs on the Host, so an entry reaching these would reach the Host itself
#: or, on a cloud machine, its metadata service. ``0.0.0.0/0`` overlaps them; ``public`` exists.
_HOST_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "fe80::/10",
    )
)


class ImageSpec(BaseModel):
    """A solver or dedicated-verifier image: an image used directly or a build's base.

    Without ``ref`` or ``base_ref``, a container image is built from the Task folder's
    Dockerfile. ``kind`` is ``None`` only for a Linux image named by a registry reference
    alone: what the registry holds decides it, before anything prepares or records the
    image.
    """

    model_config = _FROZEN

    kind: ImageKind | None
    ref: str | None = Field(default=None, min_length=1)
    base_ref: str | None = Field(default=None, min_length=1)
    user: str = Field(default="user", pattern=_ACCOUNT)
    """The agent account. It must exist in the image; preparation fails if it does not."""
    keep_alive: bool = Field(default=False, exclude_if=lambda value: not value)
    """Whether the framework layer replaces a container image's command with one that runs
    until the Sandbox is destroyed, as Harbor replaces every Task container's: a Harbor
    Task's. A VM has no command to replace."""
    multinode: bool = Field(default=False, exclude_if=lambda value: not value)
    """Whether the framework layer adds the OpenSSH that joins the nodes of a Sandbox that
    spans several machines: a Task one of whose Sandboxes does."""
    build_timeout_sec: float = Field(default=3600, gt=0)
    """How long the Task's own build may run. Not part of the image's identity."""

    # --- validation ---

    @field_validator("ref", "base_ref")
    @classmethod
    def _nonblank_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("image ref must not be blank")
        return value

    @model_validator(mode="after")
    def _valid_source(self) -> Self:
        if self.base_ref is not None:
            if self.ref is not None:
                raise ValueError("an image ref and a base ref are mutually exclusive")
            if self.kind is not ImageKind.VM:
                raise ValueError("a base ref requires a VM image")
        elif self.ref is None and self.kind is not ImageKind.CONTAINER:
            raise ValueError("only a container image is built without a ref or base ref")
        return self


class ContainerFeature(StrEnum):
    """A kernel feature a container Sandbox is guaranteed only when its Task asks for it.

    A Run gives a Sandbox only a Provider that gives root in it every feature its Task asks
    for. A leaf may give a feature to every Sandbox, with no grant, as runc gives raw
    sockets and Kata most of them, so a Sandbox that does not ask for one may have it or
    not.
    """

    NESTED_CONTAINERS = "nested_containers"
    """Running containers inside the Sandbox, with a container engine of its own."""
    PERF = "perf"
    """``perf_event_open``, which profilers use."""
    BPF = "bpf"
    """Loading eBPF programs and maps."""
    IO_URING = "io_uring"
    """The ``io_uring`` interface for asynchronous I/O."""
    FUSE = "fuse"
    """``/dev/fuse``, to mount file systems served from user space."""
    RAW_SOCKETS = "raw_sockets"
    """Raw and packet sockets, which ``ping`` and packet capture use."""
    USER_NAMESPACES = "user_namespaces"
    """Creating user namespaces, as rootless containers and browser sandboxes do."""
    MOUNT = "mount"
    """Mounting file systems, and creating mount namespaces, as root."""


def _distinct(features: tuple[ContainerFeature, ...]) -> tuple[ContainerFeature, ...]:
    repeated = sorted({feature for feature in features if features.count(feature) > 1})
    if repeated:
        raise ValueError(f"{', '.join(repeated)} listed more than once")
    return tuple(sorted(features))


#: Container features as a Task lists them, each at most once. They are kept sorted, so
#: the order they were written in changes no comparison or hash.
_ContainerFeatures = Annotated[tuple[ContainerFeature, ...], AfterValidator(_distinct)]


class Resources(BaseModel):
    model_config = _FROZEN

    cpus: int = Field(default=2, ge=1)
    memory_mb: int = Field(default=8192, ge=1)
    storage_mb: int | None = Field(default=None, ge=1)
    gpus: int = Field(default=0, ge=0)
    gpu_types: tuple[str, ...] = ()
    """GPU models the Task accepts, any one of them; empty accepts every model."""
    gpu_memory_mb: int | None = Field(default=None, ge=1)
    """The device memory, in MiB, the Sandbox needs on each of its GPUs, its CUDA contexts
    included; None takes whole GPUs."""
    sudo: bool = False
    container_features: _ContainerFeatures = ()
    """What a container Sandbox needs beyond what every container gets."""
    nodes: int = Field(default=1, ge=1, exclude_if=lambda value: value == 1)
    """The machines the Sandbox spans, each with the CPUs, memory, storage and GPUs above.
    Left out of a dump at one, so a single-node Task keeps its hash."""


class NetworkMode(StrEnum):
    BLOCK = "block"
    ALLOWLIST = "allowlist"
    BLOCKLIST = "blocklist"
    OPEN = "open"


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def host_listed(host: str, hosts: Iterable[str]) -> bool:
    """Whether ``hosts``, an allowlist or a blocklist, names ``host``.

    A name matches itself; ``*.example.org`` matches ``example.org`` and every name below it,
    at any depth, which every Provider can enforce. An address matches itself and any range
    holding it, and never a name pattern.
    """
    name = host.lower().rstrip(".")
    address = _address(name)
    for listed in (entry.lower() for entry in hosts):
        if address is None:
            pattern = listed.removeprefix("*.") if listed.startswith("*.") else None
            if name in (listed, pattern) or (pattern and name.endswith("." + pattern)):
                return True
        elif listed == address.compressed or (
            "/" in listed and address in ipaddress.ip_network(listed)
        ):
            return True
    return False


class NetworkPolicy(BaseModel):
    model_config = _FROZEN

    mode: NetworkMode = NetworkMode.BLOCK
    allowed_hosts: tuple[str, ...] = ()
    blocked_hosts: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    """Left out of a dump when empty, so a policy without a blocklist keeps its hash."""

    @property
    def proxied(self) -> bool:
        """Whether a Sandbox under it is sealed and reaches hosts only through the Host's
        egress proxy, which decides each connection by the policy."""
        return self.mode in (NetworkMode.ALLOWLIST, NetworkMode.BLOCKLIST)

    def admits(self, host: str) -> bool:
        """Whether a client asking for ``host``, a name or an address, may reach it."""
        if self.mode is NetworkMode.ALLOWLIST:
            return host_listed(host, self.allowed_hosts)
        if self.mode is NetworkMode.BLOCKLIST:
            return not host_listed(host, self.blocked_hosts)
        return self.mode is NetworkMode.OPEN

    # --- validation ---

    @model_validator(mode="after")
    def _check_hosts(self) -> Self:
        for mode, hosts, field in (
            (NetworkMode.ALLOWLIST, self.allowed_hosts, "allowed_hosts"),
            (NetworkMode.BLOCKLIST, self.blocked_hosts, "blocked_hosts"),
        ):
            if self.mode is mode and not hosts:
                raise ValueError(f"{mode} mode requires at least one host in {field}")
            if self.mode is not mode and hosts:
                raise ValueError(f"{field} is meaningless in {self.mode} mode")
        return self


class PhaseTimeouts(BaseModel):
    model_config = _FROZEN

    agent: float = Field(default=1800, gt=0)
    verify: float = Field(default=600, gt=0)
    validation: float | None = Field(default=None, gt=0, exclude_if=lambda value: value is None)
    """A validation verifier's limit, ``[verifier.validation].timeout_sec`` as it resolves over
    ``[verifier]``'s; ``None`` without that table, for ``verify``."""


class SkillSource(BaseModel):
    model_config = _FROZEN

    path: str = Field(min_length=1)
    origin: Literal["preset", "run", "cli"] | None = Field(default=None, exclude=True)
    declared: str | None = Field(default=None, exclude=True)


class McpSource(BaseModel):
    model_config = _FROZEN

    path: str | None = Field(default=None, min_length=1)
    builtin: str | None = Field(default=None, min_length=1)
    #: The longest side, in pixels, of a screenshot the built-in ``cua-desktop`` returns;
    #: unset, it returns the screen as Cua Driver captures it.
    screenshot_max_dimension: int | None = Field(default=None, ge=1)
    origin: Literal["preset", "run", "cli"] | None = Field(default=None, exclude=True)
    declared: str | None = Field(default=None, exclude=True)

    # --- validation ---

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.path is None) == (self.builtin is None):
            raise ValueError("an MCP source requires exactly one of path or builtin")
        if self.screenshot_max_dimension is not None and self.builtin != "cua-desktop":
            raise ValueError("screenshot_max_dimension applies to the built-in cua-desktop only")
        return self


class StdioMcpServer(BaseModel):
    model_config = _FROZEN

    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    cwd: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _absolute_cwd(self) -> Self:
        if self.cwd is not None and not (
            PurePosixPath(self.cwd).is_absolute()
            or PureWindowsPath(self.cwd).is_absolute()
            or self.cwd == "{mcp}"
            or self.cwd.startswith(("{mcp}/", "{mcp}\\"))
        ):
            raise ValueError("stdio MCP cwd must be an absolute sandbox path")
        return self


class StreamableHttpMcpServer(BaseModel):
    model_config = _FROZEN

    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    transport: Literal["streamable-http"]
    url: str = Field(pattern=r"^https?://")


class SseMcpServer(BaseModel):
    """A remote server on MCP's HTTP+SSE transport, Harbor's default ``transport``."""

    model_config = _FROZEN

    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    transport: Literal["sse"]
    url: str = Field(pattern=r"^https?://")


McpServer = Annotated[
    StdioMcpServer | StreamableHttpMcpServer | SseMcpServer,
    Field(discriminator="transport"),
]
#: The servers an agent reaches over the network, by URL.
RemoteMcpServer = StreamableHttpMcpServer | SseMcpServer


class Healthcheck(BaseModel):
    """``[environment.healthcheck]``: Harbor's readiness check, run once setup is done, with
    Docker HEALTHCHECK's semantics. The first success passes; a failure counts only once
    ``start_period_sec`` has passed, and ``retries`` failures in a row fail the Task."""

    model_config = _FROZEN

    command: str = Field(min_length=1)
    interval_sec: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    timeout_sec: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    start_period_sec: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    start_interval_sec: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    retries: int = Field(default=3, ge=1, strict=True)


class ToolProvision(BaseModel):
    """What a Task's image gives its agent, as ``[environment]`` declares it."""

    model_config = _FROZEN

    skills_dir: str | None = None
    mcp_servers: tuple[McpServer, ...] = ()


class VerificationMode(StrEnum):
    """Where the verifier runs: in the solver's Sandbox (``shared``, Harbor's), in a new one
    of its own image given the declared artifacts (``separate``, Harbor's), or in a new one
    booted from a snapshot of the solver's disk (``snapshot``, Orbit's)."""

    SHARED = "shared"
    SEPARATE = "separate"
    SNAPSHOT = "snapshot"


class VerifierResources(BaseModel):
    """Explicit resources for a verifier Sandbox apart from the solver's."""

    model_config = _FROZEN

    cpus: int = Field(ge=1)
    memory_mb: int = Field(ge=1)
    storage_mb: int | None = Field(ge=1)
    gpus: int = Field(ge=0)
    gpu_types: tuple[str, ...] = ()
    gpu_memory_mb: int | None = Field(default=None, ge=1)
    container_features: _ContainerFeatures = ()
    nodes: int = Field(default=1, ge=1, exclude_if=lambda value: value == 1)

    def as_resources(self) -> Resources:
        return Resources(
            cpus=self.cpus,
            memory_mb=self.memory_mb,
            storage_mb=self.storage_mb,
            gpus=self.gpus,
            gpu_types=self.gpu_types,
            gpu_memory_mb=self.gpu_memory_mb,
            sudo=False,
            container_features=self.container_features,
            nodes=self.nodes,
        )


class VerifySpec(BaseModel):
    model_config = _FROZEN

    environment_mode: VerificationMode = VerificationMode.SHARED
    image: ImageSpec | None = None
    resources: VerifierResources | None = None
    workdir: str | None = Field(default=None, exclude_if=lambda value: value is None)
    """Harbor's working directory for ``tests/`` and ``validation/``, below the agent's home
    when relative; ``None`` runs each in its own stage directory."""
    user: str | None = Field(default=None, exclude_if=lambda value: value is None)
    """``[verifier].user``: the account, by name or uid, ``tests/`` and ``validation/`` run
    as; ``None`` for the framework's identity."""
    network: NetworkPolicy = Field(
        default=NetworkPolicy(mode=NetworkMode.OPEN),
        exclude_if=lambda value: value.mode is NetworkMode.OPEN,
    )
    """The network ``tests/`` and ``validation/`` run with (``TaskConfig.verifier_network``);
    open unless the Task says otherwise."""

    # --- validation ---

    @model_validator(mode="after")
    def _valid_topology(self) -> Self:
        if self.image is not None and self.image.base_ref is not None:
            raise ValueError("a verifier image is used directly or built, never from a base ref")
        if self.environment_mode is VerificationMode.SHARED:
            if self.image is not None or self.resources is not None:
                raise ValueError("shared verification cannot declare image or resources")
            return self
        if self.resources is None:
            raise ValueError(f"{self.environment_mode} verification requires explicit resources")
        if self.environment_mode is VerificationMode.SNAPSHOT and self.image is not None:
            raise ValueError("a snapshot verifier boots from the solver's snapshot, not an image")
        return self


_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}", re.DOTALL)
_EMBEDDED_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
#: The prefix of the framework's own variables: those it sets in the commands it runs, and
#: the Host configuration it reads, model and Judge keys included.
_RESERVED_PREFIX = "ORBIT_"


class VariableReference(NamedTuple):
    """A whole value ``${NAME}`` or ``${NAME:-default}``, resolved on the Host."""

    name: str
    default: str | None

    @classmethod
    def parse(cls, value: str) -> VariableReference | None:
        """The reference ``value`` is, or ``None`` for a literal.

        Only a whole value refers to a variable, as in Harbor: ``Bearer ${TOKEN}`` is text.
        """
        match = _REFERENCE.fullmatch(value)
        return cls(match[1], match[2]) if match else None

    @classmethod
    def embedded(cls, value: str) -> tuple[VariableReference, ...]:
        """Every reference within ``value``, the form an MCP header takes, as in Harbor."""
        if "${" in _EMBEDDED_REFERENCE.sub(" ", value):
            raise ValueError("a ${ must start ${NAME} or ${NAME:-default}")
        return tuple(cls(match[1], match[2]) for match in _EMBEDDED_REFERENCE.finditer(value))

    @classmethod
    def substitute(cls, value: str, found: Mapping[VariableReference, str]) -> str:
        """``value`` with each of its embedded references replaced by what ``found`` holds."""
        return _EMBEDDED_REFERENCE.sub(lambda match: found[cls(match[1], match[2])], value)


def _valid_env(env: dict[str, str]) -> dict[str, str]:
    for key, value in env.items():
        if not _VARIABLE_NAME.fullmatch(key):
            raise ValueError(f"{key!r} is not an environment variable name")
        if key.upper().startswith(_RESERVED_PREFIX):
            raise ValueError(f"{key}: the {_RESERVED_PREFIX} prefix is reserved for the framework")
        reference = VariableReference.parse(value)
        if reference is not None and reference.name.upper().startswith(_RESERVED_PREFIX):
            # The Host's own configuration, model and Judge keys included, never enters a
            # Sandbox through a Task.
            raise ValueError(f"{key}: a Task cannot refer to the Host's {reference.name}")
    return env


#: A Harbor ``env`` table: names to literals or whole-value ``${NAME}`` references.
TaskEnv = Annotated[dict[str, str], AfterValidator(_valid_env)]

#: A name that marks a credential, as Harbor recognizes one; ``MAX_TOKENS`` is a count and
#: ``TOKENIZERS_PARALLELISM``, of Hugging Face's tokenizers, a switch.
_CREDENTIAL_NAME = re.compile(r"KEY|SECRET|TOKEN(?!S|IZE)|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
#: The one shape a credential header takes: an optional scheme, then only references.
_CREDENTIAL_HEADER = re.compile(
    r"\s*(?:[A-Za-z]+\s+)?\$\{[A-Za-z_][A-Za-z0-9_]*\}(?:[\s:-]*\$\{[A-Za-z_][A-Za-z0-9_]*\})*\s*"
)


def is_credential_name(name: str) -> bool:
    return bool(_CREDENTIAL_NAME.search(name))


def is_credential_header(name: str) -> bool:
    return is_credential_name(name) or name.lower() in {"cookie", "set-cookie"}


def _valid_headers(headers: dict[str, str]) -> dict[str, str]:
    for name, value in headers.items():
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not an HTTP header name")
        if "\r" in value or "\n" in value:
            raise ValueError(f"header {name} must not contain a line break")
        try:
            references = VariableReference.embedded(value)
        except ValueError as error:
            raise ValueError(f"header {name}: {error}") from error
        for reference in references:
            if reference.name.upper().startswith(_RESERVED_PREFIX):
                raise ValueError(f"header {name}: a Task cannot refer to {reference.name}")
        if is_credential_header(name) and not _CREDENTIAL_HEADER.fullmatch(value):
            # A task.toml is committed, so it holds the variable, never the credential.
            raise ValueError(
                f"header {name} holds a credential; write it as ${{NAME}} or Bearer ${{NAME}}, "
                "with no default, and set NAME in the checkout's .env"
            )
    return headers


#: An MCP server's HTTP headers: literal text with ``${NAME}`` references inside it.
McpHeaders = Annotated[dict[str, str], AfterValidator(_valid_headers)]


def _expectation(value: Any) -> Any:
    """A number, or a closed interval ``[lo, hi]`` of numbers; a boolean is neither."""
    interval = isinstance(value, list | tuple)
    bounds = value if interval else (value,)
    if interval and len(bounds) != 2:
        raise ValueError("an interval is written [lo, hi]")
    for bound in bounds:
        if isinstance(bound, bool) or not isinstance(bound, int | float):
            raise ValueError("expected a number or an interval [lo, hi] of numbers")
        if not math.isfinite(bound):
            raise ValueError("expected a finite number")
    if interval and bounds[0] > bounds[1]:
        raise ValueError(f"the interval [{bounds[0]}, {bounds[1]}] needs lo <= hi")
    return tuple(float(bound) for bound in bounds) if interval else float(value)


#: What a calibration case expects of one value: that number exactly, or a closed interval.
Expectation = Annotated[float | tuple[float, float], BeforeValidator(_expectation)]

#: A calibration case's name: a lowercase slug, as each part of a Task id is.
_CASE_NAME = r"^[a-z0-9][a-z0-9_-]*$"
#: The precision of a stored reward.
_CENT = Decimal("0.01")
#: The case of the Task's reference solution, the Task root's ``solution/``.
SOLUTION_CASE = "solution"


class CalibrationCase(BaseModel):
    """One ``[[calibration]]`` entry: a known final state and the scores it must receive."""

    model_config = _FROZEN

    name: str = Field(pattern=_CASE_NAME)
    expect: dict[Annotated[str, Field(min_length=1)], Expectation]
    """``reward`` and any other top-level number of the Verification Result, each with the
    number it must equal or the closed interval it must fall in."""
    expect_validation: dict[Annotated[str, Field(min_length=1)], Expectation] | None = None
    """What ``validation/``'s result must be, by the same rules, where it differs from
    ``expect``."""

    @property
    def validation_expect(self) -> dict[str, float | tuple[float, float]]:
        """What the validation check's result must be: ``expect_validation``, else
        ``expect``."""
        return self.expect if self.expect_validation is None else self.expect_validation

    def admits(self, key: str, value: float, *, validation: bool = False) -> bool:
        """Whether ``value`` is what this case expects of ``key``, of the validation check's
        result when ``validation``."""
        expected = (self.validation_expect if validation else self.expect)[key]
        if isinstance(expected, tuple):
            return expected[0] <= value <= expected[1]
        return value == expected

    # --- validation ---

    @field_validator("expect", "expect_validation")
    @classmethod
    def _reward(
        cls, expect: dict[str, float | tuple[float, float]] | None
    ) -> dict[str, float | tuple[float, float]] | None:
        if expect is None:
            return None
        reward = expect.get("reward")
        if reward is None:
            raise ValueError("must include reward")
        bounds = reward if isinstance(reward, tuple) else (reward,)
        if not all(0 <= bound <= 1 for bound in bounds):
            raise ValueError("reward must lie from 0 through 1")
        # A stored reward has two decimals, so a reward with more, or an interval holding
        # none with two, could never match.
        low, high = (Decimal(repr(bound)) for bound in (bounds[0], bounds[-1]))
        if not isinstance(reward, tuple) and low != low.quantize(_CENT):
            raise ValueError("an exact reward has at most two decimals, as a stored reward does")
        if low.quantize(_CENT, rounding=ROUND_CEILING) > high:
            raise ValueError(f"the reward interval {list(reward)} holds no two-decimal value")
        return expect


#: The case of the reference solution, which every Task has, as it is unless a
#: ``[[calibration]]`` entry named ``solution`` says otherwise: it earns full marks.
DEFAULT_SOLUTION = CalibrationCase(name=SOLUTION_CASE, expect={"reward": 1.0})


class TaskVariables(BaseModel):
    """Environment variables a Task declares, by the ``task.toml`` table declaring them.

    A TaskSpec holds them as written; resolved, they hold what each command receives.
    Variables the framework or a Harness sets for a command win over them.
    """

    model_config = _FROZEN

    environment: dict[str, str] = Field(default_factory=dict)
    """``[environment].env``: setup, the agent and the oracle, and a shared verifier."""
    verifier: dict[str, str] = Field(default_factory=dict)
    """``[verifier].env``: the verifier only."""
    verifier_environment: dict[str, str] | None = None
    """``[verifier.environment].env``. ``None`` without that table: a verifier in a Sandbox of
    its own then receives ``environment``, as Harbor copies ``[environment]`` for it."""
    validation: dict[str, str] | None = Field(default=None, exclude_if=lambda value: value is None)
    """``[verifier.validation].env``, over ``verifier`` for a validation verifier; ``None``
    without that table."""
    validation_environment: dict[str, str] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """``[verifier.validation.environment].env``, over ``verifier_environment`` for a
    validation verifier of its own; ``None`` without that table."""
    solution: dict[str, str] = Field(default_factory=dict)
    """``[solution].env``: the oracle only."""
    mcp_headers: dict[str, dict[str, str]] = Field(default_factory=dict)
    """``[[environment.mcp_servers]].headers`` by server: what the agent's MCP client sends."""

    @property
    def tables(self) -> tuple[tuple[str, dict[str, str]], ...]:
        """Each declared table by its ``task.toml`` name."""
        tables = [("environment", self.environment), ("verifier", self.verifier)]
        for name, env in (
            ("verifier.environment", self.verifier_environment),
            ("verifier.validation", self.validation),
            ("verifier.validation.environment", self.validation_environment),
        ):
            if env is not None:
                tables.append((name, env))
        tables.append(("solution", self.solution))
        return tuple(tables)

    def oracle(self) -> dict[str, str]:
        return {**self.environment, **self.solution}

    def verification(self, mode: VerificationMode, *, validation: bool = False) -> dict[str, str]:
        """What a verifier placed by ``mode`` receives: that of ``tests/``, or of validation
        rounds when ``validation``, whose tables go over the verifier's."""
        environments = [self.verifier_environment]
        if validation:
            environments.append(self.validation_environment)
        declared = [env for env in environments if env is not None]
        base: dict[str, str] = self.environment
        if mode is not VerificationMode.SHARED and declared:
            base = {key: value for env in declared for key, value in env.items()}
        own = self.validation if validation and self.validation is not None else {}
        return {**base, **self.verifier, **own}


class TaskSpec(BaseModel):
    """The Task's configuration and instruction as the runtime uses them."""

    model_config = _FROZEN

    name: TaskId
    os: OperatingSystem = OperatingSystem.LINUX
    image: ImageSpec
    instruction: str
    resources: Resources = Resources()
    network: NetworkPolicy = NetworkPolicy()
    workdir: str | None = None
    """``[agent].workdir``: where the evaluated agent starts, below its home when relative;
    ``None`` for its home."""
    timeouts: PhaseTimeouts = PhaseTimeouts()
    validation_rounds: int = Field(default=0, ge=0)
    """``[agent].validation_rounds``: how many results of ``validation/``, or else ``tests/``,
    the evaluated agent receives, each between two of its turns."""
    validation_visible: bool = Field(default=False, exclude_if=lambda value: not value)
    """``[verifier.validation].visibility``: whether the agent and the oracle work beside a
    read-only copy of the stage that validates."""
    healthcheck: Healthcheck | None = Field(default=None, exclude_if=lambda value: value is None)
    """``[environment.healthcheck]``, run where the agent starts once setup is done."""
    artifacts: tuple[Artifact, ...] = ()
    tools: ToolProvision = ToolProvision()
    verify: VerifySpec = VerifySpec()
    validation: VerifySpec | None = None
    """The verifier of validation rounds, where ``[verifier.validation]``, or a
    ``validation/Dockerfile``, sets it apart from ``tests/``'s; :attr:`validation_spec`
    resolves it."""
    variables: TaskVariables = TaskVariables()
    calibration: tuple[CalibrationCase, ...] = (DEFAULT_SOLUTION,)
    """Every calibration case, in the order ``orbit calibrate`` runs them."""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def images(self) -> tuple[ImageSpec, ...]:
        """Its images: the solver's and each verifier's own."""
        verifiers = (self.verify, *((self.validation,) if self.validation else ()))
        return (self.image, *(each.image for each in verifiers if each.image is not None))

    @property
    def spec_hash(self) -> str:
        if any(image.kind is None for image in self.images):
            raise ValueError("a specification is hashed once the kinds of its images are decided")
        return content_hash(self.model_dump(mode="json"))

    def start_directory(self, home: str) -> str:
        """Where the agent starts, given the agent's ``home``."""
        return str(_task_path(home, self.os) / (self.workdir or ""))

    @property
    def validation_spec(self) -> VerifySpec:
        """The verifier of validation rounds: its own, or ``tests/``'s."""
        return self.validation or self.verify

    @property
    def validation_timeout(self) -> float:
        """The verifier of validation rounds' limit: its own, or ``tests/``'s."""
        return (
            self.timeouts.verify if self.timeouts.validation is None else self.timeouts.validation
        )

    @property
    def solver_network(self) -> NetworkPolicy:
        """The network a solver Sandbox is created with: the agent's, and when a verifier
        runs in it or in a Sandbox booted from its snapshot as a shared one, one that can also
        apply that verifier's. A Provider fixes at creation whether it can seal a Sandbox and
        relay it to the egress proxy, so this is the least open provisioning every phase
        needs: a proxied policy (``allowlist`` or ``blocklist``), with every host when all are
        allowlists and else the agent's or, when the agent's is not proxied, a verifier's;
        then ``block``; then ``open``. Each phase then applies its own policy."""
        shared = [
            placed.network
            for placed in dict.fromkeys((self.verify, self.validation_spec))
            if placed.environment_mode is VerificationMode.SHARED
        ]
        if not shared:
            return self.network
        policies = (self.network, *shared)
        if all(policy.mode is NetworkMode.ALLOWLIST for policy in policies):
            hosts = dict.fromkeys(host for policy in policies for host in policy.allowed_hosts)
            return NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=tuple(hosts))
        if proxied := next((policy for policy in policies if policy.proxied), None):
            return proxied
        if any(policy.mode is NetworkMode.BLOCK for policy in policies):
            return NetworkPolicy(mode=NetworkMode.BLOCK)
        return NetworkPolicy(mode=NetworkMode.OPEN)

    @property
    def volume_candidates(self) -> tuple[str, ...]:
        """The artifacts a Provider may mount as volumes of their own: those named by an
        absolute path, without a glob, that no other of them contains, as it decides before
        the Sandbox starts, when the agent's home is not yet known. A Provider mounts those
        that are directories in the image."""
        path_type = PureWindowsPath if self.os is OperatingSystem.WINDOWS else PurePosixPath
        named = [
            artifact.source
            for artifact in self.artifacts
            if path_type(artifact.source).is_absolute()
            and not any(char in artifact.source for char in "*?[")
        ]
        return tuple(
            dict.fromkeys(
                source
                for source in named
                if not any(
                    other != source and path_type(other) in path_type(source).parents
                    for other in named
                )
            )
        )

    def artifact_path(self, artifact: Artifact, home: str) -> str:
        """Where ``artifact`` is, given the agent's ``home``."""
        return str(_task_path(self.start_directory(home), self.os) / artifact.source)

    # --- validation ---

    @model_validator(mode="after")
    def _valid_artifacts(self) -> Self:
        _validate_artifact_paths(self.artifacts, self.os)
        return self

    @model_validator(mode="after")
    def _kind_left_open_only_for_a_registry(self) -> Self:
        for image in self.images:
            if image.kind is not None:
                continue
            local = image.ref is not None and PurePosixPath(image.ref).is_absolute()
            if self.os is not OperatingSystem.LINUX or local:
                raise ValueError(
                    "only a Linux image named by a registry reference leaves its kind open"
                )
        return self

    @model_validator(mode="after")
    def _container_features_need_a_container(self) -> Self:
        sandboxes = [("solver", self.image, self.resources)]
        for role, placed in (("verifier", self.verify), ("validation verifier", self.validation)):
            if placed is not None and placed.resources is not None:
                image = placed.image or self.image
                sandboxes.append((role, image, placed.resources.as_resources()))
        for role, image, resources in sandboxes:
            if resources.container_features and image.kind is ImageKind.VM:
                raise ValueError(
                    f"the {role} image is a VM, which takes no container_features: "
                    "a VM has a kernel of its own"
                )
        return self


#: The ``task.toml`` format Orbit reads, named by the major and minor Orbit version that
#: last changed it, as Harbor names its own. A Harbor Task, naming one of Harbor's formats
#: or none, is read as this one, with a warning; a Task naming any other format fails.
SCHEMA_VERSION = "0.2"
#: Harbor's format versions, ``1.0`` through its current ``1.4``. A Task naming one, or none,
#: which Harbor reads as its own current version, is Harbor's (``TaskConfig.harbor``).
_HARBOR_SCHEMA = re.compile(r"1\.[0-9]+")
#: The account Harbor's agent runs as in an image that names no other ``USER``.
HARBOR_ACCOUNT = "root"

#: ``task.toml`` network modes and what they mean for the agent, the oracle and the verifier.
#: ``blocklist`` is Orbit's; Harbor's format has the other three.
_NETWORK_MODES = {
    "no-network": NetworkMode.BLOCK,
    "allowlist": NetworkMode.ALLOWLIST,
    "blocklist": NetworkMode.BLOCKLIST,
    "public": NetworkMode.OPEN,
}

_HARBOR_ENVIRONMENT_ONLY = frozenset({"memory", "storage", "tpu"})
_VERIFIER_ENVIRONMENT_ONLY = _HARBOR_ENVIRONMENT_ONLY | {
    "healthcheck",
    "mcp_servers",
    "os",
    "skills_dir",
}

#: A ``gpu_types`` entry: a GPU model as Harbor names it, such as ``H100`` or ``RTX-PRO-6000``.
_GpuType = Annotated[str, Field(pattern=r"[A-Za-z0-9]")]

#: Harbor ``task.toml`` keys that Orbit does not implement, by table. They fail with a
#: message saying so rather than as unknown keys, because a Harbor author expects them.
_UNSUPPORTED_HARBOR_KEYS: dict[tuple[str, ...], frozenset[str]] = {
    (): frozenset({"multi_step_reward_strategy", "source", "steps"}),
    ("environment",): _HARBOR_ENVIRONMENT_ONLY,
    ("verifier",): frozenset({"collect"}),
    ("verifier", "validation"): frozenset({"collect"}),
    # Harbor runs a healthcheck in the agent's environment only.
    ("verifier", "environment"): _VERIFIER_ENVIRONMENT_ONLY,
    ("verifier", "validation", "environment"): _VERIFIER_ENVIRONMENT_ONLY,
}


class Author(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    email: str | None = None


class TaskTable(BaseModel):
    """``[task]``: the Task's identity; everything else here is descriptive."""

    model_config = _FROZEN

    name: TaskId | None = None
    """``None`` identifies the Task by where it lives instead, as Harbor does."""
    description: str = ""
    version: str | None = Field(default=None, min_length=1)
    authors: tuple[Author, ...] = ()
    keywords: tuple[str, ...] = ()


class McpServerTable(BaseModel):
    """``[[environment.mcp_servers]]``: Harbor's entry for a server the image provides."""

    model_config = _FROZEN

    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    transport: Literal["stdio", "sse", "streamable-http"] = "sse"
    """As in Harbor, ``sse`` unless written; ``http`` means ``streamable-http``."""
    command: str | None = Field(default=None, min_length=1)
    args: tuple[str, ...] = ()
    url: str | None = Field(default=None, pattern=r"^https?://")
    headers: McpHeaders = Field(default_factory=dict)

    @property
    def server(self) -> StdioMcpServer | StreamableHttpMcpServer | SseMcpServer:
        if self.transport == "stdio":
            assert self.command is not None
            return StdioMcpServer(
                name=self.name, transport="stdio", command=self.command, args=self.args
            )
        assert self.url is not None
        if self.transport == "sse":
            return SseMcpServer(name=self.name, transport="sse", url=self.url)
        return StreamableHttpMcpServer(name=self.name, transport="streamable-http", url=self.url)

    # --- validation ---

    @field_validator("transport", mode="before")
    @classmethod
    def _harbor_spelling(cls, value: Any) -> Any:
        return "streamable-http" if value == "http" else value

    @model_validator(mode="after")
    def _fields_of_its_transport(self) -> Self:
        if self.transport == "stdio" and (
            self.command is None or self.url is not None or self.headers
        ):
            raise ValueError("transport 'stdio' requires command and takes no url or headers")
        if self.transport != "stdio" and (
            self.url is None or self.command is not None or self.args
        ):
            raise ValueError(f"transport {self.transport!r} requires url and takes no command")
        return self

    @model_validator(mode="after")
    def _credentials_stay_encrypted(self) -> Self:
        parts = urlsplit(self.url or "")
        if parts.scheme == "http" and not _is_loopback(parts.hostname or ""):
            for name in filter(is_credential_header, self.headers):
                raise ValueError(f"header {name} would cross the network unencrypted; use https")
        return self


def _is_loopback(host: str) -> bool:
    address = _address(host)
    return host == "localhost" or (address is not None and address.is_loopback)


def _normal_host(host: str) -> str:
    """An ``allowed_hosts`` or ``blocked_hosts`` entry in Harbor's normal form."""
    name = host.strip().lower().rstrip(".")
    try:
        network = ipaddress.ip_network(name)
    except ValueError:
        if not _ALLOWED_HOST.fullmatch(name):
            raise ValueError(
                f"{host!r} is not a hostname, *.hostname, address or CIDR range"
            ) from None
        return name
    if any(
        network.version == reserved.version and network.overlaps(reserved)
        for reserved in _HOST_NETWORKS
    ):
        raise ValueError(f"{host} reaches the Host itself or its cloud metadata service")
    return network.compressed if "/" in name else network.network_address.compressed


#: A ``task.toml`` network mode, as Harbor spells its three, and Orbit's ``blocklist``.
_NetworkModeName = Literal["no-network", "allowlist", "blocklist", "public"]


def _table_network(
    mode: str | None,
    allowed: tuple[str, ...] | None,
    blocked: tuple[str, ...] | None,
    allow_internet: bool | None = None,
) -> NetworkPolicy | None:
    """The policy a table names, with Harbor's deprecated ``allow_internet`` read only when it
    names no mode; ``None`` when it names none."""
    if mode is None and allow_internet is not None:
        mode = "public" if allow_internet else "no-network"
    if mode is None:
        return None
    return NetworkPolicy(
        mode=_NETWORK_MODES[mode], allowed_hosts=allowed or (), blocked_hosts=blocked or ()
    )


def _check_table_network(
    mode: str | None, allowed: tuple[str, ...] | None, blocked: tuple[str, ...] | None
) -> None:
    for name, hosts, field in (
        ("allowlist", allowed, "allowed_hosts"),
        ("blocklist", blocked, "blocked_hosts"),
    ):
        if mode == name and not hosts:
            raise ValueError(f"network_mode {name!r} requires a non-empty {field}")
        if mode != name and hosts is not None:
            raise ValueError(f"{field} requires network_mode {name!r}")


def _harbor_image_key(data: Any) -> Any:
    """Harbor's ``docker_image`` is ``image`` under its Harbor name: one field, written once."""
    if not isinstance(data, dict) or "docker_image" not in data:
        return data
    if "image" in data:
        raise ValueError("image and docker_image are one field; write only one of them")
    return {("image" if key == "docker_image" else key): value for key, value in data.items()}


class EnvironmentTable(BaseModel):
    """``[environment]``: the machine the agent starts on."""

    model_config = _FROZEN

    os: OperatingSystem = OperatingSystem.LINUX
    image: str | None = Field(default=None, min_length=1)
    """An image used directly, or the base ``environment/``'s build script builds on;
    Harbor's ``docker_image`` names it too."""
    build_timeout_sec: float = Field(default=3600, gt=0, allow_inf_nan=False, strict=True)
    cpus: int = Field(default=2, ge=1, strict=True)
    memory_mb: int = Field(default=8192, ge=1, strict=True)
    storage_mb: int | None = Field(default=None, ge=1, strict=True)
    gpus: int = Field(default=0, ge=0, strict=True)
    gpu_types: tuple[_GpuType, ...] | None = Field(default=None, min_length=1)
    """GPU models any of which will do; ``None`` accepts every model, as in Harbor."""
    gpu_memory_mb: int | None = Field(default=None, ge=1, strict=True)
    """The device memory the Sandbox needs on each GPU; ``None`` takes whole GPUs."""
    container_features: _ContainerFeatures = ()
    """What the container needs beyond what every container gets; a VM image takes none."""
    nodes: int = Field(default=1, ge=1, strict=True)
    """Orbit's: the machines the agent's Sandbox spans, each of the size above."""
    env: TaskEnv = Field(default_factory=dict)
    skills_dir: str | None = Field(default=None, min_length=1)
    mcp_servers: tuple[McpServerTable, ...] = ()
    healthcheck: Healthcheck | None = None
    network_mode: _NetworkModeName | None = None
    """Harbor's baseline: the agent's network unless ``[agent]`` names its own, and the
    verifier's unless ``[verifier]``, or a verifier of its own's environment, names one."""
    allowed_hosts: tuple[str, ...] | None = None
    blocked_hosts: tuple[str, ...] | None = None
    allow_internet: bool | None = Field(default=None, strict=True)
    """Harbor's deprecated switch, read only without ``network_mode``: ``true`` is
    ``public``, ``false`` ``no-network``."""
    workdir: str | None = Field(default=None, min_length=1)
    """Harbor's working directory: where the agent starts unless ``[agent].workdir`` says
    otherwise, and where ``tests/`` and ``validation/`` run; below the agent's home when
    relative."""

    @property
    def network(self) -> NetworkPolicy | None:
        """The baseline this table names, or ``None``."""
        return _table_network(
            self.network_mode, self.allowed_hosts, self.blocked_hosts, self.allow_internet
        )

    # --- validation ---

    @model_validator(mode="before")
    @classmethod
    def _docker_image(cls, data: Any) -> Any:
        return _harbor_image_key(data)

    @field_validator("allowed_hosts", "blocked_hosts")
    @classmethod
    def _normal_hosts(cls, hosts: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if hosts is None else tuple(_normal_host(host) for host in hosts)

    @model_validator(mode="after")
    def _hosts_only_for_their_mode(self) -> Self:
        _check_table_network(self.network_mode, self.allowed_hosts, self.blocked_hosts)
        return self

    @field_validator("os", mode="before")
    @classmethod
    def _any_case(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value


class AgentTable(BaseModel):
    """``[agent]``: the evaluated agent's account, budget and network; the oracle's too.

    A Harbor Task's account defaults to Harbor's (``TaskConfig.agent_user``), and a root
    account's elevation to true (``TaskConfig.agent_sudo``), so read those two through
    ``TaskConfig``: a field's own default holds only where nothing else decides it.
    """

    model_config = _FROZEN

    user: str = Field(default="user", pattern=_ACCOUNT)
    timeout_sec: float = Field(default=1800, gt=0, allow_inf_nan=False, strict=True)
    network_mode: _NetworkModeName | None = None
    """Unset, ``[environment]``'s baseline applies, as in Harbor, and else no network."""
    allowed_hosts: tuple[str, ...] | None = None
    blocked_hosts: tuple[str, ...] | None = None
    sudo: bool = Field(default=False, strict=True)
    workdir: str | None = Field(default=None, min_length=1)
    validation_rounds: int = Field(default=0, ge=0, strict=True)
    """How many results of ``validation/``, or else ``tests/``, the agent receives, each
    between two of its turns; ``N`` gives it ``N + 1`` turns."""

    @property
    def network(self) -> NetworkPolicy | None:
        """The policy this table names, or ``None``."""
        return _table_network(self.network_mode, self.allowed_hosts, self.blocked_hosts)

    # --- validation ---

    @field_validator("allowed_hosts", "blocked_hosts")
    @classmethod
    def _normal_hosts(cls, hosts: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if hosts is None else tuple(_normal_host(host) for host in hosts)

    @model_validator(mode="after")
    def _hosts_only_for_their_mode(self) -> Self:
        _check_table_network(self.network_mode, self.allowed_hosts, self.blocked_hosts)
        return self


class VerifierEnvironmentTable(BaseModel):
    """``[verifier.environment]``: a separate verifier's own image, resources and network
    baseline, or a snapshot verifier's resources and network baseline.

    ``cpus`` and ``memory_mb`` fall back to ``[environment]``'s.
    """

    model_config = _FROZEN

    image: str | None = Field(default=None, min_length=1)
    """An image used directly, also named ``docker_image`` as in Harbor; ``tests/Dockerfile``
    builds one otherwise."""
    build_timeout_sec: float = Field(default=3600, gt=0, allow_inf_nan=False, strict=True)
    cpus: int | None = Field(default=None, ge=1, strict=True)
    memory_mb: int | None = Field(default=None, ge=1, strict=True)
    storage_mb: int | None = Field(default=None, ge=1, strict=True)
    gpus: int = Field(default=0, ge=0, strict=True)
    gpu_types: tuple[_GpuType, ...] | None = Field(default=None, min_length=1)
    gpu_memory_mb: int | None = Field(default=None, ge=1, strict=True)
    container_features: _ContainerFeatures = ()
    nodes: int = Field(default=1, ge=1, strict=True)
    """Orbit's: the machines the verifier's Sandbox spans, each of the size above."""
    env: TaskEnv = Field(default_factory=dict)
    workdir: str | None = Field(default=None, min_length=1)
    """Where a verifier of its own runs ``tests/`` and ``validation/``, instead of
    ``[environment].workdir``."""
    network_mode: _NetworkModeName | None = None
    """Harbor's baseline for a verifier of its own: its network unless ``[verifier]`` names
    one."""
    allowed_hosts: tuple[str, ...] | None = None
    blocked_hosts: tuple[str, ...] | None = None
    allow_internet: bool | None = Field(default=None, strict=True)
    """Harbor's deprecated switch, read only without ``network_mode``."""

    @property
    def network(self) -> NetworkPolicy | None:
        """The baseline this table names, or ``None``."""
        return _table_network(
            self.network_mode, self.allowed_hosts, self.blocked_hosts, self.allow_internet
        )

    # --- validation ---

    @model_validator(mode="before")
    @classmethod
    def _docker_image(cls, data: Any) -> Any:
        return _harbor_image_key(data)

    @field_validator("allowed_hosts", "blocked_hosts")
    @classmethod
    def _normal_hosts(cls, hosts: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if hosts is None else tuple(_normal_host(host) for host in hosts)

    @model_validator(mode="after")
    def _hosts_only_for_their_mode(self) -> Self:
        _check_table_network(self.network_mode, self.allowed_hosts, self.blocked_hosts)
        return self


class _VerifierKeys(BaseModel):
    """What ``[verifier]`` and ``[verifier.validation]`` each say of their verifier."""

    model_config = _FROZEN

    timeout_sec: float = Field(default=600, gt=0, allow_inf_nan=False, strict=True)
    env: TaskEnv = Field(default_factory=dict)
    environment_mode: VerificationMode | None = None
    environment: VerifierEnvironmentTable | None = None
    user: (
        Annotated[str, Field(pattern=_ACCOUNT)] | Annotated[int, Field(ge=0, strict=True)] | None
    ) = None
    """Harbor's account for the verifier, by name or uid; unset, the framework's identity."""
    network_mode: _NetworkModeName | None = None
    """The verifier's own network; unset, Harbor's baseline applies
    (``TaskConfig.verifier_network``)."""
    allowed_hosts: tuple[str, ...] | None = None
    blocked_hosts: tuple[str, ...] | None = None

    @property
    def mode(self) -> VerificationMode:
        """Harbor's resolution: separate when a verifier environment is declared; a snapshot
        verifier is always named."""
        if self.environment_mode is not None:
            return self.environment_mode
        if self.environment is not None:
            return VerificationMode.SEPARATE
        return VerificationMode.SHARED

    @property
    def network(self) -> NetworkPolicy | None:
        """The policy this table names, or ``None``."""
        return _table_network(self.network_mode, self.allowed_hosts, self.blocked_hosts)

    # --- validation ---

    @field_validator("allowed_hosts", "blocked_hosts")
    @classmethod
    def _normal_hosts(cls, hosts: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if hosts is None else tuple(_normal_host(host) for host in hosts)

    @model_validator(mode="after")
    def _hosts_only_for_their_mode(self) -> Self:
        _check_table_network(self.network_mode, self.allowed_hosts, self.blocked_hosts)
        return self

    @model_validator(mode="after")
    def _environment_fits_the_mode(self) -> Self:
        if self.environment is None:
            return self
        if self.environment_mode is VerificationMode.SHARED:
            raise ValueError("environment_mode 'shared' cannot declare an environment")
        if self.environment_mode is VerificationMode.SNAPSHOT and (
            image_keys := sorted({"image", "build_timeout_sec"} & self.environment.model_fields_set)
        ):
            raise ValueError(
                "environment_mode 'snapshot' boots the verifier from the solver's snapshot, so "
                f"its environment cannot set {' or '.join(image_keys)}"
            )
        return self


class ValidationTable(_VerifierKeys):
    """``[verifier.validation]``, Orbit's: the verifier of validation rounds, wherever it
    differs from ``[verifier]``'s, with every key ``[verifier]`` has. ``TaskConfig`` resolves
    it over ``[verifier]`` (:meth:`VerifierTable.for_validation`)."""

    visibility: bool = Field(default=False, strict=True)
    """Whether the agent has a read-only copy of the stage that validates, ``validation/``,
    or else ``tests/``, as it is in the Task folder."""


#: The keys that name a verifier's network together: one names them all.
_NETWORK_KEYS = frozenset({"network_mode", "allowed_hosts", "blocked_hosts"})
_ENVIRONMENT_NETWORK_KEYS = _NETWORK_KEYS | {"allow_internet"}
_IMAGE_KEYS = frozenset({"image", "build_timeout_sec"})


class VerifierTable(_VerifierKeys):
    """``[verifier]``: the verifier's budget, network and where it runs, and in
    ``[verifier.validation]`` what differs for the verifier of validation rounds."""

    validation: ValidationTable | None = Field(default=None, exclude_if=lambda value: value is None)

    def for_validation(self) -> VerifierTable:
        """The verifier of validation rounds: each key ``[verifier.validation]`` sets, over
        ``[verifier]``'s. A table it sets merges into ``[verifier]``'s key by key, ``env``
        variable by variable; a network it names, its own or its environment's baseline,
        replaces every inherited one; and what it cannot use is not inherited: a shared one
        inherits no environment, a snapshot one no image, one without GPUs no GPU model or
        memory. An environment it declares over a shared verifier makes it separate, as
        Harbor resolves a verifier that declares one."""
        own = self.validation
        if own is None:
            return self.model_copy(update={"validation": None})
        keys = own.model_fields_set
        values: dict[str, Any] = {
            name: getattr(own if name in keys else self, name)
            for name in ("timeout_sec", "environment_mode", "user")
        }
        if (
            "environment_mode" not in keys
            and own.environment is not None
            and self.environment_mode is VerificationMode.SHARED
        ):
            values["environment_mode"] = None
        values["env"] = {**self.env, **own.env}
        named = own.environment is not None and bool(
            own.environment.model_fields_set & _ENVIRONMENT_NETWORK_KEYS
        )
        network = own if keys & _NETWORK_KEYS or named else self
        values.update({name: getattr(network, name) for name in _NETWORK_KEYS})
        mode = values["environment_mode"]
        inherited = None if mode is VerificationMode.SHARED else self.environment
        values["environment"] = _merged_environment(
            inherited, own.environment, snapshot=mode is VerificationMode.SNAPSHOT
        )
        return VerifierTable.model_validate(values)


def _merged_environment(
    inherited: VerifierEnvironmentTable | None,
    own: VerifierEnvironmentTable | None,
    *,
    snapshot: bool,
) -> VerifierEnvironmentTable | None:
    """``own`` over ``inherited``, key by key, without an inherited image for a snapshot
    verifier, an inherited network where ``own`` names one, or an inherited GPU model or
    memory where ``own`` asks for no GPUs."""
    if inherited is None:
        return own
    keys = set(own.model_fields_set) if own is not None else set()
    kept = set(inherited.model_fields_set) - keys
    if snapshot:
        kept -= _IMAGE_KEYS
    if keys & _ENVIRONMENT_NETWORK_KEYS:
        kept -= _ENVIRONMENT_NETWORK_KEYS
    if own is not None and "gpus" in keys and not own.gpus:
        kept -= {"gpu_types", "gpu_memory_mb"}
    values = {name: getattr(inherited, name) for name in kept}
    if own is not None:
        values.update({name: getattr(own, name) for name in keys})
        values["env"] = {**inherited.env, **own.env}
    return VerifierEnvironmentTable.model_validate(values)


class SolutionTable(BaseModel):
    """``[solution]``: what the oracle receives besides ``[environment].env``."""

    model_config = _FROZEN

    env: TaskEnv = Field(default_factory=dict)


class TaskConfig(BaseModel):
    """The strict authored shape of ``task.toml``.

    A Harbor field keeps Harbor's meaning. A field Orbit adds sits in the Harbor table of
    what it configures, under a name Harbor gives no other meaning, except
    ``[[calibration]]``, which no Harbor table configures. Every key has a default: a Task
    that leaves out ``schema_version`` or ``[task].name``, which Orbit's form writes, still
    loads, and the loader says so.
    """

    model_config = _FROZEN

    schema_version: str | None = None
    """``SCHEMA_VERSION``, one of Harbor's, or ``None`` when the Task names no format, as
    Harbor allows; ``harbor`` says which."""
    artifacts: tuple[Artifact, ...] = ()
    task: TaskTable = TaskTable()
    metadata: dict[str, Any] = Field(default_factory=dict)
    environment: EnvironmentTable = EnvironmentTable()
    agent: AgentTable = AgentTable()
    verifier: VerifierTable = VerifierTable()
    solution: SolutionTable = SolutionTable()
    calibration: tuple[CalibrationCase, ...] = ()
    """``[[calibration]]`` as written; ``calibration_cases`` adds the default ``solution``."""

    @property
    def harbor(self) -> bool:
        """Whether this is a Harbor Task: one naming one of Harbor's format versions, or none,
        which Harbor reads as its own. Harbor's defaults apply where it sets nothing."""
        version = self.schema_version
        return version is None or _HARBOR_SCHEMA.fullmatch(version) is not None

    @property
    def agent_user(self) -> str:
        """The agent's account: ``[agent].user``; unset, ``user``, or in a Harbor Linux Task
        ``root``, the account Harbor runs its agent as in an image naming no other ``USER``."""
        agent = self.agent
        linux = self.environment.os is OperatingSystem.LINUX
        if "user" in agent.model_fields_set or not (self.harbor and linux):
            return agent.user
        return HARBOR_ACCOUNT

    @property
    def agent_sudo(self) -> bool:
        """Whether the agent can elevate: ``[agent].sudo``; unset, whether its account is
        ``root``, which nothing can keep from elevating, outside Windows."""
        if "sudo" in self.agent.model_fields_set:
            return self.agent.sudo
        return self._root_agent

    @property
    def _root_agent(self) -> bool:
        """Whether the agent's account is root, which only Linux and macOS name so."""
        return self.agent_user == "root" and self.environment.os is not OperatingSystem.WINDOWS

    @property
    def agent_network(self) -> NetworkPolicy:
        """Harbor's resolution for the agent and the oracle: ``[agent]``'s own policy, else
        ``[environment]``'s baseline, else Orbit's default, no network."""
        return self.agent.network or self.environment.network or NetworkPolicy()

    @property
    def verifier_network(self) -> NetworkPolicy:
        """Harbor's resolution for ``tests/``: ``[verifier]``'s own policy; else, for a
        verifier with a Sandbox of its own and a ``[verifier.environment]``, that table's
        baseline, open when it names none, as Harbor's separate environment is; else
        ``[environment]``'s baseline; else open, Orbit's default for verification."""
        return self.network_of(self.verifier)

    def network_of(self, verifier: VerifierTable) -> NetworkPolicy:
        """The network of the verifier ``verifier`` describes, by Harbor's resolution: that
        of ``tests/``, or with :meth:`VerifierTable.for_validation`, of validation rounds."""
        if (own := verifier.network) is not None:
            return own
        open_ = NetworkPolicy(mode=NetworkMode.OPEN)
        if verifier.mode is not VerificationMode.SHARED and verifier.environment is not None:
            return verifier.environment.network or open_
        return self.environment.network or open_

    @property
    def calibration_cases(self) -> tuple[CalibrationCase, ...]:
        """``solution``, as declared or by default, then the other declared cases in the order
        they are written."""
        declared = {case.name: case for case in self.calibration}
        return (
            declared.get(SOLUTION_CASE, DEFAULT_SOLUTION),
            *(case for case in self.calibration if case.name != SOLUTION_CASE),
        )

    # --- validation ---

    @field_validator("schema_version")
    @classmethod
    def _readable_version(cls, value: str | None) -> str | None:
        if value is None or value == SCHEMA_VERSION or _HARBOR_SCHEMA.fullmatch(value):
            return value
        raise ValueError(
            f'"{value}" is neither Orbit\'s format "{SCHEMA_VERSION}" nor one of Harbor\'s, 1.<n>'
        )

    @model_validator(mode="before")
    @classmethod
    def _harbor_version_key(cls, data: Any) -> Any:
        """A root-level ``version`` is ``schema_version`` under the name Harbor first gave it:
        one field, written once."""
        if not isinstance(data, dict) or "version" not in data:
            return data
        if "schema_version" in data:
            raise ValueError("version and schema_version are one field; write only one of them")
        return {
            ("schema_version" if key == "version" else key): value for key, value in data.items()
        }

    @model_validator(mode="before")
    @classmethod
    def _harbor_keys_orbit_lacks(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        unsupported: list[str] = []
        for path, keys in _UNSUPPORTED_HARBOR_KEYS.items():
            table: Any = data
            for part in path:
                table = table.get(part) if isinstance(table, dict) else None
            if isinstance(table, dict):
                unsupported.extend(
                    f"{'.'.join((*path, key))} is not supported by Orbit"
                    for key in sorted(keys & table.keys())
                )
        artifacts = data.get("artifacts")
        for item in artifacts if isinstance(artifacts, list) else ():
            if isinstance(item, dict):
                unsupported.extend(
                    f"artifact {key} is not supported by Orbit"
                    for key in sorted({"service"} & item.keys())
                )
        if unsupported:
            raise ValueError("; ".join(unsupported))
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        environment, verifier = self.environment, self.verifier
        names = [server.name for server in environment.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("[[environment.mcp_servers]] names must be unique")
        cases = [case.name for case in self.calibration]
        if len(cases) != len(set(cases)):
            raise ValueError("[[calibration]] names must be unique")
        skills_dir = environment.skills_dir
        if skills_dir is not None and not _task_path(skills_dir, environment.os).is_absolute():
            raise ValueError("[environment].skills_dir must be an absolute path")
        for table, declared in (
            ("verifier", verifier),
            ("verifier.validation", verifier.validation),
        ):
            if declared is None or declared.user is None:
                continue
            if environment.os is OperatingSystem.WINDOWS:
                raise ValueError(
                    f"[{table}].user is for Linux and macOS; Windows verification runs as "
                    "Administrator"
                )
        rounds: VerifierTable | None = None
        if verifier.validation is not None:
            try:
                rounds = verifier.for_validation()
            except ValidationError as exc:
                reasons = "; ".join(
                    error["msg"].removeprefix("Value error, ") for error in exc.errors()
                )
                raise ValueError(
                    f"[verifier.validation] over what it inherits from [verifier]: {reasons}"
                ) from None
        for table, declared in (("verifier", verifier), ("verifier.validation", rounds)):
            if (
                declared is not None
                and declared.mode is VerificationMode.SNAPSHOT
                and declared.environment is not None
                and declared.environment.nodes > environment.nodes
            ):
                raise ValueError(
                    f"[{table}.environment].nodes: a snapshot verifier boots each of its nodes "
                    "from the agent's node of the same number, so it spans no more nodes than "
                    "[environment].nodes"
                )
        # The rounds' verifier as it resolves, whose GPU keys may each come from either table.
        for table, sized in (
            ("environment", environment),
            ("verifier.environment", verifier.environment),
            ("verifier.validation.environment", rounds and rounds.environment),
        ):
            if sized is None or sized.gpus:
                continue
            if sized.gpu_types is not None:
                raise ValueError(f"[{table}].gpu_types requires gpus")
            if sized.gpu_memory_mb is not None:
                raise ValueError(f"[{table}].gpu_memory_mb requires gpus")
        _validate_artifact_paths(self.artifacts, environment.os)
        if self._root_agent and not self.agent_sudo:
            raise ValueError(
                "the agent's account is root, which nothing can keep from elevating: remove "
                "[agent].sudo = false, or name a non-root [agent].user"
            )
        return self

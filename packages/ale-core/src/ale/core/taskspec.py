"""Strict public contracts for one self-contained ALE Task."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, NamedTuple, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.ids import TaskId, content_hash

__all__ = [
    "SCHEMA_VERSION",
    "AgentTable",
    "Artifact",
    "Author",
    "EnvironmentTable",
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
    "Resources",
    "SkillSource",
    "SolutionTable",
    "StdioMcpServer",
    "StreamableHttpMcpServer",
    "TaskConfig",
    "TaskEnv",
    "TaskSpec",
    "TaskTable",
    "TaskVariables",
    "ToolProvision",
    "VariableReference",
    "VerificationMode",
    "VerifierEnvironmentTable",
    "VerifierResources",
    "VerifierTable",
    "VerifySpec",
    "host_allowed",
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


class Artifact(BaseModel):
    """One ``artifacts`` entry: a path or glob, below where the agent starts when relative.

    A plain string when nothing is left out. ``exclude`` takes ``tar --exclude`` patterns, as
    in Harbor: they match after any ``/``, so ``.venv`` is that name at any depth.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    exclude: tuple[str, ...] = ()

    # --- validation ---

    @model_validator(mode="before")
    @classmethod
    def _plain_path(cls, data: Any) -> Any:
        return {"source": data} if isinstance(data, str) else data


def _validate_artifact_paths(
    artifacts: tuple[Artifact, ...], operating_system: OperatingSystem
) -> None:
    for artifact in artifacts:
        path = _task_path(artifact.source, operating_system)
        if ".." in path.parts or (path.is_absolute() and path == path.parent):
            raise ValueError("artifact paths must not be filesystem roots or contain '..'")


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

    Without ``ref`` or ``base_ref`` the image is built from the Task folder.
    """

    model_config = _FROZEN

    kind: ImageKind
    ref: str | None = Field(default=None, min_length=1)
    base_ref: str | None = Field(default=None, min_length=1)
    user: str = Field(default="user", pattern=_ACCOUNT)
    """The agent account. It must exist in the image; preparation fails if it does not."""
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
        return self


class Resources(BaseModel):
    model_config = _FROZEN

    cpus: int = Field(default=1, ge=1)
    memory_mb: int = Field(default=4096, ge=1)
    storage_mb: int | None = Field(default=None, ge=1)
    gpus: int = Field(default=0, ge=0)
    gpu_types: tuple[str, ...] = ()
    """GPU models the Task accepts, any one of them; empty accepts every model."""
    sudo: bool = False


class NetworkMode(StrEnum):
    BLOCK = "block"
    ALLOWLIST = "allowlist"
    OPEN = "open"


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    """Whether ``allowed_hosts`` admits ``host``.

    A name matches itself; ``*.example.org`` matches ``example.org`` and every name below it,
    at any depth, which every Provider can enforce. An address matches itself and any range
    holding it, and never a name pattern.
    """
    name = host.lower().rstrip(".")
    address = _address(name)
    for allowed in (entry.lower() for entry in allowed_hosts):
        if address is None:
            pattern = allowed.removeprefix("*.") if allowed.startswith("*.") else None
            if name in (allowed, pattern) or (pattern and name.endswith("." + pattern)):
                return True
        elif allowed == address.compressed or (
            "/" in allowed and address in ipaddress.ip_network(allowed)
        ):
            return True
    return False


class NetworkPolicy(BaseModel):
    model_config = _FROZEN

    mode: NetworkMode = NetworkMode.BLOCK
    allowed_hosts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_hosts(self) -> Self:
        if self.mode is NetworkMode.ALLOWLIST and not self.allowed_hosts:
            raise ValueError("allowlist mode requires at least one allowed host")
        if self.mode is not NetworkMode.ALLOWLIST and self.allowed_hosts:
            raise ValueError(f"allowed_hosts is meaningless in {self.mode} mode")
        return self


class PhaseTimeouts(BaseModel):
    model_config = _FROZEN

    agent: float = Field(default=900, gt=0)
    verify: float = Field(default=600, gt=0)


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


McpServer = Annotated[
    StdioMcpServer | StreamableHttpMcpServer,
    Field(discriminator="transport"),
]


class ToolProvision(BaseModel):
    """What a Task's image gives its agent, as ``[environment]`` declares it."""

    model_config = _FROZEN

    skills_dir: str | None = None
    mcp_servers: tuple[McpServer, ...] = ()


class VerificationMode(StrEnum):
    SHARED = "shared"
    SEPARATE = "separate"


class VerifierResources(BaseModel):
    """Explicit resources for a physically separate verifier sandbox."""

    model_config = _FROZEN

    cpus: int = Field(ge=1)
    memory_mb: int = Field(ge=1)
    storage_mb: int | None = Field(ge=1)
    gpus: int = Field(ge=0)
    gpu_types: tuple[str, ...] = ()

    def as_resources(self) -> Resources:
        return Resources(
            cpus=self.cpus,
            memory_mb=self.memory_mb,
            storage_mb=self.storage_mb,
            gpus=self.gpus,
            gpu_types=self.gpu_types,
            sudo=False,
        )


class VerifySpec(BaseModel):
    model_config = _FROZEN

    environment_mode: VerificationMode = VerificationMode.SHARED
    image: ImageSpec | None = None
    resources: VerifierResources | None = None

    @model_validator(mode="after")
    def _valid_topology(self) -> Self:
        if self.image is not None and self.image.base_ref is not None:
            raise ValueError("a verifier image is used directly or built, never from a base ref")
        if self.environment_mode is VerificationMode.SHARED:
            if self.image is not None or self.resources is not None:
                raise ValueError("shared verification cannot declare image or resources")
        elif self.resources is None:
            raise ValueError("separate verification requires explicit resources")
        return self


_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}", re.DOTALL)
_EMBEDDED_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
#: The prefix of the framework's own variables: those it sets in the commands it runs, and
#: the Host configuration it reads, model and Judge keys included.
_RESERVED_PREFIX = "ALE_"


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

#: A name that marks a credential, as Harbor recognizes one; ``MAX_TOKENS`` is a count.
_CREDENTIAL_NAME = re.compile(r"KEY|SECRET|TOKEN(?!S)|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)
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
    """``[verifier.environment].env``. ``None`` without that table: a separate verifier then
    receives ``environment``, as Harbor copies ``[environment]`` for it."""
    solution: dict[str, str] = Field(default_factory=dict)
    """``[solution].env``: the oracle only."""
    mcp_headers: dict[str, dict[str, str]] = Field(default_factory=dict)
    """``[[environment.mcp_servers]].headers`` by server: what the agent's MCP client sends."""

    @property
    def tables(self) -> tuple[tuple[str, dict[str, str]], ...]:
        """Each declared table by its ``task.toml`` name."""
        tables = [("environment", self.environment), ("verifier", self.verifier)]
        if self.verifier_environment is not None:
            tables.append(("verifier.environment", self.verifier_environment))
        tables.append(("solution", self.solution))
        return tuple(tables)

    def oracle(self) -> dict[str, str]:
        return {**self.environment, **self.solution}

    def verification(self, mode: VerificationMode) -> dict[str, str]:
        separate = mode is VerificationMode.SEPARATE and self.verifier_environment is not None
        return {**(self.verifier_environment if separate else self.environment), **self.verifier}


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
    artifacts: tuple[Artifact, ...] = ()
    tools: ToolProvision = ToolProvision()
    verify: VerifySpec = VerifySpec()
    variables: TaskVariables = TaskVariables()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    def start_directory(self, home: str) -> str:
        """Where the agent starts, given the agent's ``home``."""
        return str(_task_path(home, self.os) / (self.workdir or ""))

    def artifact_path(self, artifact: Artifact, home: str) -> str:
        """Where ``artifact`` is, given the agent's ``home``."""
        return str(_task_path(self.start_directory(home), self.os) / artifact.source)

    # --- validation ---

    @model_validator(mode="after")
    def _valid_artifacts(self) -> Self:
        _validate_artifact_paths(self.artifacts, self.os)
        return self


#: The ``task.toml`` schema ALE reads: revision 1 of ALE's fields on Harbor's schema 1.4. Plain
#: ``1.4`` is a Harbor file, which a conversion turns into this one.
SCHEMA_VERSION = "1.4+ale.1"

#: ``task.toml`` network modes and what they mean for the evaluated agent and the oracle.
_NETWORK_MODES = {
    "no-network": NetworkMode.BLOCK,
    "allowlist": NetworkMode.ALLOWLIST,
    "public": NetworkMode.OPEN,
}

_HARBOR_ENVIRONMENT_ONLY = frozenset(
    {
        "allow_internet",
        "allowed_hosts",
        "healthcheck",
        "memory",
        "network_mode",
        "storage",
        "tpu",
        "workdir",
    }
)

#: A ``gpu_types`` entry: a GPU model as Harbor names it, such as ``H100`` or ``RTX-PRO-6000``.
_GpuType = Annotated[str, Field(pattern=r"[A-Za-z0-9]")]

#: Harbor ``task.toml`` keys that ALE does not implement, by table. They fail with a
#: message saying so rather than as unknown keys, because a Harbor author expects them.
_UNSUPPORTED_HARBOR_KEYS: dict[tuple[str, ...], frozenset[str]] = {
    (): frozenset({"multi_step_reward_strategy", "source", "steps"}),
    ("environment",): _HARBOR_ENVIRONMENT_ONLY,
    ("verifier",): frozenset({"allowed_hosts", "collect", "network_mode", "user"}),
    ("verifier", "environment"): _HARBOR_ENVIRONMENT_ONLY | {"mcp_servers", "os", "skills_dir"},
}


class Author(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    email: str | None = None


class TaskTable(BaseModel):
    """``[task]``: the Task's identity; everything else here is descriptive."""

    model_config = _FROZEN

    name: TaskId
    description: str = ""
    version: str | None = Field(default=None, min_length=1)
    authors: tuple[Author, ...] = ()
    keywords: tuple[str, ...] = ()


class McpServerTable(BaseModel):
    """``[[environment.mcp_servers]]``: Harbor's entry for a server the image provides."""

    model_config = _FROZEN

    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    transport: Literal["stdio", "streamable-http"]
    command: str | None = Field(default=None, min_length=1)
    args: tuple[str, ...] = ()
    url: str | None = Field(default=None, pattern=r"^https?://")
    headers: McpHeaders = Field(default_factory=dict)

    @property
    def server(self) -> StdioMcpServer | StreamableHttpMcpServer:
        if self.transport == "stdio":
            assert self.command is not None
            return StdioMcpServer(
                name=self.name, transport="stdio", command=self.command, args=self.args
            )
        assert self.url is not None
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
        if self.transport == "streamable-http" and (
            self.url is None or self.command is not None or self.args
        ):
            raise ValueError("transport 'streamable-http' requires url and takes no command")
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
    """An ``allowed_hosts`` entry in Harbor's normal form."""
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


class EnvironmentTable(BaseModel):
    """``[environment]``: the machine the agent starts on."""

    model_config = _FROZEN

    os: OperatingSystem = OperatingSystem.LINUX
    vm: bool = Field(default=False, strict=True)
    docker_image: str | None = Field(default=None, min_length=1)
    vm_image: str | None = Field(default=None, min_length=1)
    vm_base: str | None = Field(default=None, min_length=1)
    build_timeout_sec: float = Field(default=3600, gt=0, allow_inf_nan=False, strict=True)
    cpus: int = Field(default=1, ge=1, strict=True)
    memory_mb: int = Field(default=4096, ge=1, strict=True)
    storage_mb: int | None = Field(default=None, ge=1, strict=True)
    gpus: int = Field(default=0, ge=0, strict=True)
    gpu_types: tuple[_GpuType, ...] | None = Field(default=None, min_length=1)
    """GPU models any of which will do; ``None`` accepts every model, as in Harbor."""
    env: TaskEnv = Field(default_factory=dict)
    skills_dir: str | None = Field(default=None, min_length=1)
    mcp_servers: tuple[McpServerTable, ...] = ()

    @property
    def kind(self) -> ImageKind:
        """A VM when anything here says so; only a Linux VM built from a Dockerfile sets ``vm``."""
        linux = self.os is OperatingSystem.LINUX
        vm = self.vm or not linux or self.vm_image is not None or self.vm_base is not None
        return ImageKind.VM if vm else ImageKind.CONTAINER

    # --- validation ---

    @field_validator("os", mode="before")
    @classmethod
    def _any_case(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value


class AgentTable(BaseModel):
    """``[agent]``: the evaluated agent's account, budget and network; the oracle's too."""

    model_config = _FROZEN

    user: str = Field(default="user", pattern=_ACCOUNT)
    timeout_sec: float = Field(default=900, gt=0, allow_inf_nan=False, strict=True)
    network_mode: Literal["no-network", "allowlist", "public"] = "no-network"
    allowed_hosts: tuple[str, ...] | None = None
    sudo: bool = Field(default=False, strict=True)
    workdir: str | None = Field(default=None, min_length=1)

    @property
    def network(self) -> NetworkPolicy:
        return NetworkPolicy(
            mode=_NETWORK_MODES[self.network_mode], allowed_hosts=self.allowed_hosts or ()
        )

    # --- validation ---

    @field_validator("allowed_hosts")
    @classmethod
    def _normal_hosts(cls, hosts: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if hosts is None else tuple(_normal_host(host) for host in hosts)

    @model_validator(mode="after")
    def _hosts_only_for_allowlist(self) -> Self:
        if self.network_mode == "allowlist" and not self.allowed_hosts:
            raise ValueError("network_mode 'allowlist' requires a non-empty allowed_hosts")
        if self.network_mode != "allowlist" and self.allowed_hosts is not None:
            raise ValueError("allowed_hosts requires network_mode 'allowlist'")
        return self


class VerifierEnvironmentTable(BaseModel):
    """``[verifier.environment]``: a separate verifier's own image and resources.

    ``cpus`` and ``memory_mb`` fall back to ``[environment]``'s.
    """

    model_config = _FROZEN

    vm: bool = Field(default=False, strict=True)
    docker_image: str | None = Field(default=None, min_length=1)
    vm_image: str | None = Field(default=None, min_length=1)
    build_timeout_sec: float = Field(default=3600, gt=0, allow_inf_nan=False, strict=True)
    cpus: int | None = Field(default=None, ge=1, strict=True)
    memory_mb: int | None = Field(default=None, ge=1, strict=True)
    storage_mb: int | None = Field(default=None, ge=1, strict=True)
    gpus: int = Field(default=0, ge=0, strict=True)
    gpu_types: tuple[_GpuType, ...] | None = Field(default=None, min_length=1)
    env: TaskEnv = Field(default_factory=dict)

    @property
    def kind(self) -> ImageKind:
        return ImageKind.VM if self.vm or self.vm_image is not None else ImageKind.CONTAINER


class VerifierTable(BaseModel):
    """``[verifier]``: the verifier's budget and where it runs."""

    model_config = _FROZEN

    timeout_sec: float = Field(default=600, gt=0, allow_inf_nan=False, strict=True)
    env: TaskEnv = Field(default_factory=dict)
    environment_mode: VerificationMode | None = None
    environment: VerifierEnvironmentTable | None = None

    @property
    def mode(self) -> VerificationMode:
        """Harbor's resolution: separate when a verifier environment is declared."""
        if self.environment_mode is not None:
            return self.environment_mode
        if self.environment is not None:
            return VerificationMode.SEPARATE
        return VerificationMode.SHARED

    # --- validation ---

    @model_validator(mode="after")
    def _shared_has_no_environment(self) -> Self:
        if self.environment_mode is VerificationMode.SHARED and self.environment is not None:
            raise ValueError("environment_mode 'shared' cannot declare [verifier.environment]")
        return self


class SolutionTable(BaseModel):
    """``[solution]``: what the oracle receives besides ``[environment].env``."""

    model_config = _FROZEN

    env: TaskEnv = Field(default_factory=dict)


class TaskConfig(BaseModel):
    """The strict authored shape of ``task.toml``.

    A Harbor field keeps Harbor's meaning. A field ALE adds sits in the Harbor table of what
    it configures, under a name Harbor gives no other meaning. Only ``schema_version`` and
    ``[task].name`` are required.
    """

    model_config = _FROZEN

    schema_version: Literal["1.4+ale.1"]
    artifacts: tuple[Artifact, ...] = ()
    task: TaskTable
    metadata: dict[str, Any] = Field(default_factory=dict)
    environment: EnvironmentTable = EnvironmentTable()
    agent: AgentTable = AgentTable()
    verifier: VerifierTable = VerifierTable()
    solution: SolutionTable = SolutionTable()

    # --- validation ---

    @model_validator(mode="before")
    @classmethod
    def _harbor_keys_ale_lacks(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        unsupported: list[str] = []
        for path, keys in _UNSUPPORTED_HARBOR_KEYS.items():
            table: Any = data
            for part in path:
                table = table.get(part) if isinstance(table, dict) else None
            if isinstance(table, dict):
                unsupported.extend(
                    f"{'.'.join((*path, key))} is not supported by ALE"
                    for key in sorted(keys & table.keys())
                )
        artifacts = data.get("artifacts")
        for item in artifacts if isinstance(artifacts, list) else ():
            if isinstance(item, dict):
                unsupported.extend(
                    f"artifact {key} is not supported by ALE"
                    for key in sorted({"destination", "service"} & item.keys())
                )
        if unsupported:
            raise ValueError("; ".join(unsupported))
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        environment, verifier = self.environment, self.verifier
        linux = environment.os is OperatingSystem.LINUX
        if environment.docker_image is not None and environment.kind is ImageKind.VM:
            raise ValueError("[environment].docker_image is a container image; a VM has vm_image")
        if environment.vm_image is not None and environment.vm_base is not None:
            raise ValueError("vm_image and vm_base are mutually exclusive")
        if environment.vm_base is not None and linux:
            raise ValueError("[environment].vm_base requires os 'windows' or 'macos'")
        names = [server.name for server in environment.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("[[environment.mcp_servers]] names must be unique")
        skills_dir = environment.skills_dir
        if skills_dir is not None and not _task_path(skills_dir, environment.os).is_absolute():
            raise ValueError("[environment].skills_dir must be an absolute path")
        for table, sized in (
            ("environment", environment),
            ("verifier.environment", verifier.environment),
        ):
            if sized is not None and sized.gpu_types is not None and not sized.gpus:
                raise ValueError(f"[{table}].gpu_types requires gpus")
        if verifier.environment is not None and verifier.environment.docker_image is not None:
            if verifier.environment.kind is ImageKind.VM:
                raise ValueError(
                    "[verifier.environment].docker_image is a container image; a VM has vm_image"
                )
            if environment.os is not OperatingSystem.LINUX:
                raise ValueError("only a Linux verifier runs in a container")
        _validate_artifact_paths(self.artifacts, environment.os)
        return self

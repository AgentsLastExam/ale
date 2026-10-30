"""Strict public contracts for one self-contained ALE Task."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, NamedTuple, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.ids import TaskId, content_hash

__all__ = [
    "SCHEMA_VERSION",
    "AgentTable",
    "Author",
    "EnvironmentTable",
    "ImageKind",
    "ImageSpec",
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
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ImageKind(StrEnum):
    CONTAINER = "container"
    VM = "vm"


class OperatingSystem(StrEnum):
    LINUX = "linux"
    WINDOWS = "windows"


def _task_path(path: str, operating_system: OperatingSystem) -> PurePosixPath | PureWindowsPath:
    path_type = PureWindowsPath if operating_system is OperatingSystem.WINDOWS else PurePosixPath
    return path_type(path)


def _validate_artifact_paths(paths: tuple[str, ...], operating_system: OperatingSystem) -> None:
    normalized = [_task_path(path, operating_system) for path in paths]
    relative = [
        path for path, parsed in zip(paths, normalized, strict=True) if not parsed.is_absolute()
    ]
    if relative:
        raise ValueError("artifact paths must be absolute: " + ", ".join(relative))
    if len(normalized) != len(set(normalized)):
        raise ValueError("artifact paths must be unique")
    for path in normalized:
        if ".." in path.parts or path == path.parent:
            raise ValueError("artifact paths must not be filesystem roots or contain '..'")


_ACCOUNT = r"^[a-z_][a-z0-9_-]{0,31}$"
_DNS_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ALLOWED_HOST = re.compile(rf"(?:\*\.)?{_DNS_LABEL}(?:\.{_DNS_LABEL})*")


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
    memory_mb: int = Field(default=4096, ge=128)
    storage_mb: int | None = Field(default=None, ge=256)
    gpus: int = Field(default=0, ge=0)
    sudo: bool = False


class NetworkMode(StrEnum):
    BLOCK = "block"
    ALLOWLIST = "allowlist"
    OPEN = "open"


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    """Whether ``allowed_hosts`` admits ``host``, with Harbor's matching.

    A name matches itself; ``*.example.org`` matches every name below ``example.org``, at any
    depth, and not ``example.org`` itself.
    """
    name = host.lower().rstrip(".")
    for allowed in (entry.lower() for entry in allowed_hosts):
        if name == allowed or (allowed.startswith("*.") and name.endswith(allowed[1:])):
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
    verify: float = Field(default=300, gt=0)


class SkillSource(BaseModel):
    model_config = _FROZEN

    path: str = Field(min_length=1)
    origin: Literal["preset", "run", "cli"] | None = Field(default=None, exclude=True)
    declared: str | None = Field(default=None, exclude=True)


class McpSource(BaseModel):
    model_config = _FROZEN

    path: str | None = Field(default=None, min_length=1)
    builtin: str | None = Field(default=None, min_length=1)
    origin: Literal["preset", "run", "cli"] | None = Field(default=None, exclude=True)
    declared: str | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.path is None) == (self.builtin is None):
            raise ValueError("an MCP source requires exactly one of path or builtin")
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
    memory_mb: int = Field(ge=128)
    storage_mb: int | None = Field(ge=256)
    gpus: int = Field(ge=0)

    def as_resources(self) -> Resources:
        return Resources(
            cpus=self.cpus,
            memory_mb=self.memory_mb,
            storage_mb=self.storage_mb,
            gpus=self.gpus,
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
    """``[agent].workdir``: where the evaluated agent starts; ``None`` for its home."""
    timeouts: PhaseTimeouts = PhaseTimeouts()
    artifacts: tuple[str, ...] = ()
    tools: ToolProvision = ToolProvision()
    verify: VerifySpec = VerifySpec()
    variables: TaskVariables = TaskVariables()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))

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
        "build_timeout_sec",
        "gpu_types",
        "healthcheck",
        "memory",
        "network_mode",
        "storage",
        "tpu",
        "workdir",
    }
)

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

    @model_validator(mode="before")
    @classmethod
    def _harbor_keys_ale_lacks(cls, data: Any) -> Any:
        if isinstance(data, dict) and "headers" in data:
            raise ValueError("headers is not supported by ALE")
        return data

    @model_validator(mode="after")
    def _fields_of_its_transport(self) -> Self:
        if self.transport == "stdio" and (self.command is None or self.url is not None):
            raise ValueError("transport 'stdio' requires command and takes no url")
        if self.transport == "streamable-http" and (
            self.url is None or self.command is not None or self.args
        ):
            raise ValueError("transport 'streamable-http' requires url and takes no command")
        return self


class EnvironmentTable(BaseModel):
    """``[environment]``: the machine the agent starts on."""

    model_config = _FROZEN

    os: OperatingSystem = OperatingSystem.LINUX
    vm: bool = Field(default=False, strict=True)
    docker_image: str | None = Field(default=None, min_length=1)
    vm_image: str | None = Field(default=None, min_length=1)
    vm_base: str | None = Field(default=None, min_length=1)
    cpus: int = Field(ge=1, strict=True)
    memory_mb: int = Field(ge=128, strict=True)
    storage_mb: int | None = Field(default=None, ge=256, strict=True)
    gpus: int = Field(default=0, ge=0, strict=True)
    env: TaskEnv = Field(default_factory=dict)
    skills_dir: str | None = Field(default=None, min_length=1)
    mcp_servers: tuple[McpServerTable, ...] = ()

    @property
    def kind(self) -> ImageKind:
        """A VM when anything here says so; only a Linux VM built from a Dockerfile sets ``vm``."""
        windows = self.os is OperatingSystem.WINDOWS
        vm = self.vm or windows or self.vm_image is not None or self.vm_base is not None
        return ImageKind.VM if vm else ImageKind.CONTAINER


class AgentTable(BaseModel):
    """``[agent]``: the evaluated agent's account, budget and network; the oracle's too."""

    model_config = _FROZEN

    user: str = Field(pattern=_ACCOUNT)
    timeout_sec: float = Field(gt=0, allow_inf_nan=False, strict=True)
    network_mode: Literal["no-network", "allowlist", "public"]
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
    def _hostnames_only(cls, hosts: tuple[str, ...] | None) -> tuple[str, ...] | None:
        for host in hosts or ():
            try:
                ipaddress.ip_network(host, strict=False)
            except ValueError:
                pass
            else:
                raise ValueError(f"{host}: addresses and CIDR ranges are not supported by ALE")
            if not _ALLOWED_HOST.fullmatch(host):
                raise ValueError(f"{host!r} is neither a hostname nor *.hostname")
        return hosts

    @model_validator(mode="after")
    def _hosts_only_for_allowlist(self) -> Self:
        if self.network_mode == "allowlist" and not self.allowed_hosts:
            raise ValueError("network_mode 'allowlist' requires a non-empty allowed_hosts")
        if self.network_mode != "allowlist" and self.allowed_hosts is not None:
            raise ValueError("allowed_hosts requires network_mode 'allowlist'")
        return self


class VerifierEnvironmentTable(BaseModel):
    """``[verifier.environment]``: a separate verifier's own image and resources."""

    model_config = _FROZEN

    vm: bool = Field(default=False, strict=True)
    docker_image: str | None = Field(default=None, min_length=1)
    vm_image: str | None = Field(default=None, min_length=1)
    cpus: int = Field(ge=1, strict=True)
    memory_mb: int = Field(ge=128, strict=True)
    storage_mb: int | None = Field(default=None, ge=256, strict=True)
    gpus: int = Field(default=0, ge=0, strict=True)
    env: TaskEnv = Field(default_factory=dict)

    @property
    def kind(self) -> ImageKind:
        return ImageKind.VM if self.vm or self.vm_image is not None else ImageKind.CONTAINER


class VerifierTable(BaseModel):
    """``[verifier]``: the verifier's budget and where it runs."""

    model_config = _FROZEN

    timeout_sec: float = Field(gt=0, allow_inf_nan=False, strict=True)
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
    it configures, under a name Harbor gives no other meaning. Every Harbor field whose
    default differs from ALE's is required.
    """

    model_config = _FROZEN

    schema_version: Literal["1.4+ale.1"]
    artifacts: tuple[str, ...] = ()
    task: TaskTable
    metadata: dict[str, Any] = Field(default_factory=dict)
    environment: EnvironmentTable
    agent: AgentTable
    verifier: VerifierTable
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
        if isinstance(artifacts, list) and any(isinstance(item, dict) for item in artifacts):
            unsupported.append("table-form artifacts are not supported by ALE")
        if unsupported:
            raise ValueError("; ".join(unsupported))
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        environment, verifier = self.environment, self.verifier
        windows = environment.os is OperatingSystem.WINDOWS
        if environment.docker_image is not None and environment.kind is ImageKind.VM:
            raise ValueError("[environment].docker_image is a container image; a VM has vm_image")
        if environment.vm_image is not None and environment.vm_base is not None:
            raise ValueError("vm_image and vm_base are mutually exclusive")
        if environment.vm_base is not None and not windows:
            raise ValueError("[environment].vm_base requires os 'windows'")
        names = [server.name for server in environment.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("[[environment.mcp_servers]] names must be unique")
        for key, path in (
            ("[environment].skills_dir", environment.skills_dir),
            ("[agent].workdir", self.agent.workdir),
        ):
            if path is not None and not _task_path(path, environment.os).is_absolute():
                raise ValueError(f"{key} must be an absolute path")
        if verifier.environment is not None and verifier.environment.docker_image is not None:
            if verifier.environment.kind is ImageKind.VM:
                raise ValueError(
                    "[verifier.environment].docker_image is a container image; a VM has vm_image"
                )
            if windows:
                raise ValueError("a Windows verifier runs in a VM")
        _validate_artifact_paths(self.artifacts, environment.os)
        return self

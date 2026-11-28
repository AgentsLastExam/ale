"""Layered configuration.

Files and flags must be interchangeable: anything settable in a config file is settable
on the command line and the other way round. Four layers, highest wins:

    command line  >  run config file  >  agent preset file  >  model defaults

Two rules keep this honest. Unknown keys are errors, never silent no-ops — a typo in a
token ceiling must not read as "no ceiling". And the merged result is hashed into the
provenance record, so a run always carries the settings it actually used.

Override syntax is ``--set a.b.c=value`` with TOML scalar parsing, so
``--set timeouts.agent=1800.5`` is a float and ``--set agent.stream=true``
is a boolean.
"""

from __future__ import annotations

import os
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from orbit.core.capability import Arch, GpuModel
from orbit.core.errors import ConfigError
from orbit.core.ids import content_hash
from orbit.core.sandbox import DesktopResolution
from orbit.core.taskspec import McpSource, SkillSource, TaskEnv, TaskSpec, env_problem

__all__ = [
    "CLUSTER_PROVIDERS",
    "GPU_MEMORY_MARGIN_MB",
    "AgentConfig",
    "AgentJudgeConfig",
    "ArtifactPolicy",
    "AssetCollectionConfig",
    "ClusterPartition",
    "ClusterProviderConfig",
    "DaytonaProviderConfig",
    "DockerProviderConfig",
    "FleetConfig",
    "GatewayLimits",
    "KubernetesProviderConfig",
    "KubernetesQueue",
    "LLMJudgeConfig",
    "LSFProviderConfig",
    "LSFQueue",
    "LoggingPolicy",
    "ModelProvider",
    "PBSProviderConfig",
    "PBSQueue",
    "ProviderId",
    "RunConfig",
    "RunTimeouts",
    "SandboxConfig",
    "SandboxRetentionConfig",
    "SlurmPartition",
    "SlurmProviderConfig",
    "VerificationConfig",
    "load_run_config",
    "merge_layers",
    "orbit_repo_path",
    "parse_override",
    "resolve_asset_collection",
    "select_agent_name",
]


class AssetCollectionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collection_slug: str = Field(min_length=1)
    source: Literal["cli", "environment"]


def orbit_repo_path() -> Path:
    raw = os.environ.get("ORBIT_REPO_PATH", "")
    if not raw:
        raise ConfigError("ORBIT_REPO_PATH is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError("ORBIT_REPO_PATH must be absolute")
    resolved = path.resolve()
    if (
        not (resolved / "pyproject.toml").is_file()
        or not (resolved / "packages" / "orbit-run").is_dir()
    ):
        raise ConfigError(f"ORBIT_REPO_PATH is not an Orbit engine checkout: {resolved}")
    return resolved


def resolve_asset_collection(cli_value: str | None = None) -> AssetCollectionConfig:
    if cli_value and cli_value.strip():
        return AssetCollectionConfig(collection_slug=cli_value.strip(), source="cli")
    environment = os.environ.get("ORBIT_ASSETS_COLLECTION", "").strip()
    if environment:
        return AssetCollectionConfig(
            collection_slug=environment,
            source="environment",
        )
    raise ConfigError(
        "asset collection is required; pass --collection or set ORBIT_ASSETS_COLLECTION"
    )


class GatewayLimits(BaseModel):
    """Ceilings enforced by refusing the next model call.

    ``unlimited`` is explicit. Gateway limits never inherit native program defaults.
    """

    model_config = ConfigDict(extra="forbid")

    max_model_calls: Annotated[int, Field(gt=0, strict=True)] | Literal["unlimited"] = "unlimited"
    max_input_tokens: Annotated[int, Field(gt=0, strict=True)] | Literal["unlimited"] = "unlimited"
    max_output_tokens: Annotated[int, Field(gt=0, strict=True)] | Literal["unlimited"] = "unlimited"
    max_total_tokens: Annotated[int, Field(gt=0, strict=True)] | Literal["unlimited"] = "unlimited"


class ArtifactPolicy(BaseModel):
    """What happens to the paths a task declared as its output.

    Declaring an output and keeping a copy of it are separate decisions: the task knows
    which paths hold its result, and the operator knows whether this particular run
    wants them on disk. A large sweep that only needs scores discards them; a debugging
    run keeps everything. Remote destinations belong here too when they arrive.
    """

    model_config = ConfigDict(extra="forbid")

    collect: Literal["host", "none"] = "none"


class LoggingPolicy(BaseModel):
    """Evidence collection and retention; canonical records are always written."""

    model_config = ConfigDict(extra="forbid")

    native_logs: Literal["minimal", "debug"] = "minimal"
    transport_payloads: Literal["digests", "debug"] = "digests"
    token_data: Literal["none", "exact"] = "none"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "claude-code"
    model: str = "claude-sonnet-5"
    authentication: Literal["auto", "api-key", "subscription"] = "auto"
    version: str | None = Field(
        default=None, description="Pin the agent build; None uses the harness default version"
    )
    max_steps: int = Field(
        default=100,
        gt=0,
        description=(
            "Observe-act steps a stepwise agent may take in one episode. Here rather than "
            "with the gateway's ceilings because the gateway does not enforce it — the "
            "stepwise environment does, which is what makes it bind an agent that has "
            "never heard of it."
        ),
    )
    settings: dict[str, Any] = Field(
        default_factory=dict, description="Harness-specific settings, validated by the harness"
    )
    skills: tuple[SkillSource, ...] = ()
    mcp_servers: tuple[McpSource, ...] = ()
    instruction_prefix: str = Field(
        default="",
        description=(
            "Text above the Task's instruction in the agent's first prompt. The framework "
            "composes that prompt, so it reaches every Harness alike"
        ),
    )
    instruction_suffix: str = Field(
        default="",
        description=(
            "Text below the Task's instruction in the agent's first prompt, above what the "
            "framework states there"
        ),
    )
    env: TaskEnv = Field(
        default_factory=dict,
        description=(
            "Variables every command of the agent's turns receives, under a Task's env rules; "
            "setup, the verifier and the oracle never do"
        ),
    )

    # --- validation ---

    @field_validator("env")
    @classmethod
    def _as_lint_reads_a_task_env(cls, env: dict[str, str]) -> dict[str, str]:
        # No ``orbit lint`` reads a run configuration, so its rules hold as it loads.
        for key, value in env.items():
            if problem := env_problem(key, value):
                raise ValueError(f"[agent.env] {problem}")
        return env


#: The model vendor whose API the Gateway or a Judge calls. Not a sandbox Provider.
ModelProvider = Literal["anthropic", "gemini", "meta", "moonshot", "openai", "qwen", "xai", "zai"]
#: A protocol the Gateway serves to the Sandbox and speaks to the provider.
Dialect = Literal["anthropic", "gemini", "openai-chat-completions", "openai-responses"]
#: The protocols an LLM Judge client speaks; it has no Gemini client.
JudgeDialect = Literal["anthropic", "openai-chat-completions", "openai-responses"]


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider = Field(
        default="anthropic",
        description=(
            "Whose API the Gateway calls. Its key is ORBIT_<PROVIDER>_API_KEY and its "
            "endpoint ORBIT_<PROVIDER>_BASE_URL, or the provider's own API when that is unset."
        ),
    )
    dialect: Dialect = "anthropic"
    limits: GatewayLimits = GatewayLimits()
    base_url: str = Field(
        default="",
        description="One-off endpoint override. Empty selects the provider's endpoint.",
    )
    api_key_env: str = Field(
        default="",
        description=(
            "One-off override naming the variable that holds the upstream key; required "
            "when base_url is not the provider's endpoint. Named rather than passed: a "
            "value on a command line is in shell history and in every process listing, a "
            "variable name is not."
        ),
    )


class _JudgeConfig(BaseModel):
    """Fields every Judge names; ``provider`` selects its key and endpoint as for the Gateway."""

    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)

    # --- validation ---

    @field_validator("model", "reasoning_effort")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class LLMJudgeConfig(_JudgeConfig):
    dialect: JudgeDialect = Field(description="The protocol the Judge speaks to its provider.")


class AgentJudgeConfig(_JudgeConfig):
    adapter: Literal["codex-cli", "claude-code"]
    version: str = Field(min_length=1)

    # --- validation ---

    @field_validator("version")
    @classmethod
    def _exact_version(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or any(not part.isdigit() for part in parts):
            raise ValueError("version must be an exact numeric semver such as 1.2.3")
        return value


class VerificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMJudgeConfig | None = None
    agent: AgentJudgeConfig | None = None


class SandboxRetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solver: Literal["destroy", "keep", "snapshot"] = "destroy"
    verifier: Literal["destroy", "keep"] = "destroy"


#: The batch clusters' Providers: each of a Run's episodes runs in a job of the cluster, which
#: holds every Sandbox of the episode.
CLUSTER_PROVIDERS = ("slurm", "pbs", "lsf", "kubernetes")

#: Every Provider a Run can select. A leaf starts the final Sandbox directly. A scheduler
#: places the request on machines it manages.
ProviderId = Literal[
    "docker:runc",
    "docker:kata",
    "docker:gvisor",
    "slurm",
    "pbs",
    "lsf",
    "kubernetes",
    "qemu",
    "firecracker",
    "orchard",
    "fleet",
    "daytona",
]


#: The device memory, in MiB, a GPU must have free beyond a shared Sandbox's need by default.
GPU_MEMORY_MARGIN_MB = 4096


class DockerProviderConfig(BaseModel):
    """Settings for every Docker leaf."""

    model_config = ConfigDict(extra="forbid")

    gpus: tuple[Annotated[int, Field(ge=0, strict=True)], ...] = ()
    """The Host's GPU indices Docker may lease; empty leases any of them."""
    gpu_memory_margin_mb: int = Field(default=GPU_MEMORY_MARGIN_MB, ge=0, strict=True)
    """The device memory, in MiB, a GPU must have free beyond a shared Sandbox's
    ``gpu_memory_mb`` to admit it: other tenants' jobs grow after they start."""

    # --- validation ---

    @field_validator("gpus")
    @classmethod
    def _gpu_indices(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(values)) != len(values):
            raise ValueError("Docker GPU indices must be unique")
        return values


_NAME = r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$"


class ClusterPartition(BaseModel):
    """What one job of a partition, or queue, of a cluster may hold: up to ``max_nodes``
    alike nodes, each limited as below."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_NAME)
    max_cpus: int = Field(ge=1)
    max_memory_mb: int = Field(ge=1)
    gpu_model: GpuModel | None = None
    max_gpus: int = Field(default=0, ge=0)
    max_nodes: int = Field(default=1, ge=1)
    """How many of its nodes one job, and one Sandbox, may span."""

    # --- validation ---

    @model_validator(mode="after")
    def _gpu_capacity(self) -> Self:
        if bool(self.max_gpus) != bool(self.gpu_model):
            raise ValueError("a GPU partition needs both gpu_model and max_gpus")
        return self


class SlurmPartition(ClusterPartition):
    qos: str | None = Field(default=None, pattern=_NAME)
    constraint: str | None = Field(default=None, min_length=1)
    """The node features its jobs ask for (``sbatch --constraint``)."""


class PBSQueue(ClusterPartition):
    """A PBS queue, whose jobs hold one node; its select chunk names ``chunk_resources``, and
    a job holds the queue's whole of each resource it leaves out."""

    max_nodes: Literal[1] = 1
    min_gpus: int = Field(default=0, ge=0)
    """Whole GPUs the queue charges each job, a CPU-only one's included."""
    chunk_resources: tuple[Literal["ncpus", "mem", "ngpus"], ...] = ("ncpus", "mem", "ngpus")

    # --- validation ---

    @model_validator(mode="after")
    def _queue(self) -> Self:
        if self.min_gpus > self.max_gpus:
            raise ValueError("min_gpus must fit the GPU capacity of the queue")
        if len(set(self.chunk_resources)) != len(self.chunk_resources):
            raise ValueError("chunk_resources must name each PBS resource once")
        return self


class LSFQueue(ClusterPartition):
    """An LSF queue, whose jobs hold one node."""

    max_nodes: Literal[1] = 1
    min_gpus: int = Field(default=0, ge=0)
    """Whole GPUs the queue charges each job, a CPU-only one's included."""

    # --- validation ---

    @model_validator(mode="after")
    def _queue(self) -> Self:
        if self.min_gpus > self.max_gpus:
            raise ValueError("min_gpus must fit the GPU capacity of the queue")
        return self


class ClusterProviderConfig(BaseModel):
    """A cluster whose workload manager runs each episode in a batch job of its own,
    submitted from a login node that shares ``shared_fs_dir`` with every compute node."""

    model_config = ConfigDict(extra="forbid")

    #: The Provider id, which names the cluster's workload manager.
    workload_manager: ClassVar[str]

    partitions: tuple[ClusterPartition, ...] = ()
    """In preference order. One Sandbox fits on up to ``max_nodes`` nodes of one partition."""
    account: str | None = Field(default=None, min_length=1)
    """The account jobs are charged to, where the cluster asks for one."""
    shared_fs_dir: str = Field(min_length=1)
    """Orbit's directory on the cluster's shared file system, at the same path on the login
    node and every compute node: the images prepared for the cluster, the engine, each job's
    files, snapshots written out, and the shared directories of Sandboxes of several nodes,
    so one on a large shared file system rather than a home with a small quota."""
    node_local_dir: str | None = Field(default=None, min_length=1)
    """Where on each compute node's own disk a job keeps its Podman store, which holds its
    images and what its Sandboxes write, expanded on the node: by default the job's own
    temporary directory, else ``/tmp``."""
    walltime_minutes: int = Field(default=180, ge=1)
    """Each job's wall time, within its queue's limit."""
    drain_minutes: int = Field(default=10, ge=1)
    """What saving a solver and writing it out for the episode's next job takes at most,
    which each job keeps before its wall time ends."""
    queue_timeout_sec: int = Field(default=900, ge=1)
    """How long an episode's first job, or an image's preparation, waits in the queue; a
    later job of the episode waits as long as the queue keeps it, as that spends none of the
    agent's time."""
    architecture: Arch = Arch.X86_64
    """The compute nodes' architecture, which the engine and every image are prepared for."""
    build_queue: str | None = None
    """The partition images are prepared in: by default the first without GPUs."""
    build_gpus: int = Field(default=0, ge=0)
    """The GPUs an image's preparation asks for, where its partition charges them."""
    podman: str | None = None
    """Podman's absolute path on the compute nodes, where it is not on a job's ``PATH``."""

    # --- validation ---

    @model_validator(mode="after")
    def _site(self) -> Self:
        if self.drain_minutes >= self.walltime_minutes:
            raise ValueError("drain_minutes must be below walltime_minutes")
        if not Path(os.path.expandvars(self.shared_fs_dir)).expanduser().is_absolute():
            raise ValueError("shared_fs_dir must resolve to an absolute path")
        if self.podman is not None and not Path(self.podman).is_absolute():
            raise ValueError("podman must be an absolute path on the compute nodes")
        partitions = {partition.name: partition for partition in self.partitions}
        if len(partitions) != len(self.partitions):
            raise ValueError("partition names must be unique within a cluster")
        if self.build_queue is not None and self.build_queue not in partitions:
            raise ValueError("build_queue must name a configured partition")
        if self.build_gpus and (
            not self.build_queue or self.build_gpus > partitions[self.build_queue].max_gpus
        ):
            raise ValueError("build_gpus must fit the configured build_queue")
        return self


class SlurmProviderConfig(ClusterProviderConfig):
    workload_manager: ClassVar[str] = "slurm"
    partitions: tuple[SlurmPartition, ...] = ()
    shared_fs_dir: str = Field(default="~/.cache/orbit/slurm", min_length=1)


class PBSProviderConfig(ClusterProviderConfig):
    workload_manager: ClassVar[str] = "pbs"
    partitions: tuple[PBSQueue, ...] = ()
    shared_fs_dir: str = Field(default="~/.cache/orbit/pbs", min_length=1)


class LSFProviderConfig(ClusterProviderConfig):
    workload_manager: ClassVar[str] = "lsf"
    partitions: tuple[LSFQueue, ...] = ()
    shared_fs_dir: str = Field(default="~/.cache/orbit/lsf", min_length=1)


class KubernetesQueue(ClusterPartition):
    """A node pool's capacity and the labels that place Jobs on its nodes."""

    max_nodes: int = Field(default=1, ge=1, le=254)
    """Distinct nodes available per allocation. Each peer needs a usable IPv4 /24 address."""
    node_selector: dict[str, str] = Field(default_factory=dict)
    min_gpus: int = Field(default=0, ge=0)
    """Whole GPUs reserved for every Pod in this pool."""

    # --- validation ---

    @model_validator(mode="after")
    def _gpu_floor(self) -> Self:
        if self.min_gpus > self.max_gpus:
            raise ValueError("min_gpus must fit the GPU capacity of the Kubernetes pool")
        return self


class KubernetesProviderConfig(ClusterProviderConfig):
    """The namespace, worker image and shared volume for Kubernetes Jobs."""

    workload_manager: ClassVar[str] = "kubernetes"
    node_local_dir: str = "/scratch"
    namespace: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    worker_image: str = Field(min_length=1, pattern=r"^\S+$")
    shared_claim: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$",
    )
    context: str | None = Field(default=None, min_length=1)
    kubeconfig: str | None = Field(default=None, min_length=1)
    runtime_class: str | None = Field(default=None, min_length=1)
    node_selector: dict[str, str] = Field(default_factory=dict)
    partitions: tuple[KubernetesQueue, ...] = Field(min_length=1)

    @property
    def node_labels(self) -> dict[str, str]:
        """The platform of every Job's worker and its Sandbox images."""
        arch = "amd64" if self.architecture is Arch.X86_64 else "arm64"
        return {"kubernetes.io/os": "linux", "kubernetes.io/arch": arch}

    # --- validation ---

    @field_validator("shared_fs_dir")
    @classmethod
    def _volume_mount(cls, value: str) -> str:
        # The serialized episode must use the submit host's mount path even if the
        # worker image has a different HOME or environment.
        return str(Path(os.path.expandvars(value)).expanduser())

    @model_validator(mode="after")
    def _job_placement(self) -> Self:
        cache = Path(self.shared_fs_dir)
        scratch = Path(self.node_local_dir)
        for field, mount in (("shared_fs_dir", cache), ("node_local_dir", scratch)):
            if (
                not mount.is_absolute()
                or len(mount.parts) < 2
                or ".." in mount.parts
                or mount.parts[1]
                in {"bin", "dev", "etc", "lib", "lib64", "proc", "run", "sbin", "sys", "usr"}
            ):
                raise ValueError(f"{field} must be an absolute data directory in the worker image")
        if cache.is_relative_to(scratch) or scratch.is_relative_to(cache):
            raise ValueError("shared_fs_dir and node_local_dir must be separate mount paths")
        if self.account:
            raise ValueError("account is not a Kubernetes Job setting")
        for queue in self.partitions:
            labels = self.node_labels
            for selector in (self.node_selector, queue.node_selector):
                for key, value in selector.items():
                    if key in labels and labels[key] != value:
                        raise ValueError(f"node_selector for {queue.name} conflicts on {key!r}")
                    labels[key] = value
        return self


class DaytonaProviderConfig(BaseModel):
    """Settings for the Daytona scheduler. The API key is read from the environment."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(default="us", min_length=1)
    """The Daytona region sandboxes are created in."""
    api_key_env: str = Field(default="ORBIT_DAYTONA_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    """Names the variable holding the key, so the key itself is never a setting."""
    lifetime_hours: int = Field(default=24, ge=1, le=720, strict=True)
    """Daytona deletes a sandbox this long after creation, so a crashed run cannot leak one."""
    retained_lifetime_hours: int = Field(default=72, ge=1, le=720, strict=True)
    """The same limit for sandboxes kept with ``sandbox_retention``."""


class FleetConfig(BaseModel):
    """Where a Run finds its Fleet's control plane. The token is read from the environment."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(default="", pattern=r"^(https?://.+)?$")
    """The control plane, such as ``http://superserver:7470``."""
    token_env: str = Field(default="ORBIT_FLEET_TOKEN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class SandboxConfig(BaseModel):
    """Which Providers start a Run's Sandboxes, and their settings."""

    model_config = ConfigDict(extra="forbid")

    providers: tuple[ProviderId, ...] = ("docker:runc", "qemu", "orchard")
    """In order of preference: each Sandbox goes to the first one that serves its needs. The
    choice is by capability alone, never by load, so one configuration always chooses the
    same way; a scheduler's own leaves are its deployment's to configure."""
    docker: DockerProviderConfig = DockerProviderConfig()
    slurm: SlurmProviderConfig = SlurmProviderConfig()
    pbs: PBSProviderConfig = PBSProviderConfig()
    lsf: LSFProviderConfig = LSFProviderConfig()
    kubernetes: KubernetesProviderConfig | None = None
    daytona: DaytonaProviderConfig = DaytonaProviderConfig()
    fleet: FleetConfig = FleetConfig()

    @property
    def cluster(self) -> ClusterProviderConfig | None:
        """The cluster the Run's Provider runs its episodes on, where it names one."""
        return next(
            (
                site
                for site in (self.slurm, self.pbs, self.lsf, self.kubernetes)
                if site is not None and site.workload_manager in self.providers
            ),
            None,
        )

    @property
    def linux_container_arch(self) -> Arch:
        """Native target of this Run's cluster images, independent of the controller's CPU."""
        return self.cluster.architecture if self.cluster is not None else Arch.X86_64

    # --- validation ---

    @field_validator("providers")
    @classmethod
    def _one_of_each(cls, values: tuple[ProviderId, ...]) -> tuple[ProviderId, ...]:
        if not values:
            raise ValueError("sandbox.providers names no Provider")
        if repeated := sorted({value for value in values if values.count(value) > 1}):
            raise ValueError(f"sandbox.providers names {', '.join(repeated)} more than once")
        return values

    @model_validator(mode="after")
    def _settings_have_their_provider(self) -> Self:
        if set(self.providers) & set(CLUSTER_PROVIDERS) and len(self.providers) > 1:
            raise ValueError(
                "a cluster's Provider runs each episode in a job of its own, so it is the "
                "Run's only Provider"
            )
        if "kubernetes" in self.providers and self.kubernetes is None:
            raise ValueError("the kubernetes Provider needs sandbox.kubernetes and its partitions")
        if (site := self.cluster) is not None and not site.partitions:
            name = site.workload_manager
            raise ValueError(f"the {name} Provider needs sandbox.{name}.partitions")
        if self.docker.gpus and not any(name.startswith("docker:") for name in self.providers):
            raise ValueError(
                "sandbox.docker.gpus selects Docker devices; sandbox.providers names no Docker leaf"
            )
        if "fleet" in self.providers and not self.fleet.url:
            raise ValueError("the fleet Provider needs sandbox.fleet.url, its control plane")
        return self


_SCHEDULING_FIELDS = frozenset({"episodes", "episode_retries", "resume", "concurrency"})


class RunTimeouts(BaseModel):
    """Runtime overrides; omitted phases retain the Task's deadlines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: Annotated[float, Field(gt=0, allow_inf_nan=False)] | Literal["unlimited"] | None = None
    verify: Annotated[float, Field(gt=0, allow_inf_nan=False)] | Literal["unlimited"] | None = None

    def agent_time(self, spec: TaskSpec) -> float | None:
        """The agent's time across all its turns: this Run's, or else the Task's own; None
        where unlimited."""
        if self.agent == "unlimited":
            return None
        return self.agent or spec.timeouts.agent

    def verification(self, spec: TaskSpec, *, validation: bool = False) -> float | None:
        """The limit of ``spec``'s verifier of ``tests/``, or of its validation rounds: this
        Run's, which holds for both, or else the Task's own; None where unlimited."""
        if self.verify == "unlimited":
            return None
        return self.verify or (spec.validation_timeout if validation else spec.timeouts.verify)


class RunConfig(BaseModel):
    """Everything one invocation needs, after all layers are merged."""

    model_config = ConfigDict(extra="forbid")

    sandbox: SandboxConfig = SandboxConfig()
    artifacts: ArtifactPolicy = ArtifactPolicy()
    agent: AgentConfig = AgentConfig()
    gateway: GatewayConfig = GatewayConfig()
    verification: VerificationConfig = VerificationConfig()
    timeouts: RunTimeouts = RunTimeouts()
    validation_rounds: Annotated[int, Field(ge=0, strict=True)] | Literal["unlimited"] | None = None
    """How many validation rounds each agent receives instead of each Task's
    ``[agent].validation_rounds``, or ``"unlimited"``; ``None`` keeps the Task's."""
    sandbox_retention: SandboxRetentionConfig = SandboxRetentionConfig()
    desktop_resolution: DesktopResolution = Field(
        default=DesktopResolution(),
        description=(
            "The size every Sandbox's desktop is started at, whatever its image would use. "
            "It decides what fits on the screen an agent sees, so it is a Run setting and "
            "one prepared image serves every size."
        ),
    )
    logging: LoggingPolicy = LoggingPolicy()
    episodes: int = Field(default=1, ge=1)
    episode_retries: int = Field(default=0, ge=0, strict=True)
    seed: int = 0
    resume: bool = True
    concurrency: int = Field(default=1, ge=1)
    _preset_name: str | None = PrivateAttr(default=None)
    _preset_digest: str | None = PrivateAttr(default=None)
    _authentication_source: Literal["cli", "run", "preset", "default"] = PrivateAttr(
        default="default"
    )

    @property
    def config_hash(self) -> str:
        """Identity of the effective settings, recorded in provenance."""
        return content_hash(self.model_dump(mode="json"))

    @property
    def reuse_hash(self) -> str:
        """Identity of the settings one episode runs under, for completed-episode reuse.

        Scheduling settings decide how many episodes run and how, not what any one of them
        does, so resuming with another count or parallelism reuses completed episodes.
        """
        return content_hash(self.model_dump(mode="json", exclude=set(_SCHEDULING_FIELDS)))

    @property
    def preset_name(self) -> str | None:
        return self._preset_name

    @property
    def preset_digest(self) -> str | None:
        return self._preset_digest

    @property
    def authentication_source(self) -> Literal["cli", "run", "preset", "default"]:
        return self._authentication_source

    # --- validation ---

    @model_validator(mode="after")
    def _some_limit(self) -> Self:
        """An agent whose time, rounds and Gateway limits are all unlimited could run for
        ever: one of them must hold."""
        limits = self.gateway.limits
        if (
            self.timeouts.agent == "unlimited"
            and self.validation_rounds == "unlimited"
            and all(value == "unlimited" for value in limits.model_dump().values())
        ):
            raise ValueError(
                "timeouts.agent and validation_rounds are unlimited, and so is every "
                "gateway.limits: set one of them, or the agent could run for ever"
            )
        return self


def parse_override(text: str) -> tuple[list[str], Any]:
    """Parse ``a.b.c=value`` into a key path and a TOML-typed value."""
    key, sep, raw = text.partition("=")
    if not sep or not key.strip():
        raise ConfigError(f"invalid override {text!r}: expected key.path=value")
    path = [part for part in key.strip().split(".") if part]
    if not path:
        raise ConfigError(f"invalid override {text!r}: empty key path")
    try:
        value = tomllib.loads(f"v = {raw.strip()}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()  # bare strings need no quoting
    return path, value


def _assign(target: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = target
    for part in path[:-1]:
        node = cursor.setdefault(part, {})
        if not isinstance(node, dict):
            raise ConfigError(f"cannot set {'.'.join(path)}: {part} is not a section")
        cursor = node
    cursor[path[-1]] = value


_ADDITIVE = {("agent", "skills"), ("agent", "mcp_servers")}


def _deep_merge(
    base: dict[str, Any], overlay: dict[str, Any], prefix: tuple[str, ...] = ()
) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value, (*prefix, key))
        elif (*prefix, key) in _ADDITIVE and isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value]
        else:
            merged[key] = value
    return merged


def _resolve_resource_paths(
    data: dict[str, Any], *, base: Path, origin: Literal["preset", "run", "cli"]
) -> dict[str, Any]:
    resolved = deepcopy(data)
    agent = resolved.get("agent")
    if not isinstance(agent, dict):
        return resolved
    for field in ("skills", "mcp_servers"):
        entries = agent.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            declared = entry.get("path") or entry.get("builtin")
            if path := entry.get("path"):
                candidate = Path(path)
                entry["path"] = str(candidate if candidate.is_absolute() else (base / candidate))
            entry["origin"] = origin
            entry["declared"] = str(declared)
    return resolved


def _read_toml(path: Path, *, origin: Literal["preset", "run"] | None = None) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
            return (
                _resolve_resource_paths(data, base=path.parent.resolve(), origin=origin)
                if origin
                else data
            )
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def merge_layers(
    *,
    preset: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    overrides: list[str] | None = None,
) -> dict[str, Any]:
    """Merge configuration layers in precedence order (lowest first)."""
    merged: dict[str, Any] = {}
    for layer in (preset, run):
        if layer:
            merged = _deep_merge(merged, layer)
    override_layer: dict[str, Any] = {}
    for override in overrides or []:
        path, value = parse_override(override)
        _assign(override_layer, path, value)
    if override_layer:
        merged = _deep_merge(
            merged,
            _resolve_resource_paths(override_layer, base=Path.cwd(), origin="cli"),
        )
    return merged


def select_agent_name(*, run_path: Path | None = None, overrides: list[str] | None = None) -> str:
    """Resolve only the selector needed to choose an autonomous preset."""
    merged = merge_layers(
        run=_read_toml(run_path) if run_path else None,
        overrides=overrides,
    )
    agent = merged.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("name"), str):
        return agent["name"]
    return str(AgentConfig.model_fields["name"].default)


def load_run_config(
    *,
    preset_path: Path | None = None,
    run_path: Path | None = None,
    overrides: list[str] | None = None,
) -> RunConfig:
    """Build the effective configuration.

    Raises:
        ConfigError: on a missing file, malformed TOML, a bad override, or any key the
            schema does not define.
    """
    preset_data = _read_toml(preset_path, origin="preset") if preset_path else None
    run_data = _read_toml(run_path, origin="run") if run_path else None
    merged = merge_layers(preset=preset_data, run=run_data, overrides=overrides)
    try:
        config = RunConfig.model_validate(merged)
        if preset_path:
            config._preset_name = preset_path.stem
            config._preset_digest = content_hash(preset_path.read_text(encoding="utf-8"))
        for source, data in (("preset", preset_data), ("run", run_data)):
            agent = data.get("agent") if data else None
            if isinstance(agent, dict) and "authentication" in agent:
                config._authentication_source = source  # type: ignore[assignment]
        for override in overrides or ():
            path, _ = parse_override(override)
            if path == ["agent", "authentication"]:
                config._authentication_source = "cli"
        return config
    except Exception as exc:  # pydantic ValidationError, reported as a config problem
        raise ConfigError(f"invalid configuration: {exc}") from exc

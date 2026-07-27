"""Container backend.

Shells out to the docker CLI rather than binding an SDK: the CLI is stable, already
installed wherever docker is, and keeps this provider readable.

Network isolation is topological, not rule-based. Each run gets its own
``--internal`` bridge, which has no route off the host, and the gateway is attached to
that bridge. Deny-all with a single controlled egress therefore falls out of the
network's shape — there are no firewall rules to survive a daemon restart, and nothing
inside the sandbox can lift them.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Capabilities,
    ExecResult,
    Identity,
    Provider,
    Sandbox,
    SandboxRequest,
    SandboxState,
)
from ale.core.taskspec import NetworkMode
from ale.run.transport import GuestClient, StdioTransport

__all__ = ["DockerProvider", "DockerSandbox"]

GUESTD_DIR = PurePosixPath("/opt/ale/guestd")

#: An image sets this to "true" when it starts a desktop, so provisioning waits for it.
GUI_LABEL = "ale.gui"

#: The unprivileged account an image intends the agent to be. Read rather than assumed:
#: the image chose the name, created the home directory and owns the desktop session.
USER_LABEL = "ale.user"

#: Used when an image declares nothing, so an image predating the contract still runs.
DEFAULT_AGENT_USER = "user"

LABEL = "ale.episode"

#: The name a sandbox uses for its gateway. It is mapped to the host's address *on the
#: container's own bridge* rather than to docker's usual host-gateway: an ``--internal``
#: network has no default route at all, so the only address that works is one inside the
#: sandbox's own subnet. That is the provider's half of the deny-all bargain — no route
#: off the host, and exactly one destination still reachable.
HOST_ALIAS = "ale-gateway.internal"


async def _docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise ProviderStartError(
            f"docker {' '.join(argv[:2])} timed out after {timeout:g}s"
        ) from None
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


class DockerSandbox(Sandbox):
    """One container, driven through the guest service."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        container: str,
        network: str | None,
        client: GuestClient,
        agent_user: str = DEFAULT_AGENT_USER,
    ) -> None:
        super().__init__(sandbox_id=sandbox_id, request=request)
        self.container = container
        self.network = network
        self._client = client
        self.agent_user = agent_user
        self.state = SandboxState.READY

    def _as(self, identity: Identity) -> str | None:
        """The unix user for a role, or ``None`` to keep the container's default.

        Framework work stays as the image's own user, which for a sandbox image is root:
        installing the guest service and collecting artifacts have to work whatever the
        agent did to its own files.
        """
        return self.agent_user if identity is Identity.AGENT else None

    @property
    def gateway_url(self) -> str | None:
        """The gateway as this container can reach it.

        The host binds to every interface; a container cannot use that address, so the
        URL is rewritten here rather than in the gateway — which is exactly the point of
        the gateway knowing nothing about providers.
        """
        return _reachable(self.request.gateway_url)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        identity: Identity = Identity.FRAMEWORK,
    ) -> ExecResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        exit_code, stdout, stderr = await self._client.exec(
            argv, cwd=cwd, env=env, timeout_sec=timeout_sec, run_as=self._as(identity)
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((loop.time() - started) * 1000),
        )

    async def write_file(
        self, path: PurePosixPath | str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.write_file(str(path), data, run_as=self._as(identity))

    async def read_file(self, path: PurePosixPath | str) -> bytes:
        return await self._client.read_file(str(path))

    async def upload_dir(
        self, source: str, target: PurePosixPath | str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        """Copy a directory in, owned by whoever the caller said it is for.

        `docker cp` writes as root whatever `--user` would say, so ownership is set
        afterwards rather than assumed. Staging content the agent must be able to change
        and leaving it root-owned is the failure this exists to prevent — and one the
        framework must not paper over later, since a task decides what it opens up.
        """
        await self._client.mkdirs(str(target))
        code, _, stderr = await _docker("cp", f"{Path(source)}/.", f"{self.container}:{target}")
        if code != 0:
            raise ProviderStartError(f"upload to {target} failed: {stderr.strip()}")
        if (owner := self._as(identity)) is not None:
            code, _, stderr = await _docker(
                "exec", self.container, "chown", "-R", owner, str(target)
            )
            if code != 0:
                raise ProviderStartError(f"could not give {target} to {owner}: {stderr.strip()}")

    async def download_dir(self, source: PurePosixPath | str, target: str) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)
        code, _, stderr = await _docker("cp", f"{self.container}:{source}/.", target)
        if code != 0:
            raise ProviderStartError(f"download from {source} failed: {stderr.strip()}")

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        # Accept either raw dicts or DesktopAction models, so callers need no adapter.
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED
        await self._client.close()
        await _docker("rm", "-f", "-v", self.container, timeout=60)
        if self.network:
            await _docker("network", "rm", self.network, timeout=60)


class DockerProvider(Provider):
    """Supplies container sandboxes."""

    name = "docker"

    def __init__(self, *, guestd_source: Path | None = None) -> None:
        self._guestd_source = guestd_source or Path(__file__).resolve().parents[1] / "guestd"
        # An image's conformance cannot change while it is pinned to a digest, and the
        # check costs a container start — so a sweep of ten thousand episodes pays for it
        # once rather than ten thousand times.
        self._checked: dict[str, list[str]] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            os="linux",
            gui=True,  # depends on the image; the GUI base image provides a desktop
            gpus=0,
            network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.ALLOWLIST, NetworkMode.OPEN}),
        )

    async def preflight(self) -> None:
        if shutil.which("docker") is None:
            raise ProviderStartError("docker is not installed — see `just doctor`")
        code, _, stderr = await _docker("info", "--format", "{{.ServerVersion}}", timeout=30)
        if code != 0:
            raise ProviderStartError(f"docker daemon unreachable: {stderr.strip()}")

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.accepts(request)
        if problems := await self.check_image(request.image_ref):
            raise ProviderCapabilityError(
                f"{request.image_ref} cannot serve as a sandbox: {'; '.join(problems)}. "
                "See docs/specs/sandbox-image.md"
            )

        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        container = f"ale-{sandbox_id}"
        network = await self._ensure_network(sandbox_id, request)

        argv = [
            "run", "-d", "--name", container,
            "--label", f"{LABEL}={request.episode_id}",
            "--cpus", str(request.resources.cpus),
            "--memory", f"{request.resources.memory_mb}m",
        ]  # fmt: skip
        if network:
            argv += ["--network", network]
        for key, value in request.env.items():
            argv += ["-e", f"{key}={value}"]
        if request.gateway_url:
            host_ip = await self._bridge_host_ip(network)
            reachable = _reachable(request.gateway_url)
            argv += ["--add-host", f"{HOST_ALIAS}:{host_ip}"]
            argv += ["-e", f"ALE_GATEWAY_URL={reachable}"]
        if request.network.mode is NetworkMode.ALLOWLIST and request.proxy_url:
            # Conventional proxy variables, because that is what the tools a task will
            # reach for already read. Traffic that ignores them has nowhere to go: the
            # bridge has no route off the host, so this fails closed.
            proxy = _reachable(request.proxy_url)
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                argv += ["-e", f"{name}={proxy}"]
            argv += ["-e", f"NO_PROXY={HOST_ALIAS},localhost,127.0.0.1"]
        # The image's own command, always. A container lives exactly as long as its
        # command, and substituting one replaces whatever the image starts for itself —
        # for a desktop image, the entire graphical session. Images that have nothing to
        # do declare a command that waits; that is part of the image contract, so there
        # is nothing left here to decide.
        argv += [request.image_ref]

        code, _, stderr = await _docker(*argv, timeout=300)
        if code != 0:
            if network:
                await _docker("network", "rm", network)
            raise ProviderStartError(f"could not start container: {stderr.strip()}")

        agent_user = await self._agent_user(request.image_ref)

        try:
            await self._install_guestd(container)
            if request.sudo:
                await self._grant_sudo(container, agent_user)
            client = await self._connect(container)
            if request.needs_gui or await self._has_desktop(request.image_ref):
                await self._await_desktop(client)
        except Exception:
            await _docker("rm", "-f", "-v", container)
            if network:
                await _docker("network", "rm", network)
            raise

        return DockerSandbox(
            sandbox_id=sandbox_id,
            request=request,
            container=container,
            network=network,
            client=client,
            agent_user=agent_user,
        )

    async def _ensure_network(self, sandbox_id: str, request: SandboxRequest) -> str | None:
        """Create the per-episode network.

        ``block`` and ``allowlist`` both use ``--internal``: the bridge has no route off
        the host, so the only reachable thing is the gateway. They differ in what the
        gateway will then forward, not in what the sandbox can dial — which is why
        allowlisting needs no privileged host rules and no tooling inside an image we do
        not control. ``open`` uses the default bridge.
        """
        if request.network.mode is NetworkMode.OPEN:
            return None
        network = f"ale-net-{sandbox_id}"
        code, _, stderr = await _docker("network", "create", "--internal", network, timeout=60)
        if code != 0:
            raise ProviderStartError(f"could not create isolated network: {stderr.strip()}")
        return network

    async def _bridge_host_ip(self, network: str | None) -> str:
        """The host's address on a container's network.

        On an isolated bridge this is the only host address a sandbox can dial, which is
        why it is read from the network rather than assumed.
        """
        if network is None:
            return "host-gateway"
        code, out, stderr = await _docker(
            "network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}"
        )
        if code != 0 or not out.strip():
            raise ProviderStartError(f"could not read the gateway address of {network}: {stderr}")
        return out.strip()

    async def check_image(self, reference: str) -> list[str]:
        """What this image is missing from the sandbox contract, if anything.

        Checked before the sandbox is used rather than discovered through its symptoms:
        an image with no long-lived command dies immediately, and one with no agent
        account fails on the first thing the agent tries to do — neither failure names
        its cause. See `docs/specs/sandbox-image.md`.

        Partial by construction: whether a command *stays* running cannot be known
        without running it, so this catches an image that declares none at all. The rest
        surfaces as a sandbox that dies on start, which at least says so immediately.
        """
        if reference in self._checked:
            return self._checked[reference]

        missing: list[str] = []

        code, out, _ = await _docker(
            "image",
            "inspect",
            "--format",
            "{{json .Config.Cmd}}{{json .Config.Entrypoint}}",
            reference,
        )
        if code != 0:
            return [f"image {reference} could not be inspected; is it pulled?"]
        if out.strip().startswith("null"):
            missing.append(
                "no command: a container lives exactly as long as its command, and the "
                "engine will not supply one"
            )

        probe = await _docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            reference,
            "-c",
            f"command -v python3 >/dev/null || echo no-python3; "
            f"id -u {await self._agent_user(reference)} >/dev/null 2>&1 || echo no-agent-user",
            timeout=120,
        )
        for line in probe[1].split():
            if line == "no-python3":
                missing.append("no python3 on PATH: the guest service runs on it")
            elif line == "no-agent-user":
                user = await self._agent_user(reference)
                missing.append(
                    f"no unprivileged account {user!r}: the agent would have to run as root, "
                    "and could then change the conditions it is measured under"
                )

        self._checked[reference] = missing
        return missing

    async def _agent_user(self, reference: str) -> str:
        """The account this image intends the agent to be.

        Asked of the image because the image is what created it, gave it a home and runs
        the desktop session as it. Guessing here would mean the engine deciding who owns
        files in somebody else's filesystem.
        """
        return await self._label(reference, USER_LABEL) or DEFAULT_AGENT_USER

    async def _grant_sudo(self, container: str, agent_user: str) -> None:
        """Let the agent elevate, because its task said it needs to.

        Granted at provisioning rather than left to the task's setup, so that the
        privilege is something the run configured and recorded — not something a script
        arranged where provenance would never see it.
        """
        rule = f"{agent_user} ALL=(ALL) NOPASSWD: ALL"
        code, _, stderr = await _docker(
            "exec",
            container,
            "sh",
            "-c",
            f"mkdir -p /etc/sudoers.d && printf '%s\\n' '{rule}' > /etc/sudoers.d/ale-agent "
            "&& chmod 0440 /etc/sudoers.d/ale-agent",
        )
        if code != 0:
            raise ProviderStartError(
                f"the task asked for elevated privileges and this image cannot grant them: "
                f"{stderr.strip()}"
            )

        # Writing a sudoers rule succeeds in an image with no sudo at all, so the grant is
        # confirmed by using it. Reporting a privilege that was never conferred is the
        # failure this whole declaration exists to avoid — the task would run, fail on
        # access, and provenance would record an isolation level that never applied.
        code, _, stderr = await _docker("exec", "-u", agent_user, container, "sudo", "-n", "true")
        if code != 0:
            raise ProviderStartError(
                f"elevated privileges were requested but {agent_user} still cannot elevate; "
                f"this image does not support them: {stderr.strip()}"
            )

    async def _has_desktop(self, reference: str) -> bool:
        """Whether this image brings a desktop up, and so must be waited for.

        Asked of the image rather than derived from the harness. A task's own setup can
        need the screen — this one opens a window on it — so a desktop image has to be
        ready whichever agent is about to run, including one that never looks at it.
        Deriving it from the harness made `--agent nop` skip the wait and fail in setup
        with "Can't open display", while the same task passed under a stepwise agent.
        """
        return await self._label(reference, GUI_LABEL) == "true"

    async def _label(self, reference: str, name: str) -> str:
        code, out, _ = await _docker(
            "image", "inspect", "--format", f'{{{{index .Config.Labels "{name}"}}}}', reference
        )
        return out.strip() if code == 0 else ""

    async def _await_desktop(self, client: GuestClient, timeout_sec: float = 120) -> None:
        """Block until the screen can actually be captured.

        A desktop image starts an X server, a window manager and a session from its own
        command, and none of that is instant. Waiting on a marker file would work for
        one image and be a lie for the next, so this waits on the capability itself:
        a sandbox is ready for GUI work when a screenshot succeeds.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        last: Exception | None = None
        while loop.time() < deadline:
            try:
                await client.screenshot()
                return
            except Exception as exc:
                last = exc
                await asyncio.sleep(1.0)
        raise ProviderStartError(
            f"the desktop did not come up within {timeout_sec:g}s; last attempt: {last}"
        )

    async def _install_guestd(self, container: str) -> None:
        """Copy the guest service in.

        Base images bake it, but copying keeps the provider usable with any image and
        guarantees the running code matches this checkout — worth the few milliseconds.
        """
        code, _, stderr = await _docker("exec", container, "mkdir", "-p", str(GUESTD_DIR))
        if code != 0:
            raise ProviderStartError(f"could not prepare guest directory: {stderr.strip()}")
        for name in ("main.py", "protocol.py", "gui.py"):
            source = self._guestd_source / name
            if not source.exists():
                continue
            code, _, stderr = await _docker(
                "cp", str(source), f"{container}:{GUESTD_DIR}/{name}", timeout=60
            )
            if code != 0:
                raise ProviderStartError(f"could not install {name}: {stderr.strip()}")

    async def _connect(self, container: str) -> GuestClient:
        transport = StdioTransport(
            [
                "docker",
                "exec",
                "-i",
                container,
                # One interpreter, and it is the image's own. An image that needs the
                # guest service to have Pillow installs it there; choosing between
                # interpreters was a rule that existed only to be got wrong.
                "python3",
                str(GUESTD_DIR / "main.py"),
                "--stdio",
            ]
        )
        await transport.start()
        client = GuestClient(transport)
        try:
            await asyncio.wait_for(client.health(), timeout=60)
        except Exception as exc:
            await client.close()
            raise ProviderStartError(f"guest service did not answer: {exc}") from exc
        return client


def _reachable(url: str | None) -> str | None:
    """Rewrite a host-side gateway URL into one a container can dial."""
    if not url:
        return url
    for bound in ("0.0.0.0", "127.0.0.1", "localhost", "[::]"):
        if f"//{bound}:" in url:
            return url.replace(f"//{bound}:", f"//{HOST_ALIAS}:", 1)
    return url

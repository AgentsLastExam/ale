"""Small shared helpers for pinned npm-distributed harnesses."""

from __future__ import annotations

import re

from ale.core.errors import AgentError
from ale.core.sandbox import Identity, Sandbox

_VERSION = re.compile(r"\d+(?:\.\d+)+")


async def agent_home(sandbox: Sandbox) -> str:
    result = await sandbox.exec(["sh", "-c", 'printf %s "$HOME"'], identity=Identity.AGENT)
    return result.stdout.strip() or "/home/user"


def npm_env(home: str) -> dict[str, str]:
    prefix = f"{home}/.local"
    return {
        "PATH": f"{prefix}/bin:/usr/local/bin:/usr/bin:/bin",
        "npm_config_cache": f"{prefix}/.npm-cache",
    }


async def ensure_npm_cli(
    sandbox: Sandbox,
    *,
    package: str,
    binary: str,
    version: str,
    extra_env: dict[str, str] | None = None,
) -> str:
    home = await agent_home(sandbox)
    env = {**npm_env(home), **(extra_env or {})}
    probe = await sandbox.exec(
        ["sh", "-c", f"command -v {binary} >/dev/null 2>&1 && {binary} --version"],
        env=env,
        timeout_sec=60,
        identity=Identity.AGENT,
    )
    installed = _reported_version(probe.stdout + probe.stderr) if probe.ok else None
    if installed != version:
        result = await sandbox.exec(
            [
                "npm",
                "install",
                "-g",
                "--force",
                "--prefix",
                f"{home}/.local",
                f"{package}@{version}",
            ],
            env=env,
            timeout_sec=1200,
            identity=Identity.AGENT,
        )
        if not result.ok:
            detail = (result.stderr or result.stdout)[-800:]
            raise AgentError(f"could not install {package}@{version}: {detail}")
        probe = await sandbox.exec(
            ["sh", "-c", f"{binary} --version"],
            env=env,
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        installed = _reported_version(probe.stdout + probe.stderr) if probe.ok else None
    if installed != version:
        raise AgentError(f"{binary} reports {installed or 'no version'} after installing {version}")
    return installed


def _reported_version(output: str) -> str | None:
    match = _VERSION.search(output)
    return match.group(0) if match else None

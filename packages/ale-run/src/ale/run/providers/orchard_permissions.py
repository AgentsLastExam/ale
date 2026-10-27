"""Opt-in local macOS Driver installation and UI authorization for owned VMs."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ale.core.config import OrchardProviderConfig
from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.run.framework import MACOS_DRIVER_SHA256, macos_driver_archive
from ale.run.providers.orchard_client import OrchardClient

_LOG = logging.getLogger(__name__)
# Only a readiness boolean crosses Orchard. Images for UI decisions always come from VNC.
_DRIVER_CAPTURE = r"""
import base64,json,subprocess,sys
try:
    result=subprocess.run(
        [sys.argv[1],'call','get_desktop_state','{}','--socket',sys.argv[2]],
        capture_output=True,timeout=5,check=True,
    )
    payload=json.loads(result.stdout)
    encoded=payload.get('screenshot_png_b64')
    ready=(not payload.get('isError') and not payload.get('error')
        and isinstance(encoded,str)
        and base64.b64decode(encoded,validate=True).startswith(b'\x89PNG\r\n\x1a\n'))
except (OSError,subprocess.SubprocessError,ValueError,AttributeError):
    ready=False
print('ready' if ready else 'not_ready')
"""
_INSTALL = r"""
import io,os,pathlib,pwd,shutil,sys,tarfile,tempfile
user=sys.argv[1]
if os.stat('/dev/console').st_uid != pwd.getpwnam(user).pw_uid:
    raise RuntimeError('task user must own the logged-in console')
app=pathlib.Path('/Applications/CuaDriver.app')
if app.exists() or app.is_symlink():
    raise RuntimeError('desktop bootstrap requires an image without CuaDriver.app')
with tempfile.TemporaryDirectory(prefix='ale-driver-') as td:
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as archive:
        archive.extractall(td,filter='data')
    apps=list(pathlib.Path(td).rglob('CuaDriver.app'))
    if len(apps)!=1: raise RuntimeError('archive does not contain one Driver app')
    shutil.copytree(apps[0],app,symlinks=True)
for root,dirs,files in os.walk(app):
    for name in ['.',*dirs,*files]:
        path=pathlib.Path(root)/name
        os.chown(path,0,0,follow_symlinks=False)
"""


def identity() -> str:
    """Include installed code and UI automation in the prepared framework identity."""
    digest = hashlib.sha256(MACOS_DRIVER_SHA256.encode())
    for source in [Path(__file__), *sorted(Path(__file__).parent.glob("macos_permission_*"))]:
        if source.is_file():
            digest.update(source.name.encode() + b"\0" + source.read_bytes())
    return digest.hexdigest()


def preflight(config: OrchardProviderConfig) -> None:
    if config.desktop_bootstrap is None:
        return
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        raise ProviderCapabilityError("macOS UI bootstrap needs a local Mac with Swift tools")
    if any(importlib.util.find_spec(name) is None for name in ("PIL", "vncdotool")):
        raise ProviderCapabilityError("Install ale-run[macos-ui] to use desktop_bootstrap")
    password = os.environ.get(config.desktop_bootstrap.password_env, "")
    if not password or any(not 32 <= ord(c) <= 126 for c in password):
        raise ProviderCapabilityError(
            "UI bootstrap needs a nonempty printable ASCII guest password"
        )


async def install_driver(
    client: OrchardClient, name: str, user: str, config: OrchardProviderConfig, deadline: float
) -> None:
    preflight(config)
    payload = await asyncio.to_thread(macos_driver_archive)
    result = await client.exec(
        name,
        ["sudo", "-n", config.python_path, "-I", "-S", "-c", _INSTALL, user],
        stdin=payload,
        deadline=deadline,
        timeout_sec=deadline - asyncio.get_running_loop().time(),
    )
    if result.exit_code:
        raise ProviderStartError(
            "Driver installation failed: needs a logged-in task account, Python "
            "and no existing CuaDriver.app"
        )
    result = await client.exec(
        name,
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "/Applications/CuaDriver.app"],
        deadline=deadline,
    )
    if result.exit_code:
        raise ProviderStartError("Installed CuaDriver failed signature verification")
    result = await client.exec(
        name,
        [
            "sudo",
            "-n",
            "-u",
            user,
            "/usr/bin/open",
            "-a",
            "/Applications/CuaDriver.app",
            "--args",
            "serve",
            "--socket",
            config.socket_path,
        ],
        deadline=deadline,
    )
    if result.exit_code:
        raise ProviderStartError("Could not start CuaDriver in the task login session")


async def authorize_driver(
    client: OrchardClient, name: str, uid: str, user: str, config: OrchardProviderConfig
) -> None:
    """Passwords only cross an anonymous pipe and the encrypted guest VNC channel."""
    preflight(config)
    with tempfile.TemporaryDirectory(prefix="ale-macos-ui-") as td:
        ocr = Path(td) / "ocr"
        compiler = await asyncio.create_subprocess_exec(
            "swiftc",
            str(Path(__file__).with_name("macos_permission_ocr.swift")),
            "-o",
            str(ocr),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if await compiler.wait():
                raise ProviderStartError("Could not compile Apple Vision OCR")
        finally:
            if compiler.returncode is None:
                compiler.kill()
                await compiler.wait()
        async with client.forward_vnc(name, uid) as port:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "ale.run.providers.macos_permission_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                settings = {
                    "port": port,
                    "socket": config.socket_path,
                    "temporary": td,
                    "user": user,
                    "password": os.environ[config.desktop_bootstrap.password_env],
                    "retries": config.desktop_bootstrap.retries,
                    "ocr": str(ocr),
                    "capture_argv": [
                        config.python_path,
                        "-I",
                        "-S",
                        "-c",
                        _DRIVER_CAPTURE,
                        config.driver_path,
                        config.socket_path,
                    ],
                }
                process.stdin.write(json.dumps(settings).encode() + b"\n")
                await process.stdin.drain()
                result = None
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    if "guest" in event:
                        argv = event["guest"]
                        reply = await client.exec(
                            name, ["sudo", "-n", "-u", user, *argv], timeout_sec=30
                        )
                        process.stdin.write(
                            json.dumps(
                                {"code": reply.exit_code, "stdout": reply.stdout.decode()}
                            ).encode()
                            + b"\n"
                        )
                        await process.stdin.drain()
                    elif "event" in event:
                        _LOG.info("macOS permission UI: %s", event["event"])
                    elif "result" in event:
                        result = event["result"]
                code = await process.wait()
                if code or not result or result.get("status") != "ui_enabled":
                    raise ProviderStartError(
                        "macOS permission UI did not complete; bounded retries exhausted"
                    )
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()

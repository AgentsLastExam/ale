"""The verifier can use a newer Python without changing the solver's default."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.errors import TaskError
from ale.core.sandbox import ExecResult
from ale.core.taskspec import OperatingSystem
from ale.run.environments.standard import StandardEnvironment
from ale.run.harnesses.builtin import NopHarness

pytestmark = pytest.mark.unit


class LocalSandbox:
    def __init__(self, bin_dir: Path) -> None:
        self.env = {**os.environ, "PATH": str(bin_dir)}

    async def exec(self, argv: list[str]) -> ExecResult:
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, check=False)
        return ExecResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)


def executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


@pytest.mark.asyncio
@pytest.mark.parametrize("default_is_modern", [False, True])
async def test_linux_selects_newer_python_without_replacing_default(
    tmp_path: Path, default_is_modern: bool
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sh").symlink_to("/bin/sh")
    modern = f'exec {shlex.quote(sys.executable)} "$@"'
    old = "echo 'Python 3.10 is too old' >&2; exit 1"
    executable(bin_dir / "python3", modern if default_is_modern else old)
    executable(bin_dir / "python3.12", modern)
    sandbox = LocalSandbox(bin_dir)
    ctx = SimpleNamespace(spec=SimpleNamespace(os=OperatingSystem.LINUX))
    environment = StandardEnvironment(NopHarness())

    python, site = await environment._site_packages(ctx, sandbox)  # type: ignore[arg-type]
    assert python == ("python3" if default_is_modern else "python3.12")
    assert site
    await environment._probe_package(sandbox, python, "ale-verify", "import ale_verify")  # type: ignore[arg-type]
    if not default_is_modern:
        result = await sandbox.exec(["python3", "-c", "pass"])
        assert result.exit_code != 0 and "3.10" in result.stderr


@pytest.mark.asyncio
async def test_missing_compatible_linux_interpreter_is_a_task_error(tmp_path: Path) -> None:
    (tmp_path / "sh").symlink_to("/bin/sh")
    executable(tmp_path / "python3", "echo 'Python 3.10 is too old' >&2; exit 1")
    ctx = SimpleNamespace(spec=SimpleNamespace(os=OperatingSystem.LINUX))
    with pytest.raises(TaskError, match=r"python3:.*3.10.*python3.12: not available"):
        await StandardEnvironment(NopHarness())._site_packages(ctx, LocalSandbox(tmp_path))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_windows_keeps_its_system_python(tmp_path: Path) -> None:
    executable(tmp_path / "python.exe", f'exec {shlex.quote(sys.executable)} "$@"')
    ctx = SimpleNamespace(spec=SimpleNamespace(os=OperatingSystem.WINDOWS))
    python, site = await StandardEnvironment(NopHarness())._site_packages(
        ctx,
        LocalSandbox(tmp_path),  # type: ignore[arg-type]
    )
    assert python == "python.exe" and site

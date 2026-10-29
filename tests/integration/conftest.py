"""Shared self-contained Task fixture for Docker integration tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.support import CONTAINER_BASE, merged_config, write_task

IMAGE = CONTAINER_BASE

VERIFY_DEFAULT = textwrap.dedent("""
    #!/usr/bin/env bash
    set -euo pipefail
    exec python3 verify.py
""").strip()

VERIFY_CHECK = (
    "from ale_verify import checks, write\n"
    "check = checks.text_equals('/home/user/output/result.txt', 'hello world')\n"
    "write({'reward': check['score'], 'content': check})\n"
)


HELLO_DOCKERFILE = (
    f"FROM {IMAGE}\n"
    "RUN mkdir -p /home/user/input /home/user/output \\\n"
    " && printf 'world' > /home/user/input/word.txt \\\n"
    " && chown -R user:user /home/user/input /home/user/output\n"
)

HELLO_SOLVE = (
    "#!/usr/bin/env bash\nset -euo pipefail\n"
    "word=$(cat /home/user/input/word.txt)\n"
    "printf 'hello %s' \"$word\" > /home/user/output/result.txt\n"
)


def _write_repo(
    root: Path,
    *,
    with_solution: bool = True,
    test_body: str | None = None,
    config: Mapping[str, Any] | None = None,
    asset_roots: tuple[str, ...] = (),
    dockerfile: str | None = HELLO_DOCKERFILE,
) -> Path:
    """Create one complete Task, ``agentslastexam/hello``, below ``root/tasks/hello``."""
    task = write_task(
        root / "tasks" / "hello",
        name="agentslastexam/hello",
        config=merged_config({"artifacts": ["/home/user/output"]}, config),
        dockerfile=dockerfile,
        instruction=(
            "Write hello followed by the word in /home/user/input/word.txt "
            "into /home/user/output/result.txt\n"
        ),
        test=test_body or VERIFY_DEFAULT,
        solve=HELLO_SOLVE if with_solution else None,
    )
    (task / "tests" / "verify.py").write_text(VERIFY_CHECK + "\n")
    for relative in asset_roots:
        assets = task / relative
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "fixture.txt").write_text(relative)
    return task


@pytest.fixture
def write_repo() -> Callable[..., Path]:
    return _write_repo


@pytest.fixture
def isolated_ale_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "ale-cache"
    monkeypatch.setenv("ALE_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def docker_buildx() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    for command in (("docker", "info"), ("docker", "buildx", "version")):
        if subprocess.run(command, capture_output=True, check=False).returncode != 0:
            pytest.skip(f"{' '.join(command)} is unavailable")


@pytest.fixture
def kvm_host() -> None:
    if not Path("/dev/kvm").exists() or not os.access("/dev/kvm", os.R_OK | os.W_OK):
        pytest.skip("/dev/kvm is unavailable")
    if shutil.which("qemu-img") is None:
        pytest.skip("qemu-img is unavailable")

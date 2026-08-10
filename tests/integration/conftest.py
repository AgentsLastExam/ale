"""Shared self-contained Task fixture for Docker integration tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"

VERIFY_DEFAULT = textwrap.dedent("""
    #!/usr/bin/env bash
    set -euo pipefail
    exec python3 verify.py
""").strip()

VERIFY_CHECK = textwrap.dedent("""
    from ale_verify import Verification, checks

    verification = Verification()
    verification.check(
        "reward",
        checks.text_equals("/home/user/output/result.txt", "hello world"),
    )
    verification.write()
""").strip()


def _write_repo(
    root: Path,
    *,
    with_oracle: bool = True,
    verify_body: str | None = None,
    manifest_suffix: str = "",
    stage_assets: tuple[str, ...] = (),
    image: dict[str, str] | None = None,
    with_image_dockerfile: bool = True,
) -> Path:
    """Create one complete Task; the historical function name is kept fixture-local."""
    task = root / "tasks" / "hello"
    (task / "image").mkdir(parents=True)
    (task / "verify").mkdir()
    (task / "task.yaml").write_text(
        textwrap.dedent("""
        spec_type: core/v1
        name: hello
        image: {kind: container}
        resources: {cpus: 1, memory_mb: 512}
        timeouts: {setup: 120, agent: 120, verify: 120}
        artifacts: [/home/user/output]
        params: {greeting: hello}
        """).strip()
        + "\n"
        + manifest_suffix.strip()
        + "\n"
    )
    (task / "instruction.md").write_text(
        "Write ${greeting} followed by the word in /home/user/input/word.txt "
        "into /home/user/output/result.txt\n"
    )
    if image is not None:
        manifest = task / "task.yaml"
        kind = image["kind"]
        ref = f"\n  ref: {image['ref']}" if "ref" in image else ""
        manifest.write_text(
            manifest.read_text().replace(
                "image: {kind: container}",
                f"image:\n  kind: {kind}{ref}",
            )
        )
    if with_image_dockerfile:
        (task / "image" / "Dockerfile").write_text(
            f"FROM {IMAGE}\n"
            "RUN mkdir -p /home/user/input /home/user/output \\\n"
            " && printf 'world' > /home/user/input/word.txt \\\n"
            " && chown -R user:user /home/user/input /home/user/output\n"
        )
    (task / "verify" / "run.sh").write_text(verify_body or VERIFY_DEFAULT)
    (task / "verify" / "run.sh").chmod(0o755)
    (task / "verify" / "verify.py").write_text(VERIFY_CHECK + "\n")
    for stage in stage_assets:
        assets = task / stage / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "fixture.txt").write_text(stage)
    if with_oracle:
        (task / "oracle").mkdir()
        (task / "oracle" / "run.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "word=$(cat /home/user/input/word.txt)\n"
            "printf 'hello %s' \"$word\" > /home/user/output/result.txt\n"
        )
        (task / "oracle" / "run.sh").chmod(0o755)
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

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from ale.core.taskspec import ImageKind, OperatingSystem
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.tasksets.manifest import load_task_folder
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

LINUX_IMAGE = "ghcr.io/agentslastexam/container-ubuntu22-base:latest"


def _write_task(root: Path, operating_system: OperatingSystem) -> Path:
    windows = operating_system is OperatingSystem.WINDOWS
    task = root / operating_system.value
    for stage in ("setup", "oracle", "verify"):
        (task / stage / "assets").mkdir(parents=True)

    if windows:
        image = {"kind": "vm", "ref": "local/windows-base"}
    elif local_linux_image := os.environ.get("ALE_TEST_LINUX_IMAGE"):
        (task / "image").mkdir()
        (task / "image" / "Dockerfile").write_text(f"FROM {local_linux_image}\n")
        image = {"kind": "container"}
    else:
        image = {"kind": "container", "ref": LINUX_IMAGE}
    home = r"C:\Users\user" if windows else "/home/user"
    memory = 8192 if windows else 1024
    alternate_memory = 9216 if windows else 1536
    manifest = {
        "spec_type": "core/v1",
        "name": f"standard-assets-{operating_system.value}",
        "os": operating_system.value,
        "image": image,
        "resources": {"cpus": 2, "memory_mb": memory},
        "timeouts": {"setup": 300, "agent": 300, "verify": 300},
        "artifacts": [f"{home}/output"],
        "params": {"greeting": "hello"},
        "variants": [
            {
                "name": "alternate",
                "params": {"greeting": "hola"},
                "resources": {"memory_mb": alternate_memory},
                "timeouts": {"agent": 360},
            }
        ],
    }
    (task / "task.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("Write '${greeting} world verified' to the output file.\n")
    (task / "setup" / "assets" / "seed.txt").write_text("world\n")
    (task / "oracle" / "assets" / "suffix.txt").write_text("verified\n")
    (task / "verify" / "assets" / "expected-suffix.txt").write_text("verified\n")

    if windows:
        (task / "setup" / "run.ps1").write_text(
            textwrap.dedent(
                r"""
                $ErrorActionPreference = "Stop"
                $admin = ([Security.Principal.WindowsPrincipal] `
                    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
                        [Security.Principal.WindowsBuiltInRole]::Administrator
                    )
                if ($admin) { exit 18 }
                if (Test-Path "C:\ProgramData\ALE\verify\assets") { exit 17 }
                New-Item "$env:ALE_HOME\input", "$env:ALE_HOME\output" `
                    -ItemType Directory -Force | Out-Null
                Copy-Item ".\assets\seed.txt" "$env:ALE_HOME\input\seed.txt" -Force
                Set-Content "$env:ALE_HOME\input\setup-marker.txt" "setup" -NoNewline
                """
            ).lstrip()
        )
        (task / "oracle" / "run.ps1").write_text(
            textwrap.dedent(
                r"""
                $ErrorActionPreference = "Stop"
                $params = Get-Content $env:ALE_PARAMS_JSON | ConvertFrom-Json
                $seed = (Get-Content ".\assets\suffix.txt").Trim()
                $word = (Get-Content "$env:ALE_HOME\input\seed.txt").Trim()
                Set-Content "$env:ALE_HOME\output\result.txt" `
                    "$($params.greeting) $word $seed" -NoNewline
                """
            ).lstrip()
        )
        entry = "run.ps1"
        (task / "verify" / entry).write_text("python.exe .\\verify.py\nexit $LASTEXITCODE\n")
    else:
        (task / "setup" / "run.sh").write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                test ! -e /opt/ale/verify/assets
                mkdir -p "$ALE_HOME/input" "$ALE_HOME/output"
                cp assets/seed.txt "$ALE_HOME/input/seed.txt"
                printf setup > "$ALE_HOME/input/setup-marker.txt"
                chown -R user:user "$ALE_HOME/input" "$ALE_HOME/output"
                """
            )
        )
        (task / "oracle" / "run.sh").write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                greeting=$(
                    python3 -c 'import json,sys; print(json.load(sys.stdin)["greeting"])' \
                        < "$ALE_PARAMS_JSON"
                )
                word=$(cat "$ALE_HOME/input/seed.txt")
                suffix=$(cat assets/suffix.txt)
                printf '%s %s %s' "$greeting" "$word" "$suffix" > "$ALE_HOME/output/result.txt"
                """
            )
        )
        entry = "run.sh"
        (task / "verify" / entry).write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
        )
        for script in task.glob("*/run.sh"):
            script.chmod(0o755)

    (task / "verify" / "verify.py").write_text(
        textwrap.dedent(
            """\
            import json
            import os
            from pathlib import Path

            from ale_verify import Verification, checks

            home = Path(os.environ["ALE_HOME"])
            params = json.loads(Path(os.environ["ALE_TASK_PARAMETERS_PATH"]).read_text())
            suffix = Path("assets/expected-suffix.txt").read_text().strip()
            verification = Verification()
            verification.check(
                "output",
                checks.text_equals(
                    str(home / "output/result.txt"),
                    f"{params['greeting']} world {suffix}",
                ),
            )
            verification.check(
                "setup_asset",
                checks.text_equals(str(home / "input/seed.txt"), "world\\n"),
            )
            verification.check(
                "setup_ran",
                checks.text_equals(str(home / "input/setup-marker.txt"), "setup"),
            )
            verification.write()
            """
        )
    )
    return task


@pytest.mark.parametrize("variant", ["base", "alternate"])
@pytest.mark.parametrize(
    "operating_system",
    [
        OperatingSystem.LINUX,
        pytest.param(OperatingSystem.WINDOWS, marks=pytest.mark.needs_kvm),
    ],
)
async def test_standard_task_assets_and_variants_across_operating_systems(
    tmp_path: Path,
    operating_system: OperatingSystem,
    variant: str,
) -> None:
    task_root = _write_task(tmp_path / "tasks", operating_system)
    task = load_task_folder(task_root, variant=variant)
    if operating_system is OperatingSystem.WINDOWS:
        image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
        if not image.is_file():
            pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the private Windows qcow2")
        provider = QemuProvider(image=image, overlay_dir=tmp_path / "qemu")
        providers = provider_registry(provider, kind=ImageKind.VM)
    else:
        providers = provider_registry(DockerProvider())

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        providers,
        run_dir=tmp_path / "runs",
    )

    greeting = "hello" if variant == "base" else "hola"
    assert task.spec.params == {"greeting": greeting}
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {
        "output": 1.0,
        "setup_asset": 1.0,
        "setup_ran": 1.0,
    }
    assert (result.run_dir / "artifacts/output/result.txt").read_text() == (
        f"{greeting} world verified"
    )

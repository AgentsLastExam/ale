from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from ale.core.config import LoggingPolicy
from ale.core.taskspec import ImageKind, OperatingSystem
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import Limits
from ale.run.harnesses.builtin import OracleHarness
from ale.run.harnesses.computer_use import ComputerUseHarness
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.secrets import provider_credentials
from ale.run.tasksets.manifest import load_task_folder
from tests.support import provider_registry

from .trajectory import LIVE, llm_audit_evidence

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]
VM_ACCELERATOR = pytest.mark.needs_hvf if sys.platform == "darwin" else pytest.mark.needs_kvm

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


def _write_windows_gui_task(root: Path) -> Path:
    task = _write_task(root, OperatingSystem.WINDOWS)
    manifest = yaml.safe_load((task / "task.yaml").read_text())
    manifest.pop("params")
    manifest.pop("variants")
    (task / "task.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (task / "instruction.md").write_text(
        "Use the visible Notepad window. Read the value after SCREEN_CODE=, type the "
        "same value after ANSWER=, and save the file. Do not change the first line.\n"
    )
    expected = "SCREEN_CODE=ALE-5827\r\nANSWER=ALE-5827\r\n"
    (task / "setup" / "assets" / "screen.txt").write_text("SCREEN_CODE=ALE-5827\r\nANSWER=\r\n")
    (task / "oracle" / "assets" / "answer.txt").write_text(expected)
    (task / "verify" / "assets" / "answer.txt").write_text(expected)
    (task / "setup" / "run.ps1").write_text(
        textwrap.dedent(
            r"""
            $ErrorActionPreference = "Stop"
            if ((Get-Culture).Name -ne "en-US") { throw "culture is not en-US" }
            if ((Get-WinSystemLocale).Name -ne "en-US") { throw "system locale is not en-US" }
            if ((Get-TimeZone).Id -ne "UTC") { throw "time zone is not UTC" }
            if (Test-Path "$env:SystemDrive\hiberfil.sys") { throw "hibernation is enabled" }
            $updatePolicy = Get-ItemProperty `
                "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
            if ($updatePolicy.NoAutoUpdate -ne 1) { throw "automatic updates are enabled" }
            $connectionPolicy = Get-ItemProperty `
                "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
            if ($connectionPolicy.DoNotConnectToWindowsUpdateInternetLocations -ne 1) {
                throw "Windows Update network access is enabled"
            }
            foreach ($path in @(
                "$env:ProgramFiles\7-Zip", "$env:ProgramFiles\Git",
                "$env:ProgramFiles\LibreOffice", "$env:USERPROFILE\.conda",
                "$env:USERPROFILE\.ssh"
            )) {
                if (Test-Path -LiteralPath $path) { throw "unexpected base content: $path" }
            }
            New-Item "$env:ALE_HOME\output" -ItemType Directory -Force | Out-Null
            $document = "$env:ALE_HOME\output\result.txt"
            Copy-Item ".\assets\screen.txt" $document -Force
            Start-Process notepad.exe -ArgumentList "`"$document`""
            Start-Sleep -Seconds 3
            """
        ).lstrip()
    )
    (task / "oracle" / "run.ps1").write_text(
        'Copy-Item ".\\assets\\answer.txt" "$env:ALE_HOME\\output\\result.txt" -Force\n'
    )
    (task / "verify" / "verify.py").write_text(
        textwrap.dedent(
            """\
            import os
            from pathlib import Path

            from ale_verify import Verification, checks

            home = Path(os.environ["ALE_HOME"])
            expected = Path("assets/answer.txt").read_text()
            verification = Verification()
            verification.check(
                "desktop_answer",
                checks.text_equals(str(home / "output/result.txt"), expected),
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
        pytest.param(OperatingSystem.WINDOWS, marks=VM_ACCELERATOR),
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


@VM_ACCELERATOR
@pytest.mark.needs_gui
@pytest.mark.needs_llm
@LIVE
async def test_real_llm_reads_and_edits_the_windows_desktop(tmp_path: Path) -> None:
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the private Windows qcow2")

    task = load_task_folder(_write_windows_gui_task(tmp_path / "tasks"))
    model = os.environ.get("ALE_LIVE_WINDOWS_MODEL", "claude-opus-4-8")
    api_key, upstream = provider_credentials(
        os.environ.get("ALE_LIVE_ANTHROPIC_API_KEY_ENV", ""),
        os.environ.get("ALE_LIVE_ANTHROPIC_BASE_URL", ""),
    )
    gateway = Gateway(api_key=api_key, upstream=upstream, dialect="anthropic", host="0.0.0.0")
    await gateway.start()
    try:
        result = await run_episode(
            task,
            StandardEnvironment(
                ComputerUseHarness(model=model),
                max_steps=20,
                stall_limit=6,
            ),
            provider_registry(
                QemuProvider(image=image, overlay_dir=tmp_path / "qemu"),
                kind=ImageKind.VM,
            ),
            run_dir=tmp_path / "runs",
            gateway_url=gateway.base_url,
            gateway=gateway,
            model=model,
            limits=Limits(max_model_calls=20),
            logging_policy=LoggingPolicy(transport_payloads="debug"),
        )
    finally:
        await gateway.stop()

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"desktop_answer": 1.0}
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    transport = [
        json.loads(line)
        for line in (result.run_dir / "trace.transport.jsonl").read_text().splitlines()
    ]
    calls = [call for step in trajectory["steps"] for call in step.get("tool_calls") or ()]
    actions = [call["extra"]["ale"]["normalized_action"] for call in calls]
    assert any(action["type"] == "screenshot" for action in actions)
    assert any(action["type"] in {"type", "key"} for action in actions)
    screenshots = [
        content
        for step in trajectory["steps"]
        for result_item in (step.get("observation") or {}).get("results", ())
        for content in (
            result_item.get("content") if isinstance(result_item.get("content"), list) else ()
        )
        if content.get("type") == "image"
    ]
    assert screenshots
    assert all((result.run_dir / item["source"]["path"]).is_file() for item in screenshots)
    assert any(
        record.get("kind") == "call" and record.get("model") == model for record in transport
    )
    llm_audit_evidence(
        {
            "trajectory": trajectory,
            "result": json.loads((result.run_dir / "result.json").read_text()),
            "transport": transport,
        },
        requirement=(
            "The real computer-use agent inspected a Windows screenshot, copied the "
            "screen-only code with desktop input, saved it, and earned the verifier reward."
        ),
    )

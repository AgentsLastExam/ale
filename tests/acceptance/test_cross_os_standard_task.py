from __future__ import annotations

import base64
import json
import os
import struct
import textwrap
from pathlib import Path

import pytest

from ale.core.config import LoggingPolicy
from ale.core.sandbox import Identity, SandboxRequest
from ale.core.taskspec import ImageKind, NetworkPolicy, OperatingSystem, Resources
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import Limits
from ale.run.harnesses._npm import agent_home
from ale.run.harnesses.builtin import OracleHarness
from ale.run.harnesses.computer_use import ComputerUseHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.secrets import provider_credentials
from ale.run.task_images import prepare_task_image_result
from ale.run.tasksets.manifest import load_task_folder
from ale.run.tools import resolved_cua_desktop, stage_cua_desktop
from tests.support import (
    CONTAINER_BASE,
    edit_task_config,
    merged_config,
    provider_registry,
    write_task,
)

from .trajectory import LIVE, MODELS, llm_audit_evidence

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

LINUX_IMAGE = CONTAINER_BASE


@pytest.mark.needs_kvm
@pytest.mark.needs_gui
async def test_windows_builtin_desktop_mcp_over_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the private Windows qcow2")
    monkeypatch.setattr("ale.run.task_images.cache_root", lambda: tmp_path / "cache")
    provider = QemuProvider(image=image, overlay_dir=tmp_path / "qemu")
    # The base carries no Cua Driver; building a Task image on it installs one.
    task = load_task_folder(_write_windows_gui_task(tmp_path / "tasks"))
    prepared = (
        await prepare_task_image_result(task, provider_registry(provider, kind=ImageKind.VM))
    ).image
    sandbox = await provider.create(
        SandboxRequest(
            episode_id="windows-desktop-mcp",
            os=OperatingSystem.WINDOWS,
            prepared_image=prepared,
            resources=Resources(cpus=2, memory_mb=8192),
            network=NetworkPolicy(),
        )
    )
    try:
        home = await agent_home(sandbox)
        await stage_cua_desktop(sandbox, home)
        server = resolved_cua_desktop(OperatingSystem.WINDOWS).server
        argv = [value.replace("{home}", home) for value in (server.command, *server.args)]
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ale-acceptance", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "screenshot", "arguments": {}},
            },
        ]
        result = await sandbox.exec(
            [
                "python.exe",
                "-c",
                "import json, subprocess, sys; "
                "result = subprocess.run(json.loads(sys.argv[1]), input=sys.argv[2], "
                "capture_output=True, text=True, timeout=30); "
                "sys.stdout.write(result.stdout); sys.stderr.write(result.stderr); "
                "sys.exit(result.returncode)",
                json.dumps(argv),
                "".join(json.dumps(request) + "\n" for request in requests),
            ],
            identity=Identity.AGENT,
            timeout_sec=60,
        )
        assert result.ok, result.stderr or result.stdout
        responses = {
            reply["id"]: reply for line in result.stdout.splitlines() if (reply := json.loads(line))
        }
        assert responses[1]["result"]["serverInfo"]["name"] == "cua-desktop"
        tools = {tool["name"] for tool in responses[2]["result"]["tools"]}
        assert {"screenshot", "click", "type", "key", "mouse_move"} <= tools
        screenshot = responses[3]["result"]
        assert not screenshot.get("isError"), screenshot
        content = next(item for item in screenshot["content"] if item["type"] == "image")
        assert content["mimeType"] == "image/png"
        png = base64.b64decode(content["data"], validate=True)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", png[16:24])
        assert width >= 640 and height >= 480
        (tmp_path / "desktop.png").write_bytes(png)
    finally:
        await sandbox.destroy()


@pytest.mark.needs_kvm
async def test_windows_task_image_installs_software_and_reuses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the private Windows qcow2")
    monkeypatch.setattr("ale.run.task_images.cache_root", lambda: tmp_path / "cache")
    folder = _write_task(tmp_path / "tasks", OperatingSystem.WINDOWS)
    edit_task_config(folder, {"environment": {"vm_image": None, "vm_base": str(image)}})
    (folder / "environment/assets").mkdir(parents=True)
    (folder / "environment/assets/proof.txt").write_text("built from image materials")
    (folder / "environment/build.ps1").write_text(
        textwrap.dedent(r"""
        $ErrorActionPreference = 'Stop'
        choco install 7zip --version=24.9.0 -y --no-progress --limit-output
        if ($LASTEXITCODE -notin 0, 3010) { exit $LASTEXITCODE }
        Copy-Item .\assets\proof.txt "$env:ALE_HOME\image-proof.txt"
        exit 0
    """).lstrip()
    )
    task = load_task_folder(folder)
    providers = provider_registry(QemuProvider(image=image), kind=ImageKind.VM)
    built = await prepare_task_image_result(task, providers)
    cached = await prepare_task_image_result(task, providers)
    assert built.image == cached.image
    assert built.steps[2].outcome == "executed"
    assert cached.steps[2].outcome == "reused"
    sandbox = await QemuProvider(overlay_dir=tmp_path / "restarted").create(
        SandboxRequest(
            episode_id="windows-image-check",
            os=OperatingSystem.WINDOWS,
            prepared_image=built.image,
            resources=task.spec.resources,
            network=task.spec.network,
        )
    )
    try:
        result = await sandbox.exec(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$ErrorActionPreference = 'Stop'; "
                "if (Test-Path 'C:\\ProgramData\\ALE\\environment') { exit 11 }; "
                "if ((Get-Content ($env:USERPROFILE + '\\image-proof.txt')) "
                "-ne 'built from image materials') { exit 12 }; "
                "& 'C:\\Program Files\\7-Zip\\7z.exe' i; exit $LASTEXITCODE",
            ]
        )
        assert result.ok, result.stderr or result.stdout
    finally:
        await sandbox.destroy()


def _write_task(root: Path, operating_system: OperatingSystem) -> Path:
    windows = operating_system is OperatingSystem.WINDOWS
    local_linux_image = os.environ.get("ALE_TEST_LINUX_IMAGE")
    if windows:
        image: dict[str, object] = {"environment": {"vm_image": "local/windows-base"}}
    elif local_linux_image:
        image = {}
    else:
        image = {"environment": {"docker_image": LINUX_IMAGE}}
    home = r"C:\Users\user" if windows else "/home/user"
    task = write_task(
        root / operating_system.value,
        name=f"agentslastexam/standard-assets-{operating_system.value}",
        config=merged_config(
            {
                "artifacts": [f"{home}/output"],
                "environment": {
                    "os": operating_system.value,
                    "cpus": 2,
                    "memory_mb": 8192 if windows else 1024,
                },
                "agent": {"timeout_sec": 300},
                "verifier": {"timeout_sec": 300},
            },
            image,
        ),
        dockerfile=f"FROM {local_linux_image}\n" if local_linux_image else None,
        instruction="Write 'hello world verified' to the output file.\n",
        test=None,
        solve=None,
    )
    for root_name in ("environment/setup", "solution/assets", "tests/assets"):
        (task / root_name).mkdir(parents=True)
    (task / "environment" / "setup" / "seed.txt").write_text("world\n")
    (task / "solution" / "assets" / "suffix.txt").write_text("verified\n")
    (task / "tests" / "assets" / "expected-suffix.txt").write_text("verified\n")

    if windows:
        (task / "environment" / "setup" / "setup.ps1").write_text(
            textwrap.dedent(
                r"""
                $ErrorActionPreference = "Stop"
                $admin = ([Security.Principal.WindowsPrincipal] `
                    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
                        [Security.Principal.WindowsBuiltInRole]::Administrator
                    )
                if (-not $admin) { exit 18 }
                if (Test-Path "C:\ProgramData\ALE\tests\assets") { exit 17 }
                New-Item "$env:ALE_HOME\input", "$env:ALE_HOME\output" `
                    -ItemType Directory -Force | Out-Null
                Copy-Item ".\seed.txt" "$env:ALE_HOME\input\seed.txt" -Force
                Set-Content "$env:ALE_HOME\input\setup-marker.txt" "setup" -NoNewline
                """
            ).lstrip()
        )
        (task / "solution" / "solve.ps1").write_text(
            textwrap.dedent(
                r"""
                $ErrorActionPreference = "Stop"
                if (([Security.Principal.WindowsPrincipal] `
                    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
                        [Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 18 }
                $seed = (Get-Content ".\assets\suffix.txt").Trim()
                $word = (Get-Content "$env:ALE_HOME\input\seed.txt").Trim()
                Set-Content "$env:ALE_HOME\output\result.txt" "hello $word $seed" -NoNewline
                """
            ).lstrip()
        )
        (task / "tests" / "test.ps1").write_text("python.exe .\\verify.py\nexit $LASTEXITCODE\n")
    else:
        (task / "environment" / "setup" / "setup.sh").write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                test ! -e /opt/ale/tests/assets
                mkdir -p "$ALE_HOME/input" "$ALE_HOME/output"
                cp seed.txt "$ALE_HOME/input/seed.txt"
                printf setup > "$ALE_HOME/input/setup-marker.txt"
                chown -R user:user "$ALE_HOME/input" "$ALE_HOME/output"
                """
            )
        )
        (task / "solution" / "solve.sh").write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                word=$(cat "$ALE_HOME/input/seed.txt")
                suffix=$(cat assets/suffix.txt)
                printf 'hello %s %s' "$word" "$suffix" > "$ALE_HOME/output/result.txt"
                """
            )
        )
        (task / "tests" / "test.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
        )

    (task / "tests" / "verify.py").write_text(
        textwrap.dedent(
            "import os\n"
            "from pathlib import Path\n"
            "from ale_verify import checks, write\n"
            "home = Path(os.environ['ALE_HOME'])\n"
            "suffix = Path('assets/expected-suffix.txt').read_text().strip()\n"
            "verification = {}\n"
            "verification['output'] = checks.text_equals(str(home / 'output/re"
            "sult.txt'), f'hello world {suffix}')['score']\n"
            "verification['setup_asset'] = checks.text_equals(str(home / 'inpu"
            "t/seed.txt'), 'world\\n')['score']\n"
            "verification['setup_ran'] = checks.text_equals(str(home / 'input/"
            "setup-marker.txt'), 'setup')['score']\n"
            "verification['reward'] = sum(verification.values()) / len(verification.values())\n"
            "write(verification)\n"
        )
    )
    return task


def _write_windows_gui_task(root: Path) -> Path:
    task = _write_task(root, OperatingSystem.WINDOWS)
    # Built on the base, so the image gets the framework's Cua Driver.
    edit_task_config(task, {"environment": {"vm_image": None, "vm_base": "local/windows-base"}})
    (task / "environment" / "build.ps1").write_text("exit 0\n")
    (task / "instruction.md").write_text(
        "Use the visible Notepad window. Read the value after SCREEN_CODE=, type the "
        "same value after ANSWER=, and save the file. Do not change the first line.\n"
    )
    expected = "SCREEN_CODE=ALE-5827\r\nANSWER=ALE-5827\r\n"
    (task / "environment" / "setup" / "screen.txt").write_text(
        "SCREEN_CODE=ALE-5827\r\nANSWER=\r\n"
    )
    (task / "solution" / "assets" / "answer.txt").write_text(expected)
    (task / "tests" / "assets" / "answer.txt").write_text(expected)
    (task / "environment" / "setup" / "setup.ps1").write_text(
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
                "$env:ProgramFiles\7-Zip",
                "$env:ProgramFiles\LibreOffice", "$env:USERPROFILE\.conda",
                "$env:USERPROFILE\.ssh"
            )) {
                if (Test-Path -LiteralPath $path) { throw "unexpected base content: $path" }
            }
            New-Item "$env:ALE_HOME\output" -ItemType Directory -Force | Out-Null
            $document = "$env:ALE_HOME\output\result.txt"
            Copy-Item ".\screen.txt" $document -Force
            Start-Process notepad.exe -ArgumentList "`"$document`""
            Start-Sleep -Seconds 3
            """
        ).lstrip()
    )
    (task / "solution" / "solve.ps1").write_text(
        'Copy-Item ".\\assets\\answer.txt" "$env:ALE_HOME\\output\\result.txt" -Force\n'
    )
    (task / "tests" / "verify.py").write_text(
        textwrap.dedent(
            "import os\n"
            "from pathlib import Path\n"
            "from ale_verify import checks, write\n"
            "home = Path(os.environ['ALE_HOME'])\n"
            "expected = Path('assets/answer.txt').read_text()\n"
            "verification = {}\n"
            "verification['desktop_answer'] = checks.text_equals(str(home / 'o"
            "utput/result.txt'), expected)['score']\n"
            "verification['reward'] = sum(verification.values()) / len(verification.values())\n"
            "write(verification)\n"
        )
    )
    return task


@pytest.mark.parametrize(
    "operating_system",
    [
        OperatingSystem.LINUX,
        pytest.param(OperatingSystem.WINDOWS, marks=pytest.mark.needs_kvm),
    ],
)
async def test_standard_task_assets_across_operating_systems(
    tmp_path: Path,
    operating_system: OperatingSystem,
) -> None:
    task_root = _write_task(tmp_path / "tasks", operating_system)
    task = load_task_folder(task_root)
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
        EpisodeProtocol(OracleHarness()),
        providers,
        run_dir=tmp_path / "runs",
        collect_artifacts=True,
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert (result.run_dir / "artifacts/0000/output/result.txt").read_text() == (
        "hello world verified"
    )


@pytest.mark.needs_kvm
@pytest.mark.needs_gui
@pytest.mark.needs_llm
@LIVE
async def test_real_llm_reads_and_edits_the_windows_desktop(tmp_path: Path) -> None:
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the private Windows qcow2")

    task = load_task_folder(_write_windows_gui_task(tmp_path / "tasks"))
    model = MODELS["anthropic"]
    api_key, upstream = provider_credentials("anthropic")
    gateway = Gateway(api_key=api_key, upstream=upstream, dialect="anthropic", host="0.0.0.0")
    await gateway.start()
    try:
        result = await run_episode(
            task,
            EpisodeProtocol(
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
    assert result.verdict.rewards == {"reward": 1.0}
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

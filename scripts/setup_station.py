#!/usr/bin/env python3
r"""Enroll a Windows Server machine as an ALE Station, from the Host, over SSH.

Run with uv; this script uses only Python's standard library:

    uv run scripts/setup_station.py <admin@station> <station.toml>

The destination is an administrator's SSH login on the Station (any ``ssh`` destination,
so the Host's own SSH configuration may supply the key and port). Setup uploads
``ale-stationd`` and a PowerShell script to that account's home, runs the script elevated
and deletes the uploads. Run again, it changes only what differs, so it is also how an
operator updates ``ale-stationd`` and the manifest, adds Lanes, captures the baseline of a
Lane that has signed in since, and returns quarantined Lanes to service. The script:

* installs Remote Desktop Session Host, asking for a restart the first time, and sets the
  policies Lanes need: no clipboard, drive, printer or device redirection, the hardware GPU
  for every session, and sessions that never time out;
* installs what the agents expect on Windows, as the Windows base image does: an all-users
  Python on the PATH, which only Administrators may change, Node.js with npm, and Git with
  Git Bash;
* creates the Lane accounts ``laneA``, ``laneB``, ... with the configured password, in the
  ``ALE Lanes`` group and in Remote Desktop Users;
* installs ``ale-stationd`` on its own Python under ``C:\ProgramData\ALE\station``, as the
  service ``ALEStation``, and creates the ``ale-host`` account whose SSH keys may run only
  ``ale-stationd``'s client;
* has ``ale-stationd`` lay out each Lane's stage directory, apply the isolation recipe and
  capture each signed-in Lane's profile baseline, and starts the service.

``station.toml``:

    lanes = 6                       # laneA ... laneF
    resolution = "1600x900"         # the size the keep-alive box connects each Lane at
    host_keys = ["ssh-ed25519 AAAA... ale@host"]
    lane_password_file = "~/.config/ale/station-lanes.password"

    [software.materials-studio]     # may name a probe, run before each lease that needs it
    probe = 'tasklist /fi "imagename eq lmgrd.exe" | find "lmgrd.exe"'
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path

#: The Station's own Python, the framework's python-build-standalone build for Windows.
PYTHON_RELEASE = (
    "cpython-3.12.15+20261013-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
    "7a2aef48e5ee569e310b29b32681debbc5d888e92790d85331a47736c6b06aad",
)
PYTHON_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20261013/"
STATIOND = (
    Path(__file__).resolve().parents[1]
    / "packages/ale-run/src/ale/run/providers/station/stationd.py"
)
#: What the PowerShell script exits with when Windows must restart before setup goes on.
RESTART = 3
_KEY = re.compile(
    r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|"
    r"sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^\r\n]*)?"
)
_SOFTWARE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RESOLUTION = re.compile(r"[1-9]\d{2,4}x[1-9]\d{2,4}")
_SSHD_RESTARTING = "ALE: restarting sshd"


class SetupError(Exception):
    """A configuration or enrollment problem the operator can act on."""


def lane_names(count: int) -> list[str]:
    """The accounts of ``count`` Lanes: ``laneA`` ... ``laneZ``, ``laneAA`` ..."""
    names = []
    for index in range(count):
        suffix = ""
        while True:
            suffix = chr(65 + index % 26) + suffix
            index = index // 26 - 1
            if index < 0:
                break
        names.append(f"lane{suffix}")
    return names


def read_config(path: Path) -> tuple[dict[str, object], list[str], str]:
    """``station.json`` for ``ale-stationd``, the Host's keys and the Lanes' password."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SetupError(f"cannot read {path}: {error}") from error
    lanes, resolution = data.get("lanes"), data.get("resolution")
    keys, password_file = data.get("host_keys"), data.get("lane_password_file")
    software = data.get("software", {})
    if not isinstance(lanes, int) or not 1 <= lanes <= 254:
        raise SetupError("lanes must be a number of Lanes from 1 to 254")
    if not isinstance(resolution, str) or not _RESOLUTION.fullmatch(resolution):
        raise SetupError('resolution must be the keep-alive size, such as "1600x900"')
    if not isinstance(keys, list) or not keys:
        raise SetupError("host_keys must list the public keys the Host signs in with")
    if bad := [key for key in keys if not isinstance(key, str) or not _KEY.fullmatch(key)]:
        raise SetupError(f"not an OpenSSH public key without options: {str(bad[0])[:40]!r}")
    if not isinstance(software, dict):
        raise SetupError("software must be a table of software ids")
    manifest: dict[str, dict[str, str]] = {}
    for name, entry in software.items():
        if not _SOFTWARE.fullmatch(name) or not isinstance(entry, dict):
            raise SetupError(f"bad software id {name!r}: lowercase letters, digits, . _ -")
        if set(entry) - {"probe"} or not isinstance(entry.get("probe", ""), str):
            raise SetupError(f"software {name!r} may name only a probe command")
        manifest[name] = {"probe": entry["probe"]} if entry.get("probe") else {}
    if not isinstance(password_file, str):
        raise SetupError("lane_password_file must name the file with the Lanes' password")
    try:
        password = Path(password_file).expanduser().read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SetupError(f"cannot read the Lanes' password: {error}") from error
    if not password or "\n" in password:
        raise SetupError("the Lanes' password file must hold one line")
    station = {"lanes": lane_names(lanes), "resolution": resolution, "software": manifest}
    return station, keys, password


def enroll(destination: str, station: dict[str, object], keys: list[str], password: str) -> int:
    asset, sha256 = PYTHON_RELEASE
    settings = {
        "python_asset": asset,
        "python_url": PYTHON_URL + urllib.parse.quote(asset),
        "python_sha256": sha256,
    }
    with tempfile.TemporaryDirectory(prefix="ale-station-") as directory:
        uploads = Path(directory)
        files = {
            "ale-station-enroll.ps1": ENROLL,
            "ale-station.json": json.dumps(station, indent=1),
            "ale-station-setup.json": json.dumps(settings, indent=1),
            "ale-station-keys": "\n".join(keys) + "\n",
            "ale-stationd.py": STATIOND.read_text(encoding="utf-8"),
        }
        for name, text in files.items():
            (uploads / name).write_bytes(text.encode("utf-8"))
        # Legacy scp: the Station's sshd need not offer SFTP.
        copied = subprocess.run(
            ["scp", "-O", "-q", *(str(uploads / name) for name in files), f"{destination}:"],
            check=False,
        )
        if copied.returncode != 0:
            raise SetupError(f"could not copy the setup files to {destination}")
    command = (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ale-station-enroll.ps1"
    )
    process = subprocess.Popen(
        ["ssh", "-T", destination, command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdin is not None and process.stdout is not None
    # The password goes over the SSH session, never into a file or a command line.
    process.stdin.write(password + "\n")
    process.stdin.close()
    restarting = False
    for line in process.stdout:
        sys.stdout.write(line)
        restarting |= line.startswith(_SSHD_RESTARTING)
    code = process.wait()
    # Restarting sshd may end this very session; the script had finished by then.
    return 0 if restarting and code == 255 else code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("destination", help="an administrator's SSH destination on the Station")
    parser.add_argument("config", type=Path, help="the Station's station.toml")
    args = parser.parse_args(argv)
    try:
        station, keys, password = read_config(args.config)
        code = enroll(args.destination, station, keys, password)
    except SetupError as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1
    if code == RESTART:
        print("Restart the Station, then run setup again.")
    elif code != 0:
        print(f"Setup failed on the Station (exit {code}); see above.", file=sys.stderr)
    return code


#: Runs on the Station as the administrator, from that account's home, where setup put it.
ENROLL = r"""
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$password = [Console]::In.ReadLine()
$here = $PSScriptRoot
$station_config = Get-Content -Raw "$here\ale-station.json" | ConvertFrom-Json
$settings = Get-Content -Raw "$here\ale-station-setup.json" | ConvertFrom-Json
$root = "$env:ProgramData\ALE"
$station = "$root\station"
$python = "$station\python\python.exe"
$failed = $false

function Step($text) { Write-Output "ALE: $text" }

# Through ADSI: CIM's group associations took 23 s per call on a Station, and
# Get-LocalGroupMember fails on a group that holds a SID no longer resolvable.
function Members($sid) {
    $name = ([Security.Principal.SecurityIdentifier]$sid).Translate(
        [Security.Principal.NTAccount]).Value.Split("\")[-1]
    @(([ADSI]"WinNT://./$name,group").psbase.Invoke("Members") |
        ForEach-Object { $_.GetType().InvokeMember("Name", "GetProperty", $null, $_, $null) })
}

function Add-Member($sid, $user) {
    if ((Members $sid) -notcontains $user) { Add-LocalGroupMember -SID $sid -Member $user }
}

try {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "the SSH destination must be an administrator's"
    }
    if ((Get-CimInstance Win32_OperatingSystem).ProductType -eq 1) {
        throw "a Station runs Windows Server: other Windows serves one remote session at a time"
    }
    $sshd = Get-CimInstance Win32_Service -Filter "Name='sshd'"
    if (-not $sshd) { throw "OpenSSH Server is not installed" }
    $sshdProgram = $sshd.PathName.Trim('"')
    # Setup stops the service, which would end every lease: it waits for none to be held.
    $service = Get-Service ALEStation -ErrorAction SilentlyContinue
    if ($service -and $service.Status -eq "Running") {
        if (Test-Path "$station\state.json") {
            $lanes = (Get-Content -Raw "$station\state.json" | ConvertFrom-Json).lanes
            $leased = @($lanes.PSObject.Properties |
                Where-Object { $_.Value.state -eq "leased" } | ForEach-Object Name)
            if ($leased) { throw "$($leased -join ', ') leased; run setup when no Lane is" }
        }
        Stop-Service ALEStation
    }

    Step "Remote Desktop Session Host"
    if (-not (Get-WindowsFeature RDS-RD-Server).Installed) {
        $installed = Install-WindowsFeature RDS-RD-Server
        if ([string]$installed.RestartNeeded -eq "Yes") {
            Step "Windows must restart to finish installing Remote Desktop Session Host"
            exit 3
        }
    }
    # Lanes share nothing through their sessions: no clipboard (measured to leak between
    # sessions), drives, printers or devices; each session draws on the GPU and none ends.
    $policies = "HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services"
    if (-not (Test-Path $policies)) { New-Item -Path $policies -Force | Out-Null }
    foreach ($policy in ([ordered]@{
        fDisableClip = 1; fDisableCdm = 1; fDisableCpm = 1; fDisableLPT = 1; fDisableCcm = 1
        fDisablePNPRedir = 1; fDisableAudioCapture = 1
        bEnumerateHWBeforeSW = 1; AVC444ModePreferred = 1; AVCHardwareEncodePreferred = 1
        fEnableWddmDriver = 1
        MaxIdleTime = 0; MaxDisconnectionTime = 0; MaxConnectionTime = 0; fResetBroken = 0
        fSingleSessionPerUser = 1
    }).GetEnumerator()) {
        New-ItemProperty -Force -Path $policies -Name $policy.Key -PropertyType DWord `
            -Value $policy.Value | Out-Null
    }

    # The agent runtimes of images/base/vm-windows10 (prepare.ps1 and tools.ps1).
    Step "agent runtimes"
    $work = "$env:ProgramData\ALE-setup"
    New-Item -ItemType Directory -Force $work | Out-Null
    if (-not (Test-Path "C:\Python312\python.exe")) {
        $installer = "$work\python-3.12.10-amd64.exe"
        Invoke-WebRequest -UseBasicParsing `
            "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $installer
        if ((Get-AuthenticodeSignature $installer).Status -ne "Valid") {
            throw "the Python installer's signature is not valid"
        }
        $process = Start-Process $installer -Wait -PassThru -ArgumentList @(
            "/quiet", "InstallAllUsers=1", "TargetDir=C:\Python312", "PrependPath=1",
            "Include_test=0", "Include_doc=0", "Include_dev=0", "Include_tcltk=0",
            "Include_launcher=1")
        if ($process.ExitCode -ne 0) { throw "the Python installer exited $($process.ExitCode)" }
    }
    # A folder made in the root of C:\ lets Users add files below it, and the PATH begins
    # with this one: no Lane may put a program there that an Administrator or SYSTEM runs.
    icacls C:\Python312 /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" `
        "*S-1-5-32-545:(OI)(CI)RX" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "C:\Python312 could not be closed to Lanes" }
    $choco = "$env:ProgramData\chocolatey\bin\choco.exe"
    if (-not (Test-Path $choco)) {
        $archive = "$work\chocolatey.zip"
        Invoke-WebRequest -UseBasicParsing `
            "https://community.chocolatey.org/api/v2/package/chocolatey/2.4.3" -OutFile $archive
        if ((Get-FileHash $archive -Algorithm SHA256).Hash -ne `
            "d4998ca928a85a484507dcaa39c30948a6516de0d1469b0511931d44a53456c3") {
            throw "the Chocolatey package checksum does not match"
        }
        Expand-Archive $archive "$work\chocolatey" -Force
        & powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -File "$work\chocolatey\tools\chocolateyInstall.ps1"
        if ($LASTEXITCODE -ne 0) { throw "the Chocolatey bootstrap exited $LASTEXITCODE" }
    }
    foreach ($package in @(@("nodejs-lts", "24.15.0"), @("git.install", "2.53.0"))) {
        & $choco install $package[0] "--version=$($package[1])" -y --no-progress `
            --limit-output --fail-on-unfound
        if ($LASTEXITCODE -notin 0, 3010) { throw "$($package[0]) exited $LASTEXITCODE" }
    }
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    foreach ($directory in "$env:ProgramFiles\Git\bin", "$env:ProgramFiles\Git\usr\bin") {
        if ($directory -notin $machinePath.Split(";")) { $machinePath += ";$directory" }
    }
    [Environment]::SetEnvironmentVariable("Path", $machinePath, "Machine")
    Remove-Item $work -Recurse -Force
    $env:Path = $machinePath
    foreach ($tool in ([ordered]@{
        "python.exe" = ""; "node.exe" = "v24.15.0"; "npm.cmd" = ""
        "git.exe" = "git version 2.53.0.windows.1"
    }).GetEnumerator()) {
        $version = ((& $tool.Key --version) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "$($tool.Key) is not usable" }
        if ($tool.Value -and $version -ne $tool.Value) {
            throw "$($tool.Key) is $version; expected $($tool.Value)"
        }
    }
    & "$env:ProgramFiles\Git\bin\bash.exe" -c "command -v node git python npm >/dev/null"
    if ($LASTEXITCODE -ne 0) { throw "Git Bash cannot run the agents' tools" }

    Step "ale-stationd"
    New-Item -ItemType Directory -Force $station | Out-Null
    icacls $root /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q |
        Out-Null
    $release = Get-Content "$station\python.release" -ErrorAction SilentlyContinue
    if (-not (Test-Path $python) -or $release -ne $settings.python_asset) {
        $archive = "$station\python.tar.gz"
        Invoke-WebRequest -UseBasicParsing $settings.python_url -OutFile $archive
        if ((Get-FileHash $archive -Algorithm SHA256).Hash -ne $settings.python_sha256) {
            throw "the Station's Python does not match its checksum"
        }
        if (Test-Path "$station\python") { Remove-Item "$station\python" -Recurse -Force }
        tar.exe -xzf $archive -C $station
        if ($LASTEXITCODE -ne 0) { throw "the Station's Python could not be unpacked" }
        Remove-Item $archive
        Set-Content "$station\python.release" $settings.python_asset
    }
    Copy-Item "$here\ale-stationd.py" "$station\stationd.py" -Force
    Copy-Item "$here\ale-station.json" "$station\station.json" -Force

    # The Host's account: not an administrator, no password anyone knows, keys only.
    if (-not (Get-LocalUser ale-host -ErrorAction SilentlyContinue)) {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $secret = ConvertTo-SecureString ([Convert]::ToBase64String($bytes) + "aA1!") `
            -AsPlainText -Force
        New-LocalUser ale-host -Password $secret -PasswordNeverExpires `
            -UserMayNotChangePassword -Description "ALE Host: ale-stationd's client only" |
            Out-Null
    }
    if ((Members "S-1-5-32-544") -contains "ale-host") { throw "ale-host is an administrator" }
    icacls "$station\python" /grant "ale-host:(OI)(CI)(RX)" /Q | Out-Null
    icacls "$station\stationd.py" /grant "ale-host:(RX)" /Q | Out-Null
    $client = "$python -I -S $station\stationd.py client"
    $keys = Get-Content "$here\ale-station-keys" | Where-Object { $_ } |
        ForEach-Object { "restrict,command=`"$client`" $_" }
    Set-Content "$station\authorized_keys" $keys -Encoding ascii
    icacls "$station\authorized_keys" /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" /Q |
        Out-Null
    $sshdConfig = "$env:ProgramData\ssh\sshd_config"
    $block = @(
        "# BEGIN ALE Station: ale-host runs ale-stationd's client and nothing else.",
        "Match User ale-host",
        "    AuthorizedKeysFile __PROGRAMDATA__/ALE/station/authorized_keys",
        "    ForceCommand $client",
        "    PasswordAuthentication no",
        "    PermitTTY no",
        "    AllowTcpForwarding no",
        "    AllowAgentForwarding no",
        "    X11Forwarding no",
        "    PermitTunnel no",
        "    GatewayPorts no",
        "# END ALE Station") -join "`r`n"
    $text = Get-Content -Raw $sshdConfig
    $kept = [regex]::Replace($text, "(?ms)^# BEGIN ALE Station.*?^# END ALE Station\r?\n?", "")
    $changed = $kept.TrimEnd() + "`r`n`r`n" + $block + "`r`n"
    $restartSshd = $changed -cne $text
    if ($restartSshd) {
        Copy-Item $sshdConfig "$sshdConfig.ale-backup" -Force
        Set-Content $sshdConfig $changed -NoNewline -Encoding ascii
        & $sshdProgram -t
        if ($LASTEXITCODE -ne 0) {
            Copy-Item "$sshdConfig.ale-backup" $sshdConfig -Force
            throw "sshd refused the new configuration, which is undone"
        }
    }

    Step "Lanes"
    $secure = ConvertTo-SecureString $password -AsPlainText -Force
    if (-not (Get-LocalGroup -Name 'ALE Lanes' -ErrorAction SilentlyContinue)) {
        New-LocalGroup -Name 'ALE Lanes' -Description "Lane accounts of ale-stationd" | Out-Null
    }
    $administrators = Members "S-1-5-32-544"
    foreach ($lane in $station_config.lanes) {
        if ($administrators -contains $lane) { throw "$lane is an administrator" }
        if (Get-LocalUser $lane -ErrorAction SilentlyContinue) {
            Set-LocalUser $lane -Password $secure
        } else {
            New-LocalUser $lane -Password $secure -PasswordNeverExpires `
                -UserMayNotChangePassword -Description "ALE Lane" | Out-Null
        }
        Add-Member "S-1-5-32-555" $lane  # Remote Desktop Users
        Add-Member (Get-LocalGroup -Name 'ALE Lanes').SID.Value $lane
    }
    # Stage directories, the isolation recipe and the profile baselines.
    & $python -I -S "$station\stationd.py" enroll
    if ($LASTEXITCODE -ne 0) { $failed = $true }

    Step "service ALEStation"
    $image = "`"$python`" -I -S `"$station\stationd.py`" service"
    if (Get-Service ALEStation -ErrorAction SilentlyContinue) {
        Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\ALEStation" ImagePath $image
        Set-Service ALEStation -StartupType Automatic
    } else {
        New-Service -Name ALEStation -BinaryPathName $image -DisplayName "ALE Station" `
            -StartupType Automatic -Description "Leases this Station's Lanes to ALE" | Out-Null
    }
    sc.exe failure ALEStation reset= 86400 actions= restart/5000/restart/5000/restart/30000 |
        Out-Null
    Start-Service ALEStation
    $lanes = $station_config.lanes -join ", "
    Step "the keep-alive box connects $lanes at $($station_config.resolution)"
    if ($restartSshd) {
        Write-Output "ALE: restarting sshd"
        Restart-Service sshd
    }
} catch {
    Write-Output "ALE: failed: $($_.Exception.Message)"
    $failed = $true
} finally {
    Remove-Item "$here\ale-station-enroll.ps1", "$here\ale-station.json", `
        "$here\ale-station-setup.json", "$here\ale-station-keys", "$here\ale-stationd.py" `
        -Force -ErrorAction SilentlyContinue
}
if ($failed) { exit 1 }
exit 0
"""


if __name__ == "__main__":
    raise SystemExit(main())

param([switch]$CheckOnly)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$choco = "$env:ProgramData\chocolatey\bin\choco.exe"
$bash = "$env:ProgramFiles\Git\bin\bash.exe"

if (-not $CheckOnly) {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run tools.ps1 from an elevated PowerShell session as the agent user."
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $work = "$env:ProgramData\ALE\install\tools"
    New-Item -ItemType Directory -Path $work -Force | Out-Null

    if (-not (Test-Path $choco)) {
        $archive = "$work\chocolatey.zip"
        Invoke-WebRequest -UseBasicParsing `
            "https://community.chocolatey.org/api/v2/package/chocolatey/2.4.3" -OutFile $archive
        if ((Get-FileHash $archive -Algorithm SHA256).Hash -ne `
            "d4998ca928a85a484507dcaa39c30948a6516de0d1469b0511931d44a53456c3") {
            throw "Chocolatey package checksum does not match."
        }
        Expand-Archive $archive "$work\chocolatey" -Force
        & "$work\chocolatey\tools\chocolateyInstall.ps1"
    }
    foreach ($package in @(
        @{ Name = "chocolatey"; Version = "2.4.3" },
        @{ Name = "nodejs-lts"; Version = "24.15.0" },
        @{ Name = "git.install"; Version = "2.53.0" }
    )) {
        & $choco install $package.Name "--version=$($package.Version)" -y --no-progress `
            --limit-output --fail-on-unfound
        if ($LASTEXITCODE -notin 0, 3010) {
            throw "$($package.Name) installation exited $LASTEXITCODE."
        }
    }

    # Install App Installer for this same user so winget works in its desktop session.
    $winget = "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe"
    if (-not (Test-Path $winget) -or (& $winget --version) -ne "v1.29.280") {
        $release = "https://github.com/microsoft/winget-cli/releases/download/v1.29.280"
        foreach ($file in @{
            "DesktopAppInstaller_Dependencies.zip" =
                "3bbfcaa5cb011c48fac48d896d64a5c7c6898859a9f3d01555c8cd000f4e2962"
            "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle" =
                "0809fa9f52e395d6e7de692331dce847ac991952675116bb4d8aae2ddcc20946"
        }.GetEnumerator()) {
            Invoke-WebRequest -UseBasicParsing "$release/$($file.Key)" -OutFile "$work\$($file.Key)"
            if ((Get-FileHash "$work\$($file.Key)" -Algorithm SHA256).Hash -ne $file.Value) {
                throw "WinGet package checksum does not match: $($file.Key)"
            }
        }
        Expand-Archive "$work\DesktopAppInstaller_Dependencies.zip" "$work\winget" -Force
        $dependencies = @(Get-ChildItem "$work\winget\x64\*.appx" | ForEach-Object FullName)
        Add-AppxPackage "$work\Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle" `
            -DependencyPath $dependencies
    }

    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    foreach ($directory in "$env:ProgramFiles\Git\bin", "$env:ProgramFiles\Git\usr\bin") {
        if ($directory -notin $machinePath.Split(";")) { $machinePath += ";$directory" }
    }
    [Environment]::SetEnvironmentVariable("Path", $machinePath, "Machine")
    Remove-Item $work -Recurse -Force
}

$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
    [Environment]::GetEnvironmentVariable("Path", "User")
foreach ($tool in ([ordered]@{
    "python.exe" = ""
    "node.exe" = "v24.15.0"
    "npm.cmd" = ""
    "git.exe" = "git version 2.53.0.windows.1"
    "choco.exe" = "2.4.3"
    "winget.exe" = "v1.29.280"
}).GetEnumerator()) {
    $version = (& $tool.Key --version) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "$($tool.Key) is not usable (exit $LASTEXITCODE)." }
    if ($tool.Value -and $version.Trim() -ne $tool.Value) {
        throw "$($tool.Key) has version '$version'; expected '$($tool.Value)'."
    }
    Write-Output "$($tool.Key) $version"
}
if ((& node.exe -p process.platform) -ne "win32") { throw "Native Windows Node.js is required." }
& $bash -c 'set -eu; node --version; git --version; command -v mkdir cp python npm'
if ($LASTEXITCODE -ne 0) { throw "Git Bash cannot run the native agent toolchain." }

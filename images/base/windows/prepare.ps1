param(
    [string]$AgentUser = "user",
    [string]$GuestdSource = "$PSScriptRoot\guestd",
    [string]$PythonVersion = "3.12.10",
    [string]$CuaDriverVersion = "0.12.6",
    [switch]$Compact
)

$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run prepare.ps1 from an elevated PowerShell session."
}
if (-not (Test-Path "$GuestdSource\main.py")) {
    throw "guestd source is missing: $GuestdSource"
}
if (-not (Get-LocalUser -Name $AgentUser -ErrorAction SilentlyContinue)) {
    throw "Windows account '$AgentUser' does not exist."
}
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name.Split("\\")[-1]
if ($currentUser -ne $AgentUser) {
    throw "Run prepare.ps1 as '$AgentUser' so Cua Driver is installed for its desktop session."
}

$work = "$env:ProgramData\ALE\install"
$guestd = "$env:ProgramData\ALE\guestd"
$setup = "$env:ProgramData\ALE\setup"
$verify = "$env:ProgramData\ALE\verify"
New-Item -ItemType Directory -Path $work, $guestd, $setup, $verify -Force | Out-Null

$python = "C:\Python312\python.exe"
if (-not (Test-Path $python)) {
    $installer = "$work\python-$PythonVersion-amd64.exe"
    $installLog = "$work\python-install.log"
    Invoke-WebRequest `
        "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" `
        -OutFile $installer
    if ((Get-AuthenticodeSignature $installer).Status -ne "Valid") {
        throw "Python installer signature is not valid."
    }
    $uninstall = Start-Process $installer -Wait -PassThru -ArgumentList @(
        "/uninstall", "/quiet", "/norestart"
    )
    if ($uninstall.ExitCode -notin 0, 1605, 3010) {
        throw "Python cleanup exited $($uninstall.ExitCode)."
    }
    $process = Start-Process $installer -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=1", "TargetDir=C:\Python312",
        "PrependPath=1", "Include_test=0", "Include_doc=0", "Include_dev=0",
        "Include_tcltk=0", "Include_launcher=1",
        "/log", $installLog
    )
    if ($process.ExitCode -ne 0) {
        throw "Python installer exited $($process.ExitCode)."
    }
    if (-not (Test-Path $python)) {
        $detail = if (Test-Path $installLog) {
            (Get-Content $installLog -Tail 20) -join [Environment]::NewLine
        } else {
            "installer produced no log"
        }
        throw "Python installer completed without $python.`n$detail"
    }
}
& $python -c "import sys; assert sys.version_info >= (3, 12), sys.version"

Copy-Item "$GuestdSource\*.py" $guestd -Force
icacls "$env:ProgramData\ALE" /inheritance:r /grant:r `
    "SYSTEM:(OI)(CI)(F)" "Administrators:(OI)(CI)(F)" "${AgentUser}:(OI)(CI)(RX)" | Out-Null
foreach ($stage in $setup, $verify) {
    icacls $stage /inheritance:r /grant:r `
        "SYSTEM:(OI)(CI)(F)" "Administrators:(OI)(CI)(F)" "${AgentUser}:(OI)(CI)(M)" | Out-Null
}

$driverInstaller = "$work\install-cua-driver.ps1"
Invoke-WebRequest "https://cua.ai/driver/install.ps1" -OutFile $driverInstaller
$env:CUA_DRIVER_RS_VERSION = $CuaDriverVersion
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $driverInstaller `
    -Release $CuaDriverVersion -AutoStart
if ($LASTEXITCODE -ne 0) {
    throw "Cua Driver installer exited $LASTEXITCODE."
}

$driver = "$env:LOCALAPPDATA\Programs\Cua\cua-driver\bin\cua-driver.exe"
if (-not (Test-Path $driver)) {
    throw "Cua Driver binary is missing after installation: $driver"
}
Unregister-ScheduledTask -TaskName "cua-driver-serve" -Confirm:$false `
    -ErrorAction SilentlyContinue
$driverEscaped = $driver.Replace("'", "''")
$driverAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -WindowStyle Hidden -NonInteractive -Command `"" +
    "Start-Process -FilePath '$driverEscaped' -ArgumentList " +
    "'serve --permission-mode unrestricted --dangerously-bypass-approvals' " +
    "-WindowStyle Hidden`""
)
$driverTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$AgentUser"
$driverPrincipal = New-ScheduledTaskPrincipal `
    -UserId "$env:COMPUTERNAME\$AgentUser" -LogonType Interactive -RunLevel Highest
$driverSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "ALE Cua Driver" -Action $driverAction `
    -Trigger $driverTrigger -Principal $driverPrincipal -Settings $driverSettings `
    -Force | Out-Null

$start = "$env:ProgramData\ALE\start-guestd.ps1"
@"
`$env:ALE_AGENT_USER = "$AgentUser"
`$env:ALE_AGENT_HOME = "C:\Users\$AgentUser"
`$env:ALE_GUI = "true"
& "$python" "$guestd\main.py" --tcp 0.0.0.0:7411
"@ | Set-Content -Path $start -Encoding UTF8

$account = "$env:COMPUTERNAME\$AgentUser"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument `
    "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ALE Guestd" -Action $action -Trigger $trigger `
    -Principal $taskPrincipal -Settings $settings -Force | Out-Null

# The seed came from GCP, but the prepared disk runs behind ALE's QEMU runner.  Leaving
# cloud agents enabled adds minute-long metadata timeouts to every cold boot.
Get-Service | Where-Object {
    $_.Name -match "GCE|Google" -or $_.DisplayName -match "GCE|Google"
} | ForEach-Object {
    Stop-Service -Name $_.Name -Force -ErrorAction SilentlyContinue
    Set-Service -Name $_.Name -StartupType Disabled
}
Remove-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" `
    -Name "VMware User Process" -ErrorAction SilentlyContinue
Get-Process vmtoolsd -ErrorAction SilentlyContinue | Stop-Process -Force

Get-Service | Where-Object { $_.DisplayName -like "VMware*" } | ForEach-Object {
    Stop-Service -Name $_.Name -Force -ErrorAction SilentlyContinue
    Set-Service -Name $_.Name -StartupType Disabled
}
Get-Process -Name "vmtoolsd", "vmwaretray" -ErrorAction SilentlyContinue | `
    Stop-Process -Force -ErrorAction SilentlyContinue
& "$PSScriptRoot\debloat.ps1" -AgentUser $AgentUser
if (-not (Test-Path "$env:ProgramData\ALE\debloat.done")) {
    throw "Windows base cleanup did not complete; inspect C:\ProgramData\ALE\debloat.log."
}

# A Task episode must not mutate its operating system or reboot midway through a run.
$updatePolicy = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
New-Item -Path $updatePolicy -Force | Out-Null
New-ItemProperty -Path $updatePolicy -Name NoAutoUpdate -Value 1 `
    -PropertyType DWord -Force | Out-Null
Stop-Service -Name wuauserv -Force -ErrorAction SilentlyContinue
Set-Service -Name wuauserv -StartupType Disabled

$contentDelivery = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
New-Item -Path $contentDelivery -Force | Out-Null
foreach ($name in "SilentInstalledAppsEnabled", "SoftLandingEnabled", "SystemPaneSuggestionsEnabled") {
    New-ItemProperty -Path $contentDelivery -Name $name -Value 0 `
        -PropertyType DWord -Force | Out-Null
}

Get-NetFirewallRule -DisplayName "ALE guestd" -ErrorAction SilentlyContinue | `
    Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "ALE guestd" -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 7411 -RemoteAddress 172.30.0.1 | Out-Null

powercfg.exe /hibernate off
Remove-Item "$env:TEMP\*", "$env:SystemRoot\Temp\*" -Recurse -Force `
    -ErrorAction SilentlyContinue
Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue

if ($Compact) {
    $archive = "$work\SDelete.zip"
    Invoke-WebRequest "https://download.sysinternals.com/files/SDelete.zip" -OutFile $archive
    Expand-Archive $archive "$work\sdelete" -Force
    $sdelete = "$work\sdelete\sdelete64.exe"
    if ((Get-AuthenticodeSignature $sdelete).Status -ne "Valid") {
        throw "SDelete signature is not valid."
    }
    & $sdelete -accepteula -z C:
}
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "ALE Windows base preparation completed. Reboot once, test guestd and Cua Driver, then shut down."

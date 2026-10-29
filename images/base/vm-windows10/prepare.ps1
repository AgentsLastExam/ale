param(
    [string]$AgentUser = "user",
    [string]$PythonVersion = "3.12.10",
    [switch]$Compact
)

$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run prepare.ps1 from an elevated PowerShell session."
}
if (-not (Get-LocalUser -Name $AgentUser -ErrorAction SilentlyContinue)) {
    throw "Windows account '$AgentUser' does not exist."
}
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name.Split("\\")[-1]
if ($currentUser -ne $AgentUser) {
    throw "Run prepare.ps1 as '$AgentUser' so its per-user settings apply to that account."
}

$work = "$env:ProgramData\ALE\install"
$setup = "$env:ProgramData\ALE\setup"
$verify = "$env:ProgramData\ALE\verify"
New-Item -ItemType Directory -Path $work, $setup, $verify -Force | Out-Null

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

# The framework (guestd, Cua Driver, the service that starts them) is not part of the base:
# ALE installs it into a copy of the disk before it boots, and its service locks this
# directory down at every boot. These are the directories ALE stages Task files in.
icacls "$env:ProgramData\ALE" /inheritance:r /grant:r `
    "SYSTEM:(OI)(CI)(F)" "Administrators:(OI)(CI)(F)" | Out-Null
foreach ($stage in $setup, $verify) {
    icacls $stage /inheritance:r /grant:r `
        "SYSTEM:(OI)(CI)(F)" "Administrators:(OI)(CI)(F)" "${AgentUser}:(OI)(CI)(M)" | Out-Null
}
# A seed an earlier recipe prepared also carries a guestd of its own, the logon tasks that
# started it and its firewall rule.
foreach ($task in "ALE Guestd Session", "ALE Guestd") {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
}
Get-NetFirewallRule -DisplayName "ALE guestd" -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule
foreach ($leftover in "guestd", "start-guestd.ps1", "start-session.ps1", "debloat.ps1") {
    Remove-Item "$env:ProgramData\ALE\$leftover" -Recurse -Force -ErrorAction SilentlyContinue
}

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
& "$PSScriptRoot\tools.ps1"

# The screen, filesystem and timestamps must begin every episode in one predictable locale.
$languageList = New-WinUserLanguageList "en-US"
Set-WinUserLanguageList $languageList -Force
Set-WinUILanguageOverride -Language "en-US"
Set-WinSystemLocale "en-US"
Set-Culture "en-US"
Set-WinHomeLocation -GeoId 244
Set-TimeZone -Id "UTC"
if (Get-Command Copy-UserInternationalSettingsToSystem -ErrorAction SilentlyContinue) {
    Copy-UserInternationalSettingsToSystem -WelcomeScreen $true -NewUser $true
}

# A Task episode must never update, reboot, recover, sleep or display background prompts.
$windowsUpdate = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
$automaticUpdate = Join-Path $windowsUpdate "AU"
New-Item -Path $automaticUpdate -Force | Out-Null
foreach ($entry in @{
    DisableWindowsUpdateAccess = 1
    DoNotConnectToWindowsUpdateInternetLocations = 1
    SetDisableUXWUAccess = 1
}.GetEnumerator()) {
    New-ItemProperty -Path $windowsUpdate -Name $entry.Key -Value $entry.Value `
        -PropertyType DWord -Force | Out-Null
}
foreach ($entry in @{
    AUOptions = 1
    NoAutoRebootWithLoggedOnUsers = 1
    NoAutoUpdate = 1
}.GetEnumerator()) {
    New-ItemProperty -Path $automaticUpdate -Name $entry.Key -Value $entry.Value `
        -PropertyType DWord -Force | Out-Null
}
foreach ($service in "wuauserv", "UsoSvc", "WaaSMedicSvc", "DoSvc") {
    Stop-Service -Name $service -Force -ErrorAction SilentlyContinue
    Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$service" `
        -Name Start -Value 4 -Force -ErrorAction SilentlyContinue
}
Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $_.TaskName -match "MicrosoftEdgeUpdate" -or
    $_.TaskPath -match "WindowsUpdate|UpdateOrchestrator|WaaSMedic|WindowsBackup|FileHistory"
} | ForEach-Object {
    Disable-ScheduledTask -InputObject $_ -ErrorAction SilentlyContinue | Out-Null
}
$windowsStore = "HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore"
New-Item $windowsStore -Force | Out-Null
New-ItemProperty $windowsStore -Name AutoDownload -Value 2 -PropertyType DWord -Force | Out-Null
$edgeUpdate = "HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate"
New-Item $edgeUpdate -Force | Out-Null
New-ItemProperty $edgeUpdate -Name UpdateDefault -Value 0 -PropertyType DWord -Force | Out-Null
Get-Service -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match "^edgeupdate" -or $_.DisplayName -match "Microsoft Edge Update"
} | ForEach-Object {
    Stop-Service -Name $_.Name -Force -ErrorAction SilentlyContinue
    Set-Service -Name $_.Name -StartupType Disabled -ErrorAction SilentlyContinue
}

reagentc.exe /disable | Out-Null
Disable-ComputerRestore -Drive "$env:SystemDrive\" -ErrorAction SilentlyContinue
vssadmin.exe delete shadows /all /quiet | Out-Null
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl" `
    -Name AutoReboot -Value 0 -Force

$errorReporting = "HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting"
New-Item $errorReporting -Force | Out-Null
New-ItemProperty $errorReporting -Name Disabled -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty $errorReporting -Name DontShowUI -Value 1 -PropertyType DWord -Force | Out-Null
Stop-Service WerSvc -Force -ErrorAction SilentlyContinue
Set-Service WerSvc -StartupType Disabled -ErrorAction SilentlyContinue

$maintenance = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Schedule\Maintenance"
New-Item $maintenance -Force | Out-Null
New-ItemProperty $maintenance -Name MaintenanceDisabled -Value 1 `
    -PropertyType DWord -Force | Out-Null

$contentDelivery = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
New-Item -Path $contentDelivery -Force | Out-Null
foreach ($name in "SilentInstalledAppsEnabled", "SoftLandingEnabled", "SystemPaneSuggestionsEnabled") {
    New-ItemProperty -Path $contentDelivery -Name $name -Value 0 `
        -PropertyType DWord -Force | Out-Null
}
$cloudContent = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent"
New-Item $cloudContent -Force | Out-Null
New-ItemProperty $cloudContent -Name DisableWindowsConsumerFeatures -Value 1 `
    -PropertyType DWord -Force | Out-Null
$notifications = "HKCU:\SOFTWARE\Policies\Microsoft\Windows\Explorer"
New-Item $notifications -Force | Out-Null
New-ItemProperty $notifications -Name DisableNotificationCenter -Value 1 `
    -PropertyType DWord -Force | Out-Null
$edgePolicy = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
New-Item $edgePolicy -Force | Out-Null
New-ItemProperty $edgePolicy -Name HideFirstRunExperience -Value 1 `
    -PropertyType DWord -Force | Out-Null
New-ItemProperty $edgePolicy -Name DefaultBrowserSettingEnabled -Value 0 `
    -PropertyType DWord -Force | Out-Null

powercfg.exe /setactive SCHEME_BALANCED
powercfg.exe /hibernate off
powercfg.exe /change monitor-timeout-ac 0
powercfg.exe /change monitor-timeout-dc 0
powercfg.exe /change disk-timeout-ac 0
powercfg.exe /change disk-timeout-dc 0
powercfg.exe /change standby-timeout-ac 0
powercfg.exe /change standby-timeout-dc 0
powercfg.exe /change hibernate-timeout-ac 0
powercfg.exe /change hibernate-timeout-dc 0
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" `
    -Name HiberbootEnabled -Value 0 -Force
Set-ItemProperty "HKCU:\Control Panel\Desktop" -Name ScreenSaveActive -Value "0" -Force
Set-ItemProperty "HKCU:\Control Panel\Desktop" -Name ScreenSaveTimeOut -Value "0" -Force
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

Write-Host "ALE Windows base preparation completed. Shut down completely (Fast Startup is off)."

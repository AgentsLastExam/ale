param(
    [string]$AgentUser = "user",
    [switch]$RemoveInstallCache
)

$ErrorActionPreference = "Stop"
$log = "$env:ProgramData\ALE\debloat.log"
Start-Transcript -Path $log -Append | Out-Null

function Invoke-Uninstaller {
    param([string]$Path, [string[]]$Arguments)
    $executable = if (Test-Path -LiteralPath $Path) {
        $Path
    } else {
        (Get-Command $Path -ErrorAction SilentlyContinue).Source
    }
    if (-not $executable) {
        return
    }
    $process = Start-Process -FilePath $executable -ArgumentList $Arguments -Wait -PassThru
    if ($process.ExitCode -notin 0, 1605, 1614, 1641, 3010) {
        Write-Warning "$Path exited $($process.ExitCode). Leftovers will still be removed."
    }
}

function Remove-Msi {
    param([string]$ProductCode)
    Invoke-Uninstaller msiexec.exe @("/x", $ProductCode, "/qn", "/norestart")
}

# Stop vendor processes first so their silent uninstallers do not leave locked trees.
Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match "chrome|gimp|googet|nmap|npcap|office|onedrive|thunderbird|vlc|vmware|vnc"
} | Stop-Process -Force -ErrorAction SilentlyContinue

$chrome = Get-ChildItem "$env:ProgramFiles\Google\Chrome\Application\*\Installer\setup.exe" `
    -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
if ($chrome) {
    Invoke-Uninstaller $chrome.FullName @(
        "--uninstall", "--channel=stable", "--system-level", "--force-uninstall"
    )
}
Invoke-Uninstaller "${env:ProgramFiles(x86)}\Google\Cloud SDK\uninstaller.exe" @("/S")
Invoke-Uninstaller "$env:ProgramFiles\GIMP 2\uninst\unins000.exe" `
    @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
$oneDrive = Get-ChildItem "$env:ProgramFiles\Microsoft OneDrive\*\OneDriveSetup.exe" `
    -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
if ($oneDrive) {
    Invoke-Uninstaller $oneDrive.FullName @("/uninstall", "/allusers")
}
Invoke-Uninstaller "$env:ProgramFiles\Mozilla Thunderbird\uninstall\helper.exe" @("/S")
Invoke-Uninstaller "${env:ProgramFiles(x86)}\Mozilla Maintenance Service\uninstall.exe" @("/S")
Invoke-Uninstaller "${env:ProgramFiles(x86)}\Nmap\uninstall.exe" @("/S")
Invoke-Uninstaller "$env:ProgramFiles\Npcap\uninstall.exe" @("/S")
Invoke-Uninstaller "${env:ProgramFiles(x86)}\VideoLAN\VLC\uninstall.exe" @("/S")
Invoke-Uninstaller "$env:SystemDrive\Users\$AgentUser\AppData\Local\Programs\Microsoft VS Code\unins000.exe" `
    @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
Invoke-Uninstaller "$env:ProgramFiles\7-Zip\Uninstall.exe" @("/S")
Invoke-Uninstaller "$env:ProgramFiles\Git\unins000.exe" `
    @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")

$clickToRun = "$env:ProgramFiles\Common Files\Microsoft Shared\ClickToRun\OfficeClickToRun.exe"
Invoke-Uninstaller $clickToRun @(
    "scenario=install", "scenariosubtype=ARP", "sourcetype=None",
    "productstoremove=O365ProPlusRetail.16_en-us_x-none", "culture=en-us",
    "version.16=16.0", "displaylevel=false", "forceappshutdown=true"
)

Remove-Msi "{DB1CD517-ED68-464C-A797-7C3D29B3CB27}" # RealVNC
Remove-Msi "{6070BE95-B84D-40FE-8ABD-C70B59F5A164}" # VMware Tools
Remove-Msi "{90160000-008C-0000-0000-0000000FF1CE}" # Office extension
Remove-Msi "{90160000-00DD-0000-1000-0000000FF1CE}" # Office extension x64
Remove-Msi "{1FC1A6C2-576E-489A-9B4A-92D21F542136}" # Update Health Tools
Remove-Msi "{18DF3488-2245-432B-A023-3AA05C2A00C8}" # Python documentation
Remove-Msi "{EF4A3D60-9A53-4697-A18D-D3353F8554E6}" # Python development headers
Remove-Msi "{75485683-EF03-41E6-BF21-D1491694548C}" # Python Tcl/Tk
Remove-Msi "{AB39A2A5-6C29-49BB-87B3-99C1ECE806C0}" # LibreOffice

$googet = "$env:ProgramData\GooGet\googet.exe"
foreach ($package in @(
    "certgen",
    "google-compute-engine-auto-updater",
    "google-compute-engine-metadata-scripts",
    "google-compute-engine-sysprep",
    "google-compute-engine-windows",
    "google-osconfig-agent"
)) {
    Invoke-Uninstaller $googet @("-noconfirm", "remove", $package)
}
Invoke-Uninstaller $googet @("-noconfirm", "remove", "googet")

$vendorPattern = "GCE|Google|Chrome|gupdate|MozillaMaintenance|OneDrive|OfficeClickToRun|" +
    "osconfig|VNC|VMware|vmtools|npcap"
Get-Service -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match $vendorPattern -or $_.DisplayName -match $vendorPattern
} | ForEach-Object {
    Stop-Service -Name $_.Name -Force -ErrorAction SilentlyContinue
    sc.exe delete $_.Name | Out-Null
}
Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $_.TaskName -match $vendorPattern -or $_.TaskPath -match $vendorPattern
} | Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue
Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $_.TaskName -in "AutoShutdown-agenthle", "CuaServer-agenthle" -or
    ($_.Actions | Where-Object {
        "$($_.Execute) $($_.Arguments)" -match "Users\\[^\\]+\\scripts|start_server\.vbs"
    })
} | Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue

$uninstallRoots = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
)
$uninstallRoots += Get-ChildItem "Registry::HKEY_USERS" -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.PSPath "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" } |
    Where-Object { Test-Path -LiteralPath $_ }
$removedProducts = "7-Zip|GIMP|Git|GooGet|Google Chrome|Google Cloud SDK|LibreOffice|" +
    "Microsoft 365|OneDrive|" +
    "Office 16 Click-to-Run|Microsoft Visual Studio Code|Mozilla|Thunderbird|Nmap|Npcap|" +
    "VLC media player|VMware Tools|VNC Server|Microsoft Update Health Tools|" +
    "Python .* (Documentation|Development Libraries|Tcl/Tk Support)"
foreach ($root in $uninstallRoots) {
    Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | ForEach-Object {
        $product = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
        if ($product.DisplayName -match $removedProducts) {
            Remove-Item -LiteralPath $_.PSPath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$removeTrees = @(
    "$env:SystemDrive\`$WINDOWS.~BT",
    "$env:SystemDrive\`$WinREAgent",
    "$env:SystemDrive\Google",
    "$env:SystemDrive\inetpub",
    "$env:SystemDrive\Windows.old",
    "$env:SystemDrive\Windows10Upgrade",
    "$env:ProgramData\GooGet",
    "$env:ProgramData\Google",
    "$env:ProgramData\Microsoft OneDrive",
    "$env:ProgramData\Microsoft\Office",
    "$env:ProgramData\RealVNC-Service",
    "$env:ProgramData\VMware",
    "$env:ProgramFiles\Google",
    "$env:ProgramFiles\7-Zip",
    "$env:ProgramFiles\Git",
    "${env:ProgramFiles(x86)}\Google",
    "$env:ProgramFiles\GIMP 2",
    "$env:ProgramFiles\LibreOffice",
    "$env:ProgramFiles\Microsoft OneDrive",
    "$env:ProgramFiles\Microsoft Update Health Tools",
    "$env:ProgramFiles\Microsoft Office",
    "${env:ProgramFiles(x86)}\Microsoft Office",
    "$env:ProgramFiles\Common Files\Microsoft Shared\ClickToRun",
    "$env:ProgramFiles\Mozilla Thunderbird",
    "${env:ProgramFiles(x86)}\Mozilla Maintenance Service",
    "${env:ProgramFiles(x86)}\Nmap",
    "$env:ProgramFiles\Npcap",
    "${env:ProgramFiles(x86)}\VideoLAN",
    "$env:ProgramFiles\RealVNC",
    "$env:ProgramFiles\VMware",
    "${env:ProgramFiles(x86)}\nssm-2.24",
    "$env:SystemDrive\Users\$AgentUser\.conda",
    "$env:SystemDrive\Users\$AgentUser\.ssh",
    "$env:SystemDrive\Users\$AgentUser\.vscode",
    "$env:SystemDrive\Users\$AgentUser\ale-smoke-dir",
    "$env:SystemDrive\Users\$AgentUser\scripts",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\GIMP",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Google",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\Credentials",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\Edge\User Data",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\Excel",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\FORMS",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\Office",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\OneAuth",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\OneDrive",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\Outlook",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\PowerPoint",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\TokenBroker",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\Vault",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Microsoft\Word",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Package Cache\{b6ce88eb-2ce3-4d91-8efc-425ae1f48caf}",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Programs\Microsoft VS Code",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\RealVNC",
    "$env:SystemDrive\Users\$AgentUser\AppData\Local\Thunderbird",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\Code",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\GIMP",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\gcloud",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\Microsoft\Credentials",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\Microsoft\Protect",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\Microsoft\Vault",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\Mozilla",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\Thunderbird",
    "$env:SystemDrive\Users\$AgentUser\AppData\Roaming\vlc"
)
foreach ($path in $removeTrees) {
    Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
}
Get-ChildItem "$env:ProgramData\Mozilla-*" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
$userHome = "$env:SystemDrive\Users\$AgentUser"
foreach ($name in @(
    ".bash_history", ".tmp.7z", ".viminfo", "ale-smoke.txt", "thunderbird-profile.7z"
)) {
    Remove-Item -LiteralPath (Join-Path $userHome $name) -Force -ErrorAction SilentlyContinue
}
foreach ($directory in @(
    "$userHome\Desktop", "$userHome\Documents", "$userHome\Downloads",
    "$userHome\AppData\Local\Temp", "$userHome\AppData\Roaming\Microsoft\Windows\Recent",
    "$env:SystemRoot\Temp", "$env:SystemRoot\SoftwareDistribution\Download",
    "$env:SystemDrive\`$Recycle.Bin"
)) {
    Get-ChildItem -LiteralPath $directory -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
foreach ($key in @(
    "HKCU:\SOFTWARE\Microsoft\Notepad",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\ComDlg32",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\RecentDocs",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\RunMRU",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
)) {
    Remove-Item -LiteralPath $key -Recurse -Force -ErrorAction SilentlyContinue
}

$iis = Get-WindowsOptionalFeature -Online -FeatureName IIS-WebServerRole `
    -ErrorAction SilentlyContinue
if ($iis.State -eq "Enabled") {
    Disable-WindowsOptionalFeature -Online -FeatureName IIS-WebServerRole -NoRestart | Out-Null
}

Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase
powercfg.exe /hibernate off
Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue
if ($RemoveInstallCache) {
    Remove-Item "$env:ProgramData\ALE\install" -Recurse -Force -ErrorAction SilentlyContinue
}
Set-ItemProperty `
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
    -Name EnableLUA -Value 1 -Force
$remainingProducts = foreach ($root in $uninstallRoots) {
    Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | ForEach-Object {
        $product = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
        if ($product.DisplayName -match $removedProducts) {
            $_
        }
    }
}
if ($remainingProducts) {
    throw "Removed products remain registered: $($remainingProducts.PSChildName -join ', ')"
}
New-Item "$env:ProgramData\ALE\debloat.done" -ItemType File -Force | Out-Null
Stop-Transcript | Out-Null

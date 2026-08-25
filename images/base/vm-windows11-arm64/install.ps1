$ErrorActionPreference = "Stop"

$virtio = Get-CimInstance Win32_LogicalDisk | Where-Object {
    $_.VolumeName -like "virtio-win*"
} | Select-Object -First 1
if (-not $virtio) {
    throw "The virtio-win driver ISO is not mounted."
}

pnputil.exe /add-driver "$($virtio.DeviceID)\*.inf" /subdirs /install
if ($LASTEXITCODE -ne 0) {
    throw "VirtIO driver installation exited $LASTEXITCODE."
}

& "$PSScriptRoot\prepare.ps1" -AgentUser user -GuestdSource "$PSScriptRoot\guestd"
if ($LASTEXITCODE -ne 0) {
    throw "ALE Windows preparation exited $LASTEXITCODE."
}

shutdown.exe /s /t 0 /f

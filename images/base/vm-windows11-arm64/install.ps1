$ErrorActionPreference = "Stop"

$virtio = Get-CimInstance Win32_LogicalDisk | Where-Object {
    $_.VolumeName -like "virtio-win*"
} | Select-Object -First 1
if (-not $virtio) {
    throw "The virtio-win driver ISO is not mounted."
}

$drivers = Get-ChildItem "$($virtio.DeviceID)\" -Filter *.inf -Recurse | Where-Object {
    $_.FullName -match "\\ARM64\\"
}
if (-not $drivers) {
    throw "The virtio-win ISO contains no ARM64 driver packages."
}
foreach ($driver in $drivers) {
    pnputil.exe /add-driver $driver.FullName /install
    # PnPUtil returns ERROR_NO_MORE_ITEMS when a matching package is already present,
    # and ERROR_SUCCESS_REBOOT_REQUIRED after a package that needs a later reboot.
    if ($LASTEXITCODE -notin 0, 259, 3010) {
        throw "VirtIO driver installation exited $LASTEXITCODE for $($driver.FullName)."
    }
}

& "$PSScriptRoot\prepare.ps1" -AgentUser user -GuestdSource "$PSScriptRoot\guestd"
if ($LASTEXITCODE -ne 0) {
    throw "ALE Windows preparation exited $LASTEXITCODE."
}

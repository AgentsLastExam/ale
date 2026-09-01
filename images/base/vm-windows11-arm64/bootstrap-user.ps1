$ErrorActionPreference = "Stop"
$taskName = "ALE Image Bootstrap User"
$seed = "C:\ALESeed"
$transcript = Join-Path $seed "bootstrap-user.log"
$failed = Join-Path $seed "install.failed"
$completed = Join-Path $seed "install.done"

Remove-Item $failed, $completed -Force -ErrorAction SilentlyContinue
Start-Transcript -Path $transcript -Append | Out-Null
try {
    & "$seed\install.ps1"
    "$(Get-Date -Format o) ALE Windows image preparation completed." |
        Set-Content $completed -Encoding utf8
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Stop-Transcript | Out-Null
    shutdown.exe /s /t 0 /f
} catch {
    $_ | Out-String | Set-Content $failed -Encoding utf8
    Stop-Transcript | Out-Null
    throw
}

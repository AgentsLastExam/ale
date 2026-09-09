param(
    [switch]$InstallTask,
    [switch]$LaunchUserTask
)

$ErrorActionPreference = "Stop"
$systemTaskName = "ALE Image Bootstrap System"
$userTaskName = "ALE Image Bootstrap User"
$seed = "C:\ALESeed"
$log = Join-Path $seed "bootstrap-system.log"
$agentUser = "user"

function Write-BootstrapLog([string]$Message) {
    "$(Get-Date -Format o) $Message" | Out-File $log -Append -Encoding utf8
}

try {
    if ($InstallTask) {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
            "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
            "-File `"$seed\bootstrap-system.ps1`" -LaunchUserTask"
        )
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
            -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
        Register-ScheduledTask -TaskName $systemTaskName -Action $action `
            -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Write-BootstrapLog "Registered the SYSTEM bootstrap task."
        exit 0
    }

    if ($LaunchUserTask) {
        if (-not (Get-LocalUser -Name $agentUser -ErrorAction SilentlyContinue)) {
            throw "Windows account '$agentUser' does not exist yet."
        }
        $account = "$env:COMPUTERNAME\$agentUser"
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
            "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
            "-File `"$seed\bootstrap-user.ps1`""
        )
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
        $principal = New-ScheduledTaskPrincipal -UserId $account `
            -LogonType Interactive -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 2)
        Register-ScheduledTask -TaskName $userTaskName -Action $action `
            -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $userTaskName
        Write-BootstrapLog "Started the elevated user bootstrap task."
        Unregister-ScheduledTask -TaskName $systemTaskName -Confirm:$false
        exit 0
    }

    throw "Specify either -InstallTask or -LaunchUserTask."
} catch {
    Write-BootstrapLog "FAILED: $($_ | Out-String)"
    throw
}

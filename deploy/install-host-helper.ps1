# One-time setup: generates a shared bearer token, registers the helper
# daemon as a Task Scheduler task that starts at user logon.
# Run: powershell -ExecutionPolicy Bypass -File deploy\install-host-helper.ps1

$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $PSScriptRoot 'host-helper.ps1'
if (-not (Test-Path $scriptPath)) {
    throw "Script not found: $scriptPath"
}

$tokenFile = Join-Path $env:USERPROFILE '.assistant-helper-token'
if (-not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes) -replace '\+', '-' -replace '/', '_' -replace '='
    Set-Content -Path $tokenFile -Value $token -Encoding utf8
    Write-Host "Generated bearer token in $tokenFile"
} else {
    Write-Host "Using existing token in $tokenFile"
}
$token = (Get-Content $tokenFile -Raw).Trim()

$taskName = 'AiAssistent\assistant-host-helper'

$args = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-WindowStyle', 'Hidden',
    '-File', "`"$scriptPath`""
)
$action  = New-ScheduledTaskAction  -Execute 'powershell.exe' -Argument ($args -join ' ')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Hidden

Register-ScheduledTask -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'AiAssistent host helper: exposes localhost HTTP endpoint for `claude login` triggering.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2

Write-Host ''
Write-Host "Task '$taskName' registered and started."
Write-Host ''
Write-Host 'Add these lines to your .env (bot picks them up on restart):'
Write-Host '----'
Write-Host "HOST_HELPER_URL=http://host.docker.internal:47823"
Write-Host "HOST_HELPER_TOKEN=$token"
Write-Host '----'
Write-Host ''
Write-Host 'Quick check:'
Write-Host "  Invoke-WebRequest http://127.0.0.1:47823/health -UseBasicParsing"
Write-Host ''
Write-Host 'To uninstall:'
Write-Host "  Unregister-ScheduledTask -TaskName `"$taskName`" -Confirm:`$false"

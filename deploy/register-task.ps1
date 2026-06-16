# Register the assistant-bot scheduled task. Run as the user that should own it.
# Usage:   powershell -ExecutionPolicy Bypass -File deploy\register-task.ps1
$ErrorActionPreference = 'Stop'

$taskName  = 'AiAssistent\assistant-bot'
$xmlSrc    = Join-Path $PSScriptRoot 'assistant-task.xml'
$xmlOut    = Join-Path $env:TEMP    'assistant-task.xml'
$userId    = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $xmlSrc)) {
    throw "Template not found: $xmlSrc"
}

# Substitute the placeholder and write out as UTF-16 LE w/ BOM (schtasks needs it).
$xml = (Get-Content $xmlSrc -Raw) -replace '__USERID__', [System.Security.SecurityElement]::Escape($userId)
[System.IO.File]::WriteAllText($xmlOut, $xml, [System.Text.Encoding]::Unicode)

Write-Host "Registering task '$taskName' for user '$userId'..."

# /F replaces if it already exists. No /RP because LogonType=InteractiveToken
# doesn't require a stored password — task runs only while user is logged on.
schtasks /Create /TN $taskName /XML $xmlOut /F | Out-Host

Write-Host ''
Write-Host 'Done. Useful commands:'
Write-Host "  schtasks /Run    /TN `"$taskName`"     # start now"
Write-Host "  schtasks /End    /TN `"$taskName`"     # stop"
Write-Host "  schtasks /Query  /TN `"$taskName`" /V  # status"
Write-Host "  schtasks /Delete /TN `"$taskName`" /F  # uninstall"
Write-Host ''
Write-Host "Logs: D:\GitHubProjects\AiAssistent\logs\bot.log"

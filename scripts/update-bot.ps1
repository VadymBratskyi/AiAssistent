# update-bot.ps1 — pull latest code and restart the bot.
# Run this whenever you want to deploy changes:  powershell -File scripts\update-bot.ps1
$ErrorActionPreference = 'Stop'

$root = 'D:\GitHubProjects\AiAssistent'
$py   = Join-Path $root '.venv\Scripts\python.exe'
Set-Location $root

Write-Host '==> git pull' -ForegroundColor Cyan
git pull

# Editable install: code changes are picked up on restart with no reinstall.
# Re-run install anyway so dependency changes in pyproject.toml take effect (fast no-op otherwise).
Write-Host '==> pip install -e . (applies any dependency changes)' -ForegroundColor Cyan
& $py -m pip install -e . --quiet

Write-Host '==> restarting AiAssistantBot task' -ForegroundColor Cyan
schtasks /End /TN 'AiAssistantBot' 2>$null
Start-Sleep -Seconds 2
schtasks /Run /TN 'AiAssistantBot'

Write-Host ''
Write-Host 'Done. Watch the log to confirm:  Get-Content bot.log -Tail 20 -Wait' -ForegroundColor Green

# Launcher invoked by Task Scheduler. Runs the bot foreground inside this
# process and appends stdout/stderr to logs/bot.log so the scheduler can
# treat the process lifetime as "the bot".
$ErrorActionPreference = 'Stop'

$projectDir = 'D:\GitHubProjects\AiAssistent'
$venvPython = Join-Path $projectDir '.venv\Scripts\python.exe'
$logDir     = Join-Path $projectDir 'logs'
$logFile    = Join-Path $logDir 'bot.log'

Set-Location $projectDir
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

# Defensive: repoint the polluted PostgreSQL SSL_CERT_FILE to certifi before
# the bot starts. The bot also self-heals via sslfix.py, but doing it here
# avoids any early TLS calls in the import path picking up the bad value.
try {
    $ca = & $venvPython -c "import certifi; print(certifi.where())" 2>$null
    if ($LASTEXITCODE -eq 0 -and $ca) {
        $env:SSL_CERT_FILE    = $ca
        $env:REQUESTS_CA_BUNDLE = $ca
        $env:CURL_CA_BUNDLE   = $ca
    }
} catch {}

"--- start $(Get-Date -Format o) ---" | Out-File -FilePath $logFile -Append -Encoding utf8

& $venvPython -m assistant *>> $logFile
$exit = $LASTEXITCODE
"--- exit $exit at $(Get-Date -Format o) ---" | Out-File -FilePath $logFile -Append -Encoding utf8
exit $exit

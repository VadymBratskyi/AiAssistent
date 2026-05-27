# run-bot.ps1 — launched by the "AiAssistantBot" scheduled task.
# Kills any existing instance (Telegram allows only one getUpdates poller -> 409 otherwise),
# then runs the bot in the foreground so the task stays "Running" for its lifetime.
# Logging goes to bot.log (stderr — where Python logging writes) and bot.out.log (stdout).
$ErrorActionPreference = 'Continue'

$root = 'D:\GitHubProjects\AiAssistent'
$py   = Join-Path $root '.venv\Scripts\python.exe'

# Kill stragglers (e.g. a manual run) to avoid two pollers fighting over the token.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*-m assistant*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

$proc = Start-Process -FilePath $py -ArgumentList '-m', 'assistant' `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $root 'bot.out.log') `
    -RedirectStandardError  (Join-Path $root 'bot.log')

$proc.WaitForExit()
exit $proc.ExitCode

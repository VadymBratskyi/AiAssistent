# Host-side helper daemon: exposes a tiny loopback HTTP server that the
# containerized bot can call to trigger interactive `claude login` on the
# user's PC. The re-auth flow itself still happens in the user's browser
# with real MFA — the daemon just automates opening the right terminal.
#
# Auto-started by Task Scheduler at logon (see install-host-helper.ps1).

$ErrorActionPreference = 'Stop'

$listenAddr = '127.0.0.1'
$listenPort = 47823
$tokenFile  = Join-Path $env:USERPROFILE '.assistant-helper-token'
$logFile    = Join-Path $env:USERPROFILE '.assistant-helper.log'

function Log($msg) {
    "$(Get-Date -Format o) $msg" | Out-File -FilePath $logFile -Append -Encoding utf8
}

if (-not (Test-Path $tokenFile)) {
    throw "Token file missing: $tokenFile. Run install-host-helper.ps1 first."
}
$sharedToken = (Get-Content $tokenFile -Raw).Trim()
if (-not $sharedToken) {
    throw "Token file is empty: $tokenFile"
}

$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add("http://${listenAddr}:${listenPort}/")
$listener.Start()
Log "listening on http://${listenAddr}:${listenPort}/"

function Send-Response($ctx, [int]$status, [string]$body) {
    $ctx.Response.StatusCode = $status
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $ctx.Response.ContentType = 'application/json'
    $ctx.Response.ContentLength64 = $bytes.Length
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $ctx.Response.OutputStream.Close()
}

while ($listener.IsListening) {
    try {
        $ctx = $listener.GetContext()
        $path = $ctx.Request.Url.AbsolutePath
        $method = $ctx.Request.HttpMethod

        # Bearer auth on everything but /health.
        if ($path -ne '/health') {
            $auth = $ctx.Request.Headers['Authorization']
            if (-not $auth -or $auth -ne "Bearer $sharedToken") {
                Log "unauthorized $method $path"
                Send-Response $ctx 401 '{"error":"unauthorized"}'
                continue
            }
        }

        if ($method -eq 'GET' -and $path -eq '/health') {
            Send-Response $ctx 200 '{"status":"ok"}'
        }
        elseif ($method -eq 'POST' -and $path -eq '/claude-login') {
            # Detach a new interactive terminal running `claude login`. It
            # opens the browser and blocks that window until OAuth completes.
            # We return immediately — the bot polls credmon for the fresh
            # credentials file.
            Log "triggering claude login"
            Start-Process -FilePath 'powershell.exe' `
                -ArgumentList '-NoExit', '-Command', 'claude login' `
                -WindowStyle Normal | Out-Null
            Send-Response $ctx 202 '{"status":"login_started"}'
        }
        else {
            Send-Response $ctx 404 '{"error":"not_found"}'
        }
    }
    catch {
        Log "error: $_"
        try { Send-Response $ctx 500 '{"error":"internal"}' } catch {}
    }
}

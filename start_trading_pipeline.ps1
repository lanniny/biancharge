param(
    [string]$Config = "market_autotrader.growth.example.json",
    [int]$DelaySeconds = 60
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

if (Test-Path -LiteralPath (Join-Path $logs "market-autotrader.stop")) {
    Remove-Item -LiteralPath (Join-Path $logs "market-autotrader.stop") -Force
}

function Invoke-Stage {
    param([string]$Worker)
    python -u (Join-Path $root "market_autotrader.py") --config $Config --once --worker $Worker 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "pipeline worker $Worker failed with exit code $LASTEXITCODE"
    }
}

Write-Output "trading pipeline runner started (poller -> decision -> execution)"

while (-not (Test-Path -LiteralPath (Join-Path $logs "market-autotrader.stop"))) {
    $cycle = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$cycle] pipeline cycle start"
    Invoke-Stage -Worker "poller"
    Invoke-Stage -Worker "decision"
    Invoke-Stage -Worker "execution"
    Write-Output "[$cycle] pipeline cycle done; sleeping ${DelaySeconds}s"
    Start-Sleep -Seconds $DelaySeconds
}

Write-Output "trading pipeline runner stopped"

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$targets = @(
    @{ Name = "realtime supervisor"; StopFile = "realtime-supervisor.stop"; PidFile = "realtime-supervisor.pid" },
    @{ Name = "paper autotrader"; StopFile = "market-autotrader.stop"; PidFile = "market-autotrader.pid" }
)

foreach ($target in $targets) {
    $stopPath = Join-Path $logs $target.StopFile
    Set-Content -LiteralPath $stopPath -Value "stop" -Encoding ASCII
    $pidPath = Join-Path $logs $target.PidFile
    if (Test-Path -LiteralPath $pidPath) {
        $pidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
        if ($pidText) {
            $process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
            if ($process) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    }
    Write-Output "$($target.Name) stop signal sent"
}

& (Join-Path $root "stop_trading_dashboard.ps1")
Write-Output "Autonomous trading stack stopped."

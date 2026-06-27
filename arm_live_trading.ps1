param(
    [string]$ArmPath = "logs/live-trading.armed",
    [int]$Hours = 8
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root $ArmPath
$killPath = Join-Path $root "logs/live-trading.kill"

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
if (Test-Path -LiteralPath $killPath) {
    Write-Output "Kill switch is active at $killPath. Remove it before arming live trading."
    exit 1
}

$payload = @{
    armed_at = (Get-Date).ToUniversalTime().ToString("o")
    expires_at = (Get-Date).ToUniversalTime().AddHours($Hours).ToString("o")
    armed_by = $env:USERNAME
    note = "Manual arm for Binance live autotrader. Delete this file or run disarm_live_trading.ps1 to stop."
} | ConvertTo-Json -Compress

Set-Content -LiteralPath $target -Value $payload -Encoding UTF8
Write-Output "Live trading ARMED: $target"
Write-Output "Arm expires in $Hours hour(s). Re-run this script after review to extend."
Write-Output "Limits are enforced by market_autotrader.growth.example.json (U本位合约 growth 引擎; 权益缩放/30万目标/ATR TP-SL)."
Write-Output "Emergency stop: .\kill_live_trading.ps1"

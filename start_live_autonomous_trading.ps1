param(
    [string]$AutotraderConfig = "market_autotrader.growth.example.json",
    [int]$AutotraderDelaySeconds = 60,
    [switch]$SkipDashboard,
    [switch]$RestartAutotraderOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $root $AutotraderConfig))) {
    throw "Config not found: $AutotraderConfig"
}

function Start-BackgroundRunner {
    param(
        [string]$Name,
        [string]$RunnerCmd,
        [string[]]$RunnerArgs,
        [string]$PidFile
    )

    $existingPid = if (Test-Path -LiteralPath $PidFile) { (Get-Content -LiteralPath $PidFile -Raw).Trim() } else { "" }
    if ($existingPid) {
        $existing = Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Output "$Name already running: pid=$existingPid"
            return $existingPid
        }
    }

    $argText = ($RunnerArgs | ForEach-Object { """$_""" }) -join " "
    $commandLine = "/d /s /c """"$RunnerCmd"" $argText"""
    $process = Start-Process -FilePath "cmd.exe" `
        -ArgumentList $commandLine `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ASCII
    Write-Output "$Name started: pid=$($process.Id) config=$($RunnerArgs[0])"
    return [int]$process.Id
}

function Stop-AutotraderRunner {
    $stopPath = Join-Path $logs "market-autotrader.stop"
    Set-Content -LiteralPath $stopPath -Value "stop" -Encoding ASCII
    $pidPath = Join-Path $logs "market-autotrader.pid"
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
    if (Test-Path -LiteralPath $stopPath) {
        Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
    }
    Write-Output "autotrader runner stopped"
}

if ($RestartAutotraderOnly) {
    Stop-AutotraderRunner
} else {
    & (Join-Path $root "arm_live_trading.ps1")
    if (-not $SkipDashboard) {
        & (Join-Path $root "start_trading_dashboard.ps1")
    }
}

$tunnelResult = & python (Join-Path $root "scripts/ensure_socks_jump_tunnel.py") --config $AutotraderConfig
Write-Output "socks jump tunnel: $tunnelResult"

$autotraderPid = Start-BackgroundRunner `
    -Name "live autotrader" `
    -RunnerCmd (Join-Path $root "market_autotrader_runner.cmd") `
    -RunnerArgs @($AutotraderConfig, $AutotraderDelaySeconds) `
    -PidFile (Join-Path $logs "market-autotrader.pid")

Write-Output ""
Write-Output "Live autotrader running on growth config."
Write-Output "  pid=$autotraderPid config=$AutotraderConfig ledger=logs/market-autotrader-live-decisions.jsonl"
Write-Output "  armed: logs/live-trading.armed | emergency: .\kill_live_trading.ps1"
Write-Output "Stop autotrader only: .\start_live_autonomous_trading.ps1 -RestartAutotraderOnly (then stop pid manually) or .\stop_autonomous_trading.ps1"

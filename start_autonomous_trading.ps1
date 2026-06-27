param(
    [string]$SupervisorConfig = "realtime_supervisor.example.json",
    [string]$AutotraderConfig = "market_autotrader.autonomous.example.json",
    [int]$SupervisorDelaySeconds = 30,
    [int]$AutotraderDelaySeconds = 60,
    [switch]$SkipDashboard
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$stopFiles = @(
    "realtime-supervisor.stop",
    "market-autotrader.stop"
)
foreach ($name in $stopFiles) {
    $path = Join-Path $logs $name
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
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
    Write-Output "$Name started: pid=$($process.Id)"
    return $process.Id
}

$supervisorPid = Start-BackgroundRunner `
    -Name "realtime supervisor" `
    -RunnerCmd (Join-Path $root "realtime_supervisor_runner.cmd") `
    -RunnerArgs @($SupervisorConfig, $SupervisorDelaySeconds) `
    -PidFile (Join-Path $logs "realtime-supervisor.pid")

$autotraderPid = Start-BackgroundRunner `
    -Name "paper autotrader" `
    -RunnerCmd (Join-Path $root "market_autotrader_runner.cmd") `
    -RunnerArgs @($AutotraderConfig, $AutotraderDelaySeconds) `
    -PidFile (Join-Path $logs "market-autotrader.pid")

if (-not $SkipDashboard) {
    & (Join-Path $root "start_trading_dashboard.ps1")
}

$modeLabel = "paper"
try {
    $cfgRaw = Get-Content -LiteralPath (Join-Path $root $AutotraderConfig) -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfgRaw.risk.mode -eq "live" -or $cfgRaw.execution.mode -eq "live") {
        $modeLabel = "LIVE (real orders when signals pass gates)"
    }
} catch {
    $modeLabel = "paper"
}

Write-Output ""
Write-Output "Autonomous trading stack is running ($modeLabel + read-only supervisor)."
Write-Output "  supervisor pid=$supervisorPid ledger=logs/realtime-supervisor.jsonl"
Write-Output "  autotrader pid=$autotraderPid config=$AutotraderConfig ledger=$(if ($modeLabel -like 'LIVE*') { 'logs/market-autotrader-live-decisions.jsonl' } else { 'logs/market-autotrader-autonomous-decisions.jsonl' })"
try {
    $pipeEnabled = $cfgRaw.pipeline.enabled
    if ($pipeEnabled) {
        Write-Output "  pipeline enabled: logs/pipeline/latest-*.json (MarketPoller -> Decision -> Execution)"
    }
} catch {
    # pipeline section optional
}
Write-Output "  dashboard http://127.0.0.1:8765"
if ($modeLabel -like "LIVE*") {
    Write-Output "  live armed: logs/live-trading.armed | emergency: .\kill_live_trading.ps1"
}
Write-Output "Stop with: .\stop_autonomous_trading.ps1"

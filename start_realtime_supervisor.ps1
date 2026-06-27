param(
    [string]$ConfigPath = "realtime_supervisor.example.json",
    [int]$LoopDelaySeconds = 30
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
$runner = Join-Path $root "realtime_supervisor_runner.cmd"
$pidPath = Join-Path $logs "realtime-supervisor.pid"
$stopPath = Join-Path $logs "realtime-supervisor.stop"

New-Item -ItemType Directory -Force -Path $logs | Out-Null
if (Test-Path -LiteralPath $stopPath) {
    Remove-Item -LiteralPath $stopPath -Force
}

$existingPid = if (Test-Path -LiteralPath $pidPath) { (Get-Content -LiteralPath $pidPath -Raw).Trim() } else { "" }
if ($existingPid) {
    $existing = Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Output "realtime supervisor already running: pid=$existingPid"
        exit 0
    }
}

$commandLine = "/d /s /c """"$runner"" ""$ConfigPath"" $LoopDelaySeconds"""
$process = Start-Process -FilePath "cmd.exe" `
    -ArgumentList $commandLine `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ASCII
Write-Output "realtime supervisor runner started: pid=$($process.Id)"
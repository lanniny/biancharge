param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
$runner = Join-Path $root "trading_dashboard_runner.cmd"
$pidPath = Join-Path $logs "trading-dashboard.pid"

New-Item -ItemType Directory -Force -Path $logs | Out-Null

$oldPidText = if (Test-Path -LiteralPath $pidPath) { (Get-Content -LiteralPath $pidPath -Raw).Trim() } else { "" }
if ($oldPidText) {
    $oldProcess = Get-Process -Id ([int]$oldPidText) -ErrorAction SilentlyContinue
    if ($oldProcess) {
        Write-Output "trading dashboard already running: pid=$oldPidText"
        exit 0
    }
}

$commandLine = "/d /s /c """"$runner"" ""$HostName"" $Port"""
$process = Start-Process -FilePath "cmd.exe" `
    -ArgumentList $commandLine `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ASCII
Start-Sleep -Seconds 1

$started = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
if ($started) {
    Write-Output "trading dashboard started: http://$HostName`:$Port pid=$($process.Id)"
} else {
    Write-Output "trading dashboard failed to stay running"
    exit 1
}
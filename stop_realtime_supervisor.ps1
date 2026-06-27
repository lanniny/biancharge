$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
$pidPath = Join-Path $logs "realtime-supervisor.pid"
$stopPath = Join-Path $logs "realtime-supervisor.stop"

New-Item -ItemType Directory -Force -Path $logs | Out-Null
Set-Content -LiteralPath $stopPath -Value (Get-Date -Format o) -Encoding ASCII

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Output "realtime supervisor pid file not found"
    exit 0
}

$pidRaw = Get-Content -LiteralPath $pidPath -Raw -ErrorAction SilentlyContinue
$pidText = if ($null -eq $pidRaw) { "" } else { $pidRaw.Trim() }
if (-not $pidText) {
    Write-Output "realtime supervisor pid file is empty"
    exit 0
}

$process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id ([int]$pidText) -Force
    Write-Output "realtime supervisor stopped: pid=$pidText"
} else {
    Write-Output "realtime supervisor process not running: pid=$pidText"
}
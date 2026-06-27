param(
    [string]$KillPath = "logs/live-trading.kill"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root $KillPath

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
$payload = @{
    killedAt = (Get-Date).ToString("o")
    note = "Emergency kill switch. Delete this file after review to allow future arming."
} | ConvertTo-Json -Compress

Set-Content -LiteralPath $target -Value $payload -Encoding UTF8
Write-Output "Live trading KILLED: $target"
Write-Output "All live order attempts will be blocked until this file is removed."

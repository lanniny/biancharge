param(
    [string]$ArmPath = "logs/live-trading.armed"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root $ArmPath

if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Force
    Write-Output "Live trading DISARMED: removed $target"
} else {
    Write-Output "Live trading was not armed ($target missing)."
}

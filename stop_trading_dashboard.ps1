$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidPath = Join-Path $root "logs\trading-dashboard.pid"
$port = 8765

function Stop-PortListeners {
    param([int]$TargetPort)
    $connections = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
    foreach ($conn in $connections) {
        $owner = $conn.OwningProcess
        if ($owner -and $owner -gt 0) {
            Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
            Write-Output "stopped listener on port ${TargetPort}: pid=$owner"
        }
    }
}

if (Test-Path -LiteralPath $pidPath) {
    $pidRaw = Get-Content -LiteralPath $pidPath -Raw -ErrorAction SilentlyContinue
    $dashPidText = if ($null -eq $pidRaw) { "" } else { $pidRaw.Trim() }
    if ($dashPidText) {
        $process = Get-Process -Id ([int]$dashPidText) -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -Id ([int]$dashPidText) -Force -ErrorAction SilentlyContinue
            Write-Output "trading dashboard stopped: pid=$dashPidText"
        } else {
            Write-Output "trading dashboard process not running: pid=$dashPidText"
        }
    }
}

Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*trading_dashboard.py*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Output "stopped trading_dashboard.py: pid=$($_.ProcessId)"
    }

Stop-PortListeners -TargetPort $port
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue

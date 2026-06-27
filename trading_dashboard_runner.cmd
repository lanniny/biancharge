@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "HOST=%~1"
set "PORT=%~2"
if "%HOST%"=="" set "HOST=127.0.0.1"
if "%PORT%"=="" set "PORT=8765"

cd /d "%ROOT%"
if not exist "logs" mkdir "logs"

echo [%date% %time%] trading dashboard runner started > "logs\trading-dashboard-runner.log"
python -u "trading_dashboard.py" --host "%HOST%" --port "%PORT%" >> "logs\trading-dashboard.stdout.log" 2>> "logs\trading-dashboard.stderr.log"
echo [%date% %time%] trading dashboard runner stopped exit=%ERRORLEVEL% >> "logs\trading-dashboard-runner.log"
endlocal
@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CONFIG=%~1"
set "LOOP_DELAY=%~2"
if "%CONFIG%"=="" set "CONFIG=realtime_supervisor.example.json"
if "%LOOP_DELAY%"=="" set "LOOP_DELAY=30"

cd /d "%ROOT%"
if not exist "logs" mkdir "logs"

echo [%date% %time%] realtime supervisor cmd runner started > "logs\realtime-supervisor-runner.log"

:loop
if exist "logs\realtime-supervisor.stop" goto stopped
echo cycle starting %date% %time% > "logs\realtime-supervisor-heartbeat.txt"
python -u "realtime_supervisor.py" --config "%CONFIG%" --once >> "logs\realtime-supervisor.stdout.log" 2>> "logs\realtime-supervisor.stderr.log"
echo [%date% %time%] cycle finished exit=%ERRORLEVEL% >> "logs\realtime-supervisor-runner.log"
if exist "logs\realtime-supervisor.stop" goto stopped
echo cycle sleeping %date% %time% > "logs\realtime-supervisor-heartbeat.txt"
powershell.exe -NoProfile -Command "Start-Sleep -Seconds %LOOP_DELAY%" >nul 2>nul
goto loop

:stopped
echo [%date% %time%] realtime supervisor cmd runner stopped >> "logs\realtime-supervisor-runner.log"
endlocal
@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CONFIG=%~1"
set "LOOP_DELAY=%~2"
if "%CONFIG%"=="" set "CONFIG=market_autotrader.growth.example.json"
if "%LOOP_DELAY%"=="" set "LOOP_DELAY=60"

cd /d "%ROOT%"
if not exist "logs" mkdir "logs"

echo [%date% %time%] market autotrader cmd runner started > "logs\market-autotrader-runner.log"

:loop
if exist "logs\market-autotrader.stop" goto stopped
echo cycle starting %date% %time% > "logs\market-autotrader-heartbeat.txt"
python -u "market_autotrader.py" --config "%CONFIG%" --once >> "logs\market-autotrader.stdout.log" 2>> "logs\market-autotrader.stderr.log"
echo [%date% %time%] cycle finished exit=%ERRORLEVEL% >> "logs\market-autotrader-runner.log"
if exist "logs\market-autotrader.stop" goto stopped
echo cycle sleeping %date% %time% > "logs\market-autotrader-heartbeat.txt"
powershell.exe -NoProfile -Command "Start-Sleep -Seconds %LOOP_DELAY%" >nul 2>nul
goto loop

:stopped
echo [%date% %time%] market autotrader cmd runner stopped >> "logs\market-autotrader-runner.log"
endlocal

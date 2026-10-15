@echo off
rem One-click start of DiceDriver (reads DiceDriver\dicedriver.toml).
rem Runs under the supervisor: every exit lands in logs\supervisor.log, a crash gets an incident
rem report, and a driver that dies is restarted instead of staying dead.
rem Stop it with Ctrl+C in this window (the supervisor does not restart a clean stop).
setlocal
title DiceDriver
cd /d "%~dp0DiceDriver"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv is missing. Run build-dicedriver.bat first.
    pause
    exit /b 1
)
if not exist "dicedriver.toml" (
    echo [ERROR] dicedriver.toml is missing. Copy dicedriver.example.toml to dicedriver.toml and edit it.
    pause
    exit /b 1
)
if not exist "logs" mkdir "logs"

echo Starting DiceDriver under the supervisor ... press Ctrl+C to stop.
echo   config : %~dp0DiceDriver\dicedriver.toml
echo   logs   : %~dp0DiceDriver\logs  (dicedriver.log, crash.log, supervisor.log, incidents\)
echo   data   : %~dp0data
".venv\Scripts\python.exe" -m dicedriver --config "%~dp0DiceDriver\dicedriver.toml" --supervise %*

echo.
echo DiceDriver stopped with errorlevel %errorlevel% (%date% %time%).
>> "logs\launcher.log" echo %date% %time% DiceDriver stopped with errorlevel %errorlevel%
echo Press any key to close this window - the driver has already stopped, this is not a hang.
pause
exit /b %errorlevel%
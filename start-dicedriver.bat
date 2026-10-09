@echo off
rem One-click start of DiceDriver (reads DiceDriver\dicedriver.toml).
rem Stop it with Ctrl+C in this window.
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

echo Starting DiceDriver ... press Ctrl+C to stop.
echo   config : %~dp0DiceDriver\dicedriver.toml
echo   logs   : %~dp0DiceDriver\logs
echo   data   : %~dp0data
".venv\Scripts\python.exe" -m dicedriver --config "%~dp0DiceDriver\dicedriver.toml" %*

echo.
echo DiceDriver exited with errorlevel %errorlevel%.
pause
exit /b %errorlevel%
@echo off
rem One-click build of DiceDriver: Python environment + native shim DLL.
rem Produces: output\dd_shim.dll
rem Run this after changing DiceDriver\shim\dd_shim.cpp, or on a fresh checkout.
setlocal
title build DiceDriver
cd /d "%~dp0DiceDriver"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] creating virtual environment .venv ...
    py -3.11 -m venv .venv 2>nul
    if not exist ".venv\Scripts\python.exe" python -m venv .venv
) else (
    echo [1/3] virtual environment .venv already exists
)
if not exist ".venv\Scripts\python.exe" goto fail

echo [2/3] installing Python dependencies ...
".venv\Scripts\python.exe" -m pip install -q -e ".[dev]"
if errorlevel 1 goto fail

echo [3/3] building native shim DLL (x64) ...
powershell -NoProfile -ExecutionPolicy Bypass -File "build-shim.ps1"
if errorlevel 1 goto fail

echo.
echo [OK] DiceDriver ready. Shim: "%~dp0output\dd_shim.dll"
echo Run start-dicedriver.bat to launch it.
pause
exit /b 0

:fail
echo.
echo [FAILED] See the messages above.
pause
exit /b 1
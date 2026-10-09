@echo off
rem One-click build of the Dice plugin DLL (x64) -> output\w4123.Dice.windows.amd64.dll
rem   build-dicesrc.bat              full build (configures vcpkg dependencies; slow the first time)
rem   build-dicesrc.bat -BuildOnly   incremental build only (after C++ source edits)
setlocal
title build DiceSrc (x64)
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0DiceSrc\build-dice.ps1" %*
if errorlevel 1 goto fail

echo.
echo [OK] Dice DLL:
dir /b "%~dp0output\w4123.Dice*.dll"
echo.
echo Intermediate files live in "%~dp0build". Build log: "%~dp0build\logs".
pause
exit /b 0

:fail
echo.
echo [FAILED] Build failed. Logs are in "%~dp0build\logs" (see the last lines printed above).
pause
exit /b 1
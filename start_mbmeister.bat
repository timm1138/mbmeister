@echo off
rem Double-click launcher for MBMeister (runs start_mbmeister.ps1 without changing the PowerShell policy)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_mbmeister.ps1" %*
if errorlevel 1 (
    echo.
    echo MBMeister exited with an error.
    pause
)

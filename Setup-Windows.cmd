@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-windows.ps1"
if errorlevel 1 (
  echo.
  echo Setup failed. Please review the error above.
  pause
  exit /b 1
)
echo.
echo Setup completed.
pause

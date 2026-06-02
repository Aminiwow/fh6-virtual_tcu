@echo off
setlocal
cd /d "%~dp0"

echo VirtualTCU release helper
echo.
echo Examples:
echo   Release.bat
echo   Release.bat -Version 13.4.2
echo   Release.bat -Part minor
echo   Release.bat -Yes
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\release.ps1" %*
set EXITCODE=%ERRORLEVEL%

echo.
echo Release script exited with code %EXITCODE%.
pause
exit /b %EXITCODE%

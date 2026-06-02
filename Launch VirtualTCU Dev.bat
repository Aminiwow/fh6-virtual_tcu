@echo off
setlocal
cd /d "%~dp0"

echo Starting VirtualTCU local Electron panel...
echo.
echo If the installed VirtualTCU is still running, quit it from the tray first.
echo The local panel uses this folder's tcu_config.json, tcu_profiles.json, and logs\.
echo.

corepack pnpm --filter virtual-tcu dev
echo.
echo VirtualTCU dev panel exited with code %errorlevel%.
pause

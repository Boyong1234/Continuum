@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\portable.ps1" -Action Stop %*
set "continuum_exit=%errorlevel%"
if not "%CONTINUUM_NO_PAUSE%"=="1" pause
exit /b %continuum_exit%

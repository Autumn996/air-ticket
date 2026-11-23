@echo off
chcp 65001 >nul
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0enable-monitor.ps1"
if errorlevel 1 echo Setup did not complete. See the message above.
pause

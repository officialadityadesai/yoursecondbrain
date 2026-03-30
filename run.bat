@echo off
setlocal
set ROOT=%~dp0
echo Starting My Second Brain in background mode...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\start-background.ps1"
echo Opening My Second Brain...
start http://127.0.0.1:8000
endlocal

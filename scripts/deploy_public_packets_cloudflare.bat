@echo off
setlocal

cd /d "%~dp0\.."

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_public_packets_cloudflare.ps1"

if errorlevel 1 (
    echo.
    echo Public packet deployment failed. Check the message above.
    pause
    exit /b 1
)

endlocal

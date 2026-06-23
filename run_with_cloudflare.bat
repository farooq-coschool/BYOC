@echo off
setlocal EnableDelayedExpansion
title Question Generator — Cloudflare Tunnel

echo ========================================================
echo  Question Generator  ^|  Cloudflare Quick Tunnel
echo ========================================================
echo.

REM ── 1. Find or download cloudflared ──────────────────────
set CF=
where cloudflared >nul 2>&1 && set CF=cloudflared
if not defined CF (
    if exist "%~dp0cloudflared.exe" (
        set CF="%~dp0cloudflared.exe"
    ) else (
        echo cloudflared not found — downloading...
        curl -L --progress-bar "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -o "%~dp0cloudflared.exe"
        if errorlevel 1 (
            echo.
            echo  ERROR: Download failed. Install cloudflared manually:
            echo  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
            pause
            exit /b 1
        )
        set CF="%~dp0cloudflared.exe"
        echo Downloaded cloudflared.exe
    )
)

REM ── 2. Start Flask in a new window ───────────────────────
echo Starting Flask on http://localhost:5001 ...
start "Question Generator (Flask)" /D "%~dp0" cmd /k "python app.py"

echo Waiting for Flask to start...
timeout /t 5 /nobreak >nul

REM ── 3. Start Cloudflare Quick Tunnel ─────────────────────
echo.
echo ========================================================
echo  Cloudflare tunnel starting...
echo  Look for a line like:
echo     https://xxxx-xxxx-xxxx.trycloudflare.com
echo  Share that URL — anyone can open it in a browser.
echo  Close this window to shut down the tunnel.
echo ========================================================
echo.

%CF% tunnel --url http://localhost:5001

echo.
echo Tunnel closed.
pause

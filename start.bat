@echo off
title Mimicry
cd /d "%~dp0"

echo.
echo  ==========================================
echo   Mimicry
echo  ==========================================
echo.
echo  Starting server at http://localhost:8000
echo  Press Ctrl+C to stop.
echo.

python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000

pause

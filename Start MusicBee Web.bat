@echo off
rem ============================================================
rem  MusicBee Web - start your browser music player
rem  Double-click this file. Then open:  http://localhost:8321
rem  If an old instance is still running it gets stopped first,
rem  so you can just double-click again any time to restart.
rem  Close the window (or press Ctrl+C) to stop the server.
rem ============================================================
setlocal EnableExtensions
title MusicBee Web
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed or not on PATH.
  echo Install it from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)
rem --- stop any previous instance still holding our port ---
set "PORT=8321"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT%.*LISTENING"') do (
  for /f "delims=" %%n in ('tasklist /fi "PID eq %%p" /fo list ^| findstr /i "python"') do (
    echo Stopping previous instance ^(PID %%p^)...
    taskkill /F /PID %%p >nul 2>&1
  )
)
rem give the OS a moment to release the port
timeout /t 1 /nobreak >nul
python musicbee_web.py %*
pause